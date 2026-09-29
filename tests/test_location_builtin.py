"""
tests/test_location_builtin.py — ubicación de builtin.com (2026-09-28).

La corrida de calibración del 2026-09-17 trajo 108 avisos de builtin con 0% de
ubicación: el board no tenía selector. Sin `location_raw` el matcher da PASS por
ausencia de dato, así que un aviso solo para USA pasaba igual que uno abierto a el pais del candidato.

Medido en vivo el 2026-09-28 sobre 50 cards: 15 muestran la sede ("la ciudad del candidato, PER") y
35 un conteo ("11 Locations") con la lista real en el atributo `data-bs-title`. Un
selector de texto solo habría guardado el conteo en el 70% de los avisos — un dato
que no nombra ningún país y que el matcher vuelve a leer como "sin señal".

`CARDS_REALES` (al final del archivo) es outerHTML REAL copiado del DOM renderizado,
no HTML escrito a mano: un selector congelado contra HTML inventado prueba lo que
imaginó quien escribió el test, no el board. Va incrustado y no en tests/fixtures/
porque .gitignore excluye ese directorio salvo el corpus de la Fase 7.

El camino probado es el de producción: Chromium real —su motor CSS, `:has()`
incluido, que un DOM falso no puede validar— y `_extract_all_cards` →
`_extract_card_metadata`, con TODA petición de red abortada dentro del navegador.
"""

import asyncio
import dataclasses
import queue
import re
from unittest.mock import MagicMock

import pytest

import config
import selectors_registry as sr
from database import GemaDatabase
from models import SearchConfig
from scraper import GemaScraper, ubicaciones_de_tooltip

BUILTIN = sr.SELECTORS["builtin.com"]

# Valor de data-bs-title del card de NetBox Labs tal como lo devuelve el DOM
# (get_attribute): las entidades llegan escapadas una vez, `Per&#250;`.
TOOLTIP_NETBOX = (
    "<div class='text-truncate'>Chile</div><div class='text-truncate'>Per&#250;</div>"
    "<div class='text-truncate'>Guyana</div><div class='text-truncate'>Brazil</div>"
    "<div class='text-truncate'>Ecuador</div><div class='text-truncate'>Uruguay</div>"
    "<div class='text-truncate'>Colombia</div><div class='text-truncate'>Suriname</div>"
    "<div class='text-truncate'>Bolivia</div><div class='text-truncate'>Paraguay</div>"
    "<div class='text-truncate'>Argentina</div>"
    "<div class='text-truncate'>Remote, OR, USA</div>"
)
LISTA_NETBOX = (
    "Chile; Perú; Guyana; Brazil; Ecuador; Uruguay; Colombia; Suriname; "
    "Bolivia; Paraguay; Argentina; Remote, OR, USA"
)
LISTA_RESEND = (
    "Chile; Perú; Canada; USA; Guyana; Brazil; Ecuador; México; Uruguay; "
    "Colombia; Suriname; Bolivia; Paraguay; Argentina"
)
CONTEO = re.compile(r"\d+\s+Locations?", re.IGNORECASE)

T_NETBOX = "Senior Site Reliability Engineer, NetBox Delivery"
T_LIMA = "Senior Software Engineer (Python/Flask+React) - Remote, Latin America"
T_RESEND = "Software Engineer, Backend"


# =============================================================================
# La transformación, pura
# =============================================================================

class TestUbicacionesDeTooltip:
    def test_tooltip_real_da_la_lista_con_acentos(self):
        assert ubicaciones_de_tooltip(TOOLTIP_NETBOX) == LISTA_NETBOX

    def test_separador_no_es_coma(self):
        # "Remote, OR, USA" es UNA sede. Con coma como separador no se sabría.
        partes = ubicaciones_de_tooltip(TOOLTIP_NETBOX).split("; ")
        assert partes[-1] == "Remote, OR, USA"
        assert len(partes) == 12

    def test_sin_tooltip_es_none(self):
        assert ubicaciones_de_tooltip(None) is None
        assert ubicaciones_de_tooltip("") is None
        assert ubicaciones_de_tooltip("<div class='text-truncate'></div>") is None

    def test_repetidas_se_colapsan_en_orden(self):
        dup = "<div>Chile</div><div>Per&#250;</div><div>Chile</div>"
        assert ubicaciones_de_tooltip(dup) == "Chile; Perú"

    def test_texto_sin_etiquetas_se_conserva(self):
        assert ubicaciones_de_tooltip("la ciudad del candidato, PER") == "la ciudad del candidato, PER"


# =============================================================================
# El registro
# =============================================================================

class TestRegistro:
    def test_builtin_tiene_selector_y_flag(self):
        assert BUILTIN.location, "builtin volvió a quedar sin selector de ubicación"
        assert BUILTIN.ubicacion_en_tooltip is True

    def test_el_flag_es_opt_in(self):
        # La transformación es de builtin. Otro board que la herede sin medir su
        # DOM leería el data-bs-title de cualquier cosa que tenga tooltip.
        con_flag = {d for d, s in sr.SELECTORS.items() if s.ubicacion_en_tooltip}
        assert con_flag == {"builtin.com"}


# =============================================================================
# El camino real: Chromium + _extract_all_cards sobre los cards reales
# =============================================================================

_CACHE: dict = {}


async def _extraer_con_chromium(tmp_dir) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # navegador no instalado en esta máquina
            pytest.skip(f"Chromium de Playwright no disponible: {exc}")
        try:
            context = await browser.new_context()
            # Sin red: los <img> del card apuntan a cdn.builtin.com. El bloqueo de
            # sockets de conftest cubre este proceso, no el de Chromium.
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(f"<html><body>{CARDS_REALES}</body></html>")

            # Una sola base para las dos pasadas: iniciarla cuesta ~1,5 s (medido) y
            # nada se escribe — la cola de escritura no tiene consumidor acá, y el
            # dedup en sesión vive en cada scraper, no en la base.
            db = GemaDatabase(db_path=str(tmp_dir / "builtin.db"))
            salida = {}
            variantes = {
                "con_flag": BUILTIN,
                "sin_flag": dataclasses.replace(BUILTIN, ubicacion_en_tooltip=False),
            }
            for nombre, sel in variantes.items():
                scraper = GemaScraper(
                    SearchConfig(target_titles=["python"], target_domains=["builtin.com"]),
                    db,
                    queue.Queue(),
                    webhook=MagicMock(is_enabled=False),
                )
                jobs = await scraper._extract_all_cards(page, "builtin.com", sel)
                salida[nombre] = {j.title: j for j in jobs}
            return salida
        finally:
            await browser.close()


@pytest.fixture
def extraidos(tmp_path, monkeypatch):
    """Un solo Chromium para todo el archivo: se lanza en el primer test que lo pide
    (con el bloqueo de red de conftest ya activo) y el resultado se reutiliza."""
    if not _CACHE:
        monkeypatch.setattr(config, "BOARD_STRATEGY_PATH", str(tmp_path / "bs.json"))
        _CACHE.update(asyncio.run(_extraer_con_chromium(tmp_path)))
    return _CACHE


class TestCaminoReal:
    def test_los_tres_cards_se_extraen(self, extraidos):
        assert set(extraidos["con_flag"]) == {T_NETBOX, T_LIMA, T_RESEND}

    def test_ubicacion_en_todos_y_ninguna_es_un_conteo(self, extraidos):
        for titulo, job in extraidos["con_flag"].items():
            assert job.location_raw, f"sin ubicación: {titulo}"
            assert not CONTEO.fullmatch(job.location_raw), job.location_raw

    def test_multi_sede_trae_la_lista_del_tooltip(self, extraidos):
        assert extraidos["con_flag"][T_NETBOX].location_raw == LISTA_NETBOX
        assert extraidos["con_flag"][T_RESEND].location_raw == LISTA_RESEND

    def test_una_sede_conserva_el_texto_visible(self, extraidos):
        assert extraidos["con_flag"][T_LIMA].location_raw == "la ciudad del candidato, PER"

    def test_sin_el_flag_el_selector_solo_ve_el_conteo(self, extraidos):
        # Por qué existe el flag: el selector apunta al elemento correcto, pero
        # su texto visible en un aviso multi-sede es un número, no un lugar.
        sin = extraidos["sin_flag"]
        assert sin[T_NETBOX].location_raw == "12 Locations"
        assert sin[T_RESEND].location_raw == "14 Locations"
        assert sin[T_LIMA].location_raw == "la ciudad del candidato, PER"

    def test_titulo_y_url_no_cambian(self, extraidos):
        urls = {t: j.url for t, j in extraidos["con_flag"].items()}
        assert urls == {
            T_NETBOX: "https://builtin.com/job/senior-site-reliability-engineer-netbox-delivery/11407311",
            T_LIMA: "https://builtin.com/job/senior-software-engineer-flask-react-remote-latin-america/10569999",
            T_RESEND: "https://builtin.com/job/software-engineer-backend/11256944",
        }
        for j in extraidos["con_flag"].values():
            assert j.source_domain == "builtin.com"

    def test_empresa_sale_del_enlace_company_title(self, extraidos):
        # Al verificar la ubicación apareció esto: el primer a[href*='/company/']
        # del card es el logo, sin texto, y los 50 cards medidos caían al
        # placeholder. La empresa es la clave de respaldo si la URL se pudre.
        empresas = {t: j.company for t, j in extraidos["con_flag"].items()}
        assert empresas == {
            T_NETBOX: "NetBox Labs",
            T_LIMA: "Bluelight Consulting",
            T_RESEND: "Resend",
        }


# =============================================================================
# Cards REALES de builtin.com — outerHTML del DOM renderizado con el Playwright
# del proyecto el 2026-09-28. No editar a mano: si el board cambia, se vuelve a
# copiar de una página viva y se re-mide.
# =============================================================================

CARDS_REALES = r"""<!-- https://builtin.com/jobs/remote?search=python · card 0 · multi-sede, lista con LATAM y 'Remote, OR, USA' -->
<div id="job-card-11407311" data-id="job-card" x-data="jobTracking" x-init="initObserver(11407311, true)" style="border-left: 6px solid #b7bac9" class="job-bounded-responsive  position-relative bg-white p-md rounded-3"><div id="main" class="row"><my-item x-show="!showSavedTag" entity-type="job" entity-id="11407311" class="h-2xl p-sm w-2xl fs-2xl z-1 job-card-my-item initialized" status="unsaved"></my-item><div class="col-12 col-lg-7 left-side-tile"><div class="align-self-center left-side-tile-item-1"><a href="/company/netbox-labs" target="_blank" class="align-items-center text-pretty-blue z-1"><picture class="d-inline-block"><img data-id="company-img" data-builtin-track-job-id="11407311" src="https://cdn.builtin.com/cdn-cgi/image/f=auto,fit=scale-down,w=128,h=128/sites/www.builtin.com/files/2025-02/netboxlabs_logo.JPEG" alt="NetBox Labs Logo" loading="lazy" height="72" width="72" class="p-xs me-md min-h-3xl object-fit-contain"></picture></a></div>
            <div class="left-side-tile-item-2"><a href="/company/netbox-labs" target="_blank" data-id="company-title" data-builtin-track-job-id="11407311" class="font-barlow fw-medium fs-xl d-inline-block m-0 text-pretty-blue hover-underline cursor-pointer z-1"><span>NetBox Labs</span></a></div>
            <div class="left-side-tile-item-3"><h2 class="font-barlow fw-medium fs-xl fs-xl-2xl mb-sm mb-lg-0"><a href="/job/senior-site-reliability-engineer-netbox-delivery/11407311" target="_blank" data-id="job-card-title" data-alias="/job/senior-site-reliability-engineer-netbox-delivery/11407311" data-builtin-track-job-id="11407311" class="card-alias-after-overlay text-break">Senior Site Reliability Engineer, NetBox Delivery</a></h2></div></div>
        <div class="col-12 col-lg-5 bounded-attribute-section d-flex align-items-start align-items-lg-center fs-md flex-column flex-lg-row"><div class="d-flex flex-grow-1 gap-lg"><div class="d-flex flex-column gap-0 fill-even"><div class="d-flex align-items-start gap-sm position-relative"><span x-show="!showSavedTag" class="fs-xs fw-bold bg-gray-01 font-Montserrat text-gray-03 rounded-1 py-xs px-sm"><i class="fa-regular fa-clock fs-xs text-gray-03 d-inline-block me-sm d-lg-none d-xl-inline-block" aria-hidden="true"></i>Yesterday</span><span x-show="showSavedTag === true" class="fs-xs fw-bold font-Montserrat rounded-1 py-xs px-sm text-soft-pink-02 bg-soft-pink-01" style="display: none;"><i class="fa-solid fa-heart fs-xs d-inline-block me-sm text-soft-pink-02" aria-hidden="true"></i>Saved
                            </span></div><div class="d-flex gap-md"><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-house-building fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">In-Office or Remote</span></div></div><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-location-dot fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                            <div><span class="font-barlow text-gray-04 text-decoration-underline z-1 cursor-pointer" data-bs-toggle="tooltip" data-bs-placement="bottom" data-bs-html="true" role="tooltip" aria-label="Job locations" data-bs-title="<div class='text-truncate'>Chile</div><div class='text-truncate'>Per&amp;#250;</div><div class='text-truncate'>Guyana</div><div class='text-truncate'>Brazil</div><div class='text-truncate'>Ecuador</div><div class='text-truncate'>Uruguay</div><div class='text-truncate'>Colombia</div><div class='text-truncate'>Suriname</div><div class='text-truncate'>Bolivia</div><div class='text-truncate'>Paraguay</div><div class='text-truncate'>Argentina</div><div class='text-truncate'>Remote, OR, USA</div>">12 Locations
                                    </span></div></div></div><div class="d-none d-xl-block fill-even"><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-sack-dollar fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">75K-195K Annually</span></div><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-trophy fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">Senior level</span></div></div></div></div>
        <div class="position-relative"><div class="position-absolute z-1 double-arrow-container"><button type="button" id="job-dropdown-button" data-id="job-dropdown-button" aria-label="Collapse Button" data-bs-toggle="collapse" data-bs-target="#drop-data-11407311" class="btn btn-link text-decoration-none d-flex justify-content-end h-2xl w-2xl align-items-center justify-content-center mt-xs collapsed fs-xl"><i data-id="job-dropdown-button" class="fa-solid fa-angles-down text-midnight d-none" aria-hidden="true"></i>
                    <i data-id="job-dropdown-button" class="fa-solid fa-angles-up text-pretty-blue d-block" aria-hidden="true"></i></button></div></div></div>
    <div id="drop-data-11407311" data-job-id="11407311" class="collapse"><div class="pt-md border-top border-gray-02 mt-md mt-lg-lg"><div class="d-flex flex-column flex-md-row gap-sm d-xl-none mb-md"><div class="d-flex align-items-center"><div class="w-md d-flex justify-content-center align-items-center me-sm"><i class="fa-regular fa-sack-dollar fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                        <span class="font-barlow text-gray-04">75K-195K Annually</span></div><div class="d-flex align-items-center"><div class="w-md d-flex justify-content-center align-items-center me-sm"><i class="fa-regular fa-trophy fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                        <span class="font-barlow text-gray-04">Senior level</span></div></div><div class="mb-md fs-xs fw-bold">Cloud • Software</div><div class="fs-sm fw-regular mb-md text-gray-04">Own NetBox’s build and release pipeline from image creation through Cloud and Enterprise deployment. Improve Django and PostgreSQL performance, establish observability and SLOs, strengthen software supply chain security, and support SOC 2 compliance. Participate in on-call rotations, incident response, and postmortems. Drive cross-team migrations and release processes while contributing fixes to NetBox Core when reliability issues originate in the application.</div><div class="border rounded-2 border-gray-01 mt-md position-relative p-sm d-block d-md-flex justify-content-between"><div class="d-block align-items-start align-items-md-center"><span class="fs-xs fw-bold text-uppercase text-gray-04 flex-shrink-0">Top Skills:</span>
                        <span class="d-md-inline ps-md-sm"><span class="fs-xs text-gray-04 mx-sm">Argocd</span><span class="fs-xs text-gray-04 mx-sm">Aws Ec2</span><span class="fs-xs text-gray-04 mx-sm">Aws Iam</span><span class="fs-xs text-gray-04 mx-sm">Aws Rds</span><span class="fs-xs text-gray-04 mx-sm">Aws Vpc</span><span class="fs-xs text-gray-04 mx-sm">Claude Code</span><span class="fs-xs text-gray-04 mx-sm">Cosign</span><span class="fs-xs text-gray-04 mx-sm">Django</span><span class="fs-xs text-gray-04 mx-sm">Fluxcd</span><span class="fs-xs text-gray-04 mx-sm">Github Actions</span><span class="fs-xs text-gray-04 mx-sm">Grafana</span><span class="fs-xs text-gray-04 mx-sm">Helm</span><span class="fs-xs text-gray-04 mx-sm">Kubernetes</span><span class="fs-xs text-gray-04 mx-sm">Postgres</span><span class="fs-xs text-gray-04 mx-sm">Prometheus</span><span class="fs-xs text-gray-04 mx-sm">Python</span><span class="fs-xs text-gray-04 mx-sm">Sigstore</span><span class="fs-xs text-gray-04 mx-sm">Slsa</span><span class="fs-xs text-gray-04 mx-sm">Terraform</span></span></div></div></div></div></div>
<!-- https://builtin.com/jobs/remote?search=python · card 10 · una sola sede, sin tooltip -->
<div id="job-card-10569999" data-id="job-card" x-data="jobTracking" x-init="initObserver(10569999, true)" style="border-left: 6px solid #b7bac9" class="job-bounded-responsive  position-relative bg-white p-md rounded-3"><div id="main" class="row"><my-item x-show="!showSavedTag" entity-type="job" entity-id="10569999" class="h-2xl p-sm w-2xl fs-2xl z-1 job-card-my-item initialized" status="unsaved"></my-item><div class="col-12 col-lg-7 left-side-tile"><div class="align-self-center left-side-tile-item-1"><a href="/company/bluelight-consulting" target="_blank" class="align-items-center text-pretty-blue z-1"><picture class="d-inline-block"><img data-id="company-img" data-builtin-track-job-id="10569999" src="https://cdn.builtin.com/cdn-cgi/image/f=auto,fit=scale-down,w=128,h=128/sites/www.builtin.com/files/2024-02/Bluelight%20Consulting.jpeg" alt="Bluelight Consulting Logo" loading="lazy" height="72" width="72" class="p-xs me-md min-h-3xl object-fit-contain"></picture></a></div>
            <div class="left-side-tile-item-2"><a href="/company/bluelight-consulting" target="_blank" data-id="company-title" data-builtin-track-job-id="10569999" class="font-barlow fw-medium fs-xl d-inline-block m-0 text-pretty-blue hover-underline cursor-pointer z-1"><span>Bluelight Consulting</span></a></div>
            <div class="left-side-tile-item-3"><h2 class="font-barlow fw-medium fs-xl fs-xl-2xl mb-sm mb-lg-0"><a href="/job/senior-software-engineer-flask-react-remote-latin-america/10569999" target="_blank" data-id="job-card-title" data-alias="/job/senior-software-engineer-flask-react-remote-latin-america/10569999" data-builtin-track-job-id="10569999" class="card-alias-after-overlay text-break">Senior Software Engineer (Python/Flask+React) - Remote, Latin America</a></h2></div></div>
        <div class="col-12 col-lg-5 bounded-attribute-section d-flex align-items-start align-items-lg-center fs-md flex-column flex-lg-row"><div class="d-flex flex-grow-1 gap-lg"><div class="d-flex flex-column gap-0 fill-even"><div class="d-flex align-items-start gap-sm position-relative"><span x-show="!showSavedTag" class="fs-xs fw-bold bg-gray-01 font-Montserrat text-gray-03 rounded-1 py-xs px-sm"><i class="fa-regular fa-clock fs-xs text-gray-03 d-inline-block me-sm d-lg-none d-xl-inline-block" aria-hidden="true"></i>Reposted 2 Days Ago</span><span x-show="showSavedTag === true" class="fs-xs fw-bold font-Montserrat rounded-1 py-xs px-sm text-soft-pink-02 bg-soft-pink-01" style="display: none;"><i class="fa-solid fa-heart fs-xs d-inline-block me-sm text-soft-pink-02" aria-hidden="true"></i>Saved
                            </span></div><div class="d-flex gap-md"><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-house-building fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">In-Office or Remote</span></div></div><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-location-dot fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                            <div><span class="font-barlow text-gray-04">la ciudad del candidato, PER</span></div></div></div><div class="d-none d-xl-block fill-even"><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-trophy fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">Senior level</span></div></div></div></div>
        <div class="position-relative"><div class="position-absolute z-1 double-arrow-container"><button type="button" id="job-dropdown-button" data-id="job-dropdown-button" aria-label="Collapse Button" data-bs-toggle="collapse" data-bs-target="#drop-data-10569999" class="btn btn-link text-decoration-none d-flex justify-content-end h-2xl w-2xl align-items-center justify-content-center mt-xs collapsed fs-xl"><i data-id="job-dropdown-button" class="fa-solid fa-angles-down text-midnight d-none" aria-hidden="true"></i>
                    <i data-id="job-dropdown-button" class="fa-solid fa-angles-up text-pretty-blue d-block" aria-hidden="true"></i></button></div></div></div>
    <div id="drop-data-10569999" data-job-id="10569999" class="collapse"><div class="pt-md border-top border-gray-02 mt-md mt-lg-lg"><div class="d-flex flex-column flex-md-row gap-sm d-xl-none mb-md"><div class="d-flex align-items-center"><div class="w-md d-flex justify-content-center align-items-center me-sm"><i class="fa-regular fa-trophy fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                        <span class="font-barlow text-gray-04">Senior level</span></div></div><div class="mb-md fs-xs fw-bold">Professional Services • Software</div><div class="fs-sm fw-regular mb-md text-gray-04">Architect, test, and ship end-to-end full-stack applications using Flask and React for complex financial reporting and data visualization. Continuously refactor and optimize software within Agile processes. Collaborate with product managers and data engineering to safely extract, parse, and serve large-scale private asset analytics.</div><div class="border rounded-2 border-gray-01 mt-md position-relative p-sm d-block d-md-flex justify-content-between"><div class="d-block align-items-start align-items-md-center"><span class="fs-xs fw-bold text-uppercase text-gray-04 flex-shrink-0">Top Skills:</span>
                        <span class="d-md-inline ps-md-sm"><span class="fs-xs text-gray-04 mx-sm">Agile</span><span class="fs-xs text-gray-04 mx-sm">Celery</span><span class="fs-xs text-gray-04 mx-sm">Flask</span><span class="fs-xs text-gray-04 mx-sm">JavaScript</span><span class="fs-xs text-gray-04 mx-sm">Postgres</span><span class="fs-xs text-gray-04 mx-sm">Python</span><span class="fs-xs text-gray-04 mx-sm">React</span><span class="fs-xs text-gray-04 mx-sm">Spa</span><span class="fs-xs text-gray-04 mx-sm">Sqlalchemy</span><span class="fs-xs text-gray-04 mx-sm">Typescript</span></span></div></div></div></div></div>
<!-- https://builtin.com/jobs/remote?search=backend · card 8 · multi-sede, lista con Canada y USA -->
<div id="job-card-11256944" data-id="job-card" x-data="jobTracking" x-init="initObserver(11256944, true)" style="border-left: 6px solid #1708FF" class="job-bounded-responsive  position-relative bg-white p-md rounded-3"><div id="main" class="row"><my-item x-show="!showSavedTag" entity-type="job" entity-id="11256944" class="h-2xl p-sm w-2xl fs-2xl z-1 job-card-my-item initialized" status="unsaved"></my-item><div class="col-12 col-lg-7 left-side-tile"><div class="align-self-center left-side-tile-item-1"><a href="/company/resend" target="_blank" class="align-items-center text-pretty-blue z-1"><picture class="d-inline-block"><img data-id="company-img" data-builtin-track-job-id="11256944" src="https://cdn.builtin.com/cdn-cgi/image/f=auto,fit=scale-down,w=128,h=128/sites/www.builtin.com/files/2024-12/Resend.jpeg" alt="Resend Logo" loading="lazy" height="72" width="72" class="p-xs me-md min-h-3xl object-fit-contain"></picture></a></div>
            <div class="left-side-tile-item-2"><a href="/company/resend" target="_blank" data-id="company-title" data-builtin-track-job-id="11256944" class="font-barlow fw-medium fs-xl d-inline-block m-0 text-pretty-blue hover-underline cursor-pointer z-1"><span>Resend</span></a></div>
            <div class="left-side-tile-item-3"><h2 class="font-barlow fw-medium fs-xl fs-xl-2xl mb-sm mb-lg-0"><a href="/job/software-engineer-backend/11256944" target="_blank" data-id="job-card-title" data-alias="/job/software-engineer-backend/11256944" data-builtin-track-job-id="11256944" class="card-alias-after-overlay text-break">Software Engineer, Backend</a></h2></div></div>
        <div class="col-12 col-lg-5 bounded-attribute-section d-flex align-items-start align-items-lg-center fs-md flex-column flex-lg-row"><div class="d-flex flex-grow-1 gap-lg"><div class="d-flex flex-column gap-0 fill-even"><div class="d-flex align-items-start gap-sm position-relative"><span x-show="!showSavedTag" class="fs-xs fw-bold bg-gray-01 font-Montserrat text-gray-03 rounded-1 py-xs px-sm"><i class="fa-regular fa-clock fs-xs text-gray-03 d-inline-block me-sm d-lg-none d-xl-inline-block" aria-hidden="true"></i>11 Days Ago</span><span x-show="showSavedTag === true" class="fs-xs fw-bold font-Montserrat rounded-1 py-xs px-sm text-soft-pink-02 bg-soft-pink-01" style="display: none;"><i class="fa-solid fa-heart fs-xs d-inline-block me-sm text-soft-pink-02" aria-hidden="true"></i>Saved
                            </span></div><div class="d-flex gap-md"><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-house-building fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">Remote</span></div></div><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-location-dot fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                            <div><span class="font-barlow text-gray-04 text-decoration-underline z-1 cursor-pointer" data-bs-toggle="tooltip" data-bs-placement="bottom" data-bs-html="true" role="tooltip" aria-label="Job locations" data-bs-title="<div class='text-truncate'>Chile</div><div class='text-truncate'>Per&amp;#250;</div><div class='text-truncate'>Canada</div><div class='text-truncate'>USA</div><div class='text-truncate'>Guyana</div><div class='text-truncate'>Brazil</div><div class='text-truncate'>Ecuador</div><div class='text-truncate'>M&amp;#233;xico</div><div class='text-truncate'>Uruguay</div><div class='text-truncate'>Colombia</div><div class='text-truncate'>Suriname</div><div class='text-truncate'>Bolivia</div><div class='text-truncate'>Paraguay</div><div class='text-truncate'>Argentina</div>">14 Locations
                                    </span></div></div></div><div class="d-none d-xl-block fill-even"><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-sack-dollar fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">140K-180K Annually</span></div><div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-trophy fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                                <span class="font-barlow text-gray-04">Senior level</span></div></div></div></div>
        <div class="position-relative"><div class="position-absolute z-1 double-arrow-container"><button type="button" id="job-dropdown-button" data-id="job-dropdown-button" aria-label="Collapse Button" data-bs-toggle="collapse" data-bs-target="#drop-data-11256944" class="btn btn-link text-decoration-none d-flex justify-content-end h-2xl w-2xl align-items-center justify-content-center mt-xs collapsed fs-xl"><i data-id="job-dropdown-button" class="fa-solid fa-angles-down text-midnight d-none" aria-hidden="true"></i>
                    <i data-id="job-dropdown-button" class="fa-solid fa-angles-up text-pretty-blue d-block" aria-hidden="true"></i></button></div></div></div>
    <div id="drop-data-11256944" data-job-id="11256944" class="collapse"><div class="pt-md border-top border-gray-02 mt-md mt-lg-lg"><div class="d-flex flex-column flex-md-row gap-sm d-xl-none mb-md"><div class="d-flex align-items-center"><div class="w-md d-flex justify-content-center align-items-center me-sm"><i class="fa-regular fa-sack-dollar fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                        <span class="font-barlow text-gray-04">140K-180K Annually</span></div><div class="d-flex align-items-center"><div class="w-md d-flex justify-content-center align-items-center me-sm"><i class="fa-regular fa-trophy fs-xs text-pretty-blue" aria-hidden="true"></i></div>
                        <span class="font-barlow text-gray-04">Senior level</span></div></div><div class="mb-md fs-xs fw-bold">Software</div><div class="fs-sm fw-regular mb-md text-gray-04">Build and operate reliable backend services for a developer-focused email platform. Responsibilities include owning features end-to-end, designing APIs and relational data models, scaling databases, building distributed systems with queues and background workers, managing AWS infrastructure, improving observability and deployments, participating in on-call, and partnering with customers and support to improve the product.</div><div class="border rounded-2 border-gray-01 mt-md position-relative p-sm d-block d-md-flex justify-content-between"><div class="d-block align-items-start align-items-md-center"><span class="fs-xs fw-bold text-uppercase text-gray-04 flex-shrink-0">Top Skills:</span>
                        <span class="d-md-inline ps-md-sm"><span class="fs-xs text-gray-04 mx-sm">APIs</span><span class="fs-xs text-gray-04 mx-sm">AWS</span><span class="fs-xs text-gray-04 mx-sm">Aws Cdk</span><span class="fs-xs text-gray-04 mx-sm">Container-Based Infrastructure</span><span class="fs-xs text-gray-04 mx-sm">Distributed Systems</span><span class="fs-xs text-gray-04 mx-sm">Go</span><span class="fs-xs text-gray-04 mx-sm">Infrastructure As Code</span><span class="fs-xs text-gray-04 mx-sm">Node.js</span><span class="fs-xs text-gray-04 mx-sm">Observability</span><span class="fs-xs text-gray-04 mx-sm">Postgres</span><span class="fs-xs text-gray-04 mx-sm">Relational Databases</span><span class="fs-xs text-gray-04 mx-sm">Serverless Infrastructure</span><span class="fs-xs text-gray-04 mx-sm">Terraform</span><span class="fs-xs text-gray-04 mx-sm">Typescript</span></span></div></div></div></div></div>"""
