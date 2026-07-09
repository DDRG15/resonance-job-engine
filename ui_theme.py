"""
ui_theme.py — Capa visual "Pulse" de GEMA (B5).

Presentación pura: este módulo solo produce CSS y fragmentos HTML.
No toca engine, matcher, scraper ni scheduler — si un cambio acá rompe
un test de lógica, el cambio está mal.

Uso:
    from ui_theme import inject_theme, header_html, stat_tiles_html, job_card_html
    inject_theme()                      # una vez, tras st.set_page_config()
    st.markdown(header_html(...), unsafe_allow_html=True)

Todos los datos dinámicos que entran a los builders se escapan con
html.escape() — títulos y empresas vienen de páginas scrapeadas y son
input hostil hasta que se demuestre lo contrario.
"""

import getpass
import html
import os
from datetime import datetime

import streamlit as st

# Nombres de cuenta que no son una persona — con estos el saludo cae al genérico.
_GENERIC_USERS = {"root", "user", "admin", "administrator", "app", "docker", "gema"}

# ── Tokens + CSS global (dirección A "Pulse": dark azulado + teal + cards) ────
_THEME_CSS = """
<style>
:root {
  --gema-bg:        #0B0F14;
  --gema-surface:   #111823;
  --gema-surface2:  #0E141C;
  --gema-border:    #1D2836;
  --gema-accent:    #2DD4BF;
  --gema-accent-08: rgba(45, 212, 191, .08);
  --gema-accent-16: rgba(45, 212, 191, .16);
  --gema-text:      #E6EDF3;
  --gema-muted:     #8A97A6;
  --gema-faint:     #5A6675;
  --gema-t1: #0D9488; --gema-t1-ink: #5EEAD4;
  --gema-t2: #D97706; --gema-t2-ink: #FCD34D;
  --gema-t3: #DC2626; --gema-t3-ink: #FCA5A5;
  --gema-t4: #7C3AED; --gema-t4-ink: #C4B5FD;
  --gema-mono: "Cascadia Code", Consolas, ui-monospace, monospace;
}

/* ── Chrome de Streamlit — ocultar lo que no es GEMA ── */
[data-testid="stHeader"] { background: rgba(11, 15, 20, .85); }
#MainMenu, [data-testid="stMainMenu"], footer,
[data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stAppDeployButton"], [data-testid="stStatusWidget"] {
  display: none !important;
}

/* ── Densidad: menos aire muerto que el default de Streamlit ── */
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1160px; }
[data-testid="stVerticalBlock"] { gap: .65rem; }
hr { margin: 1.1rem 0 !important; }

/* ── st.container(border=True) como panel Pulse ── */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--gema-surface);
  border: 1px solid var(--gema-border) !important;
  border-radius: 14px;
  padding: 10px 14px;
}
[data-testid="stSidebar"] {
  background: var(--gema-surface2);
  border-right: 1px solid var(--gema-border);
}
hr { border-color: var(--gema-border) !important; }
h1, h2, h3 { letter-spacing: -.01em; }

/* Subheaders del sidebar como micro-labels (Pulse side-label) */
[data-testid="stSidebar"] h3 {
  font-size: .72rem !important;
  font-weight: 600;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--gema-faint) !important;
}

/* ── Botones ── */
.stButton > button, [data-testid="stDownloadButton"] > button {
  border-radius: 9px;
  border: 1px solid var(--gema-border);
  background: var(--gema-surface);
  color: var(--gema-muted);
  font-weight: 550;
}
.stButton > button:hover, [data-testid="stDownloadButton"] > button:hover {
  border-color: var(--gema-faint);
  color: var(--gema-text);
}
.stButton > button[kind="primary"] {
  background: var(--gema-accent);
  color: #04211C;
  border: none;
  font-weight: 650;
  box-shadow: 0 0 22px rgba(45, 212, 191, .22);
}
.stButton > button[kind="primary"]:hover {
  filter: brightness(1.08);
  color: #04211C;
}

/* ── Inputs ── */
.stTextInput [data-baseweb="input"], .stTextArea [data-baseweb="textarea"],
.stTextInput [data-baseweb="base-input"], .stSelectbox [data-baseweb="select"] > div {
  background: var(--gema-bg);
  border-color: var(--gema-border);
  border-radius: 10px;
}
.stTextArea textarea, .stTextInput input { color: var(--gema-text); }

/* ── Consola / bloques de código ── */
[data-testid="stCode"] pre, .stCode pre, pre {
  background: #070B10 !important;
  border: 1px solid var(--gema-border);
  border-radius: 10px;
}
code { font-family: var(--gema-mono) !important; }

/* ── Expanders como cards ── */
[data-testid="stExpander"] details {
  border: 1px solid var(--gema-border);
  border-radius: 12px;
  background: var(--gema-surface);
}
[data-testid="stExpander"] summary:hover { color: var(--gema-accent); }

/* ── st.metric (registry del sidebar) como mini-tiles ── */
[data-testid="stMetric"] {
  background: var(--gema-surface);
  border: 1px solid var(--gema-border);
  border-radius: 10px;
  padding: 10px 14px;
}
[data-testid="stMetricValue"] {
  font-family: var(--gema-mono);
  font-variant-numeric: tabular-nums;
}

/* ── Multiselect (días del cron) como pills teal ── */
.stMultiSelect [data-baseweb="tag"] {
  background: var(--gema-accent-16);
  border-radius: 7px;
}
.stMultiSelect [data-baseweb="tag"] span { color: var(--gema-accent); }

/* ── File uploader (CV) integrado al tema ── */
[data-testid="stFileUploaderDropzone"] {
  background: var(--gema-surface);
  border: 1px dashed var(--gema-border);
  border-radius: 10px;
}
[data-testid="stFileUploaderDropzone"] button { border-radius: 8px; }

/* ── Alerts: tinte oscuro por severidad (adiós bloques chillones) ── */
[data-testid="stAlert"] { border-radius: 10px; }
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]) {
  background: rgba(217, 119, 6, .08);
  border: 1px solid rgba(217, 119, 6, .4);
}
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]) {
  background: var(--gema-accent-08);
  border: 1px solid var(--gema-accent-16);
}
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]) {
  background: rgba(220, 38, 38, .08);
  border: 1px solid rgba(220, 38, 38, .4);
}
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]) {
  background: var(--gema-surface);
  border: 1px solid var(--gema-border);
}

/* ── Panel head: título + eyebrow de fase ── */
.gema-panel-head {
  display: flex; justify-content: space-between; align-items: baseline;
  margin: 2px 0 0;
}
.gema-panel-head h3 {
  margin: 0; font-size: 15.5px; font-weight: 600; color: var(--gema-text);
}
.gema-panel-head .ph {
  font-family: var(--gema-mono); font-size: 10.5px; color: var(--gema-accent);
  letter-spacing: .12em; text-transform: uppercase;
}

/* ══ Componentes propios (header, tiles, job cards) ══ */
.gema-topbar {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 24px; flex-wrap: wrap; margin-bottom: 6px;
}
.gema-greet h2 {
  margin: 0; font-size: 26px; font-weight: 650; letter-spacing: -.01em;
  color: var(--gema-text);
}
.gema-greet h2 em { font-style: normal; color: var(--gema-accent); }
.gema-greet p { margin: 4px 0 0; color: var(--gema-muted); font-size: 13px; }
.gema-live-card {
  background: linear-gradient(160deg, var(--gema-surface), #0D1A1E);
  border: 1px solid var(--gema-border); border-radius: 14px;
  padding: 14px 18px; min-width: 260px;
}
.gema-live-card .lbl {
  display: flex; justify-content: space-between; align-items: center; gap: 14px;
  font-size: 10.5px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--gema-faint);
}
.gema-live-card .big { font-family: var(--gema-mono); font-size: 14px; margin-top: 8px; color: var(--gema-text); }
.gema-live-card .sub { font-size: 11.5px; color: var(--gema-muted); margin-top: 2px; }
.gema-pill-ok {
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 11px; color: var(--gema-accent); background: var(--gema-accent-08);
  border: 1px solid var(--gema-accent-16); border-radius: 99px; padding: 2px 9px;
  white-space: nowrap;
}
.gema-pill-warn {
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 11px; color: var(--gema-t2-ink); background: rgba(217, 119, 6, .1);
  border: 1px solid rgba(217, 119, 6, .4); border-radius: 99px; padding: 2px 9px;
  white-space: nowrap;
}
.gema-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: currentColor; box-shadow: 0 0 8px currentColor;
}

.gema-brand { display: flex; align-items: center; gap: 10px; padding: 2px 0 6px; }
.gema-brand .gem {
  width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--gema-accent), #0E7490);
  box-shadow: 0 0 18px rgba(45, 212, 191, .25);
  display: grid; place-items: center; color: #04211C;
  font-size: 13px; font-weight: 700;
}
.gema-brand b { font-size: 16px; font-weight: 650; color: var(--gema-text); }
.gema-brand small { display: block; color: var(--gema-muted); font-size: 11px; }

.gema-tiles {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
  margin: 6px 0 10px;
}
.gema-tile {
  background: var(--gema-surface); border: 1px solid var(--gema-border);
  border-radius: 13px; padding: 14px 16px 12px;
}
.gema-tile .k {
  font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--gema-muted); display: flex; align-items: center; gap: 7px;
}
.gema-tile .sw { width: 8px; height: 8px; border-radius: 3px; flex-shrink: 0; }
.gema-tile .n {
  font-family: var(--gema-mono); font-size: 28px; font-weight: 600;
  margin-top: 4px; color: var(--gema-text); font-variant-numeric: tabular-nums;
}
.gema-tile.t1 .n { color: var(--gema-t1-ink); }
.gema-tile .d { font-size: 11px; color: var(--gema-faint); }

.gema-job {
  display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 1.4fr) 120px 140px;
  gap: 14px; align-items: center;
  background: var(--gema-surface); border: 1px solid var(--gema-border);
  border-radius: 12px; padding: 12px 18px; margin-bottom: 8px;
}
.gema-job:hover { border-color: rgba(45, 212, 191, .35); }
.gema-job .t a {
  color: var(--gema-text); font-weight: 550; font-size: 13.5px; text-decoration: none;
}
.gema-job .t a:hover { color: var(--gema-accent); }
.gema-job .t small { display: block; color: var(--gema-faint); font-size: 11px; margin-top: 2px; }
.gema-job .c { color: var(--gema-muted); font-size: 12.5px; overflow-wrap: anywhere; }
.gema-job .s { font-family: var(--gema-mono); font-size: 12px; color: var(--gema-text); }
.gema-score { display: flex; align-items: center; gap: 9px; }
.gema-score .bar {
  flex: 1; height: 5px; border-radius: 4px; background: var(--gema-border);
  overflow: hidden;
}
.gema-score .bar i { display: block; height: 100%; border-radius: 4px; }
.gema-score b {
  font-family: var(--gema-mono); font-size: 12px; font-weight: 600;
  min-width: 34px; text-align: right;
}
.gema-score.t1 .bar i { background: var(--gema-t1); } .gema-score.t1 b { color: var(--gema-t1-ink); }
.gema-score.t2 .bar i { background: var(--gema-t2); } .gema-score.t2 b { color: var(--gema-t2-ink); }
.gema-score.t3 .bar i { background: var(--gema-t3); } .gema-score.t3 b { color: var(--gema-t3-ink); }
.gema-manual {
  font-family: var(--gema-mono); font-size: 11px; letter-spacing: .1em;
  color: var(--gema-t4-ink); text-align: right;
}

@media (max-width: 1100px) {
  .gema-tiles { grid-template-columns: repeat(2, 1fr); }
  .gema-job { grid-template-columns: 1fr; gap: 6px; }
}
</style>
"""


def inject_theme() -> None:
    """Inyecta el CSS global Pulse. Llamar una vez por render, tras set_page_config."""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


def _display_name() -> str | None:
    """Nombre para el saludo: GEMA_USER_NAME manda; si no, el usuario del SO.

    Cuentas genéricas (root, docker, …) no son una persona → None y el
    header cae a un saludo sin nombre. Nunca lanza: el saludo jamás puede
    tumbar la UI.
    """
    name = os.environ.get("GEMA_USER_NAME", "").strip()
    if not name:
        try:
            name = getpass.getuser().strip()
        except Exception:
            return None
    if not name or name.lower() in _GENERIC_USERS:
        return None
    return name.split()[0].capitalize()


def _greeting_word(hour: int) -> str:
    if 6 <= hour < 12:
        return "Buenos días"
    if 12 <= hour < 19:
        return "Buenas tardes"
    return "Buenas noches"


def header_html(
    boards: int,
    providers: int,
    stale_count: int,
    nominal: bool,
    sched_line: str,
) -> str:
    """Header Pulse: saludo por hora local + card de Live Status con datos reales."""
    name = _display_name()
    word = _greeting_word(datetime.now().hour)
    greet = f"{word}, <em>{html.escape(name)}</em>" if name else f"{word}. <em>GEMA</em> en línea"
    status_pill = (
        '<span class="gema-pill-ok"><span class="gema-dot"></span>Nominal</span>'
        if nominal else
        '<span class="gema-pill-warn"><span class="gema-dot"></span>Atención</span>'
    )
    return (
        '<div class="gema-topbar">'
        f'<div class="gema-greet"><h2>{greet}</h2>'
        '<p>Pipeline de descubrimiento de trabajos — extracción CSS a 0 tokens, LLM solo como fallback.</p></div>'
        '<div class="gema-live-card">'
        f'<div class="lbl">Live status {status_pill}</div>'
        f'<div class="big">{boards} boards · {providers} LLM en cadena</div>'
        f'<div class="sub">{stale_count} selectores stale · {html.escape(sched_line)}</div>'
        '</div></div>'
    )


def panel_head_html(title: str, phase: str = "") -> str:
    """Cabecera de panel Pulse: título compacto + eyebrow de fase a la derecha."""
    ph = f'<span class="ph">{html.escape(phase)}</span>' if phase else ''
    return (
        f'<div class="gema-panel-head"><h3>{html.escape(title)}</h3>{ph}</div>'
    )


def brand_html() -> str:
    """Marca del sidebar: gema + nombre. Reemplaza al st.title del Control Panel."""
    return (
        '<div class="gema-brand"><span class="gem">◆</span>'
        '<span><b>GEMA</b><small>Control Panel</small></span></div>'
    )


def stat_tiles_html(
    tier1: int,
    tier2: int,
    tier3: int,
    tier4: int,
    duration_seconds: float | None,
) -> str:
    """Fila de stat tiles de resultados (reemplaza los st.metric de Phase 4)."""
    tiles = [
        ('t1', 'var(--gema-t1)', 'Tier 1', str(tier1), '80–100 % — aplicar ya'),
        ('',   'var(--gema-t2)', 'Tier 2', str(tier2), '50–79 % — revisar'),
        ('',   'var(--gema-t3)', 'Tier 3', str(tier3), '&lt;50 % — reciclaje'),
        ('',   'var(--gema-t4)', 'Tier 4', str(tier4), 'salario en texto — manual'),
    ]
    if duration_seconds is not None:
        tiles.append(('', '', 'Duración', f'{duration_seconds:.0f}s', 'corrida completa'))
    out = ['<div class="gema-tiles">']
    for cls, sw, label, value, desc in tiles:
        swatch = f'<span class="sw" style="background:{sw}"></span>' if sw else ''
        out.append(
            f'<div class="gema-tile {cls}"><div class="k">{swatch}{label}</div>'
            f'<div class="n">{value}</div><div class="d">{desc}</div></div>'
        )
    out.append('</div>')
    return ''.join(out)


def job_card_html(
    title: str,
    url: str,
    company: str,
    salary: str | None,
    meta: str,
    score: int,
    tier_class: str,
    show_score: bool = True,
) -> str:
    """Card HTML de un job (reemplaza la fila st.columns de _render_tier).

    tier_class: "t1" | "t2" | "t3" — colorea la barra de score.
    show_score=False (Tier 4) → celda MANUAL en violeta.
    """
    title_e   = html.escape(title or "—")
    url_e     = html.escape(url or "#", quote=True)
    company_e = html.escape(company or "—")
    salary_e  = html.escape(salary or "—")
    meta_e    = html.escape(meta or "")
    if show_score and score >= 0:
        pct = max(0, min(100, score))
        score_cell = (
            f'<div class="gema-score {tier_class}"><div class="bar">'
            f'<i style="width:{pct}%"></i></div><b>{pct}%</b></div>'
        )
    else:
        score_cell = '<div class="gema-manual">MANUAL</div>'
    return (
        '<div class="gema-job">'
        f'<div class="t"><a href="{url_e}" target="_blank" rel="noopener">{title_e}</a>'
        f'<small>{meta_e}</small></div>'
        f'<div class="c">{company_e}</div>'
        f'<div class="s">{salary_e}</div>'
        f'{score_cell}'
        '</div>'
    )
