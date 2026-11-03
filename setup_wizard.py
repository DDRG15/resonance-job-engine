"""
setup_wizard.py — Primer arranque: detecta que no hay ninguna key y las guarda.

Sin dependencia de UI. Escribe las keys validadas al .env con set_key() de
python-dotenv y las aplica al proceso que esta corriendo (ver `_save_keys`).

Lo consume `api.py` (GET/POST /api/setup); el front de React dibuja el wizard.
La version Streamlit de esta pantalla vivia aca adentro y se fue con main.py:
importaba streamlit a nivel de modulo, asi que api.py — que solo usa las cuatro
funciones headless — arrastraba streamlit entero para arrancar.

SIN `importlib.reload(config)` (2026-10-08). `needs_setup()` lo hacia en cada
GET /api/status, y re-ejecutar config.py relee el .env con override=True: cualquier
valor de `config` ya cargado en el proceso se pisaba con el del disco. En la suite eso
deshacia los parches de los tests, y como el .env del usuario define GEMA_DB_PATH, despues
de un /api/status un `GemaDatabase()` nuevo apuntaba a la base REAL (neutralizado hasta hoy con un parche en conftest). Y la recarga ni siquiera daba
lo que prometia: nlp_engine copia las keys al importarse (`_PROVIDERS`, el cliente de
Groq cacheado), asi que recargar `config` no cambiaba la key que usaba la cadena LLM.
"""

import os

from dotenv import set_key

import config

from rutas import endurecer_secreto, ruta_datos

# Mismo archivo que lee config.py: RESONANCE_HOME/.env, o el .env del repo en desarrollo.
_ENV_PATH = ruta_datos(".env")

#: Las siete keys que usa la cadena LLM (nlp_engine). Una sola lista para el wizard y
#: para /api/status: el wizard miraba cuatro, y a quien tenia solo Mistral (el primario
#: de `parse`), Cerebras o SambaNova le decia «falta configurar».
LLM_KEY_NAMES = (
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "COHERE_API_KEY",
    "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY", "MISTRAL_API_KEY",
)


def needs_setup() -> bool:
    """
    True si el proceso no tiene ninguna key LLM.

    Lee `config` como esta, sin recargarlo: la verdad que importa es la del proceso que
    va a llamar al LLM, no la del .env en disco. Una key editada a mano en el .env con
    la API corriendo se toma al reiniciar, igual que el token de Notion.
    """
    return not any(str(getattr(config, k, "") or "").strip() for k in LLM_KEY_NAMES)


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
    """
    Escribe los valores no vacios al .env y los aplica al proceso.

    Solo toca lo que guarda: cada key va a `config`, a `os.environ` y, para Groq y
    Gemini, invalida el cliente que nlp_engine cachea con la key vieja. Recargar `config`
    entero pisaba con el disco todo lo demas (ver el docstring del modulo).
    """
    env = str(_ENV_PATH)
    guardadas = {
        "GROQ_API_KEY": groq_key,
        "GEMINI_API_KEY": gemini_key,
        "OPENROUTER_API_KEY": openrouter_key,
        "COHERE_API_KEY": cohere_key,
        "DISCORD_WEBHOOK_URL": discord_url,
    }
    # set_key conserva el modo del .env existente: uno 0644 de un arranque anterior se
    # estrecha ANTES de escribir la key (set_key reescribe vía un temporal y lo mueve).
    endurecer_secreto(env)
    for nombre, valor in guardadas.items():
        if valor:
            set_key(env, nombre, valor)
            setattr(config, nombre, valor)
            os.environ[nombre] = valor
    endurecer_secreto(env)

    if groq_key or gemini_key:
        import nlp_engine
        if groq_key:
            nlp_engine._groq_client = None       # se rearma con la key nueva al usarse
        if gemini_key:
            with nlp_engine._gemini_lock:
                nlp_engine._gemini_listo = False  # genai.configure() otra vez al usarse
