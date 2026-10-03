"""
repuntuar_registry.py — lleva el registry entero a la version vigente del scoring.

    python tools/repuntuar_registry.py --dry-run     # solo cuenta
    python tools/repuntuar_registry.py               # snapshot, re-puntua y persiste

Por que existe. Subir SCORE_VERSION no re-puntua nada por si solo: el atajo de la
Fase 8 deja de saltear lo viejo, pero lo viejo nunca vuelve a pasar por bucket_jobs,
porque el scrape descarta los avisos ya vistos antes de puntuar. La corrida del
2026-09-29 lo midio: seen-skip=5868, y solo los 657 avisos nuevos salieron en v3. El
registry quedo con tres escalas —sin version 1.504, v2 915, v3 657— y el dashboard
las sumaba como si fueran una.

Usa la busqueda del CLI (run_scrape_now.build_config) y el perfil de
user_profile.yaml: la misma vara con la que se puntua una corrida por CLI.

Lo que NO re-puntua: las filas off-topic para esa busqueda. bucket_jobs las descarta
antes de puntuar —es su regla, no una de esta herramienta— y quedan en su version
vieja. El dashboard las cuenta aparte, como fuera de escala, en vez de mezclarlas.

Snapshot (Plan30 Fase 3). Esta herramienta reescribe `tier`, `score` y `score_version`
de miles de filas, y el 29-09 se hicieron tres backups a mano antes de correrla: el
backup dependia de que el usuario se acordara. Una re-puntuacion con una version del scoring
mala no tiene "deshacer" —la fila vieja se pisa—, y la unica reversion es volver a una
copia. Ahora la copia la saca la herramienta, antes de tocar nada, con la API de backup
de SQLite (`Connection.backup`), que copia pagina a pagina con un lock compartido y
respeta lo que todavia vive en el -wal. `shutil.copy` sobre una base abierta en WAL copia
un `.db` al que le faltan las transacciones del -wal. Si el snapshot falla, la herramienta
NO escribe: re-puntuar sin red de seguridad es peor que no re-puntuar. Con --dry-run no
se saca snapshot, porque no se escribe nada.
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_RAIZ = Path(__file__).resolve().parent.parent


class SnapshotError(RuntimeError):
    """El snapshot no se pudo sacar o no paso la verificacion: no se escribe nada."""


def _a_fecha(iso):
    try:
        f = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return f if f.tzinfo else f.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def directorio_de_backups(valor: str) -> Path:
    """
    Una ruta relativa se resuelve contra la raiz del repo, no contra el cwd.

    `.gitignore` y `.dockerignore` excluyen `backups/` en la raiz. Con el cwd como ancla,
    correr la herramienta desde otro directorio dejaba el snapshot —una copia completa
    del registry— donde ninguna de las dos reglas lo cubre.
    """
    p = Path(valor)
    return p if p.is_absolute() else _RAIZ / p


def tomar_snapshot(db_path: str, directorio: Path, version: str,
                   ahora: datetime | None = None) -> Path | None:
    """
    Copia consistente de la base en `directorio`; devuelve la ruta, o None si la base no
    existe todavia (no hay nada que perder: la herramienta la va a crear vacia).

    Nombre: `<base>_<AAAAMMDDTHHMMSSZ>_pre-repuntaje-<version>.db`, en UTC. Se escribe
    primero a `*.parcial` y se renombra al final: una copia cortada a medias (disco
    lleno, Ctrl+C) no puede quedar con un nombre que la haga pasar por un snapshot
    valido. Antes de renombrar se corre `PRAGMA integrity_check` sobre la copia; si no
    dice `ok`, se borra y se lanza SnapshotError.
    """
    origen = Path(db_path)
    if not origen.exists():
        return None

    ahora = ahora or datetime.now(timezone.utc)
    sello = ahora.strftime("%Y%m%dT%H%M%SZ")
    directorio.mkdir(parents=True, exist_ok=True)
    base = f"{origen.stem}_{sello}_pre-repuntaje-{version}"
    destino = directorio / f"{base}{origen.suffix or '.db'}"
    n = 1
    while destino.exists():
        # Dos corridas en el mismo segundo: la segunda no pisa a la primera.
        n += 1
        destino = directorio / f"{base}-{n}{origen.suffix or '.db'}"
    parcial = destino.with_name(destino.name + ".parcial")

    src = dst = None
    try:
        # timeout=30: espera a que un escritor suelte el lock en vez de fallar al
        # instante con "database is locked".
        src = sqlite3.connect(str(origen), timeout=30.0)
        dst = sqlite3.connect(str(parcial))
        src.backup(dst)
        dst.close()
        dst = None

        # Verificar la COPIA, abierta de solo lectura: lo que se prueba es el archivo
        # que se va a usar para restaurar, no la conexion que lo escribio.
        chk = sqlite3.connect(f"{parcial.as_uri()}?mode=ro", uri=True)
        try:
            veredicto = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        if veredicto != "ok":
            raise SnapshotError(f"integrity_check de la copia devolvio {veredicto!r}")
        os.replace(parcial, destino)
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        for c in (dst, src):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        # Si algo fallo, no dejar ni la copia parcial ni sus archivos auxiliares.
        for resto in (parcial, Path(str(parcial) + "-wal"), Path(str(parcial) + "-shm")):
            try:
                resto.unlink()
            except OSError:
                pass
    return destino


def main(argv=None) -> int:
    import config
    import matcher
    import run_scrape_now
    from database import GemaDatabase
    from models import JobResult

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true", help="solo contar, no escribir")
    args = ap.parse_args(argv)

    cfg = run_scrape_now.build_config()
    perfil = run_scrape_now._load_profile()

    # El snapshot va ANTES de construir GemaDatabase: el constructor corre las
    # migraciones aditivas (CREATE TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN), que ya
    # son escrituras. "Antes de escribir" empieza ahi, no en bucket_jobs.
    if not args.dry_run:
        try:
            snap = tomar_snapshot(
                config.GEMA_DB_PATH, directorio_de_backups(config.GEMA_BACKUP_DIR),
                config.SCORE_VERSION,
            )
        except SnapshotError as exc:
            print(f"ERROR: no se pudo sacar el snapshot ({exc}). No se escribio nada.",
                  file=sys.stderr)
            return 2
        if snap is None:
            print(f"snapshot: no hay base en {config.GEMA_DB_PATH}, nada que respaldar.")
        else:
            print(f"snapshot: {snap}  ({snap.stat().st_size / 1024:.0f} KB)")
            print("  para volver atras: cerrar la API y copiar ese archivo sobre la base.")

    db = GemaDatabase()
    version = config.SCORE_VERSION

    filas = db.filas_fuera_de_version(version)
    antes = Counter(f["score_version"] or "sin version" for f in filas)

    jobs, invalidas = [], 0
    for f in filas:
        try:
            jobs.append(JobResult(
                title=f["title"] or "", company=f["company"] or "(empresa no listada)",
                url=f["url"], salary_raw=f["salary_raw"], location_raw=f["location_raw"],
                source_domain=f["source_domain"] or "", deadline=f["deadline"],
                scraped_at=_a_fecha(f["scraped_at"]),
            ))
        except Exception:
            # Una fila vieja que ya no pasa la validacion del modelo no se inventa:
            # se cuenta y queda como esta.
            invalidas += 1

    relevantes = [j for j in jobs
                  if matcher.title_is_relevant(j.title, cfg.target_titles, perfil)]

    print(f"version vigente: {version}")
    print(f"filas fuera de version: {len(filas)}  ({dict(antes)})")
    print(f"  re-puntuables (relevantes para la busqueda del CLI): {len(relevantes)}")
    print(f"  off-topic, quedan en su version: {len(jobs) - len(relevantes)}")
    print(f"  invalidas para el modelo, quedan como estan: {invalidas}")

    if args.dry_run:
        print("\n--dry-run: no se escribio nada.")
        return 0

    t1, t2, t3, t4 = matcher.bucket_jobs(relevantes, cfg, db, profile=perfil,
                                         repuntuar_todo=True)
    print(f"\nre-puntuadas con {version}: T1 {len(t1)} | T2 {len(t2)} | T3 {len(t3)} | T4 {len(t4)}")
    print("(los tiers de arriba cuentan el shortlist; vencidos, duplicados y no elegibles")
    print(" se persisten con su tier igual, pero no entran a esas listas)")

    quedan = db.filas_fuera_de_version(version)
    print(f"\nfilas que siguen fuera de {version}: {len(quedan)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
