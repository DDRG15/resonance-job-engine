"""
La version del scoring y lo que el scoring devuelve se mueven JUNTOS.

Dos veces en dos semanas cambio la salida de score_job sin que la version se moviera:
- 9bc8e4b (2026-09-17) cambio _score_title y dejo SCORE_VERSION en v2: 1024 filas con
  puntajes escalonados convivieron con los continuos bajo la misma etiqueta.
- 2026-09-29: el veredicto de ubicacion aprendio los paises con tilde y los ISO-3 de
  LATAM. Sin subir a v4, las filas ya puntuadas en v3 se hubieran quedado con el
  veredicto viejo para siempre, porque el atajo de idempotencia las saltea.

Este archivo congela salidas concretas junto con la version vigente. Si alguien cambia
el algoritmo, falla con un mensaje que dice que hacer. Si el cambio es intencional:
subir SCORE_VERSION en config.py, actualizar los valores de aca en el MISMO commit, y
correr tools/repuntuar_registry.py.
"""

import pytest

import config
import matcher
from models import JobResult

VERSION_CONGELADA = "v6"

TITULOS = {
    ("Backend Engineer", ("backend engineer",)): 50,
    ("Senior Software Engineer", ("software engineer",)): 45,
    ("Golang Kubernetes Engineer", ("backend engineer",)): 22,
    ("Principal Platform Engineer", ("platform engineer",)): 45,
}

# (location_raw, veredicto). Los formatos son los reales de los boards: builtin publica
# "la ciudad del candidato, PER" y listas con paises con tilde; jobspresso "United States, Canada".
UBICACIONES = [
    ("Perú; Canada", "PASS"),
    ("la ciudad del candidato, PER; Remote, OR, USA", "PASS"),
    ("Greece; Norway; Perú; Spain; Cyprus", "PASS"),
    ("Mexico City, Ciudad De México, MEX", "PASS"),
    ("Bogotá, Distrito Capital, COL; United States", "PASS"),
    ("Worldwide", "PASS"),
    ("", "PASS"),
    ("USA", "FLAG"),
    ("United Kingdom", "FLAG"),
    ("Remote - Europe", "FLAG"),
    # El ISO-3 solo cuenta cerrando un elemento de la lista: "per" suelto no es Peru.
    ("Remote (per diem); USA", "FLAG"),
]


def _mensaje(que, antes, ahora):
    return (
        f"{que} cambio: {antes!r} -> {ahora!r}. Si es intencional, subi SCORE_VERSION "
        f"(hoy {config.SCORE_VERSION!r}), actualiza este archivo en el mismo commit y "
        f"corre tools/repuntuar_registry.py. Si no, es una regresion."
    )


def test_la_version_vigente_es_la_congelada():
    assert config.SCORE_VERSION == VERSION_CONGELADA, (
        "SCORE_VERSION se movio sin actualizar las salidas congeladas de este archivo"
    )


@pytest.mark.parametrize("entrada,esperado", list(TITULOS.items()))
def test_puntaje_de_titulo(entrada, esperado):
    titulo, targets = entrada
    real, _, _ = matcher._score_title(titulo, list(targets))
    assert real == esperado, _mensaje(f"_score_title({titulo!r})", esperado, real)


@pytest.mark.parametrize("ubicacion,esperado", UBICACIONES)
def test_veredicto_de_ubicacion(ubicacion, esperado):
    job = JobResult(title="Backend Engineer", company="Acme", url="https://x.com/1",
                    source_domain="builtin.com", location_raw=ubicacion or None)
    real, _ = matcher._location_verdict(job)
    assert real == esperado, _mensaje(f"veredicto de {ubicacion!r}", esperado, real)


# (salary_raw, USD anuales). Solo montos en USD: una tasa de cambio que se actualiza no
# es un cambio de algoritmo y no deberia romper este archivo.
SALARIOS = [
    ("$154,445 - $185.334", 154445),       # typo real de Grafana Labs: punto de miles
    ("$139,200—$235,200", 139200),         # rango de gitlab, spans inline pegados
    ("$120,000.00", 120000),
    ("$85.5k", 85500),                     # 1 digito tras el punto: decimal
    ("USD 120k", 120000),                  # codigo delante
    ("USD 120,000 - USD 150,000", 120000),
    ("USD 2,000", None),                   # presupuesto de formacion de Canonical
    ("EUR 109", None),                     # truncado: no inventar 109 euros al ano
    ("401k match", None),
    ("Competitive", None),
    # v6: el periodo se anualiza. Remotive publica asi, y era 90 dolares al ano.
    ("$90 - $150 /hour", 187200),
    ("$3,500/month", 42000),
    ("$150,000 base + 2 days per month remote", 150000),   # ya anual: no se multiplica
    ("$3,500", None),                                       # sin periodo, sigue cerrado
]


@pytest.mark.parametrize("crudo,esperado", SALARIOS)
def test_salario_anual_usd(crudo, esperado):
    from models import salario_anual_usd
    real = salario_anual_usd(crudo)
    assert real == esperado, _mensaje(f"salario de {crudo!r}", esperado, real)


def test_el_veto_duro_sigue_siendo_fail():
    job = JobResult(title="Backend Engineer - must be a US citizen", company="Acme",
                    url="https://x.com/2", source_domain="builtin.com",
                    location_raw="Worldwide")
    real, _ = matcher._location_verdict(job)
    assert real == "FAIL", _mensaje("veto de ciudadania", "FAIL", real)


def test_salario_en_texto_se_puntua_y_se_marca():
    """v6 (D3): "Competitive" ya no es Tier 4 con -1; se puntua sin el eje de salario."""
    from models import SearchConfig
    job = JobResult(title="Backend Engineer", company="Acme", url="https://x.com/3",
                    source_domain="x.com", salary_raw="Competitive", location_raw="Worldwide")
    tj = matcher.score_job(job, SearchConfig(target_titles=["Backend Engineer"],
                                             min_salary=90000))
    real = (tj.tier, tj.salario_no_confirmado)
    assert tj.match_score >= 0 and real[1] is True and real[0] != "Tier 4",         _mensaje("salario en texto", "puntuado y marcado", real)
