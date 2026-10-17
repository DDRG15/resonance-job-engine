"""
ats_registry.py — los ATS como clase de fuente, no como un board más.

Un board es una URL con un `{title}` interpolado. Un ATS no: `boards-api.greenhouse.io`
sirve el tablero completo de **una empresa** por token, y el mismo patrón se repite por
cada empresa que usa esa plataforma. Meterlos en `selectors_registry.py` terminaría con
trescientas entradas que solo se diferencian en un string, y ninguna de ellas con
selectores porque no hacen falta.

De ahí la separación: tres plataformas con su plantilla y su mapeo de campos, y una
lista de empresas donde agregar una es una línea.

**Todo lo de acá está verificado contra el endpoint real el 2026-09-15**, tokens y
nombres de campo incluidos. Un token sin verificar es una llamada fallida por corrida:
eso ya pasó con Cerebras, que el verificador daba por bueno y devolvía 402.

Recruitee, Workable, BambooHR y Personio están
verificadas igual —pedido real, robots.txt leído, respuesta del token inexistente
medida— y con todas sus empresas apagadas hasta que se decida activarlas. Las empresas nuevas
se proponen con `tools/descubrir_ats.py`, que nunca escribe este archivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Plataforma:
    nombre: str
    url_template: str
    response_format: str = "json"
    items_path: str = ""
    field_map: dict = field(default_factory=dict)
    crawl_delay_s: float = 0.0
    salario_en_descripcion: str = ""
    """Campo del payload que trae la descripcion en HTML, cuando la plataforma NO
    publica compensacion como dato. Vacio = apagado. Es un flag con nombre, no una
    regla del `field_map`: el mapa mapea, el codigo transforma."""
    concurrencia: int = 6
    """Cuántos tokens de esta plataforma se piden a la vez. No es el semáforo de
    Chromium: ahí el recurso escaso son las pestañas, acá es la cortesía con el host."""
    url_aviso_template: str = ""
    """Fase 8: plantilla de la URL del aviso para las plataformas cuyo listado no la
    trae (BambooHR, Personio). `{token}` lo pone `ats_scraper`; el resto de los
    marcadores (`{id}`) salen de la fila. Vacio = la URL viene en el payload."""
    xml_item: str = ""
    """Fase 8: la etiqueta de cada aviso cuando `response_format="xml"` (Personio:
    `position`)."""
    seguir_redirecciones: bool = True
    """Fase 8: False en las plataformas que contestan una empresa inexistente con una
    redireccion a su home comercial (BambooHR 302, Personio 307). Sin seguirla, el error
    nombra el codigo y el destino en vez de un "parseo json" sobre HTML."""


PLATAFORMAS: dict[str, Plataforma] = {

    # Campos verificados contra gitlab: company_name trae el nombre real ('GitLab'),
    # location es un dict con .name ('Remote, Bangalore'), y application_deadline
    # existe como clave — es la fuente del `deadline` de la Fase 6.
    # robots.txt de boards-api.greenhouse.io: solo prohíbe /embed/.
    # `?content=true` (2026-09-28): Greenhouse NO expone compensacion como campo. Se
    # verificaron las tres vias contra gitlab, cloudflare y canonical y ninguna trae una
    # sola clave de pago -- ni el listado pelado, ni `?content=true`, ni `/jobs/{id}`.
    # Lo que si trae `content` es la descripcion, y ahi esta el rango: las leyes de
    # transparencia salarial de California, Colorado, Nueva York y Washington obligan a
    # publicarlo. Medido sobre 905 avisos: gitlab 84/199 (42,2%), cloudflare 157/400
    # (39,2%), canonical 1/306 -- canonical es britanica y esas leyes no la alcanzan.
    # El salario estaba en 0% para Greenhouse, que fue 452 de los 1.095 avisos de la
    # corrida del 17-09.
    #
    # El flag cuesta UNA llamada, no una por aviso: `?content=true` trae la descripcion
    # de todos los avisos en la misma respuesta. `/jobs/{id}` habria sido una llamada
    # por aviso para el mismo dato.
    "greenhouse": Plataforma(
        nombre="greenhouse",
        url_template="https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
        items_path="jobs",
        field_map={
            "title": "title",
            "company": "company_name",
            "url": "absolute_url",
            "location": "location.name",
            # Viene como clave en cada aviso. No siempre tiene valor -- depende de si
            # la empresa la cargo -- pero cuando lo tiene es una fecha real.
            "deadline": "application_deadline",
            # Fase 7: la misma descripcion de la que sale el salario (doble escapada).
            "description": "content",
        },
        salario_en_descripcion="content",
    ),

    # Lever no devuelve el nombre de la empresa: el tablero ES de una empresa, así que
    # para el payload es contexto y no dato. Va por `defaults` desde la fuente.
    # El título vive en `text`, no en `title`.
    # robots.txt de api.lever.co: `Allow: /` con **Crawl-delay: 1** declarado. Es un
    # límite, no una sugerencia — de ahí concurrencia 1 y el delay.
    "lever": Plataforma(
        nombre="lever",
        url_template="https://api.lever.co/v0/postings/{token}?mode=json",
        field_map={
            "title": "text",
            "url": "hostedUrl",
            "location": "categories.location",
            # Fase 7, verificado contra spotify el 2026-10-03: la intro va en
            # `description`, los requisitos en `lists` (secciones `{text, content}`:
            # "What You'll Do", "Who You Are") y el cierre en `additional`. Sin `lists` la
            # descripcion pierde justo la parte que lee el match por requisitos.
            "description": ("description", "lists", "additional"),
        },
        crawl_delay_s=1.0,
        concurrencia=1,
    ),

    # Ashby tampoco trae empresa. `jobUrl` es la oferta; `applyUrl` es el formulario,
    # y usar ese como URL del aviso rompería la deduplicación contra el resto.
    # api.ashbyhq.com devuelve 401 al pedir robots.txt: sin política publicada.
    # `?includeCompensation=true` (calibracion 2026-09-17): sin el flag, la clave
    # `compensation` no viene. Con el flag viene siempre, pero solo tiene contenido si
    # la empresa lo cargo — medido: modal 31 de 34, supabase 1 de 60, posthog y linear
    # 0. Vale igual: el salario estaba en 0% para las tres plataformas ATS, y cuando
    # esta es un rango real ("$220K - $300K") que el parser ya entiende.
    "ashby": Plataforma(
        nombre="ashby",
        url_template=("https://api.ashbyhq.com/posting-api/job-board/{token}"
                      "?includeCompensation=true"),
        items_path="jobs",
        field_map={
            "title": "title",
            "url": "jobUrl",
            "location": "location",
            "salary_raw": "compensation.compensationTierSummary",
            # Fase 7, verificado contra supabase el 2026-10-03. El HTML y no
            # `descriptionPlain`: los titulos de seccion quedan en su propia linea.
            "description": "descriptionHtml",
        },
    ),

    # ── Recruitee, Workable, BambooHR y Personio ─────────────────────────────────────────────
    # Las cuatro verificadas el 2026-10-04 con pedidos reales, un pedido por segundo como
    # mucho (Personio, uno cada 6 s). Ninguna tiene empresas activas: subir el
    # umbral de Tier 1 a 85 sigue pendiente. Ver `PENDIENTES_ACTIVACION`.

    # Recruitee. robots.txt de {t}.recruitee.com redirige al dominio de careers de la
    # empresa (careers.bunq.com: `Allow: /`). Medido: bunq 17 avisos, channable 9,
    # hostaway 9; token inexistente -> 404 `{"error":"Not Found"}`.
    # Es la unica de las cuatro con salario estructurado: `salary{min,max,period,currency}`
    # con montos como string. Channable publica 5 de 9: EUR/month, USD/year, EUR/year
    # (y uno en DKK con 0 y 0, que no se compone). bunq y hostaway, 0 de 26.
    # `close_at` viene como clave en los 35 avisos medidos y nulo en los 35: se mapea
    # crudo (lo normaliza `_a_fecha` al leer) sin haber visto su formato con valor.
    # La descripcion se reparte entre `description` (el puesto) y `requirements` (los
    # requisitos): sin el segundo, el eje de la Fase 7 no tiene que leer.
    "recruitee": Plataforma(
        nombre="recruitee",
        url_template="https://{token}.recruitee.com/api/offers/",
        items_path="offers",
        field_map={
            "title": "title",
            "company": "company_name",
            "url": "careers_url",
            "location": "location",
            "deadline": "close_at",
            "salary_min": "salary.min",
            "salary_max": "salary.max",
            "salary_period": "salary.period",
            "salary_currency": "salary.currency",
            "description": ("description", "requirements"),
        },
        seguir_redirecciones=False,
    ),

    # Workable. robots.txt de apply.workable.com: `Disallow:` vacio, todo permitido.
    # Medido: huggingface 8 avisos; hotjar 200 con `{"jobs": []}` (cuenta vieja: existe y
    # no publica); token inexistente -> 404 "Not Found".
    # `?details=true` agrega `description` a cada aviso EN LA MISMA respuesta (huggingface:
    # 5.683 bytes sin el flag, 51.644 con el, 8 de 8 descripciones de 1.024 a 7.073
    # caracteres). El anexo del 30-09 daba a Workable "sin descripcion": era sin el flag.
    # La empresa viene una vez arriba (`name`), no por aviso: va por `defaults`.
    # Ciudad y pais vienen separados; el pais es lo que lee la politica de ubicacion.
    "workable": Plataforma(
        nombre="workable",
        url_template="https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
        items_path="jobs",
        field_map={
            "title": "title",
            "url": "url",
            "location": ("city", "country"),
            "description": "description",
        },
        seguir_redirecciones=False,
    ),

    # BambooHR. robots.txt de {t}.bamboohr.com: solo prohibe /jobs/embed.php y
    # /jobs/embed2.php. Medido: gorillalogic 20 avisos (18 el 30-09), koombea 2; zapier
    # 200 con `{"meta":{"totalCount":0},"result":[]}`; token inexistente -> 302 a
    # https://www.bamboohr.com.
    # El listado no trae URL ni empresa. La URL se compone desde `id`, verificada:
    # gorillalogic.bamboohr.com/careers/34 -> 200 con `og:url` igual a la compuesta.
    # La descripcion vive en /careers/{id}/detail: un pedido por aviso, fuera de esta fase.
    # La ubicacion estructurada viene casi siempre nula (gorillalogic: 2 de 20 con pais);
    # el titulo la trae ("Remote: Colombia - Costa Rica") y el matcher lee el titulo.
    "bamboohr": Plataforma(
        nombre="bamboohr",
        url_template="https://{token}.bamboohr.com/careers/list",
        items_path="result",
        field_map={
            "title": "jobOpeningName",
            "location": ("atsLocation.city", "atsLocation.country"),
        },
        url_aviso_template="https://{token}.bamboohr.com/careers/{id}",
        seguir_redirecciones=False,
        concurrencia=2,
    ),

    # Personio. robots.txt de {t}.jobs.personio.de: `Allow: /`. XML, no JSON.
    # Medido: jtl-software-gmbh 30 posiciones, governikus 10; token inexistente -> 307 a
    # https://personio.com. El 30-09 el tercer pedido seguido devolvio 429: de ahi 5 s
    # entre pedidos y de a uno. La URL se compone desde `id`, verificada:
    # jtl-software-gmbh.jobs.personio.de/job/2666138 -> 200 con el titulo del aviso.
    # La descripcion son secciones `<jobDescription><name/><value>CDATA</value>`.
    # `subcompany` es la empresa real del aviso: JTL publica avisos de su filial
    # Returnless con `subcompany="Returnless / SendDirect B.V."`.
    "personio": Plataforma(
        nombre="personio",
        url_template="https://{token}.jobs.personio.de/xml",
        response_format="xml",
        xml_item="position",
        field_map={
            "title": "name",
            "company": "subcompany",
            "location": "office",
            "description": "jobDescriptions",
        },
        url_aviso_template="https://{token}.jobs.personio.de/job/{id}",
        seguir_redirecciones=False,
        crawl_delay_s=5.0,
        concurrencia=1,
    ),
}


# Plataformas soportadas y verificadas pero SIN empresas activas, hasta que el usuario decida
# que se suba TIER1_MIN_SCORE a 85 antes de sumar volumen. El test de registro exige que
# ninguna fuente de estas plataformas este activa: activar una es sacar la plataforma de
# este conjunto Y cambiar el False de la fuente, dos cambios a proposito.
PENDIENTES_ACTIVACION: frozenset[str] = frozenset({"recruitee", "workable", "bamboohr", "personio"})


@dataclass(frozen=True)
class FuenteATS:
    plataforma: str
    token: str
    empresa: str
    activa: bool = True
    nota: str = ""


# Verificadas el 2026-09-15: las 40 devolvieron HTTP 200 con avisos.
#
# `activa=False` no es "no funciona": es "funciona y está apagada". Las 40 juntas son
# ~8.700 avisos por corrida contra las 1.536 filas que el registry acumuló en cuatro
# meses. Arrancar con todas encendidas inundaría la base en la primera corrida y haría
# imposible distinguir una fuente nueva que aporta de una que solo hace ruido.
#
# El criterio de las activas: empresas remote-first o remote-friendly con volumen de
# ingeniería, que es lo que el usuario busca. Encender otra es cambiar un False.
FUENTES: list[FuenteATS] = [
    # ── greenhouse ────────────────────────────────────────────────────────────
    FuenteATS("greenhouse", "gitlab",      "GitLab",      True,  "all-remote"),
    FuenteATS("greenhouse", "canonical",   "Canonical",   True,  "all-remote"),
    FuenteATS("greenhouse", "grafanalabs", "Grafana Labs", True, "remote-first"),
    FuenteATS("greenhouse", "elastic",     "Elastic",     True,  "remote-first"),
    FuenteATS("greenhouse", "mongodb",     "MongoDB",     True),
    FuenteATS("greenhouse", "cloudflare",  "Cloudflare",  True),
    FuenteATS("greenhouse", "dropbox",     "Dropbox",     True,  "virtual-first"),
    FuenteATS("greenhouse", "databricks",  "Databricks",  False),
    FuenteATS("greenhouse", "stripe",      "Stripe",      False),
    FuenteATS("greenhouse", "figma",       "Figma",       False),
    FuenteATS("greenhouse", "discord",     "Discord",     False),
    FuenteATS("greenhouse", "reddit",      "Reddit",      False),
    FuenteATS("greenhouse", "coinbase",    "Coinbase",    False, "remote-first"),
    FuenteATS("greenhouse", "twilio",      "Twilio",      False, "remote-first"),
    FuenteATS("greenhouse", "asana",       "Asana",       False),
    FuenteATS("greenhouse", "gusto",       "Gusto",       False),
    FuenteATS("greenhouse", "instacart",   "Instacart",   False),
    FuenteATS("greenhouse", "pinterest",   "Pinterest",   False),
    FuenteATS("greenhouse", "samsara",     "Samsara",     False),
    FuenteATS("greenhouse", "sofi",        "SoFi",        False),
    FuenteATS("greenhouse", "squarespace", "Squarespace", False),
    FuenteATS("greenhouse", "robinhood",   "Robinhood",   False),
    FuenteATS("greenhouse", "airbnb",      "Airbnb",      False),
    FuenteATS("greenhouse", "duolingo",    "Duolingo",    False),

    # ── lever ─────────────────────────────────────────────────────────────────
    FuenteATS("lever", "spotify",    "Spotify",     True),
    FuenteATS("lever", "veeva",      "Veeva",       False, "914 avisos, mayoria no-eng"),
    FuenteATS("lever", "gopuff",     "GoPuff",      False, "788 avisos, mayoria retail"),
    FuenteATS("lever", "matchgroup", "Match Group", False),
    # `leverdemo` responde 200 con 11 avisos y es el tablero de demostracion de Lever.
    # Se deja registrado y apagado: sirve para probar el camino sin gastar cuota de una
    # empresa real, y no tiene sentido que aporte avisos al registry.
    FuenteATS("lever", "leverdemo",  "Lever (demo)", False, "tablero de demo, no real"),

    # ── ashby ─────────────────────────────────────────────────────────────────
    FuenteATS("ashby", "posthog",    "PostHog",    True,  "all-remote"),
    FuenteATS("ashby", "supabase",   "Supabase",   True,  "all-remote"),
    FuenteATS("ashby", "clickhouse", "ClickHouse", True,  "remote-first"),
    FuenteATS("ashby", "linear",     "Linear",     True,  "remote-first"),
    FuenteATS("ashby", "modal",      "Modal",      True),
    FuenteATS("ashby", "vanta",      "Vanta",      False),
    FuenteATS("ashby", "ramp",       "Ramp",       False),
    FuenteATS("ashby", "sardine",    "Sardine",    False),
    FuenteATS("ashby", "openai",     "OpenAI",     False, "811 avisos"),
    FuenteATS("ashby", "cohere",     "Cohere",     False),
    FuenteATS("ashby", "runway",     "Runway",     False),

    # ── Verificadas el 2026-10-04, TODAS apagadas hasta activarlas ────────────
    # El numero es lo que trajo el pedido real de ese dia. Para activar una,
    # sacar su plataforma de `PENDIENTES_ACTIVACION` y cambiar el False de la fuente.
    FuenteATS("recruitee", "bunq",      "bunq",      False, "pendiente de activar; 17 avisos, Amsterdam hibrido"),
    FuenteATS("recruitee", "channable", "Channable", False, "pendiente de activar; 9 avisos, 5 con salario"),
    FuenteATS("recruitee", "hostaway",  "Hostaway",  False, "pendiente de activar; 9 avisos, 9 remotos"),
    FuenteATS("workable",  "azumo",     "Azumo",     False, "pendiente de activar; 57 avisos remotos LATAM"),
    FuenteATS("workable",  "huggingface", "Hugging Face", False, "pendiente de activar; 8 avisos"),
    FuenteATS("bamboohr",  "gorillalogic", "Gorilla Logic", False,
              "pendiente de activar; 20 avisos, Remote: Colombia - Costa Rica"),
    FuenteATS("bamboohr",  "koombea",   "Koombea",   False, "pendiente de activar; 2 avisos, Colombia"),
    FuenteATS("personio",  "jtl-software-gmbh", "JTL-Software", False,
              "pendiente de activar; 30 avisos, mercado aleman"),
    FuenteATS("personio",  "governikus", "Governikus", False, "pendiente de activar; 8 avisos, Bremen"),
]


def activas() -> list[FuenteATS]:
    return [f for f in FUENTES if f.activa]


def plataforma_de(fuente: FuenteATS) -> Plataforma:
    return PLATAFORMAS[fuente.plataforma]


def dominio_de(plataforma: str) -> str:
    """El `source_domain` que lleva el JobResult.

    Es la plataforma, no la empresa: una empresa ya está en `company`, y agrupar las
    estadísticas por empresa daría cuarenta filas de una fila cada una. Lo que importa
    saber es si la integración con Greenhouse trae avisos, no si Dropbox publicó hoy.
    """
    return {
        "greenhouse": "boards.greenhouse.io",
        "lever": "jobs.lever.co",
        "ashby": "jobs.ashbyhq.com",
        "recruitee": "recruitee.com",
        "workable": "apply.workable.com",
        "bamboohr": "bamboohr.com",
        "personio": "jobs.personio.de",
    }.get(plataforma, plataforma)


def dominios() -> list[str]:
    """Los pseudo-dominios de las plataformas que tienen al menos una fuente activa."""
    return sorted({dominio_de(f.plataforma) for f in activas()})
