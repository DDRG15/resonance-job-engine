"""
tests/test_gmail_integracion.py — Las alertas de Gmail entran
por el mismo embudo que cualquier fuente.

Lo que se fija acá es el CABLEADO, no el parser (ese vive en test_gmail_alertas.py):

1. Con el flag apagado, `run()` no llama a Gmail. Es el estado por defecto y el que
   tiene que sobrevivir a cualquier refactor del run.
2. Con el flag prendido, los avisos llegan al registry con su `source_domain` real
   (linkedin.com / indeed.com), y cada proveedor deja traza `gmail:<proveedor>`.
3. Sin token, la corrida no revienta: un error, una vez, en el resumen.
4. Un aviso que llega en dos corridas entra una sola vez (URL canónica sin rastreo).
"""

import queue
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import gmail_alertas
from gmail_alertas import Mensaje
from models import SearchConfig
from scraper import GemaScraper


def _scraper(db, run_id, titulos=("python",)):
    webhook = MagicMock()
    webhook.flush_summary = AsyncMock()      # `run()` lo espera al cerrar
    return GemaScraper(SearchConfig(target_titles=list(titulos)), db, queue.Queue(),
                       webhook=webhook, run_id=run_id)


def _alerta_linkedin(*avisos: tuple[str, str, str, str]) -> str:
    """Título y empresa como filas HERMANAS de una tabla compartida: el caso en que la
    tarjeta «solo de este aviso» es la fila del título y el parser cae a `_segmento`."""
    filas = "".join(
        f'<tr><td><a href="https://www.linkedin.com/comm/jobs/view/{i}/?trackingId=t{n}">'
        f'{titulo}</a></td></tr><tr><td>{empresa} · {lugar}</td></tr>'
        for n, (i, titulo, empresa, lugar) in enumerate(avisos)
    )
    return f"<html><body><table>{filas}</table></body></html>"


class _LectorFalso:
    def __init__(self, mensajes):
        self._m = mensajes
        self.consultas = []

    def mensajes(self, consulta, maximo):
        self.consultas.append(consulta)
        return list(self._m)


@pytest.fixture
def lector(monkeypatch):
    """Reemplaza al lector real: ningún test de este archivo toca la red ni credentials/."""
    real = gmail_alertas.traer_alertas   # una vez: un segundo _poner no envuelve al primero

    def _poner(mensajes):
        falso = _LectorFalso(mensajes)
        monkeypatch.setattr(gmail_alertas, "traer_alertas",
                            lambda **k: real(lector=falso, **k))
        return falso
    return _poner


class TestCableado:

    async def test_los_avisos_entran_al_registry_con_su_dominio_real(self, tmp_db, lector):
        lector([Mensaje("m1", "LinkedIn <jobalerts-noreply@linkedin.com>", _alerta_linkedin(
            ("4011111111", "Senior Python Engineer", "Acme", "Bogota, Colombia (Remote)"),
            ("4022222222", "Chef de cocina", "Restaurante", "Bogota, Colombia"),
        ))])
        s = _scraper(tmp_db, "run-g1")
        await s._scrape_gmail_alertas()

        assert [j.url for j in s._all_jobs] == ["https://www.linkedin.com/jobs/view/4011111111"]
        job = s._all_jobs[0]
        assert (job.source_domain, job.company, job.location_raw) == \
               ("linkedin.com", "Acme", "Bogota, Colombia (Remote)")
        assert tmp_db.is_seen(job.url, 0)[0]

    async def test_cada_proveedor_deja_su_traza(self, tmp_db, lector):
        lector([Mensaje("m1", "jobalerts-noreply@linkedin.com", _alerta_linkedin(
            ("4011111111", "Senior Python Engineer", "Acme", "Remote"),
            ("4022222222", "Chef de cocina", "Restaurante", "Bogota"),
        ))])
        await _scraper(tmp_db, "run-g2")._scrape_gmail_alertas()

        filas = {f["domain"]: f for f in tmp_db.get_node_traces("run-g2")
                 if f["node"] == "fuente_endpoint"}
        assert set(filas) == {"gmail:linkedin", "gmail:indeed"}
        # items = lo que traía la alerta; aceptados = lo que pasó el filtro de título
        assert (filas["gmail:linkedin"]["items"], filas["gmail:linkedin"]["aceptados"]) == (2, 1)
        assert filas["gmail:indeed"]["items"] == 0

    @pytest.mark.parametrize("subdirectorio", ["", "ruta_extensa_" * 8])
    async def test_sin_token_la_corrida_sigue_con_un_solo_error(
            self, tmp_db, monkeypatch, tmp_path, subdirectorio):
        monkeypatch.setattr(config, "GMAIL_TOKEN_PATH",
                            str(tmp_path / subdirectorio / "no_existe.json"))
        s = _scraper(tmp_db, "run-g3")
        await s._scrape_gmail_alertas()

        assert s._all_jobs == []
        errores = [e for e in s.summary.errors if e.startswith("gmail:")]
        assert len(errores) == 1 and "python tools/autorizar_gmail.py" in errores[0]
        trazas = [f for f in tmp_db.get_node_traces("run-g3") if f["node"] == "fuente_endpoint"]
        assert {f["status"] for f in trazas} == {"error"}

    async def test_el_mismo_aviso_en_dos_corridas_entra_una_vez(self, tmp_db, lector):
        alerta = _alerta_linkedin(("4011111111", "Python Developer", "Acme", "Remote"))
        lector([Mensaje("m1", "jobalerts-noreply@linkedin.com", alerta)])
        primera = _scraper(tmp_db, "run-g4a")
        await primera._scrape_gmail_alertas()
        # La segunda alerta trae el MISMO aviso con otro trackingId.
        lector([Mensaje("m2", "jobalerts-noreply@linkedin.com",
                        alerta.replace("trackingId=t0", "trackingId=otro"))])
        segunda = _scraper(tmp_db, "run-g4b")
        await segunda._scrape_gmail_alertas()

        assert len(primera._all_jobs) == 1 and segunda._all_jobs == []
        assert segunda.summary.skipped_seen == 1


class TestFlag:

    async def _correr_run(self, tmp_db, monkeypatch):
        """`run()` con todo lo que toca la red reemplazado; devuelve si llamó a Gmail."""
        llamado = []

        async def _gmail(self):
            llamado.append(True)

        async def _nada(self, *a, **k):
            return None
        monkeypatch.setattr(GemaScraper, "_scrape_gmail_alertas", _gmail)
        monkeypatch.setattr(GemaScraper, "_scrape_endpoint_boards", _nada)
        monkeypatch.setattr(GemaScraper, "_scrape_ats_sources", _nada)
        monkeypatch.setattr(GemaScraper, "_ats_dominios_pedidos",
                            staticmethod(lambda d: list(d)))
        s = _scraper(tmp_db, "run-flag")
        s.config.target_domains = ["greenhouse.io"]   # se aparta como ATS: sin navegador
        await s.run()
        return bool(llamado)

    async def test_apagado_por_defecto_no_llama_a_gmail(self, tmp_db, monkeypatch):
        monkeypatch.setattr(config, "GMAIL_ALERTAS_HABILITADO", False)
        assert await self._correr_run(tmp_db, monkeypatch) is False

    async def test_prendido_llama_a_gmail(self, tmp_db, monkeypatch):
        monkeypatch.setattr(config, "GMAIL_ALERTAS_HABILITADO", True)
        assert await self._correr_run(tmp_db, monkeypatch) is True


# =============================================================================
# Regresiones de la revisión: cada hallazgo, su test
# =============================================================================

class TestRevision0710:

    _ROTO = ('<a href="http://[unsub">baja</a>'
             '<a href="https://www.linkedin.com/jobs/view/1234567890">Backend Engineer</a>')

    def test_href_malformado_no_levanta_y_el_aviso_se_lee(self):
        # HIGH: `urlsplit("http://[unsub")` levanta ValueError y tumbaba run() entero.
        avisos = gmail_alertas.parsear_alerta("linkedin", self._ROTO)
        assert [a.titulo for a in avisos] == ["Backend Engineer"]

    def test_una_alerta_que_revienta_no_se_lleva_a_las_otras(self, monkeypatch):
        real = gmail_alertas.parsear_alerta

        def _parser(proveedor, html):
            if "REVIENTA" in html:
                raise RecursionError("anidado de mas")
            return real(proveedor, html)
        monkeypatch.setattr(gmail_alertas, "parsear_alerta", _parser)
        buena = _alerta_linkedin(("4011111111", "Python Developer", "Acme", "Remote"))
        res = gmail_alertas.traer_alertas(lector=_LectorFalso([
            Mensaje("m1", "jobalerts-noreply@linkedin.com", "<p>REVIENTA</p>"),
            Mensaje("m2", "jobalerts-noreply@linkedin.com", buena),
        ]))
        li = next(r for r in res if r.proveedor == "linkedin")
        assert len(li.jobs) == 1 and li.descartados_invalidos == 1 and li.error == ""

    async def test_si_traer_alertas_levanta_la_corrida_sigue(self, tmp_db, monkeypatch):
        def _explota(**k):
            raise RuntimeError("boom")
        monkeypatch.setattr(gmail_alertas, "traer_alertas", _explota)
        s = _scraper(tmp_db, "run-rev")
        await s._scrape_gmail_alertas()          # no levanta
        assert any(e.startswith("gmail: RuntimeError") for e in s.summary.errors)

    @pytest.mark.parametrize("empresa", ["Applicant Tracking Co", "Postulantes SAS"])
    def test_empresa_con_la_palabra_applicant_no_es_ruido(self, empresa):
        # MEDIUM: la regex suelta se comía la empresa y la ubicación subía a empresa.
        html = (f'<table><tr><td><a href="https://www.linkedin.com/jobs/view/4099999999">'
                f'Staff Engineer</a></td></tr><tr><td><div>{empresa}</div><div>Bogota</div>'
                f'</td></tr></table>')
        a = gmail_alertas.parsear_alerta("linkedin", html)[0]
        assert (a.empresa, a.ubicacion) == (empresa, "Bogota")

    @pytest.mark.parametrize("linea", ["34 applicants", "Over 200 applicants",
                                       "12 solicitudes", "Más de 100 postulantes"])
    def test_el_conteo_de_postulantes_sigue_siendo_ruido(self, linea):
        assert gmail_alertas._es_ruido(linea)

    def test_cuerpo_grande_por_attachment_id(self):
        # LOW: un text/html con attachmentId (cuerpo grande) se leía como alerta vacía.
        import base64
        data = base64.urlsafe_b64encode("<p>hola ñ</p>".encode()).decode().rstrip("=")
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"attachmentId": "abc", "size": 90000}}]}
        pedidos = []
        assert gmail_alertas._html_de_payload(
            payload, lambda aid: pedidos.append(aid) or data) == "<p>hola ñ</p>"
        assert pedidos == ["abc"]
        assert gmail_alertas._html_de_payload(payload) == ""      # sin lector de adjuntos

    def test_truncado_por_el_tope_se_avisa(self):
        # LOW: las alertas más viejas que el tope se perdían sin una línea de log.
        lector = _LectorFalso([])
        lector.truncado = True
        lineas = []
        gmail_alertas.traer_alertas(lector=lector, log=lineas.append)
        assert any("tope" in ln for ln in lineas)

    def test_invalid_grant_dice_que_hacer(self, tmp_path, monkeypatch):
        # MEDIUM: con la app OAuth en Testing el refresh vence a los 7 días.
        pytest.importorskip("google.oauth2.credentials")
        from google.oauth2 import credentials as gcred

        class _Vencido:
            valid, expired, refresh_token = False, True, "r"

            def refresh(self, _req):
                raise RuntimeError("invalid_grant: Token has been expired or revoked.")
        monkeypatch.setattr(gcred.Credentials, "from_authorized_user_info",
                            classmethod(lambda cls, info, scopes=None: _Vencido()))
        token = tmp_path / "gmail_token.json"
        token.write_text('{"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}',
                         encoding="utf-8")
        with pytest.raises(gmail_alertas.GmailNoConfigurado, match="autorizar_gmail.*7 dias"):
            gmail_alertas.credenciales(str(token))
