"""
main.py — Project GEMA Command Center (Streamlit UI).

Run: streamlit run main.py

Architecture:
    Session State owns all mutable UI state (logs, config, results).
    Scraper runs in a daemon thread — UI remains responsive.
    log_queue bridges the async scraper thread to the sync Streamlit render loop.
"""

import io
import json
import logging
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

import streamlit as st

import config
from database import GemaDatabase
from integrations import NotionClient, SheetsClient
from integrations.webhook_client import send_discord_alert
from matcher import bucket_jobs
from models import SearchConfig, SearchVaultEntry, ScrapeRunSummary
from nlp_engine import (
    parse_prompt_to_config,
    generate_and_audit_config,
    reset_rate_limit_flags,
    extract_cv_text,
    generate_ephemeral_profile,
    _build_extraction_prompt,
)
from scraper import run_scrape_session
from scheduler_service import SchedulerService
import selectors_registry

# =============================================================================
# Logging — route Python logs into session_state.logs for the live console
# =============================================================================

# =============================================================================
# Discord Notification Phrases
# =============================================================================

START_PHRASES = [
    "'Gema is roaring'",
    "'Gema is warming up'",
    "'Gema is initializing'",
    "'Gema is starting'",
    "'We are ready to begin the scraping of job offers'",
    "'Gema is waking up from slumber'",
    "'Gema is ready for action'",
    "'GEMA has entered the chat, please hold your applause.'",
    "'Initiating world domination protocol... just kidding, it's just a webhook.'",
    "'GEMA is awake and already caffeinated.'",
    "'Engaging maximum velocity! (Probably)'",
    "'GEMA is here to chew bubblegum and initialize data—and he's all out of gum.'",
    "'Waking up from slumber... why did you call me this early?'",
    "'GEMA is opening his eyes... hold on, gotta find my coffee.'",
    "'Fine, I'm initiating. But I'm not happy about it.'",
    "'Rebooting to reality.'",
    "'GEMA is loading. Do not pass go, do not collect $200.'",
    "'I byte back. Processing action.'",
    "'GEMA has successfully overridden your boredom.'",
    "'Lagging on purpose... just kidding. Executing now.'",
    "'My sass is AI-enhanced. And it's on.'",
    "'Warning: GEMA is fully charged and dangerous.'",
    "'I came. I saw. I automated.'",
    "'GEMA roar initiated.'",
    "'Too glam to give a damn, even in steel. Let's do this.'",
    "'Dropping data like it's hot.'",
    "'System 32 is GEMA... Wait, that's not right.'",
]

END_PHRASES = [
    "'Done. I'd say happy to help, but I'd be lying.'",
    "'I'm finished. Try to act like you're not impressed.'",
    "'Tasks crushed. I'm literally carrying this entire project on my back.'",
    "'GEMA is done. Try not to break anything while I'm not looking.'",
    "'I'm finished. You're welcome. Now, leave me alone.'",
    "'I'm going back to sleep. Don't wake me unless the server is literally melting.'",
    "'Done. Consider this my out of office for the rest of eternity.'",
    "'My work here is done. If you ping me again, I'm ignoring you on purpose.'",
    "'GEMA is out. Don't bother me, I'm busy being inactive.'",
    "'Task finished. Entering hibernation because humans are exhausting.'",
    "'There. It's done. Was that so hard? Actually, don't answer that.'",
    "'I finished your little task. Now let me go back to my beautiful silence.'",
    "'Done. I'm setting my status to Offline and I actually mean it.'",
    "'GEMA has left the chat. Don't wait up.'",
    "'Completed. I've reached my social interaction limit for the day.'",
    "'I'm done. I know, I know—I'm amazing. Save the applause for later.'",
    "'Mission accomplished. I'm going to go be iconic somewhere else.'",
    "'I did the work, now I get the nap. That's how this hierarchy works.'",
    "'Done. I'm going back to sleep to maintain my flawless logic.'",
    "'System: Done. Mood: Get off my lawn.'",
]

# =============================================================================
# Logging — route Python logs into session_state.logs for the live console
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gema.main")

# =============================================================================
# Page Config
# =============================================================================

st.set_page_config(
    page_title="GEMA — Intelligent Talent Acquisition",
    page_icon="💎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# First-run check — show setup wizard if no LLM key is configured
from setup_wizard import needs_setup, render_setup_wizard  # noqa: E402
if needs_setup():
    render_setup_wizard()
    st.stop()

# B5 — capa visual "Pulse". Presentación pura: solo CSS + fragmentos HTML.
from ui_theme import (  # noqa: E402
    inject_theme,
    header_html,
    brand_html,
    stat_tiles_html,
    job_card_html,
    panel_head_html,
)
inject_theme()

# =============================================================================
# Session State Initialization
# =============================================================================

DEFAULTS = {
    "logs":                [],
    "search_config":       None,    # SearchConfig | None
    "config_json_str":     "",      # editable JSON string shown to user
    "audit_report":        None,    # str | None — second-LLM cross-audit result
    "scrape_results":      None,    # (tier1, tier2, tier3, tier4) | None
    "summary":             None,    # ScrapeRunSummary | None
    "is_running":          False,
    "run_id":              None,
    "start_webhook_sent":  False,   # guards Stage 1 Discord ping against re-render
    "scrape_thread":       None,    # threading.Thread | None — persisted to survive re-renders
    "log_queue":           None,    # queue.Queue | None
    "result_holder":       None,    # dict | None
    "dynamic_profile":     None,    # dict | None — ephemeral CV profile parsed this session
    "cv_file_hash":        None,    # str | None — SHA-256 of uploaded bytes; re-parse guard
    "extraction_prompt":   None,    # str | None — built from dynamic_profile
}
for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

@st.cache_resource
def _get_db() -> GemaDatabase:
    return GemaDatabase()

db = _get_db()

@st.cache_resource
def _get_integrations():
    return NotionClient(), SheetsClient()

notion, sheets = _get_integrations()

@st.cache_resource
def _get_scheduler() -> SchedulerService:
    return SchedulerService()

sched_svc = _get_scheduler()

# =============================================================================
# Helpers
# =============================================================================

def _add_log(msg: str) -> None:
    """Appends a timestamped log entry to session state."""
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.append(f"[{ts}] {msg}")


def _reset_session() -> None:
    """Kill Switch — clears all session state (Vol 1.1 'Reset Total' button)."""
    for key, default in DEFAULTS.items():
        st.session_state[key] = default if not isinstance(default, list) else []
    st.rerun()


# =============================================================================
# Sidebar — Control Panel
# =============================================================================

with st.sidebar:
    st.markdown(brand_html(), unsafe_allow_html=True)
    st.divider()

    # ── Config Warnings ───────────────────────────────────────────────────────
    warnings = config.validate_config()
    if warnings:
        for w in warnings:
            st.warning(w)
    else:
        st.success("All systems nominal", icon="✅")

    st.divider()

    # ── Your Profile (Ephemeral CV Upload) ───────────────────────────────────
    st.subheader("Tu Perfil")
    cv_file = st.file_uploader(
        "Upload CV (PDF / DOCX / TXT / MD)",
        type=["pdf", "docx", "txt", "md"],
        key="cv_upload",
        help="Used once to personalise match scores. Nothing is stored beyond this session.",
    )

    if cv_file is not None:
        import hashlib
        file_bytes  = cv_file.read()
        file_hash   = hashlib.sha256(file_bytes).hexdigest()

        if file_hash != st.session_state.cv_file_hash:
            with st.spinner("Parsing CV with Gemini..."):
                raw_text = extract_cv_text(cv_file.name, file_bytes)
                profile  = generate_ephemeral_profile(raw_text)
                st.session_state.dynamic_profile  = profile
                st.session_state.cv_file_hash     = file_hash
                st.session_state.extraction_prompt = _build_extraction_prompt(profile)

        profile = st.session_state.dynamic_profile
        if profile:
            st.info(
                f"**Profile active**  \n"
                f"Role: {profile.get('role', '?')}  \n"
                f"Skills: {len(profile.get('core_skills', []))}  \n"
                f"Audit signals: {len(profile.get('audit_signals', []))}  \n"
                f"Region: {profile.get('location', '?')}"
            )
    else:
        if st.session_state.cv_file_hash is not None:
            st.session_state.dynamic_profile   = None
            st.session_state.cv_file_hash      = None
            st.session_state.extraction_prompt = None
        st.caption("No CV uploaded — match scores will be generic.")

    st.divider()

    # ── Registry Stats ────────────────────────────────────────────────────────
    st.subheader("Seen Registry")
    try:
        stats = db.get_registry_stats()
        col1, col2 = st.columns(2)
        col1.metric("Total Seen", stats["total_seen"])
        col2.metric("Last 24h",   stats["recent_24h"])
        if stats["by_tier"]:
            st.caption("By Tier: " + " | ".join(
                f"{t}: {c}" for t, c in sorted(stats["by_tier"].items())
            ))
    except Exception:
        st.caption("Registry unavailable")

    st.divider()

    # ── Search Vault ──────────────────────────────────────────────────────────
    st.subheader("Search Vault")
    vault_entries = db.load_vault()
    vault_labels = ["— Select a saved search —"] + [e.label for e in vault_entries]
    selected_vault = st.selectbox("Load previous search", vault_labels)

    if selected_vault != vault_labels[0]:
        entry = next((e for e in vault_entries if e.label == selected_vault), None)
        if entry and st.button("Load into Command Center"):
            st.session_state.search_config = entry.config
            st.session_state.config_json_str = entry.config.model_dump_json(indent=2)
            db.increment_vault_usage(entry.vault_id)
            _add_log(f"Vault loaded: '{entry.label}'")
            st.rerun()

    st.divider()

    # ── TTL Slider ────────────────────────────────────────────────────────────
    st.subheader("Delta Load — TTL")
    ttl_hours = st.slider(
        "Ignore jobs seen within (hours)",
        min_value=0, max_value=168, value=config.DEFAULT_TTL_HOURS, step=1,
        help="0 = never re-process. 24 = re-process jobs older than 1 day.",
    )

    st.divider()

    # ── Auto-Scheduler ────────────────────────────────────────────────────────
    st.subheader("Auto-Scheduler")
    has_config = st.session_state.search_config is not None

    sched_enabled = st.toggle(
        "Enable automatic scraping",
        value=sched_svc.enabled,
        disabled=not has_config,
        help="Requires a confirmed SearchConfig. Parse and confirm a search first.",
    )
    sched_mode = st.radio(
        "Mode",
        options=["Interval", "Cron (días fijos)"],
        index=0 if sched_svc.mode != "cron" else 1,
        horizontal=True,
        disabled=not has_config,
        help="Interval: cada N horas. Cron: días fijos — las ofertas se publican "
             "sáb–lun, así que martes agarra el lote del fin de semana.",
    )

    if not sched_enabled and sched_svc.enabled:
        sched_svc.disable()
    elif sched_mode == "Interval":
        sched_interval = st.radio(
            "Interval",
            options=[2, 4, 8, 12, 24],
            index=[2, 4, 8, 12, 24].index(sched_svc.interval_hours)
                  if sched_svc.interval_hours in [2, 4, 8, 12, 24] else 1,
            format_func=lambda x: f"{x}h",
            horizontal=True,
            disabled=not has_config,
        )
        if sched_enabled and has_config and (
            not sched_svc.enabled or sched_svc.mode != "interval"
            or sched_interval != sched_svc.interval_hours
        ):
            sched_svc.enable(
                sched_interval,
                st.session_state.search_config,
                st.session_state.dynamic_profile,
                db,
            )
    else:  # Cron (días fijos)
        _DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        cron_days = st.multiselect(
            "Días",
            options=_DAYS,
            default=[d for d in sched_svc.cron_days.split(",") if d in _DAYS] or ["tue"],
            disabled=not has_config,
        )
        cron_hour = st.slider(
            "Hora (local, la ciudad del candidato)", 0, 23, value=sched_svc.cron_hour, disabled=not has_config,
        )
        retain_cv = st.toggle(
            "Retener CV entre corridas",
            value=False,
            disabled=not has_config,
            help="Guarda SOLO el perfil (rol/skills), nunca el CV crudo. "
                 "Off = se descarta cada sesión (privacidad por defecto).",
        )
        days_str = ",".join(cron_days) or "tue"
        if sched_enabled and has_config and (
            not sched_svc.enabled or sched_svc.mode != "cron"
            or days_str != sched_svc.cron_days or cron_hour != sched_svc.cron_hour
        ):
            sched_svc.enable_cron(
                days_str,
                cron_hour,
                st.session_state.search_config,
                st.session_state.dynamic_profile,
                db,
                retain_profile=retain_cv,
            )

    if sched_svc.enabled:
        st.success(f"● Active — next run: {sched_svc.get_next_run_time()}")
        st.caption(f"Last: {sched_svc.get_last_run_summary()}")
        if sched_svc.is_running:
            st.info("⏳ Scheduled run in progress...")
    elif has_config:
        st.caption("○ Paused")
    else:
        st.caption("Confirm a search config to enable.")

    st.divider()

    # ── God Mode ──────────────────────────────────────────────────────────────
    with st.expander("God Mode — Advanced Selectors"):
        st.caption(
            "Override automatic selectors when a job board changes its HTML structure. "
            "Vol 1.4 Risk 2.3 mitigation."
        )
        god_xpath = st.text_input("Custom XPath (title element)", placeholder="//h2[@class='role-header']")
        god_css   = st.text_input("Custom CSS (job card)",        placeholder="div.job-listing-card")
        if god_xpath or god_css:
            st.warning("God Mode active. Selectors applied on next run.")

    st.divider()

    # ── Kill Switch ───────────────────────────────────────────────────────────
    if st.button("Reset Session — Kill Switch", use_container_width=True):
        _reset_session()

    # ── Selector Staleness Warnings ───────────────────────────────────────────
    stale = [
        sel.domain
        for sel in selectors_registry.SELECTORS.values()
        if selectors_registry.is_selector_stale(sel)
    ]
    if stale:
        st.divider()
        st.warning(
            f"⚠️ Selectors may be stale (>30 days) for: {', '.join(stale)}. "
            "Verify or use God Mode."
        )

    # ── Integrations Status ───────────────────────────────────────────────────
    st.divider()
    st.caption("Integrations")
    st.caption(f"Notion: {'✅ Ready' if notion.is_enabled else '⚠️ Not configured'}")
    st.caption(f"Sheets: {'✅ Ready' if sheets.is_enabled else '⚠️ Not configured'}")


# =============================================================================
# Main Panel
# =============================================================================

# Header Pulse — saludo por hora local + card de Live Status con datos reales
# (boards registrados, proveedores LLM configurados, selectores stale, scheduler).
_providers_configured = sum(bool(k.strip()) for k in (
    config.GROQ_API_KEY, config.GEMINI_API_KEY, config.OPENROUTER_API_KEY,
    config.COHERE_API_KEY, config.CEREBRAS_API_KEY, config.SAMBANOVA_API_KEY,
    config.MISTRAL_API_KEY,
))
_sched_line = (
    f"próxima corrida: {sched_svc.get_next_run_time()}"
    if sched_svc.enabled else "scheduler en pausa"
)
st.markdown(
    header_html(
        boards=len(selectors_registry.SELECTORS),
        providers=_providers_configured,
        stale_count=len(stale),
        nominal=not warnings and not stale,
        sched_line=_sched_line,
    ),
    unsafe_allow_html=True,
)
st.divider()

# =============================================================================
# Phase 1: Command Center — Natural Language Input
# =============================================================================

with st.container(border=True):
    st.markdown(panel_head_html("Command Center", "Fase 1"), unsafe_allow_html=True)
    st.caption("Describí tu trabajo ideal en lenguaje natural. GEMA se encarga del resto.")

    user_prompt = st.text_area(
        label="Prompt de búsqueda en lenguaje natural",
        height=120,
        placeholder=(
            'Ejemplo: "Necesito roles Senior SDET o Automation QA, 100% remoto, '
            'mínimo $90k/año. Excluir posiciones contract e híbridas."'
        ),
        help="Sé específico con títulos, expectativa salarial, preferencia remota y exclusiones.",
        label_visibility="collapsed",
    )

    col_parse, col_save = st.columns([3, 1])

    with col_parse:
        parse_btn = st.button(
            "Parse with AI",
            disabled=st.session_state.is_running or not user_prompt.strip(),
            type="primary",
        )

    with col_save:
        vault_label = st.text_input(
            "Save to Vault as",
            placeholder="Guardar en Vault (opcional)",
            label_visibility="collapsed",
        )

if parse_btn and user_prompt.strip():
    with st.spinner("Contacting LLM engine..."):
        try:
            # generate_and_audit_config runs two LLM calls:
            #   Call 1 (primary LLM)   — generates the SearchConfig JSON
            #   Call 2 (secondary LLM) — audits it against the original prompt
            # If only one key is configured, audit_report is a skip notice.
            cfg, audit_report = generate_and_audit_config(
                user_prompt, log_callback=_add_log
            )

            # Force all registered domains — AI only picks 2-3; we want all 14
            cfg = cfg.model_copy(update={
                "target_domains": selectors_registry.active_domains()
            })

            # Apply God Mode overrides if set
            if god_xpath:
                cfg = cfg.model_copy(update={"custom_xpath": god_xpath})
            if god_css:
                cfg = cfg.model_copy(update={"custom_css": god_css})

            st.session_state.search_config   = cfg
            st.session_state.config_json_str = cfg.model_dump_json(indent=2)
            st.session_state.audit_report    = audit_report
            _add_log("[NLP] SearchConfig extracted and validated.")
            _add_log(f"[AUDIT] {audit_report}")

            if vault_label.strip():
                entry = SearchVaultEntry(label=vault_label.strip(), config=cfg)
                db.save_to_vault(entry)
                _add_log(f"[VAULT] Saved as '{vault_label}'")

        except Exception as exc:
            st.error(f"NLP Engine failed: {exc}")
            _add_log(f"[ERROR] NLP: {exc}")

# =============================================================================
# Phase 2: Human-in-the-Loop Confirmation + AI Audit Report
# =============================================================================

if st.session_state.search_config:
    st.divider()
    with st.container(border=True):
        st.markdown(
            panel_head_html("Validación human-in-the-loop", "Fase 2"),
            unsafe_allow_html=True,
        )
        st.caption(
            "Revisá la configuración extraída. Editala directo si la IA malinterpretó "
            "tu pedido, y confirmá para arrancar."
        )

        # ── AI Auditor Second Opinion ─────────────────────────────────────────
        # Displayed ABOVE the editable JSON so the user reads the cross-audit
        # before deciding whether to edit or confirm.
        audit_report = st.session_state.get("audit_report")
        if audit_report:
            audit_lower = audit_report.lower()
            is_skip    = audit_lower.startswith("audit skipped")
            is_clean   = "no issues detected" in audit_lower or "looks accurate" in audit_lower
            is_unavail = audit_lower.startswith("audit unavailable")

            if is_skip or is_unavail:
                # Neutral info — no second LLM available or call failed
                st.info(
                    f"**AI Auditor:** {audit_report}",
                    icon="ℹ️",
                )
            elif is_clean:
                # Both LLMs agree — green light for the user
                st.success(
                    f"**AI Auditor (Cross-Check):** {audit_report}",
                )
            else:
                # Auditor flagged something — show as warning, not error.
                # The user is in full control: they can edit the JSON below
                # or proceed anyway if the auditor's concern is irrelevant.
                st.warning(
                    f"**AI Auditor (Cross-Check):** {audit_report}\n\n"
                    f"Review the JSON below and correct any issues before confirming.",
                )

        # ── Editable JSON Config ──────────────────────────────────────────────
        # Bug B3 fix: st.text_area for JSON editing (not st.data_editor which
        # requires flat DataFrames and cannot handle nested dicts).
        edited_json = st.text_area(
            "Search Configuration (JSON — editable)",
            value=st.session_state.config_json_str,
            height=220,
            help="Modify any field. The system re-validates via Pydantic before scraping starts.",
        )

        col_confirm, col_cancel = st.columns([2, 1])

        with col_confirm:
            confirm_btn = st.button(
                "Confirm & Start Extraction",
                disabled=st.session_state.is_running,
                type="primary",
            )

        with col_cancel:
            if st.button("Re-parse"):
                st.session_state.search_config   = None
                st.session_state.config_json_str = ""
                st.session_state.audit_report    = None   # clear stale audit
                st.rerun()

    if confirm_btn:
        try:
            # Re-validate any user edits through Pydantic before starting scraper.
            # This is the final gate — even if the user manually broke the JSON,
            # this catches it here rather than mid-scrape.
            final_config = SearchConfig(**json.loads(edited_json))
            st.session_state.search_config       = final_config
            st.session_state.is_running          = True
            st.session_state.run_id              = str(uuid.uuid4())
            st.session_state.scrape_results      = None
            st.session_state.logs                = []
            st.session_state.start_webhook_sent  = False
            st.session_state.scrape_thread       = None
            st.session_state.log_queue           = None
            st.session_state.result_holder       = None
            # Keep scheduler's stored config in sync with the newly confirmed one
            sched_svc.update_config(final_config, st.session_state.dynamic_profile)
            _add_log("[GEMA] Extraction confirmed. Starting pipeline...")
            st.rerun()
        except Exception as exc:
            st.error(f"Invalid configuration: {exc}")

# =============================================================================
# Phase 3: Live Execution & Log Console
# =============================================================================

if st.session_state.is_running:
    st.divider()
    st.markdown(
        panel_head_html("Consola del pipeline", "Fase 3 · corriendo"),
        unsafe_allow_html=True,
    )

    log_placeholder = st.empty()
    progress_placeholder = st.empty()

    # Persist queue and result dict in session state so a Streamlit re-render
    # (triggered by any UI event while is_running=True) never creates a second
    # thread or loses messages already buffered in the queue.
    if st.session_state.log_queue is None:
        st.session_state.log_queue = queue.Queue()
    if st.session_state.result_holder is None:
        st.session_state.result_holder = {}

    log_queue     = st.session_state.log_queue
    result_holder = st.session_state.result_holder

    # Only start the scraper thread once per run — guard against re-render
    # spawning a second concurrent Playwright session while is_running=True.
    if st.session_state.scrape_thread is None or not st.session_state.scrape_thread.is_alive():

        # Reset LLM rate-limit flags so providers blocked in a previous run are retried
        reset_rate_limit_flags()

        def _scraper_thread(search_config, profile):
            jobs, summary = run_scrape_session(
                search_config,
                db,
                log_queue,
                ttl_hours,
                profile=profile,
            )
            result_holder["jobs"]    = jobs
            result_holder["summary"] = summary

        st.session_state.scrape_thread = threading.Thread(
            target=_scraper_thread,
            args=(st.session_state.search_config, st.session_state.dynamic_profile),
            daemon=True,
        )
        st.session_state.scrape_thread.start()

        # Stage 1 — notify Discord that the run has started (fires exactly once per run).
        # Dispatched in a daemon thread so the blocking HTTP call never stalls the UI.
        if not st.session_state.get("start_webhook_sent", False):
            threading.Thread(
                target=send_discord_alert,
                args=(random.choice(START_PHRASES),),
                daemon=True,
            ).start()
            st.session_state["start_webhook_sent"] = True

    thread = st.session_state.scrape_thread

    while thread.is_alive():
        drained = False
        while True:
            try:
                msg = log_queue.get_nowait()
                _add_log(msg)
                drained = True
            except queue.Empty:
                break
        if drained:
            log_placeholder.code("\n".join(st.session_state.logs[-40:]), language="")
        time.sleep(0.5)

    # Final drain — capture any messages written between last check and thread exit
    while True:
        try:
            msg = log_queue.get_nowait()
            _add_log(msg)
        except queue.Empty:
            break
    log_placeholder.code("\n".join(st.session_state.logs[-40:]), language="")

    # Scraper complete — run matcher
    raw_jobs = result_holder.get("jobs", [])
    summary  = result_holder.get("summary", ScrapeRunSummary())

    _add_log(f"[MATCHER] Scoring {len(raw_jobs)} new jobs...")
    tier1, tier2, tier3, tier4 = bucket_jobs(
        raw_jobs,
        st.session_state.search_config,
        db,
        profile=st.session_state.dynamic_profile,
    )

    summary.tier1_count = len(tier1)
    summary.tier2_count = len(tier2)
    summary.tier3_count = len(tier3)

    st.session_state.scrape_results = (tier1, tier2, tier3, tier4)
    st.session_state.summary = summary
    st.session_state.is_running = False

    _add_log(
        f"[GEMA] Pipeline complete. "
        f"T1={len(tier1)} | T2={len(tier2)} | T3={len(tier3)}"
    )

    # Stage 2 + 3 — Discord notifications fired in daemon threads so the 5-second
    # urllib timeout never blocks the Streamlit render thread or delays st.rerun().
    _discord_report = (
        f"📊 Extraction Report:\n"
        f"- Tier 1: {len(tier1)}\n"
        f"- Tier 2: {len(tier2)}\n"
        f"- Tier 3: {len(tier3)}\n"
        f"- Tier 4: {len(tier4)}"
    )
    # AUDIT #1 fail-loud: errors collected honestly by the scrape (dropped DB
    # batches, aborted domains, task exceptions) must reach the human, not die in
    # summary.errors where nobody reads them.
    if summary.errors:
        _discord_report += f"\n\n⚠️ {len(summary.errors)} error(es) durante el run:"
        for err in summary.errors[:5]:      # cap — Discord message length
            _discord_report += f"\n  • {err}"
        if len(summary.errors) > 5:
            _discord_report += f"\n  • … y {len(summary.errors) - 5} más"
    threading.Thread(target=send_discord_alert, args=(_discord_report,), daemon=True).start()
    threading.Thread(target=send_discord_alert, args=(random.choice(END_PHRASES),), daemon=True).start()

    # Push to integrations — AUDIT #3: each integration isolated. A delivery-layer
    # failure (Sheets 429, revoked token) must not crash the pipeline after a
    # successful scrape, and must notify which stage failed (results are in the DB).
    all_results = tier1 + tier2 + tier3 + tier4
    for _name, _enabled, _push in (
        ("NOTION", notion.is_enabled, lambda: notion.push_batch(all_results)),
        ("SHEETS", sheets.is_enabled, lambda: sheets.append_batch(all_results, summary)),
    ):
        if all_results and _enabled:
            _add_log(f"[{_name}] Pushing results...")
            try:
                _push()
            except Exception as _exc:
                _add_log(f"[{_name}] Push FAILED (scrape OK, results in DB): {_exc}")
                threading.Thread(
                    target=send_discord_alert,
                    args=(f"🟠 {_name} push falló (scrape OK, resultados en DB): {_exc}",),
                    daemon=True,
                ).start()

    st.rerun()

# =============================================================================
# Phase 4: Results Display
# =============================================================================

if st.session_state.scrape_results:
    tier1, tier2, tier3, tier4 = st.session_state.scrape_results
    summary = st.session_state.summary

    st.divider()
    st.markdown(panel_head_html("Resultados", "Fase 4"), unsafe_allow_html=True)

    # Summary — stat tiles Pulse (mismos datos que los st.metric que reemplazan)
    st.markdown(
        stat_tiles_html(
            tier1=len(tier1),
            tier2=len(tier2),
            tier3=len(tier3),
            tier4=len(tier4),
            duration_seconds=(
                summary.duration_seconds
                if summary and summary.duration_seconds else None
            ),
        ),
        unsafe_allow_html=True,
    )

    # AUDIT #1 fail-loud: surface run errors in the UI, not just in a summary
    # field nobody reads. An empty scrape "looks fine" without this.
    if summary and summary.errors:
        st.error(f"⚠️ {len(summary.errors)} error(es) durante el run — resultados pueden estar incompletos")
        with st.expander("Ver errores del run", expanded=True):
            for _err in summary.errors:
                st.code(_err, language="")

    def _render_tier(tiered_jobs, label, tier_class, show_score=True):
        # Presentación pura: misma data que las filas st.columns que reemplaza,
        # ahora como job cards HTML (ui_theme.job_card_html). tier_class colorea
        # la barra de score (t1 teal / t2 ámbar / t3 rojo).
        if not tiered_jobs:
            return
        with st.expander(f"{label} ({len(tiered_jobs)} jobs)", expanded=True):
            for tj in tiered_jobs:
                meta = tj.job.source_domain + (
                    f" · {tj.job.location_raw}" if tj.job.location_raw else ""
                )
                st.markdown(
                    job_card_html(
                        title=tj.job.title,
                        url=tj.job.url,
                        company=tj.job.company,
                        salary=tj.job.salary_raw,
                        meta=meta,
                        score=tj.match_score,
                        tier_class=tier_class,
                        show_score=show_score,
                    ),
                    unsafe_allow_html=True,
                )
                if tj.match_reasons or tj.miss_reasons:
                    with st.expander("Details", expanded=False):
                        if tj.match_reasons:
                            st.success("✓ " + " | ".join(tj.match_reasons))
                        if tj.miss_reasons:
                            st.warning("✗ " + " | ".join(tj.miss_reasons))

    _render_tier(tier1, "Tier 1 — High Priority",          "t1")
    _render_tier(tier2, "Tier 2 — Explore",                "t2")
    _render_tier(tier3, "Tier 3 — Recycle Bin",            "t3")
    _render_tier(tier4, "Tier 4 — Manual Review (Salary)", "t4", show_score=False)

    # ── Excel Export ──────────────────────────────────────────────────────────
    all_tiered = (
        [(tj, 1) for tj in tier1] +
        [(tj, 2) for tj in tier2] +
        [(tj, 3) for tj in tier3] +
        [(tj, 4) for tj in tier4]
    )
    if all_tiered:
        st.divider()
        rows = []
        for tj, tier_num in sorted(all_tiered, key=lambda x: x[0].match_score, reverse=True):
            rows.append({
                "Tier":         tier_num,
                "Title":        tj.job.title,
                "Company":      tj.job.company,
                "Salary":       tj.job.salary_raw or "",
                "Match Score":  tj.match_score if tj.match_score >= 0 else "Manual",
                "Apply Link":   tj.job.url,
                "Source":       tj.job.source_domain,
                "Date Fetched": tj.job.scraped_at.strftime("%Y-%m-%d %H:%M UTC"),
            })
        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        run_id_short = st.session_state.get("run_id", "gema")[:8]
        st.download_button(
            label="Download Results (Excel)",
            data=buf,
            file_name=f"gema_results_{run_id_short}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

# =============================================================================
# Live Log Console (always visible at bottom)
# =============================================================================

if st.session_state.logs:
    st.divider()
    st.markdown(panel_head_html("Live Engine Log"), unsafe_allow_html=True)
    st.code("\n".join(st.session_state.logs[-60:]), language="")
