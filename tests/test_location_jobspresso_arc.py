"""
Ubicacion (y empresa) de jobspresso.co y arc.dev, congeladas contra HTML REAL.

Los dos boards salian con ubicacion al 0% en la corrida del 2026-09-17 teniendo el
dato en el card. Medido en vivo el 2026-09-28 por el camino real
(GemaScraper._extract_all_cards), viejo contra nuevo sobre la misma pagina:

    jobspresso.co   location 0/30 -> 30/30   title+url identicos 30/30
    arc.dev         location 0/6  -> 6/6     empresa placeholder 6 -> 0

Los tres cards de cada board se copiaron del DOM renderizado (sin los <svg>).
"""

import asyncio
import queue
from unittest.mock import MagicMock

import pytest

import selectors_registry
from database import GemaDatabase
from models import SearchConfig
from scraper import GemaScraper

JOBSPRESSO_CARDS = r"""<li id="job_listing-163414" class="job_listing job-type-product-mgmt post-163414 type-job_listing status-publish has-post-thumbnail hentry job_listing_category-full-time job_listing_type-product-mgmt job-type-product" data-title="Senior Staff Product Manager, Device Security at 1Password" data-href="https://jobspresso.co/job/senior-staff-product-manager-device-security/" style="visibility: visible;">
	<a href="https://jobspresso.co/job/senior-staff-product-manager-device-security/" class="job_listing-clickbox"></a>

	<div class="job_listing-logo">
		<img class="company_logo" src="https://jobspresso.co/wp-content/uploads/nkB3lOvM_400x4001-150x150.png" alt="1Password">	</div><div class="job_listing-about">

		<div class="job_listing-position job_listing__column">
			<h3 class="job_listing-title">Senior Staff Product Manager, Device Security</h3>

			<div class="job_listing-company">
				<strong>1Password</strong> 				<span class="job_listing-company-tagline">The best way to keep you, your family and business safe online. Go ahead. Forget your passwords.</span>			</div>
		</div>

		<div class="job_listing-location job_listing__column">
			<a class="google_map_link" href="http://maps.google.com/maps?q=United+States%2C+Canada&amp;zoom=14&amp;size=512x512&amp;maptype=roadmap&amp;sensor=false" target="_blank">United States, Canada</a>		</div>

		<ul class="job_listing-meta job_listing__column">
			
							<li class="job_listing-type job-type product-mgmt">Product</li>
			
						<li class="job_listing-date">August 28</li>
			
					</ul>

	</div>
</li>
<li id="job_listing-163388" class="job_listing job-type-developer post-163388 type-job_listing status-publish has-post-thumbnail hentry job_listing_category-full-time job_listing_type-developer job-type-engineer" data-title="Senior Software Engineer - k6 Core at Grafana Labs" data-href="https://jobspresso.co/job/senior-software-engineer-k6-core/" style="visibility: visible;">
	<a href="https://jobspresso.co/job/senior-software-engineer-k6-core/" class="job_listing-clickbox"></a>

	<div class="job_listing-logo">
		<img class="company_logo" src="https://jobspresso.co/wp-content/uploads/HxPK_45O_400x4001-150x150.jpg" alt="Grafana Labs">	</div><div class="job_listing-about">

		<div class="job_listing-position job_listing__column">
			<h3 class="job_listing-title">Senior Software Engineer – k6 Core</h3>

			<div class="job_listing-company">
				<strong>Grafana Labs</strong> 				<span class="job_listing-company-tagline">Understand all of your metrics with dashboards that are easy to create and share.</span>			</div>
		</div>

		<div class="job_listing-location job_listing__column">
			<a class="google_map_link" href="http://maps.google.com/maps?q=United+States+%28EST+Time+Zone%29&amp;zoom=14&amp;size=512x512&amp;maptype=roadmap&amp;sensor=false" target="_blank">United States (EST Time Zone)</a>		</div>

		<ul class="job_listing-meta job_listing__column">
			
							<li class="job_listing-type job-type developer">Engineer</li>
			
						<li class="job_listing-date">July 30</li>
			
					</ul>

	</div>
</li>
<li id="job_listing-163358" class="job_listing job-type-marketing post-163358 type-job_listing status-publish has-post-thumbnail hentry job_listing_category-full-time job_listing_type-marketing" data-title="Back End Engineer, PHP at Airalo" data-href="https://jobspresso.co/job/back-end-engineer-php/" style="visibility: visible;">
	<a href="https://jobspresso.co/job/back-end-engineer-php/" class="job_listing-clickbox"></a>

	<div class="job_listing-logo">
		<img class="company_logo" src="https://jobspresso.co/wp-content/uploads/4-NMg9_Q_400x4001-150x150.jpg" alt="Airalo">	</div><div class="job_listing-about">

		<div class="job_listing-position job_listing__column">
			<h3 class="job_listing-title">Back End Engineer, PHP</h3>

			<div class="job_listing-company">
				<strong>Airalo</strong> 				<span class="job_listing-company-tagline">Alo! The world's first eSIM store, helping people connect in over 200+ countries and regions.</span>			</div>
		</div>

		<div class="job_listing-location job_listing__column">
			<a class="google_map_link" href="http://maps.google.com/maps?q=United+Kingdom%2C+Spain%2C+Dubai&amp;zoom=14&amp;size=512x512&amp;maptype=roadmap&amp;sensor=false" target="_blank">United Kingdom, Spain, Dubai</a>		</div>

		<ul class="job_listing-meta job_listing__column">
			
							<li class="job_listing-type job-type marketing">Marketing</li>
			
						<li class="job_listing-date">July 8</li>
			
					</ul>

	</div>
</li>"""

ARC_CARDS = r"""<div class="sc-6ac3e31c-0 fHAFFE job-card" data-testid="job-card" data-track-resource="job-card" data-job-random-key="pnngcchreu" data-arc-job="true"><div class="company-logo"><div class="sc-5e338ae8-0 gzUxsj company-logo-img"><img alt="Arc Exclusive’s job post on Arc’s remote job board." loading="lazy" width="56" height="56" decoding="async" data-nimg="1" style="color:transparent" srcset="/_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=64&amp;q=75 1x, /_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=128&amp;q=75 2x" src="https://arc.dev/_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=128&amp;q=75"></div></div><div class="company-name-wrapper"><div class="company-name">Arc Exclusive</div><div class="fast-apply"><span class="sc-6c7aebc8-0 gsBnPw"><span>Fast apply</span> <span class="ui__sc-1vljvh0-0 iKmaie icon icon-Lightning"><!--$--><!--/$--></span></span></div></div><a class="job-title" href="/remote-jobs/details/ios-android-bluetooth-ble-feasibility-study-developer-pnngcchreu" rel="nofollow">Ios/android Bluetooth (ble) Feasibility Study Developer</a><div class="other-info"><div class="categories"><div class="sc-6ac3e31c-1 ljiPrI job-type">Freelance</div><div class="sc-6ac3e31c-1 ljiPrI experience-level">Mid-level</div><div class="sc-6ac3e31c-1 ljiPrI contract-rate">Hourly rate</div><a href="/remote-jobs/ios" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">iOS</a><div class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">Bluetooth</div></div></div><div class="bottom-information"><span>Remote anywhere</span></div><div class="additional-info"><span class="actively-hiring">Actively hiring</span></div></div>
<div class="sc-6ac3e31c-0 fHAFFE job-card" data-testid="job-card" data-track-resource="job-card" data-job-random-key="pnbwza8npq" data-arc-job="true"><div class="company-logo"><div class="sc-5e338ae8-0 gzUxsj company-logo-img"><img alt="Arc Exclusive’s job post on Arc’s remote job board." loading="lazy" width="56" height="56" decoding="async" data-nimg="1" style="color:transparent" srcset="/_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=64&amp;q=75 1x, /_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=128&amp;q=75 2x" src="https://arc.dev/_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=128&amp;q=75"></div></div><div class="company-name-wrapper"><div class="company-name">Arc Exclusive</div><div class="fast-apply"><span class="sc-6c7aebc8-0 gsBnPw"><span>Fast apply</span> <span class="ui__sc-1vljvh0-0 iKmaie icon icon-Lightning"><!--$--><!--/$--></span></span></div></div><a class="job-title" href="/remote-jobs/details/remote-software-developer-pnbwza8npq" rel="nofollow">Remote - Software Developer</a><div class="other-info"><div class="categories"><div class="sc-6ac3e31c-1 ljiPrI job-type">Full-time</div><div class="sc-6ac3e31c-1 ljiPrI experience-level">Junior</div><div class="sc-ab809d0b-0 iOTqzP sc-6ac3e31c-4 dDuIMz"><span>US<!-- -->$50K - 100K</span></div><a href="/remote-jobs/python" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">Python</a><a href="/remote-jobs/nodejs" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">Node.js</a><a href="/remote-jobs/software-development" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">Software Development</a><a href="/remote-jobs/javascript" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">JavaScript</a><a href="/remote-jobs/aws" class="sc-6ac3e31c-1 fJhFxL category" data-testid="category">AWS</a></div></div><div class="bottom-information"><span>Remote anywhere</span></div><div class="additional-info"><span class="actively-hiring">Actively hiring</span></div></div>
<div class="sc-6ac3e31c-0 fHAFFE job-card" data-testid="job-card" data-track-resource="job-card" data-job-random-key="pmydlacrep" data-arc-job="true"><div class="company-logo"><div class="sc-5e338ae8-0 gzUxsj company-logo-img"><img alt="Arc Exclusive’s job post on Arc’s remote job board." loading="lazy" width="56" height="56" decoding="async" data-nimg="1" style="color:transparent" srcset="/_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=64&amp;q=75 1x, /_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=128&amp;q=75 2x" src="https://arc.dev/_next/image?url=https%3A%2F%2Fcdn.arc.dev%2Fimages%2Fcompanies%2Farc-exclusive-logo.png&amp;w=128&amp;q=75"></div></div><div class="company-name-wrapper"><div class="company-name">Arc Exclusive</div><div class="fast-apply"><span class="sc-6c7aebc8-0 gsBnPw"><span>Fast apply</span> <span class="ui__sc-1vljvh0-0 iKmaie icon icon-Lightning"><!--$--><!--/$--></span></span></div></div><a class="job-title" href="/remote-jobs/details/senior-gis-web-3d-developer-pmydlacrep" rel="nofollow">Senior GIS / Web-3D Developer</a><div class="other-info"><div class="categories"><div class="sc-6ac3e31c-1 ljiPrI job-type">Freelance</div><div class="sc-6ac3e31c-1 ljiPrI experience-level">Senior</div><div class="sc-6ac3e31c-1 ljiPrI contract-rate">Hourly rate</div><a href="/remote-jobs/arcgis" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">ArcGIS</a><div class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">CesiumJS</div><a href="/remote-jobs/javascript" class="sc-6ac3e31c-1 ljiPrI category" data-testid="category">JavaScript</a></div></div><div class="bottom-information"><span>Remote anywhere</span></div><div class="additional-info"><span class="actively-hiring">Actively hiring</span></div></div>"""


async def _extraer(html, dominio, tmp_dir):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium de Playwright no disponible: {exc}")
        try:
            ctx = await browser.new_context()
            await ctx.route("**/*", lambda route: route.abort())
            page = await ctx.new_page()
            await page.set_content(f"<html><body>{html}</body></html>")
            db = GemaDatabase(db_path=str(tmp_dir / "loc.db"))
            sc = GemaScraper(SearchConfig(target_titles=["developer"], target_domains=[dominio]),
                             db, queue.Queue(), webhook=MagicMock(is_enabled=False))
            return await sc._extract_all_cards(page, dominio, selectors_registry.SELECTORS[dominio])
        finally:
            await browser.close()


def test_jobspresso_ubicacion_y_empresa(tmp_path):
    jobs = asyncio.run(_extraer(JOBSPRESSO_CARDS, "jobspresso.co", tmp_path))
    assert len(jobs) == 3
    assert all(j.location_raw for j in jobs), "la ubicacion volvio a salir vacia"
    assert jobs[0].location_raw == "United States, Canada"
    # El fallback span[class*='company'] matchea el ESLOGAN; el <strong> va primero.
    assert jobs[0].company == "1Password"
    assert all("safe online" not in (j.company or "") for j in jobs)


def test_arc_ubicacion_y_empresa(tmp_path):
    jobs = asyncio.run(_extraer(ARC_CARDS, "arc.dev", tmp_path))
    assert jobs, "no se extrajo ningun card"
    assert all(j.location_raw == "Remote anywhere" for j in jobs)
    assert all(j.company == "Arc Exclusive" for j in jobs),         "la empresa volvio al alt del logo o al placeholder"


def test_los_selectores_estan_declarados():
    assert selectors_registry.SELECTORS["jobspresso.co"].location[0] ==         ".job_listing-location a.google_map_link"
    assert selectors_registry.SELECTORS["arc.dev"].location == ["div.bottom-information span"]
