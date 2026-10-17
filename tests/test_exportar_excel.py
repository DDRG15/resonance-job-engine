"""
Excel de resultados: hojas, tabla real, normalizacion y formato condicional.

El export viejo volcaba un DataFrame sin formato. Estos tests abren el XLSX de verdad con
openpyxl y verifican lo que el usuario ve: el orden de las hojas, que «Avisos» es una
tabla de Excel con autofiltro y encabezado congelado, que no queda ningun "None"/"nan",
que el salario es un numero, que la URL es un hipervinculo y que un titulo con una
formula no se ejecuta. Sin red: los avisos son sinteticos.
"""

import io
import zipfile
from datetime import date, datetime, timezone

import pytest
from openpyxl import load_workbook

import exportar_excel as ex
from exportar_excel import COLUMNAS_AVISOS, construir_excel
from models import JobResult, TieredJob

FORMULA = '=HYPERLINK("http://evil","x")'


def _tj(titulo="Python Developer", empresa="Acme", score=80, tier="Tier 1", url=None,
        salario="$120k - $150k", ubicacion="Anywhere", veredicto="PASS", nota="",
        fuente="remoteok.com", deadline=None, requisitos=None, cobertura=None, **kw):
    return TieredJob(
        job=JobResult(
            title=titulo, company=empresa,
            url=url or f"https://example.com/jobs/{abs(hash((titulo, empresa)))}",
            salary_raw=salario, location_raw=ubicacion, source_domain=fuente,
            deadline=deadline,
            scraped_at=datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)),
        match_score=score, tier=tier, location_verdict=veredicto, location_note=nota,
        requisitos=requisitos or [], cobertura_requisitos=cobertura,
        match_reasons=kw.pop("match_reasons", []), **kw)


def _tiers():
    return {
        "tier1": [_tj("Backend A", "Acme", 90), _tj("Backend B", "Beta", 85, salario=None)],
        "tier2": [_tj("Data C", "Gamma", 70, "Tier 2", veredicto="FLAG",
                      nota="Location soft-restricted: 'USA'", salario="Competitive")],
        "tier3": [_tj("Ops D", "Delta", 40, "Tier 3", veredicto="FAIL", nota="Pide clearance")],
    }


def _abrir(datos):
    assert isinstance(datos, bytes)
    return load_workbook(io.BytesIO(datos))


def _fila(hoja, n):
    enc = [c.value for c in hoja[1]]
    return dict(zip(enc, [c.value for c in hoja[n]]))


class TestEstructura:
    def test_orden_de_hojas(self):
        wb = _abrir(construir_excel(_tiers()))
        assert wb.sheetnames == ["Resumen", "Avisos", "Revisión manual"]
        assert wb.active.title == "Resumen"

    def test_avisos_es_una_tabla_con_ref_y_autofiltro(self):
        wb = _abrir(construir_excel(_tiers()))
        hoja = wb["Avisos"]
        assert len(hoja.tables) == 1
        tabla = next(iter(hoja.tables.values()))
        ultima = len(COLUMNAS_AVISOS)
        from openpyxl.utils import get_column_letter
        assert tabla.ref == f"A1:{get_column_letter(ultima)}5"   # 4 avisos + encabezado
        assert tabla.autoFilter is not None and tabla.autoFilter.ref == tabla.ref
        assert tabla.tableStyleInfo.name

    def test_encabezado_congelado_y_columnas_en_orden(self):
        hoja = _abrir(construir_excel(_tiers()))["Avisos"]
        assert hoja.freeze_panes == "A2"
        assert [c.value for c in hoja[1]] == list(COLUMNAS_AVISOS)
        assert COLUMNAS_AVISOS[:4] == ("Tier", "Score", "Ubicación", "Motivos")
        assert hoja.column_dimensions["D"].width >= 40

    def test_filas_ordenadas_por_tier_y_score_desc(self):
        hoja = _abrir(construir_excel({
            "x": [_tj("Z", "z", 60, "Tier 2"), _tj("A", "a", 95), _tj("M", "m", 99, "Tier 1"),
                  _tj("B", "b", 20, "Tier 3")]}))["Avisos"]
        orden = [(hoja.cell(r, 1).value, hoja.cell(r, 2).value) for r in range(2, 6)]
        assert orden == [("Tier 1", 99), ("Tier 1", 95), ("Tier 2", 60), ("Tier 3", 20)]

    def test_tabla_vacia_no_se_crea_y_el_archivo_abre(self):
        wb = _abrir(construir_excel({"tier1": []}))
        assert not wb["Avisos"].tables
        assert not wb["Revisión manual"].tables

    def test_propiedades_sin_datos_personales(self):
        props = _abrir(construir_excel(_tiers())).properties
        assert props.creator == "Resonance"
        assert props.lastModifiedBy == "Resonance"


class TestNormalizacion:
    def test_ninguna_celda_dice_none_ni_nan(self):
        tiers = {"tier1": [
            _tj("None", "nan", salario="None", ubicacion="null"),
            _tj("Con vacios", "(empresa no listada)", salario=None, ubicacion=None)]}
        wb = _abrir(construir_excel(tiers, manual_review=[
            {"title": "T", "company": "None", "source_domain": "x.com",
             "original_url": None, "srp_url": None, "reason": None, "captured_at": None,
             "resolved": 0}]))
        for hoja in wb:
            for fila in hoja.iter_rows():
                for c in fila:
                    if isinstance(c.value, str):
                        assert c.value.strip().lower() not in ("none", "nan", "null"), \
                            (hoja.title, c.coordinate)
        avisos = wb["Avisos"]
        vacia = _fila(avisos, 3)
        assert vacia["Empresa"] is None            # el placeholder del scraper es vacio real
        assert vacia["Ubicación (texto)"] is None
        assert vacia["Salario USD/año"] is None

    def test_sin_comillas_ni_espacios_de_mas(self):
        datos = construir_excel({"t": [_tj('  "Senior   Python\n Dev"  ', "'Acme  Corp'")]})
        fila = _fila(_abrir(datos)["Avisos"], 2)
        assert fila["Título"] == "Senior Python Dev"
        assert fila["Empresa"] == "Acme Corp"

    def test_comillas_interiores_se_respetan(self):
        assert ex.texto('Acme "Labs" Inc') == 'Acme "Labs" Inc'
        assert ex.texto('"a" y "b"') == '"a" y "b"'

    def test_salario_es_numero_con_formato_de_miles(self):
        hoja = _abrir(construir_excel(_tiers()))["Avisos"]
        fila2 = _fila(hoja, 2)
        assert isinstance(fila2["Salario USD/año"], (int, float))
        assert fila2["Salario USD/año"] == 120_000        # el minimo del rango
        col = COLUMNAS_AVISOS.index("Salario USD/año") + 1
        assert hoja.cell(2, col).number_format == "#,##0"
        assert fila2["Salario (texto original)"] == "$120k - $150k"

    def test_score_entero_cobertura_porcentaje_y_fechas_de_excel(self):
        tj = _tj(score=88, cobertura=0.5, deadline="2026-11-30")
        hoja = _abrir(construir_excel({"t": [tj]}))["Avisos"]
        fila = _fila(hoja, 2)
        assert fila["Score"] == 88 and isinstance(fila["Score"], int)
        assert fila["Cobertura requisitos"] == 0.5
        assert hoja.cell(2, COLUMNAS_AVISOS.index("Cobertura requisitos") + 1).number_format == "0%"
        assert isinstance(fila["Capturado"], datetime)
        assert fila["Capturado"].date() == date(2026, 10, 1)
        assert fila["Vence"].date() == date(2026, 11, 30)
        assert hoja.cell(2, COLUMNAS_AVISOS.index("Vence") + 1).number_format == "yyyy-mm-dd"

    def test_si_no(self):
        assert ex.si_no(True) == "Sí" and ex.si_no(0) == "No"
        assert ex.si_no(None) == "" and ex.si_no("nan") == ""

    def test_faltan_lista_solo_requeridos_missing(self):
        reqs = [{"requisito": "Kafka", "estado": "missing", "peso": "requerido"},
                {"requisito": "Go", "estado": "missing", "peso": "deseable"},
                {"requisito": "Python", "estado": "match", "peso": "requerido"}]
        fila = _fila(_abrir(construir_excel(
            {"t": [_tj(requisitos=reqs, cobertura=0.4)]}))["Avisos"], 2)
        assert fila["Faltan"] == "Kafka"
        assert "Faltan 1 requisito" in fila["Motivos"]

    def test_motivos_con_veredicto_y_salario_no_confirmado(self):
        fila = _fila(_abrir(construir_excel(_tiers()))["Avisos"], 4)   # Data C (FLAG)
        assert fila["Título"] == "Data C"
        assert fila["Ubicación"] == "FLAG"
        assert "Ubicación FLAG" in fila["Motivos"]
        assert "Salario no confirmado" in fila["Motivos"]


class TestEnlaceYSeguridad:
    def test_url_es_hipervinculo_con_texto_corto(self):
        url = "https://boards.example.com/jobs/" + "x" * 200
        hoja = _abrir(construir_excel({"t": [_tj(url=url)]}))["Avisos"]
        celda = hoja.cell(2, COLUMNAS_AVISOS.index("URL") + 1)
        assert celda.hyperlink is not None and celda.hyperlink.target == url
        assert celda.value == "Abrir aviso"

    def test_url_no_http_queda_como_texto_sin_enlace(self):
        hoja = _abrir(construir_excel({"t": [_tj(url="javascript:alert(1)")]}))["Avisos"]
        celda = hoja.cell(2, COLUMNAS_AVISOS.index("URL") + 1)
        assert celda.hyperlink is None
        assert celda.value == "javascript:alert(1)"

    @pytest.mark.parametrize("malo", [FORMULA, "+cmd|' /C calc'!A0", "-1+1", "@SUM(A1)"])
    def test_celda_maliciosa_queda_como_texto(self, malo):
        datos = construir_excel({"t": [_tj(titulo=malo, empresa=malo, salario=malo)]})
        hoja = _abrir(datos)["Avisos"]
        for col in ("Título", "Empresa", "Salario (texto original)"):
            celda = hoja.cell(2, COLUMNAS_AVISOS.index(col) + 1)
            assert celda.data_type != "f", col
            assert celda.value == malo, col

    def test_el_xml_no_trae_formulas(self):
        datos = construir_excel(
            {"t": [_tj(titulo=FORMULA)]},
            manual_review=[{"title": FORMULA, "company": FORMULA, "source_domain": "x.com",
                            "original_url": "/rel", "srp_url": "https://x.com/l", "reason": "r",
                            "captured_at": "2026-10-01T10:00:00+00:00", "resolved": 0}],
            no_elegibles=[_tj(titulo=FORMULA, veredicto="FAIL", nota=FORMULA)])
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            for nombre in z.namelist():
                if nombre.startswith("xl/worksheets/sheet"):
                    assert b"<f>" not in z.read(nombre), nombre

    def test_caracter_de_control_no_tumba_el_export(self):
        fila = _fila(_abrir(construir_excel({"t": [_tj(titulo="A\x07B")]}))["Avisos"], 2)
        assert fila["Título"] == "AB"


class TestFormatoCondicional:
    def _reglas(self, hoja):
        return {str(rango.sqref): [r for r in reglas]
                for rango, reglas in hoja.conditional_formatting._cf_rules.items()}

    def test_semaforo_tier_y_escala_de_score(self):
        hoja = _abrir(construir_excel(_tiers()))["Avisos"]
        reglas = self._reglas(hoja)
        assert set(reglas) == {"A2:A5", "B2:B5", "C2:C5"}
        ubic = reglas["C2:C5"]
        assert sorted(r.formula[0] for r in ubic) == ['"FAIL"', '"FLAG"', '"PASS"']
        colores = {r.formula[0]: r.dxf.fill.bgColor.rgb[-6:] for r in ubic}
        assert colores['"PASS"'] == "C6EFCE" and colores['"FLAG"'] == "FFEB9C"
        assert colores['"FAIL"'] == "FFC7CE"
        assert [r.type for r in reglas["B2:B5"]] == ["colorScale"]
        tiers = {r.formula[0]: r.dxf.fill.bgColor.rgb[-6:] for r in reglas["A2:A5"]}
        assert set(tiers) == {'"Tier 1"', '"Tier 2"', '"Tier 3"'}
        assert tiers['"Tier 1"'] == "5EEAD4"


class TestResumenYRevision:
    def test_resumen_totales_por_tier_y_fuente(self):
        hoja = _abrir(construir_excel(_tiers(), meta={"fecha": "2026-10-17"}))["Resumen"]
        celdas = {c.value: c for fila in hoja.iter_rows() for c in fila if c.value is not None}
        assert celdas["Por tier"] and celdas["Por fuente"] and celdas["Salarios"]
        assert hoja["B2"].value.date() == date(2026, 10, 17)
        assert hoja["B3"].value == 4
        r_t1 = celdas["Tier 1"].row
        assert hoja.cell(r_t1, 2).value == 2 and hoja.cell(r_t1, 3).value == 0.5

    def test_revision_incluye_pila_y_no_elegibles_con_motivo(self):
        pila = [{"title": "Staff Data Engineer", "company": "DualEntry",
                 "source_domain": "builtin.com", "original_url": "/job/roto",
                 "srp_url": "https://builtin.com/jobs?q=data",
                 "reason": "url_no_verificable_titulo_real",
                 "captured_at": "2026-10-02T08:00:00+00:00", "resolved": 0}]
        no_el = [_tj("Vetado", "V", 77, veredicto="FAIL", nota="Pide ciudadania"),
                 _tj("Viejo", "W", 66, url="https://example.com/viejo")]
        datos = construir_excel(
            _tiers(), manual_review=pila, no_elegibles=no_el,
            meta={"causas": {"https://example.com/viejo": "Vencido"}})
        hoja = _abrir(datos)["Revisión manual"]
        assert len(hoja.tables) == 1
        filas = [_fila(hoja, r) for r in range(2, 5)]
        assert filas[0]["Origen"] == "Pila de revisión"
        assert filas[0]["Motivo"].startswith("URL no verificable")
        assert filas[0]["Resuelto"] == "No"
        assert filas[0]["Título"] == "Staff Data Engineer"
        origenes = {f["Origen"]: f for f in filas}
        assert origenes["Veto de ubicación"]["Motivo"] == "Pide ciudadania"
        assert "Vencido" in origenes
        # la URL sin verificar no es enlazable (ruta relativa): queda como texto
        assert hoja.cell(2, ex.COLUMNAS_REVISION.index("URL sin verificar") + 1).hyperlink is None
        assert hoja.cell(2, ex.COLUMNAS_REVISION.index("Listado de origen") + 1).hyperlink is not None
