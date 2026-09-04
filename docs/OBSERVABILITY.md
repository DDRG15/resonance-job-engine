# Observabilidad — GEMA v3

Una corrida que sacó menos jobs que la anterior tiene que poder explicarse con una query,
no leyendo código.

Antes de la v3 no se podía: `run_id`, summary y tiers vivían en memoria y morían con el
proceso, y de los 19 call sites de `metrics.emit` **ninguno era del lado LLM** — cero
latencia, cero tokens, cero profundidad de fallback.

Toda la telemetría es **local**. Sin LangSmith, sin SaaS, sin OpenTelemetry: GEMA corre en
una laptop y los prompts llevan el CV parseado del usuario.

---

## Las cuatro tablas

Todas en `gema_registry.db`, todas aditivas con el patrón `CREATE TABLE IF NOT EXISTS`.
Aditivas no es un detalle: la app previa a la migración las ignora, así que se puede
volver atrás sin migración inversa y la telemetría acumulada sobrevive.

### `runs` — una fila por corrida

```sql
run_id, started_at, completed_at, trigger, status, config_json, summary_json,
tier1_count, tier2_count, tier3_count, tier4_count,
domains_aborted, breaker_state_json, error
```

Cierra el agujero más viejo: en v2 el `run_id` y los tiers existían solo en memoria, así
que reiniciar el server borraba la historia y `/api/results` solo sabía de la última
corrida del proceso vivo.

Lleva **`tier4_count`**, que `ScrapeRunSummary` nunca cargó pese a que `bucket_jobs`
siempre devolvió cuatro tiers — el payload del SSE y el reporte de Discord lo recalculaban
cada uno por su lado.

`trigger` distingue `api` / `cron` / `cli`. Es lo que permite responder "¿el cron está
sacando menos que las corridas manuales?", que antes no se podía ni preguntar.

### `node_traces` — una fila por nodo ejecutado

```sql
run_id, node, graph, domain, title, started_at, exec_ms, queue_wait_ms, status, error
```

**`exec_ms` y `queue_wait_ms` van separados y no hay que sumarlos.** `Send` despacha todas
las ramas del fan-out a la vez y el semáforo las frena adentro del nodo, así que veinte
nodos pueden figurar "corriendo" mientras diecisiete esperan un tab de Chromium. Con un
solo número, el nodo más lento del reporte sería siempre el que más esperó, no el que más
trabajó.

### `llm_calls` — una fila por llamada a un proveedor

```sql
run_id, trace_id, purpose, provider, model, provider_index, attempt,
latency_ms, prompt_tokens, completion_tokens, outcome, error, created_at
```

**`provider_index` ES la profundidad del fallback.** Un 3 significa que tres proveedores
fallaron antes de que el cuarto respondiera — la respuesta a "¿por qué la corrida del
martes tardó el triple?".

`trace_id` agrupa las llamadas de una misma resolución: dos intentos de autocorrección
contra groq y una a gemini son tres filas del mismo trace.

`prompt_tokens` y `completion_tokens` pueden ser `NULL`, y eso significa **"el proveedor no
lo reportó"**, no "costó cero". La distinción importa: un dashboard que las confunde
reporta corridas gratis cada vez que un SDK cambia su JSON.

### `eval_results` — una fila por (corrida de eval × caso)

```sql
eval_run_id, corpus_case, source, prompt_version, provider,
jobs_expected, jobs_extracted, grounded, hallucinated,
fields_complete, agreement, error, created_at
```

Guarda el desglose y no solo el agregado: un grounding que cae de 0.95 a 0.80 no dice nada
sin saber **qué** casos se rompieron, y casi siempre son de un board.

---

## Las métricas

Todas salen por `metrics.emit` como una línea JSON con `event_type: metric`, que es lo que
`tools/watchdog.py` consume sin necesitar cambios de parser.

| Métrica | Labels | Qué mirar |
|---|---|---|
| `llm_fallback_depth` | purpose | **La más útil.** 0 = el primario contestó. ≥3 = tres proveedores fallaron |
| `llm_call_latency_ms` | provider, purpose, outcome | Qué proveedor está lento |
| `llm_tokens_total` | provider, direction | Consumo por proveedor |
| `llm_provider_cooldown_total` | provider | 429 real |
| `llm_provider_auth_failed_total` | provider | **Credencial vencida** |
| `llm_self_correction_total` | provider, purpose | El modelo tuvo que reparar su JSON |
| `node_exec_ms` | node, graph | Dónde se va el tiempo |
| `node_failed_total` | node, error_type | Un nodo se cayó y degradó |

---

## Cómo leer una corrida

```sql
-- ¿Terminó bien?
SELECT run_id, trigger, status, tier1_count, tier2_count, tier3_count, tier4_count,
       domains_aborted, error
FROM runs ORDER BY started_at DESC LIMIT 5;

-- ¿Cuánto costó, y a quién?
SELECT provider, COUNT(*) llamadas, ROUND(AVG(latency_ms)) ms_prom,
       SUM(prompt_tokens) tok_in, SUM(completion_tokens) tok_out
FROM llm_calls WHERE run_id = ? GROUP BY provider;

-- ¿Corrió degradado? (0 = el primario contestó siempre)
SELECT purpose, MAX(provider_index) profundidad_max
FROM llm_calls WHERE run_id = ? GROUP BY purpose;

-- ¿Dónde se fue el tiempo? OJO: exec_ms sin queue_wait_ms
SELECT node, COUNT(*), ROUND(AVG(exec_ms)) exec, ROUND(AVG(queue_wait_ms)) espera
FROM node_traces WHERE run_id = ? GROUP BY node ORDER BY exec DESC;

-- ¿Alguna credencial murió?
SELECT provider, COUNT(*) FROM llm_calls
WHERE outcome = 'auth_error' GROUP BY provider;
```

---

## El watchdog

```bash
python tools/watchdog.py run.log          # digest
python tools/watchdog.py run.log --watch  # en vivo
python tools/watchdog.py run.log --full   # exhaustivo
```

Levantar el server con `uvicorn api:app --port 8000 --workers 1 > run.log 2>&1` para que
todo quede capturado.

**Principio de no pérdida:** toda línea se cuenta y reconcilia. Lo que no matchea un patrón
conocido cae a `unclassified` con heurística de peligro y se **retiene** — nunca se
descarta.

Severidades:

| Nivel | Qué lo dispara |
|---|---|
| CRITICAL | `node_failed_total`, `llm_fallback_depth ≥ 3`, credenciales (401/403), tracebacks, 5xx reales |
| WARN | `llm_fallback_depth` 1–2, `llm_provider_cooldown_total`, breaker, `[NULL-SRP]` |
| INFO | latencia, tokens, tiempos de nodo, salvage, jar |

`llm_fallback_depth` es la única métrica con **umbral por valor**: un 0 es la corrida
normal —ruido puro— y un 3 es que tres proveedores fallaron. `_METRIC_SEVERITY` es un
lookup por nombre y no puede expresar eso, así que existe `_METRIC_VALUE_THRESHOLDS`.

---

## El test de la 1 AM

Cada fila de `runs` tiene que alcanzar para decidir sin abrir el código. Si `status` es
`crashed`, `error` dice qué pasó. Si los tiers vinieron en cero, `domains_aborted` y
`breaker_state_json` distinguen anti-bot de drift de cadena de proveedores agotada.

Y desde la v3, una credencial vencida **se nombra**: el log dice qué variable de `.env`
actualizar, el watchdog la marca CRITICAL, y hay una métrica por proveedor. Antes decía
`groq exhausted (network)` y mandaba a revisar la conectividad.

---

## Condiciones de borde documentadas

**Escribir telemetría nunca puede tumbar un scrape.** Todos los `record_*` son fail-safe:
loguean y siguen. Una falla registrando una falla no escala. Hay cinco tests que apuntan el
`db_path` a una ruta inválida y verifican que cada método la trague.

**El texto de error se recorta** a 500 caracteres (2000 en `runs`). Los 5xx de gateway de
los free tiers traen páginas HTML enteras en el cuerpo de la excepción, y una fila con 8 KB
de HTML vuelve ilegible justo la tabla que se consulta cuando algo anda mal.

**No hay poda automática.** Con ~40 filas de `node_traces` y ~10 de `llm_calls` por corrida
y 3 corridas semanales, son ~8k filas y ~1.5 MB al año: no lo justifica. **El disparador
para agregarla** es que el cron pase a diario o que el fan-out se descomponga en ramas
`Send` —ahí `node_traces` pasa de 7 filas por corrida a una por (dominio × título)—. El
primero que lo va a sentir es el `db.backup()` que corre al inicio de cada corrida.

**`node_traces` no tiene el detalle por board todavía.** Siete filas por corrida, porque el
fan-out sigue dentro de `GemaScraper.run()`. Quien busque "¿qué board tardó?" no lo va a
encontrar ahí: está en el log.
