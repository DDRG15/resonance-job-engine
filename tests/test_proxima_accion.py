"""
tests/test_proxima_accion.py — La próxima acción de una
postulación y su alerta de vencida.

`follow_up_at` se escribía desde la Fase 7 y ningún código lo leía. Estos tests fijan los
tres lectores que lo conectan: la regla de «vencida» (base), el endpoint (API) y el
reporte del cron (Discord). Ninguno depende del reloj: las fechas son fijas y la regla
recibe `hoy` por parámetro, salvo los tests de API, que usan 2020 y 2099 para no caducar.
"""

import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

import api as api_module
import applications as app
import config as config_module
import database as database_module
import run_manager as run_manager_module
from database import GemaDatabase, SIN_CAMBIO
from graph.runtime import RuntimeContext

HOY = date(2026, 9, 30)
WEBHOOK_DE_PRUEBA = "https://discord.invalid/api/webhooks/0/token-de-prueba"


@pytest.fixture()
def db(tmp_path):
    return GemaDatabase(str(tmp_path / "t.db"))


def _crear(db, n, *, fecha=None, accion=None, status="postulado"):
    return db.crear_postulacion(f"https://x.com/{n}", status=status,
                                follow_up_at=fecha, next_action=accion)


def _urls(filas):
    return [f["url"] for f in filas]


# ── La regla de «vencida» ────────────────────────────────────────────────────

class TestVencidas:
    def test_fecha_pasada_con_postulacion_abierta_aparece(self, db):
        a = _crear(db, 1, fecha="2026-09-25", accion="escribir al recruiter")
        v = db.vencidas(hoy=HOY)
        assert [x["app_id"] for x in v] == [a["app_id"]]
        assert v[0]["next_action"] == "escribir al recruiter"
        assert v[0]["vencida"] is True

    def test_fecha_futura_no_aparece(self, db):
        _crear(db, 1, fecha="2026-10-05", accion="llamar")
        assert db.vencidas(hoy=HOY) == []

    def test_la_fecha_de_hoy_no_esta_vencida(self, db):
        """Una acción para hoy todavía se puede hacer. Marcarla vencida a la mañana
        enseña a ignorar la marca."""
        _crear(db, 1, fecha="2026-09-30", accion="llamar")
        assert db.vencidas(hoy=HOY) == []

    def test_el_dia_anterior_ya_esta_vencida(self, db):
        _crear(db, 1, fecha="2026-09-29", accion="llamar")
        assert len(db.vencidas(hoy=HOY)) == 1

    def test_sin_fecha_no_aparece(self, db):
        _crear(db, 1, fecha=None, accion="una accion sin fecha")
        assert db.vencidas(hoy=HOY) == []

    @pytest.mark.parametrize("terminal", sorted(app.TERMINALES))
    def test_una_postulacion_cerrada_no_aparece(self, db, terminal):
        """Rechazada, sin respuesta, retirada o aceptada: no hay nada que atender.
        Contarla inflaría el badge con cosas que nadie puede hacer."""
        a = _crear(db, 1, fecha="2026-09-01", accion="escribir")
        assert len(db.vencidas(hoy=HOY)) == 1
        db.mover_postulacion(a["app_id"], terminal)
        assert db.vencidas(hoy=HOY) == []

    def test_un_borrador_con_fecha_pasada_cuenta_como_abierto(self, db):
        _crear(db, 1, fecha="2026-09-01", accion="decidir si aplico", status=app.BORRADOR)
        assert len(db.vencidas(hoy=HOY)) == 1

    def test_la_mas_atrasada_va_primero(self, db):
        _crear(db, 1, fecha="2026-09-28", accion="b")
        _crear(db, 2, fecha="2026-09-01", accion="a")
        _crear(db, 3, fecha="2026-09-15", accion="c")
        assert _urls(db.vencidas(hoy=HOY)) == [
            "https://x.com/2", "https://x.com/3", "https://x.com/1"]

    def test_las_cerradas_viejas_no_se_comen_el_tope(self, db):
        """El LIMIT se aplica después de filtrar. Si se aplicara antes, tres rechazadas
        con fecha vieja dejarían afuera a la única postulación abierta y vencida."""
        for n in (1, 2, 3):
            a = _crear(db, n, fecha="2020-01-01", accion="x")
            db.mover_postulacion(a["app_id"], app.RECHAZADO)
        _crear(db, 4, fecha="2026-09-01", accion="la abierta")
        v = db.listar_postulaciones(solo_vencidas=True, limit=1, hoy=HOY)
        assert _urls(v) == ["https://x.com/4"]

    def test_cada_fila_de_la_lista_lleva_su_marca(self, db):
        _crear(db, 1, fecha="2026-09-01", accion="a")
        _crear(db, 2, fecha="2026-12-01", accion="b")
        marcas = {f["url"]: f["vencida"] for f in db.listar_postulaciones(hoy=HOY)}
        assert marcas == {"https://x.com/1": True, "https://x.com/2": False}

    def test_una_fecha_ilegible_no_vence_pero_queda_en_el_log(self, db, caplog):
        """Una fecha escrita antes de que la API la validara («la semana que viene»)
        no se puede comparar. No avisa nunca, y eso se dice en vez de callarlo."""
        a = _crear(db, 1)
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE applications SET follow_up_at='la semana que viene' "
                         "WHERE app_id=?", (a["app_id"],))
        with caplog.at_level("WARNING"):
            assert db.vencidas(hoy=HOY) == []
        assert any("ilegible" in r.message for r in caplog.records)

    def test_hoy_local_sobrevive_a_una_zona_horaria_invalida(self, monkeypatch, caplog):
        monkeypatch.setattr(config_module, "SCHEDULER_TIMEZONE", "Mars/Olympus")
        with caplog.at_level("WARNING"):
            assert isinstance(database_module.hoy_local(), date)
        assert any("SCHEDULER_TIMEZONE" in r.message for r in caplog.records)


# ── Validación ───────────────────────────────────────────────────────────────

class TestNormalizacion:
    @pytest.mark.parametrize("valor,esperado", [
        ("2026-10-05", "2026-10-05"),
        ("2026-10-05T14:30:00", "2026-10-05"),
        ("2026-10-05T14:30:00Z", "2026-10-05"),
        ("  2026-10-05  ", "2026-10-05"),
        ("", None), ("   ", None), (None, None),
    ])
    def test_fechas_aceptadas(self, valor, esperado):
        assert database_module.normalizar_fecha_accion(valor) == esperado

    @pytest.mark.parametrize("valor", ["mañana", "2026-13-40", "05/10/2026", "20261005x", 20261005])
    def test_fechas_rechazadas(self, valor):
        with pytest.raises(ValueError, match="follow_up_at"):
            database_module.normalizar_fecha_accion(valor)

    def test_el_texto_se_recorta_de_espacios_y_vacio_es_None(self):
        assert database_module.normalizar_proxima_accion("  escribir  ") == "escribir"
        assert database_module.normalizar_proxima_accion("   ") is None

    def test_un_texto_demasiado_largo_se_rechaza_no_se_trunca(self):
        limite = database_module.MAX_PROXIMA_ACCION
        assert database_module.normalizar_proxima_accion("x" * limite) == "x" * limite
        with pytest.raises(ValueError, match="next_action"):
            database_module.normalizar_proxima_accion("x" * (limite + 1))


# ── Edición ──────────────────────────────────────────────────────────────────

class TestEditar:
    def test_cambiar_solo_el_texto_no_toca_la_fecha(self, db):
        a = _crear(db, 1, fecha="2026-10-05", accion="vieja")
        b = db.editar_proxima_accion(a["app_id"], next_action="nueva")
        assert (b["next_action"], b["follow_up_at"]) == ("nueva", "2026-10-05")

    def test_borrar_la_fecha_conserva_el_texto(self, db):
        a = _crear(db, 1, fecha="2026-10-05", accion="llamar")
        b = db.editar_proxima_accion(a["app_id"], follow_up_at=None)
        assert (b["next_action"], b["follow_up_at"]) == ("llamar", None)

    def test_se_edita_sin_mover_de_estado_ni_tocar_el_historial(self, db):
        """El histórico registra por qué ESTADOS pasó la postulación y la conversión se
        mide sobre él. Una fecha movida tres veces no es una transición."""
        a = _crear(db, 1)
        b = db.editar_proxima_accion(a["app_id"], next_action="x", follow_up_at="2026-10-01")
        assert b["status"] == a["status"]
        assert b["stage_changed_at"] == a["stage_changed_at"]
        assert len(db.historial_postulacion(a["app_id"])) == 1

    def test_una_fecha_invalida_no_escribe_nada(self, db):
        a = _crear(db, 1, fecha="2026-10-05", accion="llamar")
        with pytest.raises(ValueError):
            db.editar_proxima_accion(a["app_id"], next_action="otra", follow_up_at="mañana")
        fila = db.listar_postulaciones()[0]
        assert (fila["next_action"], fila["follow_up_at"]) == ("llamar", "2026-10-05")

    def test_editar_sin_campos_es_un_error(self, db):
        a = _crear(db, 1)
        with pytest.raises(ValueError, match="nada que cambiar"):
            db.editar_proxima_accion(a["app_id"])

    def test_una_postulacion_inexistente_lanza_KeyError(self, db):
        with pytest.raises(KeyError):
            db.editar_proxima_accion("no-existe", next_action="x")

    def test_se_puede_editar_una_cerrada_pero_no_cuenta_como_vencida(self, db):
        a = _crear(db, 1, fecha="2026-09-01", accion="x")
        db.mover_postulacion(a["app_id"], app.RECHAZADO)
        b = db.editar_proxima_accion(a["app_id"], next_action="queda como dato")
        assert b["next_action"] == "queda como dato"
        assert db.vencidas(hoy=HOY) == []

    def test_crear_con_fecha_invalida_no_deja_la_postulacion(self, db):
        with pytest.raises(ValueError):
            db.crear_postulacion("https://x.com/1", follow_up_at="mañana")
        assert db.listar_postulaciones() == []

    def test_SIN_CAMBIO_no_es_None(self):
        assert SIN_CAMBIO is not None


# ── Migración sobre una base vieja ───────────────────────────────────────────

class TestMigracion:
    # El DDL de `applications` tal como lo creó la Fase 7, sin `next_action`.
    _DDL_FASE_7 = """
    CREATE TABLE applications (
        app_id TEXT PRIMARY KEY, job_hash TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
        title TEXT, company TEXT, source_domain TEXT, tier_al_aplicar TEXT,
        score_al_aplicar INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL,
        stage_changed_at TEXT NOT NULL, applied_at TEXT, closed_at TEXT,
        salary_offered INTEGER, follow_up_at TEXT, notes TEXT
    )"""

    @pytest.fixture()
    def base_vieja(self, tmp_path):
        ruta = str(tmp_path / "vieja.db")
        with sqlite3.connect(ruta) as conn:
            conn.execute(self._DDL_FASE_7)
            conn.execute(
                "INSERT INTO applications (app_id, job_hash, url, title, status, "
                "created_at, stage_changed_at, follow_up_at, notes) "
                "VALUES ('vieja1','h1','https://x.com/1','Backend','postulado',"
                "'2026-09-01','2026-09-01','2026-09-10','nota previa')")
        return ruta

    @staticmethod
    def _columnas(ruta):
        with sqlite3.connect(ruta) as conn:
            return {r[1] for r in conn.execute("PRAGMA table_info(applications)")}

    def test_la_columna_no_existe_antes_y_existe_despues(self, base_vieja):
        assert "next_action" not in self._columnas(base_vieja)
        GemaDatabase(base_vieja)
        assert "next_action" in self._columnas(base_vieja)

    def test_la_fila_vieja_sobrevive_y_su_follow_up_at_ahora_se_lee(self, base_vieja):
        """El dato que nadie leía desde la Fase 7 empieza a avisar sin migrar nada:
        la fecha ya estaba, solo faltaba un lector."""
        d = GemaDatabase(base_vieja)
        fila = d.listar_postulaciones(hoy=HOY)[0]
        assert fila["app_id"] == "vieja1"
        assert fila["notes"] == "nota previa"
        assert fila["next_action"] is None
        assert fila["vencida"] is True
        assert _urls(d.vencidas(hoy=HOY)) == ["https://x.com/1"]

    def test_es_idempotente(self, base_vieja):
        GemaDatabase(base_vieja)
        GemaDatabase(base_vieja)
        assert "next_action" in self._columnas(base_vieja)

    def test_una_base_nueva_ya_trae_la_columna(self, tmp_path):
        ruta = str(tmp_path / "nueva.db")
        GemaDatabase(ruta)
        assert "next_action" in self._columnas(ruta)

    def test_se_puede_escribir_la_accion_sobre_una_base_migrada(self, base_vieja):
        d = GemaDatabase(base_vieja)
        fila = d.editar_proxima_accion("vieja1", next_action="escribir al recruiter")
        assert fila["next_action"] == "escribir al recruiter"


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient con DB temporal, estado temporal y las integraciones apagadas."""
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "api.db"))
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


def _post(client, n, **extra):
    r = client.post("/api/applications", json={"url": f"https://x.com/{n}", **extra})
    assert r.status_code == 201, r.text
    return r.json()


class TestAPI:
    def test_crear_con_accion_y_fecha_pasada_aparece_en_vencidas(self, client):
        a = _post(client, 1, next_action="escribir al recruiter", follow_up_at="2020-01-01")
        assert a["next_action"] == "escribir al recruiter"
        r = client.get("/api/applications", params={"overdue": "true"}).json()
        assert [x["app_id"] for x in r["applications"]] == [a["app_id"]]
        assert r["applications"][0]["vencida"] is True
        assert r["vencidas"] == 1

    def test_fecha_futura_no_aparece(self, client):
        _post(client, 1, next_action="llamar", follow_up_at="2099-01-01")
        r = client.get("/api/applications", params={"overdue": "true"}).json()
        assert r["applications"] == [] and r["vencidas"] == 0

    def test_etapa_cerrada_no_aparece(self, client):
        a = _post(client, 1, next_action="x", follow_up_at="2020-01-01")
        assert client.get("/api/applications").json()["vencidas"] == 1
        r = client.patch(f"/api/applications/{a['app_id']}", json={"status": "rechazado"})
        assert r.status_code == 200
        r = client.get("/api/applications", params={"overdue": "true"}).json()
        assert r["applications"] == [] and r["vencidas"] == 0

    def test_la_lista_normal_trae_todo_con_la_marca_y_el_total(self, client):
        _post(client, 1, next_action="a", follow_up_at="2020-01-01")
        _post(client, 2, next_action="b", follow_up_at="2099-01-01")
        r = client.get("/api/applications").json()
        assert len(r["applications"]) == 2
        assert {x["url"]: x["vencida"] for x in r["applications"]} == {
            "https://x.com/1": True, "https://x.com/2": False}
        assert r["vencidas"] == 1

    def test_el_filtro_de_vencidas_respeta_el_status(self, client):
        _post(client, 1, follow_up_at="2020-01-01")
        r = client.get("/api/applications",
                       params={"overdue": "true", "status": "screening"}).json()
        assert r["applications"] == [] and r["vencidas"] == 1

    def test_editar_la_accion_con_PATCH_sin_mover_de_estado(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}",
                         json={"next_action": "mandar el take-home", "follow_up_at": "2020-02-02"})
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["next_action"], r.json()["follow_up_at"]) == (
            "postulado", "mandar el take-home", "2020-02-02")
        assert client.get("/api/applications").json()["vencidas"] == 1

    def test_null_y_vacio_borran_y_la_ausencia_no_toca(self, client):
        a = _post(client, 1, next_action="llamar", follow_up_at="2020-01-01")
        url = f"/api/applications/{a['app_id']}"
        r = client.patch(url, json={"follow_up_at": None}).json()
        assert (r["next_action"], r["follow_up_at"]) == ("llamar", None)
        r = client.patch(url, json={"next_action": ""}).json()
        assert (r["next_action"], r["follow_up_at"]) == (None, None)

    def test_mover_y_editar_en_el_mismo_PATCH(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}",
                         json={"status": "screening", "next_action": "preparar la tecnica",
                               "follow_up_at": "2099-01-01"})
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["next_action"]) == ("screening", "preparar la tecnica")

    def test_el_PATCH_viejo_de_solo_status_sigue_funcionando(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}", json={"status": "screening"})
        assert r.status_code == 200 and r.json()["status"] == "screening"

    def test_una_fecha_invalida_da_422_y_no_mueve_la_postulacion(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}",
                         json={"status": "screening", "follow_up_at": "mañana"})
        assert r.status_code == 422
        assert client.get("/api/applications").json()["applications"][0]["status"] == "postulado"

    def test_crear_con_fecha_invalida_da_422(self, client):
        r = client.post("/api/applications", json={"url": "https://x.com/1", "follow_up_at": "mañana"})
        assert r.status_code == 422
        assert client.get("/api/applications").json()["applications"] == []

    def test_un_texto_demasiado_largo_da_422(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}", json={"next_action": "x" * 201})
        assert r.status_code == 422

    def test_un_PATCH_vacio_da_422(self, client):
        a = _post(client, 1)
        assert client.patch(f"/api/applications/{a['app_id']}", json={}).status_code == 422

    def test_nota_sin_status_da_422_en_vez_de_perderse(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}",
                         json={"nota": "x", "next_action": "y"})
        assert r.status_code == 422

    def test_PATCH_de_una_inexistente_da_404(self, client):
        r = client.patch("/api/applications/no-existe", json={"next_action": "x"})
        assert r.status_code == 404

    def test_un_movimiento_ilegal_sigue_dando_409(self, client):
        a = _post(client, 1)
        r = client.patch(f"/api/applications/{a['app_id']}", json={"status": "oferta"})
        assert r.status_code == 409


# ── El reporte del cron ──────────────────────────────────────────────────────

def _estado(trigger="cron"):
    return {"trigger": trigger, "tier1": [], "tier2": [], "tier3": [], "tier4": [],
            "errors": []}


def _correr_notify(ctx):
    from graph.run_graph import notify
    notify(_estado(), {"configurable": {"ctx": ctx}})


@pytest.fixture()
def con_webhook(monkeypatch):
    monkeypatch.setattr(config_module, "DISCORD_WEBHOOK_URL", WEBHOOK_DE_PRUEBA)


@pytest.fixture()
def sin_webhook(monkeypatch):
    monkeypatch.setattr(config_module, "DISCORD_WEBHOOK_URL", "")


class TestNotificacionDeVencidas:
    def test_el_mensaje_incluye_las_vencidas(self, tmp_db, con_webhook):
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2020-01-01",
                                 next_action="escribir al recruiter")
        enviados: list[str] = []
        logs: list[str] = []
        _correr_notify(RuntimeContext(db=tmp_db, discord=enviados.append, log=logs.append))

        assert len(enviados) == 1, "las vencidas van en el MISMO mensaje, no en uno aparte"
        msg = enviados[0]
        assert "Tier 1: 0" in msg, "el reporte de tiers sigue intacto"
        assert "1 próxima(s) acción(es) vencida(s)" in msg
        assert "escribir al recruiter" in msg
        assert "venció 2020-01-01" in msg
        assert any("[VENCIDAS]" in l and "incluidas en el reporte" in l for l in logs)

    def test_futura_o_cerrada_no_entra_al_mensaje(self, tmp_db, con_webhook):
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2099-01-01",
                                 next_action="futura")
        c = tmp_db.crear_postulacion("https://x.com/2", follow_up_at="2020-01-01",
                                     next_action="cerrada")
        tmp_db.mover_postulacion(c["app_id"], app.RECHAZADO)
        enviados: list[str] = []
        _correr_notify(RuntimeContext(db=tmp_db, discord=enviados.append))
        assert "vencida" not in enviados[0]
        assert "futura" not in enviados[0] and "cerrada" not in enviados[0]

    def test_sin_vencidas_el_reporte_no_cambia(self, tmp_db, con_webhook):
        enviados: list[str] = []
        _correr_notify(RuntimeContext(db=tmp_db, discord=enviados.append))
        assert enviados[0].endswith("- Tier 4: 0")

    def test_lista_cinco_y_cuenta_el_resto(self, tmp_db, con_webhook):
        for n in range(7):
            tmp_db.crear_postulacion(f"https://x.com/{n}", follow_up_at="2020-01-01",
                                     next_action=f"accion {n}")
        enviados: list[str] = []
        _correr_notify(RuntimeContext(db=tmp_db, discord=enviados.append))
        msg = enviados[0]
        assert "7 próxima(s) acción(es) vencida(s)" in msg
        assert msg.count("venció 2020-01-01") == 5
        assert "… y 2 más" in msg
        assert len(msg) < 2000

    def test_una_accion_sin_texto_igual_se_avisa(self, tmp_db, con_webhook):
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2020-01-01")
        enviados: list[str] = []
        _correr_notify(RuntimeContext(db=tmp_db, discord=enviados.append))
        assert "sin texto" in enviados[0]

    def test_por_el_camino_real_el_POST_a_discord_lleva_las_vencidas(
            self, tmp_db, con_webhook, monkeypatch):
        """Mockeado el último eslabón (urlopen), no el nuestro: lo que se verifica es el
        cuerpo que Discord recibiría de verdad."""
        from integrations import webhook_client

        capturados = []

        class _Respuesta:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _urlopen(req, timeout=None):
            capturados.append((req.full_url, json.loads(req.data.decode("utf-8"))))
            return _Respuesta()

        monkeypatch.setattr(webhook_client.urllib.request, "urlopen", _urlopen)
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2020-01-01",
                                 next_action="escribir al recruiter")
        _correr_notify(RuntimeContext(db=tmp_db, discord=webhook_client.send_discord_alert))

        assert len(capturados) == 1
        url, cuerpo = capturados[0]
        assert url == WEBHOOK_DE_PRUEBA
        assert "escribir al recruiter" in cuerpo["content"]

    def test_sin_webhook_no_falla_y_lo_dice_en_el_log(self, tmp_db, sin_webhook, monkeypatch,
                                                      caplog):
        """Lo dice en las dos partes: el stream del run (`ctx.emit`) y el log del proceso.
        El cron pone `ctx.log = queue.put` y esa cola no la lee nadie."""
        from integrations import webhook_client

        def _no_deberia_salir(*a, **k):
            raise AssertionError("sin webhook no puede haber un POST")

        monkeypatch.setattr(webhook_client.urllib.request, "urlopen", _no_deberia_salir)
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2020-01-01",
                                 next_action="x")
        logs: list[str] = []
        with caplog.at_level("WARNING", logger="gema.run_graph"):
            _correr_notify(RuntimeContext(db=tmp_db, discord=webhook_client.send_discord_alert,
                                          log=logs.append))
        avisos = [l for l in logs if "[VENCIDAS]" in l]
        assert len(avisos) == 1
        assert "DISCORD_WEBHOOK_URL" in avisos[0] and "no se avisa" in avisos[0]
        assert any(r.name == "gema.run_graph" and "DISCORD_WEBHOOK_URL" in r.message
                   for r in caplog.records)

    def test_sin_callable_de_discord_tambien_lo_dice(self, tmp_db, con_webhook):
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2020-01-01",
                                 next_action="x")
        logs: list[str] = []
        _correr_notify(RuntimeContext(db=tmp_db, discord=None, log=logs.append))
        assert any("[VENCIDAS]" in l and "no se avisa" in l for l in logs)

    def test_si_la_consulta_falla_el_reporte_de_tiers_sale_igual(self, con_webhook):
        class _DbRota:
            def vencidas(self):
                raise sqlite3.OperationalError("database is locked")

        enviados: list[str] = []
        logs: list[str] = []
        _correr_notify(RuntimeContext(db=_DbRota(), discord=enviados.append, log=logs.append))
        assert enviados and "Tier 1: 0" in enviados[0]
        assert "vencida" not in enviados[0]
        assert any("[VENCIDAS]" in l and "no se pudieron consultar" in l for l in logs)

    def test_sin_db_en_el_ctx_no_hay_seccion(self, con_webhook):
        enviados: list[str] = []
        _correr_notify(RuntimeContext(db=None, discord=enviados.append))
        assert "vencida" not in enviados[0]

    def test_una_corrida_completa_del_cron_las_avisa(self, tmp_db, con_webhook, monkeypatch):
        """De punta a punta por el grafo real: init_run → ... → notify."""
        from graph.run_graph import run_pipeline
        from models import ScrapeRunSummary, SearchConfig

        monkeypatch.setattr("scraper.run_scrape_session",
                            lambda *a, **k: ([], ScrapeRunSummary()))
        tmp_db.crear_postulacion("https://x.com/1", follow_up_at="2020-01-01",
                                 next_action="escribir al recruiter")
        enviados: list[str] = []
        cfg = SearchConfig(target_titles=["Backend Engineer"], must_include=[],
                           must_exclude=[], min_salary=None, target_domains=[])
        ctx = RuntimeContext(db=tmp_db, notion=None, sheets=None, discord=enviados.append)
        run_pipeline(config=cfg, ctx=ctx, trigger="cron", ttl_hours=48)

        assert len(enviados) == 1
        assert "📅 Scheduled run" in enviados[0]
        assert "escribir al recruiter" in enviados[0]
