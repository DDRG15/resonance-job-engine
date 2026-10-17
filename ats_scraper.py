"""
ats_scraper.py — traer los tableros de empresa de Greenhouse, Lever, Ashby, Recruitee,
Workable, BambooHR y Personio.

Se apoya entero en `endpoint_scraper`: acá no hay HTTP ni
parseo, solo el armado de URLs por plataforma, la inyección del nombre de empresa que
esos payloads no traen, y la concurrencia.

La concurrencia es por plataforma y no global, porque el límite tampoco es global:
`api.lever.co` declara `Crawl-delay: 1` en su robots.txt y `boards-api.greenhouse.io`
no declara ninguno. Un pool único obligaría a usar el techo más bajo para todos, y
serializar Greenhouse por un límite que Lever declaró es pagar el peaje de otro.

Nada de esto toca Playwright. El semáforo de `SCRAPER_CONCURRENCY` existe para acotar
pestañas de Chromium abiertas; un GET no abre ninguna.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import Callable, Optional

import ats_registry
import endpoint_scraper
import metrics
from ats_registry import FuenteATS
from models import JobResult

logger = logging.getLogger(__name__)


class ResultadoATS:
    """Lo que trajo una fuente, con lo suficiente para diagnosticarla sin abrir el log."""

    __slots__ = ("fuente", "jobs", "items_crudos", "descartados", "error", "ms")

    def __init__(self, fuente: FuenteATS):
        self.fuente = fuente
        self.jobs: list[JobResult] = []
        self.items_crudos = 0
        self.descartados = 0
        self.error = ""
        self.ms = 0.0

    @property
    def etiqueta(self) -> str:
        return f"{self.fuente.plataforma}:{self.fuente.token}"


def traer_fuente(
    fuente: FuenteATS,
    *,
    titles: Optional[list[str]] = None,
    es_relevante: Optional[Callable[[str], bool]] = None,
    log: Callable[[str], None] = lambda _m: None,
) -> ResultadoATS:
    """
    Una fuente ATS. Sincrónica a propósito: la llama un executor.

    `defaults={"company": ...}` cubre a Lever y Ashby, que no devuelven el nombre de la
    empresa — el tablero ES de una empresa, así que para el payload es contexto. Sin
    esto, los 1853 avisos de Lever entrarían al registry como "(empresa no listada)",
    que es justamente la basura que la Fase 1 vino a dejar de producir.
    """
    plat = ats_registry.plataforma_de(fuente)
    res = ResultadoATS(fuente)

    r = endpoint_scraper.scrape_endpoint(
        ats_registry.dominio_de(fuente.plataforma),
        endpoint_template=plat.url_template.replace("{token}", fuente.token),
        response_format=plat.response_format,
        field_map=plat.field_map,
        items_path=plat.items_path,
        titles=titles,
        crawl_delay_s=plat.crawl_delay_s,
        salario_en_descripcion=plat.salario_en_descripcion,
        defaults={"company": fuente.empresa},
        url_aviso_template=plat.url_aviso_template.replace("{token}", fuente.token),
        xml_item=plat.xml_item,
        seguir_redirecciones=plat.seguir_redirecciones,
        es_relevante=es_relevante,
        log=lambda _m: None,      # el log util es el agregado, no 40 lineas sueltas
    )

    res.jobs = r.jobs
    res.items_crudos = r.items_crudos
    res.descartados = r.descartados_irrelevantes + r.descartados_invalidos
    res.error = r.error
    res.ms = r.ms

    if r.error:
        metrics.emit("ats_fuente_error_total", 1, plataforma=fuente.plataforma,
                     token=fuente.token, motivo=motivo_de_error(r.error))
    if r.descartados_invalidos:
        metrics.emit("ats_items_invalidos_total", r.descartados_invalidos,
                     plataforma=fuente.plataforma, token=fuente.token)
    return res


def motivo_de_error(error: str) -> str:
    """
    El error de una fuente ATS en una de pocas categorias, para la etiqueta `motivo`.

    Cada plataforma dice «esta empresa no existe» de otra
    forma —Workable y Recruitee con 404, BambooHR con 302, Personio con 307—, medido el
    2026-10-04. Una alerta por `ats_fuente_error_total{motivo="inexistente"}` agrupa las
    cuatro; una por texto libre tendria que conocer los cuatro mensajes.
    """
    e = error or ""
    if "esquema_cambiado" in e:
        return "esquema_cambiado"
    if e.startswith("parseo"):
        return "parseo"
    if "EndpointNoPublico" in e:
        return "no_publico"
    if re.search(r"HTTP (404|410|30\d)\b", e):
        return "inexistente"
    if re.search(r"HTTP (429|5\d\d)\b", e):
        return "limitado_o_caido"
    return "otro"


async def traer_todo(
    fuentes: Optional[list[FuenteATS]] = None,
    *,
    titles: Optional[list[str]] = None,
    es_relevante: Optional[Callable[[str], bool]] = None,
    log: Callable[[str], None] = lambda _m: None,
    al_terminar: Optional[Callable[[ResultadoATS], None]] = None,
) -> list[ResultadoATS]:
    """
    Todas las fuentes activas, concurrentes dentro del límite de cada plataforma.

    `al_terminar` se llama una vez por fuente en cuanto esa fuente termina, también si
    falla (recibe el `ResultadoATS` con `error`). Es lo que permite a quien llama contar
    progreso por empresa sin partir la lista en lotes, que serializaba los lotes y
    repetía el log agregado. Corre en el hilo del event loop; si el callback explota se
    registra y no afecta a la fuente ni a las demás. El orden y el valor de retorno no
    cambian: sigue siendo un resultado por fuente, en el orden de `fuentes`.

    Una fuente que explota no puede tumbar a las otras treinta y nueve. Ese es el
    motivo por el que el fan-out del scraper envuelve cada rama.
    """
    fuentes = fuentes if fuentes is not None else ats_registry.activas()
    if not fuentes:
        return []

    loop = asyncio.get_running_loop()
    semaforos = {
        nombre: asyncio.Semaphore(p.concurrencia)
        for nombre, p in ats_registry.PLATAFORMAS.items()
    }

    def _avisar(res: ResultadoATS) -> None:
        if al_terminar is None:
            return
        try:
            al_terminar(res)
        except Exception as exc:  # un contador de progreso no puede tumbar una fuente
            logger.warning("[ATS] al_terminar fallo para %s: %s", res.etiqueta, exc)

    async def _una(f: FuenteATS) -> ResultadoATS:
        try:
            async with semaforos[f.plataforma]:
                res = await loop.run_in_executor(
                    None,
                    functools.partial(traer_fuente, f, titles=titles,
                                      es_relevante=es_relevante, log=log),
                )
        except Exception as r:
            res = ResultadoATS(f)
            res.error = f"{type(r).__name__}: {str(r)[:120]}"
            log(f"[ATS] {res.etiqueta} — {res.error}")
            metrics.emit("ats_fuente_error_total", 1, plataforma=f.plataforma,
                         token=f.token, motivo="excepcion")
        _avisar(res)
        return res

    # `_una` ya no deja escapar excepciones: una fuente que explota no puede tumbar a
    # las otras treinta y nueve y queda como resultado con
    # `error`, igual que antes.
    salida: list[ResultadoATS] = list(await asyncio.gather(*[_una(f) for f in fuentes]))

    por_plataforma: dict[str, tuple[int, int, int]] = {}
    for r in salida:
        n, crudo, err = por_plataforma.get(r.fuente.plataforma, (0, 0, 0))
        por_plataforma[r.fuente.plataforma] = (
            n + len(r.jobs), crudo + r.items_crudos, err + (1 if r.error else 0))
    for plat, (n, crudo, err) in sorted(por_plataforma.items()):
        fallidas = f", {err} fuente(s) con error" if err else ""
        log(f"[ATS] {plat} — {n} avisos de {crudo} items{fallidas}")

    return salida
