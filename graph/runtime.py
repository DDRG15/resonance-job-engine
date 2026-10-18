"""
runtime.py — RuntimeContext: todo lo que el grafo necesita y no puede vivir en el estado.

Dos razones independientes ponen algo acá, y conviene no confundirlas.

1. NO ES SERIALIZABLE, y su ciclo de vida lo maneja otro. El BrowserContext de Playwright
   es el caso que fuerza el diseño: scraper.py crea UNO solo dentro de un
   `async with async_playwright()`, y un nodo del grafo no puede ser dueño de eso. Lo
   mismo el handle de la DB, los clientes de integraciones y la cola de logs.

2. ES ESTADO MUTABLE QUE LAS RAMAS RELEEN A MITAD DE SUPERSTEP. Los reducers corren
   cuando el superstep cierra, lo cual llega tarde para un set de dedup o un circuit
   breaker: `_bounded` chequea `is_open` antes y después del semáforo justamente porque
   una rama hermana puede haberlo abierto en el medio. Es seguro por una razón concreta,
   no por suerte — asyncio es single-threaded y el chequear-y-mutar no tiene `await` en
   el medio.

Se inyecta por `config["configurable"]`, que es el canal explícito de LangGraph para esto.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


def _provider_cooldowns() -> dict:
    """
    El mapa de cooldowns que usan de verdad los callers de nlp_engine.

    Import diferido: nlp_engine importa graph.instrumentation y graph.llm_graph, así que
    importarlo arriba cerraría el ciclo.
    """
    import nlp_engine
    return nlp_engine._cooldowns


@dataclass
class RuntimeContext:
    """
    Recursos de una corrida. Se arma en el wrapper que invoca al grafo y se pasa entero;
    los nodos toman lo que necesitan.

    Los campos son opcionales a propósito: un test que ejercita el nodo de bucket no
    debería tener que fabricar un cliente de Notion. Un nodo que necesita algo y no lo
    encuentra lo trata como integración deshabilitada, no como error.
    """

    # ── Persistencia y telemetría ────────────────────────────────────────────
    db: Any = None
    run_id: Optional[str] = None

    # ── Recursos de la corrida, ninguno serializable ─────────────────────────
    # El scraper de la corrida: dueño de la cola de escritura, el circuit breaker, el
    # registro de estrategias y el set de dedup. Lo crea init_run; los nodos de fuentes
    # lo toman de acá. Si una corrida se retoma en un proceso que no lo tiene, el primer
    # nodo que lo necesita lo reconstruye a partir del estado (graph/run_graph.py).
    scraper: Any = None
    # El perfil derivado del CV. Efímero: vive solo el tiempo de la corrida y nunca entra
    # al estado del grafo, para que un checkpoint no lo persista por accidente.
    profile: Optional[dict] = None

    # ── Integraciones de salida. Cada una aislada: un push caído no puede tumbar
    #    una corrida que ya scrapeó bien y ya escribió a la DB. ─────────────────
    notion: Any = None
    sheets: Any = None
    discord: Optional[Callable[[str], None]] = None

    # ── Streaming al humano (la cola que alimenta el SSE de /api/scrape/stream) ─
    log: Callable[[str], None] = lambda _msg: None

    # ── Estado compartido de la corrida que NO puede ser canal del grafo ──────
    # Los cooldowns de proveedores tienen que sobrevivir entre invocaciones del subgrafo
    # LLM dentro de una misma corrida.
    #
    # Apunta al MISMO dict que usa nlp_engine, no a uno nuevo. Un default_factory=dict
    # creaba un mapa paralelo que nadie leía: init_run limpiaba ese, los proveedores
    # seguían parkeados en el real, y un 401 mantenía muerto al proveedor 24h aunque se
    # renovara la key. Un estado con dos copias no tiene dueño, tiene dos mentiras.
    cooldowns: dict = field(default_factory=lambda: _provider_cooldowns())

    def emit(self, msg: str) -> None:
        """Log que nunca rompe: la cola puede estar llena o cerrada."""
        try:
            self.log(msg)
        except Exception:
            pass

    def notify(self, msg: str) -> None:
        """Discord, aislado. Una notificación caída no es una corrida caída."""
        if self.discord is None:
            return
        try:
            self.discord(msg)
        except Exception as exc:
            self.emit(f"[DISCORD] notificacion fallo (la corrida sigue): {exc}")
