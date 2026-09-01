# Arquitectura — GEMA v3

> **ESQUELETO (Fase 1).** Se completa en la Fase 9, cuando el grafo existe y se documenta
> lo construido, no lo planeado. El plan vigente es
> []().

El pipeline es un grafo de estado. El flujo se lee del grafo, no de 1966 líneas de
`scraper.py`.

## Secciones a completar

**El grafo del run, nodo por nodo.** Entrada, salida y slice de estado de cada uno, desde
`init_run` hasta `push`. Diagrama incluido.

**El subgrafo de proveedores.** Los siete proveedores, el orden de la cadena, y la matriz
de routing: error de schema contra error de red contra 429. Una sola vez, que es el punto.

**El subgrafo de extracción.** El gate de grounding y por qué es tan estricto: un job
recuperado sobrevive solo si su URL aparece literal en el texto que se le mandó al modelo.

**Qué vive fuera del state y por qué.** La sección que va a buscar cualquiera que abra
`graph/` y no entienda por qué el `BrowserContext`, el writer task, `_session_seen`, el
circuit breaker y el semáforo no son canales del grafo. Dos razones distintas: lo no
serializable con lifetime de context manager, y lo que una rama tiene que releer a mitad
de superstep. Los reducers corren cuando el superstep cierra — demasiado tarde para un set
de dedup.

**Lo que LangGraph no hace.** `Send` despacha todas las ramas de una y no acota
concurrencia; el semáforo que limita tabs de Chromium abiertos sigue siendo de asyncio,
adentro de los nodos. El grafo describe el flujo; asyncio hace el trabajo. El paralelismo
que se ve en las trazas es nominal, y por eso `node_traces` separa `queue_wait_ms` de
`exec_ms`.

## Condiciones de borde documentadas

**Sin checkpointing.** El `RuntimeContext` no es serializable. Un run "resumido" con un
`BrowserContext` muerto es peor que volver a correrlo. Resolución si alguna vez se
necesita resumibilidad: hacer todo el state serializable y mover los recursos a un
handle reconstruible. Es otro proyecto, no un flag.
