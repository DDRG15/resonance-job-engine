"""
tests/test_himalayas_endpoint.py — Himalayas por API (, Fase 1).

Cero red. El cuerpo es una respuesta REAL de `/jobs/api/search` capturada el 2026-09-30
(`tests/fixtures/fuentes/himalayas_search_peru.json`), con las descripciones recortadas
a 600 caracteres. Tres avisos de `q=python developer&country=<PAIS_CANDIDATO>` y tres de
`country=<PAIS_CANDIDATO>` elegidos por lo que ejercitan: una lista de 73 paises con el pais del candidato, una lista
vacia, un salario anual en USD, uno mensual, uno por hora y uno anual en EUR.
"""

import json
from pathlib import Path

import pytest

import endpoint_registry
import endpoint_scraper as es
from database import GemaDatabase
from models import SearchConfig, salario_anual_usd

FIXTURE = Path(__file__).parent / "fixtures" / "fuentes" / "himalayas_search_peru.json"
CRUDO = FIXTURE.read_bytes()
FILAS = json.loads(CRUDO)["jobs"]
CFG = endpoint_registry.ENDPOINTS["himalayas.app"]
FM = CFG["field_map"]


def _fila(pred):
    for f in FILAS:
        if pred(f):
            return f
    raise AssertionError("la fixture no tiene la fila que este test necesita")


def _job(fila):
    return es.a_job_result(fila, FM, "himalayas.app",
                           ubicacion_es_lista=CFG["ubicacion_es_lista"])


class RespuestaFalsa:
    def __init__(self, status, contenido=b""):
        self.status_code = status
        self.content = contenido
        self.headers = {}


class SesionFalsa:
    def __init__(self, contenido):
        self.contenido = contenido
        self.pedidos = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.pedidos.append(url)
        return RespuestaFalsa(200, self.contenido)


# ─────────────────────────────────────────────────────────────────────────────
# La entrada del registro
# ─────────────────────────────────────────────────────────────────────────────

class TestEntradaDelRegistro:
    def test_consulta_por_titulo_y_pais(self):
        assert "{title}" in CFG["endpoint_template"]
        assert "country={pais}" in CFG["endpoint_template"]

    def test_politica_abierta_con_razon(self):
        pol = endpoint_registry.politica_de("himalayas.app")
        assert pol["access_policy"] == "open"
        assert pol["policy_reason"]

    def test_el_vector_pasa_a_endpoint_y_hereda_la_transformacion(self, tmp_path):
        """El flag tiene que llegar a la estrategia que lee el scraper: si
        `BoardStrategy` no lo declara, `_aplicar_endpoints` lo descarta en silencio."""
        from board_strategy import AccessVector, BoardStrategyRegistry
        reg = BoardStrategyRegistry(tmp_path / "strategy.json")
        strat = reg.get("himalayas.app")
        assert strat.access_vector == AccessVector.ENDPOINT
        assert strat.ubicacion_es_lista is True
        assert strat.crawl_delay_s == 1.0
        assert strat.field_map["url"] == "guid"


# ─────────────────────────────────────────────────────────────────────────────
# Mapeo campo por campo contra la respuesta real
# ─────────────────────────────────────────────────────────────────────────────

class TestMapeo:
    def test_campos_basicos(self):
        f = FILAS[0]
        job = _job(f)
        assert job.title == f["title"]
        assert job.company == f["companyName"]
        assert job.url == f["guid"]
        assert job.url.startswith("https://himalayas.app/companies/")

    def test_lista_de_paises_se_une_con_punto_y_coma(self):
        f = _fila(lambda f: len(f["locationRestrictions"]) > 10)
        job = _job(f)
        partes = job.location_raw.split("; ")
        assert partes == f["locationRestrictions"]
        assert "Peru" in partes

    def test_lista_vacia_es_sin_restriccion(self):
        job = _job(_fila(lambda f: f["locationRestrictions"] == []))
        assert job.location_raw == "Worldwide"

    def test_sin_el_flag_la_lista_no_se_traduce(self):
        """El flag es lo que activa la semantica de "lista de restricciones". Sin el,
        una lista vacia no es "Worldwide": es un dato ausente."""
        f = _fila(lambda f: f["locationRestrictions"] == [])
        job = es.a_job_result(f, FM, "himalayas.app", ubicacion_es_lista=False)
        assert job.location_raw is None

    def test_deadline_queda_crudo_y_se_lee_como_fecha(self):
        f = FILAS[0]
        job = _job(f)
        assert job.deadline == str(f["expiryDate"])
        fecha = GemaDatabase._a_fecha(job.deadline)
        assert fecha is not None and fecha >= "2026-09-30"


class TestSalario:
    def test_anual_usd_con_codigo(self):
        f = _fila(lambda f: f.get("minSalary") and f.get("currency") == "USD"
                  and f.get("salaryPeriod") == "annual")
        job = _job(f)
        assert job.salary_raw.startswith("USD ")
        assert salario_anual_usd(job.salary_raw) == int(f["minSalary"])

    def test_anual_eur_no_se_guarda_como_dolares(self):
        """Con el `$` de antes, 35.000 euros se guardaban como 35.000 dolares."""
        f = _fila(lambda f: f.get("minSalary") and f.get("currency") == "EUR")
        job = _job(f)
        assert job.salary_raw.startswith("EUR ")
        assert "$" not in job.salary_raw
        assert salario_anual_usd(job.salary_raw) > int(f["minSalary"])  # convertido

    @pytest.mark.parametrize("periodo", ["hourly", "monthly"])
    def test_periodo_no_anual_no_se_compone(self, periodo):
        """`hourly 50` compuesto como `USD 50` seria un sueldo de 50 al ano. La Fase 6
        anualiza; hasta entonces no hay salario, que es la verdad."""
        f = _fila(lambda f: f.get("minSalary") and f.get("salaryPeriod") == periodo)
        assert _job(f).salary_raw is None

    def test_sin_moneda_no_se_asume_el_dolar(self):
        f = dict(_fila(lambda f: f.get("minSalary") and f.get("currency")))
        f["currency"] = None
        assert _job(f).salary_raw is None

    def test_sin_monto_no_hay_salario(self):
        f = _fila(lambda f: not f.get("minSalary") and not f.get("maxSalary"))
        assert _job(f).salary_raw is None

    def test_fuente_sin_moneda_declarada_conserva_el_dolar(self):
        """remoteok no declara moneda en el field_map: su camino no cambia."""
        assert es._salario({"a": 90000, "b": 120000},
                           {"salary_min": "a", "salary_max": "b"}) == "$90,000 - $120,000"


# ─────────────────────────────────────────────────────────────────────────────
# Orquestacion: el pais en la URL
# ─────────────────────────────────────────────────────────────────────────────

class TestConsulta:
    def _correr(self, **kw):
        ses = SesionFalsa(CRUDO)
        res = es.scrape_endpoint(
            "himalayas.app", endpoint_template=CFG["endpoint_template"],
            field_map=FM, ubicacion_es_lista=True, titles=["python developer"],
            sesion=ses, **kw)
        return ses, res

    def test_pais_desde_config(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "PAIS_CANDIDATO", "")
        ses, res = self._correr()
        assert ses.pedidos == [
            "https://himalayas.app/jobs/api/search?q=python%20developer&country=<PAIS_CANDIDATO>"]
        assert len(res.jobs) == len(FILAS)

    def test_pais_explicito_gana(self):
        ses, _ = self._correr(pais="Costa Rica")
        assert ses.pedidos[0].endswith("country=Costa%20Rica")

    def test_pais_vacio_se_dice_en_el_log(self):
        lineas = []
        ses, _ = self._correr(pais="", log=lineas.append)
        assert ses.pedidos[0].endswith("country=")
        assert any("PAIS_CANDIDATO" in l for l in lineas)


# ─────────────────────────────────────────────────────────────────────────────
# Lo que el matcher hace con eso
# ─────────────────────────────────────────────────────────────────────────────

class TestUbicacionParaLima:
    """El objetivo de la fase es Tier 1 con ubicacion que pasa: la lista de 73
    paises nombra a Estados Unidos y a media Europa, y tiene que pasar igual porque
    tambien nombra a el pais del candidato."""

    @pytest.mark.parametrize("pred", [
        lambda f: len(f["locationRestrictions"]) > 10,
        lambda f: f["locationRestrictions"] == [],
    ], ids=["lista_con_peru", "sin_restriccion"])
    def test_pasa(self, pred):
        from matcher import score_job
        job = _job(_fila(pred))
        tj = score_job(job, SearchConfig(target_titles=[job.title]))
        assert tj.location_verdict == "PASS", tj.location_note


# ─────────────────────────────────────────────────────────────────────────────
# _a_fecha con epoch
# ─────────────────────────────────────────────────────────────────────────────

class TestFechaEpoch:
    @pytest.mark.parametrize("valor", ["1792667993", 1792667993, "1792667993.0",
                                       "1792667993000"])
    def test_epoch_en_segundos_y_milisegundos(self, valor):
        assert GemaDatabase._a_fecha(valor) == "2026-10-22"

    @pytest.mark.parametrize("valor", ["179266799", "17926679930", "12345"])
    def test_otro_largo_no_es_fecha(self, valor):
        """Un numero de 9 u 11 digitos no es una fecha que sepamos leer: None, y el
        barrido no vence nada por eso."""
        assert GemaDatabase._a_fecha(valor) is None

    def test_los_formatos_de_antes_siguen(self):
        assert GemaDatabase._a_fecha("2026-10-09T15:41:42Z") == "2026-10-09"
        assert GemaDatabase._a_fecha("Fri, 09 Oct 2026 15:41:42 +0000") == "2026-10-09"
