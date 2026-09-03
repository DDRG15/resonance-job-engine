"""
eval_extraction.py — mide la calidad de extracción contra el corpus.

Un cambio de prompt que mejora un board y rompe otros tres no se nota hasta la corrida
siguiente, y para entonces ya está commiteado. Este harness existe para que ese cambio
falle antes de mergear, con un número al lado.

    python tools/eval_extraction.py                 # corre y reporta
    python tools/eval_extraction.py --gate          # exit 1 si empeoro vs la corrida previa
    python tools/eval_extraction.py --baseline ID   # compara contra una corrida especifica
    python tools/eval_extraction.py --dry-run       # lista el corpus sin llamar a ningun LLM

NO CORRE EN LA SUITE. Pega a proveedores LLM reales: es lento, gasta cuota y no es
determinista. Va como paso opcional de build_beta.bat y como comando manual pre-merge.
Lo que SÍ corre en pytest es el scoring (graph/eval_scoring.py) con callers mockeados —
eso valida la medición, no el modelo.

DE DÓNDE SALE EL CORPUS
-----------------------
Tres fuentes, en orden de calidad:

  fixtures/   tests/fixtures/extraction_corpus/*.json — curados a mano, con
              expected_jobs. Es la única fuente con respuestas correctas conocidas.
  drift/      drift_quarantine/*.json con schema_version >= 2 — capturados en produccion.
              Traen el input exacto y la salida del modelo, sin anotar. Las metricas
              autoverificables (grounding, completitud) funcionan igual, y por eso este
              corpus crece solo con cada corrida.
  review/     filas de manual_review con resolved=1 — el humano corrigio la URL, asi que
              hay respuesta correcta para el campo que mas falla.

Los snapshots SIN schema_version se descartan: son de v2, guardaban sent_text[:8000]
contra un input truncado a 20000, y medir grounding contra un texto incompleto marca como
alucinacion una URL que si estaba en la fuente.
"""

import argparse
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from database import GemaDatabase  # noqa: E402
from graph.eval_scoring import agregar, comparar, puntuar_caso  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = RAIZ / "tests" / "fixtures" / "extraction_corpus"


# =============================================================================
# Carga del corpus
# =============================================================================

def _casos_fixtures() -> list[dict]:
    if not FIXTURES.exists():
        return []
    casos = []
    for p in sorted(FIXTURES.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  [SKIP] {p.name}: JSON invalido ({exc})")
            continue
        if not d.get("source_text"):
            print(f"  [SKIP] {p.name}: sin source_text")
            continue
        casos.append({
            "nombre": p.stem,
            "source": "fixture",
            "texto": d["source_text"],
            "esperados": len(d.get("expected_jobs") or []) or None,
            "url_esperada": None,
        })
    return casos


def _casos_drift() -> list[dict]:
    d = pathlib.Path(config.DRIFT_QUARANTINE_DIR)
    if not d.exists():
        return []
    casos = []
    for p in sorted(d.glob("*.json")):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Sin schema_version es un snapshot de v2: input truncado, output no parseable.
        if (snap.get("schema_version") or 0) < 2:
            continue
        texto = snap.get("sent_text")
        if not texto:
            continue
        casos.append({
            "nombre": p.stem,
            "source": "drift",
            "texto": texto,
            "esperados": None,
            "url_esperada": None,
        })
    return casos


def _casos_review(db: GemaDatabase) -> list[dict]:
    """
    Filas resueltas por el humano. Son los únicos casos con respuesta correcta para la
    URL, que es justo el campo que más falla en el fallback LLM.
    """
    casos = []
    try:
        filas = db.get_manual_review(include_resolved=True)
    except Exception:
        return []
    for f in filas:
        if not f.get("resolved") or not f.get("resolved_url"):
            continue
        titulo, empresa = f.get("title"), f.get("company")
        if not titulo:
            continue
        # El texto fuente reconstruido: titulo + empresa + la URL correcta. Es minimo a
        # proposito — mide si el sistema puede mapear un titulo a SU url cuando ambos
        # estan presentes, que es exactamente lo que fallo cuando el humano tuvo que
        # corregirlo.
        casos.append({
            "nombre": f"review_{(f.get('review_hash') or '')[:8]}",
            "source": "review",
            "texto": f"{titulo} — {empresa or ''} {f['resolved_url']}",
            "esperados": 1,
            "url_esperada": f["resolved_url"],
        })
    return casos


def cargar_corpus(db: GemaDatabase) -> list[dict]:
    casos = _casos_fixtures() + _casos_drift() + _casos_review(db)
    return casos


# =============================================================================
# Ejecución
# =============================================================================

def correr_caso(caso: dict) -> dict:
    """Corre un caso por la cadena de extracción real y lo puntúa."""
    from nlp_engine import extract_jobs_from_text, prompt_version

    fila = {
        "corpus_case": caso["nombre"],
        "source": caso["source"],
        "prompt_version": prompt_version(),
        "provider": None,
        "error": None,
    }
    try:
        resultado = extract_jobs_from_text(caso["texto"], source_url=None)
        jobs = list(resultado.jobs)
    except Exception as exc:
        fila["error"] = f"{type(exc).__name__}: {exc}"
        fila.update(puntuar_caso(jobs=[], texto_fuente=caso["texto"],
                                 jobs_esperados=caso.get("esperados"),
                                 url_esperada=caso.get("url_esperada")))
        return fila

    fila.update(puntuar_caso(
        jobs=jobs,
        texto_fuente=caso["texto"],
        jobs_esperados=caso.get("esperados"),
        url_esperada=caso.get("url_esperada"),
    ))
    return fila


def _imprimir(resumen: dict, titulo: str) -> None:
    print(f"\n{titulo}")
    for k in ("casos", "jobs_extraidos", "grounding_rate", "hallucination_rate",
              "fields_complete", "agreement", "casos_con_error"):
        v = resumen.get(k)
        print(f"  {k:20} {'—' if v is None else v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 si alguna metrica empeoro respecto del baseline")
    ap.add_argument("--baseline", metavar="EVAL_RUN_ID",
                    help="corrida contra la cual comparar (default: la anterior)")
    ap.add_argument("--dry-run", action="store_true",
                    help="lista el corpus sin llamar a ningun LLM")
    ap.add_argument("--tolerancia", type=float, default=0.02,
                    help="margen antes de considerar una caida como regresion")
    args = ap.parse_args(argv)

    db = GemaDatabase()
    casos = cargar_corpus(db)

    if not casos:
        print("CORPUS VACIO.")
        print("  fixtures: tests/fixtures/extraction_corpus/*.json  (curados a mano)")
        print(f"  drift:    {config.DRIFT_QUARANTINE_DIR}/*.json con schema_version >= 2")
        print("  review:   filas de manual_review resueltas")
        print("\nEl corpus de drift se llena solo a medida que corren scrapes. Los")
        print("fixtures hay que curarlos: ver el README de esa carpeta.")
        return 1

    por_fuente = {}
    for c in casos:
        por_fuente[c["source"]] = por_fuente.get(c["source"], 0) + 1
    print(f"Corpus: {len(casos)} casos  ({', '.join(f'{k}={v}' for k, v in por_fuente.items())})")

    if args.dry_run:
        for c in casos:
            print(f"  [{c['source']:8}] {c['nombre']}  ({len(c['texto'])} chars)")
        return 0

    eval_run_id = uuid.uuid4().hex
    filas = []
    for i, c in enumerate(casos, 1):
        print(f"  [{i}/{len(casos)}] {c['nombre']} ...", end=" ", flush=True)
        fila = correr_caso(c)
        filas.append(fila)
        if fila.get("error"):
            print(f"ERROR {fila['error'][:60]}")
        else:
            print(f"{fila['jobs_extracted']} jobs, {fila['grounded']} grounded")

    db.record_eval_results(eval_run_id, filas)
    resumen = agregar(filas)
    _imprimir(resumen, f"=== EVAL {eval_run_id[:8]} ===")

    base_id = args.baseline or db.get_last_eval_run_id(before=eval_run_id)
    if not base_id:
        print("\nSin baseline previo — esta corrida ES el baseline.")
        return 0

    base = agregar(db.get_eval_results(base_id))
    _imprimir(base, f"=== BASELINE {base_id[:8]} ===")
    regresiones = comparar(resumen, base, tolerancia=args.tolerancia)

    if not regresiones:
        print("\nSin regresiones.")
        return 0

    print(f"\nREGRESIONES ({len(regresiones)}):")
    for r in regresiones:
        print(f"  - {r}")
    return 1 if args.gate else 0


if __name__ == "__main__":
    raise SystemExit(main())
