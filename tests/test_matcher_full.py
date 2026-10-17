"""
tests/test_matcher_full.py — Complete pytest suite for matcher.py.

Coverage:
    1. _score_title           — exact, contains, all-words, partial, no-match
    2. _score_salary          — no filter, meets min, below min, not published
    3. _score_must_include    — all present, partial, empty list
    4. _apply_exclusion_penalty — hit, clean, empty list
    5. score_job              — Tier 4 bypass, Tier 1, Tier 3, score=115 (regression fix)
    6. bucket_jobs            — 4-tier distribution, db update_tiers_batch called

No external API calls or disk access.
"""

from unittest.mock import MagicMock

import pytest

from matcher import (
    _apply_exclusion_penalty,
    _score_must_include,
    _score_salary,
    _score_title,
    bucket_jobs,
    score_job,
)
from models import JobResult, SearchConfig, TieredJob


# =============================================================================
# Shared fixtures
# =============================================================================

def _job(title: str, company: str = "TestCorp", salary_raw: str = None, url_id: int = 1) -> JobResult:
    return JobResult(
        title=title,
        company=company,
        salary_raw=salary_raw,
        url=f"https://example.com/job/{url_id}",
        source_domain="example.com",
    )


def _config(
    titles=None,
    min_salary=None,
    must_include=None,
    must_exclude=None,
) -> SearchConfig:
    return SearchConfig(
        target_titles=titles or ["Backend Engineer"],
        min_salary=min_salary,
        must_include=must_include or [],
        must_exclude=must_exclude or [],
        target_domains=["example.com"],
    )


FULL_PROFILE = {
    "core_skills": ["Python", "FastAPI", "Docker"],
    "audit_signals": ["SRE", "idempotency"],
}


# =============================================================================
# 1. _score_title
# =============================================================================

# Calibracion 2026-09-17: el titulo dejo de puntuarse en cinco escalones
# (0/15/35/40/50) y pasa a una curva continua sobre cobertura y precision. Con la
# renormalizacion de la Fase 4 esos cinco escalones se volvian 0/30/70/80/100, y como
# el titulo es el unico eje que casi siempre participa, un calce exacto bastaba para
# llegar a 100: 205 avisos con score 100 en la corrida del 17-09.
#
# Estos tests verifican el ORDEN, no los numeros exactos: un escalon puntual es un
# detalle de implementacion, que un calce mejor puntue mas alto que uno peor no lo es.

def test_score_title_exact_match():
    score, match, miss = _score_title("Backend Engineer", ["Backend Engineer"])
    assert score == 50
    assert match


def test_un_calce_exacto_puntua_mas_que_uno_con_palabras_de_mas():
    """Lo que la calibracion vino a arreglar: "Senior Backend Engineer, Payments
    (EMEA)" y "Backend Engineer" puntuaban igual contra el target "backend engineer",
    porque los dos lo contienen. El primero es un puesto distinto con el target
    adentro."""
    exacto, _, _ = _score_title("Backend Engineer", ["Backend Engineer"])
    con_ruido, _, _ = _score_title("Senior Backend Engineer, Payments (EMEA)",
                                   ["Backend Engineer"])
    assert exacto > con_ruido > 0


def test_el_orden_de_las_palabras_no_penaliza():
    """El match es por tokens: "Engineer Backend Senior" es el mismo puesto que
    "Senior Backend Engineer" escrito al reves."""
    score, _, _ = _score_title("Engineer Backend Senior", ["Senior Backend Engineer"])
    assert score >= 40


def test_score_title_partial_match():
    """Tolerante a la raiz: `engineer` cuenta como cubierto por `engineering`. La
    version de escalones comparaba subcadenas; pasar a tokens exactos perdia esto."""
    score, match, miss = _score_title("Engineering Lead", ["Backend Engineer"])
    assert 0 < score < 50
    assert match


def test_un_titulo_sin_nada_en_comun_da_cero():
    score, match, miss = _score_title("Chef de Cuisine", ["Backend Engineer"])
    assert score == 0
    assert miss


def test_score_title_no_match():
    score, match, miss = _score_title("Product Designer", ["Backend Engineer"])
    assert score == 0
    assert len(miss) > 0


def test_score_title_caps_at_50():
    """Even with many matching targets, max is 50."""
    score, _, _ = _score_title(
        "Backend Engineer",
        ["Backend Engineer", "Backend Engineer", "Backend Engineer"],
    )
    assert score <= 50


# =============================================================================
# 2. _score_salary
# =============================================================================

def test_score_salary_no_filter_returns_25():
    job = _job("Backend Engineer", salary_raw="$120,000")
    score, match, miss = _score_salary(job, min_salary=None)
    assert score == 25
    assert any("No salary filter" in r for r in match)


def test_score_salary_meets_minimum():
    job = _job("Backend Engineer", salary_raw="$150k")
    score, match, miss = _score_salary(job, min_salary=100_000)
    assert score == 25
    assert any("meets" in r.lower() for r in match)


def test_score_salary_below_minimum():
    job = _job("Backend Engineer", salary_raw="$60k")
    score, match, miss = _score_salary(job, min_salary=100_000)
    assert score == 0
    assert any("below" in r.lower() for r in miss)


def test_score_salary_not_published_returns_12():
    job = _job("Backend Engineer", salary_raw=None)
    score, _, miss = _score_salary(job, min_salary=100_000)
    assert score == 12
    assert any("manual review" in r.lower() for r in miss)


# =============================================================================
# 3. _score_must_include
# =============================================================================

def test_score_must_include_all_present():
    job = _job("Remote Backend Engineer Python", salary_raw="$120k Remote")
    score, match, miss = _score_must_include(job, ["Remote", "Python"])
    assert score == 15
    assert miss == []


def test_score_must_include_partial():
    job = _job("Backend Engineer Python")
    score, match, miss = _score_must_include(job, ["Python", "Remote"])
    assert 0 < score < 15
    assert any("Python" in r for r in match)
    assert any("Remote" in r for r in miss)


def test_score_must_include_empty_list():
    job = _job("Backend Engineer")
    score, match, miss = _score_must_include(job, [])
    assert score == 15
    assert any("No must-include" in r for r in match)


# =============================================================================
# 4. _apply_exclusion_penalty
# =============================================================================

def test_exclusion_penalty_hit():
    job = _job("Contract Backend Engineer")
    score, match, miss = _apply_exclusion_penalty(job, ["Contract"])
    assert score == -10
    assert any("Contract" in r for r in miss)


def test_exclusion_penalty_clean():
    job = _job("Remote Backend Engineer")
    score, match, miss = _apply_exclusion_penalty(job, ["Contract"])
    assert score == 10
    assert any("clean" in r.lower() for r in match)


def test_exclusion_penalty_empty_list():
    job = _job("Remote Backend Engineer")
    score, match, miss = _apply_exclusion_penalty(job, [])
    assert score == 0
    assert any("No exclusion" in r for r in match)


# =============================================================================
# 5. score_job
# =============================================================================

def test_score_job_salario_en_texto_se_puntua_sin_el_eje():
    """Hasta v5 un salario en texto mandaba a Tier 4 con -1. Desde v6 se puntua sin el eje de salario y se marca «no confirmado»."""
    job = _job("Backend Engineer", salary_raw="Competitive + equity")
    config = _config(titles=["Backend Engineer"])
    tiered = score_job(job, config)
    assert tiered.tier in ("Tier 1", "Tier 2", "Tier 3")
    assert tiered.match_score >= 0
    assert tiered.salario_no_confirmado is True
    assert any("Salario no confirmado" in r for r in tiered.miss_reasons)


def test_score_job_tier1_high_score():
    """Exact title + salary met + all keywords present = Tier 1."""
    job = _job("Backend Engineer", salary_raw="$120k", url_id=10)
    config = _config(titles=["Backend Engineer"], min_salary=100_000)
    tiered = score_job(job, config)
    assert tiered.tier == "Tier 1"
    assert tiered.match_score >= 80


def test_score_job_tier3_low_score():
    """No title match + salary below minimum = Tier 3."""
    job = _job("Product Designer", salary_raw="$40k", url_id=20)
    config = _config(titles=["Backend Engineer"], min_salary=100_000)
    tiered = score_job(job, config)
    assert tiered.tier == "Tier 3"
    assert tiered.match_score < 50


def test_score_job_returns_tiered_job_instance():
    job = _job("Backend Engineer", url_id=30)
    config = _config()
    result = score_job(job, config)
    assert isinstance(result, TieredJob)
    assert result.tier in {"Tier 1", "Tier 2", "Tier 3", "Tier 4"}


def test_score_job_accepts_score_above_100_regression():
    """
    Regression test for the le=100 bug.

    Before the fix: TieredJob(match_score=115) raised pydantic.ValidationError,
    silently dropping all high-quality Tier 1 results when a CV was loaded.
    After the fix (le=115): this test must pass.

    Trigger conditions:
        - title_exact(50) + salary_ok(25) + include_full(15) + no_exclude(+10) = 100
        - skill_overlap bonus (+15) pushes to 115
    """
    job = _job(
        title="Python SRE Backend Engineer FastAPI Docker",
        salary_raw="$150k",
        url_id=99,
    )
    config = _config(
        titles=["Python SRE Backend Engineer FastAPI Docker"],
        min_salary=100_000,
        must_include=["Python"],
    )
    tiered = score_job(job, config, profile=FULL_PROFILE)

    assert tiered.match_score >= 100
    assert tiered.tier == "Tier 1"


def test_score_job_with_exclusion_found():
    """Exclusion keyword in title applies -10 penalty."""
    job = _job("Contract Backend Engineer", salary_raw="$120k", url_id=40)
    config = _config(titles=["Backend Engineer"], must_exclude=["Contract"])
    tiered = score_job(job, config)
    assert any("Contract" in r for r in tiered.miss_reasons)


# =============================================================================
# 6. bucket_jobs
# =============================================================================

def test_bucket_jobs_distributes_into_four_tiers():
    jobs = [
        _job("Backend Engineer", salary_raw="$150k", url_id=100),          # Tier 1
        _job("Backend Engineer", salary_raw="$80k", url_id=101),            # Tier 2
        _job("Backend Engineer", salary_raw="Competitive + equity", url_id=103),  # v6: no confirmado
        _job("File Clerk", salary_raw="$40k", url_id=102),                  # off-topic → filtrado
    ]
    config = _config(titles=["Backend Engineer"], min_salary=130_000)
    t1, t2, t3, t4 = bucket_jobs(jobs, config)

    # v6: el salario en texto ya no puebla Tier 4; se puntua y queda marcado.
    assert t4 == []
    assert any(tj.salario_no_confirmado for tj in t1 + t2 + t3)
    total = len(t1) + len(t2) + len(t3) + len(t4)
    # AUDIT NEW #1: "File Clerk" es off-topic → descartado ANTES de puntuar.
    assert total == len(jobs) - 1
    all_titles = [tj.job.title for tj in (t1 + t2 + t3 + t4)]
    assert "File Clerk" not in all_titles


def test_bucket_jobs_persiste_el_tier_de_cada_job():
    """Con un db, el tier de cada aviso tiene que quedar persistido.

    Fase 8: la escritura pasa a ser en LOTE. escribir fila por fila costaba 107 ms
    medidos -- _get_connection abre, corre PRAGMA journal_mode=WAL, commitea y
    cierra, un fsync por fila -- y con 940 avisos por corrida eso eran ~100 s de
    puro escribir. Lo que el test verifica sigue siendo lo mismo: que ningun aviso
    se quede sin persistir.
    """
    mock_db = MagicMock()
    mock_db.hashes_puntuados.return_value = set()
    jobs = [_job("Backend Engineer", salary_raw="$150k", url_id=200)]
    config = _config(titles=["Backend Engineer"])
    bucket_jobs(jobs, config, db=mock_db)

    mock_db.update_tiers_batch.assert_called_once()
    (filas,), _ = mock_db.update_tiers_batch.call_args
    assert len(filas) == 1
    url, tier, score, _salario, tiered = filas[0]
    assert url == jobs[0].url and tier and tiered is not None


def test_bucket_jobs_empty_input_returns_four_empty_lists():
    config = _config()
    t1, t2, t3, t4 = bucket_jobs([], config)
    assert t1 == t2 == t3 == t4 == []
