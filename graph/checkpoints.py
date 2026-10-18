"""Durable run cursors. The CV stays in memory; only its fingerprint is stored."""

from contextlib import contextmanager
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Optional


class ResumeError(ValueError):
    """A saved run cannot be continued with the supplied runtime."""


def profile_fingerprint(profile: Optional[dict]) -> Optional[str]:
    if profile is None:
        return None
    source_hash = profile.get("_cv_source_hash")
    if isinstance(source_hash, str) and re.fullmatch(r"[a-f0-9]{64}", source_hash):
        return source_hash
    raw = json.dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def checkpoint_path(db) -> Optional[Path]:
    raw = getattr(db, "db_path", None)
    if not isinstance(raw, (str, Path)) or str(raw) == ":memory:":
        return None
    # The registry already follows RESONANCE_HOME in the desktop app and Docker.
    return Path(raw).resolve().with_name("checkpoints.sqlite")


def _connect(path: Path):
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS run_resume (
        run_id TEXT PRIMARY KEY, profile_hash TEXT, finished INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    return conn


@contextmanager
def open_checkpoints(db):
    """Open lazily, allow only the application's data models, never unpickle."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = checkpoint_path(db)
    if path is None:
        raise ResumeError("La base de esta corrida no admite reanudación persistente.")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    allowed = [("models", name) for name in
               ("SearchConfig", "JobResult", "ScrapeRunSummary", "TieredJob")]
    serde = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=allowed,
                               allowed_json_modules=allowed)
    try:
        saver = SqliteSaver(conn, serde=serde)
        saver.setup()
        yield saver
    finally:
        conn.close()


def register_run(saver, run_id: str, profile: Optional[dict]) -> None:
    try:
        saver.conn.execute("INSERT INTO run_resume(run_id, profile_hash) VALUES (?, ?)",
                           (run_id, profile_fingerprint(profile)))
    except sqlite3.IntegrityError:
        raise ResumeError("El identificador ya existe; reanudá la corrida pendiente.")
    saver.conn.commit()


def resume_info(db, run_id: str, profile: Optional[dict] = None) -> dict:
    """Read availability without importing LangGraph or creating a missing file."""
    result = {"resumable": False, "resume_requires_cv": False}
    path = checkpoint_path(db)
    if path is None or not path.is_file():
        return result
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute("SELECT profile_hash, finished FROM run_resume WHERE run_id=?",
                               (run_id,)).fetchone()
            if row is None or row[1]:
                return result
            saved = conn.execute("SELECT 1 FROM checkpoints WHERE thread_id=? LIMIT 1",
                                 (run_id,)).fetchone()
            if saved:
                result["resumable"] = True
                result["resume_requires_cv"] = row[0] != profile_fingerprint(profile)
    except sqlite3.Error:
        return result
    return result


def require_matching_profile(saver, run_id: str, profile: Optional[dict]) -> None:
    row = saver.conn.execute("SELECT profile_hash, finished FROM run_resume WHERE run_id=?",
                             (run_id,)).fetchone()
    if row is None or row[1]:
        raise ResumeError("No hay un avance pendiente para esta corrida.")
    if row[0] != profile_fingerprint(profile):
        raise ResumeError("Volvé a cargar el mismo perfil de CV para reanudar esta corrida.")


def finish_run(saver, run_id: str) -> None:
    """Completed runs need no cursor or job payload retained in this database."""
    saver.conn.execute("UPDATE run_resume SET finished=1, updated_at=CURRENT_TIMESTAMP "
                       "WHERE run_id=?", (run_id,))
    saver.conn.commit()
    saver.delete_thread(run_id)
    saver.conn.execute("DELETE FROM run_resume WHERE finished=1 "
                       "AND updated_at < datetime('now', '-7 days')")
    saver.conn.commit()
