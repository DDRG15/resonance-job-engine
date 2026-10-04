"""
, Fase 7 — extraccion deterministica de requisitos (requisitos.py).

Lo que fija este archivo:
- el peso requerido/deseable no cruza el fin de la oracion (el bug anotado en el
  estado del 01-10: "Kubernetes is required. Nice to have: ..." salia deseable);
- las secciones con titulo ("Nice to have", "Benefits") deciden el peso de las lineas
  que no traen señal propia;
- la evidencia es siempre una cita literal de la descripcion;
- la cobertura es None cuando no hay señal suficiente, y el LLM (D4) no puede cambiar lo
  que dijo el deterministico.
"""

import pytest

import requisitos as R

PERFIL = {"core_skills": ["Python", "FastAPI", "Docker Compose", "PostgreSQL"]}


def _por_nombre(reqs):
    return {r.requisito: r for r in reqs}


class TestPesoPorOracion:
    def test_la_señal_de_deseable_no_cruza_el_punto(self):
        reqs = _por_nombre(R.extraer(
            "Kubernetes is required. Nice to have: Kafka and Redis.", PERFIL))
        assert reqs["Kubernetes"].peso == "requerido"
        assert reqs["Kafka"].peso == "deseable"
        assert reqs["Redis"].peso == "deseable"

    def test_señal_despues_del_termino_en_la_misma_oracion(self):
        reqs = _por_nombre(R.extraer(
            "Strong Python skills. Experience with Kafka is a plus.", PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["Kafka"].peso == "deseable"

    def test_un_requerido_corta_el_alcance_de_la_señal_posterior(self):
        reqs = _por_nombre(R.extraer("Python is required, Docker is a plus.", PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["Docker"].peso == "deseable"

    def test_rotulo_en_linea_alcanza_a_toda_la_lista(self):
        reqs = R.extraer("Nice to have: Kafka, Redis, Terraform.", PERFIL)
        assert {r.requisito for r in reqs} == {"Kafka", "Redis", "Terraform"}
        assert all(r.peso == "deseable" for r in reqs)

    def test_eg_y_node_js_no_parten_la_oracion(self):
        reqs = _por_nombre(R.extraer(
            "Bonus: streaming experience (e.g. Kafka). Node.js services in production.",
            PERFIL))
        assert reqs["Kafka"].peso == "deseable"
        assert reqs["Node.js"].peso == "requerido"

    def test_not_required_es_deseable(self):
        reqs = _por_nombre(R.extraer(
            "Rust is not required but welcome. Must know Python.", PERFIL))
        assert reqs["Rust"].peso == "deseable"
        assert reqs["Python"].peso == "requerido"

    def test_no_excluyente_en_español(self):
        reqs = _por_nombre(R.extraer(
            "Experiencia con Python excluyente. Conocimiento de Kafka no excluyente.",
            PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["Kafka"].peso == "deseable"


class TestPesoPorSeccion:
    DESCRIPCION = (
        "Requirements\n"
        "Python 3+ years\n"
        "PostgreSQL in production\n"
        "Nice to have\n"
        "Kafka\n"
        "Redis\n"
        "Benefits\n"
        "We use Docker everywhere\n"
    )

    def test_la_seccion_decide_el_peso_de_las_lineas_sin_señal(self):
        reqs = _por_nombre(R.extraer(self.DESCRIPCION, PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["PostgreSQL"].peso == "requerido"
        assert reqs["Kafka"].peso == "deseable"
        assert reqs["Redis"].peso == "deseable"

    def test_un_termino_solo_en_beneficios_no_es_requisito(self):
        assert "Docker" not in _por_nombre(R.extraer(self.DESCRIPCION, PERFIL))

    def test_titulo_con_dos_puntos_y_frase_libre(self):
        reqs = _por_nombre(R.extraer(
            "What you'll need:\nPython\nIt would be great if you also have:\nTerraform\n",
            PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["Terraform"].peso == "deseable"

    def test_preferred_qualifications_es_seccion_deseable(self):
        reqs = _por_nombre(R.extraer(
            "Minimum qualifications\nPython\nPreferred qualifications\nAWS\n", PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["AWS"].peso == "deseable"

    def test_un_requerido_explicito_gana_sobre_la_seccion(self):
        reqs = _por_nombre(R.extraer(
            "Nice to have\nPython is required for this role\nRedis\n", PERFIL))
        assert reqs["Python"].peso == "requerido"
        assert reqs["Redis"].peso == "deseable"

    def test_un_item_largo_no_se_confunde_con_un_titulo(self):
        # "Bonus points if ..." con mas de cinco palabras es un item, no un titulo: su
        # peso no se extiende a la linea siguiente.
        reqs = _por_nombre(R.extraer(
            "Requirements\nBonus points if you have shipped Rust to production\nPython\n",
            PERFIL))
        assert reqs["Rust"].peso == "deseable"
        assert reqs["Python"].peso == "requerido"


class TestExtraccion:
    def test_sin_descripcion_no_hay_requisitos(self):
        assert R.extraer(None, PERFIL) == []
        assert R.extraer("   ", PERFIL) == []

    def test_estado_contra_el_cv(self):
        reqs = _por_nombre(R.extraer("We need Python, FastAPI and Kafka.", PERFIL))
        assert reqs["Python"].estado == "match"
        assert reqs["FastAPI"].estado == "match"
        assert reqs["Kafka"].estado == "missing"

    def test_skill_del_perfil_cubre_su_canonico(self):
        # "Docker Compose" en el CV cubre "Docker" en el aviso.
        reqs = _por_nombre(R.extraer("Docker and Python required.", PERFIL))
        assert reqs["Docker"].estado == "match"

    def test_skill_del_perfil_fuera_del_vocabulario_entra_como_termino(self):
        perfil = {"core_skills": ["circuit breaker", "Python"]}
        reqs = _por_nombre(R.extraer(
            "You will design a circuit breaker layer in Python.", perfil))
        assert reqs["circuit breaker"].estado == "match"

    def test_la_evidencia_es_una_cita_literal_de_la_descripcion(self):
        desc = "We build APIs. Python and PostgreSQL are required. Kafka is a plus."
        for r in R.extraer(desc, PERFIL):
            assert r.evidencia in desc

    def test_peso_de_la_aparicion_mas_exigente(self):
        reqs = _por_nombre(R.extraer(
            "Bonus if you know AWS. Requirements: Python, AWS.", PERFIL))
        assert reqs["AWS"].peso == "requerido"
        assert reqs["AWS"].evidencia == "Requirements: Python, AWS"

    def test_el_nombre_de_la_empresa_no_es_requisito(self):
        reqs = _por_nombre(R.extraer(
            "At Grafana Labs we build Grafana. Golang, Python and Prometheus required.",
            PERFIL, empresa="Grafana Labs"))
        assert "Grafana" not in reqs
        assert {"Golang", "Python", "Prometheus"} <= set(reqs)

    @pytest.mark.parametrize("texto,esperado", [
        ("You will react to incidents quickly.", False),
        ("Our frontend is built with React and TypeScript.", True),
        ("Experience with ReactJS.", True),
    ])
    def test_react_el_verbo_no_cuenta(self, texto, esperado):
        assert ("React" in _por_nombre(R.extraer(texto, PERFIL))) is esperado

    def test_go_el_verbo_no_cuenta_golang_si(self):
        reqs = _por_nombre(R.extraer("Ready to go? We write Golang services.", PERFIL))
        assert "Golang" in reqs
        assert len(reqs) == 1

    @pytest.mark.parametrize("texto,termino", [
        ("We use PostgreSQL.", "SQL"),     # SQL no se cuela dentro de PostgreSQL
        ("Push to GitHub daily.", "Git"),  # Git no se cuela dentro de GitHub
        ("Build trust with users.", "Rust"),
    ])
    def test_bordes_de_palabra(self, texto, termino):
        assert termino not in _por_nombre(R.extraer(texto, PERFIL))

    def test_deterministico(self):
        desc = TestPesoPorSeccion.DESCRIPCION
        assert R.extraer(desc, PERFIL) == R.extraer(desc, PERFIL)

    def test_orden_requeridos_primero_y_por_aparicion(self):
        reqs = R.extraer("Kafka is a plus. Python required. AWS required.", PERFIL)
        assert [r.requisito for r in reqs] == ["Python", "AWS", "Kafka"]


class TestCobertura:
    def test_sin_perfil_no_hay_cobertura(self):
        reqs = R.extraer("Python, AWS and Kafka required.", None)
        assert len(reqs) == 3
        assert R.cobertura(reqs, None) is None
        assert R.cobertura(reqs, {"core_skills": []}) is None

    def test_menos_de_tres_requisitos_no_es_señal(self):
        reqs = R.extraer("Python and AWS required.", PERFIL)
        assert R.cobertura(reqs, PERFIL) is None

    def test_ponderada_deseable_pesa_la_mitad(self):
        # requeridos: Python (match), AWS (missing); deseables: PostgreSQL (match),
        # Kafka (missing) -> (1 + 0.5) / (2 + 1) = 0.5
        reqs = R.extraer(
            "Python and AWS required. Nice to have: PostgreSQL and Kafka.", PERFIL)
        assert R.cobertura(reqs, PERFIL) == pytest.approx(0.5)

    def test_cobertura_completa(self):
        reqs = R.extraer("Python, FastAPI and PostgreSQL required.", PERFIL)
        assert R.cobertura(reqs, PERFIL) == pytest.approx(1.0)


class TestCapaLLM:
    DESC = "Python and AWS required. You will mentor junior engineers."

    def test_el_llm_no_cambia_lo_deterministico(self):
        det = R.extraer(self.DESC, PERFIL)
        fusion = R.fusionar_propuestas_llm(det, [
            {"requisito": "Python", "evidencia": "You will mentor junior engineers"},
        ], self.DESC)
        assert fusion == det

    def test_evidencia_inventada_se_descarta(self):
        det = R.extraer(self.DESC, PERFIL)
        fusion = R.fusionar_propuestas_llm(det, [
            {"requisito": "Liderazgo", "evidencia": "Must lead a team of 10"},
        ], self.DESC)
        assert fusion == det

    def test_propuesta_con_cita_literal_entra_como_implicito_y_no_cuenta(self):
        det = R.extraer(self.DESC + " Kafka is a plus.", PERFIL)
        fusion = R.fusionar_propuestas_llm(det, [
            {"requisito": "Mentoria", "evidencia": "You will mentor junior engineers"},
        ], self.DESC)
        nuevo = _por_nombre(fusion)["Mentoria"]
        assert nuevo.estado == "implicito"
        assert R.cobertura(fusion, PERFIL) == R.cobertura(det, PERFIL)
