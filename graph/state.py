"""
state.py — the graph state schemas and their reducers.

SKELETON (Fase 1). LLMState lands in Fase 3; RunState and its reducers in Fase 6.

The rule that governs this module: a channel belongs in the state only if merging
concurrent writes AT THE END of a superstep is correct. LangGraph applies reducers when
the superstep closes, not while branches run, so any value a branch must read back
mid-flight — after a sibling branch changed it — cannot live here. Those go to
graph/runtime.py instead, and that split is not a matter of taste.

Fits the state (Fase 6):

    jobs      Annotated[list[JobResult], operator.add]
    errors    Annotated[list[str], operator.add]
    counters  Annotated[Counters, merge_counters]   -- field-by-field sum

Does NOT fit, and why (all move to RuntimeContext):

    _session_seen      dedup checked and mutated inside a branch; a sibling must see
                       the effect immediately or two domains extract the same job
    _visited_srp_urls  same
    CircuitBreaker     _bounded() reads is_open before AND after the semaphore
                       precisely because a sibling may have opened it in between
"""
