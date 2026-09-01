"""
runtime.py — RuntimeContext: everything the graph needs that cannot live in the state.

SKELETON (Fase 1). Filled in Fase 6.

Two independent reasons put an object here rather than in graph/state.py.

1. It is not serializable, and its lifetime is owned by a context manager. The
   Playwright BrowserContext is the case that forces the design: scraper.py builds ONE
   shared context inside `async with async_playwright()`, and a graph node cannot own
   that. So the graph is invoked from inside the `async with`, and the resources arrive
   through config["configurable"] — LangGraph's explicit channel for exactly this.

2. It is shared mutable state that branches must read back mid-superstep. Reducers run
   when the superstep closes, which is too late for a dedup set or a circuit breaker.
   This is safe here for a specific reason, not by luck: asyncio is single-threaded and
   the check-then-mutate has no `await` between the two halves.

Expected members:

    browser_ctx    Playwright BrowserContext, created and closed by the caller
    db             GemaDatabase
    circuit        CircuitBreaker      -- per-run, mid-superstep reads
    session_seen   set[str]            -- per-run dedup, mid-superstep reads
    visited_srp    set[str]            -- per-run dedup, mid-superstep reads
    write_q        asyncio.Queue       -- producers are nodes, consumer is a long-lived
                                          task; LangGraph cannot express "run alongside
                                          the graph forever", so the task is started and
                                          drained by the caller
    sem            asyncio.Semaphore   -- bounds OPEN CHROMIUM TABS. Send dispatches every
                                          branch at once and has no per-fan-out limit, so
                                          each node acquires this inside itself. That makes
                                          graph-level parallelism nominal: node_traces
                                          records queue_wait_ms apart from exec_ms, or the
                                          per-node timings lie.
    cooldowns      dict[str, float]    -- provider -> epoch when it becomes eligible again.
                                          Replaces the module-level rate-limit globals of
                                          nlp_engine.py:156-159. Those persisted across
                                          calls by accident of being module-level; this one
                                          only persists because it lives here, so it must be
                                          copied into and back out of LLMState per invocation.
    strategy       BoardStrategyRegistry
    webhook        webhook client
    run_id         str
"""
