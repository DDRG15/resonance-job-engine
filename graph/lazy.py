"""
lazy.py — compila un grafo la primera vez que se usa, no al importar el módulo.

Escenario: `import langgraph` cuesta ~2 s en una máquina cargada y ~5 s en frío, y los
módulos del grafo se importan al arrancar la API. Un proceso que solo sirve la UI, o que
todavía no recibió ninguna corrida, pagaba ese tiempo para nada.

Consecuencia: los módulos de `graph/` no importan langgraph arriba. El import vive en la
función `build_*` que cada uno entrega a `LazyGraph`, y esa función corre una sola vez, la
primera vez que alguien invoca el grafo.

Decisión: la compilación única va protegida por un Lock con doble chequeo. El cron y la API
pueden pedir el primer uso desde hilos distintos; sin el Lock se compilarían dos grafos y
uno quedaría descartado a medias.
"""

from threading import Lock
from typing import Any, Callable


class LazyGraph:
    """Proxy del grafo compilado: delega todo atributo (`invoke`, `stream`, …) en él."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._graph = None
        self._lock = Lock()

    @property
    def compilado(self) -> bool:
        """True si el grafo ya se construyó. No lo construye."""
        return self._graph is not None

    def _get(self):
        if self._graph is None:
            with self._lock:
                if self._graph is None:
                    self._graph = self._factory()
        return self._graph

    def __getattr__(self, name):
        # Solo se llama para atributos que NO están en la instancia; evita recursión si
        # alguien accede a `_graph` antes de que __init__ lo defina (copy/pickle).
        if name.startswith("__") or name in ("_factory", "_graph", "_lock"):
            raise AttributeError(name)
        return getattr(self._get(), name)
