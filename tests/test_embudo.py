"""
tests/test_embudo.py — Fase 5 del : embudo por fuente y título, y poda.

El riesgo de esta herramienta no es proponer de menos: es proponer de más. Un título que se
saca de la búsqueda no vuelve solo, así que cada regla de abajo existe para impedir que un
mal día de red, una traza sin medir o un board de feed fijo se lea como "este título no
produce":

- NULL no es cero.
- Una fuente que no trajo nada para NINGÚN título ese día no le carga ramas vacías a ninguno.
- Un board de feed fijo ignora el título: lo que trae no habla de él.
- Una sola corrida medida es una muestra, no un patrón.
- La herramienta no escribe: ni la base, ni la configuración.
"""

import json
import sqlite3

import pytest

import embudo_fuentes
from embudo_fuentes import (VEREDICTO_INSUFICIENTE, VEREDICTO_NUNCA, VEREDICTO_PRODUCE,
                            VEREDICTO_SIN_MEDIR)
from tools import embudo as cli


def _t(n: int) -> str:
    return f"2026-09-{n:02d}T10:00:00+00:00"


def _srp(run, n, domain, title, items, ms=1000.0, status="ok"):
    return {"run_id": run, "node": "scrape_srp", "graph": "run", "domain": domain,
            "title": title, "started_at": _t(n), "exec_ms": ms, "queue_wait_ms": 0.0,
            "status": status, "items": items, "aceptados": 0}


def _feed(dominio):
    return dominio == "feed.com"


def _calc(db, **kw):
    return embudo_fuentes.embudo_desde_db(db.db_path, es_feed_fijo=_feed, **kw)


def _titulos(r):
    return {t["titulo"]: t for t in r["titulos"]}


@pytest.fixture
def base(tmp_db):
    """Tres corridas medidas, dos boards CSS, un feed fijo, y cuatro títulos."""
    filas = []
    for n, run in ((1, "r1"), (2, "r2"), (3, "r3")):
        for board in ("a.com", "b.com"):
            filas += [
                _srp(run, n, board, "python", 12 if board == "a.com" else 4),   # produce
                _srp(run, n, board, "cobol", 0, ms=2000.0),                      # nunca
                _srp(run, n, board, "fortran", 0),                               # nunca
                _srp(run, n, board, "viejo", None),                              # sin medir
            ]
        # El feed fijo trae avisos con cualquier titulo: no debe salvar a nadie.
        filas.append(_srp(run, n, "feed.com", "cobol", 30))
    tmp_db.record_node_traces_batch(filas)
    return tmp_db


class TestVeredictos:

    def test_el_titulo_que_nunca_produce_se_propone_con_su_ahorro(self, base):
        r = _calc(base, ventana=5, min_corridas=2)
        cobol = _titulos(r)["cobol"]
        assert cobol["veredicto"] == VEREDICTO_NUNCA
        assert cobol["items_total"] == 0
        # a.com y b.com, una rama cada uno por corrida
        assert cobol["ramas_por_corrida"] == 2.0
        assert cobol["segundos_por_corrida"] == 4.0          # 2 ramas x 2000 ms
        prop = {p["titulo"]: p for p in r["propuestas"]}
        assert prop["cobol"]["ramas_ahorradas_por_corrida"] == 2.0
        assert "ninguna fuente" in prop["cobol"]["evidencia"]

    def test_un_titulo_que_produce_no_se_propone(self, base):
        r = _calc(base, ventana=5, min_corridas=2)
        py = _titulos(r)["python"]
        assert py["veredicto"] == VEREDICTO_PRODUCE
        assert py["fuentes_con_items"] == ["a.com", "b.com"]
        assert "python" not in {p["titulo"] for p in r["propuestas"]}

    def test_el_feed_fijo_no_cuenta_a_favor_de_ningun_titulo(self, base):
        # feed.com trae 30 avisos con el titulo "cobol" en las tres corridas.
        r = _calc(base, ventana=5, min_corridas=2)
        assert _titulos(r)["cobol"]["items_total"] == 0
        assert _titulos(r)["cobol"]["veredicto"] == VEREDICTO_NUNCA

    def test_null_no_es_cero_el_titulo_sin_medir_no_se_propone(self, base):
        r = _calc(base, ventana=5, min_corridas=2)
        viejo = _titulos(r)["viejo"]
        assert viejo["veredicto"] == VEREDICTO_SIN_MEDIR
        assert viejo["ramas_sin_medir"] == 6
        assert "viejo" not in {p["titulo"] for p in r["propuestas"]}

    def test_una_sola_corrida_medida_es_evidencia_insuficiente(self, base):
        r = _calc(base, ventana=1, min_corridas=2)       # solo r3
        assert _titulos(r)["cobol"]["veredicto"] == VEREDICTO_INSUFICIENTE
        assert r["propuestas"] == []

    def test_el_minimo_de_corridas_es_configurable(self, base):
        assert _calc(base, ventana=1, min_corridas=1)["propuestas"] != []


class TestFuenteCaida:

    def test_una_fuente_que_no_trajo_nada_para_ningun_titulo_no_le_carga_ramas_vacias(self, tmp_db):
        # python produce en a.com. En r2 a.com cae a cero para TODO: no es culpa de "cobol".
        filas = [
            _srp("r1", 1, "a.com", "python", 9), _srp("r1", 1, "a.com", "cobol", 0),
            _srp("r2", 2, "a.com", "python", 0), _srp("r2", 2, "a.com", "cobol", 0),
        ]
        tmp_db.record_node_traces_batch(filas)
        r = _calc(tmp_db, ventana=5, min_corridas=2)
        cobol = _titulos(r)["cobol"]
        assert cobol["ramas_excluidas_por_fuente_caida"] == 1     # la de r2
        assert cobol["corridas_medidas"] == 1                     # solo r1 cuenta
        assert cobol["veredicto"] == VEREDICTO_INSUFICIENTE       # y 1 corrida no alcanza

    def test_un_mal_dia_de_red_no_propone_podar_nada(self, tmp_db):
        # Todos los boards en cero en las dos corridas: ningun titulo es culpable.
        filas = [_srp(run, n, b, t, 0)
                 for run, n in (("r1", 1), ("r2", 2))
                 for b in ("a.com", "b.com") for t in ("python", "cobol")]
        tmp_db.record_node_traces_batch(filas)
        r = _calc(tmp_db, ventana=5, min_corridas=2)
        assert r["propuestas"] == []
        assert {t["veredicto"] for t in r["titulos"]} == {VEREDICTO_SIN_MEDIR}


class TestBaseSinMedir:

    def test_una_base_anterior_a_la_fase_4_lo_dice_y_no_propone_nada(self, tmp_path):
        path = str(tmp_path / "vieja.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE node_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node TEXT NOT NULL, graph TEXT,
            domain TEXT, title TEXT, started_at TEXT, exec_ms REAL, queue_wait_ms REAL,
            status TEXT, error TEXT)""")
        for i, t in enumerate(("python", "cobol", "fortran")):
            conn.execute("INSERT INTO node_traces (run_id,node,domain,title,started_at,exec_ms,"
                         "status) VALUES ('r0','scrape_srp','a.com',?,?,10,'ok')", (t, _t(1 + i)))
        conn.commit()
        conn.close()

        r = embudo_fuentes.embudo_desde_db(path, ventana=5, min_corridas=1)
        assert r["lectura"]["tiene_items"] is False
        assert r["cobertura"]["ramas"] == 3 and r["cobertura"]["ramas_medidas"] == 0
        assert r["propuestas"] == []
        texto = cli.formatear(r)
        assert "NADA QUE PROPONER" in texto
        assert "SIN MEDIR, items NULL: 3" in texto
        assert "anterior a la Fase 4" in texto

    def test_la_herramienta_no_modifica_la_base(self, base):
        antes = open(base.db_path, "rb").read()
        _calc(base, ventana=5, min_corridas=2)
        assert open(base.db_path, "rb").read() == antes

    def test_no_migra_una_base_vieja(self, tmp_path):
        path = str(tmp_path / "vieja.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE node_traces (id INTEGER PRIMARY KEY, run_id TEXT, "
                     "node TEXT, domain TEXT, title TEXT, started_at TEXT, exec_ms REAL, "
                     "status TEXT)")
        conn.commit()
        conn.close()
        embudo_fuentes.embudo_desde_db(path, ventana=5, min_corridas=1)
        conn = sqlite3.connect(path)
        assert "items" not in [r[1] for r in conn.execute("PRAGMA table_info(node_traces)")]
        conn.close()


class TestFuentes:

    def test_el_rendimiento_por_fuente_agrega_css_endpoint_y_ats(self, base):
        base.record_node_traces_batch([
            {"run_id": "r3", "node": "fuente_endpoint", "domain": "ep.com", "started_at": _t(3),
             "exec_ms": 5.0, "status": "ok", "items": 99, "aceptados": 4},
            {"run_id": "r3", "node": "fuente_ats", "domain": "greenhouse:gitlab",
             "started_at": _t(3), "exec_ms": 5.0, "status": "ok", "items": 0, "aceptados": 0},
            {"run_id": "r2", "node": "fuente_ats", "domain": "greenhouse:gitlab",
             "started_at": _t(2), "exec_ms": 5.0, "status": "ok", "items": 50, "aceptados": 2},
        ])
        r = _calc(base, ventana=5, min_corridas=2)
        f = {x["fuente"]: x for x in r["fuentes"]}
        assert f["a.com"]["tipo"] == "css" and f["a.com"]["items_total"] == 36   # 12 x 3 corridas
        assert f["ep.com"]["tipo"] == "endpoint" and f["ep.com"]["items_total"] == 99
        assert f["greenhouse:gitlab"]["estado"] == "sospechosa"
        assert f["a.com"]["ramas_medidas"] == 9 and f["a.com"]["ramas_vacias"] == 6

    def test_las_corridas_sin_medir_de_una_fuente_se_dicen(self, tmp_db):
        tmp_db.record_node_traces_batch([_srp("r1", 1, "a.com", "t", None),
                                         _srp("r2", 2, "a.com", "t", 5)])
        f = _calc(tmp_db, ventana=5, min_corridas=1)["fuentes"][0]
        assert f["corridas"] == 2 and f["corridas_sin_medir"] == 1
        assert f["items_total"] == 5

    def test_la_ventana_recorta_las_corridas(self, base):
        r = _calc(base, ventana=2, min_corridas=2)
        assert r["corridas"] == ["r3", "r2"]
        assert r["cobertura"]["corridas"] == 2


class TestCli:

    def test_json_es_la_misma_informacion(self, base, capsys):
        assert cli.main(["--db", base.db_path, "--json", "--corridas", "5",
                         "--min-corridas", "2"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert {"titulos", "fuentes", "propuestas", "cobertura"} <= set(data)
        assert data["propuestas"]

    def test_la_salida_legible_dice_que_no_borra_y_cuanto_ahorra(self, base, capsys,
                                                                  monkeypatch):
        # El CLI usa la deteccion real de feed fijo (selectors_registry), no la inyectada
        # de los otros tests. Sin esto, feed.com no es feed fijo y sus 30 avisos de "cobol"
        # lo salvan de la propuesta.
        import selectors_registry

        class _Feed:
            feed_fijo = True
        original = selectors_registry.get_selectors
        monkeypatch.setattr(selectors_registry, "get_selectors",
                            lambda d: _Feed() if d == "feed.com" else original(d))
        assert cli.main(["--db", base.db_path, "--corridas", "5", "--min-corridas", "2"]) == 0
        out = capsys.readouterr().out
        assert "decide el usuario; esta herramienta no borra nada" in out
        assert "cobol: ahorra 2.0 ramas CSS por corrida" in out
        assert "NUNCA PRODUCE" in out

    def test_base_inexistente_sale_con_2(self, tmp_path, capsys):
        assert cli.main(["--db", str(tmp_path / "nada.db")]) == 2


class TestApi:
    """
    `EmbudoSection.tsx` lee de `GET /api/embudo`. El endpoint faltaba cuando se integró la
    fase (2026-10-03): la sección de estadísticas respondía 404 en cada apertura, un
    control visible que no funcionaba.
    """

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import api as api_module
        import config as config_module
        import run_manager as run_manager_module

        monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "test_registry.db"))
        monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
        monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
        with TestClient(api_module.app) as c:
            api_module.app.state.notion = None
            api_module.app.state.sheets = None
            yield c, api_module.app.state.db

    def test_base_sin_trazas_responde_vacio_y_no_propone(self, client):
        c, _ = client
        r = c.get("/api/embudo")
        assert r.status_code == 200
        data = r.json()
        assert {"titulos", "fuentes", "propuestas", "cobertura", "ventana_corridas",
                "ahorro_si_se_aplican_todas"} <= set(data)
        assert data["propuestas"] == []

    def test_devuelve_lo_mismo_que_la_herramienta(self, client):
        c, db = client
        filas = []
        for n, run in ((1, "r1"), (2, "r2"), (3, "r3"), (4, "r4"), (5, "r5")):
            filas += [_srp(run, n, "a.com", "python", 7), _srp(run, n, "a.com", "cobol", 0)]
        db.record_node_traces_batch(filas)
        data = c.get("/api/embudo").json()
        directo = embudo_fuentes.embudo_desde_db(db.db_path)
        assert data["propuestas"] == directo["propuestas"]
        assert {t["titulo"] for t in data["titulos"]} == {"python", "cobol"}
