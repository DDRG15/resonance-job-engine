"""
tests/test_ats.py — los ATS como clase de fuente (Fase 2).

Cero red: los payloads son recortes reales verificados el 2026-09-15 contra
boards-api.greenhouse.io, api.lever.co y api.ashbyhq.com.
"""

import asyncio
import json

import pytest

import ats_registry
import ats_scraper
import endpoint_scraper as es
import selectors_registry
from ats_registry import FuenteATS


# ── Recortes reales ──────────────────────────────────────────────────────────

GREENHOUSE = json.dumps({"jobs": [
    {"title": "AI Engineer", "company_name": "GitLab",
     "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/8556658002",
     "location": {"name": "Remote, Bangalore"}, "application_deadline": None},
    {"title": "Chef de Cuisine", "company_name": "GitLab",
     "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/2",
     "location": {"name": "Remote"}},
]}).encode()

LEVER = json.dumps([
    {"text": "Android Engineer - Experience",
     "hostedUrl": "https://jobs.lever.co/spotify/2193db3f",
     "applyUrl": "https://jobs.lever.co/spotify/2193db3f/apply",
     "country": "GB",
     "categories": {"commitment": "Permanent", "department": "Engineering",
                    "location": "London", "team": "Experience"}},
]).encode()

ASHBY = json.dumps({"jobs": [
    {"title": "Senior / Staff Fullstack Engineer",
     "jobUrl": "https://jobs.ashbyhq.com/linear/d3bc1ced",
     "applyUrl": "https://jobs.ashbyhq.com/linear/d3bc1ced/application",
     "location": "Europe", "isRemote": True, "employmentType": "FullTime"},
]}).encode()


class RespuestaFalsa:
    def __init__(self, status, contenido=b"", headers=None):
        self.status_code, self.content = status, contenido
        self.headers = headers or {}


class SesionFalsa:
    def __init__(self, *respuestas):
        self.cola, self.pedidos, self.headers = list(respuestas), [], {}

    def get(self, url, timeout=None):
        self.pedidos.append(url)
        if not self.cola:
            raise AssertionError(f"pedido inesperado a {url}")
        return self.cola.pop(0)


@pytest.fixture(autouse=True)
def _sin_dormir(monkeypatch):
    monkeypatch.setattr(es, "_dormir", lambda s: None)


# ── El registro ──────────────────────────────────────────────────────────────

class TestRegistro:
    def test_toda_fuente_apunta_a_una_plataforma_conocida(self):
        for f in ats_registry.FUENTES:
            assert f.plataforma in ats_registry.PLATAFORMAS, f.token

    def test_no_hay_tokens_repetidos_dentro_de_una_plataforma(self):
        """Un token duplicado son dos pedidos idénticos y avisos contados dos veces."""
        vistos = set()
        for f in ats_registry.FUENTES:
            clave = (f.plataforma, f.token)
            assert clave not in vistos, clave
            vistos.add(clave)

    def test_toda_fuente_declara_nombre_de_empresa(self):
        """Lever y Ashby no lo traen en el payload: si falta acá, los avisos entran
        como "(empresa no listada)", que es la basura que la Fase 1 vino a eliminar."""
        for f in ats_registry.FUENTES:
            assert f.empresa.strip(), f"{f.plataforma}:{f.token}"

    def test_toda_plataforma_mapea_titulo_y_url(self):
        for nombre, p in ats_registry.PLATAFORMAS.items():
            assert p.field_map.get("title"), nombre
            assert p.field_map.get("url"), nombre

    def test_lever_respeta_el_crawl_delay_que_declara_su_robots(self):
        """api.lever.co publica `Crawl-delay: 1`. Es un límite, no una sugerencia."""
        lever = ats_registry.PLATAFORMAS["lever"]
        assert lever.crawl_delay_s >= 1.0
        assert lever.concurrencia == 1

    def test_hay_al_menos_una_fuente_activa_por_plataforma(self):
        activas = ats_registry.activas()
        assert {f.plataforma for f in activas} == set(ats_registry.PLATAFORMAS)

    def test_el_dominio_es_la_plataforma_no_la_empresa(self):
        """Agrupar por empresa daría cuarenta filas de una fila cada una. Lo que
        importa saber es si la integración trae avisos, no si Dropbox publicó hoy."""
        assert ats_registry.dominio_de("greenhouse") == "boards.greenhouse.io"
        assert ats_registry.dominio_de("lever") == "jobs.lever.co"
        assert ats_registry.dominio_de("ashby") == "jobs.ashbyhq.com"

    def test_el_tablero_de_demo_de_lever_esta_apagado(self):
        demo = [f for f in ats_registry.FUENTES if f.token == "leverdemo"]
        assert demo and demo[0].activa is False


# ── Una fuente ───────────────────────────────────────────────────────────────

class TestTraerFuente:
    def test_greenhouse_mapea_los_campos_reales(self, monkeypatch):
        ses = SesionFalsa(RespuestaFalsa(200, GREENHOUSE))
        monkeypatch.setattr(es, "fetch",
                            lambda url, **kw: ses.get(url).content)
        r = ats_scraper.traer_fuente(
            FuenteATS("greenhouse", "gitlab", "GitLab"),
            es_relevante=lambda t: "engineer" in t.lower())
        assert len(r.jobs) == 1
        j = r.jobs[0]
        assert j.title == "AI Engineer"
        assert j.company == "GitLab"
        assert j.location_raw == "Remote, Bangalore"
        assert j.url.endswith("/8556658002")
        assert j.source_domain == "boards.greenhouse.io"

    def test_lever_toma_el_titulo_de_text_y_la_ubicacion_anidada(self, monkeypatch):
        monkeypatch.setattr(es, "fetch", lambda url, **kw: LEVER)
        r = ats_scraper.traer_fuente(FuenteATS("lever", "spotify", "Spotify"))
        j = r.jobs[0]
        assert j.title == "Android Engineer - Experience"
        assert j.location_raw == "London"

    def test_lever_toma_la_empresa_del_registro_porque_el_payload_no_la_trae(
            self, monkeypatch):
        monkeypatch.setattr(es, "fetch", lambda url, **kw: LEVER)
        r = ats_scraper.traer_fuente(FuenteATS("lever", "spotify", "Spotify"))
        assert r.jobs[0].company == "Spotify"

    def test_ashby_usa_jobUrl_y_no_applyUrl(self, monkeypatch):
        """`applyUrl` es el formulario. Usarlo como URL del aviso rompería la
        deduplicación contra el resto del sistema."""
        monkeypatch.setattr(es, "fetch", lambda url, **kw: ASHBY)
        r = ats_scraper.traer_fuente(FuenteATS("ashby", "linear", "Linear"))
        assert r.jobs[0].url == "https://jobs.ashbyhq.com/linear/d3bc1ced"
        assert "/application" not in r.jobs[0].url

    def test_el_token_va_a_la_url(self, monkeypatch):
        pedidas = []
        monkeypatch.setattr(es, "fetch",
                            lambda url, **kw: (pedidas.append(url), ASHBY)[1])
        ats_scraper.traer_fuente(FuenteATS("ashby", "posthog", "PostHog"))
        assert pedidas == ["https://api.ashbyhq.com/posting-api/job-board/posthog"]

    def test_un_403_queda_en_error_y_no_lanza(self, monkeypatch):
        def _explota(url, **kw):
            raise es.EndpointNoPublico("HTTP 403")
        monkeypatch.setattr(es, "fetch", _explota)
        r = ats_scraper.traer_fuente(FuenteATS("ashby", "x", "X"))
        assert r.jobs == []
        assert "EndpointNoPublico" in r.error


# ── Todas las fuentes ────────────────────────────────────────────────────────

class TestTraerTodo:
    def test_una_fuente_que_explota_no_tumba_a_las_demas(self, monkeypatch):
        """Riesgo R1: una rama que lanza no puede matar el fan-out entero."""
        def _fuente(f, **kw):
            if f.token == "malo":
                raise RuntimeError("boom")
            r = ats_scraper.ResultadoATS(f)
            r.items_crudos = 1
            return r

        monkeypatch.setattr(ats_scraper, "traer_fuente", _fuente)
        fuentes = [FuenteATS("ashby", "bueno", "B"),
                   FuenteATS("ashby", "malo", "M"),
                   FuenteATS("ashby", "otro", "O")]
        res = asyncio.run(ats_scraper.traer_todo(fuentes))
        assert len(res) == 3
        fallida = [r for r in res if r.fuente.token == "malo"][0]
        assert "RuntimeError" in fallida.error
        assert all(not r.error for r in res if r.fuente.token != "malo")

    def test_sin_fuentes_no_hace_nada(self):
        assert asyncio.run(ats_scraper.traer_todo([])) == []

    def test_respeta_el_orden_de_entrada_en_la_salida(self, monkeypatch):
        """El resultado se zipea con la entrada: si el orden se perdiera, un error se
        atribuiría a la fuente equivocada."""
        monkeypatch.setattr(ats_scraper, "traer_fuente",
                            lambda f, **kw: ats_scraper.ResultadoATS(f))
        fuentes = [FuenteATS("ashby", f"t{i}", f"E{i}") for i in range(5)]
        res = asyncio.run(ats_scraper.traer_todo(fuentes))
        assert [r.fuente.token for r in res] == [f.token for f in fuentes]


class TestDominiosActivos:
    def test_las_plataformas_ats_entran_a_active_domains(self):
        activos = selectors_registry.active_domains()
        for d in ats_registry.dominios():
            assert d in activos, d

    def test_el_scraper_las_aparta_del_fanout(self):
        from scraper import GemaScraper
        pedidos = ats_registry.dominios() + ["remoteok.com", "jobspresso.co"]
        apartados = GemaScraper._ats_dominios_pedidos(pedidos)
        assert set(apartados) == set(ats_registry.dominios())
        assert "jobspresso.co" not in apartados
