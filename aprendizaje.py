"""
aprendizaje.py — lo que las postulaciones del usuario le enseñan al ranking.

, Fase 10. Diseño de Bert, reimplementado. Construido e INACTIVO: hasta
que haya `config.LEARN_MIN_RESULTADOS` postulaciones con resultado no mueve ningún
puntaje, no agrega ninguna razón y no cambia la versión del score. Lo dice en el log y
en la métrica `aprendizaje_resultados`: "aprendizaje inactivo: N de 15".

Este módulo es solo el dominio, sin base y sin matcher: qué resultado dejó cada
postulación, qué rasgos de un aviso se aprenden, cuántos puntos vale cada uno y cuál
es la identidad del score cuando el aprendizaje está activo. Lo aplica
`matcher.aplicar_aprendizaje` DESPUÉS del puntaje base: `score_job` y sus dimensiones
no cambian, y `tests/test_score_version_congelada.py` sigue midiendo lo mismo.

Qué se aprende y qué no
-----------------------
**Empresa.** Una empresa que te entrevistó vuelve a publicar perfiles parecidos, y una
que te rechazó dos veces te va a rechazar la tercera. Es el rasgo más específico que
`applications` guarda al aplicar (se copia del registry en `crear_postulacion`).

**Familia de título.** El título sin seniority, modalidad ni región ("Senior Backend
Engineer (Remote)" → "backend engineer"). Dos descartes en "data engineer" dicen que el
perfil no calza con esa familia, en cualquier empresa. Se compara el conjunto exacto de
palabras, no palabra por palabra: aprender "engineer" como palabra suelta movería casi
todo el catálogo por dos rechazos.

**La fuente no se aprende.** Con 15 postulaciones, 6 salen de un mismo board. Dos
rechazos de dos empresas distintas en ese board bajarían cientos de avisos que no
tienen nada que ver con esos rechazos: sin una tasa base por board, contar resultados
por fuente mide volumen, no calce. La conversión por board ya la muestra
`GET /api/applications/stats?dimension=source_domain`, que la lee con denominador.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

import applications as app_dom
import config
from correlators import _norm_empresa   # misma identidad de empresa que la Fase 11

logger = logging.getLogger(__name__)

ENTREVISTA = "entrevista"
OFERTA = "oferta"
DESCARTE = "descarte"

RASGO_EMPRESA = "empresa"
RASGO_TITULO = "titulo"

_ESTADOS_OFERTA = frozenset({app_dom.OFERTA, app_dom.ACEPTADO})
_ESTADOS_ENTREVISTA = frozenset({app_dom.SCREENING, app_dom.TECNICA, app_dom.FINAL})


def resultado_de(estados: Iterable[str]) -> Optional[str]:
    """
    El resultado de una postulación, a partir de TODOS los estados por los que pasó.

    Se lee el histórico y no el `status` actual por la misma razón que
    `conversion_por`: un rechazo después de una técnica figura hoy `rechazado`, pero el
    aviso valía la pena. Aprender de él como descarte enseñaría lo contrario de lo que
    pasó.

    Cada postulación aporta UN resultado, el más lejano: una oferta no suma además como
    entrevista. `sin_respuesta` y `retirado` sin haber pasado de `postulado` no son
    resultado: no dicen nada del aviso, solo del volumen de candidatos o del usuario.
    """
    alcanzados = set(estados)
    if alcanzados & _ESTADOS_OFERTA:
        return OFERTA
    if alcanzados & _ESTADOS_ENTREVISTA:
        return ENTREVISTA
    if app_dom.RECHAZADO in alcanzados:
        return DESCARTE
    return None


# Palabras que no distinguen una familia de puesto: seniority, modalidad, región y
# conectores en inglés y español. Lista propia y no la de `matcher._STOPWORDS`: si
# alguien afina el pre-filtro de relevancia, las familias aprendidas no pueden cambiar
# de clave en silencio.
_RUIDO_TITULO = frozenset({
    "senior", "sr", "junior", "jr", "mid", "lead", "staff", "principal", "head",
    "i", "ii", "iii", "iv", "v", "level", "entry", "experienced",
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "at", "with", "on",
    "remote", "remoto", "hybrid", "hibrido", "onsite", "site", "full", "time",
    "part", "contract", "contractor", "permanent", "freelance", "work", "home",
    "anywhere", "worldwide", "latam", "us", "usa", "emea", "apac", "europe",
    "americas", "global", "de", "del", "la", "el", "en", "y", "para", "con",
})


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto)
                   if not unicodedata.combining(c))


def familia_de_titulo(titulo: Optional[str]) -> str:
    """
    "Senior Backend Engineer (Remote)" y "Backend Engineer II" → "backend engineer".

    Conjunto ordenado de tokens significativos: el orden de las palabras no cambia la
    familia. Devuelve "" si no queda nada: un título sin señal no se aprende.
    """
    texto = _sin_tildes(titulo or "").lower()
    tokens = re.findall(r"[a-z0-9+#]+", texto)
    significativos = {t for t in tokens
                      if t not in _RUIDO_TITULO and len(t) > 1 and not t.isdigit()}
    return " ".join(sorted(significativos))


def empresa_de(empresa: Optional[str]) -> str:
    """Clave de empresa. "Canonical Ltd" y "Canonical" son la misma; el placeholder
    "(empresa no listada)" es "" y no se aprende: dos avisos sin empresa no son de la
    misma empresa, y aprender de él castigaría a todo aviso sin empresa."""
    return _norm_empresa(empresa)


def _rasgos(titulo: Optional[str], empresa: Optional[str]) -> tuple[tuple[str, str], ...]:
    return ((RASGO_EMPRESA, empresa_de(empresa)),
            (RASGO_TITULO, familia_de_titulo(titulo)))


@dataclass(frozen=True)
class Conteo:
    entrevistas: int = 0
    ofertas: int = 0
    descartes: int = 0

    def sumar(self, resultado: str) -> "Conteo":
        return Conteo(
            self.entrevistas + (resultado == ENTREVISTA),
            self.ofertas + (resultado == OFERTA),
            self.descartes + (resultado == DESCARTE),
        )

    @property
    def descartes_confirmados(self) -> bool:
        return self.descartes >= int(config.LEARN_DESCARTES_PARA_CONFIRMAR)

    @property
    def puntos(self) -> int:
        """Lo que este rasgo aporta, antes del tope. Un descarte solo vale cero:
        necesita un segundo del mismo rasgo para restar. Una entrevista o una oferta
        suman desde la primera."""
        a_favor = (self.entrevistas * int(config.LEARN_PUNTOS_ENTREVISTA)
                   + self.ofertas * int(config.LEARN_PUNTOS_OFERTA))
        en_contra = (self.descartes * int(config.LEARN_PUNTOS_DESCARTE)
                     if self.descartes_confirmados else 0)
        return a_favor - en_contra

    def detalle(self) -> str:
        partes = []
        if self.ofertas:
            partes.append(f"{self.ofertas} oferta{'s' if self.ofertas > 1 else ''}")
        if self.entrevistas:
            partes.append(f"{self.entrevistas} entrevista"
                          f"{'s' if self.entrevistas > 1 else ''}")
        if self.descartes:
            partes.append(f"{self.descartes} descarte{'s' if self.descartes > 1 else ''}"
                          + ("" if self.descartes_confirmados else " sin confirmar"))
        return ", ".join(partes)


@dataclass(frozen=True)
class Ajuste:
    """Lo que el aprendizaje le hace a UN aviso. `delta` ya tiene el tope aplicado;
    `bruto` es la suma antes del tope, para que la razón pueda mostrar los dos."""
    delta: int = 0
    bruto: int = 0
    a_favor: tuple[str, ...] = ()
    en_contra: tuple[str, ...] = ()

    @property
    def vacio(self) -> bool:
        return self.delta == 0 and not self.a_favor and not self.en_contra


_ETIQUETA = {RASGO_EMPRESA: "empresa", RASGO_TITULO: "familia de título"}


@dataclass(frozen=True)
class ModeloAprendizaje:
    """
    Lo aprendido de un historial de postulaciones. Inmutable: se construye una vez por
    corrida (`construir` o `cargar`) y se consulta por aviso (`ajuste`).

    `error` no vacío significa que el historial no se pudo leer. El modelo queda
    inactivo —puntaje base, versión base— y el mensaje lo dice en vez de reportar
    "0 de 15", que sería falso.
    """
    resultados: int = 0
    por_rasgo: dict = field(default_factory=dict)   # (rasgo, clave) -> Conteo
    error: str = ""

    @property
    def habilitado(self) -> bool:
        return bool(config.LEARN_HABILITADO)

    @property
    def umbral(self) -> int:
        return int(config.LEARN_MIN_RESULTADOS)

    @property
    def activo(self) -> bool:
        return self.habilitado and not self.error and self.resultados >= self.umbral

    def mensaje(self) -> str:
        if self.error:
            return f"aprendizaje inactivo: no se pudo leer el historial ({self.error})"
        if not self.habilitado:
            return (f"aprendizaje apagado (GEMA_APRENDIZAJE=0): {self.resultados} de "
                    f"{self.umbral} postulaciones con resultado")
        if not self.activo:
            return (f"aprendizaje inactivo: {self.resultados} de {self.umbral} "
                    f"postulaciones con resultado")
        return (f"aprendizaje activo: {self.resultados} postulaciones con resultado, "
                f"{len(self.reglas_efectivas())} reglas que mueven puntos")

    def reglas_efectivas(self) -> list[tuple[str, str, int]]:
        """(rasgo, clave, puntos) de los rasgos que mueven puntos, ordenados. Un
        descarte sin confirmar no está acá: no mueve nada."""
        return sorted((r, c, n.puntos) for (r, c), n in self.por_rasgo.items()
                      if n.puntos != 0)

    def huella(self) -> str:
        """
        8 hex que identifican TODO lo que decide el puntaje: las reglas que mueven
        puntos y el tope. Mismas reglas efectivas, mismos puntajes, misma huella.

        Se calcula sobre las reglas efectivas y no sobre los conteos crudos: un primer
        rechazo de una empresa nueva no mueve ningún puntaje, y cambiar la versión por
        eso mandaría a re-puntuar el catálogo entero para obtener los mismos números.
        """
        carga = {"reglas": self.reglas_efectivas(), "tope": int(config.LEARN_TOPE)}
        crudo = json.dumps(carga, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:8]

    def version(self, base: Optional[str]) -> Optional[str]:
        """
        La identidad del score que produce este modelo.

        Inactivo, o activo sin ninguna regla que mueva puntos: `base` tal cual. El
        aprendizaje no movió nada, así que la escala es la de `SCORE_VERSION` y el
        registry no se entera. Activo con reglas: `base+ap.<huella>`.

        La huella va en la versión, y no un "+ap" fijo, por lo que pasó con 9bc8e4b:
        dos escalas bajo una misma etiqueta. Con "+ap" fijo, un aviso puntuado con las
        reglas del lunes y otro con las del jueves —después de un rechazo confirmado—
        compartirían versión, y el atajo de `hashes_puntuados` nunca re-puntuaría el
        primero.
        """
        if base is None or not self.activo or not self.reglas_efectivas():
            return base
        return f"{base}+ap.{self.huella()}"

    def ajuste(self, titulo: Optional[str], empresa: Optional[str]) -> Ajuste:
        """El ajuste para un aviso. Inactivo: cero y sin razones, ni una línea."""
        if not self.activo:
            return Ajuste()
        tope = int(config.LEARN_TOPE)
        bruto = 0
        a_favor: list[str] = []
        en_contra: list[str] = []
        for rasgo, clave in _rasgos(titulo, empresa):
            if not clave:
                continue
            conteo = self.por_rasgo.get((rasgo, clave))
            if conteo is None:
                continue
            etiqueta = f"{_ETIQUETA[rasgo]} '{clave}'"
            pts = conteo.puntos
            linea = f"Aprendizaje: {etiqueta} {pts:+d} ({conteo.detalle()})"
            if pts > 0:
                a_favor.append(linea)
            elif pts < 0:
                en_contra.append(linea)
            elif conteo.descartes and not (conteo.entrevistas or conteo.ofertas):
                en_contra.append(
                    f"Aprendizaje: {etiqueta} — {conteo.detalle()}, no mueve hasta "
                    f"un segundo descarte")
            else:
                a_favor.append(f"{linea} — se compensan")
            bruto += pts
        delta = max(-tope, min(tope, bruto))
        if delta != bruto:
            nota = f"Aprendizaje: tope ±{tope} aplicado (suma {bruto:+d} → {delta:+d})"
            (a_favor if delta > 0 else en_contra).append(nota)
        return Ajuste(delta, bruto, tuple(a_favor), tuple(en_contra))

    def estado(self, base: Optional[str]) -> dict:
        """Lo que la API y el log muestran. `reglas` lista lo aprendido aunque esté
        inactivo, con los puntos que aportaría: así se ve qué va a pasar el día que se
        active, antes de que pase."""
        reglas = [
            {"rasgo": r, "clave": c, "entrevistas": n.entrevistas, "ofertas": n.ofertas,
             "descartes": n.descartes, "puntos": n.puntos}
            for (r, c), n in sorted(self.por_rasgo.items())
        ]
        return {
            "activo": self.activo,
            "habilitado": self.habilitado,
            "resultados": self.resultados,
            "umbral": self.umbral,
            "faltan": max(0, self.umbral - self.resultados),
            "tope": int(config.LEARN_TOPE),
            "mensaje": self.mensaje(),
            "error": self.error or None,
            "score_version": self.version(base),
            "reglas": reglas,
        }


def construir(postulaciones: Iterable) -> ModeloAprendizaje:
    """
    El modelo a partir de las filas de `GemaDatabase.postulaciones_con_estados()`:
    dicts con `title`, `company` y `estados` (todos los estados por los que pasó).

    Tolerante a filas incompletas: una fila que no es dict o que no tiene estados no
    aporta, no explota. Una corrida no puede caerse porque una postulación quedó a
    medias. Una postulación con resultado pero sin título ni empresa cuenta para el
    umbral —es un resultado real— y no enseña ningún rasgo.
    """
    por_rasgo: dict = {}
    resultados = 0
    for fila in postulaciones or ():
        if not isinstance(fila, dict):
            continue
        resultado = resultado_de(fila.get("estados") or ())
        if resultado is None:
            continue
        resultados += 1
        for rasgo, clave in _rasgos(fila.get("title"), fila.get("company")):
            if clave:
                por_rasgo[(rasgo, clave)] = por_rasgo.get(
                    (rasgo, clave), Conteo()).sumar(resultado)
    return ModeloAprendizaje(resultados=resultados, por_rasgo=por_rasgo)


def cargar(db) -> ModeloAprendizaje:
    """
    El modelo leído de la base. Nunca lanza.

    Si la base no expone el historial o la lectura falla, devuelve un modelo inactivo
    con `error`: el puntaje vuelve al base y la versión a `SCORE_VERSION`. Que el
    aprendizaje no se pueda leer no puede tirar una corrida, y tampoco puede ajustar
    puntajes con un historial a medias.
    """
    if db is None:
        return ModeloAprendizaje()
    leer = getattr(db, "postulaciones_con_estados", None)
    if not callable(leer):
        return ModeloAprendizaje(error="la base no expone postulaciones_con_estados")
    try:
        filas = leer()
        if not isinstance(filas, list):
            # Un doble de test (MagicMock) o una base vieja: no es un historial.
            return ModeloAprendizaje(error="historial ilegible")
        return construir(filas)
    except Exception as exc:   # noqa: BLE001 — fail-safe declarado arriba
        logger.warning("[APRENDIZAJE] no se pudo leer el historial: %s", exc)
        return ModeloAprendizaje(error=type(exc).__name__)
