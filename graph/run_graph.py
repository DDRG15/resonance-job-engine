"""
run_graph.py — one run graph, replacing two orchestrators.

SKELETON (Fase 1). Filled in Fase 6.

v2 orchestrated a run in two places that drifted apart: run_manager._worker:182 pushes
to Notion and Sheets, while scheduler_service._fire:219 does not and hardcodes
ttl_hours=24. Same responsibility, different behaviour depending on who pulled the
trigger. After this phase both are one call to this graph with a different `trigger`
("api" | "cron" | "cli"), so the duplication is gone structurally.

Nodes:

    init_run      run_id, clear ctx.cooldowns, db.backup(), open the runs row
    probe_boards  probe_all_boards -> BoardStrategyRegistry
    plan_fanout   Send() per (domain, title), skipping domains with an open circuit
    scrape_srp    xN, the fan-out. Wraps _scrape_srp:1306
    camoufox      sequential, after the fan-out
    drain_writer  sentinel + await the writer task
    summarize     summary + breaker_state_json
    bucket        bucket_jobs -> tier1..tier4
    persist_run   record_run_end, INCLUDING tier4_count
    notify        Discord, isolated
    push          Notion + Sheets, each isolated

Two things to get right, both regressions if missed:

  - Every scrape_srp node must be wrapped so a failure degrades to an empty result plus
    an entry in `errors`. LangGraph propagates; asyncio.gather(return_exceptions=True)
    did not. Without the wrapper one dead board takes down the whole run, which is
    strictly worse than v2. The wrapper is @traced_node, so this holds by default.
  - scrape_srp acquires ctx.sem inside itself. Send has no per-fan-out concurrency
    limit, and config={"max_concurrency": N} is not a substitute: it bounds LangGraph's
    executor, not the number of Chromium tabs actually open.

persist_run finally writes tier4_count, which ScrapeRunSummary never carried even though
bucket_jobs has always returned a fourth tier — the SSE payload and the Discord report
each recomputed it on the side.

Out of scope here on purpose: try_acquire_scrape_gate stays where it is
(scraper.py:1917, acquired in run_manager.start:132 and released in _worker:252, in a
different module). Moving it is an orthogonal change; doing both at once makes a failure
impossible to attribute.
"""
