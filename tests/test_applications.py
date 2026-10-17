"""
tests/test_applications.py — ciclo de vida de la postulación (Fase 7).

GEMA terminaba en «encontrado y empujado a Notion». Esta fase agrega la mitad que
faltaba: qué pasó después. Y con eso, la única métrica que puede decir si el score
sirve — hoy el matcher se calibra a ojo.
"""

import pytest

import applications as app
from database import GemaDatabase
from models import JobResult


@pytest.fixture()
def db(tmp_path):
    d = GemaDatabase(str(tmp_path / "t.db"))
    d.mark_seen_batch([
        JobResult(title="Backend Engineer", company="GitLab",
                  url="https://x.com/1", source_domain="boards.greenhouse.io"),
        JobResult(title="Data Engineer", company="Supabase",
                  url="https://x.com/2", source_domain="jobs.ashbyhq.com"),
    ])
    d.update_tiers_batch([
        ("https://x.com/1", "Tier 1", 92, None, None),
        ("https://x.com/2", "Tier 2", 61, None, None),
    ])
    return d


# ── El dominio, sin base de datos ────────────────────────────────────────────

class TestTransiciones:
    def test_se_avanza_de_a_un_paso(self):
        """Saltar etapas hace que la métrica de conversión mienta: si un aviso
        figura en `oferta` sin haber pasado por `tecnica`, no se puede saber en qué
        etapa se cae el embudo — que es lo único para lo que sirve un embudo."""
        app.validar_transicion(app.POSTULADO, app.SCREENING)
        with pytest.raises(app.TransicionInvalida):
            app.validar_transicion(app.POSTULADO, app.OFERTA)

    @pytest.mark.parametrize("desde", list(app.CADENA))
    def test_se_puede_cerrar_desde_cualquier_etapa(self, desde):
        """Un rechazo llega cuando llega. Obligar a pasar por los estados
        intermedios para registrarlo haría que nadie lo registre."""
        for terminal in app.TERMINALES:
            app.validar_transicion(desde, terminal)

    @pytest.mark.parametrize("terminal", sorted(app.TERMINALES))
    def test_de_un_terminal_no_se_sale(self, terminal):
        """Una postulación rechazada que vuelve a `screening` es una postulación
        nueva, no la misma con otro estado. Si no, el histórico deja de serlo."""
        assert app.transiciones_validas(terminal) == frozenset()
        with pytest.raises(app.TransicionInvalida, match="terminal"):
            app.validar_transicion(terminal, app.SCREENING)

    def test_no_se_puede_ir_al_mismo_estado(self):
        with pytest.raises(app.TransicionInvalida):
            app.validar_transicion(app.SCREENING, app.SCREENING)

    def test_un_estado_inventado_se_rechaza(self):
        with pytest.raises(app.TransicionInvalida, match="desconocido"):
            app.validar_transicion(app.POSTULADO, "entrevistando")

    def test_el_error_dice_a_donde_SI_se_puede_ir(self):
        """Un mensaje que solo dice «no se puede» manda a leer el código."""
        with pytest.raises(app.TransicionInvalida, match="screening"):
            app.validar_transicion(app.POSTULADO, app.FINAL)

    def test_la_cadena_no_tiene_terminales_adentro(self):
        assert not (set(app.CADENA) & app.TERMINALES)

    def test_convirtio_empieza_en_screening(self):
        """Un `postulado` sin respuesta no dice nada sobre el aviso, solo sobre
        cuántos candidatos había. Un `screening` sí."""
        assert app.POSTULADO not in app.CONVIRTIO
        assert app.SCREENING in app.CONVIRTIO
        assert app.ACEPTADO in app.CONVIRTIO


# ── Persistencia ─────────────────────────────────────────────────────────────

class TestCrear:
    def test_copia_el_tier_y_el_score_del_momento(self, db):
        """El matcher cambia —las Fases 3 y 4 ya lo cambiaron dos veces— y la
        pregunta es «el score que yo vi cuando apliqué, ¿predijo algo?». Leerlo
        después contestaría otra cosa."""
        a = db.crear_postulacion("https://x.com/1")
        assert a["tier_al_aplicar"] == "Tier 1"
        assert a["score_al_aplicar"] == 92

    def test_es_idempotente_por_aviso(self, db):
        """Postularse dos veces al mismo puesto es un error de la persona, y la
        base no debería ayudarlo a pasar desapercibido."""
        a = db.crear_postulacion("https://x.com/1")
        b = db.crear_postulacion("https://x.com/1")
        assert a["app_id"] == b["app_id"]
        assert len(db.listar_postulaciones()) == 1

    def test_un_aviso_que_no_esta_en_el_registry_igual_se_puede_registrar(self, db):
        """Marcar una postulación de un aviso que llegó por fuera no puede fallar
        — perder el registro es peor que perder el tier."""
        a = db.crear_postulacion("https://otro.com/9")
        assert a["app_id"]
        assert a["tier_al_aplicar"] is None

    def test_deja_el_primer_evento_en_el_historial(self, db):
        a = db.crear_postulacion("https://x.com/1", status=app.POSTULADO)
        h = db.historial_postulacion(a["app_id"])
        assert len(h) == 1
        assert h[0]["desde"] is None and h[0]["hasta"] == app.POSTULADO

    def test_un_borrador_no_tiene_fecha_de_postulacion(self, db):
        a = db.crear_postulacion("https://x.com/1", status=app.BORRADOR)
        assert a["applied_at"] is None

    def test_un_estado_invalido_al_crear_se_rechaza(self, db):
        with pytest.raises(app.TransicionInvalida):
            db.crear_postulacion("https://x.com/1", status="inventado")


class TestMover:
    def test_avanza_y_registra_el_evento(self, db):
        a = db.crear_postulacion("https://x.com/1")
        a = db.mover_postulacion(a["app_id"], app.SCREENING, nota="llamada 20min")
        assert a["status"] == app.SCREENING
        h = db.historial_postulacion(a["app_id"])
        assert (h[-1]["desde"], h[-1]["hasta"]) == (app.POSTULADO, app.SCREENING)
        assert h[-1]["nota"] == "llamada 20min"

    def test_un_salto_de_etapa_se_rechaza_y_no_deja_rastro(self, db):
        a = db.crear_postulacion("https://x.com/1")
        with pytest.raises(app.TransicionInvalida):
            db.mover_postulacion(a["app_id"], app.OFERTA)
        assert db.listar_postulaciones()[0]["status"] == app.POSTULADO
        assert len(db.historial_postulacion(a["app_id"])) == 1

    def test_cerrar_marca_la_fecha_de_cierre(self, db):
        a = db.crear_postulacion("https://x.com/1")
        a = db.mover_postulacion(a["app_id"], app.RECHAZADO)
        assert a["closed_at"]

    def test_una_oferta_guarda_el_monto(self, db):
        a = db.crear_postulacion("https://x.com/1")
        for paso in (app.SCREENING, app.TECNICA, app.FINAL, app.OFERTA):
            a = db.mover_postulacion(a["app_id"], paso, salary_offered=140000
                                     if paso == app.OFERTA else None)
        assert a["salary_offered"] == 140000

    def test_mover_una_que_no_existe_lanza_KeyError(self, db):
        with pytest.raises(KeyError):
            db.mover_postulacion("no-existe", app.SCREENING)


class TestConversion:
    def _recorrido(self, db, url, pasos):
        a = db.crear_postulacion(url)
        for p in pasos:
            a = db.mover_postulacion(a["app_id"], p)
        return a

    def test_un_rechazo_despues_de_una_tecnica_CUENTA_como_conversion(self, db):
        """Se mide sobre el HISTORIAL, no sobre el estado actual. Mirar solo el
        estado final contaría eso como un fracaso del matcher, y es al revés: el
        aviso valía la pena aunque no saliera."""
        self._recorrido(db, "https://x.com/1",
                        [app.SCREENING, app.TECNICA, app.RECHAZADO])
        g = db.conversion_por("tier_al_aplicar")
        assert g[0]["convirtieron"] == 1

    def test_un_rechazo_sin_llegar_a_screening_NO_cuenta(self, db):
        self._recorrido(db, "https://x.com/1", [app.RECHAZADO])
        assert db.conversion_por("tier_al_aplicar")[0]["convirtieron"] == 0

    def test_agrupa_por_tier(self, db):
        self._recorrido(db, "https://x.com/1", [app.SCREENING])
        self._recorrido(db, "https://x.com/2", [app.SIN_RESPUESTA])
        por = {g["grupo"]: g for g in db.conversion_por("tier_al_aplicar")}
        assert por["Tier 1"]["tasa"] == 1.0
        assert por["Tier 2"]["tasa"] == 0.0

    def test_agrupa_por_board(self, db):
        self._recorrido(db, "https://x.com/1", [app.SCREENING])
        grupos = {g["grupo"] for g in db.conversion_por("source_domain")}
        assert "boards.greenhouse.io" in grupos

    def test_agrupa_por_rango_de_score(self, db):
        self._recorrido(db, "https://x.com/1", [app.SCREENING])
        grupos = {g["grupo"] for g in db.conversion_por("rango_score")}
        assert "90-99" in grupos

    def test_una_dimension_invalida_se_rechaza(self, db):
        with pytest.raises(ValueError):
            db.conversion_por("lo_que_sea")

    def test_sin_postulaciones_devuelve_lista_vacia(self, db):
        assert db.conversion_por() == []
