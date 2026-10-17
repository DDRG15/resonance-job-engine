"""
La descripcion llega desde la fuente y el match la lee.

Lo que fija este archivo:
- la descripcion se guarda como texto con saltos de linea (los titulos de seccion de
  requisitos.py dependen de ellos), con tope y sin HTML;
- cada fuente con descripcion la mapea (fixtures reales de las Fases 1 y 2; Lever con la
  estructura verificada contra spotify el 2026-10-03);
- el eje "requisitos" de score_job: un aviso cuya descripcion pide Python y FastAPI suma
  por eso aunque el titulo no los nombre, y sin descripcion el
  puntaje es el mismo que sin la fase.
"""

import json
from pathlib import Path

import pytest

import ats_registry
import endpoint_registry
import endpoint_scraper as es
import matcher
from models import DESCRIPCION_MAX_CHARS, JobResult, SearchConfig

FIXTURES = Path(__file__).parent / "fixtures" / "fuentes"
PERFIL = {"core_skills": ["Python", "FastAPI", "PostgreSQL", "Docker"]}
CFG = SearchConfig(target_titles=["backend engineer"])


class TestTextoConLineas:
    def test_cada_bloque_en_su_linea(self):
        html = ("<h3>Requirements</h3><ul><li>Python</li><li>FastAPI</li></ul>"
                "<p>Nice to have</p><ul><li>Kafka</li></ul>")
        assert es.texto_con_lineas(html) == "Requirements\nPython\nFastAPI\nNice to have\nKafka"

    def test_las_inline_no_cortan(self):
        assert es.texto_con_lineas("<p>Use <strong>Py</strong>thon daily</p>") == "Use Python daily"

    def test_doble_escapado_y_br(self):
        assert es.texto_con_lineas("&amp;lt;p&amp;gt;A&lt;br/&gt;B") == "A\nB"

    def test_vacio(self):
        assert es.texto_con_lineas(None) == ""
        assert es.texto_con_lineas("<div> </div>") == ""

    def test_etiqueta_cortada_al_final_no_queda_como_texto(self):
        assert es.texto_con_lineas('<p>Python</p><p class="fig') == "Python"


class TestDescripcionDeLaFuente:
    @pytest.mark.parametrize("archivo,dominio,clave", [
        ("himalayas_search_pais.json", "himalayas.app", "jobs"),
        ("remotive.json", "remotive.com", "jobs"),
        ("jobicy.json", "jobicy.com", "jobs"),
        ("arbeitnow.json", "arbeitnow.com", "data"),
    ])
    def test_fixtures_reales_traen_descripcion(self, archivo, dominio, clave):
        datos = json.loads((FIXTURES / archivo).read_text(encoding="utf-8"))
        fm = endpoint_registry.ENDPOINTS[dominio]["field_map"]
        jobs = [j for j in (es.a_job_result(f, fm, dominio) for f in datos[clave]) if j]
        # Las fixtures guardan los primeros 600 caracteres del campo: en Remotive dos
        # filas se cortan dentro del `style` de la primera etiqueta y no tienen texto
        # visible. Esas quedan en None, nunca en "" ni con HTML a medias.
        con = [j for j in jobs if j.descripcion is not None]
        assert len(con) >= len(jobs) * 0.8
        assert all(j.descripcion.strip() and "<" not in j.descripcion for j in con)

    def test_lever_une_intro_secciones_y_cierre(self):
        # Estructura verificada contra api.lever.co/v0/postings/spotify el 2026-10-03.
        fila = {
            "text": "Backend Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/1",
            "categories": {"location": "Remote"},
            "description": "<div>We build audio.</div>",
            "lists": [
                {"text": "What You'll Need", "content": "<li>Python</li><li>PostgreSQL</li>"},
                {"text": "Nice to have", "content": "<li>Kafka</li>"},
            ],
            "additional": "<div>Benefits apply.</div>",
        }
        fm = ats_registry.PLATAFORMAS["lever"].field_map
        job = es.a_job_result(fila, fm, "jobs.lever.co")
        assert job.descripcion == ("We build audio.\nWhat You'll Need\nPython\nPostgreSQL\n"
                                   "Nice to have\nKafka\nBenefits apply.")

    def test_sin_campo_de_descripcion_queda_none(self):
        job = es.a_job_result({"t": "Dev", "u": "https://x.com/1"}, {"title": "t", "url": "u"},
                              "x.com")
        assert job.descripcion is None


class TestTope:
    def test_recorta_en_un_salto_de_linea(self):
        linea = "x" * 99 + "\n"
        job = JobResult(title="t", company="c", url="https://x.com/1", source_domain="x",
                        descripcion=linea * 200)          # 20.000 caracteres
        assert len(job.descripcion) <= DESCRIPCION_MAX_CHARS
        assert job.descripcion.endswith("x")              # no corta una linea a la mitad

    def test_vacia_es_none(self):
        job = JobResult(title="t", company="c", url="https://x.com/1", source_domain="x",
                        descripcion="   ")
        assert job.descripcion is None


def _job(descripcion=None, titulo="Software Engineer", url="https://x.com/1"):
    return JobResult(title=titulo, company="Acme", url=url,
                     source_domain="himalayas.app", location_raw="Worldwide",
                     descripcion=descripcion)


class TestEjeRequisitos:
    def test_la_descripcion_suma_aunque_el_titulo_no_nombre_las_skills(self):
        # Antes el matcher leia solo titulo + empresa + salario.
        sin = matcher.score_job(_job(), CFG, PERFIL)
        con = matcher.score_job(_job("Requirements\nPython\nFastAPI\nPostgreSQL"), CFG, PERFIL)
        assert con.cobertura_requisitos == pytest.approx(1.0)
        assert con.match_score > sin.match_score

    def test_sin_descripcion_el_puntaje_no_cambia(self):
        tj = matcher.score_job(_job(), CFG, PERFIL)
        assert tj.cobertura_requisitos is None and tj.requisitos == []
        # Mismo puntaje que con una descripcion sin ningun requisito reconocible.
        assert tj.match_score == matcher.score_job(_job("We are a friendly team."),
                                                   CFG, PERFIL).match_score

    def test_sin_cv_no_hay_eje_ni_lista(self):
        tj = matcher.score_job(_job("Python, FastAPI and PostgreSQL required."), CFG, None)
        assert tj.cobertura_requisitos is None and tj.requisitos == []

    def test_cobertura_baja_resta(self):
        sin = matcher.score_job(_job(), CFG, PERFIL)
        baja = matcher.score_job(
            _job("Requirements\nJava\nSpring Boot\nKotlin\nOracle database"), CFG, PERFIL)
        assert baja.cobertura_requisitos == pytest.approx(0.0)
        assert baja.match_score < sin.match_score
        assert any(r.startswith("Requisitos sin cubrir: Java") for r in baja.miss_reasons)

    def test_razones_y_lista_serializada(self):
        tj = matcher.score_job(
            _job("Requirements\nPython\nFastAPI\nPostgreSQL\nNice to have\nKafka"), CFG, PERFIL)
        assert any(r.startswith("Requisitos cubiertos: 3 de 4") for r in tj.match_reasons)
        dump = tj.model_dump(mode="json")
        assert dump["cobertura_requisitos"] == pytest.approx(tj.cobertura_requisitos)
        assert {"requisito": "Kafka", "estado": "missing", "peso": "deseable",
                "evidencia": "Kafka"} in dump["requisitos"]

    def test_el_registry_guarda_y_devuelve_la_descripcion(self, tmp_db):
        # Sin esto, tools/repuntuar_registry.py re-puntuaria sin el eje y el registry
        # diria otra cosa que la corrida en vivo sobre el mismo aviso.
        con = _job("Python, FastAPI and PostgreSQL required.", url="https://x.com/a")
        sin = _job(url="https://x.com/b")
        tmp_db.mark_seen_batch([con, sin])
        filas = {f["url"]: f for f in tmp_db.filas_fuera_de_version("v-que-no-existe")}
        assert filas["https://x.com/a"]["descripcion"] == con.descripcion
        assert filas["https://x.com/b"]["descripcion"] is None

    def test_el_backfill_completa_y_nunca_pisa_con_null(self, tmp_db):
        url = "https://x.com/c"
        tmp_db.mark_seen_batch([_job(url=url)])                 # visto antes de la fase
        tj = matcher.score_job(_job("Python required.", url=url), CFG, PERFIL)
        tmp_db.update_tiers_batch([(url, tj.tier, tj.match_score, None, tj)])
        tj_sin = matcher.score_job(_job(url=url), CFG, PERFIL)
        tmp_db.update_tiers_batch([(url, tj_sin.tier, tj_sin.match_score, None, tj_sin)])
        fila = next(f for f in tmp_db.filas_fuera_de_version("v-que-no-existe")
                    if f["url"] == url)
        assert fila["descripcion"] == "Python required."

    def test_bucket_emite_requisitos_evaluados(self, monkeypatch):
        emitidas = []
        monkeypatch.setattr(matcher.metrics, "emit",
                            lambda nombre, valor, **kw: emitidas.append((nombre, valor)))
        matcher.bucket_jobs(
            [_job("Python, FastAPI and PostgreSQL required.", titulo="Backend Engineer",
                  url="https://x.com/1"),
             _job(titulo="Backend Engineer II", url="https://x.com/2")], CFG, profile=PERFIL)
        assert ("requisitos_evaluados", 1) in emitidas
