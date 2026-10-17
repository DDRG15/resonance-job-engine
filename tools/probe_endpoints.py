"""
probe_endpoints.py — ¿qué board se puede leer sin abrir un navegador?

No se escribe una línea de scraper hasta saber qué existe.

Por qué existe: `node_traces` del 2026-09-03 midió el nodo `scrape` en 35.726 ms contra
menos de 250 ms de todos los demás nodos sumados. Ese scraper abre una pestaña de Chromium
por búsqueda, y es además el componente que falla — cuatro boards retirados por HTTP 403 o
por muro de login, problemas que solo existen porque hay un navegador pidiendo una página
HTML pensada para humanos. Un GET a un endpoint público no tiene ninguno de esos problemas.

Este script no cambia nada. Prueba, mide y reporta:

    python tools/probe_endpoints.py                 tabla legible
    python tools/probe_endpoints.py --json out.json para el reporte
    python tools/probe_endpoints.py --robots        además, qué dice cada robots.txt

Disciplina: un GET por candidato, User-Agent identificable, pausa entre pedidos. Un probe
que golpea fuerte es un probe que te bloquea el board que venías a rescatar.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

UA = "GEMA-endpoint-probe/1.0 (job board discovery; contacto en el repo)"
TIMEOUT = 20
PAUSA_S = 1.0          # entre pedidos, para no parecer un ataque
CORTE_MUESTRA = 160    # chars del primer item que se muestran en el reporte


# ─────────────────────────────────────────────────────────────────────────────
# Candidatos
#
# `estado` refleja el registry al 2026-09-09: activo (produce filas), vacio
# (activo y sin producir nada), o retirado (active=False, con el diagnóstico del
# 2026-07-11 en selectors_registry.py).
#
# Se prueban TAMBIÉN los boards activos. Alcanzaba con los ocho en cero, pero
# remoteok.com son 518 filas de las 1399 y hoy se raspa con Playwright: si expone
# un endpoint, se lo saca del navegador y se lleva el board más grande.
# ─────────────────────────────────────────────────────────────────────────────

CANDIDATOS: dict[str, dict] = {
    # ── Activos ──────────────────────────────────────────────────────────────
    "remoteok.com": {
        "estado": "activo (518 filas)",
        "urls": [
            ("json", "https://remoteok.com/api"),
            ("rss", "https://remoteok.com/remote-jobs.rss"),
        ],
    },
    "workingnomads.com": {
        "estado": "activo (525 filas)",
        "urls": [
            ("json", "https://www.workingnomads.com/api/exposed_jobs/"),
            ("rss", "https://www.workingnomads.com/jobsfeed"),
        ],
    },
    "jobspresso.co": {
        "estado": "activo (238 filas)",
        "urls": [
            ("rss", "https://jobspresso.co/feed/"),
            ("json", "https://jobspresso.co/wp-json/wp/v2/job_listing?per_page=20"),
        ],
    },
    "news.ycombinator.com": {
        "estado": "activo (69 filas)",
        "urls": [
            ("json", "https://hn.algolia.com/api/v1/search?tags=story&query=who%20is%20hiring"),
        ],
    },
    "posthog.com": {
        "estado": "activo (65 filas)",
        "urls": [
            ("json", "https://api.ashbyhq.com/posting-api/job-board/posthog"),
        ],
    },
    "python.org": {
        "estado": "activo (60 filas)",
        "urls": [
            ("rss", "https://www.python.org/jobs/feed/rss/"),
        ],
    },
    "builtin.com": {
        "estado": "activo (36 filas)",
        "urls": [
            ("rss", "https://builtin.com/jobs/rss"),
        ],
    },
    "arc.dev": {
        "estado": "activo (19 filas)",
        "urls": [
            ("json", "https://arc.dev/api/remote-jobs"),
            ("rss", "https://arc.dev/remote-jobs.rss"),
        ],
    },
    "himalayas.app": {
        "estado": "activo (6 filas) · 403 anti-bot documentado",
        "urls": [
            ("json", "https://himalayas.app/jobs/api?limit=20"),
            ("rss", "https://himalayas.app/jobs/rss"),
        ],
    },

    # ── Activo y vacío: el peor caso, cuenta como cobertura y no la da ───────
    "remotivated.com": {
        "estado": "ACTIVO Y VACIO (0 filas, sin diagnostico)",
        "urls": [
            ("rss", "https://remotivated.com/feed"),
            ("rss", "https://remotivated.com/rss"),
        ],
    },

    # ── Retirados 2026-07-11 ─────────────────────────────────────────────────
    "weworkremotely.com": {
        "estado": "RETIRADO: HTTP 403 anti-bot",
        "urls": [
            ("rss", "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
            ("rss", "https://weworkremotely.com/remote-jobs.rss"),
        ],
    },
    "startup.jobs": {
        "estado": "RETIRADO: HTTP 403 anti-bot",
        "urls": [
            ("rss", "https://startup.jobs/feed"),
            ("json", "https://startup.jobs/api/jobs"),
        ],
    },
    "wellfound.com": {
        "estado": "RETIRADO: 403 Cloudflare JS challenge",
        "urls": [
            ("rss", "https://wellfound.com/jobs.rss"),
            ("json", "https://wellfound.com/api/jobs"),
        ],
    },
    "welcometothejungle.com": {
        "estado": "RETIRADO: muro de login",
        "urls": [
            ("rss", "https://www.welcometothejungle.com/en/jobs.rss"),
        ],
    },
    "remote.co": {
        "estado": "RETIRADO: ERR_HTTP2_PROTOCOL_ERROR",
        "urls": [
            ("rss", "https://remote.co/feed/"),
            ("rss", "https://remote.co/remote-jobs/feed/"),
        ],
    },
    "trueup.io": {
        "estado": "RETIRADO: cambio endpoint + auth",
        "urls": [
            ("json", "https://api.trueup.io/jobs?remote=true"),
            ("rss", "https://trueup.io/feed"),
        ],
    },
    "greenhouse.com": {
        "estado": "RETIRADO: mal configurado (era el sitio de marketing del ATS)",
        "urls": [
            ("json", "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs"),
        ],
    },

    # ── Plataformas ATS: no son un board, son una clase de fuente ────────────
    "[ATS] greenhouse": {
        "estado": "plataforma — un token por empresa",
        "urls": [
            ("json", "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs"),
            ("json", "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"),
        ],
    },
    "[ATS] lever": {
        "estado": "plataforma — un token por empresa",
        "urls": [
            ("json", "https://api.lever.co/v0/postings/matchgroup?mode=json"),
        ],
    },
    "[ATS] ashby": {
        "estado": "plataforma — un token por empresa",
        "urls": [
            ("json", "https://api.ashbyhq.com/posting-api/job-board/posthog"),
        ],
    },
}


@dataclass
class Sonda:
    board: str
    estado: str
    tipo: str
    url: str
    http: Optional[int] = None
    ms: Optional[float] = None
    items: Optional[int] = None
    bytes_: int = 0
    campos: list[str] = field(default_factory=list)
    muestra: str = ""
    error: str = ""

    @property
    def sirve(self) -> bool:
        """Un 200 con cero items no sirve: es una página que responde, no una fuente."""
        return self.http == 200 and (self.items or 0) > 0


def _contar_json(cuerpo: bytes) -> tuple[Optional[int], list[str], str]:
    """Devuelve (items, campos del primer item, muestra). Tolera las tres formas
    que devuelven estos endpoints: lista suelta, {"jobs": [...]}, {"results": [...]}."""
    try:
        d = json.loads(cuerpo.decode("utf-8", errors="replace"))
    except Exception:
        return None, [], ""

    lista = None
    if isinstance(d, list):
        lista = d
    elif isinstance(d, dict):
        for k in ("jobs", "results", "data", "hits", "postings", "items"):
            if isinstance(d.get(k), list):
                lista = d[k]
                break
    if lista is None:
        return None, sorted(d.keys())[:12] if isinstance(d, dict) else [], ""

    # remoteok.com mete un objeto legal como primer elemento del array.
    reales = [x for x in lista if isinstance(x, dict) and "legal" not in x]
    if not reales:
        return len(lista), [], ""
    primero = reales[0]
    titulo = (primero.get("title") or primero.get("text")
              or primero.get("position") or primero.get("name") or "")
    return len(reales), sorted(primero.keys())[:14], str(titulo)[:CORTE_MUESTRA]


def _contar_rss(cuerpo: bytes) -> tuple[Optional[int], list[str], str]:
    x = cuerpo.decode("utf-8", errors="replace")
    n = x.count("<item") + x.count("<entry")
    if n == 0:
        return 0, [], ""
    titulos = re.findall(r"<title[^>]*>(.*?)</title>", x, re.S)
    # El primer <title> del feed es el del canal, no el de un aviso.
    muestra = titulos[1] if len(titulos) > 1 else (titulos[0] if titulos else "")
    muestra = re.sub(r"<!\[CDATA\[|\]\]>", "", muestra).strip()
    campos = sorted(set(re.findall(r"<(\w+)>", x)))[:14]
    return n, campos, muestra[:CORTE_MUESTRA]


def sondear(board: str, estado: str, tipo: str, url: str, ses: requests.Session) -> Sonda:
    s = Sonda(board=board, estado=estado, tipo=tipo, url=url)
    t0 = time.perf_counter()
    try:
        r = ses.get(url, timeout=TIMEOUT)
        s.ms = (time.perf_counter() - t0) * 1000
        s.http = r.status_code
        s.bytes_ = len(r.content)
        if r.status_code == 200:
            if tipo == "json":
                s.items, s.campos, s.muestra = _contar_json(r.content)
                if s.items is None:      # respondió 200 pero no es la forma esperada
                    s.items, s.campos, s.muestra = _contar_rss(r.content)
            else:
                s.items, s.campos, s.muestra = _contar_rss(r.content)
    except Exception as exc:
        s.ms = (time.perf_counter() - t0) * 1000
        s.error = f"{type(exc).__name__}: {str(exc)[:90]}"
    return s


def leer_robots(dominio: str, ses: requests.Session) -> str:
    """Qué dice el robots.txt. No es un permiso legal, es una señal declarada:
    un board que prohíbe explícitamente el path que ibas a pedir se registra como
    `declined` en la Fase 9 en vez de reintentarse tres veces."""
    try:
        r = ses.get(f"https://{dominio}/robots.txt", timeout=TIMEOUT)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        líneas = [l.strip() for l in r.text.splitlines()
                  if l.strip().lower().startswith(("user-agent:", "disallow:", "crawl-delay:"))]
        return " | ".join(líneas[:14]) or "(sin reglas)"
    except Exception as exc:
        return f"{type(exc).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe de endpoints públicos de job boards.")
    ap.add_argument("--json", metavar="ARCHIVO", help="volcar el resultado crudo a JSON")
    ap.add_argument("--robots", action="store_true", help="leer robots.txt de cada dominio")
    ap.add_argument("--solo", metavar="BOARD", help="probar un solo board")
    args = ap.parse_args()

    ses = requests.Session()
    ses.headers.update({"User-Agent": UA, "Accept": "*/*"})

    objetivos = CANDIDATOS
    if args.solo:
        objetivos = {k: v for k, v in CANDIDATOS.items() if args.solo in k}
        if not objetivos:
            print(f"No hay candidato que matchee {args.solo!r}.")
            return 2

    resultados: list[Sonda] = []
    print(f"{'board':<24} {'tipo':<5} {'http':>5} {'ms':>7} {'items':>6}  muestra")
    print("-" * 108)

    for board, cfg in objetivos.items():
        for tipo, url in cfg["urls"]:
            s = sondear(board, cfg["estado"], tipo, url, ses)
            resultados.append(s)
            marca = "OK " if s.sirve else ("-- " if s.http == 200 else "XX ")
            print(f"{marca}{board:<21} {tipo:<5} "
                  f"{s.http if s.http is not None else '---':>5} "
                  f"{s.ms:>7.0f} "
                  f"{s.items if s.items is not None else '-':>6}  "
                  f"{(s.muestra or s.error)[:58]}")
            time.sleep(PAUSA_S)

    # ── Resumen: qué board se rescata sin navegador ──────────────────────────
    por_board: dict[str, list[Sonda]] = {}
    for s in resultados:
        por_board.setdefault(s.board, []).append(s)

    sirven = {b: [s for s in ss if s.sirve] for b, ss in por_board.items()}
    con_fuente = {b: ss for b, ss in sirven.items() if ss}

    print()
    print("=" * 108)
    print(f"RESCATABLES SIN NAVEGADOR: {len(con_fuente)} de {len(por_board)} candidatos")
    print("=" * 108)
    for b, ss in con_fuente.items():
        mejor = min(ss, key=lambda s: s.ms or 1e9)
        print(f"  {b:<24} {mejor.tipo:<5} {mejor.items:>5} items  {mejor.ms:>6.0f} ms")
        print(f"    {mejor.url}")
        if mejor.campos:
            print(f"    campos: {', '.join(mejor.campos)}")

    sin_fuente = [b for b, ss in sirven.items() if not ss]
    if sin_fuente:
        print()
        print(f"SIN ENDPOINT ÚTIL ({len(sin_fuente)}): {', '.join(sin_fuente)}")
        print("  Estos quedan para camoufox, para revisión de template, o retirados.")

    if args.robots:
        print()
        print("=" * 108)
        print("ROBOTS.TXT")
        print("=" * 108)
        dominios = sorted({urlparse(s.url).netloc for s in resultados})
        for d in dominios:
            print(f"  {d:<34} {leer_robots(d, ses)[:150]}")
            time.sleep(PAUSA_S)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([{
                "board": s.board, "estado": s.estado, "tipo": s.tipo, "url": s.url,
                "http": s.http, "ms": s.ms, "items": s.items, "bytes": s.bytes_,
                "campos": s.campos, "muestra": s.muestra, "error": s.error,
                "sirve": s.sirve,
            } for s in resultados], fh, ensure_ascii=False, indent=1)
        print(f"\nJSON crudo en {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
