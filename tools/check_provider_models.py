"""
check_provider_models.py — verifica que cada modelo pinneado siga existiendo.

Distingue los dos fallos que desde adentro de GEMA se ven idénticos y piden arreglos
opuestos:

    KEY   401/403 — la credencial venció o fue revocada. Se renueva la key.
    GONE  200 pero el id no está en el listado — el proveedor dio de baja el modelo.
          Se cambia una línea de config.py.

Confundirlos cuesta horas. El E2E del 2026-09-01 encontró los dos en la misma sesión:
llama-3.3-70b-versatile estaba GONE con la key todavía válida, y veinte minutos después
esa misma key pasó a KEY. GEMA no reportó ninguno de los dos — el log decía "network".

Uso:
    python tools/check_provider_models.py          # exit 1 si algo está mal
    python tools/check_provider_models.py --list   # además imprime los modelos disponibles

Pensado para correr a mano tras renovar credenciales, y como gate en build_beta.bat.
No corre en la suite: pega a APIs reales.
"""

import argparse
import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402

TIMEOUT = 25


def _listar(url: str, headers: dict, forma: str) -> tuple[int, list[str]]:
    """Devuelve (status_code, ids). Lista vacía si no se pudo listar."""
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        return r.status_code, []
    data = r.json()
    if forma == "gemini":
        return 200, [m.get("name", "").split("/")[-1] for m in data.get("models", [])]
    if forma == "cohere":
        # Cohere no es OpenAI-compatible ni acá: devuelve {"models": [{"name": ...}]}.
        return 200, [m.get("name") for m in data.get("models", []) if m.get("name")]
    return 200, [m.get("id") for m in data.get("data", []) if m.get("id")]


def _checks() -> list[tuple]:
    return [
        ("groq", "https://api.groq.com/openai/v1/models",
         {"Authorization": f"Bearer {config.GROQ_API_KEY}"}, config.GROQ_MODEL,
         "openai", config.GROQ_API_KEY),
        ("openrouter", "https://openrouter.ai/api/v1/models",
         {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}, config.OPENROUTER_MODEL,
         "openai", config.OPENROUTER_API_KEY),
        ("cerebras", "https://api.cerebras.ai/v1/models",
         {"Authorization": f"Bearer {config.CEREBRAS_API_KEY}"}, config.CEREBRAS_MODEL,
         "openai", config.CEREBRAS_API_KEY),
        ("sambanova", "https://api.sambanova.ai/v1/models",
         {"Authorization": f"Bearer {config.SAMBANOVA_API_KEY}"}, config.SAMBANOVA_MODEL,
         "openai", config.SAMBANOVA_API_KEY),
        ("mistral", "https://api.mistral.ai/v1/models",
         {"Authorization": f"Bearer {config.MISTRAL_API_KEY}"}, config.MISTRAL_MODEL,
         "openai", config.MISTRAL_API_KEY),
        ("cohere", "https://api.cohere.ai/v1/models",
         {"Authorization": f"Bearer {config.COHERE_API_KEY}"}, config.COHERE_MODEL,
         "cohere", config.COHERE_API_KEY),
        ("gemini",
         f"https://generativelanguage.googleapis.com/v1beta/models?key={config.GEMINI_API_KEY}",
         {}, config.GEMINI_MODEL, "gemini", config.GEMINI_API_KEY),
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true",
                    help="imprime los modelos disponibles de cada proveedor")
    args = ap.parse_args(argv)

    keys_muertas, modelos_muertos = [], []

    for nombre, url, headers, modelo, forma, key in _checks():
        if not key:
            print(f"[SKIP] {nombre:11} sin API key configurada")
            continue
        try:
            code, ids = _listar(url, headers, forma)
        except Exception as exc:
            print(f"[?]    {nombre:11} error de red: {type(exc).__name__}: {exc}")
            continue

        if code in (401, 403):
            print(f"[KEY]  {nombre:11} credencial invalida o vencida (HTTP {code}) "
                  f"— renovar en .env")
            keys_muertas.append(nombre)
            continue
        if code != 200:
            print(f"[?]    {nombre:11} HTTP {code} — no se pudo listar")
            continue
        if not ids:
            print(f"[?]    {nombre:11} listado vacio o con otra forma — revisar a mano")
            continue

        if modelo in ids:
            print(f"[OK]   {nombre:11} {modelo}")
        else:
            print(f"[GONE] {nombre:11} {modelo} NO existe "
                  f"— {len(ids)} modelos disponibles")
            modelos_muertos.append(nombre)
        if args.list:
            for i in sorted(ids):
                print(f"           {i}")

    print()
    if keys_muertas:
        print(f"CREDENCIALES VENCIDAS ({len(keys_muertas)}): {', '.join(keys_muertas)}")
        print("  -> renovar la API key en .env")
    if modelos_muertos:
        print(f"MODELOS DADOS DE BAJA ({len(modelos_muertos)}): {', '.join(modelos_muertos)}")
        print("  -> cambiar el pin en config.py (correr con --list para ver opciones)")
    if keys_muertas or modelos_muertos:
        return 1

    print("Todos los proveedores configurados responden y sus modelos existen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
