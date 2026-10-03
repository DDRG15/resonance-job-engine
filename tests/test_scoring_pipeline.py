"""
tests/test_scoring_pipeline.py — Integration tests for the full score_job() execution order.

score_job() has three mutually exclusive paths:
  1. Hard location block (clearance/citizenship) → Tier 3 immediately, score=0
  2. Tier 4 bypass (non-empty, non-numeric salary) → Tier 4, score=-1
  3. Mathematical path (Tier 1/2/3) → title+salary+include+excl+skill+loc_delta

The priority order is: hard block → Tier 4 bypass → math path.
A regression that swaps this order (e.g. Tier 4 bypass fires before the hard block)
silently buries clearance-required jobs in Tier 4 instead of Tier 3, where they
accumulate unreviewed in the Notion database without any location flag.

After BUG-2 fix, _score_location() is called exactly once per score_job() invocation.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, call

from matcher import score_job, _score_location
from models import JobResult, SearchConfig
import config


def _now():
    return datetime.now(timezone.utc)


def _job(title="Python Engineer", company="TestCo", n=1,
         location=None, salary=None):
    return JobResult(
        title=title, company=company,
        url=f"https://example.com/job/{n}",
        source_domain="test.board",
        scraped_at=_now(),
        location_raw=location,
        salary_raw=salary,
    )


_CFG = SearchConfig(target_titles=["Python Engineer", "Backend Engineer"])
_CFG_MIN = SearchConfig(target_titles=["Python Engineer"], min_salary=100_000)


# =============================================================================
# Execution path priority
# =============================================================================

class TestExecutionPathPriority:

    def test_hard_block_no_secuestra_el_tier_pero_si_marca_el_veredicto(self):
        """Fase 3: el bloqueo duro dejo de forzar Tier 3 con score 0.

        Antes competia con el bypass de Tier 4 por decidir el tier, y ganaba. Ahora
        son dos ejes distintos: el tier sale del salario y el ajuste, la elegibilidad
        sale del veredicto. Un aviso con clearance Y salario en texto se puntua (v6:
        sin el eje de salario, marcado) y es FAIL por la elegibilidad, sin que uno
        pise al otro."""
        j = _job(salary="security clearance required — non-numeric", location=None, n=1)
        tiered = score_job(j, _CFG)
        assert tiered.location_verdict == "FAIL"
        assert tiered.elegible is False
        assert tiered.match_score >= 0          # el veto no secuestra el puntaje
        assert tiered.salario_no_confirmado is True

    def test_salario_en_texto_va_por_el_camino_matematico(self):
        # v6 (D3): el bypass a Tier 4 ya no existe; el eje de salario no participa.
        j = _job(salary="Competitive + equity", location="Anywhere", n=2)
        tiered = score_job(j, _CFG)
        assert tiered.tier in ("Tier 1", "Tier 2", "Tier 3")
        assert tiered.match_score >= 0

    def test_math_path_fires_when_no_bypass_conditions(self):
        # No clearance, numeric salary → mathematical path → Tier 1/2/3
        j = _job(salary="$90,000 per year", n=3)
        tiered = score_job(j, _CFG)
        assert tiered.tier in ("Tier 1", "Tier 2", "Tier 3")
        assert tiered.match_score >= 0

    def test_no_salary_goes_through_math_path(self):
        # None salary → math path (not Tier 4 bypass)
        j = _job(salary=None, n=4)
        tiered = score_job(j, _CFG)
        assert tiered.tier in ("Tier 1", "Tier 2", "Tier 3")


# =============================================================================
# _score_location called exactly once (BUG-2 regression guard)
# =============================================================================

class TestScoreLocationCalledOnce:

    def test_score_location_called_once_in_math_path(self):
        j = _job(location="Anywhere", n=10)
        with patch("matcher._score_location", wraps=_score_location) as mock_loc:
            score_job(j, _CFG)
        assert mock_loc.call_count == 1

    def test_score_location_called_once_on_hard_block(self):
        j = _job(salary="security clearance required", n=11)
        with patch("matcher._score_location", wraps=_score_location) as mock_loc:
            score_job(j, _CFG)
        assert mock_loc.call_count == 1

    def test_score_location_called_once_on_tier4(self):
        j = _job(salary="Competitive", n=12)
        with patch("matcher._score_location", wraps=_score_location) as mock_loc:
            score_job(j, _CFG)
        assert mock_loc.call_count == 1


# =============================================================================
# Location delta applied in mathematical path
# =============================================================================

class TestLocationDeltaInMathPath:

    def test_una_ubicacion_abierta_ya_no_sube_el_puntaje(self):
        """Fase 3: la ubicacion salio del score. Que un aviso diga "Anywhere" no lo
        hace un mejor calce con tu busqueda — lo hace accesible, que es otra cosa y
        se responde en location_verdict."""
        t_neutral = score_job(_job(location=None, n=20), _CFG)
        t_open    = score_job(_job(location="Anywhere", n=21), _CFG)
        assert t_open.match_score == t_neutral.match_score
        assert t_open.location_verdict == "PASS"

    def test_una_region_bloqueada_ya_no_baja_el_puntaje_pero_da_FAIL(self):
        """El puesto calza igual de bien; lo que cambia es que no podes postular.
        Mezclar las dos cosas en un numero borraba la diferencia entre "no me sirve"
        y "no puedo"."""
        t_neutral    = score_job(_job(location=None, n=22), _CFG)
        t_restricted = score_job(_job(location="us only", n=23), _CFG)
        assert t_restricted.match_score == t_neutral.match_score
        assert t_restricted.location_verdict == "FAIL"

    def test_latam_neutral_usa_marcado_pero_no_penalizado(self):
        """La política de AUDIT #2 sigue vigente; cambió dónde se expresa.

        Antes: LATAM neutral, «USA» pelado restaba 10 puntos. Ahora los tres puntúan
        igual —el calce con la búsqueda es el mismo— y «USA» se lleva un FLAG. Es el
        nivel intermedio que faltaba: una ubicación mencionada no es un requisito
        declarado, y muchos avisos con «USA» aceptan contratistas de LATAM.
        """
        t_no_loc = score_job(_job(location=None, n=24), _CFG)
        t_brazil = score_job(_job(location="Brazil", n=25), _CFG)
        t_usa    = score_job(_job(location="USA", n=26), _CFG)

        assert t_brazil.match_score == t_no_loc.match_score
        assert t_usa.match_score == t_no_loc.match_score

        assert t_brazil.location_verdict == "PASS"
        assert t_usa.location_verdict == "FLAG"
        assert t_usa.elegible is True, "un FLAG se marca, no se descarta"


# =============================================================================
# Score bounds
# =============================================================================

class TestScoreBounds:

    def test_score_capped_at_115(self):
        # Perfect title match (50) + no salary filter (25) + must_include (15)
        # + excl bonus (10) + skill bonus (15) + location bonus (10) = 125 → cap 115
        profile = {"core_skills": ["python", "fastapi", "django", "postgres", "redis",
                                   "docker", "kubernetes", "aws", "api", "backend",
                                   "engineer", "sql", "nosql", "linux", "git"],
                   "audit_signals": []}
        j = _job(title="Python Engineer", location="Anywhere", n=30)
        tiered = score_job(j, _CFG, profile=profile)
        assert tiered.match_score <= 115

    def test_score_floor_at_zero(self):
        # Extreme penalty scenario: no title match + no salary + restricted location
        j = _job(title="Unrelated Job Title XYZ", location="us only", n=31)
        tiered = score_job(j, _CFG)
        assert tiered.match_score >= 0


# =============================================================================
# Tier assignment boundary conditions
# =============================================================================

class TestTierBoundaries:

    def test_tier1_threshold_is_config_tier1_min_score(self):
        assert config.TIER1_MIN_SCORE == 80

    def test_tier2_threshold_is_config_tier2_min_score(self):
        assert config.TIER2_MIN_SCORE == 50

    def test_score_at_tier1_min_is_tier1(self):
        # Verify _assign_tier boundary directly — score=80 → Tier 1 (80 >= TIER1_MIN_SCORE)
        from matcher import _assign_tier
        assert _assign_tier(80) == "Tier 1"
        assert _assign_tier(79) == "Tier 2"

    def test_score_below_tier2_min_is_tier3(self):
        # Verify _assign_tier boundary — score=50 → Tier 2, score=49 → Tier 3
        from matcher import _assign_tier
        assert _assign_tier(50) == "Tier 2"
        assert _assign_tier(49) == "Tier 3"

    def test_calce_perfecto_de_titulo_llega_al_techo(self):
        """Fase 4: con dimensiones renormalizadas, un calce perfecto llega a 100.

        Antes daba 90 y no por mérito: `_CFG` no tiene filtro de salario ni
        must_include, así que esos ejes regalaban 25 y 15 puntos fijos a todo el
        mundo, y el techo real de la escala nunca se tocaba. Ahora las dimensiones
        sin dato no participan y el peso se reparte entre las que sí — con solo el
        título aplicando, un calce exacto vale el 100% de lo que hay para dar.

        Que el techo sea alcanzable es el punto: una escala de 100 cuyo máximo
        observado era 90 sobre 1.399 filas no estaba midiendo hasta arriba.
        """
        j = _job(title="Python Engineer", location=None, n=42)
        tiered = score_job(j, _CFG)
        assert tiered.match_score == 100
        assert tiered.tier == "Tier 1"


# =============================================================================
# Reason population
# =============================================================================

class TestReasonPopulation:

    def test_match_reasons_include_location_info_for_open(self):
        j = _job(location="Anywhere", n=50)
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() or "open" in r.lower() or "anywhere" in r.lower()
                   for r in tiered.match_reasons)

    def test_miss_reasons_include_location_info_for_restricted(self):
        j = _job(location="us only", n=51)
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() or "restricted" in r.lower()
                   for r in tiered.miss_reasons)

    def test_hard_block_explica_por_que_en_la_nota_del_veredicto(self):
        """La razon sigue estando; cambio donde vive. Un FAIL sin nota no se puede
        auditar tres meses despues."""
        tiered = score_job(_job(salary="Top Secret clearance required", n=52), _CFG)
        assert tiered.location_verdict == "FAIL"
        assert tiered.location_note
        assert any(p in tiered.location_note.lower()
                   for p in ("block", "clearance", "hard"))
