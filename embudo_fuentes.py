"""
embudo_fuentes.py — qué títulos de la búsqueda no producen nada, y qué rinde cada fuente

La búsqueda del CLI tiene 51 títulos y cada título es UNA rama de Playwright por cada board
CSS: 51 × 8 boards = 408 pestañas por corrida, 18,9 min el 29-09. Un título que ningún board
contesta cuesta esas ramas completas corrida tras corrida, y hasta que se guardó el conteo por rama no había forma
de saber cuáles eran: `node_traces` guardaba cuánto tardó cada rama y no cuánto trajo.

Con `items` en cada traza el rendimiento por (fuente × título) es una consulta. Este módulo
la hace y PROPONE: no borra ningún título. Qué se saca de la búsqueda lo decide el usuario — una
herramienta que poda sola convierte un mal día de red (todos los boards en cero) en 51
títulos borrados.

Tres reglas separan "el título no produce" de "no pude medirlo", y por eso las propuestas
son pocas y defendibles:

1. Una fila con `items` NULL no es un cero: es una rama que no consultó la fuente o una
   traza anterior a la Fase 4. No cuenta en ningún sentido, y la herramienta dice cuántas
   hay en lugar de tratarlas como vacías.
2. Un (fuente, corrida) donde la fuente no trajo NADA para NINGÚN título es culpa de la
   fuente (bloqueada, caída), no de los títulos. Esas ramas no se le cargan a ningún título.
3. Los boards de feed fijo ignoran la búsqueda y se piden solo con el primer título: lo que
   traen no dice nada sobre ese título y se excluyen de la atribución.

Un título solo se propone con `min_corridas` corridas medidas: un cero en una sola corrida
es una muestra, no un patrón.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Callable, Optional

import config
import fuentes_estado

VEREDICTO_PRODUCE = "produce"
VEREDICTO_NUNCA = "nunca_produce"
VEREDICTO_SIN_MEDIR = "sin_medir"
VEREDICTO_INSUFICIENTE = "evidencia_insuficiente"


# =============================================================================
# Lectura — solo lectura, tolerante a bases anteriores a la Fase 4
# =============================================================================

def abrir_solo_lectura(db_path: str) -> sqlite3.Connection:
    """
    Abre la base en modo `ro`. La herramienta se corre contra la base real: abrir con
    `GemaDatabase` ejecutaría las migraciones (ALTER TABLE) y una consulta no escribe.
    """
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columnas(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(node_traces)")}


def cargar(conn: sqlite3.Connection, ventana: int) -> tuple[list[dict], list[dict], dict]:
    """
    Devuelve (ramas CSS de las últimas `ventana` corridas, filas por (fuente, corrida),
    metadatos de lectura). Una base sin la columna `items` devuelve todo en NULL y lo dice.
    """
    cols = _columnas(conn)
    if not cols:
        return [], [], {"tiene_items": False, "sin_tabla": True}
    tiene_items = "items" in cols
    items_sql = "items" if tiene_items else "NULL AS items"
    acept_sql = "aceptados" if "aceptados" in cols else "NULL AS aceptados"

    srp = [dict(r) for r in conn.execute(
        f"""SELECT run_id, domain, title, {items_sql}, {acept_sql}, status, exec_ms, started_at
            FROM node_traces
            WHERE node = 'scrape_srp'
              AND run_id IN (SELECT run_id FROM node_traces
                             WHERE node = 'scrape_srp' AND run_id IS NOT NULL
                             GROUP BY run_id ORDER BY MIN(started_at) DESC LIMIT ?)
            ORDER BY started_at""", (max(1, int(ventana)),))]

    fuentes = [dict(r) for r in conn.execute(
        f"""SELECT domain, run_id, MIN(started_at) AS inicio,
                   {'SUM(items)' if tiene_items else 'NULL'}           AS items,
                   {'COUNT(items)' if tiene_items else '0'}            AS medidas,
                   COUNT(*)                                            AS trazas,
                   {'SUM(aceptados)' if 'aceptados' in cols else 'NULL'} AS aceptados,
                   MAX(CASE WHEN status = 'error' THEN 1 ELSE 0 END)   AS hubo_error,
                   MAX(CASE WHEN node = 'fuente_ats' THEN 3
                            WHEN node = 'fuente_endpoint' THEN 2 ELSE 1 END) AS tipo
            FROM node_traces
            WHERE node IN ('scrape_srp', 'fuente_endpoint', 'fuente_ats')
              AND domain IS NOT NULL AND run_id IS NOT NULL
            GROUP BY domain, run_id
            ORDER BY domain, inicio DESC""")]
    return srp, fuentes, {"tiene_items": tiene_items, "sin_tabla": False}


def _feed_fijo_por_defecto(dominio: str) -> bool:
    try:
        from selectors_registry import get_selectors
        sel = get_selectors(dominio)
        return bool(sel and getattr(sel, "feed_fijo", False))
    except Exception:
        return False


# =============================================================================
# Cálculo — puro
# =============================================================================

def calcular(srp: list[dict], fuentes: list[dict], *,
             ventana: Optional[int] = None, min_corridas: Optional[int] = None,
             es_feed_fijo: Callable[[str], bool] = _feed_fijo_por_defecto,
             meta: Optional[dict] = None) -> dict:
    n = config.EMBUDO_VENTANA_CORRIDAS if ventana is None else ventana
    minimo = config.EMBUDO_MIN_CORRIDAS_MEDIDAS if min_corridas is None else min_corridas

    corridas = sorted({r["run_id"] for r in srp},
                      key=lambda rid: min(r["started_at"] for r in srp if r["run_id"] == rid),
                      reverse=True)
    medidas = [r for r in srp if r["items"] is not None]
    cobertura = {
        "ramas": len(srp),
        "ramas_medidas": len(medidas),
        "ramas_sin_medir": len(srp) - len(medidas),
        "corridas": len(corridas),
        "corridas_sin_ninguna_medida": [
            rid for rid in corridas
            if not any(r["items"] is not None for r in srp if r["run_id"] == rid)],
    }

    # Regla 2: (fuente, corrida) donde la fuente no trajo nada para ningun titulo.
    total_fuente_corrida: dict[tuple, int] = defaultdict(int)
    for r in medidas:
        total_fuente_corrida[(r["domain"], r["run_id"])] += r["items"]
    caidas = {k for k, v in total_fuente_corrida.items() if v == 0}

    # Regla 3: feed fijo.
    feed_cache: dict[str, bool] = {}

    def _feed(dominio: str) -> bool:
        if dominio not in feed_cache:
            feed_cache[dominio] = es_feed_fijo(dominio)
        return feed_cache[dominio]

    por_titulo: dict[str, list[dict]] = defaultdict(list)
    for r in srp:
        if r.get("title"):
            por_titulo[r["title"]].append(r)

    titulos = []
    for titulo, filas in por_titulo.items():
        ejecutadas = [r for r in filas if r["items"] is not None and not _feed(r["domain"])]
        validas = [r for r in ejecutadas if (r["domain"], r["run_id"]) not in caidas]
        corridas_validas = {r["run_id"] for r in validas}
        corridas_ejecutadas = {r["run_id"] for r in ejecutadas}
        items_total = sum(r["items"] for r in validas)
        fuentes_con_items = sorted({r["domain"] for r in validas if r["items"] > 0})

        if not validas:
            veredicto = VEREDICTO_SIN_MEDIR
        elif items_total > 0:
            veredicto = VEREDICTO_PRODUCE
        elif len(corridas_validas) < minimo:
            veredicto = VEREDICTO_INSUFICIENTE
        else:
            veredicto = VEREDICTO_NUNCA

        n_corr = max(1, len(corridas_ejecutadas))
        titulos.append({
            "titulo": titulo,
            "veredicto": veredicto,
            "items_total": items_total,
            "fuentes_con_items": fuentes_con_items,
            "ramas_ejecutadas": len(ejecutadas),
            "ramas_sin_medir": sum(1 for r in filas if r["items"] is None),
            "ramas_excluidas_por_fuente_caida": len(ejecutadas) - len(validas),
            "corridas_medidas": len(corridas_validas),
            "ramas_por_corrida": round(len(ejecutadas) / n_corr, 1),
            "segundos_por_corrida": round(
                sum((r["exec_ms"] or 0.0) for r in ejecutadas) / 1000.0 / n_corr, 1),
        })

    orden = {VEREDICTO_NUNCA: 0, VEREDICTO_INSUFICIENTE: 1, VEREDICTO_SIN_MEDIR: 2,
             VEREDICTO_PRODUCE: 3}
    titulos.sort(key=lambda t: (orden[t["veredicto"]], t["items_total"], t["titulo"]))

    propuestas = [
        {"titulo": t["titulo"],
         "ramas_ahorradas_por_corrida": t["ramas_por_corrida"],
         "segundos_ahorrados_por_corrida": t["segundos_por_corrida"],
         "evidencia": (f"0 avisos en {t['ramas_ejecutadas']} ramas de "
                       f"{t['corridas_medidas']} corridas medidas, en ninguna fuente")}
        for t in titulos if t["veredicto"] == VEREDICTO_NUNCA
    ]

    return {
        "ventana_corridas": n,
        "min_corridas_medidas": minimo,
        "corridas": corridas,
        "lectura": meta or {},
        "cobertura": cobertura,
        "fuentes": _fuentes(srp, fuentes, n),
        "titulos": titulos,
        "propuestas": propuestas,
        "ahorro_si_se_aplican_todas": {
            "titulos": len(propuestas),
            "ramas_por_corrida": round(sum(p["ramas_ahorradas_por_corrida"] for p in propuestas), 1),
            "segundos_por_corrida": round(
                sum(p["segundos_ahorrados_por_corrida"] for p in propuestas), 1),
        },
    }


def _fuentes(srp: list[dict], fuentes: list[dict], n: int) -> list[dict]:
    """Rendimiento por fuente en sus últimas `n` corridas (CSS, endpoint y ATS)."""
    por_dominio: dict[str, list[dict]] = defaultdict(list)
    for f in fuentes:
        por_dominio[f["domain"]].append(f)
    recortadas = []
    for dominio, corridas in por_dominio.items():
        corridas = sorted(corridas, key=lambda r: r.get("inicio") or "", reverse=True)
        recortadas.extend(corridas[:n])

    estados = {e["fuente"]: e for e in fuentes_estado.estados_por_fuente(recortadas, ventana=n)}
    ramas_css: dict[str, dict] = defaultdict(lambda: {"ramas": 0, "vacias": 0})
    for r in srp:
        if r["items"] is not None:
            ramas_css[r["domain"]]["ramas"] += 1
            if r["items"] == 0:
                ramas_css[r["domain"]]["vacias"] += 1

    salida = []
    for dominio, corridas in por_dominio.items():
        corridas = sorted(corridas, key=lambda r: r.get("inicio") or "", reverse=True)[:n]
        e = estados.get(dominio)
        medidas = [c for c in corridas if c.get("items") is not None]
        salida.append({
            "fuente": dominio,
            "tipo": e["tipo"] if e else "css",
            "estado": e["estado"] if e else fuentes_estado.SIN_MEDIR,
            "corridas": len(corridas),
            "corridas_sin_medir": len(corridas) - len(medidas),
            "items_total": sum(c["items"] for c in medidas) if medidas else None,
            "items_por_corrida": (round(sum(c["items"] for c in medidas) / len(medidas), 1)
                                  if medidas else None),
            "ramas_medidas": ramas_css[dominio]["ramas"] if dominio in ramas_css else None,
            "ramas_vacias": ramas_css[dominio]["vacias"] if dominio in ramas_css else None,
        })
    salida.sort(key=lambda f: (f["items_total"] is None, -(f["items_total"] or 0), f["fuente"]))
    return salida


def embudo_desde_db(db_path: str, *, ventana: Optional[int] = None,
                    min_corridas: Optional[int] = None,
                    es_feed_fijo: Callable[[str], bool] = _feed_fijo_por_defecto) -> dict:
    n = config.EMBUDO_VENTANA_CORRIDAS if ventana is None else ventana
    conn = abrir_solo_lectura(db_path)
    try:
        srp, fuentes, meta = cargar(conn, n)
    finally:
        conn.close()
    return calcular(srp, fuentes, ventana=n, min_corridas=min_corridas,
                    es_feed_fijo=es_feed_fijo, meta=meta)
