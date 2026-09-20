"""
run_scrape_now.py — Corrida one-shot por linea de comandos.

Usa el MISMO grafo que la API y el cron, con trigger="cli". Sin Streamlit, sin
servidor.

    python run_scrape_now.py

Por que importa que pase por el grafo. Hasta el 2026-09-19 este script llamaba a
`run_scrape_session` y `bucket_jobs` directo, saltandose `graph/run_graph.py`. La
corrida de calibracion del 17-09 —1.095 avisos, la mas grande del proyecto— salio por
aca, y dejo CERO filas en `runs` y CERO en `node_traces`. Ademas se salteaba
`barrer_vencidos()`, que vive en el nodo `init_run` y se diseño para correr "en cada
arranque sin pensarlo": en el arranque que importo, no corrio.

Lo que se gana entrando por el grafo, sin escribir una linea de telemetria nueva:
    init_run     record_run_start + barrer_vencidos
    @traced_node una fila de node_traces por nodo
    persist_run  record_run_end
    notify       el reporte a Discord, en el MISMO formato que los otros triggers
    push         Notion y Sheets, aislados

Salida:
    - Resultados en gema_registry.db (la misma base que usa la UI)
    - Notificacion de Discord con el conteo por tier
    - Log en consola mientras corre
"""

import logging
import queue
import sys
from pathlib import Path

import yaml

# Las consolas de Windows arrancan en cp1252 y el StreamHandler de logging explota con
# cualquier caracter fuera de latin-1 (la flecha '->' de los logs del scraper, sin ir
# mas lejos).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_scrape_now")


def _load_profile() -> dict:
    path = Path(__file__).parent / "user_profile.yaml"
    if not path.exists():
        logger.warning("user_profile.yaml not found — scoring will use generic defaults.")
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_config():
    """
    La busqueda del CLI: todos los boards registrados, sin piso de salario.

    Vive aparte de main() para que un test la pueda construir sin correr la corrida.
    """
    from models import SearchConfig
    import selectors_registry

    return SearchConfig(
        target_titles=[
            # ── Original titles ───────────────────────────────────────────────
            "Python Backend Developer",
            "Backend Engineer",
            "Python Developer",
            "SRE",
            "Site Reliability Engineer",
            "DevOps Engineer",
            "Data Engineer",
            "Software Engineer Python",

            # ── Junior / entry-level angle ────────────────────────────────────
            "Junior Python Developer",
            "Junior Backend Developer",
            "Junior Backend Engineer",
            "Junior Python Engineer",
            "Junior Software Engineer",
            "Junior Developer",
            "Junior Data Engineer",
            "Junior SRE",
            "Junior DevOps Engineer",
            "Entry Level Python Developer",
            "Associate Python Developer",

            # ── FastAPI / asyncio / framework-specific ────────────────────────
            "FastAPI Developer",
            "FastAPI Python",
            "Python FastAPI",
            "Python asyncio Developer",

            # ── Delivery Engineer (user-requested angle) ──────────────────────
            "Delivery Engineer",
            "Python Delivery Engineer",
            "Software Delivery Engineer",

            # ── Integration / API integration (user-requested) ────────────────
            "Integration Engineer",
            "API Integration Engineer",
            "Integration Developer",
            "Python Integration Developer",
            "API Integration Developer",
            "API Developer Python",
            "Python API Developer",

            # ── Projects from CV: scraping / automation ───────────────────────
            "Automation Engineer Python",
            "Python Automation Engineer",
            "Web Scraping Python",
            "Python Scraping Developer",

            # ── Projects from CV: data pipeline / ETL ────────────────────────
            "Data Pipeline Engineer",
            "Python Data Pipeline",
            "ETL Developer Python",

            # ── Projects from CV: reliability / observability ─────────────────
            "Python SRE",
            "Reliability Engineer Python",
            "Platform Engineer Python",
            "Python DevOps Engineer",
            "Observability Engineer",

            # ── Webhook / event-driven (Micro-Billing-Ledger) ─────────────────
            "Webhook Developer Python",
            "Event Driven Developer",

            # ── Single-word tags (for tag-based boards like workingnomads) ────
            "python",
            "fastapi",
            "backend",
            "junior",
            "sre",
        ],
        must_include=[],
        must_exclude=[],
        min_salary=None,
        target_domains=list(selectors_registry.SELECTORS.keys()),
    )


def main() -> int:
    from database import GemaDatabase
    from graph.run_graph import run_pipeline
    from graph.runtime import RuntimeContext
    from integrations import NotionClient, SheetsClient
    from integrations.webhook_client import send_discord_alert

    profile = _load_profile()
    logger.info("Profile loaded: %s | %d skills | %d audit signals",
                profile.get("role", "Python Backend Developer"),
                len(profile.get("core_skills", [])),
                len(profile.get("audit_signals", [])))

    cfg = build_config()
    logger.info("Search config: %d titles × %d boards",
                len(cfg.target_titles), len(cfg.target_domains))

    db = GemaDatabase()
    log_q: queue.Queue = queue.Queue()

    # Notion y Sheets se arman igual que en api.py. Si se dejaran en None, una corrida
    # por CLI no empujaria a ningun lado y estariamos inventando la misma divergencia
    # que este cambio vino a cerrar, corrida un escalon mas abajo.
    ctx = RuntimeContext(
        db=db,
        notion=NotionClient(),
        sheets=SheetsClient(),
        discord=send_discord_alert,
        log=lambda msg: logger.info("[GEMA] %s", msg),
    )

    send_discord_alert(
        f"⌨️ GEMA scrape por CLI — {len(cfg.target_titles)} títulos × "
        f"{len(cfg.target_domains)} boards. Sin filtro de salario."
    )

    logger.info("Launching pipeline (trigger=cli)...")
    try:
        estado = run_pipeline(
            config=cfg,
            ctx=ctx,
            trigger="cli",
            profile=profile,
            ttl_hours=48,
            log_queue=log_q,
        )
    except Exception as exc:
        # run_pipeline ya cerro la fila de runs como 'crashed' antes de re-lanzar; lo
        # unico que falta es que el humano se entere sin leer la base.
        logger.error("Pipeline crashed: %s", exc)
        send_discord_alert(f"🔴 GEMA scrape por CLI FALLO: {exc}")
        return 1

    t1, t2, t3, t4 = (estado.get("tier1") or [], estado.get("tier2") or [],
                      estado.get("tier3") or [], estado.get("tier4") or [])
    total = len(estado.get("jobs") or [])

    # El reporte a Discord ya lo mando el nodo `notify`, con el mismo formato que la API
    # y el cron. Duplicarlo aca era justamente la divergencia: dos mensajes distintos
    # para la misma corrida segun por donde entro.
    logger.info("Run %s (%s) — T1:%d T2:%d T3:%d T4:%d",
                (estado.get("run_id") or "?")[:8], estado.get("status"),
                len(t1), len(t2), len(t3), len(t4))

    print("\n" + "=" * 60)
    print(f"  DONE — {total} avisos nuevos | T1:{len(t1)} T2:{len(t2)} "
          f"T3:{len(t3)} T4:{len(t4)}")
    print(f"  run_id: {estado.get('run_id')}  ·  status: {estado.get('status')}")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
