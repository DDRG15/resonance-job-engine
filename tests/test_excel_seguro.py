"""
Export a Excel sin formulas inyectadas.

El titulo, la empresa, el salario y la URL de cada fila los escribio un board. openpyxl
escribe como FORMULA toda cadena que empieza con `=`, y pandas.to_excel no lo evita: un
aviso titulado `=HYPERLINK("http://evil","x")` sale del export como una celda que Excel
evalua al abrir el archivo. El que lo abre es el usuario, sobre un archivo que GEMA le dijo que
era de resultados. La defensa vive en el borde de exportacion; el texto en la DB y en la UI
no cambia. Cada test abre el XLSX de verdad con openpyxl —y el primero demuestra que sin
la defensa la celda SI sale como formula— porque un test que no puede fallar no prueba nada.
"""

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.utils.exceptions import IllegalCharacterError

import api as api_module
import config
import run_manager as run_manager_module
from excel_seguro import es_url_enlazable, limpiar_celda
from models import JobResult, TieredJob

FORMULA = '=HYPERLINK("http://evil","x")'


def _hoja(buf):
    return load_workbook(buf).active


class TestLaPremisa:
    def test_sin_la_defensa_pandas_escribe_la_formula(self):
        """Si esto deja de ser verdad (openpyxl/pandas cambian), la defensa ya no hace
        falta y hay que releer este modulo; mientras sea verdad, justifica todo lo demas."""
        buf = io.BytesIO()
        pd.DataFrame([{"Title": FORMULA}]).to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        assert _hoja(buf)["A2"].data_type == "f"

    def test_sin_la_defensa_un_caracter_de_control_tumba_el_export(self):
        with pytest.raises(IllegalCharacterError):
            pd.DataFrame([{"Title": "A\x07B"}]).to_excel(
                io.BytesIO(), index=False, engine="openpyxl")


class TestEnlaces:
    @pytest.mark.parametrize("url", [
        "https://acme.com/jobs/1", "http://acme.com/j?id=1&x=2", "HTTPS://ACME.COM/x",
        "  https://acme.com/jobs/1  ",
    ])
    def test_http_y_https_con_host_son_enlaces(self, url):
        assert es_url_enlazable(url)

    @pytest.mark.parametrize("url", [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        r"\\servidor\share\x",
        "ftp://acme.com/x",
        "data:text/html,<script>alert(1)</script>",
        "http://",                       # sin host
        "//evil.example/x",              # sin esquema
        "acme.com/jobs/1",
        "https://acme.com/" + "a" * 2100,  # mas largo de lo que Excel acepta como enlace
        "",
        None,
        "http://[::1",                   # urlparse lanza ValueError
    ])
    def test_cualquier_otra_cosa_no_es_enlace(self, url):
        assert not es_url_enlazable(url)


class TestLimpieza:
    def test_el_caracter_de_control_se_quita(self):
        assert limpiar_celda("Senior\x07 Engineer\x00") == "Senior Engineer"

    def test_un_texto_normal_no_cambia(self):
        assert limpiar_celda(FORMULA) == FORMULA
        assert limpiar_celda("Python Backend Developer") == "Python Backend Developer"

    def test_tope_de_una_celda(self):
        assert len(limpiar_celda("x" * 40000)) == 32767

    def test_none_se_vuelve_vacio(self):
        assert limpiar_celda(None) == ""

    def test_un_valor_que_no_es_cadena_se_vuelve_cadena(self):
        assert limpiar_celda(88) == "88"

    def test_lo_limpio_ya_no_tumba_el_export(self):
        """El caracter de control que openpyxl rechaza no sobrevive a la limpieza."""
        pd.DataFrame([{"Title": limpiar_celda("A\x07B")}]).to_excel(
            io.BytesIO(), index=False, engine="openpyxl")


# =============================================================================
# Por el endpoint real
# =============================================================================

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GEMA_DB_PATH", str(tmp_path / "test_registry.db"))
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    with TestClient(api_module.app) as c:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        api_module.app.state.sched.notion = None
        api_module.app.state.sched.sheets = None
        api_module.app.state.runs._notion = None
        api_module.app.state.runs._sheets = None
        yield c


def _tj(titulo, empresa, url="https://acme.com/jobs/1", salario="$100k", score=90, tier="Tier 1"):
    return TieredJob(
        job=JobResult(title=titulo, company=empresa, url=url, salary_raw=salario,
                      source_domain="remoteok.com", location_raw="Worldwide"),
        match_score=score, tier=tier)


class TestEndpoint:
    def test_el_aviso_adversarial_sale_como_texto_por_el_endpoint(self, client):
        """El caso adversarial: titulo `=HYPERLINK(...)`, empresa `+cmd`. Y un aviso
        normal al lado, que sigue igual."""
        client.app.state.runs.results = (
            [_tj(FORMULA, "+cmd", url="javascript:alert(1)", salario="=1+1"),
             _tj("Backend Engineer", "Acme")],
            [], [], [],
        )
        r = client.get("/api/export.xlsx")
        assert r.status_code == 200

        import openpyxl
        import exportar_excel
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        # Ninguna celda de formula en NINGUNA hoja del libro (Resumen incluido).
        assert [(h.title, c.coordinate) for h in wb.worksheets for fila in h.iter_rows()
                for c in fila if c.data_type == "f"] == []
        hoja = wb[exportar_excel.HOJA_AVISOS]
        cab = {c.value: c.column for c in hoja[1]}
        filas = {hoja.cell(f, cab["Título"]).value: f for f in range(2, hoja.max_row + 1)}
        mala, buena = filas[FORMULA], filas["Backend Engineer"]
        for columna, esperado in (("Título", FORMULA), ("Empresa", "+cmd"),
                                  ("Salario (texto original)", "=1+1")):
            celda = hoja.cell(mala, cab[columna])
            assert celda.value == esperado and celda.data_type == "s", columna
        assert hoja.cell(mala, cab["URL"]).hyperlink is None          # javascript: nunca enlaza
        assert hoja.cell(buena, cab["URL"]).hyperlink.target == "https://acme.com/jobs/1"

    def test_el_texto_original_no_cambia_en_el_estado_del_servidor(self, client):
        """La defensa vive en el borde: el objeto que lee la UI no se toca."""
        t1 = [_tj(FORMULA, "+cmd")]
        client.app.state.runs.results = (t1, [], [], [])
        assert client.get("/api/export.xlsx").status_code == 200
        assert client.app.state.runs.results[0][0].job.title == FORMULA
        assert client.app.state.runs.results[0][0].job.company == "+cmd"
