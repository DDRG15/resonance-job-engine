"""
tests/test_import_diferido.py — langgraph entra cuando se usa, no cuando se importa la API.

Escenario: `import langgraph` cuesta ~2 s en una máquina cargada (~5 s en frío) y los módulos
del grafo se importaban al cargar `api`, `run_manager`, `scraper` y `nlp_engine`. Un proceso
que solo sirve la UI pagaba ese tiempo en cada arranque.

Consecuencia: la prueba tiene que correr en un SUBPROCESO limpio. Dentro de pytest, otros
tests ya importaron langgraph y `sys.modules` no dice nada del import en frío.
"""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _python(codigo: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", codigo], cwd=str(RAIZ), capture_output=True, text=True,
        timeout=240,
    )


class TestImportEnFrio:

    def test_import_api_no_trae_langgraph_ni_langchain(self):
        r = _python(
            "import api, sys\n"
            "pesados = [m for m in ('langgraph', 'langchain_core') if m in sys.modules]\n"
            "assert not pesados, f'import api trajo {pesados}'\n"
        )
        assert r.returncode == 0, r.stderr[-2000:]

    @pytest.mark.parametrize("modulo", [
        "graph.run_graph", "graph.llm_graph", "graph.extract_graph", "run_manager", "scraper",
    ])
    def test_ningun_modulo_del_pipeline_importa_langgraph_al_cargarse(self, modulo):
        r = _python(
            f"import {modulo}, sys\n"
            "assert 'langgraph' not in sys.modules, 'langgraph entro al importar'\n"
        )
        assert r.returncode == 0, r.stderr[-2000:]

    def test_el_grafo_se_compila_en_el_primer_uso(self):
        r = _python(
            "import sys\n"
            "import graph.run_graph as rg, graph.llm_graph as lg\n"
            "assert not rg.RUN_GRAPH.compilado and not lg.LLM_GRAPH.compilado\n"
            "assert 'langgraph' not in sys.modules\n"
            "nodos = list(rg.RUN_GRAPH.get_graph().nodes)\n"
            "assert rg.RUN_GRAPH.compilado and 'langgraph' in sys.modules\n"
            "assert not lg.LLM_GRAPH.compilado, 'compilar el run no compila el de LLM'\n"
            "assert nodos[0] == '__start__' and nodos[1] == 'init_run', nodos\n"
        )
        assert r.returncode == 0, r.stderr[-2000:]


class TestLazyGraph:

    def test_compila_una_sola_vez_aunque_lo_pidan_varios_hilos(self):
        from graph.lazy import LazyGraph

        llamadas = []
        todos_listos = threading.Barrier(8)

        class _Compilado:
            def invoke(self, x):
                return x + 1

        def _fabrica():
            llamadas.append(1)
            return _Compilado()

        lazy = LazyGraph(_fabrica)
        assert not lazy.compilado
        resultados = []

        def _usar():
            todos_listos.wait()
            resultados.append(lazy.invoke(1))

        hilos = [threading.Thread(target=_usar) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        assert len(llamadas) == 1, "dos hilos compilaron el grafo a la vez"
        assert resultados == [2] * 8
        assert lazy.compilado

    def test_un_atributo_inexistente_no_recursa(self):
        from graph.lazy import LazyGraph

        lazy = LazyGraph(lambda: object())
        with pytest.raises(AttributeError):
            lazy.__wrapped__          # lo piden inspect/copy: no debe compilar nada
        assert not lazy.compilado
