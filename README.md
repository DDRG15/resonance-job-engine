# GEMA

Buscador de trabajo remoto que scrapea 10 job boards, puntúa cada aviso contra tu CV y te
deja los que valen la pena en cuatro tiers.

El motor principal **no usa LLM**: los boards se scrapean con Playwright y selectores CSS,
a 0 tokens. El LLM entra en tres lugares acotados —parsear tu búsqueda, parsear tu CV, y
recuperar un board cuando sus selectores se rompen— y ninguno es el camino normal. Si se
caen los siete proveedores configurados, GEMA sigue scrapeando.

---

## Arrancar

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate en Unix
pip install -r requirements.txt
playwright install chromium

cd frontend && npm ci && npm run build && cd ..   # sin esto, / devuelve 404

cp .env.example .env              # y cargar al menos una API key (ver abajo)

uvicorn api:app --port 8000 --workers 1
```

**El build del front no es opcional.** `frontend/dist/` está gitignoreado —los
artefactos de build no van al repo— y `api.py` monta el estático solo si el directorio
existe. Sin ese paso la API responde en los 29 endpoints y `/` devuelve 404. El servidor
lo avisa al arrancar, y también avisa si el bundle quedó más viejo que `frontend/src`:
entre el 04-09 y el 19-09 sirvió un build al que le faltaba la Fase 7 entera, sin fallar
una sola vez.

Abrir `http://localhost:8000`.

**`--workers 1` no es opcional.** El scheduler es un singleton por proceso: con dos workers
tenés dos crons disparando sobre la misma base.

### Las API keys

GEMA acepta siete proveedores LLM, todos de free tier, y **ninguno es obligatorio**. Con
cero keys el scraping funciona igual; lo que no vas a poder es parsear un prompt nuevo ni
recuperar un board drifteado.

```
GROQ_API_KEY=          GEMINI_API_KEY=       OPENROUTER_API_KEY=
COHERE_API_KEY=        CEREBRAS_API_KEY=     SAMBANOVA_API_KEY=
MISTRAL_API_KEY=
```

Cada tarea arranca en un proveedor distinto para repartir el consumo, y las siete quedan
como respaldo detrás de cada una. Detalle en [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md).

Después de cargar o renovar una key:

```bash
python tools/check_provider_models.py
```

Distingue una credencial vencida de un modelo dado de baja — dos fallos que desde adentro
se ven idénticos y piden arreglos opuestos.

---

## Cómo se usa

1. **Escribís qué buscás**, en lenguaje natural: *"Backend remoto con Python y FastAPI,
   mínimo 90k, sin contratos ni híbrido"*. Un LLM lo convierte en un `SearchConfig`
   validado, y un **segundo proveedor lo audita** — uno no puede validar su propia salida.
2. **Opcionalmente cargás tu CV.** Se convierte en un perfil efímero (skills, región,
   señales de dominio) que se usa para puntuar. El CV crudo nunca se persiste.
3. **Corrés el scrape.** 10 boards en paralelo, con circuit breaker por dominio, dedup
   contra el registro histórico y cookie jar para el anti-bot.
4. **Mirás los resultados** en cuatro tiers, o los exportás a Excel.

Los que tienen título y empresa reales pero URL rota van a una **pila de revisión manual**
en vez de descartarse. Nunca se tira data recuperable.

---

## Arquitectura

```
POST /api/parse   → subgrafo de proveedores → SearchConfig validado + auditoría cruzada
POST /api/scrape  → grafo del run:
                    init_run → scrape → bucket → summarize → persist_run → notify → push
GET  /api/scrape/stream   SSE con los logs en vivo
GET  /api/results         los cuatro tiers
GET  /api/export.xlsx     descarga
```

El pipeline es un grafo de estado (LangGraph). Los tres disparadores —API, cron y CLI—
pasan por el **mismo** grafo con distinto `trigger`.

Lectura completa: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), que incluye la sección que
más se pregunta — *qué vive fuera del estado del grafo y por qué*.

---

## Ver qué pasó en una corrida

Todo queda en SQLite, local, sin servicios externos:

```sql
SELECT run_id, trigger, status, tier1_count, tier4_count FROM runs
ORDER BY started_at DESC LIMIT 5;

SELECT provider, COUNT(*), ROUND(AVG(latency_ms)), SUM(prompt_tokens)
FROM llm_calls WHERE run_id = ? GROUP BY provider;
```

Y el digest del log:

```bash
uvicorn api:app --port 8000 --workers 1 > run.log 2>&1
python tools/watchdog.py run.log
```

Detalle en [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

---

## Estructura

```
api.py                 FastAPI — endpoints y estado de sesión
scraper.py             el motor: Playwright, breaker, cookie jar, fan-out
matcher.py             puntuación y tiers
nlp_engine.py          los 7 proveedores LLM y sus callers
database.py            SQLite: registro, vault, revisión manual, telemetría
run_manager.py         ciclo de vida de una corrida manual
scheduler_service.py   cron
run_scrape_now.py      corrida one-shot por CLI — el mismo grafo, trigger="cli"
selectors_registry.py  los 17 boards (10 activos) y sus selectores

graph/                 el pipeline como grafo de estado
  run_graph.py           la corrida completa
  llm_graph.py           la cadena de proveedores
  extract_graph.py       recuperación de un board drifteado
  state.py               los TypedDict y reducers
  runtime.py             lo que no puede vivir en el estado
  instrumentation.py     @traced_node y LLMCallResult
  eval_scoring.py        las métricas de calidad de extracción

tools/
  watchdog.py            analiza run.log y clasifica por severidad
  check_provider_models.py   verifica keys y pines de modelo
  eval_extraction.py     el gate de calidad de extracción

frontend/              React + Vite (se sirve desde /)
docs/                  arquitectura, observabilidad, eval, plan y auditorías
```

---

## Tests

```bash
python -m pytest -q                    # ~1000 tests
python -m pytest tests/test_matcher_full.py -v
```

La suite **no sale a internet**: hay un fixture autouse que bloquea cualquier conexión que
no sea a localhost. Un test que depende de la latencia de un tercero mide el clima, no el
código.

El gate de calidad de extracción corre aparte, porque sí pega a proveedores reales:

```bash
python tools/eval_extraction.py --gate
```

---

## Estado y límites conocidos

**10 de 17 boards activos.** Los otros 7 están desactivados por anti-bot o selectores
stale. Recuperarlos es el trabajo abierto más grande del proyecto.

**El fan-out por board no está descompuesto en ramas del grafo.** Vive dentro de
`GemaScraper.run()` con `asyncio.gather`, que funciona y está cubierto. Consecuencia:
`node_traces` guarda siete filas por corrida, no una por (dominio × título).

**El corpus de evaluación se cura a mano.** El harness está listo y el corpus de drift
crece solo, pero los casos anotados requieren mirar el board y decidir qué debería haber
salido.

Lista completa en []().
