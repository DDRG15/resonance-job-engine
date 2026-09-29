"""
selectors_registry.py — GEMA Domain Selector Registry (The Blitz Edition)

ARCHITECTURAL ROLE:
    The single source of truth for all DOM selectors and URL templates.
    When a job board changes its SRP layout (Selector Drift), update ONE
    entry here. scraper.py logic stays untouched.

SURFACE SNIPING DESIGN:
    Each domain now has a `search_url_template` — a Python format string
    that produces the direct SRP URL from a title query. The scraper jumps
    straight to the results page. No landing page interaction, no clicks.

MULTI-SELECTOR FALLBACK CHAINS:
    Every selector field that was a single string is now a list[str].
    The scraper tries each selector in order and returns the first non-null
    result. This provides automatic resilience to minor DOM changes without
    requiring God Mode intervention for every A/B test the job board runs.

    Fallback priority (enforced in list order):
        1. data-* attributes  — most stable, intentionally added for automation
        2. ARIA attributes     — semantic, unlikely to change
        3. Semantic HTML       — h2/h3 survive CSS refactors
        4. CSS classes         — last resort, changes with design systems

LAST VERIFIED:
    Each entry records the date its selectors were last confirmed against
    the live site. The Streamlit sidebar surfaces a warning when this date
    is > 30 days old, prompting the user to verify or use God Mode.

NULL RATE THRESHOLD:
    If more than `null_rate_alert_threshold` of SRP card extractions return
    null in a single run, the circuit breaker trips and emits a structured
    warning directing the user to God Mode.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DomainSelectors:
    """
    Complete scraping contract for one job board domain.
    All list fields use fallback chain semantics: try index 0 first,
    fall through to subsequent entries on null/exception.
    """
    domain:                     str

    # URL template for direct SRP access — {title} is URL-encoded by scraper
    search_url_template:        str

    # The SRP element that must be present before extraction starts.
    # Also used as the circuit breaker "is the page alive?" probe.
    wait_for_selector:          str

    # ── Fallback chains (list[str] — first non-null wins) ────────────────────

    # Selector for the repeating job card container element on the SRP.
    # The scraper iterates over all matches.
    job_card:   list[str] = field(default_factory=list)

    # Link selector — evaluated WITHIN each job_card element.
    # Must resolve to an <a> whose href is the canonical job URL.
    link:       list[str] = field(default_factory=list)

    # Title selector — evaluated within each job_card element.
    title:      list[str] = field(default_factory=list)

    # Company selector — evaluated within each job_card element.
    company:    list[str] = field(default_factory=list)

    # Salary selector — optional. None values are acceptable and route to Tier 4.
    salary:     list[str] = field(default_factory=list)

    # Location selector — optional. Extracts raw location string (e.g. "USA", "Anywhere").
    # None → location_raw stays None → location scoring skipped (neutral 0 pts).
    location:   list[str] = field(default_factory=list)

    # Ubicación en tooltip (2026-09-28). En builtin.com un aviso con varias sedes
    # muestra "11 Locations" y la lista real vive en el atributo data-bs-title del
    # MISMO elemento: 35 de 50 cards medidos. Guardar el conteo es guardar basura
    # que el matcher lee como "sin señal" → PASS engañoso. True → el scraper lee
    # ese atributo con `ubicaciones_de_tooltip`. Transformación con nombre activada
    # por un booleano: el registro mapea, el código transforma.
    ubicacion_en_tooltip: bool = False

    # Feed fijo (2026-09-29). Algunos boards IGNORAN el parametro de busqueda y
    # devuelven siempre el mismo listado: medido en vivo, posthog.com, python.org y
    # news.ycombinator.com dan exactamente los mismos avisos para q=python y
    # q=designer. Los otros cinco boards CSS activos si filtran (arc.dev comparte 9
    # de 10, no se marca). La corrida del
    # 29-09 los cargo 51 veces cada uno — una por titulo — y posthog solo se llevo
    # 5.570 s de rama (~30 min con concurrencia 3) para traer 19 avisos. True → el
    # scraper los pide UNA vez por corrida; el filtro por titulo lo hace bucket_jobs
    # con title_is_relevant, igual que para cualquier otro aviso.
    feed_fijo: bool = False

    # ── Metadata ─────────────────────────────────────────────────────────────

    # ISO date the selectors were last verified against the live site.
    # Streamlit sidebar warns if this is > 30 days old.
    last_verified: str = "2026-05-07"

    # Circuit breaker — consecutive null cards before aborting domain.
    null_threshold: int = 5

    # If more than this fraction of cards return null, emit a drift alert.
    null_rate_alert_threshold: float = 0.40

    # Pagination support (for future multi-page extension)
    next_page_btn: Optional[str] = None

    # Phase 31b (2026-07-11): board retirado del scraping activo pero su config
    # se conserva para recuperación futura. active=False → no se scrapea, no
    # cuenta en la cobertura, pero el registro queda documentado.
    active: bool = True

    # Phase 31 (board recovery, 2026-07-11): cómo construir la query del SRP.
    #   "phrase"  — el título completo va al template (default histórico).
    #   "keyword" — el título se reduce a UNA palabra clave. Para boards cuyo
    #               buscador hace matching de frase exacta: "backend python
    #               developer" → 0 resultados (evidencia: builtin.com 07-11),
    #               y ese vacío alimentaba el circuit breaker hasta abortar un
    #               board perfectamente sano. workingnomads documentó este
    #               patrón en 05-30 pero el fix nunca se generalizó.
    query_style: str = "phrase"


# =============================================================================
# DOMAIN REGISTRY
# =============================================================================

SELECTORS: dict[str, DomainSelectors] = {

    # ── Himalayas.app ─────────────────────────────────────────────────────────
    # Clean remote-first board. Stable data-testid attributes since 2023.
    # SRP loads React-rendered cards after DOMContentLoaded (~300ms).
    "himalayas.app": DomainSelectors(
        domain               = "himalayas.app",
        search_url_template  = "https://himalayas.app/jobs?q={title}&remote=true",
        # Verified 2026-05-25: Himalayas migrated from data-testid attrs to
        # Tailwind utility classes + Next.js RSC. Job cards are now <article>
        # elements; the canonical job link follows /companies/{co}/jobs/{slug}.
        # wait_for_selector uses the link pattern (stable URL structure, not CSS).
        wait_for_selector    = "a[href*='/companies/'][href*='/jobs/']",
        last_verified        = "2026-05-25",
        null_threshold       = 5,
        job_card = [
            "article[class*='cursor-pointer']",
            "article[class*='rounded-xl']",
            "article",
        ],
        link = [
            # The overlay link (class: absolute inset-0) covers the whole card
            # for click area — it has an sr-only child "View job" and no visible text.
            # The second link (no absolute class) is the visible title link.
            # Both share the same /companies/.../jobs/... href pattern.
            "a[href*='/companies/'][href*='/jobs/']:not([class*='absolute'])",
            "a[href*='/companies/'][href*='/jobs/']",
        ],
        title = [
            # Same visible link — its direct text content is the job title.
            "a[href*='/companies/'][href*='/jobs/']:not([class*='absolute'])",
            "h2",
            "h3",
        ],
        company = [
            # Company name is rendered as a logo image alt text and/or a monogram.
            # The company-page link (/companies/{slug}) without /jobs/ gives the slug.
            "img[alt*='logo']",
            "a[href*='/companies/']:not([href*='/jobs/'])",
            "span[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
            "div[class*='salary']",
        ],
        next_page_btn = "a[aria-label='Next page']",
    ),

    # ── TrueUp.io ─────────────────────────────────────────────────────────────
    # Tech-focused board. React SPA with variable hydration timing.
    # Wait for job-item before extracting — salary often injected via XHR.
    "trueup.io": DomainSelectors(
        domain               = "trueup.io",
        # RETIRADO 2026-07-11: el ?q= sirve landing de filtros + login; hrefs son
        # /co/<company>/jobs (páginas de empresa). Cambió el endpoint + auth.
        # Config conservada para eventual reverse-engineering. Ver PLAN_BOARD_RECOVERY.
        active               = False,
        search_url_template  = "https://trueup.io/jobs?q={title}&remote=true",
        wait_for_selector    = "div.job-item",
        last_verified        = "2026-05-07",
        null_threshold       = 5,
        job_card = [
            "div.job-item",
            "li.job-listing",
            "article[data-job]",
        ],
        link = [
            "a.job-link",
            "a[href*='/jobs/']",
            "h2 a",
        ],
        title = [
            "h2.job-title",
            ".position-title",
            "h2",
            "h3",
        ],
        company = [
            ".company-name",
            ".employer",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            ".compensation-range",
            "span[class*='pay']",
            "span[class*='salary']",
        ],
        next_page_btn = "button.pagination-next",
    ),

    # ── Remote.co ─────────────────────────────────────────────────────────────
    # WordPress-based board. Stable traditional HTML — minimal JS hydration.
    "remote.co": DomainSelectors(
        domain               = "remote.co",
        # RETIRADO 2026-07-11: ERR_HTTP2_PROTOCOL_ERROR consistente (URL/conexión
        # muerta). Incierto, no rescatable ahora. Revisar template a futuro.
        active               = False,
        search_url_template  = "https://remote.co/remote-jobs/search/?search_keywords={title}",
        wait_for_selector    = "li.job_listing",
        last_verified        = "2026-05-07",
        null_threshold       = 5,
        job_card = [
            "li.job_listing",
            ".remote_job",
            "article.job_listing",
        ],
        link = [
            "a.job_listing_link",
            "h2 a",
            "a[href*='/remote-jobs/']",
        ],
        title = [
            ".position",
            "h2 a",
            "h3",
        ],
        company = [
            ".company",
            ".employer-name",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
        next_page_btn = ".next.page-numbers",
    ),

    # ── WeWorkRemotely ────────────────────────────────────────────────────────
    # Minimal React. Salary rarely published in listings.
    # null_threshold=3: strict — WWR cards are very consistent; nulls = drift.
    "weworkremotely.com": DomainSelectors(
        domain               = "weworkremotely.com",
        # RETIRADO 2026-07-11: HTTP 403 (anti-bot). Reactivar con camoufox (roadmap).
        active               = False,
        search_url_template  = "https://weworkremotely.com/remote-jobs/search?term={title}",
        wait_for_selector    = "li.feature",
        last_verified        = "2026-05-07",
        null_threshold       = 3,
        job_card = [
            "li.feature",
            "li[class*='job']",
        ],
        link = [
            "a[href*='/remote-jobs/']",
            "li.feature > a",
        ],
        title = [
            "span.title",
            "h4.title",
            "h3",
        ],
        company = [
            "span.company",
            "span[class*='company']",
        ],
        salary = [
            "span.salary",
            ".compensation",
        ],
        next_page_btn = "a[rel='next']",
    ),

    # ── RemoteOK ──────────────────────────────────────────────────────────────
    # High anti-bot risk (Cloudflare). Will return 403/429 and be skipped
    # gracefully by the rate-limit handler. Selectors kept for when it works.
    "remoteok.com": DomainSelectors(
        domain               = "remoteok.com",
        search_url_template  = "https://remoteok.com/?q={title}",
        wait_for_selector    = "tr.job[data-id]",
        last_verified        = "2026-05-10",
        null_threshold       = 3,
        job_card = [
            "tr.job[data-id]",
            "tr[data-id]",
        ],
        link = [
            "a.preventLink",
            "a[href*='/remote-']",
            "td.company a",
        ],
        title = [
            "h2[itemprop='title']",
            "h2.title",
            "h2",
        ],
        company = [
            "h3[itemprop='name']",
            "h3.company",
            "span.company",
        ],
        salary = [
            "span.salary",
            "div.salary",
            "span[class*='salary']",
        ],
    ),

    # ── WorkingNomads ─────────────────────────────────────────────────────────
    # AngularJS SPA. Verified 2026-05-30: card IS the <a> element (a.job-desktop).
    # Scraper card-as-link fallback handles href extraction (line 832 scraper.py).
    # Relative URLs (/jobs/slug) resolved to https://workingnomads.com/jobs/slug.
    # search_url uses single-word tags — multi-word tags return 0 results.
    "workingnomads.com": DomainSelectors(
        domain               = "workingnomads.com",
        search_url_template  = "https://www.workingnomads.com/jobs?tag={title}",
        wait_for_selector    = "a.job-desktop",
        last_verified        = "2026-05-30",
        null_threshold       = 5,
        job_card = [
            "a.job-desktop",
        ],
        link = [
            "a.job-desktop",
        ],
        title = [
            "h4",
            "[class*='title']",
            "h3",
        ],
        company = [
            "[class*='company']",
            "span.company-name",
            "p.company",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
        location = [
            "div.box",
            "[ng-show*='locations']",
        ],
        next_page_btn = "a[rel='next']",
    ),

    # ── Hacker News Jobs ──────────────────────────────────────────────────────
    # Static HTML, no keyword search — {title} param is ignored by HN.
    # Returns all current job posts from the /jobs page.
    # No structured salary field. Company often embedded in title.
    # Verified 2026-05-30: wait_for_selector must be tr.athing (30 cards). .itemlist = 0.
    "news.ycombinator.com": DomainSelectors(
        domain               = "news.ycombinator.com",
        feed_fijo            = True,   # ignora ?q= (medido 2026-09-29: 30/30 identicos)
        search_url_template  = "https://news.ycombinator.com/jobs?q={title}",
        wait_for_selector    = "tr.athing",
        last_verified        = "2026-05-30",
        null_threshold       = 10,
        job_card = [
            "tr.athing",
        ],
        link = [
            "span.titleline > a",
            "td.title a",
        ],
        title = [
            "span.titleline > a",
            "td.title a",
        ],
        company = [
            "span.sitestr",
            "span.sitebit a",
        ],
        salary = [],
    ),

    # ── Wellfound (AngelList) ─────────────────────────────────────────────────
    # Requires login to view full job listings. The scraper will timeout on
    # wait_for_selector (login wall renders instead of job cards) and return []
    # gracefully. No action needed — zero results is the expected outcome.
    "wellfound.com": DomainSelectors(
        domain               = "wellfound.com",
        # RETIRADO 2026-07-11: HTTP 403 Cloudflare JS Challenge. Reactivar con camoufox (roadmap).
        active               = False,
        search_url_template  = "https://wellfound.com/jobs?q={title}&remote=true",
        wait_for_selector    = "div[class*='JobListing']",
        last_verified        = "2026-05-10",
        null_threshold       = 2,
        job_card = [
            "div[class*='JobListing']",
            "div[class*='styles_container']",
            "div[data-test='job-row']",
        ],
        link = [
            "a[data-test='job-link']",
            "a[href*='/jobs/']",
            "h2 a",
        ],
        title = [
            "h2[class*='title']",
            "span[class*='title']",
            "h2",
        ],
        company = [
            "h3[class*='company']",
            "span[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
        ],
    ),

    # ── Arc.dev ───────────────────────────────────────────────────────────────
    # May require login for full results. Graceful timeout → [] if blocked.
    # Verified 2026-05-30: div[data-testid='job-card'] works (10 cards).
    # Link is first <a> in card: a[href*='/remote-jobs/details/'] (6 of 10 cards).
    # Other links in card are skill tag links (/remote-jobs/postgresql etc) — exclude.
    "arc.dev": DomainSelectors(
        domain               = "arc.dev",
        search_url_template  = "https://arc.dev/remote-jobs?q={title}",
        wait_for_selector    = "div[data-testid='job-card']",
        last_verified        = "2026-09-28",
        null_threshold       = 3,
        # FIX 4 live-test 07-18: títulos multi-palabra ("SRE (Site Reliability
        # Engineer)") → 0 resultados → nulls → breaker abierto (run #1). Mismo
        # patrón que builtin (31a): buscar con 1 keyword técnico.
        query_style          = "keyword",
        job_card = [
            "div[data-testid='job-card']",
            "div[class*='job-card']",
        ],
        link = [
            "a[href*='/remote-jobs/details/']",
            "a[href*='/remote-jobs/']:not([href*='/remote-jobs/ai']):not([href*='/remote-jobs/data']):not([href*='/remote-jobs/python'])",
        ],
        title = [
            "a[href*='/remote-jobs/details/']",
            "h2",
            "h3",
        ],
        # 2026-09-28, medido en vivo (10 cards): la empresa es texto en
        # `div.company-name`; el <img> del logo tiene un alt tipo "X's job post
        # on Arc's remote job board", que no es un nombre. Queda de fallback.
        company = [
            "div.company-name",
            "div.company-logo img",
            "span[class*='company']",
            "p[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "p[class*='compensation']",
        ],
        # La ubicacion vive en `.bottom-information span` ("Remote anywhere",
        # "Remote (US only)"...). Estaba en 0%: es la restriccion que decide el
        # veredicto desde la ciudad del candidato, y el card la mostraba sin que nadie la leyera.
        location = [
            "div.bottom-information span",
        ],
    ),

    # ── Builtin ───────────────────────────────────────────────────────────────
    # React SPA, tech-focused US board. Filters to remote listings.
    # Verified 2026-05-30: div[data-id] works (25 cards), a[href*='/job/'] confirmed.
    # Has LATAM-specific remote listings — high signal for el usuario.
    "builtin.com": DomainSelectors(
        domain               = "builtin.com",
        search_url_template  = "https://builtin.com/jobs/remote?search={title}",
        # Phase 31: su buscador matchea frase exacta — "backend python
        # developer" da 0; "python" da 25 (verificado en vivo 2026-07-11).
        query_style          = "keyword",
        wait_for_selector    = "div[data-id]",
        last_verified        = "2026-09-28",
        null_threshold       = 5,
        job_card = [
            "div[data-id]",
            "div.job-card",
        ],
        link = [
            "a[href*='/job/']",
            "a[data-id]",
            "h2 a",
        ],
        title = [
            "a[href*='/job/']",
            "h2[class*='job-title']",
            "h2",
        ],
        company = [
            # 2026-09-28: el primer a[href*='/company/'] del card es ahora el LOGO
            # (sin texto) → 50 de 50 cards caían al placeholder "(empresa no
            # listada)". El nombre está en el enlace con data-id="company-title".
            "a[data-id='company-title']",
            "a[href*='/company/']",          # Phase 31b: company migró acá (verif. 2026-07-11: "Luxury Presence")
            "div[class*='company-name']",
            "span[class*='company']",
            "a[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "div[class*='salary']",
        ],
        # Ubicación medida 2026-09-28 sobre 50 cards reales ("python", "backend"):
        # la corrida del 09-17 trajo 108 avisos con 0% de ubicación porque no había
        # selector. El span junto al ícono fa-location-dot trae la sede en 15
        # ("la ciudad del candidato, PER") y un conteo en 35 ("11 Locations", lista en data-bs-title).
        # El segundo selector es el aria-label semántico del caso multi-sede: si
        # Font Awesome renombra el ícono, el 70% multi-sede sigue saliendo.
        location = [
            "div:has(> div > i.fa-location-dot) span",
            "span[aria-label='Job locations']",
        ],
        ubicacion_en_tooltip = True,
        next_page_btn = "a[aria-label='Next page']",
    ),

    # ── Welcome to the Jungle ─────────────────────────────────────────────────
    # French-origin React SPA. May require account for full listings.
    # Graceful timeout → [] if blocked by auth wall.
    "welcometothejungle.com": DomainSelectors(
        domain               = "welcometothejungle.com",
        # RETIRADO 2026-07-11: muro de login + "subí tu CV para que te matcheemos".
        # Los jobs no son accesibles sin auth. Config conservada.
        active               = False,
        search_url_template  = "https://www.welcometothejungle.com/en/jobs?query={title}&remote=true",
        wait_for_selector    = "article[data-testid='job-card']",
        last_verified        = "2026-05-10",
        null_threshold       = 3,
        job_card = [
            "article[data-testid='job-card']",
            "li[data-testid='search-results-list-item-wrapper']",
            "li[class*='sc-']",
        ],
        link = [
            "a[data-testid='job-card-link']",
            "a[href*='/en/companies/']",
            "article a",
        ],
        title = [
            "h3[class*='title']",
            "h4[class*='title']",
            "h3",
        ],
        company = [
            "span[class*='company']",
            "p[class*='company']",
            "a[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "li[class*='salary']",
        ],
    ),

    # ── Remotivated ───────────────────────────────────────────────────────────
    # Remote job board. Phase 31b (verif. 2026-07-11): migró a <article> cards;
    # company/title/link confirmados en vivo (h3 title, [class*='company']).
    "remotivated.com": DomainSelectors(
        domain               = "remotivated.com",
        search_url_template  = "https://remotivated.com/jobs?q={title}",
        wait_for_selector    = "article",
        last_verified        = "2026-07-11",
        null_threshold       = 5,
        job_card = [
            "article",
            ".job-card",
            "div[class*='job']",
        ],
        link = [
            "a[href*='/jobs/']",
            "h3 a",
            "h2 a",
        ],
        title = [
            "h3",
            "h2",
            ".job-title",
        ],
        company = [
            "[class*='company']",
            ".company",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
    ),

    # ── PostHog Cool Tech Jobs ────────────────────────────────────────────────
    # Curated list — cada job es un <a> hacia /careers/ (PostHog propio) o un ATS
    # externo (jobs.ashbyhq.com/<empresa>/). posthog-full (2026-07-16): el
    # job_card usa UN selector COMBINADO (grupo CSS) para capturar AMBOS tipos en
    # una pasada — un fallback chain paraba en el primer match (/careers/) y
    # perdía todos los ashbyhq externos (Supabase, Infisical, Chroma...). La
    # empresa se deriva de la URL (derive_company_from_url): ashbyhq slug o PostHog.
    #
    # Verificado 2026-09-28, 6 cargas en vivo por el camino real de extracción:
    #
    # UBICACIÓN: el board no la publica. Cada empresa es una grilla de tres
    # columnas —Date posted | Job title | Department— y el card es
    # <a><span>título</span></a>; "location" aparece 0 veces en 1,3 MB de DOM
    # renderizado. `location` queda vacío a propósito: dentro de la fila solo hay
    # fecha y departamento. El feed que alimenta la página (Strapi, 165 de 176 con
    # location) le borra el "Remote" al valor de Ashby en 63 de 63 avisos
    # comparados ("Remote, Global" -> "Any", "Remote" -> ""): convertiría remotos
    # en presenciales. La ubicación cruda vive en Ashby (164 de 176 avisos), y ese
    # camino es ats_registry, no este selector.
    #
    # ESPERA: `a[class*='group']` matcheaba 29 links del nav a los 0,8 s y el
    # listado llega con Strapi entre 3 y 21 s después; el jitter es de 5-15 s. Si
    # perdía la carrera, el card caía al fallback `a[class*='group']` y guardaba
    # el nav como aviso ("Get started – free" -> app.posthog.com/signup, registry
    # 2026-07-18). Ahora se espera a los links de aviso, que no existen antes del
    # listado (el nav linkea /careers sin barra final). jobs.gem.com entra al
    # grupo: 6 avisos en el DOM (Converge 2, SigNoz 4) que solo llegaban por esa
    # carrera. Medido por el camino real: con la config vieja perdiendo la carrera,
    # 89 cards, 39 sin título y el CTA como aviso; con esta, 49 cards, 49 con título
    # y link, 43 con empresa (jobs.gem.com no está en derive_company_from_url).
    "posthog.com": DomainSelectors(
        domain               = "posthog.com",
        feed_fijo            = True,   # ignora ?q= (medido 2026-09-29)
        search_url_template  = "https://posthog.com/cool-tech-jobs?q={title}",
        wait_for_selector    = "a[href*='/careers/'], a[href*='ashbyhq'], a[href*='jobs.gem.com']",
        last_verified        = "2026-09-28",
        null_threshold       = 8,
        job_card = [
            # combinado — los tres tipos de aviso en una pasada
            "a[href*='/careers/'], a[href*='ashbyhq'], a[href*='jobs.gem.com']",
        ],
        link = [
            # el card ES el <a>; el scraper cae a card.get_attribute('href')
            "a[href]",
        ],
        title = [
            "span",
            "h2",
            "h3",
        ],
        company = [],
        salary = [],
        location = [],   # el board no la publica — ver UBICACIÓN arriba
    ),

    # ── Jobspresso ────────────────────────────────────────────────────────────
    # WordPress + WP Job Manager. Clean HTML, minimal JS. Stable selectors.
    # Verified 2026-05-25: li.job_listing (30 cards), link is a.job_listing-clickbox
    # with absolute URL pattern /job/ (NOT /remote-work/). Title is h3.job_listing-title.
    "jobspresso.co": DomainSelectors(
        domain               = "jobspresso.co",
        search_url_template  = "https://jobspresso.co/remote-work/?search_keywords={title}",
        wait_for_selector    = "li.job_listing",
        last_verified        = "2026-09-28",
        null_threshold       = 5,
        job_card = [
            "li.job_listing",
            "article.job_listing",
        ],
        link = [
            "a.job_listing-clickbox",
            "a[href*='/job/']",
            "h3 a",
        ],
        title = [
            "h3.job_listing-title",
            ".position",
            "h3",
            "h2",
        ],
        # 2026-09-28, medido en vivo (30 cards): la empresa es el <strong> de
        # `.job_listing-company`. Va PRIMERO porque el fallback
        # `span[class*='company']` matchea `job_listing-company-tagline`, que es el
        # eslogan ("The best way to keep you... safe online"), no el nombre.
        company = [
            ".job_listing-company strong",
            ".company",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
        # La ubicacion estaba en 0% con el dato en el card: `.job_listing-location`
        # trae "United States, Canada", "United States (EST Time Zone)"... Justo
        # las restricciones que el veredicto desde la ciudad del candidato necesita ver.
        location = [
            ".job_listing-location a.google_map_link",
            ".job_listing-location",
        ],
        next_page_btn = "a.next",
    ),

    # ── Greenhouse Careers ────────────────────────────────────────────────────
    # Greenhouse's own company career page (not the ATS platform).
    # Static/Next.js. Limited listings but high-signal tech roles.
    "greenhouse.com": DomainSelectors(
        domain               = "greenhouse.com",
        # RETIRADO 2026-07-11: es el sitio de marketing del ATS Greenhouse
        # (Platform/Solutions/AI recruiting), NO un aggregator de empleos. Mal
        # configurado desde el inicio — greenhouse.com no agrega jobs scrapeables.
        active               = False,
        search_url_template  = "https://www.greenhouse.com/careers/opportunities?q={title}",
        wait_for_selector    = "main",
        last_verified        = "2026-05-10",
        null_threshold       = 5,
        job_card = [
            "div[class*='opening']",
            "li[class*='opening']",
            "div.job",
            "li.job",
        ],
        link = [
            "a[href*='/careers/']",
            "a[href*='boards.greenhouse.io']",
            "h2 a",
        ],
        title = [
            "h2",
            "h3",
            "p[class*='title']",
        ],
        company = [
            "span[class*='company']",
            "p[class*='department']",
        ],
        salary = [],
    ),

    # ── Python.org Jobs ───────────────────────────────────────────────────────
    # Official Python job board. Static HTML, highly stable selectors.
    # Keyword search supported via ?q= param. No salary field published.
    # Location verified 2026-06-02: span.listing-location, e.g.
    # "San Francisco, CA, United States" → feeds the soft-geo filter (AUDIT #2).
    "python.org": DomainSelectors(
        domain               = "python.org",
        feed_fijo            = True,   # ignora ?q= (medido 2026-09-29)
        search_url_template  = "https://www.python.org/jobs/?q={title}",
        wait_for_selector    = "ol.list-recent-jobs",
        last_verified        = "2026-06-02",
        null_threshold       = 5,
        job_card = [
            "ol.list-recent-jobs li",
            "ol.list-recent-jobs article",
        ],
        link = [
            "h2 a",
            "a[href*='/jobs/']",
        ],
        title = [
            "h2 a",
            "h2",
        ],
        company = [
            "span.listing-company-name",
            ".company",
            "span[class*='company']",
        ],
        salary = [],
        location = [
            "span.listing-location",
            ".listing-location",
        ],
        next_page_btn = "a[rel='next']",
    ),

    # ── Startup.jobs ──────────────────────────────────────────────────────────
    # Startup-focused board. React SPA with server-side rendered job cards.
    # Remote filter appended via &remote=true. Salary rarely published.
    # Selectors verified 2026-05-25 — run God Mode if null_rate trips.
    "startup.jobs": DomainSelectors(
        domain               = "startup.jobs",
        # RETIRADO 2026-07-11: HTTP 403 (anti-bot). Reactivar con camoufox (roadmap).
        active               = False,
        search_url_template  = "https://startup.jobs/?q={title}&remote=true",
        wait_for_selector    = "div.css-1wts5rl",
        last_verified        = "2026-05-25",
        null_threshold       = 5,
        job_card = [
            "div.css-1wts5rl",
            "div[data-cy='job-card']",
            "li[class*='job']",
            "article[class*='job']",
        ],
        link = [
            "a[data-cy='job-link']",
            "a[href*='/jobs/']",
            "h2 a",
            "h3 a",
        ],
        title = [
            "h2[class*='title']",
            "h3[class*='title']",
            "span[class*='title']",
            "h2",
            "h3",
        ],
        company = [
            "span[class*='company']",
            "p[class*='company']",
            "div[class*='company']",
            "a[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
            "div[class*='salary']",
        ],
        next_page_btn = "a[aria-label='Next page']",
    ),
}


# =============================================================================
# Lookup API
# =============================================================================

def get_selectors(domain: str) -> Optional[DomainSelectors]:
    """
    Retrieves the DomainSelectors for a domain string.
    Partial-match aware: 'himalayas.app/jobs?q=sdet' resolves correctly.
    Returns None if unknown → caller should prompt God Mode.
    """
    clean = domain.lower().strip().replace("www.", "").rstrip("/").split("/")[0]
    for key, sel in SELECTORS.items():
        if key in clean:
            return sel
    return None


def list_supported_domains() -> list[str]:
    return list(SELECTORS.keys())


def active_domains() -> list[str]:
    """Dominios que se scrapean activamente (Phase 31b — excluye retirados).

    Un board retirado (active=False) conserva su config para recuperación
    futura pero no se scrapea ni cuenta en la cobertura. Este es el listado
    que api.py/main.py usan para poblar target_domains.

    Fase 1 (2026-09-14): a ese listado se suman los boards con endpoint público.
    `active=False` significa «sus selectores CSS no sirven», que es exactamente el
    caso de weworkremotely.com — retirado el 07-11 por HTTP 403 contra el navegador
    y cuyo RSS devuelve 90 avisos en 484 ms. Un board leído por endpoint no usa
    selectores, así que el flag que lo apagaba no aplica: dejarlo afuera sería
    retirar un board por un motivo que ya no existe.
    """
    activos = [k for k, sel in SELECTORS.items() if sel.active]
    try:
        import endpoint_registry
    except Exception:
        return activos
    for d in endpoint_registry.dominios():
        if d not in activos:
            activos.append(d)
    # Fase 2: las plataformas ATS entran como pseudo-dominios. No son un sitio que se
    # raspe —son N tableros de empresa detras de una API— pero llegar por la misma
    # puerta que el resto evita un segundo camino de configuracion que driftee.
    try:
        import ats_registry
        for d in ats_registry.dominios():
            if d not in activos:
                activos.append(d)
    except Exception:
        pass

    # Fase 9: un board con politica de acceso que lo impide sale de la lista. Es la
    # diferencia entre "lo intentamos y falla" y "no se intenta, y por esto".
    # remote.co (host muerto) y welcometothejungle.com (muro de login) se
    # "diagnosticaron" dos veces entre julio y septiembre porque la razon vivia en un
    # comentario y no en la estrategia que el router lee.
    try:
        import endpoint_registry
        bloqueados = {d for d, p in endpoint_registry.POLITICAS.items()
                      if p.get("access_policy") in ("declined", "auth_required")}
        activos = [d for d in activos if d not in bloqueados]
    except Exception:
        pass
    return activos


def is_selector_stale(sel: DomainSelectors, warn_after_days: int = 30) -> bool:
    """Returns True if selectors are older than warn_after_days."""
    from datetime import date
    try:
        last = date.fromisoformat(sel.last_verified)
        return (date.today() - last).days > warn_after_days
    except ValueError:
        return True
