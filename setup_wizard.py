"""
setup_wizard.py — Primer arranque: detecta que no hay ninguna key y las guarda.

Sin dependencia de UI. Escribe las keys validadas al .env con set_key() de
python-dotenv y recarga el modulo config.

Lo consume `api.py` (GET/POST /api/setup); el front de React dibuja el wizard.
La version Streamlit de esta pantalla vivia aca adentro y se fue con main.py:
importaba streamlit a nivel de modulo, asi que api.py — que solo usa las cuatro
funciones headless — arrastraba streamlit entero para arrancar.
"""

import importlib
import time
from pathlib import Path

from dotenv import set_key

import config

_ENV_PATH = Path(__file__).parent / ".env"


def needs_setup() -> bool:
    """Return True if no LLM API key is configured in the current environment."""
    importlib.reload(config)
    return not any([
        config.GROQ_API_KEY.strip(),
        config.GEMINI_API_KEY.strip(),
        config.OPENROUTER_API_KEY.strip(),
        config.COHERE_API_KEY.strip(),
    ])


def _test_groq_key(api_key: str) -> tuple[bool, str]:
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=3,
        )
        return True, "Key verified."
    except Exception as exc:
        return False, str(exc)


def _test_gemini_key(api_key: str) -> tuple[bool, str]:
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash-lite")
        model.generate_content("ping", generation_config={"max_output_tokens": 3})
        return True, "Key verified."
    except Exception as exc:
        return False, str(exc)


def _save_keys(
    groq_key: str,
    gemini_key: str,
    openrouter_key: str,
    cohere_key: str,
    discord_url: str,
) -> None:
    """Write non-empty values to .env and reload config."""
    env = str(_ENV_PATH)
    if groq_key:
        set_key(env, "GROQ_API_KEY", groq_key)
    if gemini_key:
        set_key(env, "GEMINI_API_KEY", gemini_key)
    if openrouter_key:
        set_key(env, "OPENROUTER_API_KEY", openrouter_key)
    if cohere_key:
        set_key(env, "COHERE_API_KEY", cohere_key)
    if discord_url:
        set_key(env, "DISCORD_WEBHOOK_URL", discord_url)
    importlib.reload(config)
