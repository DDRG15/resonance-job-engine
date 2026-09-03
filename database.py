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
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlparse, urlunparse

from models import JobResult, SearchConfig, SearchVaultEntry
import config

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
    error         TEXT
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
    error             TEXT,
    created_at        TEXT NOT NULL
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
            for idx_sql in _DDL_INDEXES:
                conn.execute(idx_sql)
            # Migrations: add columns to existing DBs that predate these fields.
            # FIX 2 live-test 07-18: salary_raw — el salario solo vivía en los
            # resultados de cada corrida; sin columna no hay diff histórico.
            for mig_col, mig_sql in (
                ("location_raw", "ALTER TABLE seen_jobs_registry ADD COLUMN location_raw TEXT"),
                ("salary_raw",   "ALTER TABLE seen_jobs_registry ADD COLUMN salary_raw TEXT"),
            ):
                try:
                    conn.execute(mig_sql)
                    logger.info("Migration: added %s column to seen_jobs_registry", mig_col)
                except Exception as exc:
                    # AUDIT #7: distinguish the expected case (column already exists) from
                    # a real ALTER failure (lock, disk, permission). Swallowing the latter
                    # lets every subsequent INSERT fail silently — scream instead.
                    if "duplicate column" in str(exc).lower():
                        pass  # expected — column already exists on a migrated DB
                    else:
                        logger.error("[DB MIGRATION] ALTER seen_jobs_registry failed unexpectedly: %s", exc)
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
                   (job_hash, url, title, company, source_domain, scraped_at, location_raw, salary_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_hash, job.url, job.title, job.company,
                    job.source_domain, job.scraped_at.isoformat(), job.location_raw,
                    job.salary_raw,
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
            )
            for j in jobs
        ]
        with _get_connection(self.db_path) as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO seen_jobs_registry
                   (job_hash, url, title, company, source_domain, scraped_at, location_raw, salary_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )
        logger.info("Batch-registered %d jobs.", len(records))

    def update_tier(self, url: str, tier: str, match_score: int,
                    salary_raw: Optional[str] = None) -> None:
        """Backfills tier and match_score after the matcher has run.

        FIX 2b live-test 07-18: también backfillea salary_raw si viene — un job
        RE-procesado (hash existente, INSERT OR IGNORE no-op) que ahora sí trae
        salario (FIX 1: extraído del texto) actualiza la fila vieja. COALESCE:
        nunca pisa un salario ya guardado con NULL."""
        with _get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE seen_jobs_registry SET tier=?, match_score=?, "
                "salary_raw=COALESCE(?, salary_raw) WHERE job_hash=?",
                (tier, match_score, salary_raw, self.compute_hash(url)),
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

    def get_registry_stats(self) -> dict:
        with _get_connection(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry"
            ).fetchone()[0]
            by_tier = conn.execute(
                "SELECT tier, COUNT(*) as cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL GROUP BY tier"
            ).fetchall()
            recent_24h = conn.execute(
                "SELECT COUNT(*) FROM seen_jobs_registry WHERE scraped_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
            ).fetchone()[0]

        return {
            "total_seen": total,
            "recent_24h": recent_24h,
            "by_tier": {row["tier"]: row["cnt"] for row in by_tier},
        }

    def get_dashboard_stats(self) -> dict:
        """Agregados para el panel visual (F19) — todo desde la DB, sin LLM.

        Devuelve distribución por tier, por fuente, y volumen por día (14d)
        para que la UI dibuje el board de estadísticas del registry.
        """
        with _get_connection(self.db_path) as conn:
            by_tier = conn.execute(
                "SELECT tier, COUNT(*) AS cnt FROM seen_jobs_registry "
                "WHERE tier IS NOT NULL GROUP BY tier"
            ).fetchall()
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
        """
        t1, t2, t3, t4 = tier_counts or (None, None, None, None)
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    """UPDATE runs SET completed_at = ?, status = ?, summary_json = ?,
                              tier1_count = ?, tier2_count = ?, tier3_count = ?,
                              tier4_count = ?, domains_aborted = ?,
                              breaker_state_json = ?, error = ?
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
    ) -> None:
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO node_traces
                       (run_id, node, graph, domain, title, started_at,
                        exec_ms, queue_wait_ms, status, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, node, graph, domain, title, started_at or _now_iso(),
                     exec_ms, queue_wait_ms, status, _clip(error)),
                )
        except Exception as exc:
            logger.warning("[TELEMETRY] record_node_trace failed for %s: %s", node, exc)

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
                    c.get("outcome"), _clip(c.get("error")), now,
                )
                for c in calls
            ]
            with _get_connection(self.db_path) as conn:
                conn.executemany(
                    """INSERT INTO llm_calls
                       (run_id, trace_id, purpose, provider, model, provider_index,
                        attempt, latency_ms, prompt_tokens, completion_tokens,
                        outcome, error, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
        except Exception as exc:
            logger.warning("[TELEMETRY] record_llm_calls failed for trace %s: %s", trace_id, exc)

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
