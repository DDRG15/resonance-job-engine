"""
tests/test_fuentes_fase2.py — Remotive, Jobicy y Arbeitnow (, Fase 2).

Cero red. Los cuerpos son respuestas REALES capturadas el 2026-09-30 por el camino real
de `scrape_endpoint` (`tests/fixtures/fuentes/`), con las descripciones recortadas a 600
caracteres. De Arbeitnow se guardaron los 19 remotos y 6 presenciales de 326.
"""

import json
from pathlib import Path

import pytest

import endpoint_registry
import endpoint_scraper as es
from models import SearchConfig

FUENTES = Path(__file__).parent / "fixtures" / "fuentes"


def _crudo(nombre: str) -> bytes:
    return (FUENTES / nombre).read_bytes()


class SesionFalsa:
    def __init__(self, contenido):
        self.contenido = contenido
        self.pedidos = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.pedidos.append(url)
        r = type("R", (), {})()
        r.status_code, r.content, r.headers = 200, self.contenido, {}
        return r


def _correr(dominio: str, crudo: bytes, titulos=("python developer", "backend")):
    cfg = endpoint_registry.ENDPOINTS[dominio]
    ses = SesionFalsa(crudo)
    res = es.scrape_endpoint(
        dominio, endpoint_template=cfg["endpoint_template"], field_map=cfg["field_map"],
        titles=list(titulos), solo_si_campo=cfg.get("solo_si_campo", ""),
        region_del_board=cfg.get("region_del_board", ""), sesion=ses)
    return ses, res


def _veredicto(ubicacion: str) -> str:
    from matcher import score_job
    from models import JobResult
    job = JobResult(title="Backend Engineer", company="Acme", url="https://x.com/1",
                    source_domain="x.com", location_raw=ubicacion)
    return score_job(job, SearchConfig(target_titles=["Backend Engineer"])).location_verdict


# ─────────────────────────────────────────────────────────────────────────────
# Las entradas del registro
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistro:
    @pytest.mark.parametrize("dominio", ["remotive.com", "jobicy.com", "arbeitnow.com"])
    def test_feed_fijo_con_politica_abierta(self, dominio):
        """Los tres son feeds fijos: Remotive y Jobicy admiten un pedido por corrida,
        y Arbeitnow no acepta busqueda. El titulo se filtra del lado de GEMA."""
        assert "{title}" not in endpoint_registry.ENDPOINTS[dominio]["endpoint_template"]
        pol = endpoint_registry.politica_de(dominio)
        assert pol["access_policy"] == "open" and pol["policy_reason"]

    @pytest.mark.parametrize("dominio,intervalo", [("remotive.com", 6 * 3600),
                                                   ("jobicy.com", 3600)])
    def test_limite_declarado_entre_corridas(self, dominio, intervalo):
        assert endpoint_registry.ENDPOINTS[dominio]["intervalo_minimo_s"] == intervalo

    def test_la_estrategia_hereda_los_campos_nuevos(self, tmp_path):
        """Un campo que `BoardStrategy` no declare se descarta en silencio en
        `_aplicar_endpoints`: el filtro de Arbeitnow no llegaria nunca al scraper."""
        from board_strategy import AccessVector, BoardStrategyRegistry
        reg = BoardStrategyRegistry(tmp_path / "s.json")
        arb = reg.get("arbeitnow.com")
        assert arb.access_vector == AccessVector.ENDPOINT
        assert arb.solo_si_campo == "remote" and arb.region_del_board == "Europe"
        assert reg.get("jobicy.com").intervalo_minimo_s == 3600

    def test_los_dominios_nuevos_entran_a_la_busqueda(self):
        import selectors_registry
        activos = selectors_registry.active_domains()
        for d in ("himalayas.app", "remotive.com", "jobicy.com", "arbeitnow.com"):
            assert d in activos


# ─────────────────────────────────────────────────────────────────────────────
# Remotive
# ─────────────────────────────────────────────────────────────────────────────

class TestRemotive:
    CRUDO = _crudo("remotive.json")
    FILAS = json.loads(CRUDO)["jobs"]

    def test_un_solo_pedido(self):
        ses, res = _correr("remotive.com", self.CRUDO)
        assert len(ses.pedidos) == 1
        assert res.items_crudos == len(self.FILAS)

    def test_mapeo(self):
        fm = endpoint_registry.ENDPOINTS["remotive.com"]["field_map"]
        f = self.FILAS[0]
        job = es.a_job_result(f, fm, "remotive.com")
        assert job.title == f["title"] and job.company == f["company_name"]
        assert job.url == f["url"] and job.url.startswith("https://remotive.com/")
        assert job.location_raw == f["candidate_required_location"]

    def test_salario_de_texto_libre_pasa_crudo(self):
        fm = endpoint_registry.ENDPOINTS["remotive.com"]["field_map"]
        f = next(f for f in self.FILAS if f.get("salary"))
        assert es.a_job_result(f, fm, "remotive.com").salary_raw == f["salary"]

    def test_lista_con_peru_pasa_para_el_candidato(self):
        assert _veredicto("USA, Canada, Argentina, Mexico, Peru") == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# Jobicy
# ─────────────────────────────────────────────────────────────────────────────

class TestJobicy:
    CRUDO = _crudo("jobicy.json")
    FILAS = json.loads(CRUDO)["jobs"]
    FM = endpoint_registry.ENDPOINTS["jobicy.com"]["field_map"]

    def _job(self, f):
        return es.a_job_result(f, self.FM, "jobicy.com")

    def test_un_solo_pedido_para_todos_los_titulos(self):
        ses, res = _correr("jobicy.com", self.CRUDO)
        assert len(ses.pedidos) == 1
        assert res.items_crudos == len(self.FILAS)

    def test_mapeo(self):
        f = self.FILAS[0]
        job = self._job(f)
        assert job.title == f["jobTitle"] and job.company == f["companyName"]
        assert job.url == f["url"] and job.url.startswith("https://jobicy.com/jobs/")
        # Jobicy separa con doble espacio ("Canada,  USA"); `_texto` lo colapsa.
        assert job.location_raw == " ".join(f["jobGeo"].split())

    def test_salario_anual_con_su_moneda(self):
        f = next(f for f in self.FILAS
                 if f.get("salaryPeriod") == "yearly" and f.get("salaryCurrency") == "USD")
        job = self._job(f)
        assert job.salary_raw.startswith("USD ")

    def test_moneda_que_el_parser_no_convierte_no_se_disfraza_de_dolar(self):
        """CAD 135.000 no son 135.000 dolares. Hasta la Fase 6 el parser no convierte
        CAD, asi que el aviso queda con un salario que no lee —revision manual—, que es
        mejor que un sueldo inventado."""
        f = next(f for f in self.FILAS
                 if f.get("salaryCurrency") not in (None, "USD", "EUR", "GBP")
                 and f.get("salaryPeriod") == "yearly")
        job = self._job(f)
        assert job.salary_raw.startswith(f"{f['salaryCurrency']} ")
        assert "$" not in job.salary_raw

    def test_por_hora_se_compone_con_su_periodo(self):
        """Fase 6: antes no se componia; ahora va con su periodo y se anualiza."""
        from models import salario_anual_usd
        f = next(f for f in self.FILAS if f.get("salaryPeriod") == "hourly")
        job = self._job(f)
        assert job.salary_raw.endswith("/hour")
        if f.get("salaryCurrency") in ("USD", "EUR", "GBP"):
            assert salario_anual_usd(job.salary_raw) is not None

    def test_sin_claves_de_salario_no_hay_salario(self):
        f = next(f for f in self.FILAS if "salaryMin" not in f)
        assert self._job(f).salary_raw is None


# ─────────────────────────────────────────────────────────────────────────────
# Arbeitnow
# ─────────────────────────────────────────────────────────────────────────────

class TestArbeitnow:
    CRUDO = _crudo("arbeitnow.json")
    FILAS = json.loads(CRUDO)["data"]

    def test_solo_entra_lo_remoto(self):
        _, res = _correr("arbeitnow.com", self.CRUDO, titulos=())
        presenciales = sum(1 for f in self.FILAS if not f["remote"])
        assert presenciales > 0
        assert res.descartados_por_filtro == presenciales
        remotos = {f["url"] for f in self.FILAS if f["remote"]}
        assert {j.url for j in res.jobs} <= remotos

    def test_remote_a_secas_lleva_la_region(self):
        _, res = _correr("arbeitnow.com", self.CRUDO, titulos=())
        ubic = {j.location_raw for j in res.jobs}
        assert "Remote; Europe" in ubic
        assert all("Europe" in u for u in ubic)

    def test_remote_europeo_no_pasa_para_el_candidato(self):
        """El falso positivo que la region corrige: el matcher lee "Remote" a secas
        como neutral, y neutral es PASS. Con la region del board, es FLAG."""
        assert _veredicto("Remote; Europe") == "FLAG"

    def test_una_ubicacion_abierta_explicita_sigue_ganando(self):
        assert _veredicto(es._con_region("Remote (Worldwide)", "Europe")) == "PASS"


class TestFiltroYRegion:
    @pytest.mark.parametrize("valor,entra", [
        (True, True), (False, False), (None, False), ("true", True), ("false", False),
        (1, True), (0, False)])
    def test_pasa_filtro(self, valor, entra):
        assert es._pasa_filtro({"remote": valor}, "remote") is entra

    def test_sin_campo_entra_todo(self):
        assert es._pasa_filtro({}, "") is True

    @pytest.mark.parametrize("ubic,esperado", [
        ("Remote", "Remote; Europe"),
        ("", "Europe"),
        ("Remote, Europe", "Remote, Europe"),     # ya la nombra: no se repite
        ("Remote (Germany)", "Remote (Germany); Europe"),
    ])
    def test_con_region(self, ubic, esperado):
        assert es._con_region(ubic, "Europe") == esperado

    def test_sin_region_no_cambia_nada(self):
        assert es._con_region("Remote", "") == "Remote"


# ─────────────────────────────────────────────────────────────────────────────
# El limite entre corridas
# ─────────────────────────────────────────────────────────────────────────────

class TestEsperaPendiente:
    def test_sin_intervalo_no_espera(self):
        assert es.espera_pendiente(0, 1000.0, 1001.0) == 0

    def test_nunca_pedido_no_espera(self):
        assert es.espera_pendiente(3600, 0.0, 1000.0) == 0

    def test_dentro_del_intervalo_espera_lo_que_falta(self):
        assert es.espera_pendiente(3600, 1000.0, 1600.0) == 3000

    def test_cumplido_el_intervalo_no_espera(self):
        assert es.espera_pendiente(3600, 1000.0, 4600.0) == 0

    def test_reloj_que_vuelve_atras_no_bloquea_para_siempre(self):
        assert es.espera_pendiente(3600, 5000.0, 1000.0) == 3600


class TestLimiteEnElScraper:
    """El chequeo vive en `GemaScraper._scrape_endpoint_boards`. La estrategia va a un
    directorio temporal: `update()` persiste, y el `board_strategy.json` real no se toca."""

    def _armar(self, tmp_db, tmp_path, monkeypatch):
        import queue
        import scraper as scraper_mod
        from board_strategy import BoardStrategyRegistry
        s = scraper_mod.GemaScraper(
            SearchConfig(target_titles=["Backend Engineer"]), tmp_db, queue.Queue(),
            profile={})
        s._strategy = BoardStrategyRegistry(tmp_path / "estrategia.json")
        llamadas = []

        def falso(dominio, **kw):
            llamadas.append(dominio)
            return es.ResultadoEndpoint(dominio=dominio, jobs=[])
        monkeypatch.setattr(es, "scrape_endpoint", falso)
        return s, llamadas

    @staticmethod
    def _dos_pasadas(s, dominio):
        """Dos llamadas en UN event loop, como dentro de una corrida real: la cola de
        escritura del scraper queda atada al loop donde se uso por primera vez."""
        import asyncio

        async def _correr():
            await s._scrape_endpoint_boards([dominio])
            await s._scrape_endpoint_boards([dominio])
        asyncio.run(_correr())

    def test_segundo_pedido_dentro_de_la_hora_se_saltea(self, tmp_db, tmp_path,
                                                        monkeypatch):
        s, llamadas = self._armar(tmp_db, tmp_path, monkeypatch)
        self._dos_pasadas(s, "jobicy.com")
        assert llamadas == ["jobicy.com"]
        assert s._strategy.get("jobicy.com").ultimo_pedido_ts > 0

    def test_saltear_no_es_un_error_ni_deriva(self, tmp_db, tmp_path, monkeypatch):
        from board_strategy import Health
        s, _ = self._armar(tmp_db, tmp_path, monkeypatch)
        self._dos_pasadas(s, "jobicy.com")
        assert s._strategy.get("jobicy.com").health != Health.DEGRADED
        assert not s.summary.errors

    def test_sin_intervalo_se_pide_en_cada_corrida(self, tmp_db, tmp_path, monkeypatch):
        s, llamadas = self._armar(tmp_db, tmp_path, monkeypatch)
        self._dos_pasadas(s, "remoteok.com")
        assert llamadas == ["remoteok.com", "remoteok.com"]
