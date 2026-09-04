# Arquitectura — GEMA v3

El pipeline es un grafo de estado. El flujo se lee del grafo; el trabajo lo sigue haciendo
asyncio.

---

## El motor principal no usa LLM

Antes de cualquier cosa: **los 10 boards activos se scrapean con selectores CSS y
Playwright, a 0 tokens.** El LLM aparece en tres lugares y ninguno es el camino normal:

- **`parse`** — convierte tu prompt en lenguaje natural a un `SearchConfig` validado.
- **`profile`** — convierte tu CV en un perfil efímero para puntuar skills.
- **`extract`** — recupera un board cuando sus selectores driftearon. Es un camino de
  emergencia, capado a 3 boards por corrida.

Más una auditoría cruzada del config, que es advisory y nunca bloquea.

Si mañana se caen los siete proveedores LLM, GEMA sigue scrapeando. Pierde el parseo de
prompts nuevos y la recuperación de boards rotos, no la operación.

---

## Los tres grafos

### El grafo del run — `graph/run_graph.py`

```
init_run ─> scrape ─> bucket ─> summarize ─> persist_run ─> notify ─> push ─> END
```

| Nodo | Qué hace |
|---|---|
| `init_run` | Abre la fila en `runs`, limpia los cooldowns de la corrida previa |
| `scrape` | Llama a `run_scrape_session` — el scraper completo, sin tocar |
| `bucket` | `bucket_jobs`: puntúa y reparte en cuatro tiers |
| `summarize` | Cierra el `ScrapeRunSummary` con los conteos |
| `persist_run` | Cierra la fila de `runs` con los **cuatro** tiers |
| `notify` | Reporte a Discord |
| `push` | Notion y Sheets, cada uno aislado |

**Un solo punto de entrada para los tres disparadores.** `run_manager` (API), 
`scheduler_service` (cron) y la CLI llaman a `run_pipeline` con distinto `trigger`. Antes
de la v3 eran dos orquestadores con comportamiento distinto: el cron no empujaba a Notion
ni Sheets y hardcodeaba `ttl_hours=24`, y esa diferencia no la había decidido nadie — vivía
en que una de las dos copias no tenía veinte líneas.

**Degrade selectivo, no uniforme.** Los cinco primeros nodos NO degradan: si el scrape
revienta, seguir a `bucket` con lista vacía produciría una corrida "exitosa" con cero
resultados, que es peor que un fallo visible. `notify` y `push` SÍ degradan, porque ocurren
después de que todo se guardó en la DB — un push a Notion caído no puede convertir una
corrida exitosa en fallida.

### El subgrafo de proveedores — `graph/llm_graph.py`

```
select_provider ─> call_provider ─> parse ─> END
       │                 │            ├─ schema, quedan intentos ─> self_correct ─┐
       │                 │            ├─ schema, agotados ────────> next_provider │
       │                 │            ├─ rate_limit ──────────────> cooldown       │
       │                 ├─ auth ─────────────────────────────────> auth_failed    │
       │                 └─ network ──────────────────────────────> next_provider │
       └─ cadena agotada ─> exhausted                                             │
                ▲                                                                 │
                └─────────────────────────────────────────────────────────────────┘
```

Reemplaza a **tres** copias del mismo loop que habían derivado: una autocorregía, otra no
lo hacía nunca, la tercera tenía un `except (JSONDecodeError, ValueError, Exception)` que
es un `except Exception` disfrazado. Ningún test lo detectó porque cada archivo armaba su
propio mock y probaba su copia contra sí misma.

Dos invariantes que hay que preservar textualmente:

- **`next_provider` resetea la historia.** Arrastrar el transcripto del proveedor que
  falló envenena al siguiente: abre con un turno de assistant lleno de JSON roto y un user
  quejándose, así que empieza explicando el error de otro.
- **Un error de red no se reinyecta como error de schema.** Pedirle al modelo que arregle
  un JSON cuando lo que falló fue la conexión es pedirle que alucine una solución a un
  problema que no causó.

### El subgrafo de extracción — `graph/extract_graph.py`

```
extract ─> map_to_jobs ─> ground ─> salvage ─> snapshot ─> END
```

El gate de `ground` es el que vuelve **imposible** la alucinación de URLs, no improbable:
un job recuperado sobrevive solo si su URL aparece **literalmente** en el texto que se le
mandó al modelo. Una URL fabricada —el `example.com` del schema, que el modelo copia
cuando no encuentra una real— no está en la fuente y se rechaza.

`salvage` rescata lo que el grounding tira pero es real: si el **título** sí está en la
fuente, lo que falló es la URL, no el job. Eso va a la pila de revisión manual en vez de
a la basura.

---

## Qué vive fuera del estado, y por qué

Es la pregunta que va a hacer cualquiera que abra `graph/`. Hay **dos razones distintas**
y conviene no confundirlas.

### 1. No es serializable, y su ciclo de vida lo maneja otro

El `BrowserContext` de Playwright es el caso que fuerza el diseño: `scraper.py` crea
**uno** compartido dentro de un `async with async_playwright()`, y un nodo del grafo no
puede ser dueño de eso. Lo mismo el handle de la DB, los clientes de integraciones y la
cola de logs del SSE.

El `_db_writer_task` es otro: un consumidor de cola de larga vida. LangGraph no expresa
"corré en paralelo al grafo indefinidamente", así que se arranca y se drena en el wrapper.

### 2. Es estado mutable que las ramas releen a mitad de superstep

**LangGraph aplica los reducers cuando el superstep CIERRA, no durante.** Eso llega tarde
para:

| Estado | Por qué no puede ser canal |
|---|---|
| `_session_seen` | El dedup se chequea y muta dentro de una rama, y el efecto tiene que verse **inmediatamente** en las hermanas o dos dominios extraen el mismo job |
| `_visited_srp_urls` | Igual |
| `CircuitBreaker` | `_bounded` lee `is_open` antes **y** después del semáforo, justamente porque una hermana puede haberlo abierto en el medio |
| `cooldowns` | Tienen que sobrevivir entre invocaciones del subgrafo LLM dentro de una misma corrida |

Es seguro por una razón concreta, no por suerte: **asyncio es single-threaded y el
chequear-y-mutar no tiene `await` en el medio.**

Sobre los cooldowns hay una lección cara: en la Fase 6 se declararon como
`field(default_factory=dict)`, creando un mapa **paralelo** al que usa `nlp_engine`.
`init_run` limpiaba uno que nadie leía, así que un proveedor parkeado por 401 quedaba
muerto 24 horas aunque se renovara la key. Un estado con dos copias no tiene dueño: tiene
dos mentiras. Hoy `RuntimeContext.cooldowns` apunta al mapa real.

---

## Lo que LangGraph no hace

**`Send` no acota concurrencia.** Despacha todas las ramas a la vez y no tiene límite por
fan-out. El `asyncio.Semaphore(SCRAPER_CONCURRENCY=3)` limita **tabs de Chromium
abiertos**; sin él, 20 pares dominio×título abren 20 tabs. `config={"max_concurrency": N}`
no lo sustituye: acota el ejecutor de LangGraph, no los tabs.

**Una excepción en un nodo mata el grafo entero.** `asyncio.gather(return_exceptions=True)`
—lo que usaba v2— dejaba fallar un board solo. Por eso el try/except que degrada vive
**dentro** de `@traced_node` y no en cada call site: un nodo que se olvide no es un nodo
con menos telemetría, es esa regresión de vuelta.

Consecuencia de todo esto: **el paralelismo que se ve en las trazas es nominal.** Veinte
nodos pueden figurar "corriendo" mientras diecisiete esperan un tab. Por eso `node_traces`
separa `queue_wait_ms` de `exec_ms` — sumados, el número miente sobre dónde se fue el
tiempo.

---

## Convención de nodos

```python
@traced_node("nombre", graph="run")
def nodo(state: RunState, config: RunnableConfig) -> dict:
```

Las dos mitades de la firma son obligatorias y LangGraph las verifica por inspección:

- El parámetro **tiene que llamarse `config`**. Con otro nombre el nodo se invoca con un
  argumento y muere con `TypeError` en tiempo de ejecución del grafo, no de import — o sea
  que se presenta como un bug del grafo y no como un nombre mal puesto. Costó 24 fallos
  idénticos descubrirlo.
- **Tiene que estar anotado `RunnableConfig`**, importado de `langgraph.types` y no de
  `langchain_core.runnables`. Con anotación `dict`, LangGraph avisa y no inyecta: falla
  igual pero parece configurado.

Consecuencia: dentro de un nodo, `config` es la config del runnable, **nunca** los
settings de GEMA. Los módulos de `graph/` importan `import config as app_config`.

---

## Condiciones de borde documentadas

**Sin checkpointing.** `SqliteSaver` parece dar resumibilidad gratis; no la da. El
`RuntimeContext` no es serializable, y un run "resumido" con un `BrowserContext` muerto es
peor que volver a correrlo. Resolución si alguna vez hace falta: hacer todo el estado
serializable y mover los recursos a un handle reconstruible. Es otro proyecto, no un flag.

**El fan-out por (dominio × título) sigue dentro de `GemaScraper.run()`**, con
`asyncio.gather`, no como ramas `Send`. El plan de la migración pedía descomponerlo. No se
hizo porque ese código funciona, está cubierto por ~37 tests de breaker y ~19 de fallback,
y se lo vio entregar 74 jobs con 0 errores contra boards reales. Los reducers
(`merge_counters`, `union_domains`) ya están escritos y probados para cuando ocurra.

Consecuencia medible: **`node_traces` recibe siete filas por corrida**, no una por par. El
detalle por board vive en el log y en `board_strategy.json`.
