"""
ats_scraper.py — traer los tableros de empresa de Greenhouse, Lever y Ashby.

Fase 2 del . Se apoya entero en `endpoint_scraper`: acá no hay HTTP ni
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
from typing import Callable, Optional

import ats_registry
import endpoint_scraper
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
        es_relevante=es_relevante,
        log=lambda _m: None,      # el log util es el agregado, no 40 lineas sueltas
    )

    res.jobs = r.jobs
    res.items_crudos = r.items_crudos
    res.descartados = r.descartados_irrelevantes + r.descartados_invalidos
    res.error = r.error
    res.ms = r.ms
    return res


async def traer_todo(
    fuentes: Optional[list[FuenteATS]] = None,
    *,
    titles: Optional[list[str]] = None,
    es_relevante: Optional[Callable[[str], bool]] = None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[ResultadoATS]:
    """
    Todas las fuentes activas, concurrentes dentro del límite de cada plataforma.

    `return_exceptions=True` no está de adorno: una fuente que explota no puede tumbar
    a las otras treinta y nueve. Ese fue el riesgo R1 del plan anterior y el motivo por
    el que el fan-out del scraper envuelve cada rama.
    """
    fuentes = fuentes if fuentes is not None else ats_registry.activas()
    if not fuentes:
        return []

    loop = asyncio.get_running_loop()
    semaforos = {
        nombre: asyncio.Semaphore(p.concurrencia)
        for nombre, p in ats_registry.PLATAFORMAS.items()
    }

    async def _una(f: FuenteATS) -> ResultadoATS:
        async with semaforos[f.plataforma]:
            return await loop.run_in_executor(
                None,
                functools.partial(traer_fuente, f, titles=titles,
                                  es_relevante=es_relevante, log=log),
            )

    crudos = await asyncio.gather(*[_una(f) for f in fuentes],
                                  return_exceptions=True)

    salida: list[ResultadoATS] = []
    for f, r in zip(fuentes, crudos):
        if isinstance(r, Exception):
            fallido = ResultadoATS(f)
            fallido.error = f"{type(r).__name__}: {str(r)[:120]}"
            log(f"[ATS] {fallido.etiqueta} — {fallido.error}")
            salida.append(fallido)
        else:
            salida.append(r)

    por_plataforma: dict[str, tuple[int, int, int]] = {}
    for r in salida:
        n, crudo, err = por_plataforma.get(r.fuente.plataforma, (0, 0, 0))
        por_plataforma[r.fuente.plataforma] = (
            n + len(r.jobs), crudo + r.items_crudos, err + (1 if r.error else 0))
    for plat, (n, crudo, err) in sorted(por_plataforma.items()):
        fallidas = f", {err} fuente(s) con error" if err else ""
        log(f"[ATS] {plat} — {n} avisos de {crudo} items{fallidas}")

    return salida
