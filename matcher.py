"""
matcher.py — Match Scoring & Tier Bucketing Engine for Project GEMA.

Scores each JobResult against the SearchConfig and assigns Tier 1/2/3/4.
No external API calls — pure Python logic, runs in milliseconds.

Scoring weights (mathematical path — Tier 1/2/3 only):
    Title match (fuzzy)  : 50 points max
    Salary meets minimum : 25 points
    Must-include present : 15 points max
    Must-exclude absent  : 10 points (penalty applied if found)

Tier 4 (Manual Review) BYPASSES mathematical scoring entirely.
    Triggered when: salary_raw is present but parse_salary_usd() returns None.
    Example inputs: 'Competitive', 'Competitive + equity + 401k', 'DOE'
    These are legitimate salary disclosures in text form — penalizing them
    with a 0-point salary score would bury potentially excellent jobs in Tier 3.
"""

import logging
import re
from typing import Optional

import config
import metrics
from models import JobResult, SearchConfig, TieredJob

logger = logging.getLogger(__name__)


# =============================================================================
# AUDIT NEW #1 (2026-07-10) — pre-filtro de relevancia de título
# =============================================================================
# El SRP genérico de un board devuelve TODOS sus cards; GEMA scrapeaba y
# puntuaba File Clerk / Crypto Trader / Virtual Assistant junto con los
# relevantes. No eran alucinaciones (URLs reales) pero eran ruido: se
# colaban a Tier 3 y ensuciaban los resultados (queja de el usuario).
# Fix conservador: un job se descarta SOLO si su título no comparte NINGÚN
# token significativo con NINGÚN target_title NI skill del perfil. Un job
# borderline (comparte aunque sea 1 token real) SIEMPRE se conserva.

# Palabras que no discriminan un rol técnico — no cuentan como overlap.
_STOPWORDS = frozenset({
    "senior", "junior", "mid", "lead", "staff", "principal", "the", "a", "an",
    "of", "and", "or", "for", "to", "in", "at", "remote", "hybrid", "onsite",
    "on-site", "full", "time", "part", "contract", "permanent", "i", "ii",
    "iii", "level", "entry", "experienced", "work", "home", "anywhere",
})


def _meaningful_tokens(text: str) -> set:
    """Tokens en minúscula, sin stopwords ni tokens de 1 char."""
    toks = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) > 1 and t not in _STOPWORDS}


def title_is_relevant(
    title: str,
    target_titles: list[str],
    profile: Optional[dict] = None,
) -> bool:
    """True si el título comparte ≥1 token significativo con algún
    target_title o skill del perfil. Conservador: sin señales de búsqueda
    (target vacío) → todo es relevante (no filtrar a ciegas)."""
    job_tokens = _meaningful_tokens(title)
    if not job_tokens:
        return True  # título raro/vacío — no descartar por las dudas
    search_tokens: set = set()
    for t in (target_titles or []):
        search_tokens |= _meaningful_tokens(t)
    if profile:
        for s in profile.get("core_skills", []) or []:
            search_tokens |= _meaningful_tokens(str(s))
    if not search_tokens:
        return True  # sin criterio de búsqueda → no filtrar
    return bool(job_tokens & search_tokens)


# =============================================================================
# Scoring Components
# =============================================================================

def _score_title(job_title: str, target_titles: list[str]) -> tuple[int, list[str], list[str]]:
    """
    Fuzzy title match. Returns (score, match_reasons, miss_reasons).
    Max 50 points.

    Strategy:
        Exact match (case-insensitive)  → 50 pts
        All words present               → 35 pts
        Any word present                → 15 pts
        No match                        → 0 pts
    """
    title_lower = (job_title or "").lower()
    best_score = 0
    mejor_target = ""
    match_reasons = []
    miss_reasons = []

    tokens_titulo = _meaningful_tokens(title_lower)

    for target in target_titles:
        target_lower = target.lower()
        tokens_target = _meaningful_tokens(target_lower)

        if target_lower == title_lower:
            puntaje = 50.0
        elif not tokens_target:
            continue
        else:
            # ── Calibración 2026-09-17: curva continua, no cinco escalones ────
            #
            # La escala tenía cinco niveles (0/15/35/40/50) y con la
            # renormalización de la Fase 4 quedaban 0/30/70/80/100. Como el
            # título es el único eje que casi siempre participa, un calce exacto
            # bastaba para llegar a 100: la corrida del 17-09 dio **274 Tier 1
            # de 1095 avisos (25%)**, que para una lista de prioridades es
            # demasiado.
            #
            # Dos factores, y el segundo es el que faltaba:
            #
            #   cobertura  cuánto del target está en el título
            #   precision  cuánto del título es el target
            #
            # Sin `precision`, "Senior Staff Backend Engineer, Payments Platform
            # (EMEA)" puntuaba igual que "Backend Engineer" contra el target
            # "backend engineer" — los dos lo contienen. Pero el primero es un
            # puesto distinto con el target adentro, y tratarlos igual es lo que
            # llenaba el Tier 1.
            # Tolerante a la raíz: `engineer` cuenta como cubierto por `engineering`.
            # La versión de escalones comparaba subcadenas, así que "Engineering Lead"
            # matcheaba parcialmente con "Backend Engineer"; pasar a tokens exactos
            # perdía eso en silencio. El piso de 5 caracteres evita que `dev` matchee
            # `development`, `device` y `devops` a la vez.
            cubiertos = {
                t for t in tokens_target
                if t in tokens_titulo
                or any(len(t) >= 5 and (u.startswith(t) or t.startswith(u))
                       for u in tokens_titulo if len(u) >= 5)
            }
            if not cubiertos:
                continue
            cobertura = len(cubiertos) / len(tokens_target)

            # `precision` se mide contra TODOS los tokens del título, no contra los
            # "significativos". Ese fue el error del primer intento de esta
            # calibración: `_meaningful_tokens` descarta senior/staff/principal como
            # stopwords, así que "Principal Platform Engineer" y "Platform Engineer"
            # quedaban idénticos y la precisión no penalizaba nada. Medido: Tier 1
            # subió de 274 a 338 y 640 avisos se apilaron en un solo decil.
            todos_titulo = {t for t in re.split(r"[^a-z0-9]+", title_lower) if len(t) > 1}
            precision = len(cubiertos & todos_titulo) / max(1, len(todos_titulo))

            # La cobertura pesa más: que falte medio target es peor que que sobren
            # palabras. Pero la precisión no es cero — es lo que separa un título que
            # ES el puesto de uno que lo menciona de paso.
            puntaje = 50.0 * (0.70 * cobertura + 0.30 * precision)

        if puntaje > best_score:
            best_score = puntaje
            mejor_target = target

    if best_score <= 0:
        miss_reasons.append(f"Title '{job_title}' did not match any target")
    else:
        match_reasons.append(
            f"Title match {best_score / 50 * 100:.0f}% vs '{mejor_target}'")

    return int(round(min(best_score, 50))), match_reasons, miss_reasons


def _score_salary(
    job: JobResult,
    min_salary: Optional[int],
) -> tuple[int, list[str], list[str]]:
    """
    Salary gate. Returns (score, match_reasons, miss_reasons).
    Max 25 points.

    If salary is not published → 12 pts (Tier 2 weight, not penalized fully).
    Vol 1.3: 'no salary published' → requires secondary review.
    """
    if min_salary is None:
        return 25, ["No salary filter specified"], []

    parsed = job.parse_salary_usd()

    if parsed is None:
        return 12, [], ["Salary not published — manual review required"]

    if parsed >= min_salary:
        return 25, [f"Salary ${parsed:,} meets minimum ${min_salary:,}"], []

    return 0, [], [f"Salary ${parsed:,} below minimum ${min_salary:,}"]


def _score_must_include(
    job: JobResult,
    must_include: list[str],
) -> tuple[int, list[str], list[str]]:
    """
    Must-include keyword check across title + salary_raw + company.
    Max 15 points (proportional if multiple keywords).
    """
    if not must_include:
        return 15, ["No must-include keywords specified"], []

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or "", job.source_domain
    ])).lower()

    matched = [kw for kw in must_include if kw.lower() in searchable]
    missing = [kw for kw in must_include if kw.lower() not in searchable]

    score = int(15 * len(matched) / len(must_include))
    match_reasons = [f"Keyword present: '{kw}'" for kw in matched]
    miss_reasons = [f"Required keyword missing: '{kw}'" for kw in missing]

    return score, match_reasons, miss_reasons


def _score_skill_overlap(
    job: JobResult,
    profile: Optional[dict],
) -> tuple[int, list[str], list[str]]:
    """
    Profile skill-overlap bonus. Max 15 points (additive on top of the base 100).

    Searches job title + company + salary_raw for any string in the union of
    profile['core_skills'] and profile['audit_signals']. Score is proportional
    to the fraction of signals that hit, capped at 15.

    Returns (0, [...], []) when profile is None or contains no signals — never
    raises, never penalises a job for a missing profile.
    """
    if not profile:
        return 0, ["No profile loaded — skill overlap skipped"], []

    core_skills   = profile.get("core_skills",   [])
    audit_signals = profile.get("audit_signals", [])
    all_signals   = core_skills + audit_signals

    if not all_signals:
        return 0, ["Profile has no skills or signals — overlap skipped"], []

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or ""
    ])).lower()

    hits   = [s for s in all_signals if s.lower() in searchable]
    misses = [s for s in all_signals if s.lower() not in searchable]

    score = min(15, int(15 * len(hits) / len(all_signals)))
    match_reasons = [f"Skill/signal match: '{s}'" for s in hits]
    miss_reasons  = [f"Skill/signal absent: '{s}'" for s in misses[:3]]  # cap noise

    return score, match_reasons, miss_reasons


# ── Location policy for el usuario (la ubicacion del candidato — TUNABLE ─────────────────────────
# Accessible (no penalty): global/anywhere + LATAM — same region/timezone, no
# work-authorization barrier. Soft-restricted (-10): a named foreign region that
# requires work authorization he does not hold. -10 is gentle by design — it
# deprioritises without excluding (90→80 stays Tier 1; a borderline 80→70 drops
# to Tier 2). Edit these two sets to retune how aggressively location nudges a job.
#
# Word boundaries (\b) are mandatory: a bare substring match flags "uk" inside
# "ukraine" and "us" inside "belarus". The regex anchors whole tokens only.
_LOCATION_ACCESSIBLE = re.compile(
    r"\b("
    r"anywhere|worldwide|global|international|latam|latin america|"
    r"south america|americas|"
    r"peru|brazil|brasil|argentina|chile|colombia|mexico|uruguay|ecuador|"
    r"bolivia|paraguay|venezuela|costa rica|panama|guatemala"
    r")\b",
    re.IGNORECASE,
)
_LOCATION_SOFT_RESTRICTED = re.compile(
    r"\b("
    r"usa|us|united states|uk|united kingdom|england|canada|"
    r"europe|emea|eu|european union|north america|australia|new zealand|"
    r"germany|france|spain|portugal|poland|serbia|cyprus|netherlands|"
    r"ireland|italy|switzerland|sweden|belgium|austria|denmark|norway|finland"
    r")\b",
    re.IGNORECASE,
)


def _score_location(job: JobResult) -> tuple[int, list[str], list[str]]:
    """
    Location accessibility filter for el usuario in la ubicacion del candidato.

    Hard block  (-50): Security clearance / citizenship required → forces Tier 3.
    Restricted  (-20): Explicit region-lock phrase (US only, EU only) anywhere
                       in the listing text.
    Open        (+10): Worldwide / anywhere / all time zones phrase.
    Soft geo    (-10): location_raw names a foreign region requiring work
                       authorization el usuario lacks (US/UK/EU country/Canada/AU),
                       and does not also signal openness or a LATAM location.
    Neutral     (  0): No location signal, or an unrecognised/LATAM location.

    The soft-geo tier is the fix for AUDIT #2: the explicit RESTRICTED phrases
    ("must be authorized to work in the US") live in job descriptions, not in the
    card-level location field GEMA scrapes. Boards expose location_raw as a bare
    country/region ("USA", "Portugal", "Anywhere"). Without the soft-geo tier the
    penalty branches never fired on the only data the scraper actually has, so the
    filter gave false assurance — every US/EU-locked Tier 1 scored as if open.
    """
    HARD_BLOCK = [
        "security clearance", "top secret", "secret clearance", "ts/sci",
        "us citizenship required", "must be a us citizen", "must be a citizen",
        "usc only", "u.s. citizen", "government contractor", "dod clearance",
        "public trust clearance", "federal contractor",
    ]
    RESTRICTED = [
        "must be located in the us", "must reside in the us",
        "must be authorized to work in the us",
        "authorized to work in the united states",
        "work authorization required", "right to work in the us",
        "must be based in the uk", "eu only", "europe only",
        "must be in the us", "us only", "usa only",
        "must work from the us", "must be in united states",
    ]
    OPEN = [
        "worldwide", "anywhere", "all time zones", "any time zone",
        "global remote", "home based - worldwide", "fully remote - global",
        "open to all locations", "no location restriction",
    ]

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or "",
        getattr(job, "location_raw", None) or "",
    ])).lower()

    for signal in HARD_BLOCK:
        if signal in searchable:
            return -50, [], [f"Hard block: '{signal}' detected — requires clearance/citizenship"]
    # NOTA (Fase 3): esta funcion sigue existiendo porque `_location_verdict` la usa
    # como detector. Lo que cambio es QUIEN consume su salida: el delta ya no entra
    # al puntaje, se traduce a veredicto. Ver `_location_verdict`.

    # RESTRICTED before OPEN (AUDIT #4): a binding region-lock must dominate an
    # aspirational "worldwide team" phrase in the same listing. For a la ciudad del candidato dev,
    # "Worldwide team, but you must be authorized to work in the US" is a US lock,
    # not an open role.
    for signal in RESTRICTED:
        if signal in searchable:
            return -20, [], [f"Location restricted: '{signal}' detected"]

    for signal in OPEN:
        if signal in searchable:
            return 10, [f"Location open: '{signal}' found"], []

    # Soft geo — evaluated on location_raw only (not the blob), so a company name
    # or salary string containing "us"/"eu" can never trigger it. Fires only when
    # the field names a restricted region AND carries no LATAM/open signal.
    loc = (getattr(job, "location_raw", None) or "").lower().strip()
    if loc and not _LOCATION_ACCESSIBLE.search(loc) and _LOCATION_SOFT_RESTRICTED.search(loc):
        return -10, [], [f"Location soft-restricted for la ciudad del candidato: '{job.location_raw}'"]

    return 0, [], []


def _apply_exclusion_penalty(
    job: JobResult,
    must_exclude: list[str],
) -> tuple[int, list[str], list[str]]:
    """
    Must-exclude check. Returns penalty (negative) if any exclusion hit.
    A single hit is a 10-point deduction (from the 10-pt inclusion pool).
    Multiple hits don't stack — one hit is enough to flag.
    """
    if not must_exclude:
        return 0, ["No exclusion keywords specified"], []

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or ""
    ])).lower()

    hits = [kw for kw in must_exclude if kw.lower() in searchable]

    if hits:
        return -10, [], [f"Exclusion keyword found: '{kw}'" for kw in hits]

    return 10, [f"No exclusion keywords detected (clean)"], []


# =============================================================================
# Tier Assignment
# =============================================================================

def _assign_tier(score: int) -> str:
    """Maps a 0–100 score to a Tier label per Vol 1.3 spec."""
    if score >= config.TIER1_MIN_SCORE:
        return "Tier 1"
    if score >= config.TIER2_MIN_SCORE:
        return "Tier 2"
    return "Tier 3"


# =============================================================================
# Public Interface
# =============================================================================

# ── Fase 3: los vetos salen del score ────────────────────────────────────────
#
# Hasta el 2026-09-15 la ubicacion era un delta que se sumaba al puntaje, y el
# bloqueo duro ademas forzaba `Tier 3` con `match_score=0`. Eso destruia
# informacion: un puesto excelente bloqueado por security clearance y uno
# genuinamente malo salian identicos —Tier 3, cero— y despues no habia forma de
# preguntarle a la base cuantos Tier 1 se perdieron por elegibilidad y no por
# calidad, porque la respuesta se habia borrado al guardar.
#
# El score mide ajuste y nada mas. La elegibilidad se responde aparte, con dos
# niveles: FAIL saca del shortlist conservando el puntaje, FLAG queda rankeado con
# marca. El nivel intermedio es el que faltaba: "USA" como ubicacion no es lo mismo
# que "must be authorized to work in the US", y tratarlos igual descarta avisos que
# muchas veces aceptan contratistas de LATAM.

_VERDICT_PASS = "PASS"
_VERDICT_FLAG = "FLAG"
_VERDICT_FAIL = "FAIL"


def _veredicto_desde_delta(delta: int, match: list[str],
                           miss: list[str]) -> tuple[str, str]:
    """
    Traduce la salida del detector de ubicación a (veredicto, nota). Función pura:
    no llama a `_score_location`, recibe su resultado.

    Que sea pura no es estética. `score_job` tiene que llamar a `_score_location`
    **una sola vez** —hay un test que lo verifica desde antes de esta fase, porque
    la función recorre tres listas de frases sobre un blob de texto— y una versión
    que volviera a llamarla rompería esa garantía en silencio.

        -50 clearance/ciudadanía         -> FAIL  (no podés, punto)
        -20 region-lock explícito        -> FAIL  ("must be authorized to work in the US")
        -10 geo blanda                   -> FLAG  (la ubicación dice "USA" y nada más)
        +10 abierto / 0 sin señal        -> PASS

    La diferencia entre FAIL y FLAG es la diferencia entre un requisito declarado y
    una ubicación mencionada. Un aviso cuyo `location_raw` dice "USA" suele aceptar
    contratistas de LATAM; uno que dice "must be authorized to work in the US" no.
    """
    nota = (miss[0] if miss else (match[0] if match else ""))
    if delta <= -20:
        return _VERDICT_FAIL, nota
    if delta <= -10:
        return _VERDICT_FLAG, nota
    return _VERDICT_PASS, nota


def _location_verdict(job: JobResult) -> tuple[str, str]:
    """(veredicto, nota) para un job. Envoltorio de conveniencia para los tests y
    para quien tenga el job pero no el delta; `score_job` usa la versión pura."""
    return _veredicto_desde_delta(*_score_location(job))


# Idiomas que un aviso puede exigir, con las formas en que se nombran a si mismos.
# Solo los que aparecen de verdad en boards remotos; agregar uno es agregar una fila.
_IDIOMAS: dict[str, tuple[str, ...]] = {
    "aleman":    ("german", "deutsch", "aleman", "alemán"),
    "frances":   ("french", "francais", "français", "frances", "francés"),
    "japones":   ("japanese", "nihongo", "japones", "japonés"),
    "mandarin":  ("mandarin", "chinese", "mandarín"),
    "coreano":   ("korean", "coreano"),
    "neerlandes": ("dutch", "nederlands", "neerlandes", "neerlandés"),
    "italiano":  ("italian", "italiano"),
    "portugues": ("portuguese", "portugues", "portugués"),
    "espanol":   ("spanish", "espanol", "español", "castellano"),
    "ingles":    ("english", "ingles", "inglés"),
}

# "fluent in X", "X native", "native-level X", "muttersprache"...
_EXIGE_IDIOMA = re.compile(
    r"\b(fluent|native|proficien\w*|bilingual|c1|c2|muttersprach\w*|"
    r"nativo|fluido|dominio)\b", re.I)


def _language_verdict(job: JobResult,
                      idiomas_propios: Optional[list[str]] = None) -> tuple[str, str]:
    """
    (veredicto, nota) sobre el idioma que el aviso exige.

    Conservador por diseño: solo dispara cuando el aviso **nombra** un idioma. Un
    titulo sin mencion de idioma es PASS, no FLAG — marcar todo es lo mismo que no
    marcar nada.

    FAIL si nombra un idioma ausente de tu perfil. FLAG si nombra uno que hablas
    junto a una exigencia de nivel ("fluent", "native", "C2"): podes postular, pero
    conviene saberlo antes de escribir la carta.

    El descarte silencioso no es opcion. Un aviso desaparecido no se puede auditar;
    uno marcado si.
    """
    propios = {i.lower() for i in (idiomas_propios
                                   if idiomas_propios is not None
                                   else getattr(config, "IDIOMAS_QUE_HABLO", []))}
    texto = " ".join(filter(None, [
        job.title, job.company, getattr(job, "location_raw", None) or "",
    ])).lower()
    if not texto.strip():
        return _VERDICT_PASS, ""

    for idioma, formas in _IDIOMAS.items():
        if not any(re.search(r"\b" + re.escape(f) + r"\b", texto) for f in formas):
            continue
        if idioma not in propios:
            return _VERDICT_FAIL, f"El aviso pide {idioma} y no esta en tu perfil"
        if _EXIGE_IDIOMA.search(texto):
            return _VERDICT_FLAG, f"Pide {idioma} a nivel alto — lo hablas, verificá el nivel"
    return _VERDICT_PASS, ""


def score_job(job: JobResult, search_config: SearchConfig, profile: Optional[dict] = None) -> TieredJob:
    """
    Scores a single JobResult and returns a TieredJob with full audit trail.

    TIER 4 BYPASS:
        Condition:  job.salary_raw is a non-empty string
                    AND job.parse_salary_usd() returns None

        This means the employer disclosed SOMETHING about compensation,
        but it is in prose form ('Competitive', 'DOE', 'Based on experience').
        These are NOT missing salary data — they are human-readable salary
        disclosures that the regex cannot parse as a number.

        Explicit guard — three conditions must ALL be true to trigger bypass:
            1. salary_raw is not None
            2. salary_raw is not an empty string (stripped)
            3. parse_salary_usd() returns None for this non-empty string

        This prevents the edge case where salary_raw=" " (whitespace only)
        from incorrectly triggering Tier 4.

    Score breakdown for Tier 1/2/3 (max 100):
        Title match    : 0–50
        Salary gate    : 0–25
        Must-include   : 0–15
        Exclusion bonus: -10 to +10
    """
    # ── Location scoring (single call — result reused for both hard-block check
    #    and the mathematical delta to avoid calling the pure function twice) ──
    # Fase 3 (2026-09-15): la elegibilidad se resuelve APARTE del puntaje.
    #
    # Acá había un bypass que devolvía `Tier 3` con `match_score=0` ante un bloqueo
    # duro. Con eso se perdía para siempre la diferencia entre un puesto excelente
    # bloqueado por security clearance y uno genuinamente malo: los dos salían Tier 3
    # con cero, y después no había cómo preguntarle a la base cuántos Tier 1 se
    # perdieron por elegibilidad y no por calidad.
    # UNA sola llamada a `_score_location` — recorre tres listas de frases sobre un
    # blob de texto, y hay un test desde antes de esta fase que verifica que no se
    # llame dos veces. La traducción a veredicto es pura y recibe el resultado.
    loc_delta, lm, ln = _score_location(job)
    loc_verdict, loc_note = _veredicto_desde_delta(loc_delta, lm, ln)
    lang_verdict, lang_note = _language_verdict(job)

    # ── Tier 4 bypass: non-empty text salary that is not a parseable number ──
    salary_is_text = (
        job.salary_raw is not None
        and job.salary_raw.strip() != ""
        and job.parse_salary_usd() is None
    )
    if salary_is_text:
        logger.debug(
            "[T4] Manual Review: '%s' @ %s — salary_raw=%r is non-numeric",
            job.title, job.company, job.salary_raw,
        )
        return TieredJob(
            job=job,
            match_score=-1,
            tier="Tier 4",
            match_reasons=["Salary disclosed as text — bypassed mathematical scoring"],
            miss_reasons=[
                f"Raw salary text: '{job.salary_raw}' — requires manual evaluation"
            ],
            location_verdict=loc_verdict, location_note=loc_note,
            language_verdict=lang_verdict, language_note=lang_note,
        )

    # ── Mathematical scoring path (Tier 1 / 2 / 3) ───────────────────────────
    all_match: list[str] = []
    all_miss:  list[str] = []

    title_score,   tm, tn  = _score_title(job.title, search_config.target_titles)
    salary_score,  sm, sn  = _score_salary(job, search_config.min_salary)
    include_score, im, in_ = _score_must_include(job, search_config.must_include)
    excl_delta,    em, en  = _apply_exclusion_penalty(job, search_config.must_exclude)
    skill_score,   km, kn  = _score_skill_overlap(job, profile)
    # loc_delta, lm, ln already computed above — reused here

    all_match.extend(tm + sm + im + em + km + lm)
    all_miss.extend(tn + sn + in_ + en + kn + ln)

    # El eje de salario solo participa si hay ambas puntas: un mínimo pedido y un
    # sueldo publicado que se pueda leer. Sin filtro, `_score_salary` daba 25 de 25
    # a todo el mundo; sin sueldo publicado, daba 12 fijos. Las dos son constantes
    # que inflan el promedio sin decir nada del calce.
    salary_aplica = (search_config.min_salary is not None
                     and job.parse_salary_usd() is not None)

    # ── Fase 4: dimensiones ponderadas que se RENORMALIZAN ───────────────────
    #
    # El presupuesto absoluto de 100 puntos tenía un eje muerto. `_score_salary`
    # devolvía 12 puntos fijos cuando el salario no estaba publicado: sobre el
    # registry histórico eso era el 99,5% de las filas, y sobre la corrida del 04-09
    # contra workingnomads el 62%. Un eje que le da la misma nota a casi todos no
    # discrimina — es una constante disfrazada de dimensión. El efecto medible: la
    # escala de 100 nunca pasaba de 90 y la masa se apilaba entre 40 y 60.
    #
    # Ahora una dimensión sin dato **no participa**, y el peso se reparte entre las
    # que sí lo tienen. Un aviso sin salario se puntúa sobre las tres restantes y su
    # resultado es comparable con uno que sí lo trae. Antes no lo era: uno arrastraba
    # una constante y el otro un número real.
    #
    # Los pesos conservan la proporción vieja (50/25/15/10). Lo que cambia es la
    # renormalización, no el criterio — mezclar las dos cosas volvería imposible
    # atribuir cualquier corrimiento de puntajes.
    dimensiones = [
        ("titulo",   0.50, title_score,           50, True),
        ("salario",  0.25, salary_score,          25, salary_aplica),
        ("incluye",  0.15, include_score,         15, bool(search_config.must_include)),
        ("excluye",  0.10, excl_delta + 10,       20, bool(search_config.must_exclude)),
    ]
    peso_total = sum(w for _, w, _, _, aplica in dimensiones if aplica)
    if peso_total <= 0:
        base = 0.0
    else:
        base = sum(w * (s / techo) for _, w, s, techo, aplica in dimensiones
                   if aplica) / peso_total * 100

    # El bono de perfil sigue siendo aditivo y fuera de la normalización: es un
    # extra sobre el calce, no una quinta dimensión del calce. El techo de 115 y el
    # umbral de Tier 1 en 80 quedan como estaban.
    final_score = max(0, min(115, int(round(base)) + skill_score))
    tier        = _assign_tier(final_score)

    logger.debug(
        "[SCORE] '%s' @ %s: %d → %s (T=%d S=%d I=%d X=%d)",
        job.title, job.company, final_score, tier,
        title_score, salary_score, include_score, excl_delta,
    )

    return TieredJob(
        job=job,
        match_score=final_score,
        tier=tier,
        match_reasons=all_match,
        miss_reasons=all_miss,
        location_verdict=loc_verdict, location_note=loc_note,
        language_verdict=lang_verdict, language_note=lang_note,
    )


def score_job_inline(job: JobResult, search_config: SearchConfig) -> str:
    """
    Lightweight tier classification for use inside the scraper's hot path.

    Called during card extraction BEFORE the full bucket_jobs pass to enable
    real-time Tier 1 webhook notifications. Must be fast — no logging overhead
    beyond DEBUG level, no DB writes, returns only the tier string.

    DESIGN: Calls score_job() directly — same logic, no duplication.
    The overhead is microseconds per card. At 100 cards per domain × 4 domains
    = 400 calls. Total overhead: < 5ms. Acceptable.

    Returns: 'Tier 1', 'Tier 2', 'Tier 3', or 'Tier 4'
    """
    return score_job(job, search_config).tier


def bucket_jobs(
    jobs: list[JobResult],
    search_config: SearchConfig,
    db=None,
    profile: Optional[dict] = None,
    repuntuar_todo: bool = False,
) -> tuple[list[TieredJob], list[TieredJob], list[TieredJob], list[TieredJob]]:
    """
    Scores and buckets all jobs into four tiers. Returns (t1, t2, t3, t4).

    Tier 4 contains jobs where salary_raw is a non-empty, non-numeric string.
    They bypass mathematical scoring and must be reviewed manually via Notion.

    If db is provided, backfills tier + match_score in the seen_registry
    for the Streamlit dashboard. Tier 4 jobs are stored with match_score=-1.

    Note: score_job_inline() is the lightweight fast-path used during live
    scraping for real-time webhook triggers. bucket_jobs() is the post-scrape
    full pass that populates all display data.
    """
    tier1, tier2, tier3, tier4 = [], [], [], []
    filtered_off_topic = 0
    filtrados_no_elegibles = 0
    no_elegibles: list[TieredJob] = []

    # ── Fase 8: re-puntuar solo lo que hace falta ────────────────────────────
    #
    # `bucket_jobs` re-puntuaba TODO en cada corrida. Con 1.574 filas todavia no
    # duele, pero las Fases 1 y 2 abrieron la ingesta por endpoint y ATS: una
    # corrida ya trae 940 avisos relevantes en 7,7 s, y las 40 fuentes registradas
    # suman ~8.700. Re-puntuar lo ya puntuado es trabajo que crece con el catalogo
    # y no aporta nada — salvo cuando cambia el algoritmo, que es justo el caso que
    # `repuntuar_todo` habilita.
    #
    # Lo que NO se saltea es el barrido de deadlines de la Fase 6: cuesta cero red
    # y corre siempre. La excepcion es deliberada y esta declarada en el plan.
    from database import GemaDatabase   # import diferido: evita el ciclo

    ya_puntuados: set = set()
    if db is not None and not repuntuar_todo:
        try:
            ya_puntuados = db.hashes_puntuados(version=getattr(config, "SCORE_VERSION", None))
        except Exception as exc:
            # Que el atajo falle no puede impedir la corrida: se degrada a
            # re-puntuar todo, que es el comportamiento anterior.
            logger.debug("[BUCKET] no se pudo leer lo ya puntuado (%s) — se repuntua todo", exc)
    saltados_ya_puntuados = 0
    pendientes: list[tuple] = []

    for job in jobs:
        # AUDIT NEW #1: descartar ruido off-topic ANTES de puntuar (File Clerk,
        # Crypto Trader…). Conservador: solo cae si NO comparte ningún token
        # significativo con la búsqueda ni el perfil.
        if not title_is_relevant(job.title, search_config.target_titles, profile):
            filtered_off_topic += 1
            continue

        # Fase 8: ya puntuado con ESTA version del algoritmo -> se saltea.
        # La version importa: un aviso puntuado con v1 tiene que volver a pasar,
        # porque las Fases 3 y 4 cambiaron como se puntua y ese numero ya no es
        # comparable con los nuevos.
        if ya_puntuados and GemaDatabase.compute_hash(job.url) in ya_puntuados:
            saltados_ya_puntuados += 1
            continue

        tiered = score_job(job, search_config, profile=profile)

        # Fase 3: un veto FAIL saca del shortlist, NO del registro. El aviso se
        # persiste con su tier y su puntaje reales —abajo, en update_tier— pero no
        # entra a las listas que van al front, a Notion y al Excel.
        #
        # Es la diferencia entre "no puedo postular a esto" y "esto no me sirve".
        # Un puesto de 90 puntos bloqueado por security clearance sigue valiendo 90;
        # lo que no tiene sentido es ponerlo arriba de tu lista de postulación.
        # Borrarlo seria peor: un aviso desaparecido no se puede auditar, y despues
        # no hay forma de preguntar cuantos Tier 1 se perdieron por elegibilidad.
        if not tiered.elegible:
            filtrados_no_elegibles += 1
            no_elegibles.append(tiered)
        elif tiered.tier == "Tier 1":
            tier1.append(tiered)
        elif tiered.tier == "Tier 2":
            tier2.append(tiered)
        elif tiered.tier == "Tier 3":
            tier3.append(tiered)
        else:
            tier4.append(tiered)

        # Fase 8: se acumulan y se escriben en UNA transaccion al final.
        # `update_tier` cuesta 107 ms por aviso medidos — abre conexion, corre
        # PRAGMA journal_mode=WAL, commitea y cierra, un fsync por fila. Con 940
        # avisos por corrida eso son ~100 s de puro escribir contra los 7,7 s que
        # tarda traerlos.
        #
        # Se pasa el TieredJob entero (Fase 5) para que las razones del match y los
        # veredictos de elegibilidad tambien se persistan: sin eso la base puede
        # decir QUE puntaje saco un aviso y nunca POR QUE.
        if db:
            pendientes.append((job.url, tiered.tier, tiered.match_score,
                               job.salary_raw, tiered))

    if db and pendientes:
        try:
            db.update_tiers_batch(pendientes)
        except Exception as exc:
            # El scrape ya guardo los avisos; perder el backfill del tier degrada la
            # vista, no la data. Se reporta y se sigue.
            logger.warning("[DB] No se pudo backfillear el tier de %d avisos: %s",
                           len(pendientes), exc)

    if saltados_ya_puntuados:
        logger.info("[BUCKET] %d avisos ya puntuados con %s — salteados",
                    saltados_ya_puntuados, getattr(config, "SCORE_VERSION", "?"))
        metrics.emit("jobs_ya_puntuados", saltados_ya_puntuados)

    logger.info(
        "[BUCKET] T1=%d | T2=%d | T3=%d | T4(manual)=%d | off-topic=%d | no-elegibles=%d",
        len(tier1), len(tier2), len(tier3), len(tier4), filtered_off_topic,
        filtrados_no_elegibles,
    )
    if filtered_off_topic:
        metrics.emit("jobs_filtered_off_topic", filtered_off_topic)
    if filtrados_no_elegibles:
        # Se cuentan por causa, no en un solo total: "no puedo postular por idioma"
        # y "no puedo postular por ciudadania" se arreglan distinto, y el dia que uno
        # de los dos se dispare de mas hay que poder ver cual.
        por_causa: dict[str, int] = {}
        for tj in no_elegibles:
            if tj.location_verdict == "FAIL":
                por_causa["location"] = por_causa.get("location", 0) + 1
            if tj.language_verdict == "FAIL":
                por_causa["language"] = por_causa.get("language", 0) + 1
        for causa, n in por_causa.items():
            metrics.emit("jobs_no_elegibles", n, causa=causa)
        logger.info("[BUCKET] no-elegibles por causa: %s", por_causa)
    return tier1, tier2, tier3, tier4
