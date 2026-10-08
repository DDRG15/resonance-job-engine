"""
tests/test_setup_wizard.py — `needs_setup()` y `_save_keys()` sin recargar `config`.

Lo que se fija:

1. `needs_setup()` cuenta las SIETE keys de la cadena LLM. Contaba cuatro, y a quien
   tenía solo Mistral (primario de `parse`), Cerebras o SambaNova le pedía el wizard.
2. Ninguna de las dos funciones recarga `config`. La recarga pisaba con el .env todo
   valor ya cargado en el proceso, y el .env de el usuario define GEMA_DB_PATH: después de un
   GET /api/status, un `GemaDatabase()` nuevo apuntaba a la base real (hallazgo 03-10).
   El conftest lo tapaba en la suite; desde el 08-10 el parche no existe y estos tests
   son la garantía.
3. `_save_keys` aplica al proceso solo lo que guarda, e invalida los clientes LLM que
   nlp_engine cachea con la key vieja.
"""

import importlib

import pytest

import config
import setup_wizard
from setup_wizard import LLM_KEY_NAMES


@pytest.fixture
def sin_keys(monkeypatch):
    """Las siete keys vacías en `config`; monkeypatch las restaura al salir."""
    for k in LLM_KEY_NAMES:
        monkeypatch.setattr(config, k, "")


@pytest.fixture
def recarga_prohibida(monkeypatch):
    """Si algo recarga un módulo, el test falla en el acto."""
    def _no(m):
        raise AssertionError(f"importlib.reload({getattr(m, '__name__', m)}) prohibido")
    monkeypatch.setattr(importlib, "reload", _no)


class TestNeedsSetup:

    def test_sin_ninguna_key_pide_el_wizard(self, sin_keys, recarga_prohibida):
        assert setup_wizard.needs_setup() is True

    @pytest.mark.parametrize("key", LLM_KEY_NAMES)
    def test_cualquiera_de_las_siete_alcanza(self, sin_keys, recarga_prohibida,
                                             monkeypatch, key):
        monkeypatch.setattr(config, key, "una-key")
        assert setup_wizard.needs_setup() is False

    def test_solo_espacios_no_cuenta(self, sin_keys, recarga_prohibida, monkeypatch):
        monkeypatch.setattr(config, "GROQ_API_KEY", "   ")
        assert setup_wizard.needs_setup() is True

    def test_no_pisa_valores_ya_cargados_en_config(self, sin_keys, monkeypatch):
        # La forma exacta del hallazgo del 03-10: un valor en memoria distinto del .env.
        monkeypatch.setattr(config, "GEMA_DB_PATH", "/tmp/base-de-test.db")
        setup_wizard.needs_setup()
        assert config.GEMA_DB_PATH == "/tmp/base-de-test.db"

    def test_las_siete_son_las_de_config(self):
        # Una key nueva en config.py que no entre acá es un proveedor invisible al wizard.
        en_config = {n for n in dir(config)
                     if n.endswith("_API_KEY") and n != "NOTION_API_KEY"}
        assert en_config == set(LLM_KEY_NAMES)


class TestSaveKeys:

    @pytest.fixture
    def env_temporal(self, tmp_path, monkeypatch):
        archivo = tmp_path / ".env"
        archivo.write_text("GEMA_DB_PATH=otra.db\n", encoding="utf-8")
        monkeypatch.setattr(setup_wizard, "_ENV_PATH", archivo)
        for k in (*LLM_KEY_NAMES, "DISCORD_WEBHOOK_URL"):
            monkeypatch.setenv(k, "")                        # monkeypatch restaura os.environ
            monkeypatch.setattr(config, k, "")
        return archivo

    def _guardar(self, **k):
        base = dict(groq_key="", gemini_key="", openrouter_key="", cohere_key="",
                    discord_url="")
        base.update(k)
        setup_wizard._save_keys(**base)

    def test_escribe_al_env_y_aplica_al_proceso(self, env_temporal, recarga_prohibida):
        import os
        self._guardar(cohere_key="co-123", discord_url="https://discord/hook")
        texto = env_temporal.read_text(encoding="utf-8")
        assert "COHERE_API_KEY='co-123'" in texto or "COHERE_API_KEY=co-123" in texto
        assert config.COHERE_API_KEY == "co-123" and os.environ["COHERE_API_KEY"] == "co-123"
        assert config.DISCORD_WEBHOOK_URL == "https://discord/hook"
        assert setup_wizard.needs_setup() is False

    def test_no_pisa_lo_que_no_guarda(self, env_temporal, recarga_prohibida, monkeypatch):
        # El .env temporal dice GEMA_DB_PATH=otra.db; el proceso tiene otro valor y lo conserva.
        monkeypatch.setattr(config, "GEMA_DB_PATH", "la-del-proceso.db")
        self._guardar(groq_key="gsk-1")
        assert config.GEMA_DB_PATH == "la-del-proceso.db"

    def test_vacios_no_se_escriben(self, env_temporal):
        self._guardar(groq_key="gsk-1")
        assert "GEMINI_API_KEY" not in env_temporal.read_text(encoding="utf-8")
        assert config.GEMINI_API_KEY == ""

    def test_invalida_los_clientes_cacheados(self, env_temporal, monkeypatch):
        import nlp_engine
        monkeypatch.setattr(nlp_engine, "_groq_client", object())
        monkeypatch.setattr(nlp_engine, "_gemini_listo", True)
        self._guardar(groq_key="gsk-nueva", gemini_key="AIza-nueva")
        assert nlp_engine._groq_client is None
        assert nlp_engine._gemini_listo is False
