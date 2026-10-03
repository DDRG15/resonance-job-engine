"""
config.py — Centralized configuration for Project GEMA.

All environment variables are loaded and validated here.
Every other module imports from config — no module calls os.getenv() directly.
This creates a single failure point for misconfiguration (fail fast on startup)
rather than cryptic AttributeErrors deep in the pipeline.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env anchored to this file's directory so it works regardless of
# which directory the process was launched from (e.g. streamlit run from parent).
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)


# =============================================================================
# LLM Configuration
# =============================================================================

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
COHERE_API_KEY: str = os.getenv("COHERE_API_KEY", "")
# Extra free-tier fallback providers (added 2026-07-05). All OpenAI-compatible.
# AIMLAPI + Novita removed 2026-07-05: their free tier returns 403 on the models
# we need (not our account — their tier is unusable). Dead weight, cut from chain.
CEREBRAS_API_KEY: str = os.getenv("CEREBRAS_API_KEY", "")
SAMBANOVA_API_KEY: str = os.getenv("SAMBANOVA_API_KEY", "")
MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "")
PRIMARY_LLM: str = os.getenv("PRIMARY_LLM", "groq").lower()

# Model identifiers — picks verified live against each provider's /models endpoint
# (2026-07-05). Preference: strong instruction-followers, low hallucination on the
# structured extraction schema; small/7-8B models deliberately avoided.
# Verificado por COMPLETION real el 2026-09-03, no solo por /models: 729ms, 129/165
# tokens, JSON válido. Groq dio de baja toda la familia llama de chat —
# llama-3.3-70b-versatile devolvía 404 con la key todavía válida— y hoy su catálogo son
# qwen, gpt-oss, compound (agénticos), whisper (audio) y prompt-guard (clasificadores).
#
# Por qué este y no otro de los cinco que pasaron el smoke:
#   gpt-oss-20b      360ms, más rápido, pero LLM_PROVIDERS.md documenta que se evitaron
#                    los modelos chicos a propósito: alucinan más contra el schema.
#   qwen3.6-27b      388 tokens de salida para una tarea trivial — razona de más y eso
#                    se paga en cada extracción.
#   qwen3.8-27b      3800ms.
#   compound-mini    498 tokens de prompt para nuestros 48: inyecta andamiaje agéntico.
#
# NOTA: ese mismo LLM_PROVIDERS.md dice que gpt-oss-120b devolvía content vacío EN
# CEREBRAS. En Groq responde limpio — la lección es que el smoke va por proveedor, no
# por nombre de modelo.
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# E2E 2026-09-01: gemini-2.0-flash-lite fue dado de baja y GEMA lo descubrió en
# producción — 404 en cada llamada, absorbido en silencio por el fallback. El alias
# -latest lo resuelve el proveedor, así que una deprecación futura no vuelve a
# convertirse en un 404 permanente. Verificado presente en el listado v1beta de hoy.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
OPENROUTER_MODEL: str = "nvidia/nemotron-3-ultra-550b-a55b:free"  # free tier; was google/gemma-2-27b-it (stale)
COHERE_MODEL: str = "command-r-plus-08-2024"
# Cerebras: gemma-4-31b (non-reasoning) — gpt-oss-120b is a reasoning model that
# returned no `content` field under our call shape. gemma-4-31b answers directly.
CEREBRAS_MODEL: str = "gemma-4-31b"
SAMBANOVA_MODEL: str = "Meta-Llama-3.3-70B-Instruct"
MISTRAL_MODEL: str = "mistral-small-latest"

# LLM retry policy (exponential backoff via tenacity)
LLM_MAX_RETRIES: int = 3
LLM_RETRY_WAIT_MIN: float = 1.0   # seconds
LLM_RETRY_WAIT_MAX: float = 30.0  # seconds
# Per-call request timeout. Groq/OpenRouter/Cohere already use 30s; Gemini's SDK
# defaulted to unbounded → a hung call froze the extraction thread (AUDIT #5).
LLM_REQUEST_TIMEOUT: int = int(os.getenv("LLM_REQUEST_TIMEOUT", "30"))
# Cuánto queda fuera de la cadena un proveedor que devolvió 429 (v3, Fase 3).
# Antes era un booleano por sesión: un solo 429 transitorio en la corrida del martes
# mataba a Groq hasta el reinicio y empujaba todo a los fallbacks más lentos. Con una
# ventana, la cuota se recupera sola y la cadena vuelve a su proveedor más rápido.
# Cierra el ítem abierto anotado en LLM_PROVIDERS.md.
LLM_PROVIDER_COOLDOWN_S: int = int(os.getenv("LLM_PROVIDER_COOLDOWN_S", "60"))

# ── Fase 5 (2026-09-16): version del algoritmo de scoring ─────────────────
# Riesgo R6 del , con dos fases de deuda encima. La Fase 3 saco la
# ubicacion del puntaje y la Fase 4 renormalizo las dimensiones: un score guardado el
# 2026-09-01 y uno guardado hoy NO son comparables entre si. Sin esta marca en la fila
# no hay forma de saberlo mirandola, y comparar dos numeros incomparables es peor que
# no tener ninguno.
#
# Se sube cuando cambia COMO se evalua un aviso, no cuando cambia el resultado: dos
# corridas con configs distintas y el mismo algoritmo comparten version. "Como se
# evalua" es TODO lo que score_job persiste — puntaje, tier y veredictos de
# elegibilidad —, no solo el numero: un veredicto viejo guardado junto a un puntaje
# nuevo es la misma mezcla de escalas, en otra columna.
#
#   v1  hasta 2026-09-14  presupuesto absoluto, ubicacion sumada al puntaje
#   v2  desde 2026-09-15  vetos fuera del score, dimensiones renormalizadas, GBP/EUR
#   v3  desde 2026-09-19  score de titulo continuo (cobertura 0.70 / precision 0.30)
#   v4  desde 2026-09-29  veredicto de ubicacion sin tildes y con ISO-3 de LATAM
#   v5  desde 2026-09-29  salario: separador de miles con punto, codigo delante
#   v6  desde 2026-10-01  salario por periodo anualizado; salario en texto ya no es
#                         Tier 4: se puntua sin ese eje y se marca (D3, )
#
# El salto a v3 es retroactivo: 9bc8e4b cambio _score_title y dejo la version en v2,
# asi que 1024 filas quedaron con puntajes escalonados bajo la misma etiqueta que los
# continuos. Medido sobre titulos reales del registry, 38 de 40 cambian de puntaje
# entre las dos escalas.
#
# Y subir la version NO re-puntua nada por si solo (medido el 2026-09-29: los avisos
# ya vistos no vuelven a pasar por bucket_jobs). Despues de subirla hay que correr
# tools/repuntuar_registry.py. tests/test_score_version_congelada.py falla si alguien
# cambia la salida de score_job sin tocar esta linea.
SCORE_VERSION: str = os.getenv("SCORE_VERSION", "v6")


# ── Fase 4 (2026-09-15): salarios en otra moneda ─────────────────────
# Los cinco Tier 4 de la corrida del 2026-09-04 tenian salario NUMERICO -- GBP 75k-90k,
# EUR 72k-82k, EUR 95k-115k -- y caian a revision manual porque el parser solo entendia
# $ y "usd". Tier 4 no estaba capturando "salario en prosa": estaba capturando Europa.
#
# La fecha no es decorativa. Una tasa de cambio sin fecha es un numero que va a estar
# mal y nadie va a saber desde cuando. Si pasa de TASAS_MAX_DIAS, el matcher emite un
# warning que el watchdog ya sabe clasificar -- no deja de convertir, avisa.
TASAS_A_USD_FECHA: str = os.getenv("TASAS_A_USD_FECHA", "2026-09-15")
TASAS_A_USD: dict = {
    "usd": 1.0,
    "eur": float(os.getenv("TASA_EUR_USD", "1.08")),
    "gbp": float(os.getenv("TASA_GBP_USD", "1.27")),
}
TASAS_MAX_DIAS: int = int(os.getenv("TASAS_MAX_DIAS", "90"))


# ── , Fase 1: el pais desde donde trabaja el candidato ──────────
# Decision D2: Himalayas filtra por pais EN LA FUENTE (`country=<PAIS_CANDIDATO>`). La pregunta
# "¿se puede trabajar desde el pais del candidato?" la responde quien publica la lista de paises, no un
# regex nuestro sobre esa lista. Medido el 2026-09-30: con `country=<PAIS_CANDIDATO>` la API
# incluye los avisos sin restriccion (`locationRestrictions: []`) y los que nombran a
# el pais del candidato entre sus paises. Va en ingles porque es el valor que la API acepta.
PAIS_CANDIDATO: str = os.getenv("PAIS_CANDIDATO", "")


# ── Fase 3 (2026-09-15): gate de idioma ──────────────────────────────────────
# Los idiomas en los que podes trabajar. Un aviso que exige uno que no esta aca
# recibe veredicto FAIL y sale del shortlist; uno que exige uno que si esta pero a
# nivel nativo recibe FLAG y queda con nota. Descartar en silencio no es opcion:
# un aviso desaparecido no se puede auditar, uno marcado si.
IDIOMAS_QUE_HABLO: list = [
    s.strip().lower()
    for s in os.getenv("IDIOMAS_QUE_HABLO", "espanol,ingles").split(",")
    if s.strip()
]

# Techo de tokens de salida para las llamadas en JSON mode.
#
# Era 512, calibrado para llama-3.3-70b, que emitía la respuesta y nada más. El catálogo
# actual de Groq son modelos de RAZONAMIENTO: gastan tokens pensando antes de emitir, y
# con el system prompt real gpt-oss-120b consume ~567 solo para el SearchConfig. Con 512
# el proveedor devuelve HTTP 400 json_validate_failed —"max completion tokens reached
# before generating a valid document"— y GEMA lo clasificaba como error de RED, quemando
# un round-trip en cada parse y cayendo al fallback sin decir por qué.
#
# 2048 deja margen sobre los 567 medidos. Subirlo no cuesta: solo se cobran los tokens
# realmente emitidos.
LLM_MAX_TOKENS_JSON: int = int(os.getenv("LLM_MAX_TOKENS_JSON", "2048"))

# Primario POR PROPÓSITO — reparto de carga entre proveedores (v3).
#
# El problema que resuelve: hasta acá las cuatro tareas arrancaban en PRIMARY_LLM y solo
# pasaban al siguiente cuando fallaba. O sea que un proveedor se comía el 100% del
# tráfico hasta agotarse mientras los otros seis miraban. Con siete free tiers
# disponibles, eso es tirar seis a la basura y esperar el 429.
#
# El reparto es por propósito y no rotativo a propósito: es determinístico, así que
# siempre se sabe qué proveedor atendió qué sin mirar un contador. Un round-robin
# reparte más parejo pero vuelve irreproducible cualquier diagnóstico.
#
# El criterio cruza DOS ejes, y el segundo se descubrió midiendo (2026-09-03):
#
#   1. Perfil de consumo de la tarea:
#        extract  ~20k chars de entrada, hasta 3 por corrida -> el más pesado
#        profile  el CV entero, ~8k chars, 1 por carga
#        parse    ~500 tokens, 1 por búsqueda, y el usuario ESPERA
#        audit    1-2 frases, corre justo después de parse — el usuario sigue esperando
#
#   2. Sobrecosto de razonamiento del modelo. Medido con el prompt REAL, contando
#      cuántos tokens gasta antes de emitir el JSON útil:
#
#        mistral-small          25%   2.4s   <- el más eficiente
#        cohere command-r-plus  35%  23.8s   <- eficiente pero lento
#        gemini flash-lite      43%   2.1s   <- el más rápido
#        openrouter nemotron    91%  66.8s   <- inservible como primario
#        groq gpt-oss-120b      95%   3.5s   <- 742 tokens para producir 37 de JSON
#
# El eje 2 domina donde la tarea se repite o el usuario espera. Un modelo de razonamiento
# paga su sobrecosto EN CADA LLAMADA, así que en extract (hasta 3 por corrida) se
# multiplica, y en parse+audit el usuario lo siente como latencia.
#
# Asignación resultante, con las tolerancias de cada tarea:
#   parse    -> mistral   el usuario espera; 25% de overhead y 2.4s es lo mejor que hay
#   audit    -> gemini    corre pegado al parse; 2.1s es el más rápido
#   extract  -> groq      corre en background, tolera 3.5s, y es el modelo más grande
#   profile  -> cohere    23.8s es aceptable para una carga de CV manual con spinner
#
# NO se usan como primarios: cerebras devuelve 402 Payment Required (el free tier exige
# pago) y sambanova está en 429. Los dos siguen en la cadena como respaldo — si se
# recuperan, entran solos.
#
# La cadena COMPLETA sigue detrás de cada uno: esto cambia por dónde EMPIEZA, no qué
# respaldo tiene. Un propósito cuyo primario cae recorre los otros seis igual que antes.
#
# Un nombre no configurado o sin key cae al orden por defecto sin romper nada.
LLM_PRIMARY_BY_PURPOSE: dict = {
    "parse":   os.getenv("LLM_PRIMARY_PARSE",   "mistral"),
    "audit":   os.getenv("LLM_PRIMARY_AUDIT",   "gemini"),
    "extract": os.getenv("LLM_PRIMARY_EXTRACT", "groq"),
    "profile": os.getenv("LLM_PRIMARY_PROFILE", "cohere"),
}


# =============================================================================
# Board Strategy Registry (Fase 0 — shared contract for resilience layers)
# =============================================================================
# Per-board strategy: protection, access_vector, cookies_ref, extraction_mode,
# health. Phases 27/28/29 write here. JSON store, safe free/CSS defaults.
BOARD_STRATEGY_PATH: str = os.getenv("BOARD_STRATEGY_PATH", "board_strategy.json")
# How long a board's probe stays fresh before it should be re-probed (Phase 28/29).
BOARD_STRATEGY_TTL_HOURS: int = int(os.getenv("BOARD_STRATEGY_TTL_HOURS", "168"))  # 7 days

# LLM extraction fallback (Phase 27): when a board drifts (CSS selectors return
# ~100% null), retry that board's rendered HTML through the LLM extraction path.
# Only fires on drift (rare) and is capped per run to bound token cost.
LLM_EXTRACTION_FALLBACK_ENABLED: bool = (
    os.getenv("LLM_EXTRACTION_FALLBACK_ENABLED", "true").lower() == "true"
)
LLM_FALLBACK_MAX_BOARDS_PER_RUN: int = int(os.getenv("LLM_FALLBACK_MAX_BOARDS_PER_RUN", "3"))
# Cap the cleaned SRP text sent to the LLM. Free tiers have low TPM limits (Groq
# ~12k tokens/min); a full SRP (~40k chars ≈ 12.5k tokens) hits 413. ~20k chars
# ≈ ~6k tokens leaves room for the system prompt + output. Truncated, not chunked.
LLM_FALLBACK_MAX_INPUT_CHARS: int = int(os.getenv("LLM_FALLBACK_MAX_INPUT_CHARS", "20000"))

# NEW #6 (auditoría 07-10) — replayability del drift: cuando el fallback LLM
# recupera 0 jobs de un board drifteado, guardamos {url, html/texto enviado,
# output} a este dir para debuggear offline por qué falló (builtin/greenhouse
# recuperaron 0 en runs reales y no se podía investigar). Feeds Phase 30.
DRIFT_QUARANTINE_DIR: str = os.getenv("DRIFT_QUARANTINE_DIR", "drift_quarantine")
# v3 Fase 5: subido de 50 a 300. El cap de 50 servía para debug puntual; ahora estos
# snapshots SON el corpus de evaluación de la Fase 7, y a 3 boards drifteados por
# corrida, 50 archivos se reciclan en dos semanas — justo el período que habría que
# comparar cuando se cambia un prompt.
DRIFT_QUARANTINE_MAX_FILES: int = int(os.getenv("DRIFT_QUARANTINE_MAX_FILES", "300"))
# 1 de cada N extracciones EXITOSAS también se snapshotea. Un corpus hecho solo de
# fallas produce un gate que mide el peor caso y nada más: no puede detectar que un
# cambio de prompt empeoró lo que ya funcionaba. 0 desactiva el muestreo.
DRIFT_SNAPSHOT_SUCCESS_SAMPLE: int = int(os.getenv("DRIFT_SNAPSHOT_SUCCESS_SAMPLE", "10"))


# =============================================================================
# Scheduler / cron (B4)
# =============================================================================
# Jobs are usually posted Sat–Mon → Tuesday is a good scrape day (el usuario).
SCHEDULER_TIMEZONE: str = os.getenv("SCHEDULER_TIMEZONE", "UTC")   # la ciudad del candidato = UTC-5
SCHEDULER_CRON_DAYS: str = os.getenv("SCHEDULER_CRON_DAYS", "tue,fri,sat")  # day_of_week
SCHEDULER_CRON_HOUR: int = int(os.getenv("SCHEDULER_CRON_HOUR", "9"))       # local hour
# CV profile retention for unattended cron. Stores ONLY the ephemeral profile
# (role/skills/signals) — NEVER the raw CV document. Opt-in; default = discard.
CV_PROFILE_PATH: str = os.getenv("CV_PROFILE_PATH", "cv_profile.json")


# =============================================================================
# Scraper Configuration
# =============================================================================

JITTER_MIN: float = float(os.getenv("JITTER_MIN_SECONDS", "5"))
JITTER_MAX: float = float(os.getenv("JITTER_MAX_SECONDS", "15"))

# Circuit breaker: consecutive null extractions before flagging a domain
CIRCUIT_BREAKER_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
# Seconds an OPEN domain waits before it may probe once (OPEN → HALF_OPEN).
# A transient 429 burst should not kill a board for the whole session.
CIRCUIT_HALF_OPEN_COOLDOWN: int = int(os.getenv("CIRCUIT_HALF_OPEN_COOLDOWN", "120"))

# Maximum consecutive HTTP 429/403 before aborting full run
MAX_RATE_LIMIT_HITS: int = int(os.getenv("MAX_RATE_LIMIT_HITS", "50"))

# Per-page timeout in milliseconds (30s — from Vol 1.4, Risk 2.2)
PAGE_TIMEOUT_MS: int = 30_000

# User-Agent pool for rotation
USER_AGENT_POOL: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
]


# =============================================================================
# Tier / Matching Configuration
# =============================================================================

TIER1_MIN_SCORE: int = 80   # 80–100% → Apply immediately
TIER2_MIN_SCORE: int = 50   # 50–79%  → Secondary review
TIER3_MIN_SCORE: int = 1    # 1–49%   → Recycle bin


# =============================================================================
# Database Configuration
# =============================================================================

GEMA_DB_PATH: str = os.getenv("GEMA_DB_PATH", "gema_registry.db")
DB_BACKUP_SUFFIX: str = ".bak"
DEFAULT_TTL_HOURS: int = int(os.getenv("DEFAULT_TTL_HOURS", "0"))

# Donde `tools/repuntuar_registry.py` deja la copia de la base antes de escribir. Una ruta
# relativa se resuelve contra la raiz del repo, no contra el cwd: `.gitignore` y
# `.dockerignore` excluyen `backups/` en la raiz, y un directorio que cambia segun desde
# donde se lance la herramienta es un directorio que esas reglas no cubren.
GEMA_BACKUP_DIR: str = os.getenv("GEMA_BACKUP_DIR", "backups")


# =============================================================================
# API: hosts permitidos (Plan30 Fase 3)
# =============================================================================
#
# La API no tiene autenticacion: CV, setup, scheduler y reset quedan abiertos a quien
# alcance el puerto. docker-compose ya publica solo en 127.0.0.1, pero un navegador en la
# misma maquina puede ser empujado a hablarle por DNS rebinding: una pagina de
# `evil.example` cuyo DNS pasa a resolver 127.0.0.1 hace que el navegador mande
# `Host: evil.example` a localhost:8000 y, para el navegador, la respuesta es del mismo
# origen que la pagina. Esa pagina lee el perfil y dispara un reset. El unico dato que el
# atacante no controla es el Host que le toca al navegador, asi que ahi se corta.
#
# Lista separada por comas, sin puerto (el middleware compara solo el host). Acepta
# `*.dominio.com`. Un `*` pelado se descarta: apagar la proteccion no se hace con un
# valor por defecto de comodin; se hace listando los hosts que de verdad se van a usar.
_ALLOWED_HOSTS_DEFAULT = "localhost,127.0.0.1"


def parse_allowed_hosts(raw: str) -> list[str]:
    """
    Lista de patrones de Host validos a partir del texto de GEMA_ALLOWED_HOSTS.

    Descarta, con aviso en el log, lo que no puede funcionar o apaga la proteccion:
    un `*` pelado, un comodin que no sea `*.dominio`, un patron con puerto o con
    esquema (el middleware compara el host sin puerto, asi que `localhost:8000` no
    matchea NADA y deja al usuario fuera de su propia API). Si no queda ningun patron
    valido vuelve al valor por defecto: una variable mal escrita no puede dejar el
    servidor sin ningun host permitido ni abrirlo a todos.
    """
    import logging

    validos: list[str] = []
    for pat in (p.strip().lower() for p in (raw or "").split(",")):
        if not pat:
            continue
        sin_puerto = pat.startswith("[") or ":" not in pat
        comodin_ok = "*" not in pat or (pat.startswith("*.") and "*" not in pat[1:])
        if pat == "*" or not sin_puerto or not comodin_ok or "/" in pat or " " in pat:
            logging.getLogger("gema.config").warning(
                "[CONFIG] GEMA_ALLOWED_HOSTS: se descarta %r (sin puerto ni esquema, "
                "y '*' solo como '*.dominio.com').", pat,
            )
            continue
        if pat not in validos:
            validos.append(pat)
    return validos or _ALLOWED_HOSTS_DEFAULT.split(",")


ALLOWED_HOSTS: list[str] = parse_allowed_hosts(
    os.getenv("GEMA_ALLOWED_HOSTS", _ALLOWED_HOSTS_DEFAULT)
)

# ── Origins permitidos para metodos que mutan estado ─────────────────────────
#
# El Host no alcanza. Una pagina de `evil.example` abierta en el navegador de el usuario puede
# mandar un `<form method=POST action="http://localhost:8000/api/reset">` y el navegador
# lo envia con `Host: localhost:8000` —el Host es el del destino, que es legitimo—. No hay
# preflight de CORS para un formulario simple, asi que CORS no lo frena: CORS decide si la
# pagina puede LEER la respuesta, no si el request se ejecuta. El reset ya corrio. Lo que
# delata el request es el header `Origin`, que el navegador pone en todo POST/PUT/PATCH/
# DELETE y que la pagina no puede falsear ni quitar.
#
# Lista separada por comas de `esquema://host[:puerto]`, sin ruta. Los cuatro del default
# son los dos del frontend en desarrollo (Vite, 5173) y los dos de la API que sirve el
# bundle (8000, tambien el contenedor publicado en 127.0.0.1:8000). El proxy de Vite
# reenvia el Origin del navegador sin tocarlo, asi que los POST del frontend en dev llegan
# con `http://localhost:5173`.
_ALLOWED_ORIGINS_DEFAULT = (
    "http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:8000,http://127.0.0.1:8000"
)


def parse_allowed_origins(raw: str) -> list[str]:
    """
    Lista de origins validos a partir del texto de GEMA_ALLOWED_ORIGINS.

    Un origin es `http(s)://host[:puerto]` y nada mas: sin ruta, sin comodin. Se descarta
    con aviso lo que no cumple —un `*` que dejaria pasar a cualquier pagina, una URL con
    ruta que nunca iguala a un Origin real— y si no queda nada vuelve al default.
    """
    import logging
    import re

    validos: list[str] = []
    # rstrip("/"): `http://localhost:5173/` es el error de tipeo mas comun y un Origin real
    # nunca lleva barra final, asi que se normaliza en vez de descartarlo.
    for original in (p.strip() for p in (raw or "").split(",")):
        if not original:
            continue
        o = original.lower().rstrip("/")
        if not re.fullmatch(r"https?://[a-z0-9.\-]+(:\d{1,5})?|https?://\[[0-9a-f:]+\](:\d{1,5})?", o):
            logging.getLogger("gema.config").warning(
                "[CONFIG] GEMA_ALLOWED_ORIGINS: se descarta %r (formato "
                "`http(s)://host[:puerto]`, sin ruta ni comodin).", original,
            )
            continue
        if o not in validos:
            validos.append(o)
    return validos or _ALLOWED_ORIGINS_DEFAULT.split(",")


ALLOWED_ORIGINS: list[str] = parse_allowed_origins(
    os.getenv("GEMA_ALLOWED_ORIGINS", _ALLOWED_ORIGINS_DEFAULT)
)


# =============================================================================
# Integration: Notion
# =============================================================================

NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")

# Notion tier → color label mapping
NOTION_TIER_COLORS: dict[str, str] = {
    "Tier 1": "green",
    "Tier 2": "yellow",
    "Tier 3": "red",
    "Tier 4": "purple",   # Manual Review — unstructured salary text
}


# =============================================================================
# Integration: Google Sheets
# =============================================================================

GOOGLE_CREDENTIALS_PATH: str = os.getenv(
    "GOOGLE_CREDENTIALS_PATH", "credentials/google_service_account.json"
)
GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")

# Column order for the historical data warehouse sheet
SHEETS_COLUMN_ORDER: list[str] = [
    "scraped_at", "title", "company", "salary",
    "url", "tier", "match_score", "search_run_id",
]


# =============================================================================
# Startup Validation
# =============================================================================

def validate_config() -> list[str]:
    """
    Checks that all required keys are populated.
    Returns a list of human-readable warning strings (empty = all good).

    Called by main.py at startup — shows warnings in the Streamlit sidebar
    rather than crashing with a cryptic KeyError during the first API call.
    """
    warnings: list[str] = []

    if not any([GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, COHERE_API_KEY]):
        warnings.append(
            "⚠️  No LLM API key found. Set at least one of: "
            "GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, COHERE_API_KEY in .env"
        )

    if PRIMARY_LLM == "groq" and not GROQ_API_KEY:
        warnings.append(
            "⚠️  PRIMARY_LLM=groq but GROQ_API_KEY is empty. "
            "Will attempt fallback to Gemini."
        )

    if PRIMARY_LLM == "gemini" and not GEMINI_API_KEY:
        warnings.append(
            "⚠️  PRIMARY_LLM=gemini but GEMINI_API_KEY is empty. "
            "Will attempt fallback to Groq."
        )

    if not OPENROUTER_API_KEY:
        warnings.append(
            "⚠️  OPENROUTER_API_KEY not set — OpenRouter fallback disabled."
        )

    if not COHERE_API_KEY:
        warnings.append(
            "⚠️  COHERE_API_KEY not set — Cohere fallback disabled."
        )

    if not DISCORD_WEBHOOK_URL:
        warnings.append(
            "⚠️  DISCORD_WEBHOOK_URL not set — Discord notifications disabled."
        )

    if NOTION_API_KEY and not NOTION_DATABASE_ID:
        warnings.append(
            "⚠️  NOTION_API_KEY set but NOTION_DATABASE_ID is missing."
        )

    if GOOGLE_SHEET_ID and not Path(GOOGLE_CREDENTIALS_PATH).exists():
        warnings.append(
            f"⚠️  GOOGLE_SHEET_ID set but credentials file not found: "
            f"{GOOGLE_CREDENTIALS_PATH}"
        )

    if JITTER_MIN >= JITTER_MAX:
        warnings.append(
            f"⚠️  JITTER_MIN ({JITTER_MIN}s) must be less than "
            f"JITTER_MAX ({JITTER_MAX}s)."
        )

    return warnings


# =============================================================================
# "The Blitz" — Parallelism & Webhook Configuration
# =============================================================================

# Semaphore value: max concurrent Chromium tabs inside asyncio.gather().
# Analysis: 3 is the RAM/CPU sweet spot on a 2-core Docker host.
# At 3 tabs: ~440MB RAM, CPU stays under 85%. At 5: timeout cascade risk.
SCRAPER_CONCURRENCY: int = int(os.getenv("SCRAPER_CONCURRENCY", "3"))
IGNORE_HTTPS_ERRORS: bool = os.getenv("IGNORE_HTTPS_ERRORS", "False").lower() == "true"
MAX_PAGES_PER_DOMAIN: int = int(os.getenv("MAX_PAGES_PER_DOMAIN", "5"))

# Write-queue flush policy for the async DB writer task
DB_WRITE_BATCH_SIZE: int    = int(os.getenv("DB_WRITE_BATCH_SIZE", "10"))
DB_WRITE_FLUSH_TIMEOUT: float = float(os.getenv("DB_WRITE_FLUSH_TIMEOUT", "2.0"))
# Bounded retries before a failed batch is declared a permanent loss and recorded
# in summary.errors. mark_seen_batch is INSERT OR IGNORE, so retries are idempotent.
DB_WRITE_MAX_RETRIES: int   = int(os.getenv("DB_WRITE_MAX_RETRIES", "3"))

# Webhooks — Discord and/or Slack (both optional, both fire if set)
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL:   str = os.getenv("SLACK_WEBHOOK_URL",   "")

# Null-rate threshold: if >40% of SRP cards return null, alert for selector drift
SRP_NULL_RATE_ALERT_THRESHOLD: float = float(
    os.getenv("SRP_NULL_RATE_ALERT_THRESHOLD", "0.4")
)
