"""
eval_scoring.py — las cuatro métricas de calidad de extracción, puras.

Separadas del CLI a propósito: el scoring se puede probar con callers mockeados y sin
tocar la red, que es lo que permite que exista un test de pytest para el harness sin que
la suite pegue a proveedores reales.

QUÉ MIDE CADA UNA, Y QUÉ SIGNIFICA QUE BAJE
--------------------------------------------
grounding_rate     — de los jobs que el modelo devolvió, cuántos tienen su URL
                     LITERALMENTE en el texto que se le mandó. Es la única métrica que no
                     depende de tener respuestas correctas anotadas: la fuente es su
                     propio ground truth. Si baja, el modelo empezó a inventar URLs.

hallucination_rate — el complemento: qué fracción fabricó. Se reporta aparte porque un
                     grounding de 0.9 con 40 jobs y uno con 4 no son el mismo riesgo.

fields_complete    — promedio de campos no vacíos sobre los que importan para el matcher
                     (title, company, url, salary). Detecta el modo de falla silencioso:
                     el modelo devuelve la cantidad correcta de jobs, todos grounded, y
                     con la mitad de los campos en null.

agreement          — URLs contra referencias humanas: coincidencia de una URL corregida
                     o F1 del conjunto en fixtures confirmados, incluyendo casos vacíos.
"""

import re
from typing import Any, Optional

# Campos que el matcher realmente usa para puntuar. Medir "completeness" sobre los 15
# campos del schema diluiría la señal: que falte experience_max_years no cambia nada.
_CAMPOS_QUE_IMPORTAN = ("title", "company", "source_url", "salary_min")

_URL_RE = re.compile(r"https?://[^\s)]+")


def urls_presentes(texto: str) -> set:
    """URLs que aparecen literalmente en el texto, normalizadas por barra final."""
    return {u.rstrip("/") for u in _URL_RE.findall(texto or "")}


def _campo(job: Any, nombre: str):
    if isinstance(job, dict):
        return job.get(nombre)
    return getattr(job, nombre, None)


def esta_grounded(job: Any, presentes: set) -> bool:
    """
    Un job sobrevive si su URL está en la fuente. Mismo criterio que
    scraper._ground_and_repair — se replica la normalización, no la lógica de reparación.
    """
    url = _campo(job, "source_url")
    if not url:
        return False
    return str(url).rstrip("/") in presentes


def completitud(job: Any) -> float:
    """Fracción de campos relevantes con valor. 1.0 = todos presentes."""
    llenos = sum(1 for c in _CAMPOS_QUE_IMPORTAN
                 if _campo(job, c) not in (None, "", []))
    return llenos / len(_CAMPOS_QUE_IMPORTAN)


def puntuar_caso(
    *,
    jobs: list,
    texto_fuente: str,
    jobs_esperados: Optional[int] = None,
    url_esperada: Optional[str] = None,
    expected_jobs: Optional[list[dict]] = None,
) -> dict:
    """
    Puntúa una extracción contra su fuente.

    `jobs_esperados` y `url_esperada` son opcionales porque la mayoría del corpus no los
    tiene: un snapshot capturado en producción trae el input y el output, no la respuesta
    correcta. Las métricas autoverificables (grounding, completitud) funcionan igual, y
    esa es la razón por la que el corpus puede crecer solo.
    """
    presentes = urls_presentes(texto_fuente)
    total = len(jobs)
    grounded = sum(1 for j in jobs if esta_grounded(j, presentes))
    hallucinated = total - grounded

    completos = (sum(completitud(j) for j in jobs) / total) if total else 0.0

    acuerdo = None
    if expected_jobs is not None:
        expected_urls = {str(j.get("source_url") or "").rstrip("/") for j in expected_jobs}
        expected_urls.discard("")
        actual_urls = {str(_campo(j, "source_url") or "").rstrip("/") for j in jobs}
        actual_urls.discard("")
        if not expected_jobs:
            acuerdo = 1.0 if not jobs else 0.0
        elif expected_urls:
            acuerdo = 2 * len(expected_urls & actual_urls) / (len(expected_urls) + len(actual_urls))
        jobs_esperados = len(expected_jobs)
    elif url_esperada is not None:
        objetivo = str(url_esperada).rstrip("/")
        acuerdo = 1.0 if any(
            str(_campo(j, "source_url") or "").rstrip("/") == objetivo for j in jobs
        ) else 0.0

    return {
        "jobs_expected": jobs_esperados,
        "jobs_extracted": total,
        "grounded": grounded,
        "hallucinated": hallucinated,
        "fields_complete": round(completos, 4),
        "agreement": acuerdo,
    }


def agregar(filas: list[dict]) -> dict:
    """
    Agrega las filas de una corrida de eval.

    Las tasas se calculan sobre TOTALES, no como promedio de tasas por caso: un caso con
    1 job y otro con 40 no pesan igual, y promediar sus tasas le daría al chico la misma
    influencia que al grande.
    """
    extraidos = sum(f.get("jobs_extracted") or 0 for f in filas)
    grounded = sum(f.get("grounded") or 0 for f in filas)
    hallucinated = sum(f.get("hallucinated") or 0 for f in filas)
    con_acuerdo = [f for f in filas if f.get("agreement") is not None]
    con_jobs = [f for f in filas if (f.get("jobs_extracted") or 0) > 0]

    return {
        "casos": len(filas),
        "jobs_extraidos": extraidos,
        "grounding_rate": round(grounded / extraidos, 4) if extraidos else None,
        "hallucination_rate": round(hallucinated / extraidos, 4) if extraidos else None,
        "fields_complete": round(
            sum(f.get("fields_complete") or 0 for f in con_jobs) / len(con_jobs), 4
        ) if con_jobs else None,
        "agreement": round(
            sum(f["agreement"] for f in con_acuerdo) / len(con_acuerdo), 4
        ) if con_acuerdo else None,
        "casos_con_error": sum(1 for f in filas if f.get("error")),
    }


def comparar(actual: dict, baseline: dict, *, tolerancia: float = 0.02) -> list[str]:
    """
    Devuelve las regresiones. Lista vacía = no empeoró.

    La tolerancia existe porque los modelos no son determinísticos: dos corridas del mismo
    corpus con el mismo prompt difieren un poco. Un gate sin margen falla por ruido, y un
    gate que falla por ruido se termina desactivando — que es peor que no tenerlo.

    Solo se reporta lo que EMPEORA. Una mejora no es una regresión y no debe bloquear.
    """
    problemas = []

    def _peor(clave, mayor_es_mejor=True):
        a, b = actual.get(clave), baseline.get(clave)
        if a is None or b is None:
            return
        if mayor_es_mejor and a < b - tolerancia:
            problemas.append(f"{clave}: {b:.4f} -> {a:.4f} (cayo {b - a:.4f})")
        if not mayor_es_mejor and a > b + tolerancia:
            problemas.append(f"{clave}: {b:.4f} -> {a:.4f} (subio {a - b:.4f})")

    _peor("grounding_rate", mayor_es_mejor=True)
    _peor("hallucination_rate", mayor_es_mejor=False)
    _peor("fields_complete", mayor_es_mejor=True)
    _peor("agreement", mayor_es_mejor=True)

    if (actual.get("casos_con_error") or 0) > (baseline.get("casos_con_error") or 0):
        problemas.append(
            f"casos_con_error: {baseline.get('casos_con_error')} -> "
            f"{actual.get('casos_con_error')}"
        )
    return problemas
