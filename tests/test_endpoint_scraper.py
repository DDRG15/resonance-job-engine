"""
tests/test_endpoint_scraper.py — el vector sin navegador (Fase 1).

Cero red: toda respuesta la sirve una sesión falsa. Los cuerpos son recortes de las
respuestas reales medidas el 2026-09-14, no invenciones — un test contra un JSON
inventado valida el parser contra la imaginación del que lo escribió.
"""

import json

import pytest

import endpoint_scraper as es


# ─────────────────────────────────────────────────────────────────────────────
# Dobles
# ─────────────────────────────────────────────────────────────────────────────

class RespuestaFalsa:
    def __init__(self, status, contenido=b"", headers=None):
        self.status_code = status
        self.content = contenido
        self.headers = headers or {}


class SesionFalsa:
    """Devuelve respuestas de una cola. Registra cada URL pedida."""

    def __init__(self, *respuestas):
        self.cola = list(respuestas)
        self.pedidos = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.pedidos.append(url)
        if not self.cola:
            raise AssertionError(f"pedido inesperado a {url}")
        r = self.cola.pop(0)
        return r if len(self.cola) else r


@pytest.fixture(autouse=True)
def _sin_dormir(monkeypatch):
    """El backoff es real y se testea, pero nadie quiere esperarlo."""
    dormidas = []
    monkeypatch.setattr(es, "_dormir", lambda s: dormidas.append(s))
    return dormidas


# Recortes reales (remoteok.com/api, 2026-09-14)
REMOTEOK = json.dumps([
    {"legal": "See https://remoteok.com/api for terms"},
    {"position": "Senior Backend Engineer", "company": "Salesforge",
     "url": "https://remoteok.com/remote-jobs/123-senior-backend",
     "location": "Worldwide", "salary_min": 90000, "salary_max": 150000},
    {"position": "Social Comms", "company": "Acme",
     "url": "https://remoteok.com/remote-jobs/124-social",
     "location": "", "salary_min": 0, "salary_max": 0},
]).encode()

# Recorte real (weworkremotely.com/remote-jobs.rss, 2026-09-14)
WWR = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>We Work Remotely: Remote jobs</title>
  <item>
    <title>Reddit: Backend Engineer, IAM</title>
    <link>https://weworkremotely.com/remote-jobs/reddit-backend-engineer-iam</link>
    <region>Anywhere in the World</region>
    <expires_at>Fri, 09 Oct 2026 15:41:42 +0000</expires_at>
  </item>
  <item>
    <title>Recruitment Circle GmbH: Werkstudent:in im Headhunting</title>
    <link>https://weworkremotely.com/remote-jobs/rc-werkstudent</link>
    <region>Anywhere in the World</region>
  </item>
</channel></rss>"""

FM_REMOTEOK = {"title": "position", "company": "company", "url": "url",
               "location": "location", "salary_min": "salary_min",
               "salary_max": "salary_max"}
FM_WWR = {"title": "title", "url": "link", "location": "region"}


# ─────────────────────────────────────────────────────────────────────────────
# fetch — política de reintentos
# ─────────────────────────────────────────────────────────────────────────────

class TestFetch:
    def test_200_devuelve_el_cuerpo(self):
        ses = SesionFalsa(RespuestaFalsa(200, b"hola"))
        assert es.fetch("https://x.com/api", sesion=ses) == b"hola"

    def test_403_no_se_reintenta(self):
        """Reintentar un 403 gasta cuota para confirmar lo que el server ya dijo."""
        ses = SesionFalsa(RespuestaFalsa(403))
        with pytest.raises(es.EndpointNoPublico):
            es.fetch("https://x.com/api", sesion=ses)
        assert len(ses.pedidos) == 1

    def test_401_tampoco(self):
        ses = SesionFalsa(RespuestaFalsa(401))
        with pytest.raises(es.EndpointNoPublico):
            es.fetch("https://x.com/api", sesion=ses)

    def test_429_reintenta_y_al_final_se_agota(self, _sin_dormir):
        ses = SesionFalsa(*[RespuestaFalsa(429) for _ in range(es.MAX_REINTENTOS)])
        with pytest.raises(es.EndpointAgotado):
            es.fetch("https://x.com/api", sesion=ses)
        assert len(ses.pedidos) == es.MAX_REINTENTOS
        # Backoff exponencial: cada espera mayor que la anterior.
        assert _sin_dormir == sorted(_sin_dormir)
        assert len(_sin_dormir) == es.MAX_REINTENTOS - 1

    def test_429_que_se_recupera_devuelve_el_cuerpo(self):
        ses = SesionFalsa(RespuestaFalsa(429), RespuestaFalsa(200, b"ok"))
        assert es.fetch("https://x.com/api", sesion=ses) == b"ok"

    def test_retry_after_gana_sobre_el_backoff(self, _sin_dormir):
        """Si el server dice cuánto esperar, esperar otra cosa es pelearse con
        quien te está dejando entrar."""
        ses = SesionFalsa(RespuestaFalsa(429, headers={"Retry-After": "7"}),
                          RespuestaFalsa(200, b"ok"))
        es.fetch("https://x.com/api", sesion=ses)
        assert _sin_dormir == [7.0]

    def test_retry_after_basura_no_rompe(self, _sin_dormir):
        ses = SesionFalsa(RespuestaFalsa(429, headers={"Retry-After": "Wed, 21 Oct"}),
                          RespuestaFalsa(200, b"ok"))
        assert es.fetch("https://x.com/api", sesion=ses) == b"ok"

    def test_5xx_reintenta(self):
        ses = SesionFalsa(RespuestaFalsa(503), RespuestaFalsa(200, b"ok"))
        assert es.fetch("https://x.com/api", sesion=ses) == b"ok"

    def test_404_no_reintenta(self):
        ses = SesionFalsa(RespuestaFalsa(404))
        with pytest.raises(es.EndpointAgotado):
            es.fetch("https://x.com/api", sesion=ses)
        assert len(ses.pedidos) == 1

    def test_crawl_delay_se_respeta(self, _sin_dormir):
        """api.lever.co declara Crawl-delay: 1 en su robots.txt."""
        ses = SesionFalsa(RespuestaFalsa(200, b"ok"))
        es.fetch("https://api.lever.co/x", crawl_delay_s=1.0, sesion=ses)
        assert 1.0 in _sin_dormir


# ─────────────────────────────────────────────────────────────────────────────
# Parseo
# ─────────────────────────────────────────────────────────────────────────────

class TestParseo:
    def test_json_lista_suelta_descarta_el_objeto_legal(self):
        filas = es.items_de_json(REMOTEOK)
        assert len(filas) == 2
        assert all("legal" not in f for f in filas)

    def test_json_envuelto_en_jobs(self):
        cuerpo = json.dumps({"jobs": [{"title": "a"}, {"title": "b"}]}).encode()
        assert len(es.items_de_json(cuerpo)) == 2

    def test_json_con_items_path_explicito(self):
        cuerpo = json.dumps({"data": {"listings": [{"title": "a"}]}}).encode()
        assert len(es.items_de_json(cuerpo, "data.listings")) == 1

    def test_json_sin_lista_devuelve_vacio_sin_romper(self):
        assert es.items_de_json(b'{"mensaje": "sin resultados"}') == []

    def test_rss_cuenta_items(self):
        assert len(es.items_de_rss(WWR)) == 2

    def test_rss_lee_expires_at(self):
        """`expires_at` está en 90 de 90 items del feed real — es la fuente del
        `deadline` de la Fase 6."""
        assert es.items_de_rss(WWR)[0]["expires_at"].startswith("Fri, 09 Oct 2026")

    def test_rss_atom_lee_el_href_del_link(self):
        atom = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Dev</title><link href="https://x.com/1"/></entry>
        </feed>"""
        assert es.items_de_rss(atom)[0]["link"] == "https://x.com/1"

    def test_por_path_anidado(self):
        assert es._por_path({"location": {"name": "Remote"}}, "location.name") == "Remote"

    def test_por_path_cortado_devuelve_none(self):
        assert es._por_path({"location": None}, "location.name") is None


# ─────────────────────────────────────────────────────────────────────────────
# Mapeo a JobResult
# ─────────────────────────────────────────────────────────────────────────────

class TestMapeo:
    def test_compone_salario_de_min_y_max(self):
        fila = es.items_de_json(REMOTEOK)[0]
        job = es.a_job_result(fila, FM_REMOTEOK, "remoteok.com")
        assert job.salary_raw == "$90,000 - $150,000"

    def test_el_salario_compuesto_lo_entiende_el_parser_existente(self):
        """Componer al formato que `parse_salary_usd()` ya lee mantiene una sola
        ruta de parseo en todo el sistema."""
        fila = es.items_de_json(REMOTEOK)[0]
        job = es.a_job_result(fila, FM_REMOTEOK, "remoteok.com")
        assert job.parse_salary_usd() == 90000

    def test_salario_en_cero_es_ausencia_no_cero(self):
        fila = es.items_de_json(REMOTEOK)[1]
        job = es.a_job_result(fila, FM_REMOTEOK, "remoteok.com")
        assert job.salary_raw is None

    def test_ubicacion_vacia_queda_none(self):
        fila = es.items_de_json(REMOTEOK)[1]
        assert es.a_job_result(fila, FM_REMOTEOK, "remoteok.com").location_raw is None

    def test_sin_titulo_se_descarta(self):
        assert es.a_job_result({"url": "https://x.com/1"}, {"title": "t", "url": "url"},
                               "x.com") is None

    def test_url_relativa_sin_base_se_descarta(self):
        """La URL es la clave de idempotencia (`compute_hash`). Una relativa
        envenena la deduplicación: mejor perder el aviso que corromper el registro."""
        assert es.a_job_result({"t": "Dev", "u": "/jobs/1"}, {"title": "t", "url": "u"},
                               "x.com") is None

    def test_url_relativa_con_base_se_absolutiza(self):
        job = es.a_job_result({"t": "Dev", "u": "/jobs/1"}, {"title": "t", "url": "u"},
                              "x.com", url_base="https://x.com/api/feed")
        assert job.url == "https://x.com/jobs/1"

    def test_empresa_ausente_degrada_no_descarta(self):
        """Decisión de la Fase 31 (07-11): exigir company nuleaba boards enteros."""
        job = es.a_job_result({"t": "Dev", "u": "https://x.com/1"},
                              {"title": "t", "url": "u"}, "x.com")
        assert job.company == "(empresa no listada)"

    def test_html_en_el_campo_se_limpia(self):
        job = es.a_job_result({"t": "<b>Dev</b>  &nbsp;", "u": "https://x.com/1"},
                              {"title": "t", "url": "u"}, "x.com")
        assert job.title == "Dev &nbsp;"


class TestEmpresaEnElTitulo:
    """weworkremotely.com mete la empresa en el título: 87 de 87 items del feed real."""

    def test_parte_en_el_primer_dos_puntos(self):
        assert es._partir_prefijo_empresa("Reddit: Backend Engineer") == \
            ("Backend Engineer", "Reddit")

    def test_no_parte_dos_puntos_sin_espacio(self):
        """`Werkstudent:in` es alemán, no un separador."""
        assert es._partir_prefijo_empresa("Werkstudent:in") == ("Werkstudent:in", "")

    def test_parte_solo_en_el_primero(self):
        titulo, empresa = es._partir_prefijo_empresa(
            "Recruitment Circle GmbH: Werkstudent:in im Headhunting")
        assert empresa == "Recruitment Circle GmbH"
        assert titulo == "Werkstudent:in im Headhunting"

    def test_sin_separador_vuelve_intacto(self):
        assert es._partir_prefijo_empresa("Backend Engineer") == ("Backend Engineer", "")

    def test_lado_vacio_no_parte(self):
        assert es._partir_prefijo_empresa(": Backend") == (": Backend", "")

    def test_aplicado_al_feed_completo(self):
        r = es.scrape_endpoint(
            "weworkremotely.com",
            endpoint_template="https://weworkremotely.com/remote-jobs.rss",
            response_format="rss", field_map=FM_WWR, titulo_trae_empresa=True,
            sesion=SesionFalsa(RespuestaFalsa(200, WWR)))
        assert [j.company for j in r.jobs] == ["Reddit", "Recruitment Circle GmbH"]
        assert r.jobs[0].title == "Backend Engineer, IAM"


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación
# ─────────────────────────────────────────────────────────────────────────────

class TestScrapeEndpoint:
    def test_feed_fijo_se_pide_una_sola_vez(self):
        """`remoteok.com/api` no acepta búsqueda: pedirlo una vez por título sería
        pedir tres veces lo mismo."""
        ses = SesionFalsa(RespuestaFalsa(200, REMOTEOK))
        es.scrape_endpoint("remoteok.com", endpoint_template="https://remoteok.com/api",
                           field_map=FM_REMOTEOK, titles=["python", "backend", "data"],
                           sesion=ses)
        assert len(ses.pedidos) == 1

    def test_template_con_title_se_pide_por_titulo(self):
        ses = SesionFalsa(RespuestaFalsa(200, b"[]"), RespuestaFalsa(200, b"[]"))
        es.scrape_endpoint("x.com", endpoint_template="https://x.com/api?q={title}",
                           field_map=FM_REMOTEOK, titles=["python dev", "backend"],
                           sesion=ses)
        assert len(ses.pedidos) == 2
        assert "python%20dev" in ses.pedidos[0]

    def test_filtra_por_relevancia_de_titulo(self):
        r = es.scrape_endpoint(
            "remoteok.com", endpoint_template="https://remoteok.com/api",
            field_map=FM_REMOTEOK,
            es_relevante=lambda t: "backend" in t.lower(),
            sesion=SesionFalsa(RespuestaFalsa(200, REMOTEOK)))
        assert len(r.jobs) == 1
        assert r.descartados_irrelevantes == 1

    def test_sin_filtro_devuelve_todo(self):
        r = es.scrape_endpoint("remoteok.com",
                               endpoint_template="https://remoteok.com/api",
                               field_map=FM_REMOTEOK,
                               sesion=SesionFalsa(RespuestaFalsa(200, REMOTEOK)))
        assert len(r.jobs) == 2

    def test_403_no_mata_la_corrida_devuelve_error(self):
        """Un board caído reporta y sigue. Que un endpoint falle no puede tumbar
        el run — es una regresión contra el comportamiento actual."""
        r = es.scrape_endpoint("x.com", endpoint_template="https://x.com/api",
                               field_map=FM_REMOTEOK,
                               sesion=SesionFalsa(RespuestaFalsa(403)))
        assert r.jobs == []
        assert "EndpointNoPublico" in r.error

    def test_json_corrupto_no_mata_la_corrida(self):
        r = es.scrape_endpoint("x.com", endpoint_template="https://x.com/api",
                               field_map=FM_REMOTEOK,
                               sesion=SesionFalsa(RespuestaFalsa(200, b"{no es json")))
        assert r.jobs == []
        assert "parseo json" in r.error

    def test_deduplica_por_url_dentro_de_la_corrida(self):
        cuerpo = json.dumps([
            {"position": "Dev", "company": "A", "url": "https://x.com/1"},
            {"position": "Dev", "company": "A", "url": "https://x.com/1"},
        ]).encode()
        r = es.scrape_endpoint("x.com", endpoint_template="https://x.com/api",
                               field_map=FM_REMOTEOK,
                               sesion=SesionFalsa(RespuestaFalsa(200, cuerpo)))
        assert len(r.jobs) == 1

    def test_url_relativa_se_resuelve_contra_el_endpoint(self):
        """Dentro de `scrape_endpoint` siempre hay base, así que un href relativo
        se absolutiza en vez de descartarse. El descarte solo ocurre cuando no hay
        base — ver `TestMapeo.test_url_relativa_sin_base_se_descarta`."""
        cuerpo = json.dumps([
            {"position": "Dev", "company": "A", "url": "/jobs/7"}]).encode()
        r = es.scrape_endpoint("x.com", endpoint_template="https://x.com/api",
                               field_map=FM_REMOTEOK,
                               sesion=SesionFalsa(RespuestaFalsa(200, cuerpo)))
        assert r.jobs[0].url == "https://x.com/jobs/7"

    def test_cuenta_los_descartes_por_separado(self):
        """Off-topic y sin-URL usable son causas distintas y se cuentan aparte: un
        board que descarta todo por una razón se diagnostica distinto que por la otra.
        `mailto:` pasa por `urljoin` sin cambiar y no es una URL de aviso."""
        cuerpo = json.dumps([
            {"position": "Backend Engineer", "company": "A", "url": "mailto:rh@a.com"},
            {"position": "Chef", "company": "B", "url": "https://x.com/2"},
        ]).encode()
        r = es.scrape_endpoint("x.com", endpoint_template="https://x.com/api",
                               field_map=FM_REMOTEOK,
                               es_relevante=lambda t: "engineer" in t.lower(),
                               sesion=SesionFalsa(RespuestaFalsa(200, cuerpo)))
        assert r.descartados_invalidos == 1
        assert r.descartados_irrelevantes == 1
        assert r.jobs == []

    def test_el_dominio_queda_en_el_job(self):
        r = es.scrape_endpoint("remoteok.com",
                               endpoint_template="https://remoteok.com/api",
                               field_map=FM_REMOTEOK,
                               sesion=SesionFalsa(RespuestaFalsa(200, REMOTEOK)))
        assert all(j.source_domain == "remoteok.com" for j in r.jobs)
