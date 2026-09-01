"""
llm_graph.py — one provider chain, replacing three.

SKELETON (Fase 1). Filled in Fase 3.

v2 wrote the provider x retry loop three times and the copies drifted:

    nlp_engine.py:654-704    correct. Schema error -> re-prompt the SAME provider with
                             the error injected. Network error -> next provider with a
                             CLEAN history. Exhaustion -> raise.
    nlp_engine.py:1021-1042  never self-corrects at all.
    nlp_engine.py:1282-1338  `except (json.JSONDecodeError, ValueError, Exception)` at
                             line 1318 — an except Exception wearing a disguise.

No test caught the drift because every test file builds its own provider mock. One graph
makes the other two inherit the correct semantics by construction rather than by review.

Nodes: select_provider, call_provider, parse, self_correct, next_provider, cooldown,
exhausted. Routing out of parse:

    parsed ok                              -> END
    schema and attempt < LLM_MAX_RETRIES   -> self_correct
    schema                                 -> next_provider
    rate_limit                             -> cooldown
    network                                -> next_provider

Two invariants to preserve verbatim, both easy to lose in a rewrite:

  - next_provider resets messages to the original system+user pair. Carrying a failed
    provider's history to the next one poisons it. The reasoning is written out at
    nlp_engine.py:689-695 and should be moved here, not paraphrased.
  - cooldowns must survive between invocations of this subgraph within a single run.
    The v2 flags persisted because they were module-level globals; a graph channel does
    not. They live in RuntimeContext and get copied into and out of LLMState each time.
    Getting this wrong reintroduces a 429 storm that looks like it works in tests.

Closes the open item recorded in LLM_PROVIDERS.md: a 429 no longer kills a provider for
the whole session. cooldowns[p] = now() + LLM_PROVIDER_COOLDOWN_S, and select_provider
reconsiders it once that passes.

parse also unifies the three JSON extraction paths v2 accumulated —
_extract_json_from_text:46, _parse_and_validate:601, and the inline fence strip at
1296-1307 — into a single parse_llm_json(raw, model_cls).

The four public entry points (parse_prompt_to_config, generate_and_audit_config,
generate_ephemeral_profile, extract_jobs_from_text) keep their signatures and become
thin wrappers over this graph, parameterized by prompt template, response model, and
on_exhaustion ("raise" or "empty"). That is what keeps the ~100 tests calling them by
signature green through the migration.
"""
