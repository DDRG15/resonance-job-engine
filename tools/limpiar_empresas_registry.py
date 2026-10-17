"""
limpiar_empresas_registry.py — aplica la normalización de `company` al registry ya guardado.

    python tools/limpiar_empresas_registry.py              # solo cuenta
    python tools/limpiar_empresas_registry.py --aplicar    # snapshot, reescribe y marca

Por qué existe. El registry guarda cada aviso una sola vez (INSERT OR IGNORE): corregir
el scraper limpia lo que entra desde ahora, pero lo que ya entró con el título pegado a la
empresa, con un eslogan o con el marcador vacío se queda así. Esta herramienta pasa esas
filas por `normalizar_empresa`, deriva la empresa de la URL cuando el ATS la lleva en el
slug, y manda a la pila de revisión manual lo que sigue dudoso. No borra ninguna fila.

Antes de escribir saca el mismo snapshot que `repuntuar_registry`; si falla, no escribe.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.repuntuar_registry import SnapshotError, directorio_de_backups, tomar_snapshot


def main(argv=None) -> int:
    import config
    from database import GemaDatabase

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--aplicar", action="store_true", help="escribir (por defecto solo cuenta)")
    args = ap.parse_args(argv)

    if args.aplicar:
        try:
            snap = tomar_snapshot(
                config.GEMA_DB_PATH, directorio_de_backups(config.GEMA_BACKUP_DIR), "empresas"
            )
        except SnapshotError as exc:
            print(f"ERROR: no se pudo sacar el snapshot ({exc}). No se escribió nada.",
                  file=sys.stderr)
            return 2
        if snap is not None:
            print(f"snapshot: {snap}")

    try:
        r = (GemaDatabase().reparar_empresas(aplicar=True) if args.aplicar
             else GemaDatabase.previsualizar_empresas(config.GEMA_DB_PATH))
    except sqlite3.Error as exc:
        print(f"ERROR: no se pudo leer o reparar la base ({exc}).", file=sys.stderr)
        return 2
    verbo = "corregidas" if args.aplicar else "se corregirían"
    print(f"filas: {r['filas']} | {verbo}: {r['corregidas']} | "
          f"dudosas a revisión manual: {r['dudosas']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
