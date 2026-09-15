"""
endpoint_scraper.py — leer un board sin abrir un navegador.

Fase 1 del . Implementa `AccessVector.ENDPOINT`.

Por qué existe: `node_traces` del 2026-09-03 midió el nodo `scrape` en 35.726 ms contra
menos de 250 ms de todos los demás nodos sumados. Ese tiempo es Chromium abriendo pestañas
para leer HTML pensado para humanos, y es también el componente que falla — cuatro boards
retirados por HTTP 403 o muro de login son problemas que solo existen porque hay un
navegador pidiendo una página. El inventario del 2026-09-09 midió la alternativa:

    remoteok.com /api        99 items, salario entero en 12%, ubicación en 72%
    weworkremotely.com RSS   90 items en 484 ms, expires_at en 90 de 90
    boards-api.greenhouse.io 230 items en 137 ms

Contrato: este módulo devuelve `list[JobResult]`, la misma forma que produce el scraper
CSS. Aguas abajo nadie se entera de por dónde vino el aviso — `_ingest_external_jobs()` de
scraper.py lo deduplica, lo encola y lo puntúa igual que a cualquier otro.

Lo que NO hace, por decisión: no pagina, no ejecuta JavaScript, no reintenta contra un 403.
Un 403 significa que el endpoint no es público, y la respuesta correcta es marcar el board
y pasar a camoufox, no insistir.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

import config
from models import JobResult

logger = logging.getLogger(__name__)

UA = getattr(config, "ENDPOINT_USER_AGENT",
             "GEMA/3.0 (+job board aggregator; respeta robots.txt y crawl-delay)")
TIMEOUT = getattr(config, "ENDPOINT_TIMEOUT_S", 20)
MAX_REINTENTOS = getattr(config, "ENDPOINT_MAX_RETRIES", 3)
BACKOFF_BASE_S = getattr(config, "ENDPOINT_BACKOFF_BASE_S", 2.0)
BACKOFF_TECHO_S = getattr(config, "ENDPOINT_BACKOFF_CAP_S", 30.0)


class EndpointNoPublico(Exception):
    """403/401: el endpoint exige credenciales o bloquea. No se reintenta —
    reintentar un 403 es gastar cuota para confirmar lo que ya dijo."""


class EndpointAgotado(Exception):
    """Se agotaron los reintentos contra 429/5xx."""


# ─────────────────────────────────────────────────────────────────────────────
# Fetch
# ─────────────────────────────────────────────────────────────────────────────

def _dormir(segundos: float) -> None:
    """Aislado para que los tests lo puedan parchear sin tocar time.sleep global."""
    time.sleep(segundos)


def fetch(url: str, *, crawl_delay_s: float = 0.0,
          sesion: Optional[requests.Session] = None) -> bytes:
    """
    Un GET con backoff exponencial en 429 y 5xx. Devuelve el cuerpo crudo.

    `Retry-After` gana sobre el backoff calculado: si el server dice cuánto esperar,
    esperar otra cosa es elegir pelearse con quien te está dejando entrar.

    `crawl_delay_s` viene del robots.txt del board — api.lever.co declara 1 segundo.
    Es un límite, no una sugerencia.
    """
    ses = sesion or requests.Session()
    ses.headers.setdefault("User-Agent", UA)

    if crawl_delay_s > 0:
        _dormir(crawl_delay_s)

    ultimo = ""
    for intento in range(1, MAX_REINTENTOS + 1):
        r = ses.get(url, timeout=TIMEOUT)

        if r.status_code == 200:
            return r.content

        if r.status_code in (401, 403):
            raise EndpointNoPublico(f"HTTP {r.status_code} en {url}")

        if r.status_code == 429 or r.status_code >= 500:
            ultimo = f"HTTP {r.status_code}"
            if intento == MAX_REINTENTOS:
                break
            espera = min(BACKOFF_TECHO_S, BACKOFF_BASE_S * (2 ** (intento - 1)))
            cabecera = r.headers.get("Retry-After")
            if cabecera:
                try:
                    espera = min(BACKOFF_TECHO_S, float(cabecera))
                except ValueError:
                    pass
            logger.info("[ENDPOINT] %s en %s — esperando %.1fs (intento %d/%d)",
                        ultimo, url, espera, intento, MAX_REINTENTOS)
            _dormir(espera)
            continue

        # 404 y demás: no hay nada que reintentar.
        raise EndpointAgotado(f"HTTP {r.status_code} en {url}")

    raise EndpointAgotado(f"{ultimo} tras {MAX_REINTENTOS} intentos en {url}")


# ─────────────────────────────────────────────────────────────────────────────
# Parseo — JSON y RSS a una lista de dicts planos
#
# El `field_map` vive en la estrategia del board, no acá. Cuando un endpoint
# renombra un campo, se arregla editando un JSON y no recompilando nada. El límite
# está declarado en el riesgo R2 del plan: mapeo plano y `path` con puntos, nada
# más. Cualquier transformación real va en código — un formato declarativo que
# crece sin techo se convierte en un lenguaje que nadie quiere mantener.
# ─────────────────────────────────────────────────────────────────────────────

def _por_path(obj: Any, path: str) -> Any:
    """`"location.name"` sobre un dict anidado. Devuelve None si el camino se corta."""
    actual = obj
    for parte in path.split("."):
        if isinstance(actual, dict):
            actual = actual.get(parte)
        else:
            return None
        if actual is None:
            return None
    return actual


def items_de_json(cuerpo: bytes, items_path: str = "") -> list[dict]:
    """
    La lista de avisos dentro de una respuesta JSON.

    Tres formas conviven en los endpoints reales y las tres se aceptan: lista suelta
    (remoteok), `{"jobs": [...]}` (greenhouse) y `{"results": [...]}`.
    """
    d = json.loads(cuerpo.decode("utf-8", errors="replace"))

    if items_path:
        lista = _por_path(d, items_path)
    elif isinstance(d, list):
        lista = d
    else:
        lista = None
        for clave in ("jobs", "results", "data", "postings", "items"):
            if isinstance(d, dict) and isinstance(d.get(clave), list):
                lista = d[clave]
                break

    if not isinstance(lista, list):
        return []

    # remoteok.com pone un objeto legal como primer elemento del array. No es un
    # aviso y no tiene los campos de uno.
    return [x for x in lista if isinstance(x, dict) and "legal" not in x]


def _sin_ns(tag: str) -> str:
    """`{http://...}title` -> `title`. Los feeds mezclan namespaces sin avisar."""
    return tag.rsplit("}", 1)[-1]


def items_de_rss(cuerpo: bytes) -> list[dict]:
    """
    Cada `<item>` (RSS) o `<entry>` (Atom) como un dict plano de tag -> texto.

    Los hijos repetidos se quedan con el primero: un `<category>` múltiple no hace
    que el aviso tenga dos títulos. `<link href="...">` de Atom se lee del atributo.
    """
    raiz = ElementTree.fromstring(cuerpo)
    salida: list[dict] = []
    for nodo in raiz.iter():
        if _sin_ns(nodo.tag) not in ("item", "entry"):
            continue
        fila: dict[str, str] = {}
        for hijo in nodo:
            clave = _sin_ns(hijo.tag)
            if clave in fila:
                continue
            texto = (hijo.text or "").strip()
            if not texto and clave == "link":
                texto = (hijo.attrib.get("href") or "").strip()
            fila[clave] = texto
        salida.append(fila)
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# Mapeo a JobResult
# ─────────────────────────────────────────────────────────────────────────────

_ETIQUETAS = re.compile(r"<[^>]+>")


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, (list, tuple)):
        valor = " ".join(str(v) for v in valor if v)
    s = _ETIQUETAS.sub(" ", str(valor))
    return re.sub(r"\s+", " ", s).strip()


def _salario(fila: dict, fm: dict) -> Optional[str]:
    """
    `salary_raw` es un string porque `parse_salary_usd()` lee strings. Un endpoint
    que entrega enteros —remoteok da `salary_min`/`salary_max`— se compone al
    formato que ese parser ya entiende, con el `$` que exige.

    Componer en vez de agregar una columna nueva mantiene una sola ruta de parseo.
    El día que la Fase 4 acepte libras y euros, este camino se beneficia igual.
    """
    directo = _texto(_por_path(fila, fm["salary_raw"])) if fm.get("salary_raw") else ""
    if directo:
        return directo

    lo = _por_path(fila, fm["salary_min"]) if fm.get("salary_min") else None
    hi = _por_path(fila, fm["salary_max"]) if fm.get("salary_max") else None
    try:
        lo = int(lo) if lo not in (None, "", 0) else None
        hi = int(hi) if hi not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None

    if lo and hi and hi != lo:
        return f"${lo:,} - ${hi:,}"
    if lo:
        return f"${lo:,}"
    if hi:
        return f"${hi:,}"
    return None


def _partir_prefijo_empresa(titulo: str) -> tuple[str, str]:
    """
    `"Reddit: Backend Engineer, IAM"` -> `("Backend Engineer, IAM", "Reddit")`.

    Existe porque el RSS de weworkremotely.com no tiene tag de empresa: la mete en el
    titulo. Medido el 2026-09-14 sobre su feed: **87 de 87 items** con ese formato, asi
    que no es una corazonada sino la forma declarada del feed.

    Esta es una transformacion con NOMBRE, activada por un booleano de la estrategia, y
    no una regla dentro del `field_map`. El limite del riesgo R2 es ese: el mapa mapea,
    el codigo transforma. Si manana hace falta una segunda transformacion, es otra
    funcion con su nombre, no una gramatica.

    Conservadora: parte en el PRIMER `": "` y solo si lo que queda a ambos lados tiene
    contenido. Un titulo sin ese formato vuelve intacto y sin empresa.
    """
    if ": " not in titulo:
        return titulo, ""
    empresa, _, resto = titulo.partition(": ")
    empresa, resto = empresa.strip(), resto.strip()
    if not empresa or not resto:
        return titulo, ""
    return resto, empresa


def a_job_result(fila: dict, fm: dict, dominio: str,
                 url_base: str = "",
                 titulo_trae_empresa: bool = False) -> Optional[JobResult]:
    """
    Una fila cruda a `JobResult`, o `None` si no se puede sostener.

    Se descarta cuando falta título o cuando la URL no es absoluta. La URL es la clave
    de idempotencia de todo el sistema (`compute_hash`), así que una relativa o vacía
    envenena la deduplicación: mejor perder el aviso que corromper el registro.

    `company` degrada a placeholder y no descarta — esa decisión ya se tomó en la
    Fase 31 (2026-07-11), cuando exigir company nuleaba boards enteros.
    """
    titulo = _texto(_por_path(fila, fm.get("title", "title")))
    if not titulo:
        return None

    empresa_del_titulo = ""
    if titulo_trae_empresa:
        titulo, empresa_del_titulo = _partir_prefijo_empresa(titulo)
        if not titulo:
            return None

    url = _texto(_por_path(fila, fm.get("url", "url")))
    if url and url_base and not url.lower().startswith(("http://", "https://")):
        url = urljoin(url_base, url)
    if not url.lower().startswith(("http://", "https://")):
        return None
    if not urlparse(url).netloc:
        return None

    empresa = _texto(_por_path(fila, fm["company"])) if fm.get("company") else ""
    empresa = empresa or empresa_del_titulo

    ubicacion = _texto(_por_path(fila, fm["location"])) if fm.get("location") else ""

    return JobResult(
        title=titulo,
        company=empresa or "(empresa no listada)",
        url=url,
        salary_raw=_salario(fila, fm) or None,
        location_raw=ubicacion or None,
        source_domain=dominio,
        scraped_at=datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación por board
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResultadoEndpoint:
    dominio: str
    jobs: list[JobResult]
    items_crudos: int = 0
    descartados_irrelevantes: int = 0
    descartados_invalidos: int = 0
    error: str = ""
    ms: float = 0.0


def scrape_endpoint(
    dominio: str,
    *,
    endpoint_template: str,
    response_format: str = "json",
    field_map: Optional[dict] = None,
    items_path: str = "",
    titles: Optional[list[str]] = None,
    crawl_delay_s: float = 0.0,
    titulo_trae_empresa: bool = False,
    es_relevante: Optional[Callable[[str], bool]] = None,
    log: Callable[[str], None] = lambda _m: None,
    sesion: Optional[requests.Session] = None,
) -> ResultadoEndpoint:
    """
    Trae los avisos de un board por su endpoint público.

    `endpoint_template` con `{title}` se pide una vez por título; sin `{title}` es un
    feed fijo y se pide una sola vez. Esa distinción no es cosmética: `remoteok.com/api`
    no acepta búsqueda —devuelve los últimos avisos y punto—, así que el filtrado por
    título tiene que pasar del lado de GEMA o el registry se llena de puestos que nadie
    buscó. `es_relevante` es el mismo criterio que usa `bucket_jobs`, para que un aviso
    no se guarde con una regla y se descarte con otra.
    """
    fm = field_map or {}
    t0 = time.perf_counter()
    res = ResultadoEndpoint(dominio=dominio, jobs=[])

    if "{title}" in endpoint_template:
        urls = [endpoint_template.replace("{title}", requests.utils.quote(t))
                for t in (titles or [])]
    else:
        urls = [endpoint_template]

    vistos: set[str] = set()
    for url in urls:
        try:
            cuerpo = fetch(url, crawl_delay_s=crawl_delay_s, sesion=sesion)
        except (EndpointNoPublico, EndpointAgotado) as exc:
            res.error = f"{type(exc).__name__}: {exc}"
            log(f"[ENDPOINT] {dominio} — {res.error}")
            continue
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {str(exc)[:120]}"
            log(f"[ENDPOINT] {dominio} — {res.error}")
            continue

        try:
            filas = (items_de_rss(cuerpo) if response_format == "rss"
                     else items_de_json(cuerpo, items_path))
        except Exception as exc:
            res.error = f"parseo {response_format}: {type(exc).__name__}: {str(exc)[:90]}"
            log(f"[ENDPOINT] {dominio} — {res.error}")
            continue

        res.items_crudos += len(filas)

        for fila in filas:
            job = a_job_result(fila, fm, dominio, url_base=url,
                               titulo_trae_empresa=titulo_trae_empresa)
            if job is None:
                res.descartados_invalidos += 1
                continue
            if es_relevante is not None and not es_relevante(job.title):
                res.descartados_irrelevantes += 1
                continue
            if job.url in vistos:
                continue
            vistos.add(job.url)
            res.jobs.append(job)

    res.ms = (time.perf_counter() - t0) * 1000
    log(f"[ENDPOINT] {dominio} — {len(res.jobs)} avisos de {res.items_crudos} items "
        f"en {res.ms:.0f}ms "
        f"(descartados: {res.descartados_irrelevantes} off-topic, "
        f"{res.descartados_invalidos} sin título o sin URL absoluta)")
    return res
