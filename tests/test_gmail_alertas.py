"""
test_gmail_alertas.py — Fase 11: alertas de LinkedIn e Indeed leidas desde Gmail.

Las fixtures de `tests/fixtures/gmail/` son SINTETICAS (ver el README de esa carpeta):
estos tests prueban que el parser hace lo que dice su codigo, no que lea bien los correos
reales. Nada aca toca la red, la API de Gmail ni `credentials/`.

Cuatro tests nacieron como `xfail(strict=True)` por bugs reales del parser (host de Indeed
por substring, host de LinkedIn sin anclar, `@` en el nombre visible del remitente, y la
tarjeta que subia hasta <body> en un correo corto). Se corrigieron el 07-10 y quedan como
tests de regresion.
"""

import base64
import json
from pathlib import Path

import pytest

import gmail_alertas as g
from gmail_alertas import (
    INDEED, LINKEDIN, GmailNoConfigurado, Mensaje,
    consulta_gmail, id_de_enlace, parsear_alerta, proveedor_de_remitente, url_canonica,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gmail"


def _html(nombre: str) -> str:
    return (FIXTURES / nombre).read_text(encoding="utf-8")


# =============================================================================
# Enlaces
# =============================================================================

class TestIdDeEnlace:
    def test_linkedin_comm_con_rastreo(self):
        href = ("https://www.linkedin.com/comm/jobs/view/40123456789/"
                "?trackingId=abc%3D%3D&refId=xyz")
        assert id_de_enlace(LINKEDIN, href) == "40123456789"

    def test_linkedin_sin_comm(self):
        assert id_de_enlace(LINKEDIN, "https://www.linkedin.com/jobs/view/3987654321") \
            == "3987654321"

    @pytest.mark.parametrize("href", [
        "https://www.linkedin.com/comm/jobs/search?keywords=backend&trackingId=zzz",
        "https://www.linkedin.com/comm/jobs/alerts?trackingId=aaa",
        "https://www.linkedin.com/jobs/view/123",          # id de menos de 6 digitos
        "https://www.linkedin.com/comm/in/anatorres",
        "",
    ])
    def test_linkedin_enlaces_que_no_son_avisos(self, href):
        assert id_de_enlace(LINKEDIN, href) is None

    def test_indeed_rc_clk_y_viewjob(self):
        assert id_de_enlace(
            INDEED, "https://co.indeed.com/rc/clk/dl?jk=1A2B3C4D5E6F7A8B&from=ja") \
            == "1a2b3c4d5e6f7a8b"                           # se normaliza a minusculas
        assert id_de_enlace(INDEED, "https://www.indeed.com/viewjob?jk=9f8e7d6c5b4a3921") \
            == "9f8e7d6c5b4a3921"

    @pytest.mark.parametrize("href", [
        "https://co.indeed.com/jobs?q=backend&l=Bogota",     # busqueda: sin jk
        "https://co.indeed.com/rc/clk?jk=nohex",           # jk que no es hex
        "https://co.indeed.com/rc/clk?jk=abc",             # jk demasiado corto
        "https://example.com/rc/clk?jk=1a2b3c4d5e6f7a8b",  # otro host
        "",
    ])
    def test_indeed_enlaces_que_no_son_avisos(self, href):
        assert id_de_enlace(INDEED, href) is None

    def test_proveedor_desconocido(self):
        assert id_de_enlace("glassdoor", "https://www.linkedin.com/jobs/view/123456789") is None

    def test_indeed_host_con_indeed_como_sufijo_de_otro_dominio(self):
        assert id_de_enlace(INDEED, "https://evil-indeed.com/rc/clk?jk=1a2b3c4d5e6f7a8b") is None

    def test_linkedin_ruta_de_linkedin_dentro_de_otro_host(self):
        assert id_de_enlace(LINKEDIN, "https://evil.com/r?u=linkedin.com/jobs/view/123456789") \
            is None


class TestUrlCanonica:
    def test_linkedin_sin_rastreo(self):
        href = "https://www.linkedin.com/comm/jobs/view/40123456789/?trackingId=abc%3D%3D&refId=xyz"
        url = url_canonica(LINKEDIN, href, "40123456789")
        assert url == "https://www.linkedin.com/jobs/view/40123456789"
        assert "tracking" not in url and "refId" not in url

    def test_el_mismo_aviso_con_rastreo_distinto_da_la_misma_url(self):
        a = "https://www.linkedin.com/comm/jobs/view/40123456789/?trackingId=AAA"
        b = "https://www.linkedin.com/comm/jobs/view/40123456789/?trackingId=BBB&refId=Z"
        assert url_canonica(LINKEDIN, a, "40123456789") == url_canonica(LINKEDIN, b, "40123456789")

    def test_indeed_conserva_el_host_del_pais(self):
        href = "https://co.indeed.com/rc/clk/dl?jk=1a2b3c4d5e6f7a8b&from=ja&qd=abc"
        assert url_canonica(INDEED, href, "1a2b3c4d5e6f7a8b") \
            == "https://co.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"

    def test_indeed_sin_host_cae_a_www(self):
        assert url_canonica(INDEED, "/rc/clk?jk=1a2b3c4d5e6f7a8b", "1a2b3c4d5e6f7a8b") \
            == "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"


# =============================================================================
# parsear_alerta sobre las fixtures
# =============================================================================

class TestParsearLinkedinVarios:
    @pytest.fixture(scope="class")
    def avisos(self):
        return parsear_alerta(LINKEDIN, _html("linkedin_alerta_varios.html"))

    def test_tres_avisos_con_sus_ids_y_urls_limpias(self, avisos):
        assert [a.id_aviso for a in avisos] == ["40123456789", "40234567890", "40345678901"]
        assert [a.url for a in avisos] == [
            f"https://www.linkedin.com/jobs/view/{i}"
            for i in ("40123456789", "40234567890", "40345678901")]

    def test_titulos(self, avisos):
        assert [a.titulo for a in avisos] == [
            "Senior Backend Engineer", "Python Developer", "Staff Software Engineer - Platform"]

    def test_empresa_y_ubicacion_se_separan_por_el_punto_medio(self, avisos):
        assert [(a.empresa, a.ubicacion) for a in avisos] == [
            ("Nubank", "Sao Paulo, Brazil (Remote)"),
            ("Globant", "Buenos Aires, Argentina (Remote)"),
            ("Rappi", "Bogota, Colombia (Hybrid)"),
        ]

    def test_salario_solo_donde_lo_hay(self, avisos):
        assert [a.salario for a in avisos] == ["", "$90K/yr - $120K/yr", ""]

    def test_el_ruido_no_se_cuela_en_ningun_campo(self, avisos):
        ruido = ("Actively recruiting", "Easy Apply", "connections", "early applicant",
                 "View job")
        for a in avisos:
            campos = " | ".join([a.titulo, a.empresa, a.ubicacion, a.salario])
            assert not any(r.lower() in campos.lower() for r in ruido), campos

    def test_encabezado_y_pie_no_son_avisos_ni_empresa(self, avisos):
        empresas = {a.empresa for a in avisos}
        assert not any("job alert" in e.lower() or "see all" in e.lower() for e in empresas)
        assert all("search" not in a.url and "unsubscribe" not in a.url for a in avisos)
        assert len(avisos) == 3                      # «See all jobs» y «Unsubscribe» no suman


class TestParsearLinkedinUno:
    def test_un_solo_aviso_en_castellano(self):
        avisos = parsear_alerta(LINKEDIN, _html("linkedin_alerta_uno.html"))
        assert len(avisos) == 1
        a = avisos[0]
        assert a.id_aviso == "3987654321"
        assert a.titulo == "Ingeniero Backend Python"
        assert a.url == "https://www.linkedin.com/jobs/view/3987654321"

    def test_el_tope_de_700_chars_deja_afuera_el_encabezado(self):
        # Con un solo aviso, «ningun otro aviso» se cumple hasta <body>: sin el tope la
        # tarjeta seria el correo entero y la empresa saldria del encabezado.
        a = parsear_alerta(LINKEDIN, _html("linkedin_alerta_uno.html"))[0]
        assert a.empresa == "Innova Fintech S.A.C."
        assert "alerta" not in a.empresa.lower()

    def test_empresa_y_ubicacion_en_lineas_separadas_sin_ruido(self):
        a = parsear_alerta(LINKEDIN, _html("linkedin_alerta_uno.html"))[0]
        assert a.ubicacion == "Bogota, Colombia"
        for ruido in ("Contratando activamente", "Solicitud sencilla", "Ver empleo"):
            assert ruido not in (a.titulo, a.empresa, a.ubicacion)

    def test_correo_corto_de_un_aviso_no_toma_el_encabezado_como_empresa(self):
        html = ('<html><body><h2>Tu alerta</h2><table><tr><td>'
                '<a href="https://www.linkedin.com/comm/jobs/view/3987654321/">Dev Backend</a>'
                '<div>Acme</div><div>Bogota</div></td></tr></table></body></html>')
        a = parsear_alerta(LINKEDIN, html)[0]
        assert (a.empresa, a.ubicacion) == ("Acme", "Bogota")


class TestParsearIndeed:
    @pytest.fixture(scope="class")
    def avisos(self):
        return parsear_alerta(INDEED, _html("indeed_alerta.html"))

    def test_el_aviso_repetido_con_el_mismo_jk_colapsa(self, avisos):
        # El correo trae 4 enlaces-titulo pero 3 jk distintos.
        ids = [a.id_aviso for a in avisos]
        assert ids == ["1a2b3c4d5e6f7a8b", "9f8e7d6c5b4a3921", "0a1b2c3d4e5f6071"]
        assert len(set(ids)) == len(ids)

    def test_url_conserva_co_indeed_y_no_el_rastreo(self, avisos):
        assert avisos[0].url == "https://co.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"
        assert all(a.url.startswith("https://co.indeed.com/viewjob?jk=") for a in avisos)
        assert all("qd=" not in a.url and "from=" not in a.url for a in avisos)

    def test_titulos_y_empresas(self, avisos):
        assert [(a.titulo, a.empresa) for a in avisos] == [
            ("Desarrollador Backend Python", "Banco Andino"),
            ("Ingeniero de Software Senior", "Telecom Pacifico"),
            ("Analista QA Automatizacion", "Retail Colombia Holding"),
        ]

    def test_el_rating_no_es_ubicacion_ni_empresa(self, avisos):
        for a in avisos:
            assert a.ubicacion == "Bogota, Bogota"
            assert a.empresa not in ("4.1", "3.8", "4.4")

    def test_salario_en_soles(self, avisos):
        assert avisos[0].salario == "$ 8.000.000 - $ 10.000.000 al mes"
        assert avisos[1].salario == "" and avisos[2].salario == ""

    def test_snippet_largo_y_ruido_filtrados(self, avisos):
        for a in avisos:
            for campo in (a.titulo, a.empresa, a.ubicacion, a.salario):
                assert len(campo) <= 160
                assert "Hace" not in campo and "sencilla" not in campo.lower()

    def test_pie_y_busqueda_no_son_avisos(self, avisos):
        assert len(avisos) == 3


class TestParsearCasosBorde:
    def test_sin_avisos_da_lista_vacia(self):
        assert parsear_alerta(LINKEDIN, _html("sin_avisos.html")) == []

    @pytest.mark.parametrize("html", ["", "   \n\t ", None])
    def test_html_vacio(self, html):
        assert parsear_alerta(LINKEDIN, html) == []
        assert parsear_alerta(INDEED, html) == []

    def test_proveedor_desconocido(self):
        assert parsear_alerta("glassdoor", _html("linkedin_alerta_varios.html")) == []

    def test_proveedor_cruzado_no_saca_nada(self):
        assert parsear_alerta(INDEED, _html("linkedin_alerta_varios.html")) == []
        assert parsear_alerta(LINKEDIN, _html("indeed_alerta.html")) == []

    def test_aviso_sin_titulo_legible_se_descarta(self):
        html = ('<html><body><a href="https://www.linkedin.com/comm/jobs/view/40123456789/">'
                '<img alt="logo"></a></body></html>')
        assert parsear_alerta(LINKEDIN, html) == []

    def test_aviso_sin_empresa_entra_con_empresa_vacia(self):
        relleno = "<p>" + "texto de pie de pagina. " * 40 + "</p>"
        html = ('<html><body><table><tr><td>'
                '<a href="https://www.linkedin.com/comm/jobs/view/40123456789/">Backend Dev</a>'
                '</td></tr></table>' + relleno + '</body></html>')
        [a] = parsear_alerta(LINKEDIN, html)
        assert a.titulo == "Backend Dev" and a.empresa == "" and a.ubicacion == ""


class TestAJobResult:
    def test_campos_y_dominio(self):
        a = parsear_alerta(INDEED, _html("indeed_alerta.html"))[0]
        j = g.a_job_result(a)
        assert j.title == "Desarrollador Backend Python"
        assert j.company == "Banco Andino"
        assert j.url == "https://co.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"
        assert j.location_raw == "Bogota, Bogota"
        assert j.salary_raw == "$ 8.000.000 - $ 10.000.000 al mes"
        assert j.source_domain == "indeed.com"

    def test_sin_empresa_usa_la_marca_del_resto_de_gema(self):
        j = g.a_job_result(g.AvisoCrudo(LINKEDIN, "40123456789", "https://x", "Dev"))
        assert j.company == "(empresa no listada)"
        assert j.location_raw is None and j.salary_raw is None
        assert j.source_domain == "linkedin.com"


# =============================================================================
# Remitente y consulta
# =============================================================================

class TestProveedorDeRemitente:
    @pytest.mark.parametrize("remitente, esperado", [
        ("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", LINKEDIN),
        ("jobs-listings@linkedin.com", LINKEDIN),
        ("LinkedIn <alerts@e.linkedin.com>", LINKEDIN),
        ("Indeed <alert@indeed.com>", INDEED),
        ("Indeed <donotreply@jobalert.indeed.com>", INDEED),
        ("ALERT@INDEED.COM", INDEED),
        ("Ana <ana@gmail.com>", None),
        ("", None),
        (None, None),
    ])
    def test_remitentes_legitimos_y_ajenos(self, remitente, esperado):
        assert proveedor_de_remitente(remitente) == esperado

    @pytest.mark.parametrize("remitente", [
        "x@linkedin.com.evil.com",
        "x@evil-linkedin.com",
        "x@evillinkedin.com",
        "x@indeed.com.evil.com",
        "x@evil-indeed.com",
        "Falso <jobalerts-noreply@linkedin.com.evil.com>",
    ])
    def test_dominios_que_solo_se_parecen_no_se_aceptan(self, remitente):
        assert proveedor_de_remitente(remitente) is None

    def test_arroba_en_el_nombre_visible_no_suplanta_al_dominio_real(self):
        assert proveedor_de_remitente('"jobalerts@linkedin.com" <x@evil.com>') is None


class TestConsultaGmail:
    def test_remitentes_unidos_con_or_y_ventana(self):
        assert consulta_gmail(["a@linkedin.com", "b@indeed.com"], 4) \
            == "from:(a@linkedin.com OR b@indeed.com) newer_than:4d"

    @pytest.mark.parametrize("dias", [0, -3])
    def test_la_ventana_minima_es_un_dia(self, dias):
        assert consulta_gmail(["a@linkedin.com"], dias).endswith("newer_than:1d")

    def test_dias_como_texto_numerico(self):
        assert consulta_gmail(["a@linkedin.com"], "7").endswith("newer_than:7d")


# =============================================================================
# Payload de la API de Gmail
# =============================================================================

def _b64url_sin_padding(texto: str) -> str:
    return base64.urlsafe_b64encode(texto.encode("utf-8")).decode("ascii").rstrip("=")


class TestHtmlDePayload:
    HTML = "<p>Ingeniero Backend ñandú → ¿Cali? >>> ???</p>"

    def test_multipart_anidado_base64url_sin_padding(self):
        codificado = _b64url_sin_padding(self.HTML)
        # Que el test cubra lo que dice: sin padding y con caracteres propios de base64url.
        assert not codificado.endswith("=")
        assert len(codificado) % 4 != 0
        assert "-" in codificado or "_" in codificado
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url_sin_padding("texto plano")}},
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/html", "body": {"data": codificado}},
                ]},
            ],
        }
        assert g._html_de_payload(payload) == self.HTML

    def test_html_en_el_nivel_raiz(self):
        payload = {"mimeType": "text/html", "body": {"data": _b64url_sin_padding(self.HTML)}}
        assert g._html_de_payload(payload) == self.HTML

    @pytest.mark.parametrize("payload", [
        {}, None,
        {"mimeType": "text/plain", "body": {"data": _b64url_sin_padding("solo texto")}},
        {"mimeType": "text/html", "body": {}},
        {"mimeType": "multipart/alternative", "parts": []},
    ])
    def test_sin_html_devuelve_cadena_vacia(self, payload):
        assert g._html_de_payload(payload) == ""

    def test_remitente_de_los_headers(self):
        payload = {"headers": [{"name": "Subject", "value": "x"},
                               {"name": "FROM", "value": "Indeed <alert@indeed.com>"}]}
        assert g._remitente_de(payload) == "Indeed <alert@indeed.com>"
        assert g._remitente_de({}) == ""


# =============================================================================
# traer_alertas con un lector falso
# =============================================================================

class LectorFalso:
    def __init__(self, mensajes=None, error: Exception | None = None):
        self._mensajes = mensajes or []
        self._error = error
        self.llamadas: list[tuple[str, int]] = []

    def mensajes(self, consulta, maximo):
        self.llamadas.append((consulta, maximo))
        if self._error:
            raise self._error
        return list(self._mensajes)


def _por_proveedor(resultados):
    return {r.proveedor: r for r in resultados}


LINKEDIN_REMITENTE = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
INDEED_REMITENTE = "Indeed <alert@indeed.com>"


class TestTraerAlertas:
    def test_un_resultado_por_proveedor_con_su_etiqueta(self):
        res = g.traer_alertas(lector=LectorFalso())
        assert [r.proveedor for r in res] == [LINKEDIN, INDEED]
        assert [r.etiqueta for r in res] == ["gmail:linkedin", "gmail:indeed"]
        assert all(r.jobs == [] and r.error == "" for r in res)

    def test_la_consulta_y_el_maximo_llegan_al_lector(self):
        lector = LectorFalso()
        g.traer_alertas(lector=lector, dias=2, maximo=7, remitentes=["a@linkedin.com"])
        assert lector.llamadas == [("from:(a@linkedin.com) newer_than:2d", 7)]

    def test_mismo_aviso_en_dos_alertas_entra_una_vez(self):
        html = _html("linkedin_alerta_varios.html")
        lector = LectorFalso([Mensaje("m1", LINKEDIN_REMITENTE, html),
                              Mensaje("m2", LINKEDIN_REMITENTE, html)])
        li = _por_proveedor(g.traer_alertas(lector=lector))[LINKEDIN]
        assert li.mensajes == 2
        assert li.items_crudos == 6
        assert len(li.jobs) == 3
        assert len({j.url for j in li.jobs}) == 3

    def test_los_dos_proveedores_se_separan(self):
        lector = LectorFalso([
            Mensaje("m1", LINKEDIN_REMITENTE, _html("linkedin_alerta_varios.html")),
            Mensaje("m2", INDEED_REMITENTE, _html("indeed_alerta.html")),
        ])
        res = _por_proveedor(g.traer_alertas(lector=lector))
        assert len(res[LINKEDIN].jobs) == 3 and len(res[INDEED].jobs) == 3
        assert {j.source_domain for j in res[LINKEDIN].jobs} == {"linkedin.com"}
        assert {j.source_domain for j in res[INDEED].jobs} == {"indeed.com"}

    def test_es_relevante_descarta_y_cuenta(self):
        lector = LectorFalso([Mensaje("m1", LINKEDIN_REMITENTE,
                                      _html("linkedin_alerta_varios.html"))])
        li = _por_proveedor(g.traer_alertas(
            lector=lector, es_relevante=lambda titulo: "Python" in titulo))[LINKEDIN]
        assert [j.title for j in li.jobs] == ["Python Developer"]
        assert li.descartados_irrelevantes == 2
        assert li.items_crudos == 3

    def test_el_irrelevante_no_ocupa_el_lugar_del_dedup(self):
        # Un aviso descartado igual queda «visto»: no vuelve a contarse en la 2da alerta.
        html = _html("linkedin_alerta_varios.html")
        lector = LectorFalso([Mensaje("m1", LINKEDIN_REMITENTE, html),
                              Mensaje("m2", LINKEDIN_REMITENTE, html)])
        li = _por_proveedor(g.traer_alertas(
            lector=lector, es_relevante=lambda t: False))[LINKEDIN]
        assert li.jobs == [] and li.descartados_irrelevantes == 3

    def test_remitente_desconocido_se_ignora(self):
        lector = LectorFalso([
            Mensaje("m1", "Phisher <jobs@linkedin.com.evil.com>",
                    _html("linkedin_alerta_varios.html")),
            Mensaje("m2", "Ana <ana@gmail.com>", _html("indeed_alerta.html")),
        ])
        res = g.traer_alertas(lector=lector)
        assert all(r.mensajes == 0 and r.items_crudos == 0 and r.jobs == [] for r in res)

    def test_alerta_sin_avisos_cuenta_el_mensaje_y_cero_items(self):
        lector = LectorFalso([Mensaje("m1", LINKEDIN_REMITENTE, _html("sin_avisos.html"))])
        li = _por_proveedor(g.traer_alertas(lector=lector))[LINKEDIN]
        assert li.mensajes == 1 and li.items_crudos == 0 and li.jobs == []

    def test_lector_que_levanta_deja_error_en_ambos_y_no_levanta(self):
        mensajes_log: list[str] = []
        res = g.traer_alertas(lector=LectorFalso(error=RuntimeError("Gmail se cayo")),
                              log=mensajes_log.append)
        assert len(res) == 2
        for r in res:
            assert r.jobs == []
            assert "RuntimeError" in r.error and "Gmail se cayo" in r.error
        assert any("no se leyeron las alertas" in m for m in mensajes_log)

    def test_el_error_se_trunca(self):
        res = g.traer_alertas(lector=LectorFalso(error=RuntimeError("x" * 5000)))
        assert len(res[0].error) < 300

    def test_sin_lector_y_sin_token_vuelve_con_error_y_sin_avisos(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "GMAIL_TOKEN_PATH", str(tmp_path / "no_existe.json"))
        res = g.traer_alertas()
        assert all(r.jobs == [] and "GmailNoConfigurado" in r.error for r in res)


# =============================================================================
# credenciales(): nunca contra credentials/ real
# =============================================================================

class TestCredenciales:
    def test_token_inexistente(self, tmp_path):
        with pytest.raises(GmailNoConfigurado, match="no hay token"):
            g.credenciales(str(tmp_path / "gmail_token.json"))

    def test_el_token_por_defecto_sale_de_config(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "GMAIL_TOKEN_PATH", str(tmp_path / "otro.json"))
        with pytest.raises(GmailNoConfigurado, match="otro.json"):
            g.credenciales()

    @pytest.mark.parametrize("scopes", [
        ["https://www.googleapis.com/auth/gmail.readonly",
         "https://www.googleapis.com/auth/gmail.modify"],
        ["https://www.googleapis.com/auth/gmail.modify"],
    ])
    def test_token_con_scopes_de_mas_se_rechaza(self, tmp_path, scopes):
        pytest.importorskip("google.oauth2.credentials")   # si no, fallaria por otra razon
        token = tmp_path / "gmail_token.json"
        token.write_text(json.dumps({
            "token": "ya29.falso", "refresh_token": "1//falso",
            "client_id": "id.apps.googleusercontent.com", "client_secret": "secreto",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes,
        }), encoding="utf-8")
        with pytest.raises(GmailNoConfigurado, match="permisos distintos"):
            g.credenciales(str(token))


# =============================================================================
# Flag de config
# =============================================================================

class TestFlagApagadoPorDefecto:
    """
    Cada caso importa `config` en un SUBPROCESO, con `dotenv.load_dotenv` anulado.

    Dentro del proceso de la suite no se puede: `importlib.reload(config)` deshace los
    parches de los demas tests, y `GEMA_DB_PATH` del `.env` del usuario apunta a la base real
    (hallazgo del 03-10). Y sin anular `load_dotenv`, el `.env` (override=True) decidiria
    el valor en vez de la variable de entorno del caso.
    """

    _CODIGO = ("import dotenv; dotenv.load_dotenv = lambda *a, **k: False; "
               "import config; print(config.GMAIL_ALERTAS_HABILITADO)")

    @pytest.mark.parametrize("valor, esperado", [
        (None, False), ("", False), ("0", False), ("no", False), ("basura", False),
        ("1", True), ("true", True), ("SI", True), (" on ", True),
    ])
    def test_valor_de_la_variable_de_entorno(self, valor, esperado):
        import os
        import subprocess
        import sys
        entorno = dict(os.environ)
        entorno.pop("GEMA_GMAIL_ALERTAS", None)
        if valor is not None:
            entorno["GEMA_GMAIL_ALERTAS"] = valor
        r = subprocess.run([sys.executable, "-c", self._CODIGO], env=entorno,
                           cwd=Path(__file__).resolve().parent.parent,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(esperado)
