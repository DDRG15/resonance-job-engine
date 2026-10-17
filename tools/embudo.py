"""
embudo.py — qué títulos de la búsqueda nunca producen nada, y qué rinde cada fuente.

Lee `node_traces` (columna `items`, de la Fase 4) y responde
lo que hasta el 29-09 solo se podía contestar sumando tiempos a mano: de los 51 títulos de la
búsqueda, ¿cuáles no trajeron un solo aviso en ningún board, corrida tras corrida?

PROPONE, no borra. Saca de la búsqueda lo que el usuario decida: la herramienta no escribe nada,
ni en la base (se abre en modo solo lectura, sin migraciones) ni en la configuración.

Las filas anteriores a la Fase 4 tienen `items` NULL. La salida dice cuántas son en lugar de
leerlas como cero: tratar "no medido" como "vacío" propondría podar la búsqueda entera.

Uso:
    python tools/embudo.py                          # base de config.GEMA_DB_PATH
    python tools/embudo.py --db copia.db            # contra una copia
    python tools/embudo.py --corridas 8 --min-corridas 3
    python tools/embudo.py --json                   # la misma información, para otra herramienta
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import embudo_fuentes  # noqa: E402

_ETIQUETA = {
    "nunca_produce": "NUNCA PRODUCE",
    "evidencia_insuficiente": "poca evidencia",
    "sin_medir": "sin medir",
    "produce": "produce",
}


def _fmt(v, ancho=0, dec=None):
    if v is None:
        return "—".rjust(ancho)
    if dec is not None and isinstance(v, float):
        return f"{v:.{dec}f}".rjust(ancho)
    return str(v).rjust(ancho)


def formatear(r: dict, *, todos: bool = False) -> str:
    L: list[str] = []
    cob = r["cobertura"]
    L.append(f"EMBUDO por fuente y título — últimas {r['ventana_corridas']} corridas con ramas CSS "
             f"(mínimo {r['min_corridas_medidas']} corridas medidas para proponer)")
    L.append("=" * 100)
    if r["lectura"].get("sin_tabla"):
        L.append("La base no tiene tabla node_traces: nunca corrió el grafo contra ella.")
        return "\n".join(L)

    L.append(f"Corridas consideradas : {cob['corridas']}")
    L.append(f"Ramas CSS en ellas    : {cob['ramas']}  "
             f"(medidas: {cob['ramas_medidas']} | SIN MEDIR, items NULL: {cob['ramas_sin_medir']})")
    if not r["lectura"].get("tiene_items"):
        L.append("AVISO: esta base es anterior a la Fase 4 — node_traces no tiene la columna "
                 "`items`.")
    if cob["ramas_medidas"] == 0:
        L.append("")
        L.append("NADA QUE PROPONER: ninguna rama de estas corridas tiene `items` medido. Las "
                 "trazas son anteriores")
        L.append("a la Fase 4, y un NULL no es un cero: tratarlo como vacío marcaría los "
                 f"{len({t['titulo'] for t in r['titulos']})} títulos como improductivos. La "
                 "próxima corrida real llena la columna;")
        L.append("después de {} corridas medidas esta herramienta ya puede proponer.".format(
            r["min_corridas_medidas"]))
    elif cob["corridas_sin_ninguna_medida"]:
        L.append(f"Corridas sin ninguna rama medida (se ignoran): "
                 f"{len(cob['corridas_sin_ninguna_medida'])}")

    L.append("")
    L.append("RENDIMIENTO POR FUENTE (sus últimas corridas)")
    L.append("-" * 100)
    L.append(f"{'fuente':<26}{'tipo':<10}{'estado':<17}{'corr.':>6}{'sin med.':>9}"
             f"{'avisos':>9}{'por corr.':>10}{'ramas':>7}{'vacías':>8}")
    for f in r["fuentes"]:
        L.append(f"{f['fuente']:<26}{f['tipo']:<10}{f['estado']:<17}{f['corridas']:>6}"
                 f"{f['corridas_sin_medir']:>9}{_fmt(f['items_total'], 9)}"
                 f"{_fmt(f['items_por_corrida'], 10, 1)}{_fmt(f['ramas_medidas'], 7)}"
                 f"{_fmt(f['ramas_vacias'], 8)}")

    L.append("")
    L.append("TÍTULOS")
    L.append("-" * 100)
    L.append(f"{'título':<46}{'veredicto':<16}{'avisos':>8}{'ramas/corr.':>12}{'s/corr.':>9}"
             f"{'corr. med.':>11}")
    mostrados = r["titulos"] if todos else [t for t in r["titulos"] if t["veredicto"] != "produce"]
    for t in mostrados:
        L.append(f"{t['titulo'][:45]:<46}{_ETIQUETA[t['veredicto']]:<16}{t['items_total']:>8}"
                 f"{t['ramas_por_corrida']:>12}{t['segundos_por_corrida']:>9}"
                 f"{t['corridas_medidas']:>11}")
    n_prod = sum(1 for t in r["titulos"] if t["veredicto"] == "produce")
    if not todos and n_prod:
        L.append(f"(+ {n_prod} títulos que producen; --todos los muestra)")
    if not r["titulos"]:
        L.append("(sin títulos en las corridas consideradas)")

    L.append("")
    L.append("PROPUESTA DE PODA — decide el usuario; esta herramienta no borra nada")
    L.append("-" * 100)
    if not r["propuestas"]:
        L.append("Ningún título cumple la condición: 0 avisos en todas sus ramas medidas, en "
                 f"al menos {r['min_corridas_medidas']} corridas, descontando las fuentes que "
                 "ese día no trajeron nada para ningún título.")
    for p in r["propuestas"]:
        L.append(f"- {p['titulo']}: ahorra {p['ramas_ahorradas_por_corrida']} ramas CSS por "
                 f"corrida (~{p['segundos_ahorrados_por_corrida']} s). {p['evidencia']}.")
    a = r["ahorro_si_se_aplican_todas"]
    if r["propuestas"]:
        L.append(f"Si se aplican las {a['titulos']}: {a['ramas_por_corrida']} ramas y "
                 f"~{a['segundos_por_corrida']} s menos por corrida.")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=config.GEMA_DB_PATH, help="base SQLite (por defecto la de config)")
    ap.add_argument("--corridas", type=int, default=config.EMBUDO_VENTANA_CORRIDAS,
                    help="cuántas corridas recientes mirar")
    ap.add_argument("--min-corridas", type=int, default=config.EMBUDO_MIN_CORRIDAS_MEDIDAS,
                    help="corridas medidas necesarias para proponer un título")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    ap.add_argument("--todos", action="store_true", help="incluir los títulos que sí producen")
    args = ap.parse_args(argv)

    if not pathlib.Path(args.db).exists():
        print(f"No existe la base: {args.db}", file=sys.stderr)
        return 2
    r = embudo_fuentes.embudo_desde_db(args.db, ventana=args.corridas,
                                       min_corridas=args.min_corridas)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(formatear(r, todos=args.todos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
