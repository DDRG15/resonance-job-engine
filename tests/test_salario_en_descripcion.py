"""
Greenhouse publica el salario en el TEXTO, no en un campo.

Verificado el 2026-09-28 contra la API real: ninguna de las tres vias
—listado pelado, `?content=true` y `/jobs/{id}`— devuelve una sola clave de pago.
Lo que si trae `content` es la descripcion, y ahi esta el rango porque las leyes de
transparencia salarial de California, Colorado, Nueva York y Washington obligan a
publicarlo.

Medido sobre 905 avisos: gitlab 84/199 (42,2%), cloudflare 157/400 (39,2%),
canonical 1/306 — canonical es britanica y esas leyes no la alcanzan.
"""

import pytest

import ats_registry
import endpoint_scraper as es


# El fragmento es REAL: copiado del `content` de gitlab tal como lo manda la API,
# con el doble escapado incluido. Un fixture inventado no habria tenido `&amp;mdash;`
# y el test habria pasado con el bug adentro.
GITLAB_CRUDO = (
    "&lt;div class=&quot;pay-input-container&quot;&gt;&lt;div class=&quot;title&quot;&gt;"
    "United States Salary Range&lt;/div&gt;&lt;div class=&quot;pay-range&quot;&gt;"
    "&lt;span&gt;$139,200&lt;/span&gt;&lt;span class=&quot;divider&quot;&gt;&amp;mdash;"
    "&lt;/span&gt;&lt;span&gt;$235,200 USD&lt;/span&gt;&lt;/div&gt;&lt;/div&gt;"
)

CLOUDFLARE_CRUDO = (
    "&lt;p&gt;For New York based hires: Estimated annual salary of "
    "$166,000 - $208,000.&lt;/p&gt;"
)


class TestTextoDeHtml:
    def test_desescapa_hasta_estabilizar(self):
        """
        El bug que este test congela: con UNA vuelta de unescape, `&amp;mdash;` queda
        como `&mdash;` literal en el medio del texto, el extractor no ve el separador
        y devuelve solo el piso. Los 84 avisos de gitlab con salario pasaban de rango
        completo a numero suelto por esa unica vuelta.
        """
        texto = es.texto_de_html(GITLAB_CRUDO)
        assert "&mdash;" not in texto, "quedo una entidad literal: falto una vuelta"
        assert "—" in texto
        assert "$139,200" in texto and "$235,200" in texto
        assert "<div" not in texto and "&lt;" not in texto

    def test_colapsa_espacios_y_no_deja_etiquetas(self):
        assert es.texto_de_html("&lt;p&gt;  a   b  &lt;/p&gt;") == "a b"

    def test_entrada_vacia_o_nula(self):
        assert es.texto_de_html(None) == ""
        assert es.texto_de_html("") == ""

    def test_no_se_cuelga_con_escapado_anidado(self):
        """Tope de tres vueltas: una entrada adversaria no se vuelve trabajo cuadratico."""
        bomba = "&" + "amp;" * 500 + "mdash;"
        assert isinstance(es.texto_de_html(bomba), str)


class TestSalarioDesdeDescripcion:
    def test_gitlab_da_el_rango_completo(self):
        s = es._salario({"content": GITLAB_CRUDO}, {}, "content")
        # Sin espacios alrededor del guion: los tres <span> son inline y el navegador
        # los dibuja pegados. Lo que importa es que venga el RANGO: sin las vueltas de
        # unescape esto devolvia '$139,200' a secas.
        assert s == "$139,200—$235,200", (
            "sin las vueltas de unescape esto devuelve '$139,200' a secas"
        )

    def test_cloudflare_en_prosa(self):
        assert es._salario({"content": CLOUDFLARE_CRUDO}, {}, "content") == \
            "$166,000 - $208,000"

    def test_apagado_por_defecto(self):
        """Sin el flag, la descripcion no se mira. Ninguna otra plataforma paga el costo."""
        assert es._salario({"content": GITLAB_CRUDO}, {}) is None

    def test_un_campo_declarado_le_gana_a_la_prosa(self):
        """
        Ashby publica `compensationTierSummary`. Un numero pescado del texto no puede
        pisar un campo que la plataforma declara: el campo es dato, la prosa es indicio.
        """
        fila = {"content": GITLAB_CRUDO, "comp": "$220K - $300K"}
        assert es._salario(fila, {"salary_raw": "comp"}, "content") == "$220K - $300K"

    def test_min_max_tambien_le_ganan(self):
        fila = {"content": GITLAB_CRUDO, "lo": 90000, "hi": 120000}
        assert es._salario(fila, {"salary_min": "lo", "salary_max": "hi"}, "content") == \
            "$90,000 - $120,000"

    def test_descripcion_sin_plata_no_inventa(self):
        """Un salario inventado es peor que ninguno: alimenta el score con ruido."""
        fila = {"content": "&lt;p&gt;We offer 401k matching and equity.&lt;/p&gt;"}
        assert es._salario(fila, {}, "content") is None

    def test_campo_ausente_o_vacio(self):
        assert es._salario({}, {}, "content") is None
        assert es._salario({"content": ""}, {}, "content") is None


class TestGreenhouseCableado:
    def test_pide_content_true(self):
        gh = ats_registry.PLATAFORMAS["greenhouse"]
        assert "content=true" in gh.url_template, (
            "sin el flag en la URL, `content` no viene y no hay de donde sacar el rango"
        )

    def test_declara_el_campo_de_la_descripcion(self):
        assert ats_registry.PLATAFORMAS["greenhouse"].salario_en_descripcion == "content"

    def test_las_otras_plataformas_siguen_apagadas(self):
        """
        Lever no trae descripcion en el listado y Ashby ya publica compensacion como
        campo. Encender esto ahi seria pagar un parseo por aviso sin nada que ganar.
        """
        for nombre in ("lever", "ashby"):
            assert ats_registry.PLATAFORMAS[nombre].salario_en_descripcion == ""

    def test_ashby_sigue_leyendo_su_campo(self):
        assert ats_registry.PLATAFORMAS["ashby"].field_map["salary_raw"] == \
            "compensation.compensationTierSummary"


class TestFilaCompletaDeGreenhouse:
    def test_un_aviso_de_greenhouse_sale_con_salario(self):
        """La fila cruda entera, con la forma que devuelve la API."""
        fila = {
            "title": "Backend Engineer, AI",
            "company_name": "GitLab",
            "absolute_url": "https://boards.greenhouse.io/gitlab/jobs/8556658002",
            "location": {"name": "Remote, US"},
            "application_deadline": None,
            "content": GITLAB_CRUDO,
        }
        gh = ats_registry.PLATAFORMAS["greenhouse"]
        job = es.a_job_result(fila, gh.field_map, "boards.greenhouse.io",
                              salario_en_descripcion=gh.salario_en_descripcion)

        assert job is not None
        assert job.title == "Backend Engineer, AI"
        assert job.company == "GitLab"
        assert job.salary_raw == "$139,200—$235,200"
        assert job.location_raw == "Remote, US"

    def test_el_rango_se_convierte_en_numero(self):
        """
        El eslabon que hace util a todo lo demas: si el string no parsea, el eje de
        salario sigue muerto aunque el campo este lleno. `parse_salary_usd` toma el
        MINIMO de un rango, que es la lectura conservadora para el gate de salario.
        """
        fila = {"title": "X", "company_name": "GitLab",
                "absolute_url": "https://boards.greenhouse.io/gitlab/jobs/1",
                "location": {"name": "Remote"}, "content": GITLAB_CRUDO}
        gh = ats_registry.PLATAFORMAS["greenhouse"]
        job = es.a_job_result(fila, gh.field_map, "boards.greenhouse.io",
                              salario_en_descripcion=gh.salario_en_descripcion)
        assert job.parse_salary_usd() == 139200

    def test_sin_salario_el_aviso_igual_entra(self):
        """Greenhouse trae ~58% sin rango publicado. Esos avisos no se pierden."""
        fila = {"title": "X", "company_name": "GitLab",
                "absolute_url": "https://boards.greenhouse.io/gitlab/jobs/2",
                "location": {"name": "Remote"},
                "content": "&lt;p&gt;Join us.&lt;/p&gt;"}
        gh = ats_registry.PLATAFORMAS["greenhouse"]
        job = es.a_job_result(fila, gh.field_map, "boards.greenhouse.io",
                              salario_en_descripcion=gh.salario_en_descripcion)
        assert job is not None
        assert job.salary_raw is None


class TestCodigoDeMonedaDelante:
    """
    Regresion hallada en la corrida en vivo del 2026-09-29: Grafana Labs publica en
    Greenhouse "EUR 109,000 - EUR 137,000" y el extractor devolvia "EUR 109" — la
    forma con codigo delante no admitia miles y cortaba en la coma. parse_salary_usd
    ademas solo entendia el codigo DETRAS. Resultado: 10 avisos europeos a Tier 4.
    """

    # El fragmento es REAL, de job-boards.greenhouse.io/grafanalabs/jobs/6202561004.
    GRAFANA = ("In Germany, the Base compensation range for this role is "
               "EUR 109,000 - EUR 137,000 . Actual compensation may vary")

    def test_el_extractor_trae_el_rango_completo(self):
        from scraper import extract_salary_from_text
        assert extract_salary_from_text(self.GRAFANA) == "EUR 109,000 - EUR 137,000"
        assert extract_salary_from_text("GBP 103,000 - GBP 129,000") == \
            "GBP 103,000 - GBP 129,000"

    def test_el_parser_lo_convierte_a_usd(self):
        import config
        from models import JobResult
        j = JobResult(title="x", company="Grafana Labs", url="https://x.com/1",
                      source_domain="boards.greenhouse.io",
                      salary_raw="EUR 109,000 - EUR 137,000")
        assert j.parse_salary_usd() == int(109000 * config.TASAS_A_USD["eur"])

    @pytest.mark.parametrize("crudo", ["EUR 109", "EUR 90", "401k match", "GBP 91"])
    def test_sin_miles_ni_k_no_inventa_un_salario(self, crudo):
        """Los 10 guardados como 'EUR 109' NO pueden leerse como 109 euros al ano."""
        from models import JobResult
        j = JobResult(title="x", company="y", url="https://x.com/1",
                      source_domain="d", salary_raw=crudo)
        assert j.parse_salary_usd() is None

    def test_codigo_delante_con_k(self):
        from models import JobResult
        j = JobResult(title="x", company="y", url="https://x.com/1",
                      source_domain="d", salary_raw="USD 120k")
        assert j.parse_salary_usd() == 120000


class TestNumeroPartidoPorEtiquetaInline:
    """
    Grafana Labs pega los rangos desde Google Sheets y el numero llega partido por un
    <span>. Reemplazar toda etiqueta por un espacio daba "EUR 97 ,000" -> "EUR 97".
    Fragmento REAL de job-boards.greenhouse.io/grafanalabs (2026-09-29).
    """

    SHEETS = ('role is EUR 97<span data-sheets-root="1">,000</span>\xa0- EUR '
              '<span data-sheets-root="1">121,000</span> . Actual')

    def test_el_numero_no_se_parte(self):
        from scraper import extract_salary_from_text
        assert es.texto_de_html(self.SHEETS) == "role is EUR 97,000 - EUR 121,000 . Actual"
        assert extract_salary_from_text(es.texto_de_html(self.SHEETS)) == \
            "EUR 97,000 - EUR 121,000"

    def test_las_etiquetas_de_bloque_siguen_separando(self):
        assert es.texto_de_html("<p>Uno</p><p>Dos</p><ul><li>Tres</li></ul>") == "Uno Dos Tres"
        assert es.texto_de_html("<br>linea<blockquote>cita</blockquote>fin") == "linea cita fin"

    def test_inline_no_agrega_espacios(self):
        assert es.texto_de_html("<p>Senior <strong>Backend</strong> Engineer</p>") == \
            "Senior Backend Engineer"


class TestPisoDeSueldoAnual:
    """
    Una descripcion entera trae otras cifras con moneda. Medido el 2026-09-29: 239
    avisos de Canonical dicen "Personal learning and development budget of USD 2,000
    per year" y tomar el PRIMER monto guardaba un sueldo de 2.000 USD al ano.
    """

    CANONICAL = ("Distributed work environment with twice-yearly team sprints in person "
                 "Personal learning and development budget of USD 2,000 per year "
                 "Annual compensation review")

    def test_un_presupuesto_no_es_un_sueldo(self):
        assert es._salario({"content": self.CANONICAL}, {}, "content") is None

    def test_el_sueldo_real_gana_aunque_venga_despues(self):
        texto = ("Learning budget of USD 2,000 per year. The base salary range for this "
                 "role is $140,000 - $180,000.")
        assert es._salario({"content": texto}, {}, "content") == "$140,000 - $180,000"
