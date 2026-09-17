"""
applications.py — el ciclo de vida de una postulación.

Fase 7 del . Es la única fase que agrega un dominio nuevo.

GEMA terminaba en «encontrado y empujado a Notion» y no sabía nada de lo que pasaba
después. Sin eso no se puede responder *de los Tier 1 que apliqué, cuántos convirtieron*
— y esa pregunta no es curiosidad, es el único feedback real que tiene el matcher. Hoy
el scoring se calibra a ojo y el corpus de evaluación sigue con dos casos sintéticos.

Este módulo es solo el dominio: los estados, qué transición es legal y por qué. La
persistencia vive en `database.py` y los endpoints en `api.py`. Separarlo permite probar
las reglas sin una base de datos de por medio, que es lo que hace que estas reglas se
puedan cambiar sin miedo cuando el uso real de el usuario las contradiga.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Estados
#
# Nombrados desde el lado de quien postula, no del sistema: `sin_respuesta` y no
# `ghosted_by_recruiter`. Son los estados que el usuario va a tipear, no los que un ATS
# reporta.
#
# El riesgo R5 del plan advierte que esto se puede diseñar sobre un flujo que nadie
# usa. La mitigación es que la lista sea corta y que agregar un estado cueste una
# línea — no que sea exhaustiva desde el día uno.
# ─────────────────────────────────────────────────────────────────────────────

BORRADOR   = "borrador"      # marcado como interesante, todavía no enviado
POSTULADO  = "postulado"     # enviado, esperando respuesta
SCREENING  = "screening"     # llamada con RR.HH. o filtro inicial
TECNICA    = "tecnica"       # entrevista técnica, challenge, take-home
FINAL      = "final"         # última ronda, panel, con el hiring manager
OFERTA     = "oferta"        # hay oferta sobre la mesa

RECHAZADO     = "rechazado"       # dijeron que no
SIN_RESPUESTA = "sin_respuesta"   # no contestaron nunca
RETIRADO      = "retirado"        # te bajaste vos
ACEPTADO      = "aceptado"        # firmaste

#: Los estados por los que una postulación avanza, en orden.
CADENA: tuple[str, ...] = (BORRADOR, POSTULADO, SCREENING, TECNICA, FINAL, OFERTA)

#: Estados de los que no se sale. Una postulación acá está cerrada.
TERMINALES: frozenset[str] = frozenset(
    {RECHAZADO, SIN_RESPUESTA, RETIRADO, ACEPTADO})

ESTADOS: frozenset[str] = frozenset(CADENA) | TERMINALES

#: Desde qué estado se puede considerar que hubo conversión real. Un `screening`
#: ya es señal de que el aviso valía la pena; un `postulado` sin respuesta no dice
#: nada sobre el aviso, solo sobre el volumen de candidatos.
CONVIRTIO: frozenset[str] = frozenset(
    {SCREENING, TECNICA, FINAL, OFERTA, ACEPTADO})


class TransicionInvalida(ValueError):
    """Se intentó mover una postulación a un estado al que no se puede llegar."""


def transiciones_validas(desde: str) -> frozenset[str]:
    """
    A qué estados se puede pasar desde `desde`.

    Tres reglas, y ninguna es arbitraria:

    **Se avanza de a un paso.** De `postulado` se va a `screening`, no a `oferta`.
    Saltar etapas hace que la métrica de conversión mienta: si un aviso figura en
    `oferta` sin haber pasado por `tecnica`, no se puede saber en qué etapa se cae
    el embudo, que es justamente lo que el embudo existe para mostrar.

    **Se puede cerrar desde cualquier lado.** Un rechazo llega cuando llega, y
    obligar a pasar por los estados intermedios para registrarlo haría que nadie lo
    registre.

    **De un terminal no se sale.** Una postulación rechazada que vuelve a `screening`
    es una postulación nueva, no la misma con otro estado — si no, el histórico deja
    de ser un histórico.
    """
    if desde in TERMINALES:
        return frozenset()
    if desde not in CADENA:
        raise TransicionInvalida(f"Estado desconocido: {desde!r}")
    i = CADENA.index(desde)
    siguiente = {CADENA[i + 1]} if i + 1 < len(CADENA) else set()
    return frozenset(siguiente) | TERMINALES


def validar_transicion(desde: str, hasta: str) -> None:
    """Lanza `TransicionInvalida` si el movimiento no es legal. No devuelve nada:
    o pasa, o explica por qué no."""
    if hasta not in ESTADOS:
        raise TransicionInvalida(f"Estado desconocido: {hasta!r}")
    if desde == hasta:
        raise TransicionInvalida(f"Ya está en {hasta!r}")
    permitidos = transiciones_validas(desde)
    if hasta not in permitidos:
        if desde in TERMINALES:
            raise TransicionInvalida(
                f"{desde!r} es terminal — una postulación cerrada que vuelve a "
                f"abrirse es una postulación nueva, no la misma con otro estado")
        raise TransicionInvalida(
            f"No se puede pasar de {desde!r} a {hasta!r}. "
            f"Desde {desde!r} se puede ir a: {', '.join(sorted(permitidos))}")


def es_terminal(estado: str) -> bool:
    return estado in TERMINALES


def etapa_de(estado: str) -> int:
    """Qué tan lejos llegó, para ordenar un embudo. Los terminales devuelven la
    etapa en la que se cerraron no — devuelven -1, porque el dato de dónde se
    cerraron vive en el histórico y no en el estado actual."""
    return CADENA.index(estado) if estado in CADENA else -1


@dataclass(frozen=True)
class Conversion:
    """Cuántas postulaciones de un grupo llegaron a una señal real."""
    grupo: str
    postulaciones: int
    convirtieron: int

    @property
    def tasa(self) -> float:
        return self.convirtieron / self.postulaciones if self.postulaciones else 0.0
