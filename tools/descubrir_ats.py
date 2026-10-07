"""
descubrir_ats.py — ¿qué empresa publica en qué ATS, y con qué token?

Fase 8 del . La idea es de Job-Radar: la página de careers de una empresa
embebe el tablero de su ATS, y el token está en el HTML (`jobs.lever.co/spotify`,
`bunq.recruitee.com`, `gorillalogic.bamboohr.com`). Leerlo es más barato que adivinarlo,
y los 404 del 30-09 a la mañana salieron justamente de tokens adivinados.

Tres reglas que este script no negocia:

**Exige avisos, no un 200.** Medido el 30-09 y el 04-10: Workable devuelve 200 con
`"jobs": []` para hotjar, toggl, typeform, deel y otras cuatro; BambooHR devuelve 200 con
`"result": []` para zapier, close, toggl y cinco más. Son cuentas viejas de empresas que
se mudaron de ATS. Un token que existe y no publica es una llamada vacía por corrida.

**El nombre que reporta el board tiene que coincidir con el dominio.** Una página de
careers puede embeber el tablero de la casa matriz, de un partner o de un cliente. JTL
publica en Personio avisos de su filial Returnless con otra `subcompany`. Cuando la
plataforma no reporta nombre (Lever, Ashby, BambooHR), se compara el token y la
propuesta lo dice: es una validación más débil y no se esconde.

**Propone, nunca escribe.** La salida es texto o JSON con la línea lista para pegar en
`ats_registry.py`, siempre con `activa=False`. Activar una empresa es una decisión de
el usuario (D1 del plan), no de un script.

Disciplina de red: respeta el robots.txt de cada host (RFC 9309: 4xx = sin política,
5xx o sin red = todo prohibido), su `Crawl-delay`, el `crawl_delay_s` de la plataforma
en el registro (Personio: 5 s) y nunca más de un pedido por segundo en total.

    python tools/descubrir_ats.py gorillalogic.com azumo.com
    python tools/descubrir_ats.py --archivo dominios.txt --json
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ats_registry  # noqa: E402
import endpoint_scraper as es  # noqa: E402
import metrics  # noqa: E402

UA = "GEMA-descubrir-ats/1.0 (+job board aggregator; respeta robots.txt y crawl-delay)"
TIMEOUT_S = 20
PAUSA_MIN_S = 1.0             # entre dos pedidos cualesquiera, a cualquier host
MAX_BYTES = 3_000_000         # una pagina de careers mas grande que esto no es una pagina
REINTENTOS = 2                # 429/5xx: un reintento con backoff y jitter, no mas
BACKOFF_TECHO_S = 30.0

# Donde buscar la pagina de careers, en orden. Se corta en la primera que trae un token.
RUTAS_CAREERS = ("https://{d}/careers", "https://{d}/jobs", "https://careers.{d}/",
                 "https://jobs.{d}/", "https://{d}/")

# Segundo salto: enlaces del mismo sitio que parecen la lista de puestos. Medido el
# 2026-10-04: gorillalogic.com/careers/ no nombra a BambooHR, pero enlaza a
# /careers/open-roles/; about.gitlab.com/jobs/ enlaza a /jobs/all-jobs/. El token suele
# estar una pagina mas adentro. Tope fijo: un sitio con cincuenta enlaces "jobs" no
# convierte al descubridor en un crawler.
MAX_SEGUNDO_SALTO = 3
_ENLACE_CAREERS = re.compile(
    r"(career|jobs?\b|jobs/|open-?roles|open-?positions|openings|vacanc|join-us)", re.I)
_EXTENSIONES_NO_HTML = (".json", ".atom", ".xml", ".rss", ".pdf", ".png", ".jpg", ".svg",
                        ".css", ".js")


def _sin_barra(url: str) -> str:
    return url.rstrip("/").lower()


def enlaces_de_careers(texto: str, base: str, dominio: str) -> list[str]:
    """Los enlaces de `texto` al MISMO sitio que parecen una pagina de puestos."""
    etiqueta = etiqueta_de_dominio(dominio)
    salida: list[str] = []
    for href in re.findall(r"""href=["']?([^"' >#]+)""", html.unescape(texto or "")):
        url = urljoin(base, href)
        p = urlparse(url)
        if p.scheme != "https" or etiqueta_de_dominio(p.hostname or "") != etiqueta:
            continue
        if p.path.lower().endswith(_EXTENSIONES_NO_HTML) or p.query:
            continue
        if not _ENLACE_CAREERS.search(p.path):
            continue
        limpia = f"https://{p.hostname}{p.path}"
        if _sin_barra(limpia) != _sin_barra(base) and limpia not in salida:
            salida.append(limpia)
    # Primero los que nombran una lista ("open-roles", "all-jobs", "positions"): son la
    # pagina que embebe el tablero. Orden estable para el resto.
    return sorted(salida, key=lambda u: 0 if _LISTA_DE_PUESTOS.search(u) else 1)


_LISTA_DE_PUESTOS = re.compile(r"(open|all-jobs|positions|openings|vacanc|roles)", re.I)


# ─────────────────────────────────────────────────────────────────────────────
# Deteccion de tokens
# ─────────────────────────────────────────────────────────────────────────────

# (plataforma, regex con un grupo = token). Hosts sin distincion de mayusculas; el token
# se conserva tal cual aparece y se valida contra el formato del registro despues.
PATRONES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (p, re.compile(rx, re.IGNORECASE)) for p, rx in (
        ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
        ("greenhouse", r"boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
        ("greenhouse", r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)"),
        ("lever", r"api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)"),
        ("lever", r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)"),
        ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)"),
        ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
        ("workable", r"apply\.workable\.com/api/v\d+/widget/accounts/([A-Za-z0-9_-]+)"),
        ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
        ("recruitee", r"\b([a-z0-9][a-z0-9-]*)\.recruitee\.com"),
        ("personio", r"\b([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)"),
        ("bamboohr", r"\b([a-z0-9][a-z0-9-]*)\.bamboohr\.com"),
    )
)

# Segmentos que esos patrones capturan y no son tokens: rutas y servicios de la propia
# plataforma. `careers-analytics` salio de la corrida del 2026-10-04: toda pagina de
# careers servida por Recruitee lo trae como `analyticsBaseUrl`, y sin excluirlo el
# descubridor proponia la empresa "careers-analytics" (403 en su API).
NO_TOKENS = {
    "greenhouse": {"embed", "v1", "boards", "job_board"},
    "lever": {"v0"},
    "ashby": {"api", "posting-api"},
    "workable": {"api", "j", "careers", "static"},
    "recruitee": {"www", "app", "api", "careers", "help", "blog", "careers-analytics"},
    "personio": {"www", "api"},
    "bamboohr": {"www", "api", "help", "marketplace", "partners"},
}

# Paginas de careers en dominio propio que no nombran el host del ATS. Medido el
# 2026-10-04: careers.hostaway.com y jobs.channable.com los sirve Recruitee y el unico
# rastro del token es `"subdomain":"hostaway"` dentro de la configuracion de la app
# (con las comillas escapadas como `&quot;`). Una clave "subdomain" suelta es demasiado
# generica: solo cuenta si la pagina trae tambien el CDN de la plataforma.
PATRONES_CONDICIONADOS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("recruitee", "recruiteecdn.com",
     re.compile(r'"subdomain"\s*:\s*"([a-z0-9][a-z0-9-]*)"', re.IGNORECASE)),
)

# El formato que exige el test del registro (tests/test_ats_fase8.py).
FORMATO_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*")


def detectar_tokens(texto: str) -> list[tuple[str, str]]:
    """Los `(plataforma, token)` distintos que aparecen en `texto`, en orden de aparicion.

    El texto se desescapa antes: los props de React llegan con `&quot;` y los enlaces de
    Greenhouse con `&amp;for=`.
    """
    texto = html.unescape(texto or "")
    vistos: list[tuple[str, str]] = []
    candidatos = [(p, rx) for p, rx in PATRONES]
    candidatos += [(p, rx) for p, marca, rx in PATRONES_CONDICIONADOS if marca in texto]
    for plataforma, rx in candidatos:
        for m in rx.finditer(texto):
            token = m.group(1).strip(".")
            if not token or token.lower() in NO_TOKENS.get(plataforma, set()):
                continue
            par = (plataforma, token)
            if par not in vistos:
                vistos.append(par)
    return vistos


# ─────────────────────────────────────────────────────────────────────────────
# Nombre del board contra dominio
# ─────────────────────────────────────────────────────────────────────────────

_SUFIJOS_LEGALES = {
    "gmbh", "ag", "inc", "llc", "ltd", "limited", "bv", "nv", "sa", "sas", "sl", "srl",
    "sac", "saa", "spa", "co", "corp", "corporation", "company", "group", "plc", "oy",
    "ab", "as", "aps", "kg", "se", "pty", "the",
}
_SEGUNDO_NIVEL = {"co", "com", "org", "net", "gob", "gov", "edu", "ac"}


def etiqueta_de_dominio(dominio: str) -> str:
    """`www.gorillalogic.com` -> `gorillalogic`; `empresa.com.co` -> `empresa`."""
    host = (urlparse(dominio).hostname if "//" in dominio else dominio).lower().strip(".")
    partes = [p for p in host.split(".") if p]
    if partes and partes[0] == "www":
        partes = partes[1:]
    if len(partes) >= 3 and partes[-2] in _SEGUNDO_NIVEL and len(partes[-1]) == 2:
        return partes[-3]
    return partes[-2] if len(partes) >= 2 else (partes[0] if partes else "")


def normalizar_nombre(texto: str) -> str:
    """Minusculas, sin tildes, sin sufijos legales, solo letras y digitos."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    palabras = [w for w in re.split(r"[^a-z0-9]+", t) if w and w not in _SUFIJOS_LEGALES]
    return "".join(palabras)


def coincide(nombre: str, dominio: str) -> bool:
    """
    El nombre del board corresponde a la empresa del dominio.

    Igualdad tras normalizar, o una contenida en la otra si la corta tiene al menos 4
    caracteres: "Gorilla Logic" ~ gorillalogic.com, "JTL-Software GmbH" ~ jtl-software.com,
    "Azumo" ~ azumo.com. El minimo de 4 evita que "ai" o "io" coincidan con cualquier cosa.
    """
    a, b = normalizar_nombre(nombre), normalizar_nombre(etiqueta_de_dominio(dominio))
    if not a or not b:
        return False
    if a == b:
        return True
    corta, larga = sorted((a, b), key=len)
    return len(corta) >= 4 and corta in larga


# ─────────────────────────────────────────────────────────────────────────────
# HTTP con robots.txt, pausas y reintento
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Respuesta:
    status: int
    url: str
    cuerpo: bytes
    headers: dict = field(default_factory=dict)

    @property
    def texto(self) -> str:
        return self.cuerpo.decode("utf-8", errors="replace")


def _get_real(url: str, seguir: bool) -> Respuesta:
    import requests
    with requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT_S,
                      allow_redirects=seguir, stream=True) as r:
        cuerpo = bytearray()
        for trozo in r.iter_content(64 * 1024):
            cuerpo.extend(trozo)
            if len(cuerpo) > MAX_BYTES:
                break
        return Respuesta(r.status_code, r.url, bytes(cuerpo[:MAX_BYTES]), dict(r.headers))


class Red:
    """
    Todo pedido del descubridor pasa por aca: robots.txt por host, pausa minima global,
    `Crawl-delay` por host y un reintento con backoff y jitter ante 429/5xx.

    `get_crudo(url, seguir_redirecciones) -> Respuesta` es inyectable: los tests lo
    reemplazan por un diccionario y no salen a la red.
    """

    def __init__(self, get_crudo: Callable[[str, bool], Respuesta] = _get_real,
                 dormir: Callable[[float], None] = time.sleep,
                 reloj: Callable[[], float] = time.monotonic):
        self._get = get_crudo
        self._dormir = dormir
        self._reloj = reloj
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._ultimo_global = -1e9
        self._ultimo_host: dict[str, float] = {}
        self.pedidos = 0

    def _esperar(self, host: str, minimo_host: float) -> None:
        ahora = self._reloj()
        espera = max(PAUSA_MIN_S - (ahora - self._ultimo_global),
                     minimo_host - (ahora - self._ultimo_host.get(host, -1e9)), 0.0)
        if espera > 0:
            self._dormir(espera)
        t = self._reloj()
        self._ultimo_global = t
        self._ultimo_host[host] = t

    def _pedir(self, url: str, seguir: bool, minimo_host: float = 0.0,
               reintentos: int = REINTENTOS) -> Respuesta:
        host = urlparse(url).hostname or ""
        ultimo: Optional[Respuesta] = None
        for intento in range(1, reintentos + 1):
            self._esperar(host, minimo_host)
            self.pedidos += 1
            ultimo = self._get(url, seguir)
            if ultimo.status != 429 and ultimo.status < 500:
                return ultimo
            metrics.emit("descubridor_http_limitado_total", 1, host=host,
                         status=ultimo.status)
            if intento < reintentos:
                espera = min(BACKOFF_TECHO_S, 2.0 * (2 ** (intento - 1)))
                try:
                    espera = min(BACKOFF_TECHO_S, float(ultimo.headers.get("Retry-After")))
                except (TypeError, ValueError):
                    pass
                espera = max(0.0, espera)          # Retry-After: -5 -> time.sleep lanza
                self._dormir(espera + random.uniform(0, 0.5))
        assert ultimo is not None
        return ultimo

    def robots(self, host: str) -> Optional[RobotFileParser]:
        """
        El robots.txt del host, o None si no se pudo leer (5xx, sin red): RFC 9309 manda
        tratar ese caso como prohibicion total. Un 4xx es "sin politica": todo permitido.
        Se sigue la redireccion porque Recruitee manda el robots.txt de
        {t}.recruitee.com al dominio de careers de la empresa.

        **429 no es "sin politica", es "no me pidas".** Medido el 2026-10-04: channable.com
        contesto 429 al robots.txt, y leerlo como 4xx dejaba al descubridor pidiendo
        /careers y /jobs —cuatro 429 mas— a un host que ya habia dicho que pare. Se trata
        como inalcanzable: ningun pedido mas a ese host en la corrida.
        """
        if host in self._robots:
            return self._robots[host]
        rp: Optional[RobotFileParser] = RobotFileParser()
        try:
            r = self._pedir(f"https://{host}/robots.txt", True, reintentos=1)
            if r.status == 200:
                rp.parse(r.texto.splitlines())
            elif 400 <= r.status < 500 and r.status != 429:
                rp.parse([])                       # sin politica publicada
            else:
                rp = None
        except Exception:
            rp = None
        if rp is None:
            metrics.emit("descubridor_robots_inaccesible_total", 1, host=host)
        self._robots[host] = rp
        return rp

    def permitido(self, url: str) -> bool:
        rp = self.robots(urlparse(url).hostname or "")
        return rp is not None and rp.can_fetch(UA, url)

    def get(self, url: str, *, seguir_redirecciones: bool = True,
            crawl_delay_s: float = 0.0) -> Optional[Respuesta]:
        """El GET si robots.txt lo permite; None si no. Respeta el mayor de los delays."""
        host = urlparse(url).hostname or ""
        if not self.permitido(url):
            return None
        rp = self._robots.get(host)
        delay = max(crawl_delay_s, float((rp.crawl_delay(UA) if rp else None) or 0))
        return self._pedir(url, seguir_redirecciones, minimo_host=delay)


# ─────────────────────────────────────────────────────────────────────────────
# Validacion contra la API de la plataforma
# ─────────────────────────────────────────────────────────────────────────────

# De donde sale el nombre que reporta el board: ("item", clave) = mayoria entre los
# avisos; ("raiz", clave) = campo del objeto raiz. Verificado el 2026-10-04: Greenhouse
# `company_name` ('GitLab'), Recruitee `company_name` ('Channable'), Workable `name`
# ('Hugging Face'), Personio `subcompany` ('JTL-Software GmbH'). Lever, Ashby y BambooHR
# no reportan nombre en el endpoint: se valida el token.
NOMBRE_DEL_BOARD = {
    "greenhouse": ("item", "company_name"),
    "recruitee": ("item", "company_name"),
    "personio": ("item", "subcompany"),
    "workable": ("raiz", "name"),
}


@dataclass
class Validacion:
    avisos: int = 0
    nombre: str = ""
    nombre_desde: str = ""           # "board" o "token"
    error: str = ""


def validar_token(red: Red, plataforma: str, token: str) -> Validacion:
    """Pide el listado por el MISMO endpoint que usa el scraper y cuenta avisos."""
    v = Validacion()
    plat = ats_registry.PLATAFORMAS[plataforma]
    url = plat.url_template.replace("{token}", token)
    try:
        r = red.get(url, seguir_redirecciones=False, crawl_delay_s=plat.crawl_delay_s)
    except Exception as exc:
        v.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        return v
    if r is None:
        v.error = "robots.txt de la API lo prohibe"
        return v
    if r.status != 200:
        destino = r.headers.get("Location")
        v.error = f"HTTP {r.status}" + (f" -> {destino}" if destino else "")
        return v
    try:
        if plat.response_format == "xml":
            filas = es.items_de_xml(r.cuerpo, plat.xml_item)
            raiz: dict = {}
        else:
            raiz = json.loads(r.texto)
            filas = es.items_de_json(r.cuerpo, plat.items_path)
    except Exception as exc:
        v.error = f"parseo {plat.response_format}: {type(exc).__name__}"
        return v

    v.avisos = len(filas)
    origen = NOMBRE_DEL_BOARD.get(plataforma)
    if origen and origen[0] == "raiz" and isinstance(raiz, dict):
        v.nombre = es._texto(raiz.get(origen[1]))
    elif origen and origen[0] == "item":
        nombres = Counter(es._texto(f.get(origen[1])) for f in filas if f.get(origen[1]))
        v.nombre = nombres.most_common(1)[0][0] if nombres else ""
    if v.nombre:
        v.nombre_desde = "board"
    else:
        v.nombre, v.nombre_desde = token, "token"
    return v


# ─────────────────────────────────────────────────────────────────────────────
# Un dominio de punta a punta
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Hallazgo:
    dominio: str
    plataforma: str = ""
    token: str = ""
    estado: str = ""                 # "propuesta" o "rechazada"
    motivo: str = ""
    avisos: int = 0
    nombre_board: str = ""
    nombre_desde: str = ""
    pagina: str = ""
    linea_registro: str = ""


def _ya_registrados() -> set[tuple[str, str]]:
    return {(f.plataforma, f.token.lower()) for f in ats_registry.FUENTES}


def _linea_registro(plataforma: str, token: str, empresa: str, v: Validacion,
                    hoy: str) -> str:
    nota = (f"propuesto por descubrir_ats {hoy}: {v.avisos} avisos, "
            f"nombre '{v.nombre}' ({v.nombre_desde})")
    if plataforma in ats_registry.PENDIENTES_D1:
        nota = "D1 pendiente; " + nota
    # json.dumps: un literal de string valido en Python aunque el nombre traiga comillas.
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731
    return f"FuenteATS({q(plataforma)}, {q(token)}, {q(empresa)}, False, {q(nota)}),"


def _rechazo(h: Hallazgo, motivo: str) -> Hallazgo:
    h.estado, h.motivo = "rechazada", motivo
    metrics.emit("descubridor_rechazo_total", 1, motivo=motivo.split(":")[0],
                 dominio=h.dominio, plataforma=h.plataforma or "-")
    return h


def descubrir_dominio(red: Red, dominio: str, *, hoy: Optional[str] = None) -> list[Hallazgo]:
    """
    Todos los hallazgos de un dominio: uno por token detectado, o uno solo con el motivo
    por el que no hubo token. Nunca lanza: un dominio roto no corta la lista.
    """
    hoy = hoy or date.today().isoformat()
    dominio = dominio.strip().lower()
    dominio = (urlparse(dominio).hostname if "//" in dominio else dominio).removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", dominio or ""):
        return [_rechazo(Hallazgo(dominio=dominio), "dominio_invalido")]

    tokens: list[tuple[str, str]] = []
    pagina = ""
    leidas = bloqueadas = saltos = 0
    vistas: set[str] = set()
    pendientes = [p.format(d=dominio) for p in RUTAS_CAREERS]
    segundo_salto: list[str] = []
    while pendientes or (segundo_salto and saltos < MAX_SEGUNDO_SALTO):
        if pendientes:
            url = pendientes.pop(0)
        else:
            url, saltos = segundo_salto.pop(0), saltos + 1
        if _sin_barra(url) in vistas:
            continue                       # otra ruta ya redirigio aca
        vistas.add(_sin_barra(url))
        try:
            r = red.get(url)
        except Exception:
            continue                       # sin red a mitad del pedido: la siguiente
        if r is None:
            # robots.txt prohibe, o el host no responde (careers.{d} que no existe):
            # solo lo primero cuenta como bloqueo.
            if red.robots(urlparse(url).hostname or "") is not None:
                bloqueadas += 1
            continue
        if r.status != 200:
            continue
        leidas += 1
        if _sin_barra(r.url) != _sin_barra(url) and _sin_barra(r.url) in vistas:
            continue                       # ya leida por otra ruta
        vistas.add(_sin_barra(r.url))
        # La URL final cuenta: /careers suele redirigir directo al tablero del ATS.
        tokens = detectar_tokens(r.url + "\n" + r.texto)
        if tokens:
            pagina = r.url
            break
        for enlace in enlaces_de_careers(r.texto, r.url, dominio):
            if enlace not in segundo_salto and _sin_barra(enlace) not in vistas:
                segundo_salto.append(enlace)

    if not tokens:
        if leidas:
            motivo = "sin_token"
        elif bloqueadas:
            motivo = "robots_bloquea"
        else:
            motivo = "sin_pagina"
        return [_rechazo(Hallazgo(dominio=dominio), motivo)]

    registrados = _ya_registrados()
    salida: list[Hallazgo] = []
    for plataforma, token in tokens:
        h = Hallazgo(dominio=dominio, plataforma=plataforma, token=token, pagina=pagina)
        if not FORMATO_TOKEN.fullmatch(token):
            salida.append(_rechazo(h, "token_formato: fuera de [a-z0-9_-], revisar a mano"))
            continue
        if (plataforma, token.lower()) in registrados:
            h.estado, h.motivo = "rechazada", "ya_registrado"
            salida.append(h)
            continue
        v = validar_token(red, plataforma, token)
        h.avisos, h.nombre_board, h.nombre_desde = v.avisos, v.nombre, v.nombre_desde
        if v.error:
            salida.append(_rechazo(h, f"error_validacion: {v.error}"))
        elif v.avisos == 0:
            salida.append(_rechazo(h, "sin_avisos: la cuenta existe y no publica"))
        elif not coincide(v.nombre, dominio):
            salida.append(_rechazo(
                h, f"nombre_no_coincide: el board dice '{v.nombre}' ({v.nombre_desde})"))
        else:
            empresa = v.nombre if v.nombre_desde == "board" else \
                etiqueta_de_dominio(dominio).replace("-", " ").title()
            h.estado = "propuesta"
            h.linea_registro = _linea_registro(plataforma, token, empresa, v, hoy)
            metrics.emit("descubridor_propuesta_total", 1, plataforma=plataforma,
                         dominio=dominio)
            salida.append(h)
    return salida


def descubrir(dominios: list[str], red: Optional[Red] = None,
              hoy: Optional[str] = None) -> list[Hallazgo]:
    red = red or Red()
    salida: list[Hallazgo] = []
    for d in dominios:
        if d.strip() and not d.strip().startswith("#"):
            salida.extend(descubrir_dominio(red, d, hoy=hoy))
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _tabla(hallazgos: list[Hallazgo]) -> str:
    lineas = []
    for h in hallazgos:
        donde = f"{h.plataforma}:{h.token}" if h.token else "-"
        avisos = f"{h.avisos} avisos" if h.token else ""
        lineas.append(f"{h.estado.upper():10} {h.dominio:28} {donde:34} {avisos:11} {h.motivo}")
    propuestas = [h.linea_registro for h in hallazgos if h.estado == "propuesta"]
    if propuestas:
        lineas.append("")
        lineas.append("# Para ats_registry.FUENTES (apagadas; activar es decision de D1):")
        lineas.extend("    " + p for p in propuestas)
    return "\n".join(lineas)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dominios", nargs="*", help="dominios de empresa (gorillalogic.com)")
    ap.add_argument("--archivo", help="un dominio por linea; '#' comenta")
    ap.add_argument("--json", action="store_true", help="salida JSON por stdout")
    args = ap.parse_args(argv)

    dominios = list(args.dominios)
    if args.archivo:
        dominios += pathlib.Path(args.archivo).read_text(encoding="utf-8").splitlines()
    if not [d for d in dominios if d.strip() and not d.strip().startswith("#")]:
        ap.error("ningun dominio")

    hallazgos = descubrir(dominios)
    if args.json:
        print(json.dumps([asdict(h) for h in hallazgos], ensure_ascii=False, indent=1))
    else:
        print(_tabla(hallazgos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
