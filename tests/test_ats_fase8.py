"""
tests/test_ats_fase8.py — Recruitee, Workable, BambooHR y Personio.

Cero red. Las fixtures de `tests/fixtures/ats/` son recortes de las respuestas reales
pedidas el 2026-10-04 (claves que GEMA no lee fuera, textos a 600 caracteres, menos
avisos); los cuerpos cortos que van incrustados —el 404 de Recruitee, el `jobs: []` de
Hotjar, el `result: []` de Zapier— son la respuesta entera, copiada tal cual.
"""

import json
from pathlib import Path

import pytest

import ats_registry
import ats_scraper
import endpoint_scraper as es
import metrics
from ats_registry import FuenteATS

FIX = Path(__file__).parent / "fixtures" / "ats"
NUEVAS = ("recruitee", "workable", "bamboohr", "personio")


def _cuerpo(nombre: str) -> bytes:
    return (FIX / nombre).read_bytes()


@pytest.fixture(autouse=True)
def _sin_dormir(monkeypatch):
    monkeypatch.setattr(es, "_dormir", lambda s: None)


@pytest.fixture
def emitidas(monkeypatch):
    """Las metricas que emite el camino, en orden: (nombre, valor, etiquetas)."""
    lista = []
    monkeypatch.setattr(metrics, "emit",
                        lambda nombre, valor=1, **et: lista.append((nombre, valor, et)))
    return lista


@pytest.fixture
def servir(monkeypatch):
    """Parchea `es.fetch` para devolver `cuerpo` y registra URL y kwargs de cada pedido."""
    pedidos = []

    def _instalar(cuerpo: bytes):
        def _fetch(url, **kw):
            pedidos.append((url, kw))
            return cuerpo
        monkeypatch.setattr(es, "fetch", _fetch)
        return pedidos
    return _instalar


def _fallar_con(monkeypatch, exc):
    def _fetch(url, **kw):
        raise exc
    monkeypatch.setattr(es, "fetch", _fetch)


class RespuestaFalsa:
    def __init__(self, status, contenido=b"", headers=None):
        self.status_code, self.content, self.headers = status, contenido, headers or {}


class SesionConRedirecciones:
    """Como la sesion falsa de test_ats, pero registra `allow_redirects`."""

    def __init__(self, *respuestas):
        self.cola, self.pedidos, self.headers = list(respuestas), [], {}

    def get(self, url, timeout=None, allow_redirects=True):
        self.pedidos.append((url, allow_redirects))
        return self.cola.pop(0)


# ─────────────────────────────────────────────────────────────────────────────
# El registro
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistroFase8:
    def test_las_cuatro_plataformas_estan_registradas(self):
        for p in NUEVAS:
            assert p in ats_registry.PLATAFORMAS, p

    def test_d1_pendiente_ninguna_fuente_nueva_esta_activa(self):
        """Sumar volumen con un Tier 1 que es el 46% de lo puntuado es ruido.
        Hasta que el usuario decida activarlas, las plataformas nuevas no aportan avisos."""
        assert ats_registry.PENDIENTES_ACTIVACION == frozenset(NUEVAS)
        for f in ats_registry.FUENTES:
            if f.plataforma in ats_registry.PENDIENTES_ACTIVACION:
                assert f.activa is False, f"{f.plataforma}:{f.token}"
                assert "pendiente" in f.nota, f"{f.plataforma}:{f.token} sin nota de pendiente"

    def test_toda_plataforma_tiene_al_menos_una_fuente_registrada(self):
        registradas = {f.plataforma for f in ats_registry.FUENTES}
        assert set(ats_registry.PLATAFORMAS) <= registradas

    def test_los_tokens_son_segmentos_de_url_inofensivos(self):
        """El token va dentro del host o de la ruta. Uno con `/` o `.` de mas pediria
        otra cosa que la que dice el registro."""
        import re
        for f in ats_registry.FUENTES:
            assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", f.token), f.token

    def test_personio_espera_5_segundos_y_va_de_a_uno(self):
        """El 30-09 el tercer pedido seguido devolvio 429. Espaciados 5 s, pasaron todos."""
        p = ats_registry.PLATAFORMAS["personio"]
        assert p.crawl_delay_s >= 5.0
        assert p.concurrencia == 1

    def test_las_nuevas_no_siguen_redirecciones(self):
        for p in NUEVAS:
            assert ats_registry.PLATAFORMAS[p].seguir_redirecciones is False, p

    def test_las_viejas_no_cambian_de_comportamiento(self):
        for p in ("greenhouse", "lever", "ashby"):
            plat = ats_registry.PLATAFORMAS[p]
            assert plat.seguir_redirecciones is True
            assert plat.url_aviso_template == ""

    def test_las_que_no_traen_url_la_componen(self):
        """BambooHR y Personio no publican la URL del aviso: sin plantilla, los 50
        avisos medidos se descartarian todos como invalidos."""
        for p in ("bamboohr", "personio"):
            plat = ats_registry.PLATAFORMAS[p]
            assert "{token}" in plat.url_aviso_template and "{id}" in plat.url_aviso_template
            assert "url" not in plat.field_map

    def test_dominio_por_plataforma(self):
        assert ats_registry.dominio_de("recruitee") == "recruitee.com"
        assert ats_registry.dominio_de("workable") == "apply.workable.com"
        assert ats_registry.dominio_de("bamboohr") == "bamboohr.com"
        assert ats_registry.dominio_de("personio") == "jobs.personio.de"

    def test_apagadas_no_entran_a_los_dominios_activos(self):
        for p in NUEVAS:
            assert ats_registry.dominio_de(p) not in ats_registry.dominios()


# ─────────────────────────────────────────────────────────────────────────────
# Recruitee
# ─────────────────────────────────────────────────────────────────────────────

class TestRecruitee:
    def _traer(self, servir):
        pedidos = servir(_cuerpo("recruitee_channable.json"))
        r = ats_scraper.traer_fuente(FuenteATS("recruitee", "channable", "Channable"))
        return r, pedidos

    def test_trae_los_nueve_avisos_con_empresa_y_url_publica(self, servir):
        r, pedidos = self._traer(servir)
        assert pedidos[0][0] == "https://channable.recruitee.com/api/offers/"
        assert pedidos[0][1].get("seguir_redirecciones") is False
        assert (r.items_crudos, len(r.jobs), r.error) == (9, 9, "")
        j = r.jobs[0]
        assert j.title == "Customer Success Manager Benelux - Dutch speaking"
        assert j.company == "Channable"
        assert j.url == ("https://jobs.channable.com/o/"
                         "customer-success-manager-benelux-dutch-speaking-3")
        assert j.location_raw == "Utrecht, Utrecht, Netherlands"
        assert j.source_domain == "recruitee.com"

    def test_compone_el_salario_con_moneda_y_periodo(self, servir):
        """5 de 9 avisos de Channable publican salario. El mensual va con su periodo
        (lo anualiza la Fase 6); el DKK con 0 y 0 no es un salario."""
        r, _ = self._traer(servir)
        por_id = {j.url.rsplit("/", 1)[-1]: j.salary_raw for j in r.jobs}
        assert por_id["customer-success-manager-benelux-dutch-speaking-3"] == \
            "€3,750 - €5,250 /month"
        assert por_id["account-executive-north-america-1"] == "USD 200,000 - USD 250,000"
        assert por_id["team-lead-platform-team"] == "EUR 77,000 - EUR 103,000"
        assert por_id["open-application-3"] is None          # DKK 0 - 0
        assert por_id["open-application"] is None            # todo nulo
        assert sum(1 for s in por_id.values() if s) == 5

    def test_el_salario_mensual_se_anualiza(self, servir):
        from models import salario_anual_usd
        r, _ = self._traer(servir)
        mensual = next(j for j in r.jobs if j.salary_raw == "€3,750 - €5,250 /month")
        assert salario_anual_usd(mensual.salary_raw) == 48600

    def test_la_descripcion_incluye_los_requisitos(self, servir):
        """Fase 7: el eje de requisitos lee la descripcion. En Recruitee los requisitos
        viven en `requirements`, separados de `description`."""
        r, _ = self._traer(servir)
        ae = next(j for j in r.jobs if j.url.endswith("account-executive-north-america-1"))
        assert "Who you are" in ae.descripcion
        assert "3+ years closing new business in B2B SaaS" in ae.descripcion
        assert "<" not in ae.descripcion
        assert all(j.descripcion for j in r.jobs)

    def test_close_at_nulo_no_inventa_deadline(self, servir):
        r, _ = self._traer(servir)
        assert all(j.deadline is None for j in r.jobs)

    def test_empresa_inexistente_es_404_y_motivo_inexistente(self, monkeypatch, emitidas):
        # Respuesta real del 2026-10-04 a gema-token-inexistente-zz9.recruitee.com.
        ses = SesionConRedirecciones(RespuestaFalsa(404, b'{"error":"Not Found"}'))
        real = es.fetch
        monkeypatch.setattr(es, "fetch", lambda url, **kw: real(url, sesion=ses, **{
            k: v for k, v in kw.items() if k != "sesion"}))
        r = ats_scraper.traer_fuente(FuenteATS("recruitee", "zz9", "ZZ9", False, "pendiente"))
        assert r.jobs == [] and "HTTP 404" in r.error
        assert ses.pedidos[0][1] is False                  # allow_redirects=False
        assert ("ats_fuente_error_total", 1,
                {"plataforma": "recruitee", "token": "zz9", "motivo": "inexistente"}) \
            in emitidas


# ─────────────────────────────────────────────────────────────────────────────
# Workable
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkable:
    def _traer(self, servir):
        pedidos = servir(_cuerpo("workable_azumo.json"))
        r = ats_scraper.traer_fuente(FuenteATS("workable", "azumo", "Azumo"))
        return r, pedidos

    def test_pide_con_details_para_traer_la_descripcion(self, servir):
        """Sin `?details=true` no hay `description` (5.683 contra 51.644 bytes en
        huggingface). Es el mismo pedido, no uno por aviso."""
        _, pedidos = self._traer(servir)
        assert len(pedidos) == 1
        assert pedidos[0][0] == \
            "https://apply.workable.com/api/v1/widget/accounts/azumo?details=true"

    def test_un_aviso_repetido_por_pais_es_un_aviso_con_todos_sus_paises(self, servir):
        """El widget repite el aviso una vez por ubicacion: 13 filas, 3 avisos. Antes de
        fusionar, un aviso abierto en varios paises quedaba solo como "Argentina"."""
        r, _ = self._traer(servir)
        assert (r.items_crudos, len(r.jobs)) == (13, 3)
        por_codigo = {j.url.rsplit("/", 1)[-1]: j for j in r.jobs}
        assert por_codigo["95E09FC57F"].location_raw == \
            "Argentina; Brazil; Colombia; Mexico; Chile; Peru"
        assert por_codigo["37A90941C3"].location_raw == \
            "Argentina; Dominican Republic; Mexico; Chile; Colombia; Brazil"
        assert por_codigo["DD003E6EE0"].location_raw == "Peru"

    def test_la_empresa_va_por_defaults_porque_el_aviso_no_la_trae(self, servir):
        r, _ = self._traer(servir)
        assert {j.company for j in r.jobs} == {"Azumo"}

    def test_la_descripcion_viene_limpia(self, servir):
        r, _ = self._traer(servir)
        assert all(j.descripcion and "<" not in j.descripcion for j in r.jobs)
        assert r.jobs[0].descripcion.startswith("Azumo builds and operates production AI")

    def test_ciudad_y_pais_se_unen(self):
        fm = ats_registry.PLATAFORMAS["workable"].field_map
        fila = {"title": "Engineer", "url": "https://apply.workable.com/j/F4C096B22E",
                "city": "Paris", "state": "Île-de-France", "country": "France"}
        assert es.a_job_result(fila, fm, "apply.workable.com").location_raw == \
            "Paris, France"

    def test_cuenta_vieja_200_sin_avisos_no_es_error(self, servir):
        # Respuesta entera de apply.workable.com/api/v1/widget/accounts/hotjar, 2026-10-04.
        servir(b'{"name":"Hotjar","description":null,"jobs":[]}')
        r = ats_scraper.traer_fuente(FuenteATS("workable", "hotjar", "Hotjar", False, "pendiente"))
        assert (r.items_crudos, r.jobs, r.error) == (0, [], "")


# ─────────────────────────────────────────────────────────────────────────────
# BambooHR
# ─────────────────────────────────────────────────────────────────────────────

class TestBambooHR:
    def _traer(self, servir):
        servir(_cuerpo("bamboohr_gorillalogic.json"))
        return ats_scraper.traer_fuente(FuenteATS("bamboohr", "gorillalogic", "Gorilla Logic"))

    def test_compone_la_url_desde_el_id(self, servir):
        """Verificada el 2026-10-04: /careers/34 devuelve 200 con og:url igual a esta."""
        r = self._traer(servir)
        assert (r.items_crudos, len(r.jobs), r.error) == (20, 20, "")
        assert r.jobs[0].url == "https://gorillalogic.bamboohr.com/careers/34"
        assert len({j.url for j in r.jobs}) == 20

    def test_la_empresa_va_por_defaults(self, servir):
        r = self._traer(servir)
        assert {j.company for j in r.jobs} == {"Gorilla Logic"}

    def test_el_titulo_conserva_la_ubicacion_que_el_campo_no_trae(self, servir):
        r = self._traer(servir)
        j = r.jobs[0]
        assert j.title == ("Java/Vue Software Engineer - AA, Remote: Colombia - Costa Rica, "
                           "Full Time")
        assert j.location_raw is None                    # atsLocation todo nulo

    def test_la_ubicacion_estructurada_se_usa_cuando_viene(self, servir):
        r = self._traer(servir)
        por_id = {j.url.rsplit("/", 1)[-1]: j for j in r.jobs}
        assert por_id["166"].location_raw == "San Jose, Costa Rica"
        assert por_id["187"].location_raw == "United States"

    def test_sin_descripcion_en_el_listado(self, servir):
        r = self._traer(servir)
        assert all(j.descripcion is None for j in r.jobs)

    def test_cuenta_vieja_200_sin_avisos_no_es_error(self, servir):
        # Respuesta entera de zapier.bamboohr.com/careers/list, 2026-10-04.
        servir(b'{"meta":{"totalCount":0},"result":[]}')
        r = ats_scraper.traer_fuente(FuenteATS("bamboohr", "zapier", "Zapier", False, "pendiente"))
        assert (r.items_crudos, r.jobs, r.error) == (0, [], "")

    def test_empresa_inexistente_es_302_con_destino_en_el_error(self, emitidas):
        """Medido: 302 a https://www.bamboohr.com. Siguiendola, el error era un parseo
        de JSON sobre la home comercial; sin seguirla, dice lo que paso."""
        ses = SesionConRedirecciones(
            RespuestaFalsa(302, b"", {"Location": "https://www.bamboohr.com"}))
        r = es.scrape_endpoint(
            "bamboohr.com",
            endpoint_template="https://zz9.bamboohr.com/careers/list",
            items_path="result", field_map={"title": "jobOpeningName"},
            seguir_redirecciones=False, sesion=ses)
        assert ses.pedidos == [("https://zz9.bamboohr.com/careers/list", False)]
        assert "HTTP 302 -> https://www.bamboohr.com" in r.error
        assert ats_scraper.motivo_de_error(r.error) == "inexistente"

    def test_un_id_que_no_es_un_segmento_inofensivo_descarta_el_aviso(self):
        plantilla = "https://gorillalogic.bamboohr.com/careers/{id}"
        fm = ats_registry.PLATAFORMAS["bamboohr"].field_map
        for malo in ("../admin", "34?x=1", "34/../35", "", None, "a b"):
            fila = {"id": malo, "jobOpeningName": "Engineer"}
            assert es.a_job_result(fila, fm, "bamboohr.com",
                                   url_aviso_template=plantilla) is None, malo
        bueno = es.a_job_result({"id": 34, "jobOpeningName": "Engineer"}, fm,
                                "bamboohr.com", url_aviso_template=plantilla)
        assert bueno.url == "https://gorillalogic.bamboohr.com/careers/34"


# ─────────────────────────────────────────────────────────────────────────────
# Personio
# ─────────────────────────────────────────────────────────────────────────────

class TestPersonio:
    def _traer(self, servir):
        pedidos = servir(_cuerpo("personio_jtl.xml"))
        r = ats_scraper.traer_fuente(
            FuenteATS("personio", "jtl-software-gmbh", "JTL-Software"))
        return r, pedidos

    def test_lee_el_xml_y_compone_la_url(self, servir):
        """Verificada el 2026-10-04: /job/2666138 devuelve 200 con el titulo del aviso."""
        r, pedidos = self._traer(servir)
        assert pedidos[0][0] == "https://jtl-software-gmbh.jobs.personio.de/xml"
        assert (r.items_crudos, len(r.jobs), r.error) == (4, 4, "")
        j = r.jobs[0]
        assert j.title == "Account Executive (w/m/d) - Returnless"
        assert j.url == "https://jtl-software-gmbh.jobs.personio.de/job/2666138"
        assert j.location_raw == "Mobil"
        assert j.source_domain == "jobs.personio.de"

    def test_respeta_los_5_segundos(self, servir):
        _, pedidos = self._traer(servir)
        assert pedidos[0][1]["crawl_delay_s"] >= 5.0

    def test_la_empresa_es_la_filial_que_publica(self, servir):
        r, _ = self._traer(servir)
        assert r.jobs[0].company == "Returnless / SendDirect B.V."
        assert r.jobs[1].company == "JTL-Software GmbH"

    def test_sin_subcompany_la_empresa_va_por_defaults(self, servir):
        r, _ = self._traer(servir)
        assert r.jobs[3].company == "JTL-Software"

    def test_la_descripcion_lleva_cada_seccion_con_su_titulo(self, servir):
        r, _ = self._traer(servir)
        lineas = r.jobs[0].descripcion.split("\n")
        assert lineas[0] == "Deine Aufgabe"
        assert "Was Du dafür mitbringst" in lineas
        assert "Was wir Dir bieten" in lineas
        assert "<" not in r.jobs[0].descripcion

    def test_sin_secciones_la_descripcion_es_none(self, servir):
        """`<jobDescriptions></jobDescriptions>`: 16 de 30 avisos de JTL el 2026-10-04."""
        r, _ = self._traer(servir)
        assert r.jobs[1].title == "Engineering Manager (f/m/d)"
        assert r.jobs[1].descripcion is None

    def test_empresa_inexistente_es_307(self):
        ses = SesionConRedirecciones(
            RespuestaFalsa(307, b"", {"Location": "https://personio.com"}))
        r = es.scrape_endpoint(
            "jobs.personio.de", endpoint_template="https://zz9.jobs.personio.de/xml",
            response_format="xml", xml_item="position", field_map={"title": "name"},
            seguir_redirecciones=False, sesion=ses)
        assert "HTTP 307 -> https://personio.com" in r.error
        assert ats_scraper.motivo_de_error(r.error) == "inexistente"


# ─────────────────────────────────────────────────────────────────────────────
# Piezas compartidas
# ─────────────────────────────────────────────────────────────────────────────

class TestXml:
    def test_una_sola_seccion_tambien_es_lista(self):
        x = (b"<r><position><id>1</id><name>Dev</name><jobDescriptions><jobDescription>"
             b"<name>Aufgabe</name><value><![CDATA[<p>Python</p>]]></value>"
             b"</jobDescription></jobDescriptions></position></r>")
        filas = es.items_de_xml(x, "position")
        assert filas == [{"id": "1", "name": "Dev", "jobDescriptions": [
            {"name": "Aufgabe", "value": "<p>Python</p>"}]}]

    def test_xml_roto_queda_como_error_de_parseo(self, monkeypatch):
        monkeypatch.setattr(es, "fetch", lambda url, **kw: b"<html><body>no")
        r = es.scrape_endpoint("jobs.personio.de", endpoint_template="https://x/xml",
                               response_format="xml", xml_item="position",
                               field_map={"title": "name"})
        assert r.error.startswith("parseo xml")
        assert ats_scraper.motivo_de_error(r.error) == "parseo"


class TestFusionDeUbicaciones:
    def _job(self, loc):
        from models import JobResult
        return JobResult(title="t", company="c", url="https://x.com/1",
                         source_domain="x", location_raw=loc)

    def test_no_repite(self):
        j = self._job("Peru")
        es._fusionar_ubicacion(j, "Peru")
        assert j.location_raw == "Peru"

    def test_suma_con_punto_y_coma(self):
        j = self._job("Argentina")
        es._fusionar_ubicacion(j, "Peru")
        assert j.location_raw == "Argentina; Peru"

    def test_sin_ubicacion_previa_toma_la_nueva(self):
        j = self._job(None)
        es._fusionar_ubicacion(j, "Peru")
        assert j.location_raw == "Peru"

    def test_tiene_tope(self):
        j = self._job("A")
        for i in range(200):
            es._fusionar_ubicacion(j, f"Ciudad numero {i}")
        assert len(j.location_raw) <= es._UBICACION_FUSIONADA_MAX


class TestMotivoDeError:
    @pytest.mark.parametrize("error, motivo", [
        ("EndpointAgotado: HTTP 404 en https://x", "inexistente"),
        ("EndpointAgotado: HTTP 302 -> https://www.bamboohr.com en https://x", "inexistente"),
        ("EndpointAgotado: HTTP 307 -> https://personio.com en https://x", "inexistente"),
        ("EndpointNoPublico: HTTP 403 en https://x", "no_publico"),
        ("EndpointAgotado: HTTP 429 tras 3 intentos en https://x", "limitado_o_caido"),
        ("EndpointAgotado: HTTP 503 tras 3 intentos en https://x", "limitado_o_caido"),
        ("parseo json: JSONDecodeError: Expecting value", "parseo"),
        ("esquema_cambiado: 5 de 5 items sin título", "esquema_cambiado"),
        ("ConnectionError: timeout", "otro"),
    ])
    def test_categoria(self, error, motivo):
        assert ats_scraper.motivo_de_error(error) == motivo


class TestMetricas:
    def test_items_invalidos_emiten_contador(self, servir, emitidas):
        """Un aviso sin id en BambooHR no tiene URL: se descarta, y se cuenta."""
        servir(json.dumps({"result": [
            {"id": "34", "jobOpeningName": "Engineer"},
            {"id": None, "jobOpeningName": "Engineer 2"}]}).encode())
        r = ats_scraper.traer_fuente(FuenteATS("bamboohr", "gorillalogic", "Gorilla Logic"))
        assert len(r.jobs) == 1
        assert ("ats_items_invalidos_total", 1,
                {"plataforma": "bamboohr", "token": "gorillalogic"}) in emitidas

    def test_una_fuente_sana_no_emite_errores(self, servir, emitidas):
        servir(_cuerpo("bamboohr_gorillalogic.json"))
        ats_scraper.traer_fuente(FuenteATS("bamboohr", "gorillalogic", "Gorilla Logic"))
        assert not [e for e in emitidas if e[0].startswith("ats_")]

    def test_la_excepcion_del_fanout_emite_contador(self, monkeypatch, emitidas):
        import asyncio

        def _explota(f, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(ats_scraper, "traer_fuente", _explota)
        asyncio.run(ats_scraper.traer_todo([FuenteATS("personio", "x", "X", False, "pendiente")]))
        assert ("ats_fuente_error_total", 1,
                {"plataforma": "personio", "token": "x", "motivo": "excepcion"}) in emitidas
