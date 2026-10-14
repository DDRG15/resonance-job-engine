"""
progreso.py — avance y tiempo estimado de la corrida en curso (Pieza 4 del plan 10-13).

Escenario: una corrida dura 10-20 minutos y hasta ahora la UI solo mostraba líneas de log.
Sin una señal de avance una corrida viva es indistinguible de una colgada; el usuario cierra
la ventana, relanza, y la segunda choca con el gate (409). Este módulo es esa señal.

QUÉ MIDE
--------
Unidades terminadas, no reloj. Tres clases, porque cuestan distinto y se estiman distinto:

    endpoint   un board que se lee con un GET           (scraper._scrape_endpoint_boards)
    ats        una empresa de Greenhouse/Lever/Ashby…   (scraper._scrape_ats_sources)
    css        una rama (dominio x título) del navegador (scraper.GemaScraper.run, _bounded)

Una rama CSS que el probe o el circuit breaker saltean cuenta como TERMINADA: si no, el total
incluiría unidades que nunca van a correr y la barra no llegaría al 100%.

DÓNDE VIVE EL ESTADO
--------------------
En memoria, global del proceso, protegido por un lock. La corrida escribe desde su propio
hilo (y, adentro, desde un event loop y desde threads de executor) y la API lee desde el
pool de FastAPI: sin lock, un snapshot podría ver `completadas` de una clase y `total` de la
otra a medio actualizar. El gate de scraping garantiza UNA corrida por proceso, así que un
solo estado alcanza.

Todas las funciones de escritura son NO-OP si no hay corrida iniciada: `run_scrape_session`
se llama también desde scripts y tests sin pasar por `run_pipeline`, y el scraper no tiene
que saber quién lo invoca. Ninguna levanta: la barra de progreso jamás tumba una corrida.

LA ETA
------
    restante_s = Σ_clase  unidades_restantes × mediana_exec_ms(clase) / 1000 / concurrencia

con la mediana de `exec_ms` de esa clase (nodos `fuente_endpoint`, `fuente_ats`,
`scrape_srp` de `node_traces`) en las últimas 5 corridas. La concurrencia es la real de cada
clase: el loop de endpoints es secuencial (1); las ramas CSS corren con
Semaphore(SCRAPER_CONCURRENCY); ATS corre en paralelo dentro del límite de cada plataforma
(`CONCURRENCIA_ATS`, aproximada: depende de la mezcla de plataformas).

Si una clase no tiene historial, su parte sale de la mediana de duración total de las
corridas anteriores (tabla `runs`) prorrateada por la fracción de unidades que le faltan.
Sin ninguna corrida previa: `restante_s = None` y la UI dice «calculando…».

Pasado el estimado TOTAL (lo que habría tardado la corrida entera según el historial), la
estimación deja de ser útil: la corrida está yendo más lenta que siempre. Ahí
`excedido = True` y `restante_s = None`. Nunca se devuelve una cuenta regresiva negativa.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import config

logger = logging.getLogger("gema.progreso")

#: Orden de las fases. `ats` y `gmail` pueden no darse (sin fuentes activas): la fase
#: salta, no se queda esperando una que no corre.
FASES = ("endpoints", "ats", "gmail", "navegador", "puntuando", "listo")

#: Clase de unidad -> nodo de `node_traces` que mide su tiempo.
NODO_DE_CLASE = {
    "endpoint": "fuente_endpoint",
    "ats": "fuente_ats",
    "css": "scrape_srp",
}

#: Cuántas corridas previas entran a la mediana.
ULTIMAS_CORRIDAS = 5

#: Empresas ATS en vuelo a la vez, en promedio. Cada plataforma tiene su semáforo (1 a 6
#: según lo que declare su robots.txt) y corren en paralelo entre sí; 4 es el punto medio
#: conservador. Sobreestimar la concurrencia subestima el tiempo, que es el error caro:
#: la UI prometería «2 min» y tardaría 6.
CONCURRENCIA_ATS = 4

#: Cuántas empresas ATS se piden por lote (ver scraper._scrape_ats_sources).
LOTE_ATS = 10


def _concurrencia(clase: str) -> int:
    if clase == "css":
        return max(1, int(getattr(config, "SCRAPER_CONCURRENCY", 1) or 1))
    if clase == "ats":
        return CONCURRENCIA_ATS
    return 1  # endpoint: el loop es secuencial a propósito (respeta crawl-delay)


class _Estado:
    __slots__ = ("activo", "run_id", "fase", "total", "hechas", "iniciado",
                 "ultima_actividad", "historial", "duracion_mediana_s")

    def __init__(self) -> None:
        self.activo = False
        self.run_id: Optional[str] = None
        self.fase = "endpoints"
        self.total = {c: 0 for c in NODO_DE_CLASE}
        self.hechas = {c: 0 for c in NODO_DE_CLASE}
        self.iniciado = 0.0
        self.ultima_actividad = 0.0
        # clase -> {"mediana_ms", "ejecutadas", "filas"} | ausente si no hay historial
        self.historial: dict[str, dict] = {}
        self.duracion_mediana_s: Optional[float] = None


_lock = threading.Lock()
_est = _Estado()

#: Reloj monotónico. Es un atributo del módulo para que los tests lo controlen sin dormir.
_reloj: Callable[[], float] = time.monotonic


# =============================================================================
# Escritura — la llama la corrida
# =============================================================================

def iniciar(run_id: Optional[str], db=None) -> None:
    """Abre el seguimiento de una corrida y carga el historial para la ETA."""
    global _est
    try:
        historial: dict[str, dict] = {}
        duracion: Optional[float] = None
        if db is not None:
            # Se lee UNA vez por corrida, antes de scrapear: las medianas no cambian
            # mientras la corrida corre, y el snapshot se pide cada medio segundo.
            for clase, nodo in NODO_DE_CLASE.items():
                h = db.tiempo_por_unidad_ms(nodo, excluir_run_id=run_id,
                                            ultimas=ULTIMAS_CORRIDAS)
                if isinstance(h, dict) and h.get("mediana_ms"):
                    historial[clase] = h
            d = db.duracion_mediana_corridas_s(excluir_run_id=run_id,
                                               ultimas=ULTIMAS_CORRIDAS)
            duracion = float(d) if isinstance(d, (int, float)) and d > 0 else None
        ahora = _reloj()
        with _lock:
            e = _Estado()
            e.activo = True
            e.run_id = run_id
            e.iniciado = ahora
            e.ultima_actividad = ahora
            e.historial = historial
            e.duracion_mediana_s = duracion
            _est = e
    except Exception as exc:  # pragma: no cover - defensivo
        logger.warning("[PROGRESO] no se pudo iniciar: %s", exc)


def finalizar() -> None:
    """Cierra el seguimiento. Después de esto `snapshot()` devuelve None."""
    with _lock:
        if _est.activo:
            _est.fase = "listo"
            _est.activo = False
            _est.ultima_actividad = _reloj()


def cambiar_fase(fase: str) -> None:
    if fase not in FASES:
        return
    with _lock:
        if not _est.activo:
            return
        _est.fase = fase
        _est.ultima_actividad = _reloj()


def fijar_total(clase: str, total: int) -> None:
    """Declara cuántas unidades tiene una clase. Se puede llamar de nuevo para corregir."""
    if clase not in NODO_DE_CLASE:
        return
    with _lock:
        if not _est.activo:
            return
        _est.total[clase] = max(0, int(total))
        _est.ultima_actividad = _reloj()


def unidad_hecha(clase: str, n: int = 1) -> None:
    """Suma `n` unidades terminadas (con éxito, con error o salteadas: todas cuentan)."""
    if clase not in NODO_DE_CLASE or n <= 0:
        return
    with _lock:
        if not _est.activo:
            return
        _est.hechas[clase] += int(n)
        # Un total subestimado no puede dejar completadas > total (porcentaje > 100).
        if _est.hechas[clase] > _est.total[clase]:
            _est.total[clase] = _est.hechas[clase]
        _est.ultima_actividad = _reloj()


def latido() -> None:
    """Marca actividad sin tocar unidades (una línea de log, un item procesado)."""
    with _lock:
        if _est.activo:
            _est.ultima_actividad = _reloj()


# =============================================================================
# Lectura — la llama la API
# =============================================================================

def _estimar(est: _Estado, transcurrido: float) -> tuple[Optional[float], bool, Optional[str]]:
    """(restante_s, excedido, estimado_desde) a partir de una copia consistente."""
    pendientes = {c: max(0, est.total[c] - est.hechas[c]) for c in est.total}
    if not any(pendientes.values()):
        return 0.0, False, None   # nada por scrapear: solo queda puntuar y guardar

    total_unidades = sum(est.total.values())
    restante = 0.0
    total_estimado = 0.0
    usa_historial = usa_corridas = False
    for clase, pend in pendientes.items():
        tot = est.total[clase]
        if tot == 0:
            continue
        h = est.historial.get(clase)
        if h:
            # Ponderar por la fracción de ramas que de verdad corren: si el 40% de las
            # ramas CSS suelen saltarse, no todas las pendientes cuestan la mediana.
            ejecuta = h["ejecutadas"] / h["filas"] if h["filas"] else 1.0
            seg = h["mediana_ms"] / 1000.0 * ejecuta / _concurrencia(clase)
            restante += pend * seg
            total_estimado += tot * seg
            usa_historial = True
        elif est.duracion_mediana_s:
            frac = tot / total_unidades if total_unidades else 0.0
            restante += est.duracion_mediana_s * (pend / total_unidades)
            total_estimado += est.duracion_mediana_s * frac
            usa_corridas = True
        else:
            # Una clase con unidades por correr y sin ninguna referencia: una ETA que
            # ignora esa parte sería un número falso y optimista. Mejor «calculando…».
            return None, False, None

    # «corridas» gana si CUALQUIER clase cayó al respaldo: es la fuente menos precisa y
    # la UI no debe presentar como exacta una cuenta que mezcla las dos.
    desde = "corridas" if usa_corridas else ("historial" if usa_historial else None)
    if total_estimado > 0 and transcurrido > total_estimado:
        return None, True, desde
    return restante, False, desde


def snapshot() -> Optional[dict]:
    """El bloque `progreso` de /api/status, o None si no hay corrida en curso."""
    with _lock:
        if not _est.activo:
            return None
        ahora = _reloj()
        e = _Estado()
        e.fase, e.total, e.hechas = _est.fase, dict(_est.total), dict(_est.hechas)
        e.historial, e.duracion_mediana_s = _est.historial, _est.duracion_mediana_s
        iniciado, ultima = _est.iniciado, _est.ultima_actividad

    transcurrido = max(0.0, ahora - iniciado)
    total = sum(e.total.values())
    hechas = min(sum(e.hechas.values()), total)
    # 99 máximo mientras no esté `listo`: llegar al 100% con la corrida todavía puntuando
    # y guardando haría creer que terminó. `listo` lo marca finalizar().
    porcentaje = min(99, int(hechas * 100 / total)) if total else 0

    restante, excedido, desde = _estimar(e, transcurrido)
    return {
        "fase": e.fase,
        "porcentaje": porcentaje,
        "completadas": hechas,
        "total": total,
        "transcurrido_s": int(transcurrido),
        "restante_s": None if restante is None else int(round(restante)),
        "excedido": excedido,
        "ultima_actividad_hace_s": int(max(0.0, ahora - ultima)),
        "estimado_desde": desde,
    }
