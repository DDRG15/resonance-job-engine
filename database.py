"""
database.py — Seen Registry & Search Vault for Project GEMA.

SRE Principles:
    WAL journal mode   — survives abrupt kill without corruption
    Pre-write backup   — .db.bak before any bulk write session
    SHA-256 Delta Load — hash(normalized_url) ONLY, never hash+timestamp
    TTL filter         — ignore jobs seen within N hours
    Search Vault       — persists successful SearchConfig JSONs for reuse
"""

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from models import JobResult, SearchConfig, SearchVaultEntry
import config
import metrics

logger = logging.getLogger(__name__)


# Un mensaje de telemetría existe para identificar la falla, no para archivarla. Los 5xx
# de gateway de los free tiers traen páginas HTML enteras en el cuerpo de la excepción, y
# una fila de llm_calls con 8 KB de HTML vuelve ilegible justo la tabla que se consulta
# cuando algo anda mal.
_MAX_ERROR_CHARS = 500


def _clip(text, limit: int = _MAX_ERROR_CHARS):
    """Recorta dejando constancia de cuánto se cortó — un texto truncado en silencio
    parece completo y manda a buscar una causa que no está."""
    if text is None:
        return None
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… (+{len(text) - limit} chars)"


def _now_iso() -> str:
    """UTC siempre. Un timestamp de telemetría en hora local no se puede correlacionar
    con nada — ni entre máquinas, ni a través de un cambio de horario."""
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Fase 9 (2026-09-30) — próxima acción de una postulación
#
# `follow_up_at` se escribía desde la Fase 7 y nadie lo leía: una postulación con
# «escribir al recruiter el jueves» se enteraba del jueves por casualidad, y un
# seguimiento que no se hace a tiempo es una postulación que pasa a `sin_respuesta`
# sin que nadie lo haya decidido. La fase conecta el dato con dos lectores: la vista de
# postulaciones (marca la vencida) y el reporte del cron (la avisa por Discord).
#
# Estas funciones son el único lugar donde vive la regla de «vencida». La lista, el
# contador del sidebar y el reporte de Discord la llaman a ella: tres copias del mismo
# criterio terminan en tres números distintos para la misma pregunta.
# =============================================================================

#: Tope del texto. «Escribir al recruiter» son 22 caracteres; 200 cubre una frase larga
#: y evita que un pegado accidental de un mail entero viaje al reporte de Discord, que
#: corta en 2000 caracteres.
MAX_PROXIMA_ACCION = 200

#: Distingue «no mandaron el campo» (no tocar) de «mandaron null» (borrar). Con None
#: solo no se puede borrar una fecha sin pisar también la otra.
SIN_CAMBIO = object()


def normalizar_fecha_accion(valor) -> Optional[str]:
    """
    Devuelve la fecha como `YYYY-MM-DD`, `None` si el valor está vacío, o lanza
    `ValueError` si no es una fecha.

    Se guarda solo el día. «Vencida» se decide por día calendario —una acción para el
    jueves no vence el jueves a las 00:01—, así que una hora guardada sería un dato que
    nadie lee y que dos clientes (el input `date` del front y un curl) escribirían en
    formatos distintos. Si llega un datetime ISO se acepta y se descarta la hora.

    Strings en la base y no un tipo fecha: es lo que `follow_up_at` ya era desde la
    Fase 7, y el orden lexicográfico de `YYYY-MM-DD` coincide con el cronológico.
    """
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ValueError("follow_up_at debe ser una fecha YYYY-MM-DD (o vacío para borrarla)")
    texto = valor.strip()
    if not texto:
        return None
    try:
        if len(texto) == 10:
            return date.fromisoformat(texto).isoformat()
        return datetime.fromisoformat(texto).date().isoformat()
    except ValueError:
        raise ValueError(
            f"follow_up_at debe ser una fecha YYYY-MM-DD (o vacío para borrarla), "
            f"llegó {valor!r}") from None


def normalizar_proxima_accion(valor) -> Optional[str]:
    """
    El texto de la próxima acción, sin espacios sobrantes; `None` si está vacío.

    Lanza `ValueError` si pasa de `MAX_PROXIMA_ACCION`. No se trunca: un texto cortado
    en silencio parece completo, y quien lo escribió no se entera de que perdió el final.
    """
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ValueError("next_action debe ser texto")
    texto = valor.strip()
    if not texto:
        return None
    if len(texto) > MAX_PROXIMA_ACCION:
        raise ValueError(
            f"next_action admite hasta {MAX_PROXIMA_ACCION} caracteres, "
            f"llegaron {len(texto)}")
    return texto


def hoy_local() -> date:
    """
    «Hoy» en la zona del cron (`SCHEDULER_TIMEZONE`, la ciudad del candidato por defecto), no en UTC.

    La fecha de seguimiento la elige una persona en la ciudad del candidato. Con UTC, entre las 19:00 y la
    medianoche de la ciudad del candidato «hoy» ya es mañana y una acción para hoy figuraría vencida cinco
    horas antes de tiempo, justo en la franja en que el usuario revisa la pantalla.
    """
    try:
        return datetime.now(ZoneInfo(config.SCHEDULER_TIMEZONE)).date()
    except Exception as exc:
        logger.warning("[DB] SCHEDULER_TIMEZONE %r no sirve (%s); 'hoy' sale de la "
                       "hora local del sistema", config.SCHEDULER_TIMEZONE, exc)
        return date.today()


def _fecha_de(texto) -> Optional[date]:
    """La fecha de un `follow_up_at` ya guardado, o None si no se puede leer."""
    try:
        normalizada = normalizar_fecha_accion(texto)
    except ValueError:
        return None
    return date.fromisoformat(normalizada) if normalizada else None


def esta_vencida(fila, hoy: date) -> bool:
    """
    Una postulación está vencida si tiene fecha, la fecha es anterior a `hoy` y la
    postulación sigue abierta.

    **Anterior a hoy, no «hasta hoy».** Una acción para hoy todavía se puede hacer;
    marcarla vencida a la mañana enseña a ignorar la marca. **Y abierta:** una
    postulación rechazada o aceptada ya no tiene nada que atender, y contarla inflaría
    el badge con cosas que nadie puede hacer. «Cerrada» es `applications.TERMINALES`,
    la misma fuente que usa el embudo, no una lista paralela.
    """
    import applications as app
    if fila["status"] in app.TERMINALES:
        return False
    fecha = _fecha_de(fila["follow_up_at"])
    return fecha is not None and fecha < hoy


# Un aviso con 40 razones de match es un aviso con un bug en el matcher, no un aviso
# muy bien explicado. El tope existe para que ese bug no infle la fila a kilobytes.
_MAX_RAZONES = 20


def _json_o_none(valor) -> Optional[str]:
    """Una lista de razones a JSON, o None si no hay nada que guardar.

    None y no `"[]"`: la columna usa COALESCE, y un `"[]"` pisaría con una lista
    vacía lo que una corrida anterior sí averiguó. La diferencia entre «no sé» y
    «sé que no hay» tiene que sobrevivir al guardado.
    """
    if not valor:
        return None
    try:
        return json.dumps([_clip(str(v), 300) for v in list(valor)[:_MAX_RAZONES]],
                          ensure_ascii=False)
    except Exception:
        return None


# =============================================================================
# Schema DDL
# =============================================================================

_DDL_SEEN_REGISTRY = """
CREATE TABLE IF NOT EXISTS seen_jobs_registry (
    job_hash        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    source_domain   TEXT NOT NULL,
    scraped_at      TEXT NOT NULL,
    tier            TEXT,
    match_score     INTEGER,
    location_raw    TEXT,
    salary_raw      TEXT
);
"""

# Fase 7 (2026-09-16) — el ciclo de vida de una postulación.
#
# GEMA terminaba en «encontrado y empujado a Notion». Sin esto no se puede responder
# «de los Tier 1 que apliqué, cuántos convirtieron», y esa pregunta es el único
# feedback real que tiene el matcher: hoy el scoring se calibra a ojo.
#
# `job_hash` es la misma clave que usa el registry, no un id nuevo. Una postulación
# sin aviso no existe, y duplicar la identidad sería inventarse una segunda forma de
# decir cuál es el mismo trabajo — que es exactamente el problema que la Fase 11
# (correlators) existe para arreglar en el registry.
#
# UNIQUE en job_hash: una postulación por aviso. Postularse dos veces al mismo puesto
# es un error de la persona, y la base no debería ayudarlo a pasar desapercibido.
_DDL_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS applications (
    app_id           TEXT PRIMARY KEY,
    job_hash         TEXT NOT NULL UNIQUE,
    url              TEXT NOT NULL,
    title            TEXT,
    company          TEXT,
    source_domain    TEXT,
    tier_al_aplicar  TEXT,
    score_al_aplicar INTEGER,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    stage_changed_at TEXT NOT NULL,
    applied_at       TEXT,
    closed_at        TEXT,
    salary_offered   INTEGER,
    follow_up_at     TEXT,
    notes            TEXT,
    next_action      TEXT
);
"""

# El histórico de transiciones va en su propia tabla, no en una columna JSON.
#
# `applications.status` dice dónde está hoy; esto dice por dónde pasó. Sin el
# histórico, un embudo no puede decir en qué etapa se cae la gente — solo dónde
# quedó la que ya terminó, que es otra pregunta. Y un rechazo en `tecnica` y uno en
# `screening` cuentan distinto para calibrar el matcher.
_DDL_APPLICATION_EVENTS = """
CREATE TABLE IF NOT EXISTS application_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     TEXT NOT NULL,
    desde      TEXT,
    hasta      TEXT NOT NULL,
    ocurrio_en TEXT NOT NULL,
    nota       TEXT
);
"""

# Fase 11 (2026-09-17) — identidad como pipeline, no como campo.
#
# `compute_hash` es SHA-256 de la URL canónica: un aviso es «el mismo» solo si su URL
# lo es. Medido el 2026-09-11: 1.536 filas, 1.407 pares (título, empresa) únicos —
# 129 de más, 8,4%. La duplicación no es entre boards, es el mismo board relistando.
#
# `estado` calca el modelo de Version Tracking de Ghidra: aceptar un canónico BLOQUEA
# al rival en vez de borrarlo. El duplicado sigue ahí como evidencia de que el board
# relista, y la decisión es reversible.
_DDL_JOB_LINKS = """
CREATE TABLE IF NOT EXISTS job_links (
    job_hash       TEXT NOT NULL,
    hash_duplicado TEXT NOT NULL,
    correlator     TEXT NOT NULL,
    confianza      REAL NOT NULL,
    motivo         TEXT,
    estado         TEXT NOT NULL,
    decidido_en    TEXT,
    creado_en      TEXT NOT NULL,
    PRIMARY KEY (job_hash, hash_duplicado)
);
"""

_DDL_SEARCH_VAULT = """
CREATE TABLE IF NOT EXISTS search_vault (
    vault_id        TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    times_used      INTEGER DEFAULT 0
);
"""

# Phase 30a — Pila de revisión manual. Jobs con title+company REALES pero URL
# no verificable (relativa/alucinada por el LLM fallback). Se rescatan acá en vez
# de tirarlos (principio: no descartar data salvageable). Key = hash(title|company
# |domain) porque NO tienen URL confiable — separado del dedup principal.
#
# SEAM PARA 30b: se guardan las TRES URLs — original_url (la mala que produjo el
# LLM), srp_url (la pista: el listado donde apareció), y resolved_url (la que el
# humano corrige). Con las tres, 30b puede DIFERENCIARLAS y aprender el patrón de
# transformación por board (ej. "url real = srp_url + slug relativo") sin migrar
# el schema. Sin estos 3 campos, 30b no tendría de dónde aprender.
_DDL_MANUAL_REVIEW = """
CREATE TABLE IF NOT EXISTS manual_review (
    review_hash     TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    source_domain   TEXT NOT NULL,
    original_url    TEXT,
    srp_url         TEXT,
    reason          TEXT,
    captured_at     TEXT NOT NULL,
    resolved        INTEGER DEFAULT 0,
    resolved_url    TEXT,
    resolved_at     TEXT
);
"""

# =============================================================================
# v3 Fase 4 — Instrumentación
#
# Hasta acá GEMA no podía responder "¿por qué esta corrida sacó menos jobs que la
# anterior?" sin leer código: run_id, summary y tiers vivían solo en memoria y morían
# con el proceso, y de los 19 call sites de metrics.emit ninguno era del lado LLM.
#
# Tres tablas, aditivas: la app vieja las ignora, así que el rollback de la rama no
# necesita migración inversa y la telemetría acumulada sobrevive.
# =============================================================================

# tier4_count existe acá y NO en ScrapeRunSummary, que nunca lo llevó pese a que
# bucket_jobs devuelve cuatro tiers — el payload del SSE y el reporte de Discord lo
# recalculaban cada uno por su lado.
_DDL_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    started_at         TEXT NOT NULL,
    completed_at       TEXT,
    trigger            TEXT,
    status             TEXT,
    config_json        TEXT,
    summary_json       TEXT,
    tier1_count        INTEGER,
    tier2_count        INTEGER,
    tier3_count        INTEGER,
    tier4_count        INTEGER,
    domains_aborted    TEXT,
    breaker_state_json TEXT,
    error              TEXT
);
"""

# exec_ms y queue_wait_ms van SEPARADOS a propósito. Send despacha todas las ramas del
# fan-out a la vez y el semáforo las frena adentro del nodo, así que veinte nodos pueden
# figurar "corriendo" mientras diecisiete esperan un tab de Chromium. Sumados, el número
# miente sobre dónde se fue el tiempo.
#
# Fase 4 (2026-09-30): `items` y `aceptados` cuentan avisos, no tiempo. Hasta el 29-09
# la tabla sabía cuánto tardó cada rama y no sabía cuánto trajo: trueup.io gastó 1.618 s
# en 51 búsquedas vacías y la única forma de enterarse fue sumar tiempos a mano.
#   items      avisos CRUDOS que la fuente devolvió, ANTES de cualquier filtro propio y de
#              la dedup contra el registry. NULL = la rama no consultó la fuente (breaker
#              abierto, query ya visitada) o la fila es anterior a esta columna; NULL no
#              es cero, y todo lo que lee esta columna tiene que distinguirlos.
#   aceptados  lo que pasó a la dedup. Ramas CSS: avisos NUEVOS (post delta load).
#              Endpoint/ATS: avisos que pasan relevancia y validez, aunque el registry
#              ya los tenga. Son dos magnitudes distintas bajo un mismo nombre porque
#              cada camino filtra en un punto distinto; la decisión de salud usa `items`.
_DDL_NODE_TRACES = """
CREATE TABLE IF NOT EXISTS node_traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    node          TEXT NOT NULL,
    graph         TEXT,
    domain        TEXT,
    title         TEXT,
    started_at    TEXT,
    exec_ms       REAL,
    queue_wait_ms REAL,
    status        TEXT,
    error         TEXT,
    items         INTEGER,
    aceptados     INTEGER
);
"""

# provider_index ES la profundidad del fallback: un 3 significa que tres proveedores
# fallaron antes de que el cuarto respondiera. Es la métrica que v2 no tenía y la que
# contesta "¿por qué esta corrida tardó el triple?".
_DDL_LLM_CALLS = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT,
    trace_id          TEXT NOT NULL,
    purpose           TEXT,
    provider          TEXT,
    model             TEXT,
    provider_index    INTEGER,
    attempt           INTEGER,
    latency_ms        REAL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    outcome           TEXT,
    output_chars      INTEGER,
    error             TEXT,
    created_at        TEXT NOT NULL
);
"""

# v3 Fase 7 — Resultados de evaluación de extracción.
#
# Una fila por (corrida de eval × caso del corpus). Se guarda el desglose y no solo el
# agregado: un grounding rate que baja de 0.95 a 0.80 no dice nada por sí solo — lo que
# sirve es saber QUÉ casos se rompieron, porque casi siempre son de un board.
#
# prompt_version y provider van en cada fila porque son las dos variables que explican
# una caída. Sin ellas, una regresión del modelo y un cambio de prompt se ven idénticas.
_DDL_EVAL_RESULTS = """
CREATE TABLE IF NOT EXISTS eval_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_id     TEXT NOT NULL,
    corpus_case     TEXT NOT NULL,
    source          TEXT,
    prompt_version  TEXT,
    provider        TEXT,
    jobs_expected   INTEGER,
    jobs_extracted  INTEGER,
    grounded        INTEGER,
    hallucinated    INTEGER,
    fields_complete REAL,
    agreement       REAL,
    error           TEXT,
    created_at      TEXT NOT NULL
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_registry_domain  ON seen_jobs_registry(source_domain);",
    "CREATE INDEX IF NOT EXISTS idx_registry_scraped ON seen_jobs_registry(scraped_at);",
    "CREATE INDEX IF NOT EXISTS idx_vault_created    ON search_vault(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_review_resolved  ON manual_review(resolved);",
    "CREATE INDEX IF NOT EXISTS idx_runs_started     ON runs(started_at);",
    "CREATE INDEX IF NOT EXISTS idx_traces_run       ON node_traces(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_run    ON llm_calls(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_trace  ON llm_calls(trace_id);",
    "CREATE INDEX IF NOT EXISTS idx_eval_run        ON eval_results(eval_run_id);",
]


# =============================================================================
# Connection Management
# =============================================================================

@contextmanager
def _get_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Yields a WAL-mode SQLite connection, commits on clean exit, rolls back on error.

    WAL mode (Write-Ahead Logging):
        Writes go to a separate log file first. On reconnect after a crash,
        SQLite replays the log — the database is self-healing.
        Directly mitigates Vol 1.4 Risk 4.1 (corruption on abrupt exit).
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# GemaDatabase
# =============================================================================

class GemaDatabase:
    """
    Primary interface to the GEMA SQLite database.
    All public methods manage their own connection — safe for Streamlit threads.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or config.GEMA_DB_PATH
        self._initialize()

    def _initialize(self) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute(_DDL_SEEN_REGISTRY)
            conn.execute(_DDL_SEARCH_VAULT)
            conn.execute(_DDL_MANUAL_REVIEW)  # Phase 30a
            conn.execute(_DDL_RUNS)           # v3 Fase 4
            conn.execute(_DDL_NODE_TRACES)    # v3 Fase 4
            conn.execute(_DDL_LLM_CALLS)      # v3 Fase 4
            conn.execute(_DDL_EVAL_RESULTS)   # v3 Fase 7
            conn.execute(_DDL_JOB_LINKS)           # Fase 11
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_links_dup "
                "ON job_links(hash_duplicado)")
            conn.execute(_DDL_APPLICATIONS)        # Fase 7 (ingesta/scoring)
            conn.execute(_DDL_APPLICATION_EVENTS)  # Fase 7
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_app_events_app "
                "ON application_events(app_id)")
            for idx_sql in _DDL_INDEXES:
                conn.execute(idx_sql)
            # Migrations: add columns to existing DBs that predate these fields.
            # FIX 2 live-test 07-18: salary_raw — el salario solo vivía en los
            # resultados de cada corrida; sin columna no hay diff histórico.
            for mig_col, mig_sql in (
                # v3: output_chars llego despues de llm_calls. Con completion_tokens da
                # el sobrecosto de razonamiento del modelo, que es lo que decide si vale
                # la pena cambiarlo.
                ("output_chars", "ALTER TABLE llm_calls ADD COLUMN output_chars INTEGER"),
                ("location_raw", "ALTER TABLE seen_jobs_registry ADD COLUMN location_raw TEXT"),
                ("salary_raw",   "ALTER TABLE seen_jobs_registry ADD COLUMN salary_raw TEXT"),

                # ── Fase 5 (2026-09-16): el razonamiento deja de morir con la sesión ──
                # Cada aviso puntuado lleva `match_reasons` y `miss_reasons` — la razón
                # exacta de por qué sacó 90 o 38 — y `update_tier` guardaba tier, score
                # y salario. Resultado: cualquier vista que leyera de la base podía decir
                # QUÉ puntaje sacó un aviso y nunca POR QUÉ. El panel de detalle ya sabe
                # mostrarlas; le llegan por /api/results mientras la sesión vive y
                # desaparecen al reiniciar.
                ("match_reasons", "ALTER TABLE seen_jobs_registry ADD COLUMN match_reasons TEXT"),
                ("miss_reasons",  "ALTER TABLE seen_jobs_registry ADD COLUMN miss_reasons TEXT"),

                # Los veredictos de la Fase 3. Sin persistirlos no se puede responder
                # cuántos Tier 1 se perdieron por elegibilidad y no por calidad, que es
                # exactamente la pregunta que esa fase existe para poder contestar.
                ("location_verdict", "ALTER TABLE seen_jobs_registry ADD COLUMN location_verdict TEXT"),
                ("location_note",    "ALTER TABLE seen_jobs_registry ADD COLUMN location_note TEXT"),
                ("language_verdict", "ALTER TABLE seen_jobs_registry ADD COLUMN language_verdict TEXT"),
                ("language_note",    "ALTER TABLE seen_jobs_registry ADD COLUMN language_note TEXT"),

                # Riesgo R6 del plan, con dos fases de deuda encima. Las Fases 3 y 4
                # cambiaron cómo se puntúa, así que un score guardado el 01-09 y uno
                # guardado hoy NO son comparables. Sin esta columna no hay forma de
                # saberlo mirando la fila, y comparar dos números incomparables es
                # peor que no tener ninguno.
                ("score_version", "ALTER TABLE seen_jobs_registry ADD COLUMN score_version TEXT"),

                # ── Fase 6 (2026-09-16): fecha limite y barrido de vencidos ──
                # 1.040 de 1.574 filas (66%) tienen mas de 60 dias y se tratan como si
                # estuvieran vivas. `deadline` guarda lo que la fuente publica; `expired_at`
                # guarda cuando el barrido la dio por vencida. Dos columnas y no un booleano:
                # "vencio" y "cuando me entere" son datos distintos, y el segundo es el que
                # permite auditar el barrido.
                ("deadline",   "ALTER TABLE seen_jobs_registry ADD COLUMN deadline TEXT"),
                ("expired_at", "ALTER TABLE seen_jobs_registry ADD COLUMN expired_at TEXT"),

                # ── , Fase 7: la descripcion del aviso ──
                # Sin guardarla, re-puntuar el registry (tools/repuntuar_registry.py) no
                # veria los requisitos y bajaria a v7 sin el eje: el score de la corrida y
                # el del registry dirian cosas distintas del mismo aviso. Texto limpio con
                # tope de 12.000 (models.DESCRIPCION_MAX_CHARS), nunca HTML. Las filas
                # viejas quedan en NULL: se puntuan sin el eje, como un aviso sin salario.
                ("descripcion", "ALTER TABLE seen_jobs_registry ADD COLUMN descripcion TEXT"),

                # ── revision externa 2026-09-30, punto 9: runs huerfanos ──
                # Cuando el servidor muere a mitad de una corrida, su fila queda en
                # `running` para siempre. Al arrancar se reconcilian a `interrupted`:
                # la causa va en `error` (columna que ya existe para eso) y la hora en
                # que se DETECTO va aca. No se toca `completed_at`: la corrida nunca
                # termino, y un timestamp inventado haria mentir cualquier duracion.
                # Aditiva: la app vieja ignora la columna, rollback sin migracion inversa.
                ("interrupted_at", "ALTER TABLE runs ADD COLUMN interrupted_at TEXT"),
            ):
                try:
                    conn.execute(mig_sql)
                    logger.info("Migration: added %s column (%s)", mig_col, mig_sql)
                except Exception as exc:
                    # AUDIT #7: distinguish the expected case (column already exists) from
                    # a real ALTER failure (lock, disk, permission). Swallowing the latter
                    # lets every subsequent INSERT fail silently — scream instead.
                    if "duplicate column" in str(exc).lower():
                        pass  # expected — column already exists on a migrated DB
                    else:
                        logger.error("[DB MIGRATION] ALTER seen_jobs_registry failed unexpectedly: %s", exc)
                        raise

            # ── Fase 9 (2026-09-30): la próxima acción de una postulación ──
            # `follow_up_at` ya existía y no tenía texto: una fecha sin «qué hacer ese
            # día» es una alarma sin mensaje. El DDL de arriba ya la trae para bases
            # nuevas; esto la agrega a las que se crearon en la Fase 7. Aditiva y con el
            # mismo criterio que arriba: «duplicate column» es lo esperado en una base
            # migrada, cualquier otro error grita.
            try:
                conn.execute("ALTER TABLE applications ADD COLUMN next_action TEXT")
                logger.info("Migration: added next_action column to applications")
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    logger.error("[DB MIGRATION] ALTER applications failed unexpectedly: %s", exc)
                    raise

            # ── Fase 4 (2026-09-30): node_traces cuenta avisos ──
            # Aditiva: una base creada antes de hoy tiene la tabla sin estas dos
            # columnas y `CREATE TABLE IF NOT EXISTS` no las agrega. Las filas viejas
            # quedan con NULL —"no medido"—, nunca con 0: un cero inventado por la
            # migración marcaría como vacía a toda fuente que corrió antes de hoy.
            for mig_col, mig_sql in (
                ("items",     "ALTER TABLE node_traces ADD COLUMN items INTEGER"),
                ("aceptados", "ALTER TABLE node_traces ADD COLUMN aceptados INTEGER"),
            ):
                try:
                    conn.execute(mig_sql)
                    logger.info("Migration: added %s column to node_traces", mig_col)
                except Exception as exc:
                    if "duplicate column" in str(exc).lower():
                        pass  # esperado — la columna ya existe
                    else:
                        logger.error("[DB MIGRATION] ALTER node_traces failed unexpectedly: %s", exc)
                        raise
        logger.info("Database initialized: %s", self.db_path)

    def backup(self) -> str:
        """
        Creates an atomic, WAL-safe backup using SQLite's native backup API.

        WHY shutil.copy2 IS WRONG WITH WAL MODE:
            When WAL mode is active, SQLite uses three files:
                gema_registry.db       — main database
                gema_registry.db-wal   — write-ahead log (uncommitted pages)
                gema_registry.db-shm   — shared memory index

            shutil.copy2 copies only the .db file at filesystem level.
            If transactions are pending in the -wal file at the moment of
            copy, those pages are NOT in the .db file yet — the backup
            is a corrupt, partially-written database that cannot be read.

        WHY src_conn.backup(dst_conn) IS CORRECT:
            SQLite's C-level backup API (exposed in Python as conn.backup())
            checkpoints the WAL, then copies page-by-page while holding a
            shared lock. The result is a consistent, self-contained .db file
            that includes all committed transactions — even those still in
            the -wal file. This is the only safe backup method for WAL mode.

        Vol 1.4 Risk 4.1 mitigation.
        Returns the backup path, or empty string if db does not exist yet.
        """
        src_path = Path(self.db_path)
        if not src_path.exists():
            logger.warning("Backup skipped — database file does not exist yet.")
            return ""

        backup_path = str(src_path) + config.DB_BACKUP_SUFFIX
        # timeout=5000ms: waits for the writer task to release its lock rather
        # than raising OperationalError immediately under concurrent write load.
        src_conn = sqlite3.connect(self.db_path, timeout=5.0)
        dst_conn = sqlite3.connect(backup_path, timeout=5.0)
        try:
            # Mirror the WAL + foreign-keys pragmas from _get_connection() so
            # the backup connection reads a fully consistent WAL checkpoint.
            src_conn.execute("PRAGMA journal_mode=WAL")
            src_conn.execute("PRAGMA foreign_keys=ON")
            src_conn.backup(dst_conn)
            logger.info("WAL-safe backup created: %s", backup_path)
        finally:
            dst_conn.close()
            src_conn.close()
        return backup_path

    # =========================================================================
    # Idempotency Core — SHA-256 Seen Registry
    # =========================================================================

    @staticmethod
    def compute_hash(url: str) -> str:
        """
        SHA-256 of the canonical URL — the idempotency key.

        NORMALIZATION PIPELINE (order matters):
            1. Strip whitespace
            2. Lowercase scheme + netloc
            3. Strip trailing slash from path
            4. DROP query string entirely  ← Fix 1 (audit)
            5. DROP fragment (#anchor)

        WHY DROP QUERY PARAMS:
            Job boards append tracking parameters to every URL:
                example.com/job/123?ref=newsletter&utm_source=email
                example.com/job/123?ref=homepage&utm_campaign=daily
            These are the SAME job. The previous implementation only
            did str.lower() + str.rstrip('/'), producing two different
            hashes for the same posting — Delta Load failed silently.

            urllib.parse.urlunparse with empty query and fragment ensures
            the canonical URL is deterministic regardless of how the job
            board decorated the link at the time of scraping.

        SHA-256 collision probability at 1M URLs: ~2.9e-68.
        """
        stripped = url.strip()
        parsed = urlparse(stripped)
        canonical = urlunparse((
            parsed.scheme.lower(),  # normalize scheme
            parsed.netloc.lower(),  # normalize host
            parsed.path.rstrip("/"),  # strip trailing slash
            "",                     # drop params (;key=val)
            "",                     # drop query string (?ref=...)
            "",                     # drop fragment (#section)
        ))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_seen(self, url: str, ttl_hours: int = 0) -> tuple[bool, str]:
        """
        Single-URL seen check. Returns (is_seen, reason).

        ttl_hours=0  → permanent: skip forever once seen
        ttl_hours=N  → skip only if seen within last N hours
        """
        job_hash = self.compute_hash(url)
        with _get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT scraped_at FROM seen_jobs_registry WHERE job_hash = ?",
                (job_hash,),
            ).fetchone()

        if row is None:
            return False, ""

        if ttl_hours <= 0:
            return True, "permanent_registry"

        scraped_at = datetime.fromisoformat(row["scraped_at"])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        if scraped_at >= cutoff:
            return True, f"within_ttl_{ttl_hours}h"

        return False, ""  # Outside TTL — re-eligible

    def filter_new_urls(
        self, urls: list[str], ttl_hours: int = 0
    ) -> tuple[list[str], int, int]:
        """
        Batch Delta Load filter — single SQL query for the entire URL batch.

        Returns: (new_urls, skipped_permanent_count, skipped_ttl_count)

        More efficient than calling is_seen() in a loop for large batches.
        """
        if not urls:
            return [], 0, 0

        hash_to_url = {self.compute_hash(u): u for u in urls}
        all_hashes = list(hash_to_url.keys())

        # SQLite SQLITE_MAX_VARIABLE_NUMBER is 999 by default.
        # Chunking prevents "too many SQL variables" on large batches.
        _CHUNK = 900
        rows = []
        with _get_connection(self.db_path) as conn:
            for i in range(0, len(all_hashes), _CHUNK):
                chunk = all_hashes[i : i + _CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows.extend(
                    conn.execute(
                        f"SELECT job_hash, scraped_at FROM seen_jobs_registry "
                        f"WHERE job_hash IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )

        seen_map = {
            row["job_hash"]: datetime.fromisoformat(row["scraped_at"])
            for row in rows
        }

        new_urls: list[str] = []
        skipped_permanent = 0
        skipped_ttl = 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
            if ttl_hours > 0 else None
        )

        for url in urls:
            h = self.compute_hash(url)
            if h not in seen_map:
                new_urls.append(url)
                continue
            if cutoff and seen_map[h] < cutoff:
                new_urls.append(url)  # Outside TTL — re-eligible
            elif ttl_hours > 0:
                skipped_ttl += 1
            else:
                skipped_permanent += 1

        logger.info(
            "Delta Load: %d total | %d new | %d perm-skip | %d ttl-skip",
            len(urls), len(new_urls), skipped_permanent, skipped_ttl,
        )
        return new_urls, skipped_permanent, skipped_ttl

    def mark_seen(self, job: JobResult) -> None:
        """
        Registers one job immediately after extraction (before matcher runs).
        INSERT OR IGNORE ensures concurrent calls are safe.
        """
        job_hash = self.compute_hash(job.url)
        with _get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at, location_raw,
                    salary_raw, descripcion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_hash, job.url, job.title, job.company,
                    job.source_domain, job.scraped_at.isoformat(), job.location_raw,
                    job.salary_raw, getattr(job, "descripcion", None),
                ),
            )

    def mark_seen_batch(self, jobs: list[JobResult]) -> None:
        """Bulk-registers a list of jobs in a single transaction."""
        records = [
            (
                self.compute_hash(j.url),
                j.url, j.title, j.company,
                j.source_domain, j.scraped_at.isoformat(),
                j.location_raw, j.salary_raw,
                getattr(j, "deadline", None),
                getattr(j, "descripcion", None),
            )
            for j in jobs
        ]
        with _get_connection(self.db_path) as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at,
                    location_raw, salary_raw, deadline, descripcion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )
        logger.info("Batch-registered %d jobs.", len(records))

    def update_tier(self, url: str, tier: str, match_score: int,
                    salary_raw: Optional[str] = None,
                    tiered: Optional[object] = None) -> None:
        """Backfills tier and match_score after the matcher has run.

        FIX 2b live-test 07-18: también backfillea salary_raw si viene — un job
        RE-procesado (hash existente, INSERT OR IGNORE no-op) que ahora sí trae
        salario (FIX 1: extraído del texto) actualiza la fila vieja. COALESCE:
        nunca pisa un salario ya guardado con NULL.

        Fase 5 (2026-09-16): `tiered` es el `TieredJob` completo, opcional. Cuando
        viene, se persisten también las razones del match y los veredictos de
        elegibilidad. Es opcional a propósito — la firma vieja sigue funcionando y
        los llamadores que no tengan el objeto no se rompen.

        Se acepta un `object` y no un `TieredJob` tipado para no importar `models`
        acá: `database` es la capa de abajo y no debería depender de la de arriba.
        Los campos se leen con getattr, así que un objeto sin ellos degrada a NULL
        en vez de explotar.
        """
        razones = _json_o_none(getattr(tiered, "match_reasons", None))
        faltas  = _json_o_none(getattr(tiered, "miss_reasons", None))
        loc_v   = getattr(tiered, "location_verdict", None)
        loc_n   = getattr(tiered, "location_note", None)
        lang_v  = getattr(tiered, "language_verdict", None)
        lang_n  = getattr(tiered, "language_note", None)
        version = getattr(config, "SCORE_VERSION", None) if tiered is not None else None
        # Fase 7: un aviso visto antes de la columna, que vuelve a entrar ahora con
        # descripcion, la completa. COALESCE: nunca se pisa con NULL.
        descripcion = getattr(getattr(tiered, "job", None), "descripcion", None)

        with _get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE seen_jobs_registry SET tier=?, match_score=?, "
                "salary_raw=COALESCE(?, salary_raw), "
                "descripcion=COALESCE(?, descripcion), "
                # COALESCE en los campos nuevos por la misma razón que en salary_raw:
                # un re-procesamiento sin `tiered` no puede borrar lo que una corrida
                # anterior sí averiguó.
                "match_reasons=COALESCE(?, match_reasons), "
                "miss_reasons=COALESCE(?, miss_reasons), "
                "location_verdict=COALESCE(?, location_verdict), "
                "location_note=COALESCE(?, location_note), "
                "language_verdict=COALESCE(?, language_verdict), "
                "language_note=COALESCE(?, language_note), "
                "score_version=COALESCE(?, score_version) "
                "WHERE job_hash=?",
                (tier, match_score, salary_raw, descripcion, razones, faltas,
                 loc_v, loc_n, lang_v, lang_n, version, self.compute_hash(url)),
            )

    # =========================================================================
    # Search Vault (Vol 1.3)
    # =========================================================================

    def save_to_vault(self, entry: SearchVaultEntry) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO search_vault
                   (vault_id, label, config_json, created_at, times_used)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    entry.vault_id, entry.label,
                    entry.config.model_dump_json(),
                    entry.created_at.isoformat(),
                    entry.times_used,
                ),
            )
        logger.info("Vault saved: '%s'", entry.label)

    def load_vault(self) -> list[SearchVaultEntry]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM search_vault ORDER BY created_at DESC"
            ).fetchall()

        entries = []
        for row in rows:
            try:
                entries.append(SearchVaultEntry(
                    vault_id=row["vault_id"],
                    label=row["label"],
                    config=SearchConfig(**json.loads(row["config_json"])),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    times_used=row["times_used"],
                ))
            except Exception as exc:
                logger.warning("Corrupt vault entry %s skipped: %s", row["vault_id"], exc)
        return entries

    def increment_vault_usage(self, vault_id: str) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE search_vault SET times_used = times_used + 1 WHERE vault_id = ?",
                (vault_id,),
            )

    def delete_vault_entry(self, vault_id: str) -> None:
        with _get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM search_vault WHERE vault_id = ?", (vault_id,))

    # =========================================================================
    # Phase 30a — Pila de revisión manual (salvage-to-manual-review)
    # =========================================================================

    @staticmethod
    def compute_review_hash(title: str, company: str, domain: str) -> str:
        """Key de la pila de revisión: hash(title|company|domain) normalizado.
        NO usa URL (estos jobs no tienen una confiable). Idempotente: el mismo
        job rescatado dos veces cae en la misma fila (INSERT OR IGNORE)."""
        canonical = "|".join(
            p.strip().lower() for p in (title or "", company or "", domain or "")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def add_to_manual_review(
        self, job: "JobResult", srp_url: Optional[str], reason: str
    ) -> bool:
        """Rescata un job (title+company reales, URL no verificable) a la pila.
        Retorna True si se insertó, False si ya existía (idempotente).
        SEAM 30b: original_url = la URL mala del LLM; srp_url = la pista."""
        review_hash = self.compute_review_hash(
            job.title, job.company, job.source_domain
        )
        with _get_connection(self.db_path) as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO manual_review
                   (review_hash, title, company, source_domain, original_url,
                    srp_url, reason, captured_at, resolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (review_hash, job.title, job.company, job.source_domain,
                 job.url, srp_url, reason, datetime.now(timezone.utc).isoformat()),
            )
            return cur.rowcount > 0

    def get_manual_review(self, include_resolved: bool = False,
                          limit: int = 500) -> list[dict]:
        """Filas de la pila, pendientes primero, más nuevas primero."""
        query = "SELECT * FROM manual_review"
        if not include_resolved:
            query += " WHERE resolved = 0"
        query += " ORDER BY resolved ASC, captured_at DESC LIMIT ?"
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(query, (max(1, min(int(limit), 1000)),)).fetchall()
        return [dict(r) for r in rows]

    def resolve_manual_review(self, review_hash: str,
                             resolved_url: Optional[str]) -> bool:
        """Marca una fila como resuelta. resolved_url = la URL que el humano
        corrigió (o None si la descartó como no-rescatable). Retorna True si
        actualizó una fila. SEAM 30b: la diferencia original_url↔resolved_url↔
        srp_url es lo que 30b aprenderá por board."""
        with _get_connection(self.db_path) as conn:
            cur = conn.execute(
                """UPDATE manual_review
                   SET resolved = 1, resolved_url = ?, resolved_at = ?
                   WHERE review_hash = ?""",
                (resolved_url, datetime.now(timezone.utc).isoformat(), review_hash),
            )
            return cur.rowcount > 0

    def get_review_stats(self) -> dict:
        with _get_connection(self.db_path) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM manual_review WHERE resolved = 0"
            ).fetchone()[0]
            resolved = conn.execute(
                "SELECT COUNT(*) FROM manual_review WHERE resolved = 1"
            ).fetchone()[0]
        return {"pending": pending, "resolved": resolved}

    # =========================================================================
    # Stats for Streamlit Dashboard
    # =========================================================================

    def get_recent_jobs(self, limit: int = 200, hours: Optional[int] = None) -> list[dict]:
        """Filas crudas del registry, más nuevas primero (F12 — visor de DB).

        Da acceso directo a TODO lo recopilado — incluidas las corridas del
        scheduler, que notifican por Discord pero cuyos resultados no pasan
        por la UI (F14). `hours` filtra a las últimas N horas; None = todo.
        """
        query = "SELECT * FROM seen_jobs_registry"
        params: list = []
        if hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            query += " WHERE scraped_at >= ?"
            params.append(cutoff.isoformat())
        query += " ORDER BY scraped_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # Fase 6 — Barrido de vencidos
    # =========================================================================

    @staticmethod
    def _a_fecha(texto) -> Optional[str]:
        """Una fecha cruda de cualquier fuente a `YYYY-MM-DD`, o None.

        Los formatos varían por fuente y no hay un estándar: Greenhouse manda ISO,
        el RSS de WeWorkRemotely manda RFC-2822 (`Fri, 09 Oct 2026 15:41:42 +0000`).
        Por eso `deadline` se guarda CRUDO y se normaliza al leerlo — reescribir el
        original perdería la única evidencia de qué formato hablaba esa fuente el día
        que se scrapeó, que es justo lo que hace falta cuando la fuente lo cambia.

        Devuelve None ante cualquier cosa que no entienda. Una fecha mal parseada que
        se toma por buena vence avisos vivos, y eso es peor que no barrer nada.

        **Epoch** (, Fase 1): Himalayas manda `expiryDate` como segundos
        Unix (`1792667993` = 2026-10-22), en 20 de 20 avisos medidos. Antes esto
        devolvia None para el string, el float y el entero: el deadline se perdia sin
        que nadie lo notara. Se aceptan solo 10 digitos (segundos, 2001-2286) y 13
        (milisegundos): un numero de otro largo no es una fecha que sepamos leer.
        """
        if not texto:
            return None
        s = str(texto).strip()
        if re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}", s):
            segundos = float(s) / (1000 if len(s) == 13 else 1)
            return datetime.fromtimestamp(segundos, timezone.utc).date().isoformat()
        try:                                   # ISO: 2026-10-09, 2026-10-09T15:41:42Z
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            pass
        try:                                   # RFC-2822: Fri, 09 Oct 2026 15:41:42 +0000
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(s).date().isoformat()
        except Exception:
            return None

    def barrer_vencidos(self) -> dict:
        """Marca como vencidos los avisos cuya fecha límite ya pasó.

        **No hace ninguna llamada de red.** Compara fechas ya guardadas contra hoy, así
        que cuesta una lectura y una escritura, y puede correr en cada arranque sin
        pensarlo. Un barrido que refetchea es un barrido que nadie va a correr seguido,
        y uno que no corre seguido no sirve: el registry tiene 1.040 filas de más de
        60 días tratadas como si estuvieran vivas.

        Devuelve `{"vencidos": n, "cierran_pronto": n, "sin_fecha": n}`.
        """
        hoy = datetime.now(timezone.utc).date()
        en_7 = (hoy + timedelta(days=7)).isoformat()
        hoy_iso = hoy.isoformat()

        vencidos, cierran, sin_fecha = [], 0, 0
        with _get_connection(self.db_path) as conn:
            filas = conn.execute(
                "SELECT job_hash, deadline FROM seen_jobs_registry "
                "WHERE deadline IS NOT NULL AND expired_at IS NULL"
            ).fetchall()
            for f in filas:
                fecha = self._a_fecha(f["deadline"])
                if fecha is None:
                    sin_fecha += 1
                    continue
                if fecha < hoy_iso:
                    vencidos.append((f["job_hash"],))
                elif fecha <= en_7:
                    cierran += 1
            if vencidos:
                ahora = _now_iso()
                conn.executemany(
                    "UPDATE seen_jobs_registry SET expired_at=? WHERE job_hash=?",
                    [(ahora, h) for (h,) in vencidos],
                )

        resultado = {"vencidos": len(vencidos), "cierran_pronto": cierran,
                     "sin_fecha": sin_fecha}
        if vencidos or sin_fecha:
            logger.info("[BARRIDO] %s", resultado)
        return resultado

    def cierran_pronto(self, dias: int = 7, limit: int = 50) -> list[dict]:
        """Avisos vivos cuya fecha límite cae dentro de N días, los más urgentes
        primero. Es lo que convierte una lista de avisos en una lista de prioridades."""
        hoy = datetime.now(timezone.utc).date()
        with _get_connection(self.db_path) as conn:
            filas = conn.execute(
                "SELECT * FROM seen_jobs_registry "
                "WHERE deadline IS NOT NULL AND expired_at IS NULL"
            ).fetchall()
        salida = []
        for f in filas:
            fecha = self._a_fecha(f["deadline"])
            if not fecha:
                continue
            restan = (datetime.fromisoformat(fecha).date() - hoy).days
            if 0 <= restan <= dias:
                d = dict(f)
                d["dias_restantes"] = restan
                salida.append(d)
        salida.sort(key=lambda d: d["dias_restantes"])
        return salida[:limit]

    def update_tiers_batch(self, filas: list[tuple],
                           version: Optional[str] = None) -> int:
        """
        Backfillea tier, score y razonamiento para muchos avisos en UNA transacción.

        Medido el 2026-09-16: `update_tier` cuesta **107 ms por aviso**, consistente
        entre tandas. La causa está en `_get_connection`, que abre una conexión, corre
        `PRAGMA journal_mode=WAL`, commitea y cierra — un fsync por fila. Con las
        Fases 1 y 2 una corrida ya trae 940 avisos, así que eso son ~100 segundos de
        puro escribir contra los 7,7 s que tarda traerlos.

        Una transacción para todos lo vuelve proporcional al disco y no al catálogo.

        `filas` son tuplas `(url, tier, match_score, salary_raw, tiered)` — la misma
        forma que `update_tier`, para que no haya dos maneras de decir lo mismo.
        Devuelve cuántas se escribieron.

        `version` (Fase 10 del ): la identidad del score con que se
        puntuaron estas filas. `None` es `SCORE_VERSION`, que es lo que pasa siempre
        mientras el aprendizaje esté inactivo. `bucket_jobs` la pasa solo cuando el
        aprendizaje movió la escala (`v7+ap.<huella>`).
        """
        if not filas:
            return 0
        if version is None:
            version = getattr(config, "SCORE_VERSION", None)
        params = []
        for url, tier, score, salary_raw, tiered in filas:
            params.append((
                tier, score, salary_raw,
                getattr(getattr(tiered, "job", None), "descripcion", None),   # Fase 7
                _json_o_none(getattr(tiered, "match_reasons", None)),
                _json_o_none(getattr(tiered, "miss_reasons", None)),
                getattr(tiered, "location_verdict", None),
                getattr(tiered, "location_note", None),
                getattr(tiered, "language_verdict", None),
                getattr(tiered, "language_note", None),
                version if tiered is not None else None,
                self.compute_hash(url),
            ))
        with _get_connection(self.db_path) as conn:
            conn.executemany(
                "UPDATE seen_jobs_registry SET tier=?, match_score=?, "
                "salary_raw=COALESCE(?, salary_raw), "
                "descripcion=COALESCE(?, descripcion), "
                "match_reasons=COALESCE(?, match_reasons), "
                "miss_reasons=COALESCE(?, miss_reasons), "
                "location_verdict=COALESCE(?, location_verdict), "
                "location_note=COALESCE(?, location_note), "
                "language_verdict=COALESCE(?, language_verdict), "
                "language_note=COALESCE(?, language_note), "
                "score_version=COALESCE(?, score_version) "
                "WHERE job_hash=?",
                params,
            )
        logger.info("Batch-updated tier for %d jobs.", len(params))
        return len(params)

    def _conn(self):
        """Una conexión al mismo archivo, para consultas de solo lectura de otros
        módulos.

        Existe para que `provider_budget` no tenga que importar `_get_connection`, que
        es privada del módulo. Un accesor con nombre es más honesto que un import de
        algo marcado con guión bajo: si mañana cambia cómo se conecta, cambia acá.
        """
        return _get_connection(self.db_path)

    # =========================================================================
    # Fase 11 — Correlators de identidad
    # =========================================================================

    def correlacionar_duplicados(self, umbral_auto: float = 0.90,
                                 limite: int = 5000) -> dict:
        """
        Corre los correlators sobre el registry y registra lo que encuentran.

        **Auto-acepta solo por encima del umbral.** Mismo board, mismo día, mismo
        título y misma empresa es inequívoco; el resto queda `AVAILABLE` para que lo
        decida un humano. Un correlator que decide solo con poca confianza convierte
        un problema de duplicados en uno de avisos desaparecidos, que es peor.

        Nunca borra nada. El duplicado queda `BLOCKED` apuntando a su canónico: sigue
        en el registry como evidencia de que ese board relista, y la decisión se puede
        revertir. Y nunca toca un par ya decidido —`ACCEPTED` o `REJECTED`— para que
        una corrida nueva no pise lo que un humano resolvió.
        """
        import correlators as corr

        with _get_connection(self.db_path) as conn:
            # Ubicacion, salario, fecha de cierre y vencimiento (, Fase 2):
            # los usa el correlator entre fuentes para elegir que copia queda visible, y
            # `correr()` para no fundir ubicaciones incompatibles para la ciudad del candidato.
            filas = [dict(r) for r in conn.execute(
                "SELECT url, title, company, source_domain, scraped_at, location_raw, "
                "salary_raw, deadline, expired_at "
                "FROM seen_jobs_registry ORDER BY scraped_at DESC LIMIT ?",
                (limite,)).fetchall()]
            # Todo par ya registrado, en CUALQUIER estado. Antes solo se salteaban
            # ACCEPTED/REJECTED, y los AVAILABLE se re-insertaban en cada corrida con
            # INSERT OR REPLACE: medido el 2026-09-28 sobre la base real, tres corridas
            # seguidas reportaron "nuevos: 119" las tres veces, y `creado_en` se
            # reseteaba — se perdia cuanto hace que una decision espera a un humano.
            ya_decididos = {
                (r["job_hash"], r["hash_duplicado"])
                for r in conn.execute(
                    "SELECT job_hash, hash_duplicado FROM job_links").fetchall()
            }

        candidatos = corr.correr(filas)
        ahora = _now_iso()
        nuevos, auto, a_revisar = 0, 0, 0
        registros = []
        for c in candidatos:
            h_can = self.compute_hash(c.canonico_url)
            h_dup = self.compute_hash(c.duplicado_url)
            if h_can == h_dup or (h_can, h_dup) in ya_decididos:
                continue
            estado = corr.ACCEPTED if c.confianza >= umbral_auto else corr.AVAILABLE
            registros.append((h_can, h_dup, c.correlator, c.confianza, c.motivo,
                              estado, ahora if estado == corr.ACCEPTED else None,
                              ahora))
            nuevos += 1
            if estado == corr.ACCEPTED:
                auto += 1
            else:
                a_revisar += 1

        if registros:
            with _get_connection(self.db_path) as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO job_links "
                    "(job_hash, hash_duplicado, correlator, confianza, motivo, "
                    " estado, decidido_en, creado_en) VALUES (?,?,?,?,?,?,?,?)",
                    registros)

        reabiertos = self._reabrir_fusiones_incompatibles()

        resultado = {"candidatos": len(candidatos), "nuevos": nuevos,
                     "auto_aceptados": auto, "a_revisar": a_revisar,
                     "reabiertos": reabiertos}
        if nuevos or reabiertos:
            logger.info("[CORRELATOR] %s", resultado)
        return resultado

    def _reabrir_fusiones_incompatibles(self) -> int:
        """
        Devuelve a AVAILABLE los pares que el SISTEMA acepto solo y que funden dos
        ubicaciones incompatibles para la ciudad del candidato. , Fase 2.

        Medido el 2026-09-30: 5 pares asi en la base real, todos de
        `titulo_empresa_dia`, aceptados antes de que existiera la regla. Uno escondia
        la variante "Latin America" de un aviso detras de su gemela "Europe".
        `correlacionar_duplicados` no vuelve a mirar un par ya registrado, asi que sin
        esto la regla nueva no los alcanzaba nunca.

        **Solo toca lo que decidio el sistema**: un auto-aceptado se graba con
        `decidido_en == creado_en` (el mismo `ahora`), y una decision humana
        (`decidir_correlator`) siempre es posterior a la creacion. Lo que decidio
        el usuario no se pisa. Idempotente: un par reabierto ya no es ACCEPTED.
        """
        import correlators as corr
        with _get_connection(self.db_path) as conn:
            pares = conn.execute(
                "SELECT l.job_hash, l.hash_duplicado, l.motivo, "
                "       a.location_raw AS loc_a, b.location_raw AS loc_b "
                "FROM job_links l "
                "JOIN seen_jobs_registry a ON a.job_hash = l.job_hash "
                "JOIN seen_jobs_registry b ON b.job_hash = l.hash_duplicado "
                "WHERE l.estado = ? AND l.decidido_en = l.creado_en",
                (corr.ACCEPTED,)).fetchall()
            cambios = [
                (corr.AVAILABLE, corr.CONFIANZA_UBICACION_INCOMPATIBLE,
                 f"{p['motivo'] or ''} — reabierto: ubicaciones distintas para la ciudad del candidato "
                 f"({p['loc_a']!r} vs {p['loc_b']!r})",
                 p["job_hash"], p["hash_duplicado"], corr.ACCEPTED)
                for p in pares
                if not corr.compatibles_para_lima(p["loc_a"], p["loc_b"])
            ]
            if cambios:
                conn.executemany(
                    "UPDATE job_links SET estado=?, confianza=?, motivo=?, "
                    "decidido_en=NULL WHERE job_hash=? AND hash_duplicado=? AND estado=?",
                    cambios)
        return len(cambios)

    def correlators_pendientes(self) -> list[dict]:
        """
        Los pares AVAILABLE, con los datos de LOS DOS avisos para decidir sin abrir
        nada. Los de mayor confianza primero: son los que mas rapido se resuelven.

        Hasta el 2026-09-28 la unica forma de resolverlos era SQL a mano, y habia 119
        esperando.
        """
        import correlators as corr
        with _get_connection(self.db_path) as conn:
            filas = conn.execute(
                """SELECT l.job_hash, l.hash_duplicado, l.correlator, l.confianza,
                          l.motivo, l.creado_en,
                          c.title AS can_title, c.company AS can_company,
                          c.source_domain AS can_board, c.scraped_at AS can_fecha,
                          c.url AS can_url, c.location_raw AS can_ubicacion,
                          d.title AS dup_title, d.company AS dup_company,
                          d.source_domain AS dup_board, d.scraped_at AS dup_fecha,
                          d.url AS dup_url, d.location_raw AS dup_ubicacion
                   FROM job_links l
                   LEFT JOIN seen_jobs_registry c ON c.job_hash = l.job_hash
                   LEFT JOIN seen_jobs_registry d ON d.job_hash = l.hash_duplicado
                   WHERE l.estado = ?
                   ORDER BY l.confianza DESC, l.creado_en""",
                (corr.AVAILABLE,)).fetchall()
        return [dict(r) for r in filas]

    def decidir_correlator(self, job_hash: str, hash_duplicado: str,
                           decision: str) -> dict:
        """
        Un humano resuelve un par: AVAILABLE -> ACCEPTED ("es el mismo aviso") o
        AVAILABLE -> REJECTED ("son distintos").

        Solo desde AVAILABLE. Un par que ya se decidio no se re-decide desde aca: si
        la UI mandara dos clics seguidos, el segundo tiene que fallar con el estado
        actual y no convertir un REJECTED en ACCEPTED en silencio. Sin "deshacer" en
        esta version, a proposito: agregarlo es una decision de producto, no un bug.

        Levanta LookupError si el par no existe y ValueError si la transicion no es
        legal; api.py los traduce a 404 y 409.
        """
        import correlators as corr
        if decision not in (corr.ACCEPTED, corr.REJECTED):
            raise ValueError(
                f"Decision invalida {decision!r}. Se puede decidir: "
                f"{corr.ACCEPTED} (es el mismo aviso) o {corr.REJECTED} (son distintos).")
        with _get_connection(self.db_path) as conn:
            fila = conn.execute(
                "SELECT estado FROM job_links WHERE job_hash=? AND hash_duplicado=?",
                (job_hash, hash_duplicado)).fetchone()
            if fila is None:
                raise LookupError("No existe ese par de avisos.")
            if fila["estado"] != corr.AVAILABLE:
                raise ValueError(
                    f"Ese par ya esta decidido como {fila['estado']}. Solo se puede "
                    f"decidir un par {corr.AVAILABLE}.")
            ahora = _now_iso()
            # El WHERE repite el estado: si dos requests llegan juntos, el segundo
            # no encuentra fila que actualizar en vez de pisar al primero.
            cur = conn.execute(
                "UPDATE job_links SET estado=?, decidido_en=? "
                "WHERE job_hash=? AND hash_duplicado=? AND estado=?",
                (decision, ahora, job_hash, hash_duplicado, corr.AVAILABLE))
            if cur.rowcount != 1:
                raise ValueError("Ese par se decidio mientras tanto. Recarga la lista.")
        return {"job_hash": job_hash, "hash_duplicado": hash_duplicado,
                "estado": decision, "decidido_en": ahora}

    def hashes_bloqueados(self) -> set:
        """Los `job_hash` que perdieron contra un canónico. `bucket_jobs` los saca
        del shortlist (conectado el 2026-09-28: hasta entonces nadie los leia); siguen
        en la base."""
        with _get_connection(self.db_path) as conn:
            import correlators as corr
            return {r[0] for r in conn.execute(
                "SELECT hash_duplicado FROM job_links WHERE estado=?",
                (corr.ACCEPTED,)).fetchall()}

    def duplicados_de(self, url: str) -> list[dict]:
        """Los duplicados registrados de un aviso, con su correlator y confianza."""
        with _get_connection(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM job_links WHERE job_hash=? ORDER BY confianza DESC",
                (self.compute_hash(url),)).fetchall()]

    def hashes_vencidos(self) -> set:
        """
        Los `job_hash` que el barrido de la Fase 6 ya dio por vencidos.

        `barrer_vencidos()` los marca y hasta hoy nadie leía la marca: un aviso
        cerrado que volvía a aparecer en una corrida entraba al shortlist como
        cualquier otro. «Venció» es otra forma de «no podés postular», que es
        exactamente el criterio de elegibilidad de la Fase 3.
        """
        try:
            with _get_connection(self.db_path) as conn:
                return {
                    r[0] for r in conn.execute(
                        "SELECT job_hash FROM seen_jobs_registry WHERE expired_at IS NOT NULL"
                    )
                }
        except Exception as exc:
            logger.warning("[DB] hashes_vencidos fallo: %s", exc)
            return set()

    def hashes_puntuados(self, version: Optional[str] = None) -> set:
        """
        Los `job_hash` que ya tienen tier, para que `bucket_jobs` no los repita.

        **La versión importa.** Un aviso puntuado con `v1` tiene que volver a pasar:
        las Fases 3 y 4 cambiaron cómo se puntúa —la ubicación salió del score, las
        dimensiones se renormalizaron— y ese número ya no es comparable con los
        nuevos. Saltearlo dejaría la base con dos escalas mezcladas y sin forma de
        distinguirlas al ordenar.

        Con `version=None` devuelve todo lo puntuado, sin importar con qué algoritmo.
        """
        q = "SELECT job_hash FROM seen_jobs_registry WHERE tier IS NOT NULL"
        params: list = []
        if version is not None:
            q += " AND score_version = ?"
            params.append(version)
        with _get_connection(self.db_path) as conn:
            return {r[0] for r in conn.execute(q, params).fetchall()}

    # =========================================================================
    # Fase 7 — Postulaciones
    # =========================================================================

    def crear_postulacion(self, url: str, *, status: str = "postulado",
                          notes: Optional[str] = None,
                          follow_up_at: Optional[str] = None,
                          next_action: Optional[str] = None) -> dict:
        """
        Registra una postulación contra un aviso del registry.

        El default es `postulado` y no `borrador` porque registrar una postulación
        normalmente significa que ya aplicaste — el botón de la UI dice «Apliqué».
        `borrador` es el caso especial y se pide explícito. Un default distinto acá
        que en el endpoint sería la misma acción con dos significados.

        El tier y el score se copian **al momento de aplicar**, no se leen después.
        El matcher cambia —las Fases 3 y 4 ya lo cambiaron dos veces— y la pregunta
        que esta tabla existe para contestar es «el score que yo vi cuando apliqué,
        ¿predijo algo?». Leerlo después contestaría otra cosa.

        Idempotente por aviso: si ya hay una postulación para esa URL, la devuelve
        sin tocarla. Postularse dos veces al mismo puesto es un error de la persona,
        y la base no debería ayudarlo a pasar desapercibido.
        """
        import uuid
        import applications as app

        if status not in app.ESTADOS:
            raise app.TransicionInvalida(f"Estado desconocido: {status!r}")
        # Se valida ANTES de abrir la conexión: una fecha o un texto inválidos no pueden
        # dejar una postulación a medias. Lanzan ValueError (la API lo traduce a 422).
        follow_up_at = normalizar_fecha_accion(follow_up_at)
        next_action = normalizar_proxima_accion(next_action)

        job_hash = self.compute_hash(url)
        ahora = _now_iso()

        with _get_connection(self.db_path) as conn:
            ya = conn.execute(
                "SELECT * FROM applications WHERE job_hash=?", (job_hash,)
            ).fetchone()
            if ya:
                return dict(ya)

            aviso = conn.execute(
                "SELECT title, company, source_domain, tier, match_score "
                "FROM seen_jobs_registry WHERE job_hash=?", (job_hash,)
            ).fetchone()

            app_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO applications
                   (app_id, job_hash, url, title, company, source_domain,
                    tier_al_aplicar, score_al_aplicar, status, created_at,
                    stage_changed_at, applied_at, follow_up_at, notes, next_action)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (app_id, job_hash, url,
                 aviso["title"] if aviso else None,
                 aviso["company"] if aviso else None,
                 aviso["source_domain"] if aviso else None,
                 aviso["tier"] if aviso else None,
                 aviso["match_score"] if aviso else None,
                 status, ahora, ahora,
                 ahora if status != app.BORRADOR else None,
                 follow_up_at, notes, next_action),
            )
            conn.execute(
                "INSERT INTO application_events (app_id, desde, hasta, ocurrio_en, nota) "
                "VALUES (?,?,?,?,?)",
                (app_id, None, status, ahora, notes),
            )
            fila = conn.execute(
                "SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        logger.info("[APP] Postulacion %s creada en %s", app_id[:8], status)
        return dict(fila)

    def mover_postulacion(self, app_id: str, hasta: str,
                          nota: Optional[str] = None,
                          salary_offered: Optional[int] = None) -> dict:
        """
        Mueve una postulación de estado, validando que la transición sea legal.

        La validación no es defensiva por costumbre: un embudo con saltos de etapa no
        puede decir dónde se cae la gente, que es lo único para lo que sirve un embudo.
        Ver `applications.transiciones_validas` para el porqué de cada regla.

        Cada movimiento deja una fila en `application_events`. `status` dice dónde
        está hoy; el histórico dice por dónde pasó, y son preguntas distintas.
        """
        import applications as app

        with _get_connection(self.db_path) as conn:
            fila = conn.execute(
                "SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
            if fila is None:
                raise KeyError(f"No existe la postulacion {app_id!r}")

            desde = fila["status"]
            app.validar_transicion(desde, hasta)   # lanza si no es legal

            ahora = _now_iso()
            conn.execute(
                "UPDATE applications SET status=?, stage_changed_at=?, "
                "applied_at=COALESCE(applied_at, ?), "
                "closed_at=?, "
                "salary_offered=COALESCE(?, salary_offered), "
                "notes=COALESCE(?, notes) "
                "WHERE app_id=?",
                (hasta, ahora,
                 ahora if hasta != app.BORRADOR else None,
                 ahora if app.es_terminal(hasta) else fila["closed_at"],
                 salary_offered, nota, app_id),
            )
            conn.execute(
                "INSERT INTO application_events (app_id, desde, hasta, ocurrio_en, nota) "
                "VALUES (?,?,?,?,?)",
                (app_id, desde, hasta, ahora, nota),
            )
            nueva = conn.execute(
                "SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        logger.info("[APP] %s: %s -> %s", app_id[:8], desde, hasta)
        return dict(nueva)

    def editar_proxima_accion(self, app_id: str, *, next_action=SIN_CAMBIO,
                              follow_up_at=SIN_CAMBIO) -> dict:
        """
        Cambia la próxima acción de una postulación (texto y/o fecha) sin moverla de
        estado. Fase 9.

        Cada campo es independiente: lo que no se manda no se toca, y mandar `None` o
        un texto vacío lo borra. Hay que poder borrar una fecha sin perder el texto, y
        al revés; con un solo parámetro `None` las dos cosas serían la misma.

        No escribe en `application_events`. Ese histórico registra por qué ESTADOS pasó
        una postulación y la conversión se mide sobre él; una fecha de seguimiento
        movida tres veces no es una transición, y mezclarlas ensuciaría el embudo.

        Se puede editar una postulación cerrada: el dato no se pierde ni se bloquea,
        simplemente no cuenta como vencida (ver `esta_vencida`).

        Lanza `ValueError` si el texto o la fecha no son válidos y `KeyError` si la
        postulación no existe; en ninguno de los dos casos se escribe nada.
        """
        sets: list[str] = []
        params: list = []
        if next_action is not SIN_CAMBIO:
            sets.append("next_action=?")
            params.append(normalizar_proxima_accion(next_action))
        if follow_up_at is not SIN_CAMBIO:
            sets.append("follow_up_at=?")
            params.append(normalizar_fecha_accion(follow_up_at))
        if not sets:
            raise ValueError("no hay nada que cambiar: falta next_action o follow_up_at")

        with _get_connection(self.db_path) as conn:
            existe = conn.execute(
                "SELECT 1 FROM applications WHERE app_id=?", (app_id,)).fetchone()
            if existe is None:
                raise KeyError(f"No existe la postulacion {app_id!r}")
            conn.execute(
                f"UPDATE applications SET {', '.join(sets)} WHERE app_id=?",
                (*params, app_id))
            fila = conn.execute(
                "SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        logger.info("[APP] %s: proxima accion editada", app_id[:8])
        return dict(fila)

    def listar_postulaciones(self, status: Optional[str] = None,
                             limit: int = 500, *, solo_vencidas: bool = False,
                             hoy: Optional[date] = None) -> list[dict]:
        """
        Las postulaciones, con la marca `vencida` calculada por `esta_vencida`.

        `solo_vencidas=True` es la consulta de vencidas de la Fase 9 y devuelve las más
        atrasadas primero: la que lleva más días sin atenderse es la que más se enfría.
        `hoy` existe para que un test fije la fecha; en producción es `hoy_local()`.
        """
        import applications as app

        hoy = hoy or hoy_local()
        donde: list[str] = []
        params: list = []
        if status:
            donde.append("status=?")
            params.append(status)
        if solo_vencidas:
            # Prefiltro barato; el criterio final lo decide `esta_vencida` en Python.
            # Se excluyen los cerrados en SQL para que un montón de rechazadas con fecha
            # vieja no ocupen el LIMIT antes de llegar a las abiertas.
            cerrados = sorted(app.TERMINALES)
            donde.append("follow_up_at IS NOT NULL AND follow_up_at <> ''")
            donde.append(f"status NOT IN ({','.join('?' * len(cerrados))})")
            params.extend(cerrados)

        q = "SELECT * FROM applications"
        if donde:
            q += " WHERE " + " AND ".join(donde)
        tope = max(1, min(int(limit), 2000))
        if solo_vencidas:
            # Sin LIMIT en SQL: se corta DESPUÉS de filtrar, o el tope se comería filas
            # que el filtro de Python iba a descartar.
            q += " ORDER BY follow_up_at ASC, stage_changed_at DESC"
        else:
            q += " ORDER BY stage_changed_at DESC LIMIT ?"
            params.append(tope)

        with _get_connection(self.db_path) as conn:
            filas = [dict(r) for r in conn.execute(q, params).fetchall()]

        for f in filas:
            f["vencida"] = esta_vencida(f, hoy)
        if not solo_vencidas:
            return filas

        ilegibles = [f for f in filas if _fecha_de(f["follow_up_at"]) is None]
        if ilegibles:
            # Una fecha escrita antes de que la API la validara («la semana que viene»)
            # no se puede comparar y no va a avisar nunca. Se nombra en el log en vez de
            # callarla: el aviso que no llega es el que nadie echa de menos.
            logger.warning("[APP] %d postulacion(es) abiertas con follow_up_at ilegible "
                           "no pueden vencer nunca: %s", len(ilegibles),
                           [f["app_id"][:8] for f in ilegibles[:5]])
        return [f for f in filas if f["vencida"]][:tope]

    def vencidas(self, hoy: Optional[date] = None) -> list[dict]:
        """Las postulaciones abiertas con la próxima acción atrasada. Fase 9."""
        return self.listar_postulaciones(solo_vencidas=True, limit=2000, hoy=hoy)

    def historial_postulacion(self, app_id: str) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM application_events WHERE app_id=? ORDER BY id",
                (app_id,)).fetchall()]

    def conversion_por(self, dimension: str = "tier_al_aplicar") -> list[dict]:
        """
        Tasa de conversión agrupada por tier, board o rango de score.

        **Esta es la métrica que justifica toda la fase.** Convierte tu propio
        historial en ground truth para calibrar el matcher, que hoy se calibra a ojo
        y cuyo corpus de evaluación sigue con dos casos sintéticos.

        «Convirtió» significa que llegó a `screening` o más lejos — ver
        `applications.CONVIRTIO`. Un `postulado` sin respuesta no dice nada sobre el
        aviso, solo sobre cuántos candidatos había.

        Se mide sobre el HISTÓRICO, no sobre el estado actual: una postulación
        rechazada después de una técnica convirtió, aunque hoy figure `rechazado`.
        Mirar solo el estado final contaría eso como un fracaso del matcher, que es
        exactamente al revés.
        """
        import applications as app

        permitidas = {"tier_al_aplicar", "source_domain", "rango_score"}
        if dimension not in permitidas:
            raise ValueError(f"dimension debe ser una de {sorted(permitidas)}")

        with _get_connection(self.db_path) as conn:
            apps = conn.execute("SELECT * FROM applications").fetchall()
            eventos = conn.execute(
                "SELECT app_id, hasta FROM application_events").fetchall()

        alcanzados: dict[str, set] = {}
        for e in eventos:
            alcanzados.setdefault(e["app_id"], set()).add(e["hasta"])

        grupos: dict[str, list[int]] = {}
        for a in apps:
            if dimension == "rango_score":
                s = a["score_al_aplicar"]
                clave = "sin score" if s is None else f"{(s // 10) * 10}-{(s // 10) * 10 + 9}"
            else:
                clave = a[dimension] or "sin dato"
            convirtio = bool(alcanzados.get(a["app_id"], set()) & app.CONVIRTIO)
            grupos.setdefault(clave, []).append(1 if convirtio else 0)

        salida = [
            {"grupo": k, "postulaciones": len(v), "convirtieron": sum(v),
             "tasa": round(sum(v) / len(v), 3) if v else 0.0}
            for k, v in grupos.items()
        ]
        salida.sort(key=lambda d: (-d["tasa"], -d["postulaciones"]))
        return salida

    # =========================================================================
    # , Fase 10 — aprender de las postulaciones
    # =========================================================================

    def postulaciones_con_estados(self) -> list[dict]:
        """
        Cada postulación con título, empresa y TODOS los estados por los que pasó.

        Es lo único que `aprendizaje.construir` necesita. El conjunto `estados` sale
        de `application_events` más el `status` actual: el histórico dice que hubo una
        entrevista aunque hoy figure `rechazado`, y sumar el status cubre una fila
        cuyo evento de alta se hubiera perdido. Mismo criterio que `conversion_por`.

        Sin columnas nuevas: título y empresa ya se copian del registry al aplicar.
        """
        with _get_connection(self.db_path) as conn:
            apps = conn.execute(
                "SELECT app_id, title, company, source_domain, status "
                "FROM applications").fetchall()
            eventos = conn.execute(
                "SELECT app_id, hasta FROM application_events").fetchall()

        alcanzados: dict[str, set] = {}
        for e in eventos:
            alcanzados.setdefault(e["app_id"], set()).add(e["hasta"])
        return [
            {"app_id": a["app_id"], "title": a["title"], "company": a["company"],
             "source_domain": a["source_domain"], "status": a["status"],
             "estados": sorted(alcanzados.get(a["app_id"], set()) | {a["status"]})}
            for a in apps
        ]

    def version_vigente(self) -> str:
        """
        La versión con la que se escribe y se cuenta el registry hoy.

        `SCORE_VERSION` mientras el aprendizaje esté inactivo, que es hoy y hasta las
        15 postulaciones con resultado. Activo y con reglas que mueven puntos:
        `SCORE_VERSION+ap.<huella>`. El dashboard, el atajo de `hashes_puntuados` y
        `tools/repuntuar_registry.py` tienen que hablar la misma escala que escribe
        `bucket_jobs`; si no, el día que se active el aprendizaje el tablero mostraría
        cero avisos en la escala vigente.

        Nunca lanza: si el aprendizaje no se puede leer, la versión es la base.
        """
        base = getattr(config, "SCORE_VERSION", None)
        try:
            import aprendizaje   # diferido: aprendizaje no depende de database
            return aprendizaje.cargar(self).version(base) or base
        except Exception as exc:   # noqa: BLE001
            logger.warning("[APRENDIZAJE] version vigente no calculable (%s): uso %s",
                           exc, base)
            return base

    def filas_fuera_de_version(self, version: str) -> list[dict]:
        """
        Las filas del registry que NO estan puntuadas con `version`: lo que hay que
        re-puntuar para que el registry hable una sola escala.

        Existe porque subir SCORE_VERSION no alcanza. El 2026-09-19 se asumio que la
        proxima corrida re-puntuaba todo el registry, y la corrida del 29-09 lo
        desmintio: los avisos ya vistos se descartan en el scrape (seen-skip=5868) y
        nunca llegan a bucket_jobs. Solo se puntuaron con v3 los 657 nuevos.

        Trae exactamente los campos que lee el matcher —titulo, empresa, url, salario,
        ubicacion, board y, desde la Fase 7, descripcion— asi que re-puntuar desde aca
        da el mismo resultado que puntuar el aviso en vivo.
        """
        with _get_connection(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT url, title, company, salary_raw, location_raw, source_domain, "
                "       deadline, scraped_at, score_version, descripcion "
                "FROM seen_jobs_registry "
                "WHERE score_version IS NULL OR score_version <> ?", (version,)).fetchall()]

    def get_registry_stats(self) -> dict:
        # Fase 10 (): la escala que escribe bucket_jobs. Es SCORE_VERSION
        # mientras el aprendizaje este inactivo; activo, SCORE_VERSION+ap.<huella>.
        vigente = self.version_vigente()
        with _get_connection(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry"
            ).fetchone()[0]
            # Solo la version vigente. Hasta el 2026-09-29 se sumaban tres escalas
            # (sin version, v2, v3) como si fueran una: el "Tier 1 = 304" del 19-09 era
            # 274 de una escala mas 30 de otra. Lo que esta en otra version se cuenta
            # aparte, en `fuera_de_escala`, en vez de desaparecer.
            by_tier = conn.execute(
                "SELECT tier, COUNT(*) as cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL AND score_version = ? GROUP BY tier",
                (vigente,),
            ).fetchall()
            fuera = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE tier IS NOT NULL "
                "AND (score_version IS NULL OR score_version <> ?)",
                (vigente,),
            ).fetchone()[0]
            recent_24h = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE scraped_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
            ).fetchone()[0]

        return {
            "total_seen": total,
            "recent_24h": recent_24h,
            "by_tier": {row["tier"]: row["cnt"] for row in by_tier},
            "score_version": vigente,
            "fuera_de_escala": fuera,
        }

    def get_dashboard_stats(self) -> dict:
        """Agregados para el panel visual (F19) — todo desde la DB, sin LLM.

        Devuelve distribución por tier, por fuente, y volumen por día (14d)
        para que la UI dibuje el board de estadísticas del registry.
        """
        vigente = self.version_vigente()   # Fase 10: misma escala que get_registry_stats
        with _get_connection(self.db_path) as conn:
            # Misma regla que get_registry_stats: una sola escala.
            by_tier = conn.execute(
                "SELECT tier, COUNT(*) AS cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL AND score_version = ? GROUP BY tier",
                (vigente,),
            ).fetchall()
            fuera = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE tier IS NOT NULL "
                "AND (score_version IS NULL OR score_version <> ?)",
                (vigente,),
            ).fetchone()[0]
            by_source = conn.execute(
                "SELECT source_domain, COUNT(*) AS cnt FROM seen_jobs_registry "
                "GROUP BY source_domain ORDER BY cnt DESC LIMIT 20"
            ).fetchall()
            # scraped_at es ISO-8601; los 10 primeros chars = YYYY-MM-DD
            cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            by_day = conn.execute(
                "SELECT substr(scraped_at, 1, 10) AS day, COUNT(*) AS cnt "
                "FROM seen_jobs_registry WHERE scraped_at >= ? "
                "GROUP BY day ORDER BY day",
                (cutoff,),
            ).fetchall()
        return {
            "by_tier": {r["tier"]: r["cnt"] for r in by_tier},
            "score_version": vigente,
            "fuera_de_escala": fuera,
            "by_source": [{"source": r["source_domain"], "count": r["cnt"]} for r in by_source],
            "by_day": [{"day": r["day"], "count": r["cnt"]} for r in by_day],
        }

    # =========================================================================
    # Async-Safe Interface for "The Blitz" (asyncio.gather concurrency)
    # =========================================================================

    def async_filter_new_urls(
        self, urls: list[str], ttl_hours: int = 0
    ) -> tuple[list[str], int, int]:
        """
        Synchronous method safe to call via loop.run_in_executor().

        WHY THIS EXISTS:
            The existing filter_new_urls() is already thread-safe for reads —
            WAL mode allows unlimited concurrent readers with no lock risk.
            This alias makes the intent explicit at the call site in scraper.py:
            it is designed to be dispatched to the thread pool executor, keeping
            disk I/O off the asyncio event loop without blocking it.

            Pattern in scraper.py:
                new_urls, skip_p, skip_t = await loop.run_in_executor(
                    None,
                    self.db.async_filter_new_urls,
                    raw_urls,
                    self.ttl_hours,
                )

        SQLite WAL + multiple readers:
            WAL mode allows any number of readers to operate concurrently
            with each other and with a single writer. The thread pool executor
            may have multiple threads calling this simultaneously — all safe.
        """
        return self.filter_new_urls(urls, ttl_hours)

    def async_mark_seen_batch(self, jobs: list["JobResult"]) -> None:
        """
        Synchronous bulk insert — safe to call via loop.run_in_executor().

        CRITICAL: In "The Blitz" architecture, ONLY the _db_writer_task
        coroutine calls this method, and it does so sequentially (one batch
        at a time). Multiple concurrent callers are architecturally impossible
        because there is exactly one writer task per scrape session.

        This means the run_in_executor() wrapping this call never has
        more than one thread executing it at any moment — zero lock contention
        guaranteed by design, not by timeout tuning.

        WAL BUSY TIMEOUT:
            Set to 5000ms as a safety net for the one theoretical case where
            the Streamlit UI (which also writes to SQLite for vault operations)
            and the writer task overlap. In practice this is < 1ms.
        """
        if not jobs:
            return
        # Ensure WAL busy timeout on the write connection
        records = [
            (
                self.compute_hash(j.url),
                j.url, j.title, j.company,
                j.source_domain, j.scraped_at.isoformat(),
                j.location_raw, j.salary_raw,
            )
            for j in jobs
        ]
        with _get_connection(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.executemany(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at, location_raw, salary_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )

    # =========================================================================
    # v3 Fase 4 — Escritura de telemetría
    #
    # Todos estos métodos son FAIL-SAFE: loguean y siguen. Es la misma disciplina
    # que metrics.emit y por la misma razón — una falla registrando una falla no
    # puede escalar a una falla real. Si escribir una fila de traza pudiera tumbar
    # un scrape, la instrumentación sería un modo de falla nuevo en vez de una
    # herramienta de diagnóstico.
    # =========================================================================

    def record_run_start(
        self,
        run_id: str,
        *,
        trigger: str,
        config_json: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> None:
        """Abre la fila del run. INSERT OR REPLACE: reintentar un run_id no explota."""
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO runs
                       (run_id, started_at, trigger, status, config_json)
                       VALUES (?, ?, ?, 'running', ?)""",
                    (run_id, started_at or _now_iso(), trigger, config_json),
                )
        except Exception as exc:
            logger.warning("[TELEMETRY] record_run_start failed for %s: %s", run_id, exc)

    def record_run_end(
        self,
        run_id: str,
        *,
        status: str,
        summary_json: Optional[str] = None,
        tier_counts: Optional[tuple[int, int, int, int]] = None,
        domains_aborted: Optional[list[str]] = None,
        breaker_state_json: Optional[str] = None,
        error: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        """
        Cierra la fila del run.

        `tier_counts` lleva los CUATRO tiers. ScrapeRunSummary solo carga tres pese a
        que bucket_jobs devuelve cuatro, así que el cuarto se pierde salvo que se lo
        pase explícitamente acá.

        Cerrar una corrida limpia `interrupted_at`: si la reconciliación del arranque
        marcó por error una corrida que seguía viva (otro proceso sobre la misma DB), el
        cierre real gana y no deja una fila `ok` con huella de interrupción.
        """
        t1, t2, t3, t4 = tier_counts or (None, None, None, None)
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    """UPDATE runs SET completed_at = ?, status = ?, summary_json = ?,
                              tier1_count = ?, tier2_count = ?, tier3_count = ?,
                              tier4_count = ?, domains_aborted = ?,
                              breaker_state_json = ?, error = ?,
                              interrupted_at = NULL
                       WHERE run_id = ?""",
                    (
                        completed_at or _now_iso(), status, summary_json,
                        t1, t2, t3, t4,
                        json.dumps(domains_aborted) if domains_aborted is not None else None,
                        # 2000 para el run: es UNA fila por corrida y ahí el traceback
                        # completo sí vale más que el ahorro de espacio.
                        breaker_state_json, _clip(error, 2000), run_id,
                    ),
                )
        except Exception as exc:
            logger.warning("[TELEMETRY] record_run_end failed for %s: %s", run_id, exc)

    def reconcile_orphan_runs(
        self,
        cause: str = ("el proceso terminó antes de cerrar la corrida "
                      "(reinicio del servidor o caída)"),
    ) -> list[dict]:
        """
        Pasa a `interrupted` toda fila de `runs` que sigue en `running`. Se llama UNA vez,
        al arrancar la API.

        Escenario (revision externa 2026-09-30, punto 9): el servidor muere a mitad de un scrape
        —reinicio, OOM, corte de luz—. Los hilos del run son daemon, así que nadie cierra
        la fila: queda `running` para siempre, `/api/results` arranca vacío y nada le
        dice a el usuario si la última corrida terminó, se cortó o nunca existió.

        Decisión: GEMA es un solo proceso (uvicorn --workers 1; el gate de scrape es un
        lock de proceso), así que al arrancar toda fila `running` es huérfana por
        definición. El UPDATE exige `status = 'running'`: si un CLI de otro proceso
        estuviera corriendo en ese instante y se marcara por error, su `record_run_end`
        posterior sobrescribe el estado con el real — el error se autocorrige.

        Fail-safe como el resto de la telemetría: loguea fuerte y devuelve [] — una
        higiene que falla no puede impedir que la API levante. Devuelve las filas
        reconciliadas (run_id, started_at, trigger) para que el caller las muestre.
        """
        try:
            ahora = _now_iso()
            with _get_connection(self.db_path) as conn:
                huerfanas = [dict(r) for r in conn.execute(
                    "SELECT run_id, started_at, trigger FROM runs "
                    "WHERE status = 'running' ORDER BY started_at"
                ).fetchall()]
                if huerfanas:
                    conn.execute(
                        "UPDATE runs SET status = 'interrupted', interrupted_at = ?, "
                        "error = ? WHERE status = 'running'",
                        (ahora, _clip(cause, 2000)),
                    )
        except Exception as exc:
            logger.error("[RECONCILE] no se pudo reconciliar runs huérfanos: %s", exc)
            return []
        if huerfanas:
            logger.warning(
                "[RECONCILE] %d run(s) quedaron en 'running' tras morir el proceso "
                "anterior: marcados 'interrupted' (%s)",
                len(huerfanas), ", ".join(r["run_id"][:8] for r in huerfanas),
            )
            metrics.emit("runs_interrupted_total", len(huerfanas))
        return huerfanas

    def record_node_trace(
        self,
        *,
        node: str,
        run_id: Optional[str] = None,
        graph: Optional[str] = None,
        domain: Optional[str] = None,
        title: Optional[str] = None,
        exec_ms: Optional[float] = None,
        queue_wait_ms: Optional[float] = None,
        status: str = "ok",
        error: Optional[str] = None,
        started_at: Optional[str] = None,
        items: Optional[int] = None,
        aceptados: Optional[int] = None,
    ) -> None:
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO node_traces
                       (run_id, node, graph, domain, title, started_at,
                        exec_ms, queue_wait_ms, status, error, items, aceptados)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, node, graph, domain, title, started_at or _now_iso(),
                     exec_ms, queue_wait_ms, status, _clip(error), items, aceptados),
                )
        except Exception as exc:
            logger.warning("[TELEMETRY] record_node_trace failed for %s: %s", node, exc)

    def record_node_traces_batch(self, rows: list[dict]) -> int:
        """
        Escribe N trazas en UNA transaccion. Devuelve cuantas entraron.

        `record_node_trace` abre una conexion, hace fsync y cierra: 107 ms por fila
        medidos en la Fase 8 sobre este mismo patron. El fan-out del scraper produce
        una traza por (dominio x titulo) —14 boards x 50 titulos son 700 filas— y
        hacerlo de a una agregaria ~75 s a una corrida de 576. La telemetria que
        encarece la corrida que mide se termina apagando, y entonces no mide nada.

        Nunca levanta: una traza perdida no puede tumbar una corrida que ya scrapeo
        bien. Se avisa por log y se sigue.
        """
        if not rows:
            return 0
        try:
            with _get_connection(self.db_path) as conn:
                conn.executemany(
                    """INSERT INTO node_traces
                       (run_id, node, graph, domain, title, started_at,
                        exec_ms, queue_wait_ms, status, error, items, aceptados)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (r.get("run_id"), r.get("node"), r.get("graph"),
                         r.get("domain"), r.get("title"),
                         r.get("started_at") or _now_iso(),
                         r.get("exec_ms"), r.get("queue_wait_ms"),
                         r.get("status", "ok"), _clip(r.get("error")),
                         r.get("items"), r.get("aceptados"))
                        for r in rows
                    ],
                )
            return len(rows)
        except Exception as exc:
            logger.warning("[TELEMETRY] record_node_traces_batch failed (%d filas): %s",
                           len(rows), exc)
            return 0

    def record_llm_calls(
        self,
        calls: list[dict],
        *,
        trace_id: str,
        run_id: Optional[str] = None,
    ) -> None:
        """
        Persiste en lote los registros que produce el subgrafo de proveedores.

        En lote porque una sola resolución puede generar varias llamadas — dos intentos
        de autocorrección contra groq y una a gemini son tres filas del mismo trace_id, y
        leerlas juntas es lo que muestra la profundidad del fallback.
        """
        if not calls:
            return
        now = _now_iso()
        try:
            rows = [
                (
                    run_id, trace_id, c.get("purpose"), c.get("provider"), c.get("model"),
                    c.get("provider_index"), c.get("attempt"), c.get("latency_ms"),
                    c.get("prompt_tokens"), c.get("completion_tokens"),
                    c.get("outcome"), c.get("output_chars"),
                    _clip(c.get("error")), now,
                )
                for c in calls
            ]
            with _get_connection(self.db_path) as conn:
                conn.executemany(
                    """INSERT INTO llm_calls
                       (run_id, trace_id, purpose, provider, model, provider_index,
                        attempt, latency_ms, prompt_tokens, completion_tokens,
                        outcome, output_chars, error, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
        except Exception as exc:
            logger.warning("[TELEMETRY] record_llm_calls failed for trace %s: %s", trace_id, exc)

    def record_eval_results(self, eval_run_id: str, filas: list[dict]) -> None:
        """Persiste una corrida de evaluación completa. Fail-safe como el resto."""
        if not filas:
            return
        now = _now_iso()
        try:
            rows = [
                (eval_run_id, f.get("corpus_case"), f.get("source"),
                 f.get("prompt_version"), f.get("provider"),
                 f.get("jobs_expected"), f.get("jobs_extracted"),
                 f.get("grounded"), f.get("hallucinated"),
                 f.get("fields_complete"), f.get("agreement"),
                 _clip(f.get("error")), now)
                for f in filas
            ]
            with _get_connection(self.db_path) as conn:
                conn.executemany(
                    """INSERT INTO eval_results
                       (eval_run_id, corpus_case, source, prompt_version, provider,
                        jobs_expected, jobs_extracted, grounded, hallucinated,
                        fields_complete, agreement, error, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
        except Exception as exc:
            logger.warning("[EVAL] record_eval_results failed for %s: %s",
                           eval_run_id, exc)

    def get_token_cost_by_purpose(self, run_id: Optional[str] = None) -> list[dict]:
        """
        Cuanto cuesta cada proceso, en tokens y en sobrecosto de razonamiento.

        Es la pregunta que decide si vale la pena cambiar de modelo. `overhead_pct` estima
        cuanto gasta el modelo PENSANDO antes de emitir: se compara completion_tokens
        contra el tamano de la salida util (chars/4 ~ tokens). Un 95% significa que de
        cada 100 tokens facturados, 95 fueron razonamiento y 5 fueron respuesta — y eso se
        paga en CADA llamada, asi que en extract (hasta 3 por corrida) se multiplica.

        Medido el 2026-09-03 con el prompt real: groq gpt-oss-120b 95%, openrouter
        nemotron 91%, gemini flash-lite 43%, cohere 35%, mistral-small 25%.

        LIMITE DE LA HEURISTICA: chars/4 aproxima bien los tokens de JSON, que es denso
        en separadores. Para prosa —la auditoria devuelve 1-2 frases— SOBREestima los
        tokens utiles, y el resultado puede dar NEGATIVO. Un valor negativo significa
        "la estimacion se paso", no "sobrecosto negativo". Sirve para comparar modelos
        en la MISMA tarea; comparar entre tareas de distinto formato no es valido.
        """
        sql = """
            SELECT purpose, provider,
                   COUNT(*)                      AS llamadas,
                   SUM(prompt_tokens)            AS tok_in,
                   SUM(completion_tokens)        AS tok_out,
                   ROUND(AVG(latency_ms))        AS ms_prom,
                   ROUND(100.0 * (1.0 - (SUM(output_chars) / 4.0) /
                         NULLIF(SUM(completion_tokens), 0))) AS overhead_pct
            FROM llm_calls
            WHERE outcome = 'ok'
        """
        params: list = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " GROUP BY purpose, provider ORDER BY tok_out DESC"
        with _get_connection(self.db_path) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_eval_results(self, eval_run_id: str) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM eval_results WHERE eval_run_id = ? ORDER BY id",
                (eval_run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_last_eval_run_id(self, before: Optional[str] = None) -> Optional[str]:
        """
        El eval_run_id más reciente, opcionalmente anterior a otro. Es lo que hace
        posible `--baseline` sin que el humano tenga que copiar un id a mano.
        """
        sql = "SELECT eval_run_id FROM eval_results"
        params: list = []
        if before is not None:
            sql += (" WHERE created_at < (SELECT MIN(created_at) FROM eval_results "
                    "WHERE eval_run_id = ?)")
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with _get_connection(self.db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def get_run(self, run_id: str) -> Optional[dict]:
        """Lee una fila de runs. Devuelve None si no existe."""
        with _get_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_recent_runs(self, limit: int = 20) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_llm_calls(self, *, run_id: Optional[str] = None,
                      trace_id: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM llm_calls"
        params: list = []
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params.append(run_id)
        elif trace_id is not None:
            sql += " WHERE trace_id = ?"
            params.append(trace_id)
        sql += " ORDER BY id"
        with _get_connection(self.db_path) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_node_traces(self, run_id: str) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM node_traces WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # Los tres nodos que LEEN una fuente. Un nodo del grafo (scrape, bucket, push...) no
    # cuenta avisos de ninguna fuente y no entra a estas consultas.
    NODOS_FUENTE = ("scrape_srp", "fuente_endpoint", "fuente_ats")

    def get_items_por_fuente_y_corrida(self) -> list[dict]:
        """
        Una fila por (fuente, corrida), la más nueva primero dentro de cada fuente.

        Es la entrada de la regla de estado de `fuentes_estado`: agrega las ramas CSS de
        un board (51 filas por corrida) a un solo número comparable con el de un
        endpoint (1 fila) o el de una empresa ATS (1 fila).

        `items` es SUM sobre las filas medidas y queda NULL si NINGUNA lo fue. SQLite
        ignora los NULL al sumar, así que una corrida donde solo midieron 3 de 51 ramas
        suma esas 3: `medidas` y `trazas` van aparte para que quien lee sepa cuánta de
        la corrida entra en el número, y una corrida anterior a la columna (todo NULL)
        no se lea como cero.

        Agrupa por `run_id` del grafo, no por fecha: dos corridas el mismo día son dos
        corridas (el 29-09 hubo tres). Sin `run_id` no hay forma de saber a qué corrida
        pertenece una traza, así que esas filas no se cuentan.
        """
        ph = ",".join("?" for _ in self.NODOS_FUENTE)
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                f"""SELECT domain,
                           run_id,
                           MIN(started_at)                              AS inicio,
                           SUM(items)                                   AS items,
                           COUNT(items)                                 AS medidas,
                           COUNT(*)                                     AS trazas,
                           SUM(aceptados)                               AS aceptados,
                           MAX(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS hubo_error,
                           MAX(CASE WHEN node = 'fuente_ats' THEN 3
                                    WHEN node = 'fuente_endpoint' THEN 2
                                    ELSE 1 END)                         AS tipo
                    FROM node_traces
                    WHERE node IN ({ph}) AND domain IS NOT NULL AND run_id IS NOT NULL
                    GROUP BY domain, run_id
                    ORDER BY domain, inicio DESC""",
                self.NODOS_FUENTE,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scrape_session_stats(self, run_id: str) -> dict:
        """
        Returns per-run statistics for the Streamlit live dashboard.
        Called from main.py after the scrape completes to populate metrics.
        """
        with _get_connection(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry"
            ).fetchone()[0]

            by_tier = conn.execute(
                "SELECT tier, COUNT(*) as cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL GROUP BY tier"
            ).fetchall()

            recent_1h = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE scraped_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),),
            ).fetchone()[0]

        return {
            "total_seen":   total,
            "recent_1h":    recent_1h,
            "by_tier":      {row["tier"]: row["cnt"] for row in by_tier},
        }
