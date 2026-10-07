"""
tests/test_descubrir_ats.py — el descubridor de tokens ATS (Fase 8 del ).

Cero red: `Red` recibe un `get_crudo` que sirve un diccionario. Las paginas, robots.txt y
respuestas de API de `tests/fixtures/descubridor/` son recortes de la corrida en vivo del
2026-10-04 (42 pedidos sobre 7 dominios). Los cuerpos de una linea incrustados aca son la
respuesta entera de esa corrida o de la verificacion de plataformas del mismo dia. Las
paginas de una sola etiqueta `<a>` que arman los casos borde estan marcadas como tales.
"""

import json
from pathlib import Path

import pytest

import metrics
from tools import descubrir_ats as d

FIX = Path(__file__).parent / "fixtures" / "descubridor"
ATS = Path(__file__).parent / "fixtures" / "ats"


def _fix(nombre: str) -> bytes:
    return (FIX / nombre).read_bytes()


class Web:
    """Un `get_crudo` sobre un diccionario. URL desconocida = host que no responde."""

    def __init__(self, paginas: dict):
        self.paginas = paginas
        self.pedidos: list[tuple[str, bool]] = []

    def __call__(self, url: str, seguir: bool) -> d.Respuesta:
        self.pedidos.append((url, seguir))
        if url not in self.paginas:
            raise ConnectionError(f"sin red para {url}")
        v = self.paginas[url]
        if isinstance(v, d.Respuesta):
            return v
        status, cuerpo, *resto = v
        final = resto[0] if resto else url
        headers = resto[1] if len(resto) > 1 else {}
        return d.Respuesta(status, final, cuerpo, headers)


class Reloj:
    """Reloj falso: `dormir` avanza el tiempo y registra cuanto se pidio esperar."""

    def __init__(self):
        self.t = 1000.0
        self.dormidas: list[float] = []

    def ahora(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.dormidas.append(s)
        self.t += s


def _red(paginas: dict) -> tuple[d.Red, Web, Reloj]:
    web, reloj = Web(paginas), Reloj()
    return d.Red(get_crudo=web, dormir=reloj.dormir, reloj=reloj.ahora), web, reloj


@pytest.fixture
def emitidas(monkeypatch):
    lista = []
    monkeypatch.setattr(metrics, "emit",
                        lambda nombre, valor=1, **et: lista.append((nombre, valor, et)))
    return lista


# Lo que devolvio la corrida del 2026-10-04 para workmotion.com, recortado.
WORKMOTION = {
    "https://workmotion.com/robots.txt": (200, _fix("workmotion_robots.txt")),
    "https://workmotion.com/careers": (200, _fix("workmotion_careers.html"),
                                       "https://workmotion.com/careers/"),
    "https://apply.workable.com/robots.txt": (200, _fix("apply_workable_robots.txt")),
    "https://apply.workable.com/api/v1/widget/accounts/workmotion?details=true":
        (200, _fix("workable_workmotion.json")),
}


# ─────────────────────────────────────────────────────────────────────────────
# Deteccion
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectar:
    def test_workable_desde_el_boton_de_la_pagina(self):
        assert d.detectar_tokens(_fix("workmotion_careers.html").decode()) == \
            [("workable", "workmotion")]

    def test_bamboohr_desde_la_lista_de_puestos(self):
        assert d.detectar_tokens(_fix("gorillalogic_open_roles.html").decode()) == \
            [("bamboohr", "gorillalogic")]

    def test_recruitee_en_dominio_propio_por_su_subdomain(self):
        """careers.hostaway.com no nombra hostaway.recruitee.com: el token esta en la
        configuracion de la app, escapado como &quot;."""
        assert d.detectar_tokens(_fix("hostaway_careers.html").decode()) == \
            [("recruitee", "hostaway")]

    def test_careers_analytics_no_es_una_empresa(self):
        """Corrida del 04-10: sin la exclusion, el descubridor propuso
        `recruitee:careers-analytics` para hostaway y channable (403 en su API)."""
        tokens = d.detectar_tokens(_fix("hostaway_careers.html").decode())
        assert ("recruitee", "careers-analytics") not in tokens

    def test_subdomain_sin_el_cdn_de_recruitee_no_cuenta(self):
        # Caso borde armado: la misma clave fuera de una pagina de Recruitee.
        assert d.detectar_tokens('{"subdomain":"api","x":1}') == []

    def test_la_pagina_de_careers_sin_tablero_no_tiene_token(self):
        assert d.detectar_tokens(_fix("gorillalogic_careers.html").decode()) == []

    @pytest.mark.parametrize("texto, esperado", [
        ("https://boards.greenhouse.io/embed/job_board?for=gitlab&amp;b=x",
         [("greenhouse", "gitlab")]),
        ("https://job-boards.greenhouse.io/gitlab/jobs/8556658002", [("greenhouse", "gitlab")]),
        ("https://jobs.lever.co/spotify/2193db3f", [("lever", "spotify")]),
        ("https://jobs.ashbyhq.com/linear/d3bc1ced", [("ashby", "linear")]),
        ("https://apply.workable.com/j/F4C096B22E", []),
        ("https://jtl-software-gmbh.jobs.personio.de/job/2666138",
         [("personio", "jtl-software-gmbh")]),
        ("https://www.bamboohr.com", []),
    ])
    def test_urls_reales_de_cada_plataforma(self, texto, esperado):
        assert d.detectar_tokens(texto) == esperado


class TestEnlaces:
    def test_segundo_salto_a_open_roles(self):
        enlaces = d.enlaces_de_careers(_fix("gorillalogic_careers.html").decode(),
                                       "https://gorillalogic.com/careers/", "gorillalogic.com")
        assert enlaces == ["https://gorillalogic.com/careers/open-roles/"]

    def test_no_sale_del_sitio(self):
        texto = '<a href="https://otro.com/careers/">x</a><a href="/jobs/all-jobs/">y</a>'
        assert d.enlaces_de_careers(texto, "https://about.gitlab.com/jobs/", "gitlab.com") == \
            ["https://about.gitlab.com/jobs/all-jobs/"]


class TestNombre:
    @pytest.mark.parametrize("nombre, dominio", [
        ("WorkMotion", "workmotion.com"),
        ("Hugging Face", "huggingface.co"),
        ("JTL-Software GmbH", "jtl-software.com"),
        ("Gorilla Logic", "www.gorillalogic.com"),
        ("gorillalogic", "gorillalogic.com"),
        ("Azumo", "azumo.com"),
        ("Empresa SAS", "empresa.com.co"),
    ])
    def test_coinciden(self, nombre, dominio):
        assert d.coincide(nombre, dominio)

    @pytest.mark.parametrize("nombre, dominio", [
        ("Returnless / SendDirect B.V.", "jtl-software.com"),
        ("WorkMotion", "deel.com"),
        ("AI", "ai-labs.io"),
        ("", "workmotion.com"),
    ])
    def test_no_coinciden(self, nombre, dominio):
        assert not d.coincide(nombre, dominio)


# ─────────────────────────────────────────────────────────────────────────────
# De punta a punta
# ─────────────────────────────────────────────────────────────────────────────

class TestDescubrir:
    def test_propone_workmotion_apagada_y_con_d1(self, emitidas):
        red, web, _ = _red(WORKMOTION)
        [h] = d.descubrir_dominio(red, "workmotion.com", hoy="2026-10-04")
        assert (h.estado, h.plataforma, h.token, h.avisos) == ("propuesta", "workable",
                                                                "workmotion", 3)
        assert (h.nombre_board, h.nombre_desde) == ("WorkMotion", "board")
        assert h.linea_registro == (
            'FuenteATS("workable", "workmotion", "WorkMotion", False, "D1 pendiente; '
            "propuesto por descubrir_ats 2026-10-04: 3 avisos, nombre 'WorkMotion' "
            '(board)"),')
        # La API se pide sin seguir redirecciones y con el endpoint del registro.
        assert ("https://apply.workable.com/api/v1/widget/accounts/workmotion?details=true",
                False) in web.pedidos
        assert ("descubridor_propuesta_total", 1,
                {"plataforma": "workable", "dominio": "workmotion.com"}) in emitidas

    def test_la_linea_propuesta_es_python_valido_y_pasa_el_registro(self):
        """La linea se pega en ats_registry.FUENTES: tiene que evaluar a una FuenteATS
        apagada, con nota de D1 si la plataforma esta pendiente."""
        from ats_registry import FuenteATS
        red, _, _ = _red(WORKMOTION)
        [h] = d.descubrir_dominio(red, "workmotion.com", hoy="2026-10-04")
        f = eval(h.linea_registro.rstrip(","), {"FuenteATS": FuenteATS})
        assert f.activa is False and "D1" in f.nota
        assert d.FORMATO_TOKEN.fullmatch(f.token)

    def test_exige_avisos_no_un_200(self, emitidas):
        """Respuesta entera de Workable para hotjar el 2026-10-04: existe y no publica.
        La pagina es un caso borde armado: una sola etiqueta <a>."""
        red, _, _ = _red({
            "https://hotjar.com/robots.txt": (404, b""),
            "https://hotjar.com/careers": (200, b'<a href="https://apply.workable.com/hotjar/">'),
            "https://apply.workable.com/robots.txt": (200, _fix("apply_workable_robots.txt")),
            "https://apply.workable.com/api/v1/widget/accounts/hotjar?details=true":
                (200, b'{"name":"Hotjar","description":null,"jobs":[]}'),
        })
        [h] = d.descubrir_dominio(red, "hotjar.com")
        assert h.estado == "rechazada" and h.motivo.startswith("sin_avisos")
        assert h.linea_registro == ""
        assert any(e[0] == "descubridor_rechazo_total" and e[2]["motivo"] == "sin_avisos"
                   for e in emitidas)

    def test_nombre_del_board_distinto_del_dominio_se_rechaza(self):
        """El riesgo que esta validacion cubre: una pagina que embebe el tablero de OTRA
        empresa. Mismo recorte real de workmotion.com/careers, servido bajo deel.com."""
        paginas = {k.replace("workmotion.com/", "deel.com/"): v for k, v in WORKMOTION.items()}
        red, _, _ = _red(paginas)
        [h] = d.descubrir_dominio(red, "deel.com")
        assert h.estado == "rechazada"
        assert h.motivo == "nombre_no_coincide: el board dice 'WorkMotion' (board)"

    def test_segundo_salto_y_ya_registrado(self):
        """gorillalogic.com/careers/ no nombra a BambooHR; /careers/open-roles/ si. El
        token ya esta en el registro: no se valida ni se propone de nuevo."""
        red, web, _ = _red({
            "https://gorillalogic.com/robots.txt": (200, _fix("gorillalogic_robots.txt")),
            "https://gorillalogic.com/careers": (200, _fix("gorillalogic_careers.html"),
                                                 "https://gorillalogic.com/careers/"),
            "https://gorillalogic.com/jobs": (404, b"Not Found"),
            "https://gorillalogic.com/": (200, b"<html></html>"),
            "https://gorillalogic.com/careers/open-roles/":
                (200, _fix("gorillalogic_open_roles.html")),
        })
        [h] = d.descubrir_dominio(red, "gorillalogic.com")
        assert (h.plataforma, h.token, h.estado, h.motivo) == \
            ("bamboohr", "gorillalogic", "rechazada", "ya_registrado")
        assert h.pagina == "https://gorillalogic.com/careers/open-roles/"
        assert not [u for u, _ in web.pedidos if "bamboohr.com" in u]

    def test_respeta_el_crawl_delay_del_sitio(self):
        """gorillalogic.com declara `Crawl-delay: 10`. Entre dos pedidos a ese host
        pasan al menos 10 s; entre dos pedidos cualesquiera, al menos 1."""
        red, web, reloj = _red({
            "https://gorillalogic.com/robots.txt": (200, _fix("gorillalogic_robots.txt")),
            "https://gorillalogic.com/careers": (200, _fix("gorillalogic_careers.html"),
                                                 "https://gorillalogic.com/careers/"),
            "https://gorillalogic.com/jobs": (404, b"Not Found"),
            "https://gorillalogic.com/": (200, b"<html></html>"),
            "https://gorillalogic.com/careers/open-roles/":
                (200, _fix("gorillalogic_open_roles.html")),
        })
        d.descubrir_dominio(red, "gorillalogic.com")
        assert reloj.dormidas.count(10.0) >= 3        # /jobs, /, /open-roles

    def test_robots_que_prohibe_no_se_pide(self):
        red, web, _ = _red({
            "https://cerrada.com/robots.txt": (200, b"User-agent: *\nDisallow: /\n"),
        })
        [h] = d.descubrir_dominio(red, "cerrada.com")
        assert h.motivo == "robots_bloquea"
        assert [u for u, _ in web.pedidos if not u.endswith("/robots.txt")] == []

    def test_robots_con_429_corta_el_host(self, emitidas):
        """channable.com contesto 429 al robots.txt el 2026-10-04. Ningun pedido mas a
        ese host; jobs.channable.com, que es otro host, si se lee."""
        hostaway_como_channable = _fix("hostaway_careers.html").replace(
            b"hostaway&quot;,&quot;textingSettings", b"channable&quot;,&quot;textingSettings")
        red, web, _ = _red({
            "https://channable.com/robots.txt": (429, b"Too Many Requests"),
            "https://jobs.channable.com/robots.txt": (200, b"User-agent: *\nAllow: /\n"),
            "https://jobs.channable.com/": (200, hostaway_como_channable),
        })
        [h] = d.descubrir_dominio(red, "channable.com")
        assert (h.plataforma, h.token, h.motivo) == ("recruitee", "channable", "ya_registrado")
        pedidos_a_channable = [u for u, _ in web.pedidos if "//channable.com" in u]
        assert pedidos_a_channable == ["https://channable.com/robots.txt"]
        assert ("descubridor_robots_inaccesible_total", 1, {"host": "channable.com"}) \
            in emitidas

    def test_error_de_la_api_se_reporta_con_su_codigo(self):
        """Respuesta real a un token inexistente en BambooHR: 302 a www.bamboohr.com.
        Pagina armada: una etiqueta <a>."""
        red, _, _ = _red({
            "https://nueva.com/robots.txt": (404, b""),
            "https://nueva.com/careers": (200, b'<a href="https://nueva-zz9.bamboohr.com/careers">'),
            "https://nueva-zz9.bamboohr.com/robots.txt": (200, b"User-agent: *\n"),
            "https://nueva-zz9.bamboohr.com/careers/list":
                (302, b"", "https://nueva-zz9.bamboohr.com/careers/list",
                 {"Location": "https://www.bamboohr.com"}),
        })
        [h] = d.descubrir_dominio(red, "nueva.com")
        assert h.motivo == "error_validacion: HTTP 302 -> https://www.bamboohr.com"

    def test_validacion_de_personio_respeta_sus_5_segundos(self, monkeypatch):
        """El JTL real (4 posiciones del recorte), con nombre del board por mayoria de
        `subcompany`. Pagina armada: una etiqueta <a>."""
        red, _, reloj = _red({
            "https://jtl-software.com/robots.txt": (404, b""),
            "https://jtl-software.com/careers":
                (200, b'<a href="https://jtl-software-gmbh.jobs.personio.de/">'),
            "https://jtl-software-gmbh.jobs.personio.de/robots.txt": (200, b"User-agent: *\nAllow: /\n"),
            "https://jtl-software-gmbh.jobs.personio.de/xml":
                (200, (ATS / "personio_jtl.xml").read_bytes()),
        })
        import ats_registry
        # JTL ya esta registrada (apagada): se saca para ejercitar la validacion.
        monkeypatch.setattr(ats_registry, "FUENTES",
                            [f for f in ats_registry.FUENTES if f.plataforma != "personio"])
        [h] = d.descubrir_dominio(red, "jtl-software.com", hoy="2026-10-04")
        assert (h.estado, h.avisos, h.nombre_board) == ("propuesta", 4, "JTL-Software GmbH")
        assert 5.0 in reloj.dormidas

    def test_lever_sin_nombre_valida_por_token_y_lo_dice(self):
        """Lever no reporta nombre: se compara el token, y la propuesta lo declara.
        Cuerpo: un aviso real de spotify recortado (el de tests/test_ats.py)."""
        red, _, _ = _red({
            "https://acmecorp.com/robots.txt": (404, b""),
            "https://acmecorp.com/careers": (200, b'<a href="https://jobs.lever.co/acmecorp">'),
            "https://api.lever.co/robots.txt": (200, b"User-agent: *\nAllow: /\nCrawl-delay: 1\n"),
            "https://api.lever.co/v0/postings/acmecorp?mode=json": (200, json.dumps([
                {"text": "Android Engineer - Experience",
                 "hostedUrl": "https://jobs.lever.co/spotify/2193db3f",
                 "categories": {"location": "London"}}]).encode()),
        })
        [h] = d.descubrir_dominio(red, "acmecorp.com", hoy="2026-10-04")
        assert (h.estado, h.nombre_desde) == ("propuesta", "token")
        assert '"Acmecorp"' in h.linea_registro and "(token)" in h.linea_registro
        assert "D1" not in h.linea_registro          # lever no esta pendiente de D1

    def test_host_que_no_responde_no_es_bloqueo(self):
        red, _, _ = _red({})                         # ningun host responde
        [h] = d.descubrir_dominio(red, "inexistente-zz9.com")
        assert h.motivo == "sin_pagina"

    def test_dominio_invalido(self):
        red, web, _ = _red({})
        [h] = d.descubrir_dominio(red, "no es un dominio")
        assert h.motivo == "dominio_invalido" and web.pedidos == []

    def test_token_con_formato_fuera_del_registro_no_se_valida(self):
        red, web, _ = _red({
            "https://linear.app/robots.txt": (404, b""),
            "https://linear.app/careers": (200, b'<a href="https://jobs.ashbyhq.com/Linear.App">'),
        })
        [h] = d.descubrir_dominio(red, "linear.app")
        assert h.motivo.startswith("token_formato")
        assert not [u for u, _ in web.pedidos if "ashbyhq" in u]


class TestReintento:
    def test_429_reintenta_una_vez_con_retry_after(self, emitidas):
        red, web, reloj = _red({
            "https://x.com/robots.txt": (404, b""),
        })
        respuestas = [d.Respuesta(429, "https://x.com/careers", b"", {"Retry-After": "7"}),
                      d.Respuesta(200, "https://x.com/careers", b"ok")]

        def _get(url, seguir):
            web.pedidos.append((url, seguir))
            if url.endswith("robots.txt"):
                return d.Respuesta(404, url, b"")
            return respuestas.pop(0)
        red._get = _get
        r = red.get("https://x.com/careers")
        assert r.status == 200
        assert any(7.0 <= s <= 7.5 for s in reloj.dormidas)
        assert ("descubridor_http_limitado_total", 1, {"host": "x.com", "status": 429}) \
            in emitidas

    def test_retry_after_negativo_no_rompe_el_sleep(self, emitidas):
        """`time.sleep(-5)` lanza ValueError: un host que contesta `Retry-After: -5`
        cortaba el dominio entero. El piso es 0 (mas el jitter)."""
        red, web, reloj = _red({})
        respuestas = [d.Respuesta(503, "https://x.com/careers", b"", {"Retry-After": "-5"}),
                      d.Respuesta(200, "https://x.com/careers", b"ok")]
        red._get = lambda url, seguir: respuestas.pop(0)
        r = red._pedir("https://x.com/careers", True)
        assert r.status == 200
        assert all(s >= 0 for s in reloj.dormidas)


class TestCli:
    def test_nunca_escribe_el_registro(self, monkeypatch, capsys, tmp_path):
        """Propone por stdout. `ats_registry.py` no cambia aunque haya propuestas."""
        import ats_registry
        antes = Path(ats_registry.__file__).read_bytes()
        red = _red(WORKMOTION)[0]          # antes del setattr: `_red` instancia `d.Red`
        monkeypatch.setattr(d, "Red", lambda: red)
        assert d.main(["workmotion.com", "--json"]) == 0
        salida = json.loads(capsys.readouterr().out)
        assert salida[0]["estado"] == "propuesta"
        assert Path(ats_registry.__file__).read_bytes() == antes

    def test_archivo_con_comentarios(self, monkeypatch, capsys, tmp_path):
        lista = tmp_path / "dominios.txt"
        lista.write_text("# empresas LATAM\nworkmotion.com\n\n", encoding="utf-8")
        red = _red(WORKMOTION)[0]
        monkeypatch.setattr(d, "Red", lambda: red)
        assert d.main(["--archivo", str(lista)]) == 0
        out = capsys.readouterr().out
        assert "PROPUESTA" in out and "workable:workmotion" in out
        assert "FuenteATS(" in out
