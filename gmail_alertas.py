"""
gmail_alertas.py — avisos de LinkedIn e Indeed leidos desde las alertas en Gmail.

Fase 11 del . LinkedIn prohibe `/jobs-guest/` en su robots.txt e Indeed
bloquea el scraping; las alertas por correo son la unica via legitima a las dos. GEMA no
toca ningun sitio: lee el buzon de el usuario con el permiso minimo (`gmail.readonly`), parsea
titulo, empresa, ubicacion y enlace de cada alerta, y los avisos entran por el mismo
embudo que cualquier otra fuente (dedup, registry, matcher).

APAGADA por defecto (`GEMA_GMAIL_ALERTAS`). Y el parser se escribio contra fixtures
SINTETICAS (`tests/fixtures/gmail/`), no contra alertas reales: el formato de los correos
de LinkedIn e Indeed no esta verificado. Antes de prenderla hay que correr
`tools/autorizar_gmail.py --probar`, que lee las alertas reales y muestra lo que el parser
saca de cada una SIN escribir nada.

Por eso el parser no depende de clases CSS ni de la estructura exacta de la tabla: se
ancla en lo unico estable de un correo de alerta, el enlace al aviso
(`/jobs/view/<id>` en LinkedIn, `jk=<id>` en Indeed), y lee el texto de la tarjeta que
contiene ese enlace y ningun otro.

Dos mitades:
    parsear_alerta(proveedor, html)   pura, sin red — lo que testean las fixtures
    traer_alertas(lector=...)         el lector de Gmail entra inyectado; el real importa
                                      las librerias de Google recien al usarse
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

import config
from models import JobResult

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

LINKEDIN = "linkedin"
INDEED = "indeed"
PROVEEDORES = (LINKEDIN, INDEED)

#: `source_domain` de los avisos. El de la traza de fuentes es `gmail:<proveedor>`: la
#: unidad que se rompe es la alerta, no el sitio.
DOMINIO = {LINKEDIN: "linkedin.com", INDEED: "indeed.com"}


class GmailNoConfigurado(RuntimeError):
    """Falta el token, falta la libreria o el token no sirve. No es un fallo de red."""


# =============================================================================
# Enlaces: el ancla de cada aviso
# =============================================================================

_LINKEDIN_VIEW = re.compile(r"^/(?:comm/)?jobs/view/(\d{6,})(?:/|$)", re.I)
_INDEED_JK = re.compile(r"^[0-9a-f]{12,20}$", re.I)
# Indeed tiene un sitio por pais con TLD propio: co.indeed.com, indeed.co.uk, indeed.de.
_INDEED_HOST = re.compile(r"^(?:[\w-]+\.)*indeed\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$")


def _es_host_de(host: str, dominio: str) -> bool:
    return host == dominio or host.endswith("." + dominio)


def id_de_enlace(proveedor: str, href: str) -> Optional[str]:
    """
    El id del aviso dentro de un enlace de la alerta, o None si no apunta a un aviso.

    El host se compara entero, no por substring: `evil-indeed.com` o un redirector con
    `linkedin.com/jobs/view/...` en el query pasaban con un `in` o un `search` suelto
    (hallado por los tests de la Fase 11, 07-10).
    """
    partes = urlsplit((href or "").strip())
    host = (partes.hostname or "").lower()
    if proveedor == LINKEDIN:
        if not _es_host_de(host, "linkedin.com"):
            return None
        m = _LINKEDIN_VIEW.match(partes.path)
        return m.group(1) if m else None
    if proveedor == INDEED:
        if not _INDEED_HOST.match(host):
            return None
        jk = (parse_qs(partes.query).get("jk") or [""])[0]
        return jk.lower() if _INDEED_JK.match(jk) else None
    return None


def url_canonica(proveedor: str, href: str, id_aviso: str) -> str:
    """
    La URL del aviso sin el rastreo del correo.

    Sin esto, el mismo aviso llega con un `trackingId` distinto en cada alerta, su hash
    cambia, y `is_seen` lo deja entrar otra vez en cada corrida. Indeed conserva el host
    (`co.indeed.com` no es `www.indeed.com`: el aviso vive en el sitio del pais).
    """
    if proveedor == LINKEDIN:
        return f"https://www.linkedin.com/jobs/view/{id_aviso}"
    host = (urlsplit(href).hostname or "").lower() or "www.indeed.com"
    return f"https://{host}/viewjob?jk={id_aviso}"


# =============================================================================
# Texto de la tarjeta, linea por linea
# =============================================================================

_BLOQUE = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
           "tr", "td", "th", "table", "tbody", "thead", "section", "article",
           "header", "footer", "blockquote", "hr", "center"}
_SIN_TEXTO = {"style", "script", "head", "title"}


_MARCA = "\x00"


def _lineas(el, marca: Optional[Callable] = None) -> list[str]:
    """
    El texto de un elemento, una linea por bloque. Incluye `td`: los correos se maquetan
    con tablas, y `endpoint_scraper.texto_con_lineas` trata `td` como inline — ahi el
    titulo y la empresa de una tarjeta salian pegados en la misma linea.

    Con `marca`, cada enlace a un aviso arranca su propia linea con `_MARCA<id>_MARCA`
    delante: es lo que usa `_segmento` para saber donde empieza el aviso siguiente.
    """
    partes: list[str] = []

    def _walk(e, raiz: bool = False) -> None:
        tag = e.tag.lower() if isinstance(e.tag, str) else ""
        if tag in _SIN_TEXTO:
            if e.tail and not raiz:
                partes.append(e.tail)
            return
        bloque = tag in _BLOQUE
        if bloque:
            partes.append("\n")
        if marca is not None and tag == "a":
            id_aviso = marca(e)
            if id_aviso:
                partes.append(f"\n{_MARCA}{id_aviso}{_MARCA}")
        if tag and e.text:
            partes.append(e.text)
        for hijo in e:
            _walk(hijo)
        if bloque:
            partes.append("\n")
        if e.tail and not raiz:
            partes.append(e.tail)

    _walk(el, raiz=True)
    salida = []
    for linea in "".join(partes).split("\n"):
        linea = re.sub(r"\s+", " ", linea.replace("\xa0", " ")).strip()
        if linea:
            salida.append(linea)
    return salida


# Lineas de la tarjeta que no son empresa ni ubicacion. En ingles y castellano: el usuario
# recibe las alertas en los dos idiomas segun la cuenta.
_RUIDO = re.compile(
    r"^(?:easy apply|easily apply|solicitud sencilla|postulaci[oó]n sencilla|"
    r"actively recruiting|contratando activamente|contrataci[oó]n activa|"
    r"promoted|promocionad[oa]|new|nuevo|nueva|just posted|reci[eé]n publicad[oa]|"
    r"view job|ver empleo|ver oferta|apply now|postular|see all jobs|ver todos los empleos|"
    r"responsive employer|empleador receptivo|urgently hiring|contrataci[oó]n urgente|"
    r"be an early applicant|s[eé] de los primeros.*|top applicant|"
    r"\d+ (?:connections?|conexi[oó]n(?:es)?|school alumn[ia]?|alumn[oa]s?).*|"
    r".*\b(?:applicants?|solicitudes|postulantes)\b.*|"
    r"(?:posted |publicado )?(?:hace|\d+) ?(?:\d+ )?(?:d[ií]as?|horas?|semanas?|days?|hours?|weeks?)(?: ago)?|"
    r"\d(?:[.,]\d)?(?: ?★)?)$",
    re.I,
)
_SALARIO = re.compile(r"(?:\$|US\$|USD|S/\.?|€|EUR|£)\s?\d|\d[\d.,]*\s?(?:k|K)\b.*(?:year|año|yr|mes|month|hour|hora)",
                      re.I)


def _es_ruido(linea: str) -> bool:
    return bool(_RUIDO.match(linea)) or len(linea) > 160


@dataclass
class AvisoCrudo:
    """Lo que el parser saco de una tarjeta, antes de ser JobResult."""
    proveedor: str
    id_aviso: str
    url: str
    titulo: str
    empresa: str = ""
    ubicacion: str = ""
    salario: str = ""


def _tarjeta(ancla, ids_de, titulo: str):
    """
    El ancestro mas grande del enlace que contiene enlaces de ESTE aviso y de ningun otro.

    Con una alerta de un solo aviso, «ningun otro aviso» se cumple hasta <body>, y la
    tarjeta seria el correo entero. Dos frenos:

    - texto ANTES del titulo: en LinkedIn e Indeed el titulo encabeza la tarjeta, asi que
      un padre que trae texto previo ya incluye el encabezado del correo («Tu alerta para
      backend engineer»), que si no saldria como empresa;
    - tope de largo, para el pie cuando no hay encabezado.
    """
    propio = ids_de(ancla)
    actual = ancla
    while True:
        padre = actual.getparent()
        if padre is None:
            return actual
        ids = {i for a in padre.iter("a") for i in [ids_de(a)] if i}
        if ids - {propio}:
            return actual
        lineas = _lineas(padre)
        if len(" ".join(lineas)) > 700:
            return actual
        previas = lineas[:lineas.index(titulo)] if titulo in lineas else lineas
        if any(not _es_ruido(ln) for ln in previas):
            return actual
        actual = padre


def _segmento(doc, ids_de, id_aviso: str, titulo: str) -> list[str]:
    """
    Respaldo de `_tarjeta`: las lineas que siguen al titulo en el documento, hasta el
    primer enlace de OTRO aviso.

    Hace falta cuando titulo y empresa son filas hermanas de una tabla compartida por
    varios avisos: ahi la tarjeta «solo de este aviso» es la fila del titulo y nada mas.
    """
    salida: list[str] = []
    dentro = False
    for ln in _lineas(doc, ids_de):
        if ln.startswith(_MARCA):
            otro, _, texto = ln[1:].partition(_MARCA)
            if otro != id_aviso:
                if dentro:
                    break
                continue
            if texto.strip() == titulo:
                dentro = True
            continue
        if dentro:
            salida.append(ln)
    return salida


def parsear_alerta(proveedor: str, html: str) -> list[AvisoCrudo]:
    """
    Los avisos de UNA alerta. Puro: sin red, sin base, sin config.

    Un aviso sin titulo legible se descarta aca (no hay con que puntuarlo). Uno sin
    empresa NO: entra con empresa vacia y `a_job_result` le pone la marca que usa el
    resto de GEMA, para que el humano lo vea y no se pierda un aviso real.
    """
    if proveedor not in PROVEEDORES or not (html or "").strip():
        return []
    import lxml.html

    try:
        doc = lxml.html.fromstring(html)
    except Exception as exc:          # HTML ilegible: la alerta cuenta como cero, no explota
        logger.warning("[GMAIL] alerta %s ilegible: %s", proveedor, exc)
        return []

    def _ids_de(a) -> Optional[str]:
        return id_de_enlace(proveedor, a.get("href") or "")

    por_id: dict[str, list] = {}
    for a in doc.iter("a"):
        i = _ids_de(a)
        if i:
            por_id.setdefault(i, []).append(a)

    avisos: list[AvisoCrudo] = []
    for id_aviso, anclas in por_id.items():
        textos = [" ".join(_lineas(a)) for a in anclas]
        candidatos = [t for t in textos if t and not _es_ruido(t)]
        if not candidatos:
            continue
        titulo = max(candidatos, key=len)
        ancla = anclas[textos.index(titulo)]
        tarjeta = _tarjeta(ancla, _ids_de, titulo)

        resto = [ln for ln in _lineas(tarjeta) if ln != titulo and not _es_ruido(ln)]
        if not resto:
            resto = [ln for ln in _segmento(doc, _ids_de, id_aviso, titulo)
                     if not _es_ruido(ln)]
        salario = next((ln for ln in resto if _SALARIO.search(ln)), "")
        resto = [ln for ln in resto if ln != salario]

        empresa = ubicacion = ""
        if resto and " · " in resto[0]:
            # LinkedIn: «Empresa · Ciudad, Pais (Remoto)» en una sola linea.
            empresa, _, ubicacion = (p.strip() for p in resto[0].partition(" · "))
        else:
            if resto:
                empresa = resto[0]
            if len(resto) > 1:
                ubicacion = resto[1]

        avisos.append(AvisoCrudo(
            proveedor=proveedor, id_aviso=id_aviso,
            url=url_canonica(proveedor, ancla.get("href") or "", id_aviso),
            titulo=titulo, empresa=empresa, ubicacion=ubicacion, salario=salario,
        ))
    return avisos


def a_job_result(aviso: AvisoCrudo) -> JobResult:
    return JobResult(
        title=aviso.titulo,
        company=aviso.empresa or "(empresa no listada)",
        url=aviso.url,
        location_raw=aviso.ubicacion or None,
        salary_raw=aviso.salario or None,
        source_domain=DOMINIO[aviso.proveedor],
    )


def proveedor_de_remitente(remitente: str) -> Optional[str]:
    """
    `"LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"` -> "linkedin".

    El dominio sale de la DIRECCION, no del primer `@` del header: el nombre visible lo
    escribe el que manda, y `"jobalerts@linkedin.com" <x@evil.com>` no es LinkedIn.
    La consulta a Gmail ya filtra por remitente; esto es la segunda llave.
    """
    from email.utils import parseaddr
    _, direccion = parseaddr(remitente or "")
    dominio = direccion.rpartition("@")[2].lower()
    if _es_host_de(dominio, "linkedin.com"):
        return LINKEDIN
    if dominio == "indeed.com" or dominio.endswith(".indeed.com"):
        return INDEED
    return None


# =============================================================================
# El lector de Gmail
# =============================================================================

@dataclass
class Mensaje:
    id: str
    remitente: str
    html: str


def consulta_gmail(remitentes: Iterable[str], dias: int) -> str:
    return f"from:({' OR '.join(remitentes)}) newer_than:{max(1, int(dias))}d"


def _html_de_payload(payload: dict) -> str:
    """El primer `text/html` de un mensaje de la API, decodificado (base64url)."""
    pendientes = [payload or {}]
    while pendientes:
        parte = pendientes.pop(0)
        if parte.get("mimeType") == "text/html":
            data = (parte.get("body") or {}).get("data") or ""
            if data:
                crudo = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                return crudo.decode("utf-8", errors="replace")
        pendientes.extend(parte.get("parts") or [])
    return ""


def _remitente_de(payload: dict) -> str:
    for h in (payload or {}).get("headers") or []:
        if (h.get("name") or "").lower() == "from":
            return h.get("value") or ""
    return ""


def _guardar_token(path: str, contenido: str) -> None:
    """Escritura atomica: un token a medio escribir es una autorizacion perdida."""
    carpeta = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=carpeta, prefix="gmail_token_", suffix=".tmp.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contenido)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def credenciales(token_path: Optional[str] = None):
    """
    Las credenciales del token guardado, renovadas si vencieron.

    Rechaza un token con permisos de mas: el plan (R7) pide `gmail.readonly` y nada mas,
    y un token con `gmail.modify` filtrado puede borrar el correo de el usuario.
    """
    path = token_path or config.GMAIL_TOKEN_PATH
    if not os.path.isfile(path):
        raise GmailNoConfigurado(
            f"no hay token en {path}: correr `python tools/autorizar_gmail.py`")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GmailNoConfigurado(f"falta la libreria de Google: {exc}") from exc

    with open(path, encoding="utf-8") as f:
        info = json.load(f)
    otorgados = set(info.get("scopes") or [])
    if otorgados and otorgados != set(SCOPES):
        raise GmailNoConfigurado(
            f"el token tiene permisos distintos de gmail.readonly ({sorted(otorgados)}): "
            f"revocarlo y autorizar de nuevo")
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _guardar_token(path, creds.to_json())
        else:
            raise GmailNoConfigurado("token invalido y sin refresh_token: autorizar de nuevo")
    return creds


class LectorGmail:
    """Lee mensajes con la API de Gmail. Solo lectura; nunca marca ni mueve nada."""

    def __init__(self, token_path: Optional[str] = None):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailNoConfigurado(f"falta google-api-python-client: {exc}") from exc
        self._svc = build("gmail", "v1", credentials=credenciales(token_path),
                          cache_discovery=False)

    def mensajes(self, consulta: str, maximo: int) -> list[Mensaje]:
        msgs = self._svc.users().messages()
        ids: list[str] = []
        token = None
        while len(ids) < maximo:
            r = msgs.list(userId="me", q=consulta, pageToken=token,
                          maxResults=min(100, maximo - len(ids))).execute()
            ids.extend(m["id"] for m in r.get("messages") or [])
            token = r.get("nextPageToken")
            if not token:
                break
        salida = []
        for mid in ids[:maximo]:
            m = msgs.get(userId="me", id=mid, format="full").execute()
            payload = m.get("payload") or {}
            salida.append(Mensaje(mid, _remitente_de(payload), _html_de_payload(payload)))
        return salida


# =============================================================================
# La corrida
# =============================================================================

@dataclass
class ResultadoGmail:
    """Por proveedor. Mismos nombres que ResultadoEndpoint: fuentes_estado lo lee igual."""
    proveedor: str
    jobs: list[JobResult] = field(default_factory=list)
    items_crudos: int = 0
    descartados_irrelevantes: int = 0
    descartados_invalidos: int = 0
    mensajes: int = 0
    error: str = ""
    ms: float = 0.0

    @property
    def etiqueta(self) -> str:
        return f"gmail:{self.proveedor}"


def traer_alertas(
    *,
    lector=None,
    es_relevante: Optional[Callable[[str], bool]] = None,
    dias: Optional[int] = None,
    maximo: Optional[int] = None,
    remitentes: Optional[list[str]] = None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[ResultadoGmail]:
    """
    Las alertas de los ultimos `dias`, un resultado por proveedor. Sincronica: la llama
    un executor.

    Nunca levanta. Sin token o sin libreria, los dos proveedores vuelven con `error` y
    cero avisos: la corrida sigue y el error queda en el resumen, que es donde el usuario lo
    ve. Un aviso repetido entre alertas (LinkedIn manda el mismo en dos alertas
    distintas) entra una vez.
    """
    t0 = time.monotonic()
    resultados = {p: ResultadoGmail(p) for p in PROVEEDORES}
    try:
        if lector is None:
            lector = LectorGmail()
        mensajes = lector.mensajes(
            consulta_gmail(remitentes or config.GMAIL_ALERTAS_REMITENTES,
                           config.GMAIL_ALERTAS_DIAS if dias is None else dias),
            config.GMAIL_ALERTAS_MAX_MENSAJES if maximo is None else maximo,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
        log(f"[GMAIL] no se leyeron las alertas — {error}")
        for r in resultados.values():
            r.error = error
            r.ms = (time.monotonic() - t0) * 1000
        return list(resultados.values())

    vistos: set[tuple[str, str]] = set()
    for m in mensajes:
        proveedor = proveedor_de_remitente(m.remitente)
        if proveedor is None:
            continue
        r = resultados[proveedor]
        r.mensajes += 1
        for aviso in parsear_alerta(proveedor, m.html):
            r.items_crudos += 1
            if (proveedor, aviso.id_aviso) in vistos:
                continue
            vistos.add((proveedor, aviso.id_aviso))
            if es_relevante is not None and not es_relevante(aviso.titulo):
                r.descartados_irrelevantes += 1
                continue
            try:
                r.jobs.append(a_job_result(aviso))
            except Exception:
                r.descartados_invalidos += 1

    ms = (time.monotonic() - t0) * 1000
    for r in resultados.values():
        r.ms = ms
        log(f"[GMAIL] {r.proveedor} — {len(r.jobs)} avisos de {r.items_crudos} items "
            f"en {r.mensajes} alerta(s)")
    return list(resultados.values())
