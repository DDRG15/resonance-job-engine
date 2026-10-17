"""
tests/test_location_posthog.py — posthog.com: el card no trae ubicación, y se lee
recién cuando el listado llegó (2026-09-28).

Dos hechos medidos en vivo el 2026-09-28 sobre https://posthog.com/cool-tech-jobs y
congelados acá contra HTML REAL, copiado tal cual del DOM renderizado:

1. Cada empresa es una grilla de tres columnas —Date posted | Job title | Department—
   y el card es `<a><span>título</span></a>`. No hay celda de ubicación. Que
   `location_raw` salga None es la forma del board, no un selector roto: cualquier
   selector de ubicación dentro de la fila devolvería la fecha o el departamento, y
   un aviso con "Engineering" como ubicación es peor que uno sin ubicación.

2. El nav existe entre 3 y 20 s antes que el listado (Strapi respondió a los 15,5 s
   en una de las cargas). `a[class*='group']` —el wait y el fallback de job_card hasta hoy—
   matcheaba 29 links del nav, y con el jitter de 5-15 s el card se leía antes de que
   llegara el listado: "Get started – free" -> app.posthog.com/signup quedó guardado
   como aviso en el registry el 2026-07-18. Ni el wait ni el card pueden matchear el
   nav.

Pasa por el camino real, `GemaScraper._extract_all_cards` sobre un DOM de Chromium
(`page.set_content`, sin red), y no por un querySelector suelto ni un card falso: la
falla vivía en la cadena de job_card, que un card falso se saltea.
"""

import asyncio
import functools
import queue

import pytest

import selectors_registry as sr
from models import ScrapeRunSummary
from scraper import CircuitBreaker, GemaScraper

POSTHOG = sr.SELECTORS["posthog.com"]

# ── HTML real, 2026-09-28 ─────────────────────────────────────────────────────
# Substrings crudos de page.content() sobre /cool-tech-jobs?q=python+developer.
# Las dos primeras grillas son idénticas byte a byte al outerHTML que devolvió el
# navegador. No se recortó nada: los atributos class multilínea son así en el sitio.

# PostHog propio: links relativos a /careers/<slug>.
GRILLA_POSTHOG = '''<div class="text-primary inline-grid max-w-full divide-x divide-y divide-border border-b border-primary text-[15px] [&amp;>div]:px-2 rounded-md w-min [&amp;>div]:py-2 " style="grid-template-columns: minmax(120px, auto) minmax(300px, 2fr) minmax(150px, auto);"><div class="text-sm border-l border-t border-primary bg-input font-bold rounded-tl-md   ">Date posted</div><div class="text-sm border-l border-t border-primary bg-input font-bold    ">Job title</div><div class="text-sm border-l border-t border-primary bg-input font-bold  !border-r rounded-tr-md  ">Department</div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">4 days ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a class="group !text-inherit underline font-medium" href="/careers/product-engineer"><span class="relative">Product Engineer</span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">Engineering</span></div><div class="col-span-full text-center !py-0 !border-r border-primary bg-accent hover:bg-input-hover "><button class="text-primary hover:text-accent font-semibold text-[13px] w-full py-1">Show 4 more engineering roles</button></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">14 days ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a class="group !text-inherit underline font-medium" href="/careers/finance-manager-revenue-accounting"><span class="relative">Finance Manager, Revenue Accounting</span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">Finance</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">11 days ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a class="group !text-inherit underline font-medium" href="/careers/technical-customer-success-manager-emea"><span class="relative">Technical Customer Success Manager - EMEA</span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">Sales &amp; Customer Success</span></div><div class="col-span-full text-center !py-0 !border-r border-primary bg-accent hover:bg-input-hover rounded-b-md"><button class="text-primary hover:text-accent font-semibold text-[13px] w-full py-1">Show 1 more sales &amp; customer success role</button></div></div>'''

# Ashby externo: el slug de la empresa viene en la URL (Nango, con mayúscula).
GRILLA_NANGO = '''<div class="text-primary inline-grid max-w-full divide-x divide-y divide-border border-b border-primary text-[15px] [&amp;>div]:px-2 rounded-md w-min [&amp;>div]:py-2 " style="grid-template-columns: minmax(120px, auto) minmax(300px, 2fr) minmax(150px, auto);"><div class="text-sm border-l border-t border-primary bg-input font-bold rounded-tl-md   ">Date posted</div><div class="text-sm border-l border-t border-primary bg-input font-bold    ">Job title</div><div class="text-sm border-l border-t border-primary bg-input font-bold  !border-r rounded-tr-md  ">Department</div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">a month ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a rel="noopener noreferrer" href="https://jobs.ashbyhq.com/Nango/09421bc9-987b-4fe3-aeac-bd183afc5b8e?utm_source=posthog" class="group !text-inherit underline font-medium group" target="_blank"><span class="relative">Staff Engineer, Platform &amp; Infrastructure<svg class="LemonIcon inline-block size-4 opacity-0 group-hover:opacity-50 text-primary dark:text-primary-dark absolute left-full top-0.5 ml-0.5" width="100%" fill="currentColor" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M7.995 5.75a.75.75 0 0 1 .75-.75h8.505c.966 0 1.75.784 1.75 1.75v9.496a.75.75 0 0 1-1.5 0V7.56L7.03 18.03a.75.75 0 0 1-1.06-1.061L16.44 6.5H8.744a.75.75 0 0 1-.75-.75Z" clip-rule="evenodd"></path></svg></span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">Engineering</span></div><div class="col-span-full text-center !py-0 !border-r border-primary bg-accent hover:bg-input-hover "><button class="text-primary hover:text-accent font-semibold text-[13px] w-full py-1">Show 3 more engineering roles</button></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">3 hours ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a rel="noopener noreferrer" href="https://jobs.ashbyhq.com/Nango/c946685a-e091-47f9-9ac4-796c6d2955de?utm_source=posthog" class="group !text-inherit underline font-medium group" target="_blank"><span class="relative">Field Marketer (Founding)<svg class="LemonIcon inline-block size-4 opacity-0 group-hover:opacity-50 text-primary dark:text-primary-dark absolute left-full top-0.5 ml-0.5" width="100%" fill="currentColor" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M7.995 5.75a.75.75 0 0 1 .75-.75h8.505c.966 0 1.75.784 1.75 1.75v9.496a.75.75 0 0 1-1.5 0V7.56L7.03 18.03a.75.75 0 0 1-1.06-1.061L16.44 6.5H8.744a.75.75 0 0 1-.75-.75Z" clip-rule="evenodd"></path></svg></span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">GTM</span></div><div class="col-span-full text-center !py-0 !border-r border-primary bg-accent hover:bg-input-hover rounded-b-md"><button class="text-primary hover:text-accent font-semibold text-[13px] w-full py-1">Show 4 more gtm roles</button></div></div>'''

# Gem externo (Converge): hasta hoy solo entraba si el card perdía la carrera y caía
# al fallback `a[class*='group']`. 6 avisos así en el DOM de esa carga.
GRILLA_GEM = '''<div class="text-primary inline-grid max-w-full divide-x divide-y divide-border border-b border-primary text-[15px] [&amp;>div]:px-2 rounded-md w-min [&amp;>div]:py-2 " style="grid-template-columns: minmax(120px, auto) minmax(300px, 2fr) minmax(150px, auto);"><div class="text-sm border-l border-t border-primary bg-input font-bold rounded-tl-md   ">Date posted</div><div class="text-sm border-l border-t border-primary bg-input font-bold    ">Job title</div><div class="text-sm border-l border-t border-primary bg-input font-bold  !border-r rounded-tr-md  ">Department</div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">2 hours ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a rel="noopener noreferrer" href="https://jobs.gem.com/converge/am9icG9zdDreA6I3WJ4ZJ1Yx_WHS5zKP?utm_source=posthog" class="group !text-inherit underline font-medium group" target="_blank"><span class="relative">Product Engineer<svg class="LemonIcon inline-block size-4 opacity-0 group-hover:opacity-50 text-primary dark:text-primary-dark absolute left-full top-0.5 ml-0.5" width="100%" fill="currentColor" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M7.995 5.75a.75.75 0 0 1 .75-.75h8.505c.966 0 1.75.784 1.75 1.75v9.496a.75.75 0 0 1-1.5 0V7.56L7.03 18.03a.75.75 0 0 1-1.06-1.061L16.44 6.5H8.744a.75.75 0 0 1-.75-.75Z" clip-rule="evenodd"></path></svg></span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">Engineering</span></div><div class="col-span-full text-center !py-0 !border-r border-primary bg-accent hover:bg-input-hover "><button class="text-primary hover:text-accent font-semibold text-[13px] w-full py-1">Show 1 more engineering role</button></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span class="text-sm">2 hours ago</span></div><div class="
                                relative
               bg-primary
               
               
               
            flex items-center
             "><span data-state="closed" class=""><a rel="noopener noreferrer" href="https://jobs.gem.com/converge/am9icG9zdDr1aeHCHQYLLzHpg3nAKdgC?utm_source=posthog" class="group !text-inherit underline font-medium group" target="_blank"><span class="relative">Account Executive<svg class="LemonIcon inline-block size-4 opacity-0 group-hover:opacity-50 text-primary dark:text-primary-dark absolute left-full top-0.5 ml-0.5" width="100%" fill="currentColor" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M7.995 5.75a.75.75 0 0 1 .75-.75h8.505c.966 0 1.75.784 1.75 1.75v9.496a.75.75 0 0 1-1.5 0V7.56L7.03 18.03a.75.75 0 0 1-1.06-1.061L16.44 6.5H8.744a.75.75 0 0 1-.75-.75Z" clip-rule="evenodd"></path></svg></span></a></span></div><div class="
                                relative
               bg-primary
               !border-r
               
               
            flex items-center
             "><span class="text-sm opacity-75">Sales</span></div><div class="col-span-full text-center !py-0 !border-r border-primary bg-accent hover:bg-input-hover rounded-b-md"><button class="text-primary hover:text-accent font-semibold text-[13px] w-full py-1">Show 1 more sales role</button></div></div>'''

# Links del nav que YA están en el DOM antes de que llegue el listado, copiados de
# page.content() tomado 2,5 s después del wait viejo, con 0 avisos todavía.
NAV_ANTES_DEL_LISTADO = (
    '''<a rel="noopener" href="https://app.posthog.com/signup" class="relative items-center rounded rounded border text-primary transition-colors transition-[font-size,line-height,padding] transition-50 hover:transition-none disabled:text-muted disabled:cursor-not-allowed inline-flex border-[1.5px] top-[0px] rounded-[6px] bg-button-shadow dark:bg-button-shadow-dark text-primary border-button text-center group disabled:opacity-50 disabled:cursor-not-allowed inline-block justify-center w-auto  !no-underline  group" target="_blank"><span class="flex items-center justify-center bg-orange text-black hover:text-black dark:text-black dark:hover:text-black no-underline border-button dark:border-button-dark dark:bg-orange font-bold active:transition-all active:duration-100 select-none px-2.5 py-1 gap-1 rounded-[6px] text-[13px] translate-y-[-2px] hover:translate-y-[-3px] active:translate-y-[-1.5px] border-[1.5px] mx-[-1.5px] group-disabled:hover:!translate-y-[-2px] ">Get started – free</span></a>'''
    '''<a class="" href="/careers">Visit our careers page.</a>'''
    '''<a class="group flex select-none items-center justify-between gap-0.5 rounded px-1.5 py-0.5 text-[13px] leading-none text-primary outline-none data-[highlighted]:bg-accent hover:bg-accent-2 data-[state=open]:bg-accent font-medium " href="/pricing">Pricing</a>'''
)


# ── Arnés: el scraper real sin DB ni webhook ──────────────────────────────────

class _RegistryVacio:
    """Todo aviso es nuevo: el dedup contra la base no es lo que se prueba acá."""

    def is_seen(self, _url, _ttl_hours):
        return False, ""


class _SinWebhook:
    is_enabled = False


def _scraper() -> GemaScraper:
    """Solo lo que `_extract_all_cards` lee. Se arma dentro del loop que lo usa."""
    s = GemaScraper.__new__(GemaScraper)
    s.circuit = CircuitBreaker()
    s.summary = ScrapeRunSummary()
    s.db = _RegistryVacio()
    s.ttl_hours = 0
    s.webhook = _SinWebhook()
    s.log_queue = queue.Queue()
    s._session_seen = set()
    s._abort_event = asyncio.Event()
    s._write_q = asyncio.Queue()
    return s


async def _medir() -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:          # Chromium no instalado en esta máquina
            return {"sin_chromium": f"{type(exc).__name__}: {str(exc)[:160]}"}
        try:
            page = await browser.new_page()
            medicion = {}

            await page.set_content(
                "<html><body>" + GRILLA_POSTHOG + GRILLA_NANGO + GRILLA_GEM + "</body></html>"
            )
            medicion["listado_wait"] = len(await page.query_selector_all(POSTHOG.wait_for_selector))
            medicion["listado_jobs"] = await _scraper()._extract_all_cards(
                page, "posthog.com", POSTHOG
            )

            await page.set_content("<html><body>" + NAV_ANTES_DEL_LISTADO + "</body></html>")
            medicion["nav_group"] = len(await page.query_selector_all("a[class*='group']"))
            medicion["nav_wait"] = len(await page.query_selector_all(POSTHOG.wait_for_selector))
            medicion["nav_por_card"] = {
                css: len(await page.query_selector_all(css)) for css in POSTHOG.job_card
            }
            medicion["nav_jobs"] = await _scraper()._extract_all_cards(
                page, "posthog.com", POSTHOG
            )
            return medicion
        finally:
            await browser.close()


@functools.lru_cache(maxsize=1)
def _medicion() -> dict:
    """Un solo Chromium para todo el archivo: lanzarlo por test cuesta segundos."""
    return asyncio.run(_medir())


def _o_saltar() -> dict:
    m = _medicion()
    if "sin_chromium" in m:
        pytest.skip(f"Chromium de Playwright no disponible: {m['sin_chromium']}")
    return m


# ── 1. La ubicación: el board no la publica ───────────────────────────────────

def test_el_card_de_posthog_no_trae_ubicacion():
    jobs = _o_saltar()["listado_jobs"]
    assert len(jobs) == 7
    assert [j.location_raw for j in jobs] == [None] * 7


# ── 2. Lo que andaba sigue saliendo igual ─────────────────────────────────────

def test_titulo_empresa_y_link_por_el_camino_real():
    jobs = _o_saltar()["listado_jobs"]
    assert [(j.title, j.company, j.url) for j in jobs] == [
        ("Product Engineer", "PostHog",
         "https://posthog.com/careers/product-engineer"),
        ("Finance Manager, Revenue Accounting", "PostHog",
         "https://posthog.com/careers/finance-manager-revenue-accounting"),
        ("Technical Customer Success Manager - EMEA", "PostHog",
         "https://posthog.com/careers/technical-customer-success-manager-emea"),
        ("Staff Engineer, Platform & Infrastructure", "Nango",
         "https://jobs.ashbyhq.com/Nango/09421bc9-987b-4fe3-aeac-bd183afc5b8e"
         "?utm_source=posthog"),
        ("Field Marketer (Founding)", "Nango",
         "https://jobs.ashbyhq.com/Nango/c946685a-e091-47f9-9ac4-796c6d2955de"
         "?utm_source=posthog"),
        # jobs.gem.com lleva la empresa en el primer segmento de la ruta: el aviso ya no
        # queda como «(empresa no listada)».
        ("Product Engineer", "Converge",
         "https://jobs.gem.com/converge/am9icG9zdDreA6I3WJ4ZJ1Yx_WHS5zKP"
         "?utm_source=posthog"),
        ("Account Executive", "Converge",
         "https://jobs.gem.com/converge/am9icG9zdDr1aeHCHQYLLzHpg3nAKdgC"
         "?utm_source=posthog"),
    ]
    assert all(j.source_domain == "posthog.com" for j in jobs)


def test_el_wait_ve_el_listado():
    """Si el wait no matchea el listado, el scraper corta en timeout y el board da 0."""
    assert _o_saltar()["listado_wait"] == 7


# ── 3. El nav previo al listado no se lee como aviso ──────────────────────────

def test_el_nav_si_matchea_el_selector_viejo():
    """Precondición: sin esto el test de abajo no prueba nada. El HTML real del nav
    tiene links `group` —el CTA de signup y el menú—, que es lo que el wait viejo veía."""
    assert _o_saltar()["nav_group"] == 2


def test_ni_el_wait_ni_el_card_matchean_el_nav():
    m = _o_saltar()
    assert m["nav_wait"] == 0
    assert m["nav_por_card"] == {css: 0 for css in POSTHOG.job_card}
    assert m["nav_jobs"] == []
