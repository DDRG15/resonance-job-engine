# Observabilidad — Resonance (motor GEMA)

Una corrida que sacó menos jobs que la anterior tiene que poder explicarse con una query,
no leyendo código.

Antes de la v3 no se podía: `run_id`, summary y tiers vivían en memoria y morían con el
proceso, y de los 19 call sites de `metrics.emit` **ninguno era del lado LLM** — cero
latencia, cero tokens, cero profundidad de fallback. Hoy la telemetría cubre además cada
fuente de avisos: una fuente que dejó de traer avisos se nombra en lugar de esconderse
detrás de un total que sigue pareciendo normal.

Toda la telemetría es **local**. Sin LangSmith, sin SaaS, sin OpenTelemetry: Resonance corre
en una laptop y los prompts llevan el CV parseado del usuario.

---

## Las cuatro tablas

Todas en `gema_registry.db`, todas aditivas con el patrón `CREATE TABLE IF NOT EXISTS`.
Aditivas no es un detalle: la app previa a la migración las ignora, así que se puede
volver atrás sin migración inversa y la telemetría acumulada sobrevive. Las columnas que se
agregaron después (`items`, `aceptados`, `output_chars`) entran con `ALTER TABLE ... ADD
COLUMN` y las filas viejas quedan en `NULL`.

Junto a ellas viven las tablas de dominio (`seen_jobs_registry`, `applications`,
`application_events`, `job_links`, `manual_review`, `search_vault`). No son telemetría y no
se podan.

### `runs` — una fila por corrida

```sql
run_id, started_at, completed_at, trigger, status, config_json, summary_json,
tier1_count, tier2_count, tier3_count,
domains_aborted, breaker_state_json, error
```

Cierra el agujero más viejo: en v2 el `run_id` y los tiers existían solo en memoria, así
que reiniciar el server borraba la historia y `/api/results` solo sabía de la última
corrida del proceso vivo.

Los tres conteos se escriben desde el mismo estado que alimenta la API y Discord.
Las bases antiguas conservan sus columnas históricas; los lectores exponen los campos
vigentes y las corridas nuevas escriben los tres conteos.

`trigger` distingue `api` / `cron` / `cli`. Es lo que permite responder "¿el cron está
sacando menos que las corridas manuales?", que antes no se podía ni preguntar.

Una corrida que quedó abierta porque el proceso murió se reconcilia al arrancar: pasa a
`interrupted` y emite `runs_interrupted_total`.

### `node_traces` — una fila por unidad que se puede romper

```sql
run_id, node, graph, domain, title, started_at, exec_ms, queue_wait_ms, status, error,
items, aceptados
```

**Qué filas escribe cada capa.** Los nodos del grafo del run (`init_run`, `scrape`,
`bucket`...) escriben una fila cada uno. Además, cada fuente de avisos escribe la suya:

| `node` | Unidad | `domain` |
|---|---|---|
| `scrape_srp` | una rama CSS: (board x título) | el board; `title` lleva el título buscado |
| `fuente_endpoint` | un board de endpoint | el board |
| `fuente_ats` | una empresa de un ATS | `plataforma:token` |
| `fuente_endpoint` | un proveedor de alertas de Gmail | `gmail:<proveedor>` |

Las ramas CSS se acumulan en memoria y se escriben en una sola transacción después del
`gather`: una escritura por rama sería un fsync por rama dentro del event loop. Con una
búsqueda de 51 títulos y 8 boards CSS, una corrida deja ~408 filas `scrape_srp`, más las de
endpoint y ATS.

**`exec_ms` y `queue_wait_ms` van separados y no hay que sumarlos.** `Send` despacha todas
las ramas del fan-out a la vez y el semáforo las frena adentro del nodo, así que veinte
nodos pueden figurar "corriendo" mientras diecisiete esperan un tab de Chromium. Con un
solo número, el nodo más lento del reporte sería siempre el que más esperó, no el que más
trabajó.

**`items` y `aceptados` cuentan avisos, no tiempo.** Hasta el 29-09 la tabla sabía cuánto
tardó cada rama y no cuánto trajo: `trueup.io` gastó 1.618 s en 51 búsquedas vacías y la
única forma de enterarse fue sumar tiempos a mano.

- `items` — avisos **crudos** que la fuente devolvió, antes de cualquier filtro propio y de
  la dedup contra el registry. **`NULL` no es cero**: significa que la rama no consultó la
  fuente (breaker abierto, query ya visitada, TTL de consulta vigente) o que la fila es
  anterior a la columna. Toda query sobre `items` tiene que distinguirlos.
- `aceptados` — lo que pasó a la dedup. En una rama CSS son los avisos **nuevos** (después
  del delta load); en endpoint y ATS son los que pasan relevancia y validez, aunque el
  registry ya los tenga. Son dos magnitudes bajo un nombre porque cada camino filtra en un
  punto distinto. La decisión de salud usa `items`.

### `llm_calls` — una fila por llamada a un proveedor

```sql
run_id, trace_id, purpose, provider, model, provider_index, attempt,
latency_ms, prompt_tokens, completion_tokens, outcome, output_chars, error, created_at
```

**`provider_index` ES la profundidad del fallback.** Un 3 significa que tres proveedores
fallaron antes de que el cuarto respondiera — la respuesta a "¿por qué la corrida del
martes tardó el triple?".

`trace_id` agrupa las llamadas de una misma resolución: dos intentos de autocorrección
contra groq y una a gemini son tres filas del mismo trace.

`prompt_tokens` y `completion_tokens` pueden ser `NULL`, y eso significa **"el proveedor no
lo reportó"**, no "costó cero". La distinción importa: un dashboard que las confunde
reporta corridas gratis cada vez que un SDK cambia su JSON. `output_chars` es el largo del
texto devuelto y existe para medir sobrecosto de razonamiento: un modelo que gasta 742
tokens para emitir 37 de JSON se ve en la relación entre las dos columnas.

Esta tabla también es la **fuente del presupuesto**: `provider_budget.py` cuenta RPM, RPD,
TPM y TPD con una query sobre ella, sin contadores en memoria que se pierdan al reiniciar.
Una llamada con `outcome = 'auth_error'` no cuenta: no consumió cuota.

### `eval_results` — una fila por (corrida de eval x caso)

```sql
eval_run_id, corpus_case, source, prompt_version, provider,
jobs_expected, jobs_extracted, grounded, hallucinated,
fields_complete, agreement, error, created_at
```

Guarda el desglose y no solo el agregado: un grounding que cae de 0.95 a 0.80 no dice nada
sin saber **qué** casos se rompieron, y casi siempre son de un board. Ver [EVAL.md](EVAL.md).

---

## Las métricas

Todas salen por `metrics.emit` como una línea JSON con `event_type: metric`, que es lo que
`tools/watchdog.py` consume sin necesitar cambios de parser. Se emiten a nivel WARNING para
que sean visibles con el log en su nivel por defecto.

### Del lado LLM y del grafo

| Métrica | Labels | Qué mirar |
|---|---|---|
| `llm_fallback_depth` | purpose | **La más útil.** 0 = el primario contestó. ≥3 = tres proveedores fallaron |
| `llm_call_latency_ms` | provider, purpose, outcome | Qué proveedor está lento |
| `llm_tokens_total` | provider, purpose, direction | Consumo por proveedor y por propósito |
| `llm_output_chars` | provider, purpose | Largo de la respuesta, contra tokens |
| `llm_provider_cooldown_total` | provider | 429 real |
| `llm_provider_auth_failed_total` | provider | **Credencial vencida** |
| `llm_self_correction_total` | provider, purpose | El modelo tuvo que reparar su JSON |
| `llm_fallback_failed_total` | domain, error | La extracción de un board drifteado falló entera |
| `node_exec_ms` | node, graph | Dónde se va el tiempo |
| `node_failed_total` | node, error_type | Un nodo se cayó y degradó |

### De las fuentes

| Métrica | Labels | Qué mirar |
|---|---|---|
| `fuente_items` | fuente, tipo | Avisos crudos de cada fuente en la corrida; `tipo` es `css`, `endpoint` o `ats` |
| `fuente_sospechosa_total` | fuente, tipo, previos_max | **Una fuente trajo 0 y antes traía avisos.** `previos_max` es lo más que trajo en la ventana |
| `endpoint_jobs` / `endpoint_latency_ms` | domain | Avisos y latencia de un board de endpoint |
| `endpoint_saltado_por_limite` | domain | Se salteó para respetar el límite declarado por la API |
| `endpoint_cobertura_parcial` | domain | Se pidió menos de lo que la fuente declaró tener |
| `ats_jobs` / `ats_latency_ms` | plataforma (y token) | Avisos y latencia por plataforma |
| `ats_fuente_error_total` | plataforma, token, motivo | Una empresa de un ATS falló; `motivo` distingue la causa |
| `ats_items_invalidos_total` | plataforma, token | Avisos del ATS que no se pudieron convertir |
| `gmail_alertas_jobs` | proveedor | Avisos leídos de las alertas (solo con el flag prendido) |
| `gmail_alertas_error_total` | proveedor | La lectura de Gmail falló (token vencido, buzón inaccesible) |
| `gmail_alertas_truncado` | — | Hay más alertas que `GMAIL_ALERTAS_MAX_MENSAJES`; las más viejas no se leyeron |
| `circuit_breaker_open` | domain, reason | Un board CSS abrió el breaker (`consecutive_nulls` o `rate_limit`) |
| `drift_snapshot_saved` | domain, reason | Se guardó un snapshot para el corpus de eval |

### Del scoring

| Métrica | Labels | Qué mirar |
|---|---|---|
| `requisitos_evaluados` | — | Cuántos avisos se puntuaron **con** el eje de requisitos. Si cae a 0 con fuentes de endpoint activas, una fuente dejó de mandar la descripción o el CV no se cargó: el eje se apaga en silencio |
| `jobs_salario_no_confirmado` | — | Avisos con salario en texto, puntuados sin ese eje. Si sube de golpe, un board cambió el formato |
| `jobs_salario_anualizado` | — | Avisos cuyo monto se multiplicó desde un período (hora, día, semana, mes) |
| `jobs_no_elegibles` | causa | Avisos con veredicto `FAIL`, por `location` o `language` |
| `jobs_filtered_vencidos` / `_duplicados` / `_off_topic` | — | Por qué un aviso no llegó al shortlist |
| `jobs_ya_puntuados` | — | Avisos saltados por tener ya el puntaje de la versión vigente |
| `aprendizaje_resultados` | activo, umbral | Postulaciones con resultado contra el umbral de 15 |
| `aprendizaje_ajustados` | — | Avisos que cambiaron de puntaje por el historial (solo con el aprendizaje activo) |

### De seguridad e integraciones

| Métrica | Labels | Qué mirar |
|---|---|---|
| `host_rechazado_total` | host, tipo, cliente | Un request con `Host` ajeno (DNS rebinding) |
| `origen_rechazado_total` | origin, metodo, ruta, cliente | Un POST/PUT/PATCH/DELETE con `Origin` ajeno |
| `cerco_delimitador_escapado_total` | origen | Un aviso traía el delimitador del cerco LLM; casi nunca es un falso positivo |
| `notion_token_invalido` | origen | Notion rechazó el token con 401; `origen` es `arranque` o `push` |
| `ingest_records_dropped_total` | reason | Se perdieron avisos al escribir en la DB |

Hay que decir qué hace el watchdog con ellas: `tools/watchdog.py` tiene severidad propia
solo para las métricas listadas en `_METRIC_SEVERITY` (por ejemplo `node_failed_total`
CRITICAL, `llm_provider_cooldown_total` WARN). Las demás, incluidas `fuente_sospechosa_total`,
`host_rechazado_total` y `notion_token_invalido`, entran como `INFO`. Consecuencia: una
fuente sospechosa no pone el digest en rojo por sí sola. Se ve en el digest como
`metric:fuente_sospechosa_total` y en `GET /api/fuentes`, no como alerta.

---

## Cómo leer una corrida

```sql
-- ¿Terminó bien?
SELECT run_id, trigger, status, tier1_count, tier2_count, tier3_count,
       domains_aborted, error
FROM runs ORDER BY started_at DESC LIMIT 5;

-- ¿Qué trajo cada fuente? NULL en items = no se midió, no es cero
SELECT domain, node, COUNT(*) ramas, SUM(items) items, SUM(aceptados) aceptados,
       SUM(items IS NULL) sin_medir
FROM node_traces
WHERE run_id = ? AND node IN ('scrape_srp','fuente_endpoint','fuente_ats')
GROUP BY domain, node ORDER BY items;

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

Para el estado de cada fuente contra su historial no hace falta escribir la query:
`GET /api/fuentes` devuelve `sana`, `sospechosa`, `sin_rendimiento` o `sin_medir`, y
`python tools/embudo.py` (o `GET /api/embudo`) muestra qué títulos de la búsqueda no rinden
en ninguna fuente. Ver [ARCHITECTURE.md](ARCHITECTURE.md#la-fuente-dice-la-verdad).

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
| WARN | `llm_fallback_depth` 1–2, `llm_provider_cooldown_total`, breaker, `[NULL-SRP]`, `drift_snapshot_saved` |
| INFO | latencia, tokens, tiempos de nodo, salvage, jar, y toda métrica sin entrada propia |

`llm_fallback_depth` es la única métrica con **umbral por valor**: un 0 es la corrida
normal —ruido puro— y un 3 es que tres proveedores fallaron. `_METRIC_SEVERITY` es un
lookup por nombre y no puede expresar eso, así que existe `_METRIC_VALUE_THRESHOLDS`.

La credencial vencida llega a CRITICAL por la **línea de log**, no por la métrica
`llm_provider_auth_failed_total` (que entra como INFO): el nodo `auth_failed` escribe
"credencial invalida o vencida (401/403)" y ese texto matchea el marcador `credencial`.

---

## El test de la 1 AM

Cada fila de `runs` tiene que alcanzar para decidir sin abrir el código. Si `status` es
`crashed`, `error` dice qué pasó. Si los tiers vinieron en cero, `domains_aborted` y
`breaker_state_json` distinguen anti-bot de drift de cadena de proveedores agotada, y
`node_traces.items` distingue una fuente que murió de una que respondió vacía.

Y desde la v3, una credencial vencida **se nombra**: el log dice qué variable de `.env`
actualizar, el watchdog la marca CRITICAL, y hay una métrica por proveedor. Antes decía
`groq exhausted (network)` y mandaba a revisar la conectividad. Lo mismo vale para Notion:
un 401 desactiva la integración, nombra `NOTION_API_KEY` y `/api/status` informa
`notion_estado`.

---

## Condiciones de borde documentadas

**Escribir telemetría nunca puede tumbar un scrape.** Todos los `record_*` son fail-safe:
loguean y siguen. Una falla registrando una falla no escala. Los tests apuntan el
`db_path` a una ruta inválida y verifican que cada método la trague. Lo mismo vale para
`fuentes_estado`: ninguna función de escritura levanta, porque la telemetría que puede
tumbar la corrida que mide se termina apagando.

**El texto de error se recorta** a 500 caracteres (2000 en `runs`). Los 5xx de gateway de
los free tiers traen páginas HTML enteras en el cuerpo de la excepción, y una fila con 8 KB
de HTML vuelve ilegible justo la tabla que se consulta cuando algo anda mal.

**No hay poda automática.** Con una búsqueda de 51 títulos, una corrida escribe ~430 filas
de `node_traces` (408 ramas CSS más las de endpoint y ATS) y ~10 de `llm_calls`. A 3
corridas semanales (`SCHEDULER_CRON_DAYS=tue,fri,sat`) son ~67.000 filas de `node_traces`
al año: SQLite lo sostiene sin esfuerzo, así que no se justifica una poda todavía. **El
disparador para agregarla** es que el cron pase a diario o que el fan-out CSS se
descomponga y `node_traces` escriba más filas por corrida. El primero que lo va a sentir es
el `db.backup()` que corre al inicio de cada corrida.

**`NULL` en `items` se arrastra.** Las filas anteriores a la columna y las ramas que no
consultaron la fuente quedan en `NULL` para siempre. `fuentes_estado` las descarta antes de
recortar a la ventana y `embudo_fuentes` dice cuántas hay en lugar de contarlas como
vacías. Una query propia que haga `SUM(items)` sin mirar los `NULL` subestima y no avisa.
