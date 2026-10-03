"""
Export a Excel sin formulas inyectadas (Plan30 Fase 3, revision externa 14).

El titulo, la empresa, el salario y la URL de cada fila los escribio un board. openpyxl
escribe como FORMULA toda cadena que empieza con `=`, y pandas.to_excel no lo evita: un
aviso titulado `=HYPERLINK("http://evil","x")` sale del export como una celda que Excel
evalua al abrir el archivo. El que lo abre es el usuario, sobre un archivo que GEMA le dijo que
era de resultados. La defensa vive en el borde de exportacion; el texto en la DB y en la UI
no cambia. Cada test abre el XLSX de verdad con openpyxl —y el primero demuestra que sin
la defensa la celda SI sale como formula— porque un test que no puede fallar no prueba nada.
"""

import io
import zipfile

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.utils.exceptions import IllegalCharacterError

import api as api_module
import config
import run_manager as run_manager_module
from excel_seguro import (
    COLUMNAS_EXTERNAS,
    construir_xlsx,
    es_url_enlazable,
    limpiar_celda,
)
from models import JobResult, TieredJob

FORMULA = '=HYPERLINK("http://evil","x")'


def _fila(**kw):
    base = {
        "Tier": 1, "Title": "Python Backend Developer", "Company": "Acme",
        "Salary": "$120k", "Match Score": 88, "Apply Link": "https://acme.com/jobs/1",
        "Source": "remoteok.com", "Date Fetched": "2026-09-30 12:00 UTC",
    }
    base.update(kw)
    return base


def _hoja(buf):
    return load_workbook(buf).active


def _celdas(hoja):
    """{(encabezado, fila): celda} para las filas de datos."""
    enc = {c.column: c.value for c in hoja[1]}
    return {(enc[c.column], c.row): c for fila in hoja.iter_rows(min_row=2) for c in fila}


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


class TestFormulas:
    @pytest.mark.parametrize("columna,valor", [
        ("Title", FORMULA),
        ("Company", "+cmd"),
        ("Salary", "-1+1"),
        ("Source", "@SUM(A1)"),
        ("Title", "=cmd|' /C calc'!A0"),                  # el vector clasico de DDE
        ("Title", " =1+1"),                               # tras un espacio
        ("Company", "\t=1+1"),                            # tras un tab
        ("Apply Link", "=HYPERLINK(\"http://evil\")"),
    ])
    def test_toda_celda_externa_sale_como_texto_y_conserva_su_contenido(self, columna, valor):
        hoja = _hoja(construir_xlsx([_fila(**{columna: valor})]))
        celda = _celdas(hoja)[(columna, 2)]
        assert celda.data_type == "s"
        assert celda.value == valor, "el contenido visible cambio"

    def test_los_controles_antes_de_la_formula_se_quitan_y_sigue_siendo_texto(self):
        """`\\x01=1+1`: el control es invisible y XML no lo admite; sin quitarlo el export
        entero fallaba. Lo visible, `=1+1`, se conserva y NO es formula."""
        hoja = _hoja(construir_xlsx([_fila(Title="\x01\x02=1+1")]))
        celda = _celdas(hoja)[("Title", 2)]
        assert celda.data_type == "s" and celda.value == "=1+1"

    def test_ninguna_celda_del_libro_es_formula(self):
        filas = [
            _fila(Title=FORMULA, Company="+cmd", Salary="-1+1", **{"Source": "@x"}),
            _fila(Title="=1+1", **{"Apply Link": "=2+2"}),
            _fila(),
        ]
        hoja = _hoja(construir_xlsx(filas))
        assert [c.coordinate for fila in hoja.iter_rows() for c in fila
                if c.data_type == "f"] == []

    def test_el_xml_de_la_hoja_no_tiene_ningun_elemento_de_formula(self):
        """Independiente del lector de openpyxl: se mira el XML que Excel va a evaluar."""
        buf = construir_xlsx([_fila(Title=FORMULA, Company="=1+1")])
        with zipfile.ZipFile(buf) as z:
            xml = z.read("xl/worksheets/sheet1.xml")
        assert b"<f>" not in xml and b"<f " not in xml
        assert b"HYPERLINK" in xml, "el texto no se perdio: esta como cadena"

    def test_un_titulo_normal_sigue_igual(self):
        hoja = _hoja(construir_xlsx([_fila()]))
        c = _celdas(hoja)
        assert c[("Title", 2)].value == "Python Backend Developer"
        assert c[("Title", 2)].data_type == "s"
        assert c[("Company", 2)].value == "Acme"
        assert c[("Salary", 2)].value == "$120k"

    def test_las_columnas_que_calcula_gema_conservan_su_tipo(self):
        c = _celdas(_hoja(construir_xlsx([_fila(**{"Match Score": 88})])))
        assert c[("Tier", 2)].data_type == "n" and c[("Tier", 2)].value == 1
        assert c[("Match Score", 2)].data_type == "n" and c[("Match Score", 2)].value == 88

    def test_el_encabezado_no_cambia(self):
        hoja = _hoja(construir_xlsx([_fila()]))
        assert [c.value for c in hoja[1]] == list(_fila().keys())

    def test_la_defensa_sigue_a_la_columna_aunque_cambie_el_orden(self):
        fila = {"Source": "@x", "Title": FORMULA, "Tier": 2}
        c = _celdas(_hoja(construir_xlsx([fila])))
        assert c[("Title", 2)].data_type == "s" and c[("Source", 2)].data_type == "s"

    def test_todas_las_columnas_externas_estan_cubiertas(self):
        assert set(COLUMNAS_EXTERNAS) == {"Title", "Company", "Salary", "Apply Link", "Source"}


class TestEnlaces:
    @pytest.mark.parametrize("url", [
        "https://acme.com/jobs/1", "http://acme.com/j?id=1&x=2", "HTTPS://ACME.COM/x",
    ])
    def test_http_y_https_con_host_son_enlaces(self, url):
        celda = _celdas(_hoja(construir_xlsx([_fila(**{"Apply Link": url})])))[("Apply Link", 2)]
        assert celda.hyperlink is not None and celda.hyperlink.target == url
        assert celda.value == url and celda.data_type == "s"

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
    ])
    def test_cualquier_otra_cosa_queda_como_texto_sin_hipervinculo(self, url):
        celda = _celdas(_hoja(construir_xlsx([_fila(**{"Apply Link": url})])))[("Apply Link", 2)]
        assert celda.hyperlink is None
        if url:
            assert celda.data_type == "s" and celda.value == url, \
                "se oculto el texto: el usuario tiene que poder ver que es"

    def test_es_url_enlazable(self):
        assert es_url_enlazable("https://a.com")
        assert not es_url_enlazable("javascript:alert(1)")
        assert not es_url_enlazable(None)
        assert not es_url_enlazable("http://[::1")      # urlparse lanza ValueError


class TestLimpieza:
    def test_el_caracter_de_control_se_quita_y_el_export_no_falla(self):
        c = _celdas(_hoja(construir_xlsx([_fila(Title="Senior\x07 Engineer\x00")])))
        assert c[("Title", 2)].value == "Senior Engineer"

    def test_tope_de_una_celda(self):
        c = _celdas(_hoja(construir_xlsx([_fila(Title="x" * 40000)])))
        assert len(c[("Title", 2)].value) == 32767

    def test_none_se_vuelve_vacio(self):
        assert limpiar_celda(None) == ""

    def test_sin_filas_el_libro_es_valido(self):
        hoja = _hoja(construir_xlsx([]))
        assert hoja.max_row <= 1

    def test_salario_vacio_no_rompe(self):
        c = _celdas(_hoja(construir_xlsx([_fila(Salary="")])))
        assert c[("Salary", 2)].value in (None, "")


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
        """El caso de la auditoria: titulo `=HYPERLINK(...)`, empresa `+cmd`. Y un aviso
        normal al lado, que sigue igual."""
        client.app.state.runs.results = (
            [_tj(FORMULA, "+cmd", url="javascript:alert(1)", salario="=1+1"),
             _tj("Backend Engineer", "Acme")],
            [], [], [],
        )
        r = client.get("/api/export.xlsx")
        assert r.status_code == 200

        hoja = _hoja(io.BytesIO(r.content))
        assert [c.coordinate for fila in hoja.iter_rows() for c in fila
                if c.data_type == "f"] == []
        c = _celdas(hoja)
        # Orden por score descendente y estable: el adversarial (fila 2) y el normal (fila 3).
        assert c[("Title", 2)].value == FORMULA and c[("Title", 2)].data_type == "s"
        assert c[("Company", 2)].value == "+cmd" and c[("Company", 2)].data_type == "s"
        assert c[("Salary", 2)].value == "=1+1" and c[("Salary", 2)].data_type == "s"
        assert c[("Apply Link", 2)].value == "javascript:alert(1)"
        assert c[("Apply Link", 2)].hyperlink is None
        assert c[("Title", 3)].value == "Backend Engineer"
        assert c[("Apply Link", 3)].hyperlink.target == "https://acme.com/jobs/1"

    def test_el_texto_original_no_cambia_en_el_estado_del_servidor(self, client):
        """La defensa vive en el borde: el objeto que lee la UI no se toca."""
        t1 = [_tj(FORMULA, "+cmd")]
        client.app.state.runs.results = (t1, [], [], [])
        assert client.get("/api/export.xlsx").status_code == 200
        assert client.app.state.runs.results[0][0].job.title == FORMULA
        assert client.app.state.runs.results[0][0].job.company == "+cmd"
