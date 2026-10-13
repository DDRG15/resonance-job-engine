"""
tests/test_cv_ciclo_y_privacidad.py — puntos 11 y 12 de la auditoria de revision externa (2026-09-30).

Punto 11: el ciclo de vida del perfil del CV contradice los controles de la UI.
Punto 12: el CV crudo sale a terceros sin aviso previo y el upload no tiene limite.

Cada test nombra la falla que reproduce. Se escribieron ANTES del fix y se corrieron
contra b871963: los marcados [ROJO] fallaron ahi; los marcados [GUARDA] ya pasaban y
quedan para que el fix no los rompa (la regla "sin retencion nunca se crea el archivo"
ya se cumplia por accidente y ahora se cumple por diseno).

Cero red, cero proveedor LLM real, CV sintetico. config.CV_PROFILE_PATH va a un
temporal por la fixture autouse de conftest.py.
"""

import asyncio
import io
import json
import re
import sys
import threading
import time
import types
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config as config_module
import nlp_engine
import run_manager as run_manager_module

REPO = Path(__file__).resolve().parent.parent

# Version del aviso de envio de CV que la UI muestra y el server exige. Duplicada a
# proposito: si alguien la cambia en config.py sin cambiarla en el frontend (o al
# reves), test_version_del_aviso_coincide_con_el_frontend lo grita.
CONSENT = "cv-llm-2026-09-30"

VALID_CONFIG = {
    "target_titles": ["senior python backend", "sdet"],
    "must_exclude": ["contract"],
    "min_salary": 90000,
}

PERFIL = {
    "role": "Backend Engineer",
    "core_skills": ["python", "fastapi"],
    "location": "LATAM (UTC-5)",
    "timezone": "UTC-5",
    "key_projects": [],
    "audit_signals": [],
}


# =============================================================================
# Fixtures y helpers
# =============================================================================

def _apagar(sched) -> None:
    """Apaga el APScheduler de una instancia; tolera que ya lo hayan apagado."""
    if sched.enabled:
        sched.disable()
    if sched._scheduler.running:
        sched._scheduler.shutdown(wait=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "test_registry.db"))
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
    _apagar(api_module.app.state.sched)


@pytest.fixture()
def perfil_stub(monkeypatch):
    """El proveedor LLM va con stub: cuenta llamadas y captura el texto recibido."""
    llamadas: list[str] = []

    def _fake(text, raise_on_failure=False):
        llamadas.append(text)
        return dict(PERFIL)

    monkeypatch.setattr(api_module, "generate_ephemeral_profile", _fake)
    return llamadas


@contextmanager
def reiniciar():
    """
    Simula un reinicio del server: apaga el APScheduler viejo (un proceso real muere con
    todos sus hilos) y levanta un lifespan nuevo sobre el mismo disco temporal.
    """
    _apagar(api_module.app.state.sched)
    with TestClient(api_module.app) as c2:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        api_module.app.state.sched.notion = None
        api_module.app.state.sched.sheets = None
        api_module.app.state.runs._notion = None
        api_module.app.state.runs._sheets = None
        try:
            yield c2
        finally:
            _apagar(api_module.app.state.sched)


def subir_cv(client, nombre="cv.txt", cuerpo=b"Ana Perez - Backend Engineer - Python", consent=CONSENT):
    data = {"llm_consent": consent} if consent is not None else {}
    return client.post("/api/cv", files={"file": (nombre, cuerpo, "text/plain")}, data=data)


def put_config(client):
    assert client.put("/api/config", json=VALID_CONFIG).status_code == 200


def activar_cron(client, retain_cv, enabled=True, mode="cron"):
    r = client.put("/api/scheduler", json={
        "enabled": enabled, "mode": mode, "cron_days": ["tue"], "cron_hour": 9,
        "retain_cv": retain_cv,
    })
    assert r.status_code == 200, r.text
    return r.json()


def archivo_perfil() -> Path:
    return Path(config_module.CV_PROFILE_PATH)


def estado_en_disco() -> dict:
    return json.loads(api_module.API_STATE_FILE.read_text(encoding="utf-8"))


def perfil_que_usa_el_cron(monkeypatch):
    """
    Dispara el cron de verdad (SchedulerService._fire) con el grafo stubbeado y devuelve
    el perfil que le llego a run_pipeline. "Presente" no alcanza para el punto 11: tiene
    que estar USADO por la corrida desatendida.
    """
    visto: dict = {}
    listo = threading.Event()

    def _fake_pipeline(**kw):
        visto["profile"] = kw.get("profile")
        listo.set()
        return {"jobs": [], "tier1": [], "tier2": []}

    monkeypatch.setattr("graph.run_graph.run_pipeline", _fake_pipeline)
    monkeypatch.setattr("integrations.webhook_client.send_discord_alert", lambda *a, **k: None)
    import scraper
    monkeypatch.setattr(scraper, "try_acquire_scrape_gate", lambda: True)
    monkeypatch.setattr(scraper, "release_scrape_gate", lambda: None)

    sched = api_module.app.state.sched
    sched.is_running = False
    sched._fire(api_module.app.state.db)
    assert listo.wait(10), "el cron no llego a llamar al grafo"
    deadline = time.monotonic() + 5
    while sched.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    return visto["profile"]


# =============================================================================
# PUNTO 11 — ciclo de vida del perfil
# =============================================================================

class TestPerfilCronConRetencion:
    def test_preferencia_real_retain_cv_se_serializa(self, client, perfil_stub):
        """[ROJO] _persist_state escribia retain_cv=False aunque el usuario pidio True."""
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)
        assert archivo_perfil().exists()
        assert estado_en_disco()["schedule"]["retain_cv"] is True

    def test_dos_reinicios_conservan_y_usan_el_perfil(self, client, perfil_stub, monkeypatch):
        """
        [ROJO] Escenario de revision externa: cron activo con retencion, reinicio 1 -> _apply_pending_
        schedule reconfigura con retain_profile=False y _discard_profile borra el archivo;
        reinicio 2 -> cron sin perfil.
        """
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)

        for n in (1, 2):
            with reiniciar() as c:
                assert api_module.app.state.sched.enabled, f"reinicio {n}: el cron no volvio"
                assert archivo_perfil().exists(), f"reinicio {n}: el archivo retenido se borro"
                assert api_module.app.state.sched.profile is not None, f"reinicio {n}: sched sin perfil"
                # La UI ve el mismo perfil que usa el cron (si no, "Descartar" no existe)
                assert c.get("/api/cv").json()["profile"]["role"] == "Backend Engineer"
                usado = perfil_que_usa_el_cron(monkeypatch)
                assert usado is not None and usado["role"] == "Backend Engineer", (
                    f"reinicio {n}: el cron corrio sin perfil"
                )

    def test_put_config_tras_reinicio_no_pisa_el_perfil_retenido(self, client, perfil_stub, monkeypatch):
        """[ROJO] update_config(cfg, app.state.profile=None) dejaba sched.profile en None."""
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)
        with reiniciar() as c:
            put_config(c)
            assert api_module.app.state.sched.profile is not None
            assert perfil_que_usa_el_cron(monkeypatch) is not None

    def test_estado_legado_con_archivo_y_sin_clave_conserva_el_perfil(self, client, perfil_stub):
        """
        [ROJO] api_state.json escrito por la version anterior: schedule.retain_cv=False
        (el bug) y sin clave de nivel superior, pero cv_profile.json existe porque el
        usuario SI eligio retener. El archivo solo lo escribe _save_profile con
        retain=True, asi que su existencia es la evidencia de la preferencia.
        """
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)
        legado = estado_en_disco()
        legado.pop("retain_cv", None)
        legado["schedule"]["retain_cv"] = False
        api_module.API_STATE_FILE.write_text(json.dumps(legado), encoding="utf-8")
        with reiniciar() as c:
            assert archivo_perfil().exists()
            assert c.get("/api/cv").json()["profile"] is not None

    def test_clave_explicita_false_borra_un_archivo_huerfano(self, client, perfil_stub):
        """[GUARDA] La preferencia explicita False manda: un cv_profile.json suelto se borra."""
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=False)
        archivo_perfil().write_text(json.dumps(PERFIL), encoding="utf-8")
        with reiniciar() as c:
            assert not archivo_perfil().exists()
            assert c.get("/api/cv").json()["profile"] is None


class TestPerfilSinRetencion:
    def test_cron_sin_retencion_nunca_crea_el_archivo(self, client, perfil_stub, monkeypatch):
        """[GUARDA] Activar cron sin retencion no crea cv_profile.json en ningun paso."""
        assert not archivo_perfil().exists()
        subir_cv(client)
        assert not archivo_perfil().exists(), "subir el CV creo el archivo"
        put_config(client)
        activar_cron(client, retain_cv=False)
        assert not archivo_perfil().exists(), "activar el cron sin retencion creo el archivo"
        # El cron usa el perfil de la sesion, en memoria
        assert perfil_que_usa_el_cron(monkeypatch) is not None
        assert estado_en_disco()["schedule"]["retain_cv"] is False
        with reiniciar():
            assert not archivo_perfil().exists(), "el reinicio creo el archivo"
            # Sin retencion el perfil muere con el proceso: la UI no debe afirmar otra cosa
            assert api_module.app.state.sched.profile is None

    def test_subir_cv_con_cron_activo_llega_al_cron(self, client, perfil_stub, monkeypatch):
        """
        [ROJO] upload_cv solo seteaba app.state.profile: un CV subido DESPUES de activar
        el cron no llegaba a sched.profile hasta el siguiente PUT /api/config.
        """
        put_config(client)
        activar_cron(client, retain_cv=False)
        subir_cv(client)
        assert api_module.app.state.sched.profile is not None
        assert not archivo_perfil().exists()

    def test_subir_cv_con_retencion_activa_lo_guarda(self, client, perfil_stub):
        """[ROJO] Con retain_cv=True el CV nuevo tambien se retiene (antes solo al activar)."""
        put_config(client)
        activar_cron(client, retain_cv=True)
        assert not archivo_perfil().exists()  # no habia perfil que retener
        subir_cv(client)
        assert archivo_perfil().exists()

    def test_apagar_retencion_con_cron_activo_borra_el_archivo_no_la_sesion(self, client, perfil_stub):
        """[GUARDA] "No retener" = el perfil ya no queda en disco; la sesion sigue viva."""
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)
        assert archivo_perfil().exists()
        activar_cron(client, retain_cv=False)
        assert not archivo_perfil().exists()
        assert client.get("/api/cv").json()["profile"] is not None


class TestClearProfile:
    def _con_perfil_retenido(self, client):
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)
        assert archivo_perfil().exists()

    def test_delete_cv_limpia_api_scheduler_y_archivo(self, client, perfil_stub, monkeypatch):
        """
        [ROJO] DELETE /api/cv solo limpiaba app.state.profile. La UI decia "Perfil
        descartado — los scores vuelven a ser genericos" y el cron del martes seguia
        puntuando con el perfil retenido.
        """
        self._con_perfil_retenido(client)
        assert client.delete("/api/cv").json() == {"profile": None}
        assert client.get("/api/cv").json()["profile"] is None
        assert api_module.app.state.sched.profile is None
        assert not archivo_perfil().exists()
        assert perfil_que_usa_el_cron(monkeypatch) is None

    def test_delete_cv_sobrevive_al_reinicio(self, client, perfil_stub):
        """[ROJO] Tras descartar y reiniciar, el perfil no resucita desde el archivo."""
        self._con_perfil_retenido(client)
        client.delete("/api/cv")
        with reiniciar() as c:
            assert c.get("/api/cv").json()["profile"] is None
            assert api_module.app.state.sched.profile is None

    def test_reset_limpia_api_scheduler_y_archivo(self, client, perfil_stub, monkeypatch):
        """[ROJO] Reset no tocaba sched.profile ni cv_profile.json."""
        self._con_perfil_retenido(client)
        assert client.post("/api/reset").json() == {"reset": True}
        assert client.get("/api/cv").json()["profile"] is None
        assert api_module.app.state.sched.profile is None
        assert not archivo_perfil().exists()
        assert api_module.app.state.sched.retain_cv is False
        assert estado_en_disco()["schedule"] is None

    def test_desactivar_cron_pidiendo_no_retener_limpia_el_perfil(self, client, perfil_stub):
        """[ROJO] Apagar el cron con retain_cv=False dejaba el archivo en disco."""
        self._con_perfil_retenido(client)
        activar_cron(client, retain_cv=False, enabled=False)
        assert not archivo_perfil().exists()
        assert client.get("/api/cv").json()["profile"] is None
        assert api_module.app.state.sched.profile is None

    def test_desactivar_cron_conservando_retencion_no_borra(self, client, perfil_stub):
        """[GUARDA] Apagar el cron CON retencion es pausar, no descartar."""
        self._con_perfil_retenido(client)
        activar_cron(client, retain_cv=True, enabled=False)
        assert archivo_perfil().exists()
        assert client.get("/api/cv").json()["profile"] is not None

    def test_desactivar_cron_sin_retencion_previa_no_borra_la_sesion(self, client, perfil_stub):
        """[GUARDA] Quien nunca retuvo y apaga el cron no pierde su perfil de sesion."""
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=False)
        activar_cron(client, retain_cv=False, enabled=False)
        assert client.get("/api/cv").json()["profile"] is not None

    def test_get_scheduler_expone_la_preferencia(self, client, perfil_stub):
        """[ROJO] La UI no podia mostrar la preferencia real: el toggle nacia en False."""
        subir_cv(client)
        put_config(client)
        activar_cron(client, retain_cv=True)
        assert client.get("/api/scheduler").json()["retain_cv"] is True


class TestIgnoreFiles:
    def test_gitignore_nombra_el_perfil_derivado(self):
        """[ROJO] Hoy lo cubre el comodin *.json: un cambio al comodin lo expone."""
        lineas = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "cv_profile.json" in [l.strip() for l in lineas]

    def test_dockerignore_excluye_el_perfil_derivado(self):
        """[ROJO] El perfil retenido es dato personal: no puede viajar en la imagen."""
        lineas = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        assert "cv_profile.json" in [l.strip() for l in lineas]

    def test_dockerignore_sin_comentarios_al_final_de_linea(self):
        """
        [GUARDA] El 2026-09-29 `cookies/*.json   # nota` no excluyo nada: .dockerignore no
        soporta comentarios al final y el '#' pasa a ser parte del patron.
        """
        malas = [
            l for l in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#") and "#" in l
        ]
        assert malas == []


# =============================================================================
# PUNTO 12 — aviso previo, limite de upload, limites del parser, redaccion local
# =============================================================================

class TestConsentimientoEnElServidor:
    def test_sin_aceptacion_no_se_envia_nada(self, client, perfil_stub):
        """
        [ROJO] POST /api/cv mandaba el texto al LLM sin ninguna senal de que el usuario
        hubiera visto a donde va. Sin aceptacion el proveedor no recibe una sola llamada.
        """
        r = subir_cv(client, consent=None)
        assert r.status_code == 428
        assert perfil_stub == []
        assert client.get("/api/cv").json()["profile"] is None

    def test_version_vieja_del_aviso_se_rechaza(self, client, perfil_stub):
        """[ROJO] Si el aviso cambia, la aceptacion de la version anterior no vale."""
        r = subir_cv(client, consent="cv-llm-2000-01-01")
        assert r.status_code == 428
        assert perfil_stub == []

    def test_con_aceptacion_el_flujo_funciona(self, client, perfil_stub):
        """[GUARDA] El camino feliz sigue igual."""
        r = subir_cv(client)
        assert r.status_code == 200
        assert r.json()["profile"]["role"] == "Backend Engineer"
        assert len(perfil_stub) == 1

    def test_version_del_aviso_coincide_con_el_frontend(self):
        """[ROJO] Una constante duplicada sin guarda es una deriva esperando ocurrir."""
        fuente = (REPO / "frontend" / "src" / "consent.ts").read_text(encoding="utf-8")
        m = re.search(r"CV_CONSENT_VERSION\s*=\s*'([^']+)'", fuente)
        assert m, "frontend/src/consent.ts no declara CV_CONSENT_VERSION"
        assert m.group(1) == config_module.CV_CONSENT_VERSION == CONSENT


class TestLimiteDeUpload:
    def test_archivo_sobre_el_limite_da_413_y_no_llega_al_parser(self, client, perfil_stub, monkeypatch):
        """
        [ROJO] upload_cv hacia file.file.read() entero y despues extract_cv_text: un
        archivo de 6 MiB llegaba al parser y respondia 200/422, nunca 413.
        """
        parser: list[int] = []
        monkeypatch.setattr(
            api_module, "extract_cv_text",
            lambda name, data: parser.append(len(data)) or "texto",
        )
        r = subir_cv(client, nombre="cv.pdf", cuerpo=b"a" * (6 * 1024 * 1024))
        assert r.status_code == 413
        assert "MB" in r.json()["detail"]
        assert parser == [], "el parser recibio bytes de un archivo sobre el limite"
        assert perfil_stub == []

    def test_archivo_dentro_del_limite_pasa(self, client, perfil_stub):
        """[GUARDA] El tope no rechaza un CV normal."""
        r = subir_cv(client, cuerpo=b"x" * (200 * 1024))
        assert r.status_code == 200

    def test_content_length_declarado_se_rechaza_sin_consumir_el_cuerpo(self, client, monkeypatch):
        """
        [ROJO] A nivel ASGI: un Content-Length de 20 MiB sobre un limite de 1 MiB tiene que
        responder 413 sin pedir un solo chunk del cuerpo. Antes de este fix FastAPI
        consumia el stream entero a un archivo temporal antes de ejecutar el endpoint.
        """
        monkeypatch.setattr(config_module, "CV_MAX_UPLOAD_BYTES", 1024 * 1024, raising=False)
        estado = _enviar_multipart(chunks=20, declarar_content_length=True)
        assert estado["status"] == 413
        assert estado["chunks_consumidos"] == 0

    def test_sin_content_length_se_corta_al_pasar_el_limite(self, client, monkeypatch):
        """
        [ROJO] Transfer-Encoding: chunked (o un Content-Length mentiroso) no trae el tamano
        de antemano: el corte tiene que ocurrir al cruzar el limite, no al final del stream.
        """
        monkeypatch.setattr(config_module, "CV_MAX_UPLOAD_BYTES", 1024 * 1024, raising=False)
        estado = _enviar_multipart(chunks=200, declarar_content_length=False)
        assert estado["status"] == 413
        # limite 1 MiB + holgura multipart: con chunks de 256 KiB son ~5-6 chunks, no 200
        assert estado["chunks_consumidos"] <= 8


def _enviar_multipart(chunks: int, declarar_content_length: bool) -> dict:
    """Llama a la app por ASGI crudo con un multipart valido de `chunks` x 256 KiB."""
    chunk = b"a" * (256 * 1024)
    cabecera = (
        b'--x\r\nContent-Disposition: form-data; name="file"; filename="cv.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    cola = b"\r\n--x--\r\n"
    total = len(cabecera) + chunks * len(chunk) + len(cola)
    piezas = [cabecera] + [chunk] * chunks + [cola]
    estado = {"status": None, "chunks_consumidos": 0}
    indice = {"i": 0}

    async def receive():
        i = indice["i"]
        if i >= len(piezas):
            return {"type": "http.disconnect"}
        indice["i"] += 1
        if i > 0 and i <= chunks:
            estado["chunks_consumidos"] += 1
        return {
            "type": "http.request", "body": piezas[i], "more_body": i < len(piezas) - 1,
        }

    async def send(message):
        if message["type"] == "http.response.start":
            estado["status"] = message["status"]

    # Host: el guard de la Fase 3 es el middleware mas externo y rechaza (400) un request
    # sin Host antes de que llegue al tope de bytes. Un navegador siempre lo manda.
    headers = [(b"host", b"localhost:8000"),
               (b"content-type", b"multipart/form-data; boundary=x")]
    if declarar_content_length:
        headers.append((b"content-length", str(total).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/cv", "raw_path": b"/api/cv", "root_path": "",
        "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000), "state": {},
    }
    asyncio.run(api_module.app(scope, receive, send))
    return estado


class TestLecturaAcotadaEnElEndpoint:
    def test_upload_cv_no_lee_mas_de_limite_mas_uno(self, monkeypatch):
        """
        [GUARDA] Defensa en profundidad: aunque el middleware no estuviera, el endpoint
        pide como mucho limite+1 bytes. read() sin argumento es la falla original.
        """
        from fastapi import HTTPException, UploadFile

        pedidos: list[int] = []

        class Contador(io.BytesIO):
            def read(self, n=-1):
                pedidos.append(n)
                return super().read(n)

        monkeypatch.setattr(config_module, "CV_MAX_UPLOAD_BYTES", 1024, raising=False)
        archivo = UploadFile(file=Contador(b"a" * 5000), filename="cv.txt")
        with pytest.raises(HTTPException) as exc:
            api_module.upload_cv(file=archivo, llm_consent=CONSENT)
        assert exc.value.status_code == 413
        assert pedidos and all(n == 1025 for n in pedidos), pedidos


# -- limites del parser ------------------------------------------------------

class _PaginaFake:
    def __init__(self, texto, espera=None, registro=None, numero=0):
        self._texto, self._espera = texto, espera
        self._registro, self._numero = registro, numero

    def extract_text(self):
        if self._registro is not None:
            self._registro.append(self._numero)
        if self._espera is not None:
            self._espera.wait(timeout=3)
        return self._texto


class _PdfFake:
    def __init__(self, paginas):
        self.pages = paginas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _instalar_pdfplumber_fake(monkeypatch, paginas):
    modulo = types.ModuleType("pdfplumber")
    modulo.open = lambda stream, **kw: _PdfFake(paginas)
    monkeypatch.setitem(sys.modules, "pdfplumber", modulo)


class TestLimitesDelParser:
    def test_pdf_se_corta_por_paginas(self, monkeypatch):
        """
        [ROJO] extract_cv_text recorria TODAS las paginas: un PDF de 500 paginas costaba 500
        extract_text para quedarse con 8 000 caracteres.
        """
        monkeypatch.setattr(config_module, "CV_MAX_PDF_PAGES", 3, raising=False)
        registro: list[int] = []
        paginas = [_PaginaFake(f"p{i} " * 10, registro=registro, numero=i) for i in range(50)]
        _instalar_pdfplumber_fake(monkeypatch, paginas)
        texto = nlp_engine.extract_cv_text("cv.pdf", b"%PDF-fake")
        assert registro == [0, 1, 2]
        assert "p0" in texto and "p3" not in texto

    def test_pdf_real_se_corta_por_paginas(self, monkeypatch):
        """[ROJO] Lo mismo con pdfplumber de verdad: la API `pages=` existe y se respeta."""
        monkeypatch.setattr(config_module, "CV_MAX_PDF_PAGES", 3, raising=False)
        texto = nlp_engine.extract_cv_text("cv.pdf", _pdf_sintetico(paginas=12))
        assert "PAGINA-1" in texto and "PAGINA-3" in texto
        assert "PAGINA-4" not in texto and "PAGINA-12" not in texto

    def test_pdf_se_corta_por_tiempo_sin_esperar_a_la_pagina_lenta(self, monkeypatch):
        """
        [ROJO] Una pagina patologica bloqueaba la peticion hasta terminar. Con presupuesto
        de 0.3 s la llamada vuelve con lo ya extraido; el hilo huerfano termina solo.
        """
        monkeypatch.setattr(config_module, "CV_PARSE_TIMEOUT_SECONDS", 0.3, raising=False)
        liberar = threading.Event()
        paginas = [_PaginaFake("Perfil Backend Engineer"), _PaginaFake("nunca", espera=liberar)]
        _instalar_pdfplumber_fake(monkeypatch, paginas)
        t0 = time.monotonic()
        try:
            texto = nlp_engine.extract_cv_text("cv.pdf", b"%PDF-fake")
        finally:
            liberar.set()
        assert time.monotonic() - t0 < 1.5
        assert "Backend Engineer" in texto

    def test_pdf_normal_no_se_degrada(self):
        """[GUARDA] Un PDF corto sigue extrayendose completo con el parser real."""
        texto = nlp_engine.extract_cv_text("cv.pdf", _pdf_sintetico(paginas=2))
        assert "PAGINA-1" in texto and "PAGINA-2" in texto

    def test_docx_con_descompresion_enorme_no_llega_a_python_docx(self, monkeypatch):
        """
        [ROJO] Un .docx es un zip: 5 KB comprimidos pueden ser 50 MB de XML. python-docx lo
        descomprimia y parseaba entero antes de que nadie mirara el tamano.
        """
        import docx
        monkeypatch.setattr(config_module, "CV_MAX_DOCX_UNCOMPRESSED_BYTES", 1024 * 1024, raising=False)
        llamadas: list[int] = []
        monkeypatch.setattr(docx, "Document", lambda *a, **k: llamadas.append(1) or (_ for _ in ()).throw(RuntimeError("no deberia")))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"\0" * (5 * 1024 * 1024))
        assert len(buf.getvalue()) < 100 * 1024  # el "bomb": chico comprimido, enorme abierto
        assert nlp_engine.extract_cv_text("cv.docx", buf.getvalue()) == ""
        assert llamadas == []

    def test_docx_normal_no_se_degrada(self):
        """[GUARDA] Un .docx real y chico se extrae con python-docx."""
        import docx
        d = docx.Document()
        d.add_paragraph("Ana Perez - Backend Engineer")
        d.add_paragraph("Python, FastAPI, SQLite")
        buf = io.BytesIO()
        d.save(buf)
        texto = nlp_engine.extract_cv_text("cv.docx", buf.getvalue())
        assert "Backend Engineer" in texto and "FastAPI" in texto


def _pdf_sintetico(paginas: int) -> bytes:
    """PDF valido a mano (sin dependencias nuevas): una linea 'PAGINA-n' por pagina."""
    objetos: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(paginas))
    objetos.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objetos.append(f"<< /Type /Pages /Kids [{kids}] /Count {paginas} >>".encode())
    for i in range(paginas):
        contenido = f"BT /F1 12 Tf 72 720 Td (PAGINA-{i + 1}) Tj ET".encode()
        objetos.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Contents {4 + 2 * i} 0 R /Resources << /Font << /F1 "
             f"{3 + 2 * paginas} 0 R >> >> >>").encode()
        )
        objetos.append(
            f"<< /Length {len(contenido)} >>\nstream\n".encode() + contenido + b"\nendstream"
        )
    objetos.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    salida = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, cuerpo in enumerate(objetos, start=1):
        offsets.append(len(salida))
        salida += f"{n} 0 obj\n".encode() + cuerpo + b"\nendobj\n"
    xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        salida += f"{off:010d} 00000 n \n".encode()
    salida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(salida)


# -- redaccion local de email/telefono (Solucion B parcial de revision externa) -------------

CV_SINTETICO = """Ana Perez Gomez
Backend Engineer | SRE
ana.perez+cv@example.com | +57 312 654 321 | (02) 234-5678 | 312654321
Bogota, Colombia

Experiencia
2019 - 2023  Senior Backend Engineer en Acme: Python 3.12, FastAPI, PostgreSQL 15.4.
2016 - 2019  SRE: 99.99% uptime, ISO 27001, Node 18.17.1, 1,200,000 requests/dia.
Skills: Python, FastAPI, SQLite, Docker, Kubernetes
"""


class TestRedaccionLocal:
    def test_el_prompt_que_sale_al_proveedor_no_lleva_email_ni_telefono(self, monkeypatch):
        """
        [ROJO] generate_ephemeral_profile metia el texto crudo en el prompt: el proveedor
        recibia el email y los telefonos tal cual.
        """
        capturado: dict = {}

        def _fake_chain(**kw):
            capturado["prompt"] = kw["messages"][0]["content"]
            return {"parsed": dict(PERFIL)}

        monkeypatch.setattr(nlp_engine, "_run_chain", _fake_chain)
        nlp_engine.generate_ephemeral_profile(CV_SINTETICO, raise_on_failure=True)
        prompt = capturado["prompt"]
        assert "ana.perez+cv@example.com" not in prompt
        assert "example.com" not in prompt
        for telefono in ("312 654 321", "312654321", "234-5678"):
            assert telefono not in prompt, telefono
        # La utilidad sobrevive: rol, skills, anos, versiones y metricas siguen
        for conserva in ("Backend Engineer", "SRE", "Python 3.12", "FastAPI", "PostgreSQL 15.4",
                         "2019 - 2023", "2016 - 2019", "99.99%", "ISO 27001", "Node 18.17.1",
                         "1,200,000", "Kubernetes"):
            assert conserva in prompt, conserva

    @pytest.mark.parametrize("texto", [
        "Periodo 2019 - 2023 y 2016 - 2019",
        "Python 3.12.1 sobre Ubuntu 22.04.3",
        "IPv4 192.168.100.200",
        "Version 10.0.19045.1234",
        "Ventas 120,000,000 USD",
        "01/2019 - 03/2021",
    ])
    def test_no_redacta_numeros_que_no_son_telefonos(self, texto):
        """[ROJO] Una redaccion que se come anos y versiones destruye el perfil."""
        assert nlp_engine._redact_contact_data(texto) == texto

    @pytest.mark.parametrize("texto", [
        "+57 312 654 321",
        "+1 (415) 555-2671",
        "312-654-321",
        "(02) 234-5678",
        "312654321",
        "Tel: 999 888 777",
    ])
    def test_redacta_formatos_de_telefono_comunes(self, texto):
        """[ROJO]"""
        salida = nlp_engine._redact_contact_data(texto)
        assert not re.search(r"\d{3}", salida), salida
        assert "[telefono]" in salida

    def test_redacta_emails(self):
        """[ROJO]"""
        salida = nlp_engine._redact_contact_data("Escribeme a ana.perez+cv@mail.example.co o a b_c@x.io.")
        assert "@" not in salida
        assert salida.count("[email]") == 2
