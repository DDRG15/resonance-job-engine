"""
repuntuar_registry.py — lleva el registry entero a la version vigente del scoring.

    python tools/repuntuar_registry.py --dry-run     # solo cuenta
    python tools/repuntuar_registry.py               # re-puntua y persiste

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
"""

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _a_fecha(iso):
    try:
        f = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return f if f.tzinfo else f.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


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
