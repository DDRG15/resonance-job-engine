"""
tests/test_nodes_are_traced.py — el decorador solo protege lo que envuelve.

Hallazgo de la auditoría de Fase 4: `traced_node` existía, estaba documentado como la
pieza que impide que una excepción de una rama mate el grafo entero, y tenía **cero call
sites**. Doce nodos sin decorar y una garantía que no protegía nada.

Este test falla si un nodo nuevo entra al paquete sin traza — que es exactamente como
volvería esa regresión, en silencio y sin que ningún otro test lo note.
"""

import inspect

import pytest

from graph import extract_graph, llm_graph, run_graph

# `exhausted` lanza ProvidersExhausted por contrato cuando on_exhaustion="raise".
# Decorarlo registraría esa excepción esperada como node_failed_total y contaminaría
# la métrica que sirve para detectar fallos REALES.
EXENTOS = {"exhausted"}


def _nodos(modulo):
    """Funciones publicas del modulo cuyo primer parametro es `state` — o sea, nodos."""
    encontrados = {}
    for nombre, fn in vars(modulo).items():
        if not inspect.isfunction(fn) or nombre.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        if params and params[0] == "state":
            encontrados[nombre] = fn
    return encontrados


def test_hay_nodos_que_verificar():
    # Guard del guard: si la deteccion se rompe, este test pasaria vacio y no
    # protegeria nada.
    assert len(_nodos(llm_graph)) >= 7
    assert len(_nodos(extract_graph)) >= 5
    assert len(_nodos(run_graph)) >= 10


MODULOS = [llm_graph, extract_graph, run_graph]
IDS = ["llm_graph", "extract_graph", "run_graph"]


@pytest.mark.parametrize("modulo", MODULOS, ids=IDS)
def test_todos_los_nodos_estan_decorados(modulo):
    sin_traza = [
        nombre for nombre, fn in _nodos(modulo).items()
        if nombre not in EXENTOS and getattr(fn, "__wrapped__", None) is None
    ]
    assert not sin_traza, (
        f"nodos sin @traced_node en {modulo.__name__}: {sin_traza}. "
        f"Un nodo sin decorar no escribe traza y, en el fan-out, se lleva la corrida "
        f"entera cuando falla."
    )


@pytest.mark.parametrize("modulo", MODULOS, ids=IDS)
def test_el_decorador_no_rompe_la_firma_que_langgraph_inspecciona(modulo):
    """
    LangGraph inyecta la config del runnable mirando la FIRMA del nodo. functools.wraps
    deja `__wrapped__`, y inspect.signature lo sigue — por eso sigue viendo (state,
    config). Si alguien cambia el decorador y rompe esto, los nodos pasan a recibir un
    solo argumento y mueren con TypeError en ejecucion, no en import: parece un bug del
    grafo y no del decorador. Costo la ultima vez: 24 fallos identicos sin causa visible.
    """
    for nombre, fn in _nodos(modulo).items():
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["state", "config"], (
            f"{modulo.__name__}.{nombre} expone {params} — LangGraph no va a inyectar "
            f"la config"
        )


def test_los_nodos_del_fanout_de_extraccion_degradan():
    """
    El subgrafo de extraccion corre sobre un board que YA drifteo. Si un paso revienta,
    ese board queda sin recuperar y los otros terminan. Sin degrade, LangGraph propaga
    y se lleva la corrida — estrictamente peor que el asyncio.gather(return_exceptions=
    True) que usaba v2.
    """
    for nombre in ("extract", "map_to_jobs", "ground", "salvage", "snapshot"):
        fn = getattr(extract_graph, nombre)
        cerradas = (fn.__closure__ or ())
        degrades = [c.cell_contents for c in cerradas if callable(c.cell_contents)]
        assert degrades, f"{nombre} no tiene degrade — un fallo suyo tumba la corrida"


def test_los_nodos_de_la_cadena_llm_no_degradan():
    """
    Al reves: la cadena de proveedores es una secuencia, no un fan-out. No hay ramas
    hermanas que salvar, y tragarse un fallo dejaria al grafo avanzando con estado
    incompleto hasta morir mas lejos, con un traceback que no señala la causa.
    """
    for nombre in ("select_provider", "call_provider", "parse", "self_correct",
                   "next_provider", "cooldown", "auth_failed"):
        fn = getattr(llm_graph, nombre)
        cerradas = (fn.__closure__ or ())
        degrades = [c.cell_contents for c in cerradas if callable(c.cell_contents)]
        # La unica callable capturada debe ser la funcion original envuelta, no un degrade.
        assert len(degrades) <= 1, (
            f"{nombre} parece tener un degrade — en una secuencia, tragarse un fallo "
            f"deja al grafo avanzando con estado incompleto"
        )


def test_los_nodos_de_la_corrida_que_degradan_son_solo_los_posteriores_al_scrape():
    """
    `notify` y `push` corren DESPUES de que el scrape guardo todo: un Discord o un Notion
    caidos no pueden volver fallida una corrida con resultados. Todos los demas nodos NO
    degradan: seguir con una lista vacia produciria una corrida «exitosa» con cero avisos.
    """
    degradan = set()
    for nombre, fn in _nodos(run_graph).items():
        cerradas = (fn.__closure__ or ())
        if len([c for c in cerradas if callable(c.cell_contents)]) > 1:
            degradan.add(nombre)
    assert degradan == {"notify", "push"}


def test_cada_nodo_del_grafo_de_la_corrida_esta_en_la_secuencia_declarada():
    """Un nodo nuevo en run_graph.py que no entre a NODOS nunca se ejecutaria."""
    declarados = {nombre for nombre, _ in run_graph.NODOS}
    definidos = {fn.__wrapped__.__name__ for fn in _nodos(run_graph).values()}
    assert declarados == definidos


def test_la_recuperacion_de_extract_graph_corre_sus_cinco_pasos_en_orden():
    """Son funciones planas, pero la secuencia sigue siendo la misma de siempre."""
    assert [paso.__wrapped__.__name__ for paso in extract_graph.PASOS] == [
        "extract", "map_to_jobs", "ground", "salvage", "snapshot",
    ]
