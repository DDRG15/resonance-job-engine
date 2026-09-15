"""
tests/test_endpoint_registry.py — la config de endpoints ship-ea, el estado local no.

Fase 1. Cero red: solo se verifica el cableado entre `endpoint_registry` (commiteado),
`BoardStrategyRegistry` (estado local, gitignoreado) y `active_domains()`.
"""

import pytest

import endpoint_registry
import selectors_registry
from board_strategy import (
    AccessVector, BoardStrategy, BoardStrategyRegistry, ExtractionMode, Health,
)


class TestRegistroCommiteado:
    def test_cada_entrada_tiene_lo_minimo_para_funcionar(self):
        """Una entrada sin template o sin mapeo de título y URL es una entrada que
        va a fallar en la primera corrida, no en el arranque."""
        for dominio, cfg in endpoint_registry.ENDPOINTS.items():
            assert cfg.get("endpoint_template"), f"{dominio} sin endpoint_template"
            fm = cfg.get("field_map") or {}
            assert fm.get("title"), f"{dominio} sin mapeo de title"
            assert fm.get("url"), f"{dominio} sin mapeo de url"

    def test_formato_declarado_es_uno_de_los_soportados(self):
        for dominio, cfg in endpoint_registry.ENDPOINTS.items():
            assert cfg.get("response_format", "json") in ("json", "rss"), dominio

    def test_los_campos_existen_en_BoardStrategy(self):
        """Un campo mal escrito acá se ignoraría en silencio al aplicarse."""
        for dominio, cfg in endpoint_registry.ENDPOINTS.items():
            for campo in cfg:
                assert campo in BoardStrategy.model_fields, f"{dominio}: {campo!r}"

    def test_el_template_es_https(self):
        for dominio, cfg in endpoint_registry.ENDPOINTS.items():
            assert cfg["endpoint_template"].startswith("https://"), dominio

    def test_para_devuelve_none_para_un_board_sin_endpoint(self):
        assert endpoint_registry.para("jobspresso.co") is None


class TestAplicacionEnMemoria:
    def test_un_registry_vacio_ya_trae_los_endpoints(self, tmp_path):
        reg = BoardStrategyRegistry(tmp_path / "s.json")
        strat = reg.get("remoteok.com")
        assert strat.access_vector == AccessVector.ENDPOINT
        assert strat.extraction_mode == ExtractionMode.STRUCTURED
        assert strat.endpoint_template == "https://remoteok.com/api"

    def test_no_escribe_disco_al_cargar(self, tmp_path):
        """Un `_load` que persiste convierte cada arranque en una escritura."""
        path = tmp_path / "s.json"
        BoardStrategyRegistry(path)
        assert not path.exists()

    def test_pisa_el_vector_guardado_en_el_json_local(self, tmp_path):
        """El JSON puede decir `direct` porque se escribió antes de que el board
        tuviera endpoint. El archivo commiteado es la fuente de verdad sobre la
        capacidad; el local, sobre el estado."""
        path = tmp_path / "s.json"
        path.write_text(
            '{"remoteok.com": {"domain": "remoteok.com", "access_vector": "direct",'
            ' "extraction_mode": "css", "health": "degraded", "last_updated": 1.0}}',
            encoding="utf-8")
        reg = BoardStrategyRegistry(path)
        strat = reg.get("remoteok.com")
        assert strat.access_vector == AccessVector.ENDPOINT
        # ...pero el estado local sobrevive: la salud la escribió una corrida real.
        assert strat.health == Health.DEGRADED

    def test_un_board_sin_endpoint_no_se_toca(self, tmp_path):
        reg = BoardStrategyRegistry(tmp_path / "s.json")
        assert reg.get("jobspresso.co").access_vector == AccessVector.DIRECT
        assert reg.get("jobspresso.co").endpoint_template is None

    def test_weworkremotely_trae_el_flag_de_empresa_en_el_titulo(self, tmp_path):
        """Su RSS no tiene campo de empresa: la mete en el título, 87 de 87 items."""
        reg = BoardStrategyRegistry(tmp_path / "s.json")
        assert reg.get("weworkremotely.com").titulo_trae_empresa is True


class TestDominiosActivos:
    def test_los_boards_con_endpoint_entran_aunque_esten_retirados(self):
        """`active=False` significa «sus selectores CSS no sirven». Un board leído
        por endpoint no usa selectores, así que el flag que lo apagaba no aplica.
        weworkremotely.com se retiró el 07-11 por HTTP 403 contra el navegador."""
        activos = selectors_registry.active_domains()
        assert selectors_registry.SELECTORS["weworkremotely.com"].active is False
        assert "weworkremotely.com" in activos

    def test_no_duplica_un_board_que_ya_estaba_activo(self):
        activos = selectors_registry.active_domains()
        assert activos.count("remoteok.com") == 1

    def test_sigue_excluyendo_a_los_retirados_sin_endpoint(self):
        activos = selectors_registry.active_domains()
        for d in ("wellfound.com", "welcometothejungle.com", "remote.co"):
            assert d not in activos


class TestProbeNoDegradaEndpoint:
    def test_el_probe_no_puede_bajar_un_board_de_endpoint_a_direct(self):
        """El probe usa un navegador y no puede saber nada útil sobre un board que
        se lee con un GET. Hoy no los ve —se apartan antes— pero dejarlo fuera sería
        confiar en que ese orden no cambie nunca."""
        from scraper import _resolve_vector
        assert _resolve_vector(AccessVector.ENDPOINT, AccessVector.DIRECT) == \
            AccessVector.ENDPOINT

    def test_pero_si_puede_subirlo_a_camoufox(self):
        from scraper import _resolve_vector
        assert _resolve_vector(AccessVector.ENDPOINT, AccessVector.CAMOUFOX) == \
            AccessVector.CAMOUFOX
