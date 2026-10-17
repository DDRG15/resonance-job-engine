"""
graph/ — GEMA v3: the pipeline as a LangGraph state graph.

Why this package exists. Three defects motivated it, all verified in the v2 code:
the provider x retry loop was hand-written three times with drifted semantics
(nlp_engine.py:654-704, 1021-1042, 1282-1338), the run was orchestrated twice with
different behaviour depending on the trigger (run_manager._worker vs
scheduler_service._fire), and nothing on the LLM side was instrumented — no latency,
no tokens, no fallback depth, and no runs table to persist any of it.

A state graph fixes the first two structurally rather than by discipline: there is one
provider subgraph, and one run graph invoked with a different `trigger`.

Module map:

    state.py            TypedDicts and reducers
    runtime.py          RuntimeContext, non-serializable
    instrumentation.py  @traced_node, LLMCallResult
    llm_graph.py        provider subgraph
    extract_graph.py    extraction subgraph
    run_graph.py        the run graph

Nothing is re-exported here: import the modules by path
(`from graph.llm_graph import ...`) rather than adding re-exports, so importing the
package stays cheap and free of import cycles.

CONVENTION FOR EVERY NODE IN THIS PACKAGE
-----------------------------------------
A node signature is `def node(state, config: RunnableConfig)`. Both halves are load
bearing and LangGraph enforces them by signature inspection:

  - The parameter MUST be spelled `config`. Any other name and the node is invoked with
    one argument and dies with a TypeError at execution time, not at import — so it
    presents as a graph bug rather than a typo.
  - It MUST be annotated `RunnableConfig`. With a bare `dict` annotation LangGraph warns
    and does not inject, which fails the same way while looking like it was configured.

Import the type from `langgraph.types`, not from `langchain_core.runnables`. It is the
same object reexported, and it keeps langchain out of the surface this project declares
it uses.

The consequence: inside a node, `config` is the runnable config, never GEMA's settings
module. Modules here import the settings as `import config as app_config`.
"""
