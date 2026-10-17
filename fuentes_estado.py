"""
fuentes_estado.py — ¿esta fuente dice la verdad?

Una fuente que antes traía avisos y hoy trae cero no está vacía y no dio error: está
SOSPECHOSA. Hasta el 29-09 GEMA no tenía esa palabra. trueup.io devolvió cero avisos en
51 búsquedas seguidas, gastó 1.618 s de corrida en eso, y la única forma de enterarse fue
sumar `exec_ms` de `node_traces` a mano — la tabla sabía cuánto tardó cada rama y no
cuánto trajo. Endpoints y ATS ni siquiera escribían traza.

La regla se decide por CONTEO contra el historial de la fuente, nunca por el código HTTP:

    items = 0 y alguna de las últimas N corridas medidas trajo avisos  -> sospechosa
    items = 0 y ninguna de ellas trajo avisos                           -> sin rendimiento
    items > 0                                                           -> sana
    items sin medir (corrida anterior a la columna, o la rama no leyó)  -> sin medir

El código HTTP no sirve porque miente en las dos direcciones. Ashby devuelve 200 con un
arreglo vacío tanto si el board no existe como si te está limitando; Workable y Recruitee
devuelven 404 si la empresa no existe y BambooHR un 302 (medido
el 2026-09-30). Un cero contra un historial con avisos dice lo mismo en todos los casos:
algo que antes se leía ya no se lee.

Este módulo no tiene estado propio: lee `node_traces` y emite métricas. Ninguna función de
escritura levanta excepciones — la telemetría que puede tumbar la corrida que mide se
termina apagando, y entonces no mide nada.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Iterable, Optional

import config
import metrics

logger = logging.getLogger(__name__)

SANA = "sana"
SOSPECHOSA = "sospechosa"
SIN_RENDIMIENTO = "sin_rendimiento"
SIN_MEDIR = "sin_medir"
ESTADOS = (SOSPECHOSA, SIN_RENDIMIENTO, SIN_MEDIR, SANA)   # del más urgente al menos

NODO_CSS = "scrape_srp"
NODO_ENDPOINT = "fuente_endpoint"
NODO_ATS = "fuente_ats"

# El número que devuelve `get_items_por_fuente_y_corrida` en la columna `tipo`.
_TIPOS = {1: "css", 2: "endpoint", 3: "ats"}


# =============================================================================
# La regla — pura, sin base de datos
# =============================================================================

def clasificar(items_actual: Optional[int], historial_previo: Iterable[Optional[int]],
               ventana: Optional[int] = None) -> str:
    """
    El estado de una fuente a partir de lo que trajo ahora y de lo que trajo antes.

    `historial_previo` va de la corrida más reciente a la más vieja y NO incluye la
    actual. Los None (corridas sin medir) se descartan antes de recortar a la ventana:
    una corrida anterior a la columna `items` no es un cero, y tampoco ocupa lugar en
    las N que cuentan como "reciente".
    """
    if items_actual is None:
        return SIN_MEDIR
    if items_actual > 0:
        return SANA
    n = config.FUENTE_VENTANA_CORRIDAS if ventana is None else ventana
    recientes = [h for h in historial_previo if h is not None][:max(0, n)]
    return SOSPECHOSA if any(h > 0 for h in recientes) else SIN_RENDIMIENTO


def _dia(iso: Optional[str]) -> str:
    return (iso or "")[:10] or "?"


def _motivo(estado: str, items: Optional[int], hist: list[int],
            ultima_con_avisos: Optional[str], medidas: int) -> str:
    """La razón en una línea, para que quien ve el panel no tenga que abrir el código."""
    if estado == SANA:
        return f"{items} avisos en la última corrida"
    if estado == SOSPECHOSA:
        return (f"cayó a 0 avisos; en las {len(hist)} corridas medidas anteriores llegó a "
                f"traer {max(hist)} (último aviso: {_dia(ultima_con_avisos)})")
    if estado == SIN_RENDIMIENTO:
        if ultima_con_avisos:
            return (f"0 avisos ahora y en las {len(hist)} corridas medidas anteriores; "
                    f"su último aviso fue el {_dia(ultima_con_avisos)}")
        return f"nunca produjo un aviso en {medidas} corrida(s) medida(s)"
    return ("la última corrida es anterior a la medición de avisos, o la rama no "
            "consultó la fuente")


def estados_por_fuente(filas: list[dict], *, ventana: Optional[int] = None,
                       run_id: Optional[str] = None) -> list[dict]:
    """
    Una entrada por fuente, con su estado.

    `filas` es la salida de `GemaDatabase.get_items_por_fuente_y_corrida`. Con `run_id`
    se evalúa ESA corrida contra las anteriores (lo que hace el scraper al cerrar una
    corrida); sin él se evalúa la más reciente de cada fuente (lo que muestra el panel).
    """
    n = config.FUENTE_VENTANA_CORRIDAS if ventana is None else ventana
    por_fuente: dict[str, list[dict]] = {}
    for f in filas:
        por_fuente.setdefault(f["domain"], []).append(f)

    salida: list[dict] = []
    for dominio, corridas in por_fuente.items():
        corridas = sorted(corridas, key=lambda r: r.get("inicio") or "", reverse=True)
        idx = 0
        if run_id is not None:
            idx = next((i for i, r in enumerate(corridas) if r["run_id"] == run_id), -1)
            if idx < 0:
                continue                      # la fuente no participó de esa corrida
        actual = corridas[idx]
        previas = corridas[idx + 1:]
        items = actual.get("items")
        medidas_previas = [r["items"] for r in previas if r.get("items") is not None]
        hist = medidas_previas[:max(0, n)]
        estado = clasificar(items, hist, n)

        # La fecha del último aviso mira TODO el historial y no solo la ventana: es lo
        # que distingue "nunca rindió" de "rindió hace tres meses" cuando el estado ya
        # no es sospechosa.
        con_avisos = next((r["inicio"] for r in previas if (r.get("items") or 0) > 0), None)
        if (items or 0) > 0:
            con_avisos = actual.get("inicio")

        salida.append({
            "fuente": dominio,
            "tipo": _TIPOS.get(actual.get("tipo"), "css"),
            "estado": estado,
            "items": items,
            "aceptados": actual.get("aceptados"),
            "run_id": actual["run_id"],
            "corrida": actual.get("inicio"),
            "historial": hist,
            "ultima_con_avisos": con_avisos,
            "corridas_medidas": len(medidas_previas) + (1 if items is not None else 0),
            "hubo_error": bool(actual.get("hubo_error")),
            "motivo": _motivo(estado, items, hist, con_avisos,
                              len(medidas_previas) + (1 if items is not None else 0)),
        })

    salida.sort(key=lambda e: (ESTADOS.index(e["estado"]), e["fuente"]))
    return salida


def resumen(estados: list[dict]) -> dict[str, int]:
    return {e: sum(1 for s in estados if s["estado"] == e) for e in ESTADOS}


def estado_de_fuentes(db, *, ventana: Optional[int] = None) -> dict:
    """Lo que sirve `/api/fuentes`: el estado de cada fuente en su corrida más reciente."""
    n = config.FUENTE_VENTANA_CORRIDAS if ventana is None else ventana
    estados = estados_por_fuente(db.get_items_por_fuente_y_corrida(), ventana=n)
    return {"fuentes": estados, "resumen": resumen(estados), "ventana_corridas": n}


# =============================================================================
# Métricas — el test de la 1 AM
# =============================================================================

def emitir_metricas(estados: list[dict]) -> None:
    """
    `fuente_items{fuente}` por cada fuente medida y `fuente_sospechosa_total` por cada
    una sospechosa. Un log que nadie lee no dispara ninguna alerta: la métrica sí.
    """
    for e in estados:
        if e["items"] is not None:
            metrics.emit("fuente_items", e["items"], fuente=e["fuente"], tipo=e["tipo"])
        if e["estado"] == SOSPECHOSA:
            metrics.emit("fuente_sospechosa_total", 1, fuente=e["fuente"], tipo=e["tipo"],
                         previos_max=max(e["historial"]) if e["historial"] else 0)


# =============================================================================
# Escritura de trazas de fuente — sincrónica, para correr en un executor
# =============================================================================

def evaluar_corrida_sync(db, run_id: Optional[str], dominios: Optional[Iterable[str]] = None
                         ) -> list[dict]:
    """Evalúa las fuentes de una corrida contra su historial y emite sus métricas."""
    if not run_id:
        return []
    try:
        filas = db.get_items_por_fuente_y_corrida()
        if dominios is not None:
            permitidos = set(dominios)
            filas = [f for f in filas if f["domain"] in permitidos]
        estados = estados_por_fuente(filas, run_id=run_id)
        emitir_metricas(estados)
        return estados
    except Exception as exc:
        logger.warning("[FUENTES] no se pudo evaluar la corrida %s: %s", run_id, exc)
        return []


def _fila(run_id, nodo, dominio, *, items, aceptados, error, ms) -> dict:
    return {
        "run_id": run_id, "node": nodo, "graph": "run", "domain": dominio, "title": None,
        "exec_ms": ms, "queue_wait_ms": None,
        "status": "error" if error else "ok", "error": error or None,
        "items": items, "aceptados": aceptados,
    }


def _endpoint_salteado(res) -> bool:
    """
    True si el board no consultó la fuente en esta corrida.

    Dos formas: un `saltado` explícito, o que el TTL de consulta (`ttl_consulta_s`, Himalayas
    20 h) haya omitido TODAS las URLs. En el segundo caso `ResultadoEndpoint` vuelve con
    `consultas` vacío y `items_crudos = 0`: anotarlo como traza con items=0 marcaba
    «sospechosa» a una fuente sana en la segunda corrida del día. Si se pidió aunque sea
    una URL, el cero es un cero medido y cuenta.
    """
    if getattr(res, "saltado", False):
        return True
    omitidas = int(getattr(res, "consultas_omitidas", 0) or 0)
    return omitidas > 0 and not (getattr(res, "consultas", None) or [])


def registrar_endpoint_sync(db, run_id, dominio, res=None, error: str = "") -> list[dict]:
    """
    Una fila por board de endpoint. `res` es un ResultadoEndpoint; sin él (el fetch
    reventó antes de devolver nada) la fuente queda con items=0 y el error a la vista:
    desde el punto de vista del conteo, una fuente que explotó trajo cero.
    """
    try:
        if res is not None:
            # Un board SALTEADO (limite de su API, TTL de consulta) no leyo nada: no es
            # una caida a cero y no escribe traza. Los atributos se piden con getattr
            # porque ResultadoEndpoint cambia entre ramas (consultas, parcial,
            # consultas_omitidas) y este helper no puede depender de que existan.
            if _endpoint_salteado(res):
                return []
            fila = _fila(run_id, NODO_ENDPOINT, dominio,
                         items=int(getattr(res, "items_crudos", 0) or 0),
                         aceptados=len(getattr(res, "jobs", None) or []),
                         error=getattr(res, "error", "") or error,
                         ms=float(getattr(res, "ms", 0.0) or 0.0))
        else:
            fila = _fila(run_id, NODO_ENDPOINT, dominio,
                         items=0, aceptados=0, error=error or "sin resultado", ms=0.0)
        db.record_node_traces_batch([fila])
        return evaluar_corrida_sync(db, run_id, [dominio])
    except Exception as exc:
        logger.warning("[FUENTES] traza de endpoint %s no escrita: %s", dominio, exc)
        return []


def registrar_ats_sync(db, run_id, resultados) -> list[dict]:
    """
    Una fila por empresa ATS, en UNA transacción: 40 fuentes a 107 ms de fsync cada una
    (medido en la Fase 8) serían 4 s agregados a la corrida que se está midiendo.

    El identificador de la fuente es `plataforma:token`. No es el pseudo-dominio de la
    plataforma —ese es el `source_domain` de los avisos— porque acá la unidad que se
    rompe es la empresa: que Stripe cierre su tablero no tiene que esconderse detrás de
    los 800 avisos que Greenhouse sigue trayendo de otras.
    """
    try:
        filas = [
            _fila(run_id, NODO_ATS, r.etiqueta, items=int(getattr(r, "items_crudos", 0) or 0),
                  aceptados=len(getattr(r, "jobs", None) or []),
                  error=getattr(r, "error", ""), ms=float(getattr(r, "ms", 0.0) or 0.0))
            for r in resultados if not getattr(r, "saltado", False)
        ]
        if not filas:
            return []
        db.record_node_traces_batch(filas)
        return evaluar_corrida_sync(db, run_id, [f["domain"] for f in filas])
    except Exception as exc:
        logger.warning("[FUENTES] trazas ATS no escritas: %s", exc)
        return []


# =============================================================================
# Wrappers async — lo único que el scraper llama
# =============================================================================

async def registrar_endpoint(db, run_id, dominio, res=None, error: str = "") -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(registrar_endpoint_sync, db, run_id, dominio, res, error))


async def registrar_ats(db, run_id, resultados) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(registrar_ats_sync, db, run_id, resultados))


async def evaluar_css(db, run_id, trazas: list[dict]) -> list[dict]:
    """Después de escribir el lote de ramas CSS: evalúa los boards que participaron."""
    loop = asyncio.get_running_loop()
    dominios = {t["domain"] for t in trazas if t.get("domain")}
    return await loop.run_in_executor(
        None, functools.partial(evaluar_corrida_sync, db, run_id, dominios))
