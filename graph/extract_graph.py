"""
extract_graph.py — LLM extraction fallback, plus the fix to its own instrumentation.

SKELETON (Fase 1). Filled in Fase 5.

Wraps scraper._llm_extraction_fallback:1465 as a subgraph:

    build_prompt -> llm_subgraph(on_exhaustion="empty") -> ground -> repair_url
                 -> salvage -> snapshot

The grounding step is the anti-hallucination gate and stays exactly as strict as it is
today: a recovered job survives only if its URL appears literally in the text that was
sent to the model. A Phase 30b repair is accepted only if the repaired URL is grounded
too. Jobs with a real title but a bad URL go to the manual_review pile rather than being
discarded — never blanket-drop salvageable data.

This phase also repairs the drift capture, which is a prerequisite for the eval harness
in Fase 7 and is broken today in three ways:

  - _snapshot_drift_failure:480 stores sent_text[:8000] while the extraction truncates
    its input at LLM_FALLBACK_MAX_INPUT_CHARS (20000). Grounding is judged against the
    text that was sent, so the URL that decides the verdict can sit in the 8000-20000
    range that was never written down. Store the full text; compress it if size matters.
  - llm_output is written as str(...)[:4000] — a Python repr, not JSON, so the recorded
    output cannot be parsed back. Store [j.model_dump() for j in jobs].
  - No prompt_version and no provider are recorded, so two runs cannot be compared
    across a prompt change, which is the main thing an eval is for.

And one addition: snapshot on SUCCESS too, sampled (roughly 1 in 10). A corpus made only
of failures yields a regression gate that measures the worst case and nothing else.
"""
