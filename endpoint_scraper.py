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

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
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
          sesion: Optional[requests.Session] = None,
          seguir_redirecciones: bool = True) -> bytes:
    """
    Un GET con backoff exponencial en 429 y 5xx. Devuelve el cuerpo crudo.

    `Retry-After` gana sobre el backoff calculado: si el server dice cuánto esperar,
    esperar otra cosa es elegir pelearse con quien te está dejando entrar.

    `crawl_delay_s` viene del robots.txt del board — api.lever.co declara 1 segundo.
    Es un límite, no una sugerencia.

    `seguir_redirecciones=False` (Fase 8 del ): BambooHR contesta un token
    inexistente con 302 a www.bamboohr.com y Personio con 307 a personio.com, medido el
    2026-10-04. Siguiendo la redirección, el error llegaba como "parseo json" sobre la
    home comercial de la plataforma, y el que lee la traza no tiene forma de saber que
    la empresa se fue. Sin seguirla, el error dice "HTTP 302 -> https://www.bamboohr.com".
    """
    ses = sesion or requests.Session()
    ses.headers.setdefault("User-Agent", UA)

    if crawl_delay_s > 0:
        _dormir(crawl_delay_s)

    # El kwarg va solo cuando cambia el default: las sesiones falsas de los tests
    # firman `get(url, timeout)` y no tienen por que conocerlo.
    extra = {} if seguir_redirecciones else {"allow_redirects": False}

    ultimo = ""
    for intento in range(1, MAX_REINTENTOS + 1):
        r = ses.get(url, timeout=TIMEOUT, **extra)

        if r.status_code == 200:
            return r.content

        if r.status_code in (401, 403):
            raise EndpointNoPublico(f"HTTP {r.status_code} en {url}")

        if 300 <= r.status_code < 400:
            destino = (getattr(r, "headers", None) or {}).get("Location") or "?"
            raise EndpointAgotado(f"HTTP {r.status_code} -> {destino} en {url}")

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


def _nodo_xml(nodo) -> Any:
    """
    Un elemento XML a texto, lista o dict, segun su forma.

    Hoja -> su texto (CDATA incluido). Hijos que comparten TODOS la misma etiqueta
    (`<jobDescriptions><jobDescription>...`) -> lista, aunque sea uno solo: un aviso de
    Personio con una sola seccion es tan lista como uno con tres. Hijos distintos ->
    dict con la primera aparicion de cada etiqueta. Es la forma minima que deja a
    `_por_path` y a `_descripcion_de` leer el XML de Personio igual que leen un JSON.
    """
    hijos = list(nodo)
    if not hijos:
        return (nodo.text or "").strip()
    etiquetas = [_sin_ns(h.tag) for h in hijos]
    if len(set(etiquetas)) == 1:
        return [_nodo_xml(h) for h in hijos]
    fila: dict[str, Any] = {}
    for h, clave in zip(hijos, etiquetas):
        if clave not in fila:
            fila[clave] = _nodo_xml(h)
    return fila


def items_de_xml(cuerpo: bytes, item_tag: str) -> list[dict]:
    """
    Cada `<item_tag>` de un XML como dict, con los hijos anidados resueltos por
    `_nodo_xml`.

    Existe por Personio (Fase 8 del ): `/xml` devuelve
    `<workzag-jobs><position>...` y la descripcion vive en
    `<jobDescriptions><jobDescription><name/><value>CDATA</value>`. `items_de_rss` solo
    reconoce `<item>` y `<entry>`, y se queda con el texto de cada hijo: la
    descripcion, que es un hijo con hijos, llegaba vacia. Medido el 2026-10-04 contra
    jtl-software-gmbh: 30 posiciones, la descripcion repartida en 3 secciones.
    """
    raiz = ElementTree.fromstring(cuerpo)
    salida: list[dict] = []
    for nodo in raiz.iter():
        if _sin_ns(nodo.tag) != item_tag:
            continue
        fila: dict[str, Any] = {}
        for hijo in nodo:
            clave = _sin_ns(hijo.tag)
            if clave not in fila:
                fila[clave] = _nodo_xml(hijo)
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


def texto_de_html(bruto: Optional[str]) -> str:
    """
    HTML de una descripcion a texto plano.

    **Desescapa hasta que estabiliza, no una vez.** El `content` de Greenhouse viene
    DOBLE escapado: el crudo trae `&amp;mdash;`, asi que una sola vuelta deja `&mdash;`
    literal en el medio del texto. Medido el 2026-09-28 sobre gitlab: con una vuelta el
    extractor lee `$139,200` y con dos lee `$139,200 - $235,200`. Los 84 avisos de
    gitlab con salario pasan de piso suelto a rango completo por esa diferencia.

    El tope de tres vueltas es para que una entrada adversaria —`&amp;amp;amp;...`
    anidado mil veces— no se convierta en trabajo cuadratico dentro del parser.

    **Las etiquetas inline se sacan SIN espacio; las de bloque, con espacio.** Asi es
    como renderiza un navegador. Medido el 2026-09-29: Grafana Labs pega los rangos
    desde Google Sheets y el numero llega partido — `EUR 97<span ...>,000</span>` —.
    Reemplazar toda etiqueta por un espacio producia "EUR 97 ,000" y el extractor leia
    "EUR 97": 3 avisos a revision manual con un salario publicado y legible.
    """
    t = bruto or ""
    for _ in range(3):
        n = html.unescape(t)
        if n == t:
            break
        t = n
    t = _ETIQUETAS_INLINE.sub("", t)
    return re.sub(r"\s+", " ", _ETIQUETAS.sub(" ", t)).strip()


# Inline segun HTML: no cortan el texto al renderizar. Todo lo demas (p, div, li, br,
# h1-h6, td...) si lo corta, y por eso sigue reemplazandose por un espacio.
_ETIQUETAS_INLINE = re.compile(
    r"</?(?:span|strong|b|em|i|u|a|font|small|sup|sub|mark|code)(?:\s[^>]*)?>",
    re.IGNORECASE,
)

# Etiquetas de bloque: en `texto_con_lineas` cada una corta la linea.
_ETIQUETAS_BLOQUE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote|"
    r"pre|dl|dt|dd|hr)(?:\s[^>]*)?/?>",
    re.IGNORECASE,
)


def texto_con_lineas(bruto: Optional[str]) -> str:
    """
    Como `texto_de_html`, pero cada bloque (p, li, h1-h6, br...) queda en su propia linea.

    , Fase 7. `texto_de_html` colapsa todo a una linea, y para el salario
    esta bien. Para los requisitos no: el aviso dice que algo es deseable poniendolo
    debajo de un titulo ("Nice to have") y una linea por item, y sin los saltos el titulo
    y sus items se funden con la oracion de al lado (ver `requisitos._peso`). Mismo
    desescapado iterativo y mismo trato de las etiquetas inline que `texto_de_html`.
    """
    t = bruto or ""
    for _ in range(3):
        n = html.unescape(t)
        if n == t:
            break
        t = n
    # Una etiqueta abierta al final (`<p class="fi`): el campo llego cortado. `_ETIQUETAS`
    # solo saca etiquetas cerradas, y sin esto el "<p cla..." quedaba como texto.
    t = re.sub(r"<[^>]*$", "", t)
    t = _ETIQUETAS_INLINE.sub("", t)
    t = _ETIQUETAS_BLOQUE.sub("\n", t)
    t = _ETIQUETAS.sub(" ", t)
    lineas = (re.sub(r"[^\S\n]+", " ", linea).strip() for linea in t.split("\n"))
    return "\n".join(linea for linea in lineas if linea)


def _descripcion_de(valor: Any) -> str:
    """
    El texto de un campo de descripcion: HTML o texto plano, o la lista de secciones de
    Lever (`lists`: `[{"text": "Requirements", "content": "<li>...</li>"}]`), donde el
    `text` es el titulo de la seccion y tiene que quedar en su propia linea.

    Personio (Fase 8) usa la misma forma con otros nombres: `{"name": "Deine Aufgabe",
    "value": "<ul>...</ul>"}`. Mismo trato: titulo en su linea, despues el cuerpo.
    """
    if valor is None:
        return ""
    if isinstance(valor, dict):
        partes = [texto_con_lineas(str(valor.get("text") or valor.get("name") or "")),
                  texto_con_lineas(str(valor.get("content") or valor.get("value") or ""))]
        return "\n".join(p for p in partes if p)
    if isinstance(valor, (list, tuple)):
        return "\n".join(p for p in (_descripcion_de(v) for v in valor) if p)
    return texto_con_lineas(str(valor))


def _descripcion(fila: dict, fm: dict) -> Optional[str]:
    """
    `fm["description"]` es una ruta o una lista de rutas: Lever reparte el aviso entre la
    intro (`description`), las secciones (`lists`) y el cierre (`additional`), y los
    requisitos viven en las secciones. El tope lo aplica `JobResult`.
    """
    rutas = fm.get("description")
    if not rutas:
        return None
    if isinstance(rutas, str):
        rutas = (rutas,)
    texto = "\n".join(p for p in (_descripcion_de(_por_path(fila, r)) for r in rutas) if p)
    return texto or None


def _salario_de_descripcion(fila: dict, campo: str) -> Optional[str]:
    """
    El rango publicado en el TEXTO de la descripcion.

    Greenhouse no expone compensacion como campo: verificado el 2026-09-28 contra
    gitlab, cloudflare y canonical, ni el listado, ni `?content=true`, ni
    `/jobs/{id}` traen una sola clave de pago. Pero las leyes de transparencia
    salarial de California, Colorado, Nueva York y Washington obligan a publicar el
    rango, y las empresas lo ponen en la descripcion.

    Medido sobre 905 avisos: gitlab 84 de 199 (42,2%), cloudflare 157 de 400 (39,2%),
    canonical 1 de 306 — canonical es britanica y esas leyes no la alcanzan, que es
    la explicacion de por que el mismo codigo rinde distinto por empresa.

    Usa el MISMO extractor que el resto del sistema. Una segunda ruta de parseo de
    salarios daria dos respuestas distintas para el mismo texto, y el dia que una
    falle nadie sabria cual miro.
    """
    bruto = fila.get(campo)
    if not bruto:
        return None
    # Import diferido a proposito: `endpoint_scraper` es el camino SIN navegador y no
    # puede colgarse `scraper` —con playwright y nlp_engine detras— solo por importarse.
    # Cuando esta rama corre, el proceso ya tiene scraper cargado.
    from scraper import _SALARY_RX
    from models import salario_anual_usd

    # El PRIMER monto plausible como sueldo anual, no el primer monto. Una descripcion
    # entera trae otras cifras con moneda: medido el 2026-09-29, 239 avisos de Canonical
    # dicen "Personal learning and development budget of USD 2,000 per year", y tomar el
    # primer monto guardaba un sueldo de 2.000 USD al ano — peor que no tener salario,
    # porque el gate y el eje de salario lo creen. Por debajo del piso no es un sueldo
    # anual: es un presupuesto, un bono, una tarifa por hora o un mensual.
    texto = texto_de_html(bruto)
    for m in _SALARY_RX.finditer(texto):
        candidato = m.group(0).strip()
        valor = salario_anual_usd(candidato)
        if valor is not None and valor >= _PISO_SUELDO_ANUAL_USD:
            return candidato
    return None


# 10.000 USD al ano. Ningun puesto de los boards que GEMA lee paga menos al ano; por
# debajo, una cifra con moneda en una descripcion es otra cosa (ver arriba).
_PISO_SUELDO_ANUAL_USD = 10_000


# Los valores de `salaryPeriod` (Himalayas) y equivalentes que significan "al ano".
# Medido el 2026-09-30 sobre 20 avisos de Himalayas: annual 17, hourly 2, monthly 1.
# Jobicy dice "yearly". Lo que no esta aca no se compone: lo anualiza la Fase 6.
_PERIODOS_ANUALES = frozenset({"annual", "annually", "yearly", "year", "per year", "anual"})

# Fase 6: los periodos no anuales que se componen, con el sufijo que
# `models._periodo_del_salario` reconoce para anualizar.
_SUFIJO_PERIODO = {
    "hourly": " /hour", "hour": " /hour", "per hour": " /hour",
    "daily": " /day", "day": " /day", "per day": " /day",
    "weekly": " /week", "week": " /week", "per week": " /week",
    "monthly": " /month", "month": " /month", "per month": " /month",
}
_SIMBOLO_DE = {"USD": "$", "EUR": "€", "GBP": "£"}


def _salario(fila: dict, fm: dict, salario_en_descripcion: str = "") -> Optional[str]:
    """
    `salary_raw` es un string porque `parse_salary_usd()` lee strings. Un endpoint
    que entrega enteros —remoteok da `salary_min`/`salary_max`— se compone al
    formato que ese parser ya entiende, con el `$` que exige.

    Componer en vez de agregar una columna nueva mantiene una sola ruta de parseo.
    El día que la Fase 4 acepte libras y euros, este camino se beneficia igual.

    **Moneda y periodo declarados (Fase 1 del ).** Si el `field_map`
    dice donde vive la moneda (`salary_currency`), el monto se compone con ESE
    codigo —`EUR 90,000 - EUR 120,000`, que v5 ya lee— y no con `$`. Medido en la
    muestra del 30-09: Himalayas trae EUR, Jobicy trae BRL. Con el `$` de siempre,
    `BRL 249000` se guardaba como 249.000 dolares: un sueldo inventado, peor que
    ninguno. Sin codigo en la fila, no se asume el dolar: no hay salario.

    Si dice donde vive el periodo (`salary_period`), solo se compone el anual. Un
    `hourly` de 60 compuesto como `$60` se leeria como 60 dolares al ano; la Fase 6
    es la que anualiza, con la tabla de multiplicadores.
    """
    directo = _texto(_por_path(fila, fm["salary_raw"])) if fm.get("salary_raw") else ""
    if directo:
        return directo

    lo = _por_path(fila, fm["salary_min"]) if fm.get("salary_min") else None
    hi = _por_path(fila, fm["salary_max"]) if fm.get("salary_max") else None
    try:
        lo = int(float(lo)) if lo not in (None, "", 0) else None
        hi = int(float(hi)) if hi not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None

    sufijo = ""
    if (lo or hi) and fm.get("salary_period"):
        periodo = _texto(_por_path(fila, fm["salary_period"])).lower()
        if periodo not in _PERIODOS_ANUALES:
            # Fase 6: un periodo conocido se compone CON su periodo y lo anualiza el
            # parser ("$50 - $90 /hour" -> 104.000). Uno desconocido ("per-project")
            # no se compone: no hay forma honesta de llevarlo a un sueldo anual.
            sufijo = _SUFIJO_PERIODO.get(periodo)
            if sufijo is None:
                lo = hi = None

    if (lo or hi) and fm.get("salary_currency"):
        codigo = _texto(_por_path(fila, fm["salary_currency"])).upper()
        if not re.fullmatch(r"[A-Z]{3}", codigo):
            return None
        # Un periodo no anual va con simbolo cuando el parser lo tiene: "USD 50" sin
        # miles no lo lee (lo protege del presupuesto "USD 2,000" de Canonical), y
        # "$50 /hour" si. Las monedas sin simbolo conocido conservan el codigo.
        pre = _SIMBOLO_DE.get(codigo, f"{codigo} ") if sufijo else f"{codigo} "
    else:
        pre = "$"

    if lo and hi and hi != lo:
        return f"{pre}{lo:,} - {pre}{hi:,}{sufijo}"
    if lo:
        return f"{pre}{lo:,}{sufijo}"
    if hi:
        return f"{pre}{hi:,}{sufijo}"

    # Ultimo recurso, y en ese orden a proposito: un campo declarado siempre le gana a
    # un numero pescado de la prosa. La descripcion se lee solo cuando la plataforma no
    # tiene donde publicarlo.
    if salario_en_descripcion:
        return _salario_de_descripcion(fila, salario_en_descripcion)
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


def _ubicacion_de_lista(valor: Any) -> str:
    """
    `["Argentina", "Chile", "Spain"]` -> `"Argentina; Chile; Spain"`, y `[]` -> `"Worldwide"`.

    Existe por Himalayas: `locationRestrictions` es la lista de paises desde los que se
    puede trabajar el puesto, a veces de 70 elementos. `_texto` unia una lista con
    espacios, y "United States United Kingdom Chile" no se puede partir de vuelta. Con
    "; " queda en el mismo formato que ya publica builtin.com ("Greece; Norway; Chile;
    ..."), que el matcher lee desde el 29-09.

    **La lista vacia significa "sin restriccion"**, no "sin dato": es la semantica de
    un campo que se llama *restrictions*, y en la muestra del 30-09 el segundo aviso de
    `country=<pais>` vino con `[]` —Himalayas lo devuelve para ese pais porque no excluye a
    nadie—. Traducirla a "Worldwide" es decir lo que la fuente dijo. Dejarla vacia
    mandaria el aviso a "el board no la expone", que es falso.

    Transformacion con nombre, activada por `ubicacion_es_lista` en la entrada del
    board: el mapa mapea, el codigo transforma. Un valor que no es lista pasa como
    texto, sin tocar.
    """
    if not isinstance(valor, (list, tuple)):
        return _texto(valor)
    partes = [_texto(_nombre_de_pais(v)) for v in valor]
    partes = [p for p in partes if p]
    return "; ".join(partes) if partes else "Worldwide"


def _nombre_de_pais(v: Any) -> Any:
    """
    Un pais como string ("Chile") o como objeto (`{"alpha2": "CL", "name": "Chile",
    "slug": "chile"}`) al nombre que el matcher sabe leer.

    La respuesta real del 2026-09-30 trae strings; el diccionario de datos y el OpenAPI
    oficiales de Himalayas documentan objetos (auditoria de revision externa, punto 15). Si la API
    pasa a la forma documentada, `_texto(dict)` guardaria la representacion del dict
    ("{'alpha2': 'PE', ...}") y el veredicto de ubicacion seria basura. Se aceptan las
    dos formas para que ese cambio no rompa nada en silencio.
    """
    if isinstance(v, dict):
        return v.get("name") or v.get("alpha2") or v.get("slug") or ""
    return v


def _url_del_aviso(valor: str, url_base: str = "") -> str:
    """
    El valor como URL absoluta http(s), o "" si no lo es ni se puede volver una sin
    inventarla.

    **Solo se resuelve contra la base un camino relativo** (`/jobs/7`, `./x`, `../x`).
    Hasta el 30-09 se resolvia cualquier cosa que no empezara con http: un `guid` opaco
    —"stripe-abc123", que es lo que el OpenAPI de Himalayas documenta para ese campo
    (auditoria de revision externa, punto 15)— se convertia en
    `https://himalayas.app/jobs/api/stripe-abc123`, un enlace que nadie publico y que
    `compute_hash` tomaba como identidad del aviso. Un ID no es un camino: sin URL real
    se prueba `url_alternativa`, y si tampoco hay, el aviso se descarta y cuenta como
    invalido.
    """
    if not valor:
        return ""
    if url_base and valor.startswith(("/", "./", "../")):
        valor = urljoin(url_base, valor)
    if not valor.lower().startswith(("http://", "https://")):
        return ""
    if not urlparse(valor).netloc:
        return ""
    return valor


_SEGMENTO_SEGURO = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MARCADOR = re.compile(r"\{(\w+)\}")


def _url_compuesta(fila: dict, plantilla: str) -> str:
    """
    `"https://gorillalogic.bamboohr.com/careers/{id}"` + `{"id": "34"}` ->
    `"https://gorillalogic.bamboohr.com/careers/34"`, o "" si no se puede componer.

    Existe por BambooHR y Personio (Fase 8 del ): sus listados no traen la
    URL del aviso, solo el `id`. Verificado el 2026-10-04 con un pedido real a cada una:
    `gorillalogic.bamboohr.com/careers/34` devuelve 200 con `og:url` igual a la URL
    compuesta, y `jtl-software-gmbh.jobs.personio.de/job/2666138` devuelve 200 con el
    titulo del aviso. Transformacion con nombre, activada por la plantilla de la
    plataforma: el mapa mapea, el codigo transforma.

    **Cada valor tiene que ser un segmento de ruta inofensivo** (letras, digitos, `_` y
    `-`). Un `id` con `/`, `?` o `..` compondria una URL que nadie publico y que
    `compute_hash` tomaria como identidad del aviso: sin URL real, el aviso se descarta
    y cuenta como invalido, igual que en `_url_del_aviso`.
    """
    if not plantilla:
        return ""
    salida = plantilla
    for campo in set(_MARCADOR.findall(plantilla)):
        valor = _texto(_por_path(fila, campo))
        if not _SEGMENTO_SEGURO.fullmatch(valor):
            return ""
        salida = salida.replace("{" + campo + "}", valor)
    return _url_del_aviso(salida)


def _ubicacion_compuesta(fila: dict, rutas: tuple) -> str:
    """
    Varias rutas de ubicacion unidas con ", ", sin vacios ni repetidos.

    Workable separa `city` y `country`, BambooHR los anida en `atsLocation`. Medido el
    2026-10-04: huggingface en Workable trae `city="Paris", country="France"`; Gorilla
    Logic en BambooHR trae `atsLocation.city="San Jose", country="Costa Rica"` en un
    aviso y nulos en los demas. El pais es lo que lee la politica de ubicacion del
    matcher, y con una sola ruta habria que elegir entre perder la ciudad o el pais.
    """
    partes: list[str] = []
    for r in rutas:
        p = _texto(_por_path(fila, r))
        if p and p not in partes:
            partes.append(p)
    return ", ".join(partes)


def _con_region(ubicacion: str, region: str) -> str:
    """
    `"Remote"` + `"Europe"` -> `"Remote; Europe"`.

    Para boards cuyos avisos son todos de una region que el campo de ubicacion no
    nombra. Arbeitnow, medido el 2026-09-30: de 19 remotos, 10 dicen "Remote" a secas,
    2 "Remote job", 1 "Homeoffice", 1 "Poste à distance". Es remoto dentro de Europa,
    por derecho laboral y porque el board es europeo. El matcher leia "Remote" como
    neutral —ni abierto ni restringido— y eso es PASS desde el país del candidato: un falso positivo.

    Se agrega al final, no se reemplaza. "Remote (Worldwide); Europe" sigue siendo
    abierto porque el matcher mira primero si la ubicacion es accesible; lo que cambia
    es el caso sin informacion, que deja de leerse como abierto. Y si la ubicacion ya
    nombra la region, no se repite.
    """
    if not region:
        return ubicacion
    if not ubicacion:
        return region
    if region.lower() in ubicacion.lower():
        return ubicacion
    return f"{ubicacion}; {region}"


def _pasa_filtro(fila: dict, campo: str) -> bool:
    """`solo_si_campo`: la fila entra solo si ese campo es verdadero. Un string
    "true"/"false" se lee como lo que dice: hay APIs que serializan booleanos asi."""
    if not campo:
        return True
    v = _por_path(fila, campo)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "si", "sí")
    return bool(v)


def a_job_result(fila: dict, fm: dict, dominio: str,
                 url_base: str = "",
                 titulo_trae_empresa: bool = False,
                 salario_en_descripcion: str = "",
                 defaults: Optional[dict] = None,
                 ubicacion_es_lista: bool = False,
                 region_del_board: str = "",
                 url_aviso_template: str = "") -> Optional[JobResult]:
    """
    Una fila cruda a `JobResult`, o `None` si no se puede sostener.

    Se descarta cuando falta título o cuando la URL no es absoluta. La URL es la clave
    de idempotencia de todo el sistema (`compute_hash`), así que una relativa o vacía
    envenena la deduplicación: mejor perder el aviso que corromper el registro.

    `company` degrada a placeholder y no descarta — esa decisión ya se tomó en la
    Fase 31 (2026-07-11), cuando exigir company nuleaba boards enteros.

    `url_aviso_template` (Fase 8) compone la URL desde campos de la fila cuando la
    fuente no la publica (BambooHR, Personio). Va despues de `url` y `url_alternativa`:
    una URL publicada siempre le gana a una compuesta.
    """
    titulo = _texto(_por_path(fila, fm.get("title", "title")))
    if not titulo:
        return None

    empresa_del_titulo = ""
    if titulo_trae_empresa:
        titulo, empresa_del_titulo = _partir_prefijo_empresa(titulo)
        if not titulo:
            return None

    url = _url_del_aviso(_texto(_por_path(fila, fm.get("url", "url"))), url_base)
    if not url and fm.get("url_alternativa"):
        url = _url_del_aviso(_texto(_por_path(fila, fm["url_alternativa"])), url_base)
    if not url and url_aviso_template:
        url = _url_compuesta(fila, url_aviso_template)
    if not url:
        return None

    # `defaults` rellena lo que la fuente no trae en el payload. Lever y Ashby no
    # devuelven el nombre de la empresa —el tablero ES de una empresa, asi que para
    # ellos es contexto, no dato—, y sin esto los 1853 avisos de Lever entrarian al
    # registry como "(empresa no listada)". No pisa lo que si vino: un default que
    # gana sobre el dato real seria peor que no tenerlo.
    dfl = defaults or {}
    empresa = _texto(_por_path(fila, fm["company"])) if fm.get("company") else ""
    empresa = empresa or empresa_del_titulo or _texto(dfl.get("company"))

    if isinstance(fm.get("location"), (list, tuple)):
        ubicacion = _ubicacion_compuesta(fila, tuple(fm["location"]))
    elif fm.get("location"):
        crudo = _por_path(fila, fm["location"])
        # `_por_path` devuelve None tanto si el campo falta como si vale null: solo una
        # lista REAL (vacia incluida) es una declaracion de "sin restriccion".
        ubicacion = (_ubicacion_de_lista(crudo)
                     if ubicacion_es_lista and isinstance(crudo, (list, tuple))
                     else _texto(crudo))
    else:
        ubicacion = ""
    ubicacion = ubicacion or _texto(dfl.get("location"))
    ubicacion = _con_region(ubicacion, region_del_board)

    fecha_limite = _texto(_por_path(fila, fm["deadline"])) if fm.get("deadline") else ""

    return JobResult(
        title=titulo,
        company=empresa or "(empresa no listada)",
        url=url,
        salary_raw=_salario(fila, fm, salario_en_descripcion) or None,
        location_raw=ubicacion or None,
        source_domain=dominio,
        scraped_at=datetime.now(timezone.utc),
        deadline=fecha_limite or None,
        descripcion=_descripcion(fila, fm),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación por board
# ─────────────────────────────────────────────────────────────────────────────

def espera_pendiente(intervalo_s: float, ultimo_ts: float, ahora: float) -> float:
    """
    Segundos que faltan para poder volver a pedir este endpoint, o 0 si ya se puede.

    Es el `intervalo_minimo_s` de la estrategia contra el `ultimo_pedido_ts` guardado.
    Los terminos de Jobicy dicen "no mas de una vez por hora" y los de Remotive "como
    mucho 4 veces por dia": un limite declarado entre corridas, que el `crawl_delay_s`
    —dentro de una corrida— no cubre. Dos corridas manuales seguidas en una tarde de
    desarrollo ya lo violaban.

    Un reloj que vuelve atras (`ahora < ultimo_ts`) no bloquea para siempre: se trata
    como si el ultimo pedido fuera ahora, y se espera el intervalo completo una vez.
    """
    if intervalo_s <= 0 or ultimo_ts <= 0:
        return 0.0
    transcurrido = max(0.0, ahora - ultimo_ts)
    return max(0.0, intervalo_s - transcurrido)


@dataclass
class ResultadoEndpoint:
    dominio: str
    jobs: list[JobResult]
    items_crudos: int = 0
    descartados_irrelevantes: int = 0
    descartados_invalidos: int = 0
    descartados_por_filtro: int = 0
    error: str = ""
    ms: float = 0.0
    #  + auditoria de revision externa, punto 16: que se pidio, cuanto declaro la
    # fuente que habia, y que se dejo de pedir. "20 de 171" no es "la fuente tiene 20".
    consultas: list = field(default_factory=list)
    """Una entrada por URL pedida: `{"url", "items", "total"}`; `total` es lo que la
    fuente declara (`totalCount` en Himalayas) o None si no lo declara."""
    consultas_omitidas: int = 0
    """URLs no pedidas porque una consulta identica se hizo dentro de `ttl_consulta_s`."""

    @property
    def parcial(self) -> bool:
        """Alguna consulta trajo menos avisos de los que la fuente declara tener."""
        return any(c.get("total") is not None and c["total"] > c["items"]
                   for c in self.consultas)


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
    salario_en_descripcion: str = "",
    defaults: Optional[dict] = None,
    ubicacion_es_lista: bool = False,
    pais: Optional[str] = None,
    solo_si_campo: str = "",
    region_del_board: str = "",
    total_path: str = "",
    ttl_consulta_s: float = 0.0,
    consultas_ts: Optional[dict] = None,
    url_aviso_template: str = "",
    xml_item: str = "",
    seguir_redirecciones: bool = True,
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

    `{pais}` se reemplaza por el país del candidato (`config.PAIS_CANDIDATO`, o `pais`
    si se pasa). Es la decisión D2 del : Himalayas responde qué avisos
    se pueden trabajar desde el pais del candidato, y esa respuesta es mejor que cualquier regex nuestro
    sobre una lista de 70 países. **Falla cerrado** (auditoría de revision externa, punto 15): sin
    país, o con uno que la política de ubicación del matcher no da por accesible, no se
    pide nada. Pedir `country=` vacío traería el catálogo mundial, y la fuente y el
    matcher estarían respondiendo dos preguntas distintas.

    `ttl_consulta_s` con `consultas_ts` ({url: epoch}) no repite una consulta idéntica
    hecha hace menos de ese tiempo (revision externa, punto 16): Himalayas actualiza sus datos cada
    24 h, y lo que devolvería ya está en el registry. `total_path` lee cuántos avisos
    declara la fuente para esa consulta (`totalCount`): 20 de 171 es cobertura parcial,
    no una fuente con 20 avisos.

    Fase 8 del : `response_format="xml"` con `xml_item` lee un XML que no
    es RSS (Personio, `<position>`); `url_aviso_template` compone la URL del aviso
    cuando la fuente solo trae el `id`; `seguir_redirecciones=False` deja ver el 302/307
    con que BambooHR y Personio contestan una empresa inexistente.
    """
    fm = field_map or {}
    t0 = time.perf_counter()
    res = ResultadoEndpoint(dominio=dominio, jobs=[])

    if "{pais}" in endpoint_template:
        p = (pais if pais is not None else getattr(config, "PAIS_CANDIDATO", "")).strip()
        motivo = _pais_no_utilizable(p)
        if motivo:
            res.error = f"PAIS_CANDIDATO: {motivo}"
            res.ms = (time.perf_counter() - t0) * 1000
            log(f"[ENDPOINT] {dominio} — no se consulta: {motivo}")
            return res
        endpoint_template = endpoint_template.replace("{pais}", requests.utils.quote(p))

    if "{title}" in endpoint_template:
        urls = [endpoint_template.replace("{title}", requests.utils.quote(t))
                for t in (titles or [])]
    else:
        urls = [endpoint_template]

    if ttl_consulta_s > 0 and consultas_ts:
        ahora = time.time()
        frescas = {u for u in urls
                   if 0 <= ahora - float(consultas_ts.get(u, 0) or 0) < ttl_consulta_s}
        res.consultas_omitidas = len(frescas)
        urls = [u for u in urls if u not in frescas]
        if frescas:
            log(f"[ENDPOINT] {dominio} — {len(frescas)} consultas omitidas: se hicieron "
                f"hace menos de {ttl_consulta_s / 3600:.0f} h y sus avisos ya estan "
                f"en el registry")

    vistos: dict[str, JobResult] = {}
    for url in urls:
        try:
            extra = {} if seguir_redirecciones else {"seguir_redirecciones": False}
            cuerpo = fetch(url, crawl_delay_s=crawl_delay_s, sesion=sesion, **extra)
        except (EndpointNoPublico, EndpointAgotado) as exc:
            res.error = f"{type(exc).__name__}: {exc}"
            log(f"[ENDPOINT] {dominio} — {res.error}")
            continue
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {str(exc)[:120]}"
            log(f"[ENDPOINT] {dominio} — {res.error}")
            continue

        try:
            if response_format == "rss":
                filas = items_de_rss(cuerpo)
            elif response_format == "xml":
                filas = items_de_xml(cuerpo, xml_item)
            else:
                filas = items_de_json(cuerpo, items_path)
        except Exception as exc:
            res.error = f"parseo {response_format}: {type(exc).__name__}: {str(exc)[:90]}"
            log(f"[ENDPOINT] {dominio} — {res.error}")
            continue

        res.items_crudos += len(filas)
        total = _total_declarado(cuerpo, total_path) if total_path else None
        res.consultas.append({"url": url, "items": len(filas), "total": total})
        invalidos_antes = res.descartados_invalidos

        for fila in filas:
            if not _pasa_filtro(fila, solo_si_campo):
                res.descartados_por_filtro += 1
                continue
            job = a_job_result(fila, fm, dominio, url_base=url,
                               titulo_trae_empresa=titulo_trae_empresa,
                               salario_en_descripcion=salario_en_descripcion,
                               defaults=defaults,
                               ubicacion_es_lista=ubicacion_es_lista,
                               region_del_board=region_del_board,
                               url_aviso_template=url_aviso_template)
            if job is None:
                res.descartados_invalidos += 1
                continue
            if es_relevante is not None and not es_relevante(job.title):
                res.descartados_irrelevantes += 1
                continue
            if job.url in vistos:
                _fusionar_ubicacion(vistos[job.url], job.location_raw)
                continue
            vistos[job.url] = job
            res.jobs.append(job)

        # Ninguna fila de una respuesta no vacía se pudo leer: no es una búsqueda sin
        # resultados, es una forma de respuesta que el mapa ya no entiende (revision externa, punto
        # 15: `source_schema_changed`). Se reporta como error para que el board quede
        # degradado y se vea, en vez de pasar como "0 avisos".
        evaluadas = len(filas) - sum(1 for f in filas if not _pasa_filtro(f, solo_si_campo))
        if evaluadas and res.descartados_invalidos - invalidos_antes == evaluadas:
            res.error = (f"esquema_cambiado: {evaluadas} de {evaluadas} items sin título "
                         f"o sin URL http(s) válida en {url}")
            log(f"[ENDPOINT] {dominio} — {res.error}")

    res.ms = (time.perf_counter() - t0) * 1000
    pedidas = [c for c in res.consultas if c["total"] is not None]
    cobertura = (f" · primera página de {sum(c['total'] for c in pedidas)} declarados"
                 if res.parcial else "")
    log(f"[ENDPOINT] {dominio} — {len(res.jobs)} avisos de {res.items_crudos} items "
        f"en {res.ms:.0f}ms{cobertura} "
        f"(descartados: {res.descartados_irrelevantes} off-topic, "
        f"{res.descartados_invalidos} sin título o sin URL absoluta"
        + (f", {res.descartados_por_filtro} sin {solo_si_campo}" if solo_si_campo else "")
        + ")")
    return res


_UBICACION_FUSIONADA_MAX = 400


def _fusionar_ubicacion(job: JobResult, otra: Optional[str]) -> None:
    """
    Suma a `job` la ubicacion de otra fila con la misma URL, con "; " como separador.

    Existe por Workable (Fase 8 del ): el widget repite el aviso una vez
    por ubicacion. Medido el 2026-10-04 contra azumo: 57 filas para 10 avisos, cada uno
    seis veces —Argentina, Chile, Brazil, Colombia, Mexico, Uruguay...—. La deduplicacion
    por URL se quedaba con la primera fila, y un aviso abierto en Uruguay entraba al
    registry como "Argentina": el dato que mas le importa a la politica de ubicacion
    del matcher, perdido por el orden de un arreglo. "; " es el formato de lista que el
    matcher ya lee desde el 29-09 (builtin.com).

    Una ubicacion repetida no se suma, y el total tiene tope: una fuente que repitiera
    el aviso en doscientas ciudades no puede inflar el campo sin limite.
    """
    if not otra:
        return
    actual = job.location_raw or ""
    if otra in [p.strip() for p in actual.split(";")]:
        return
    nueva = f"{actual}; {otra}" if actual else otra
    if len(nueva) <= _UBICACION_FUSIONADA_MAX:
        job.location_raw = nueva


def _total_declarado(cuerpo: bytes, total_path: str) -> Optional[int]:
    """Cuántos avisos dice la fuente que hay para esta consulta, o None si no lo dice."""
    try:
        d = json.loads(cuerpo.decode("utf-8", errors="replace"))
        v = _por_path(d, total_path) if isinstance(d, dict) else None
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _pais_no_utilizable(pais: str) -> str:
    """
    "" si `pais` sirve para filtrar la fuente; si no, por qué.

    La política de ubicación del matcher está escrita para LATAM: da por accesibles
    LATAM y lo mundial. Un país que esa política no reconoce como accesible haría que
    la fuente filtre por una pregunta y el matcher puntúe con otra.
    """
    if not pais:
        return ("vacío. Sin país la fuente devolvería el catálogo mundial y el filtro de "
                "D2 se apagaría en silencio")
    try:
        from matcher import _es_accesible, _sin_tildes
    except Exception:
        return ""
    if not _es_accesible(_sin_tildes(pais.lower())):
        return (f"{pais!r} no es un país que la política de ubicación del matcher dé por "
                f"accesible; la fuente y el matcher responderían preguntas distintas")
    return ""
