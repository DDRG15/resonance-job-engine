# Observabilidad — GEMA v3

> **ESQUELETO (Fase 1).** Se completa en la Fase 4 (tablas y métricas) y se cierra en la
> Fase 9. Plan vigente: []().

Una corrida que sacó menos jobs que la anterior tiene que poder explicarse con una query,
no leyendo código. En v2 no se podía: `run_id`, summary y tiers vivían en memoria y morían
al reiniciar el proceso, y de los 19 call sites de `metrics.emit` ninguno era del lado
LLM — cero latencia, cero tokens, cero profundidad de fallback.

Toda la telemetría es local. Sin LangSmith, sin SaaS, sin OpenTelemetry: GEMA corre en una
laptop y los prompts llevan el CV parseado del usuario.

## Secciones a completar

**Las tablas.** `runs`, `node_traces`, `llm_calls`, `eval_results`. Esquema, qué escribe
cada fila y quién la escribe. Todas aditivas y con el patrón `CREATE TABLE IF NOT EXISTS`
+ `ALTER TABLE` idempotente que ya usa `database.py:_initialize` — la app vieja las ignora,
que es lo que hace barato el rollback.

**Las métricas.** Las siete nuevas, sus labels, y qué significa cada valor.
`llm_fallback_depth` es la que más dice: un 3 significa que tres proveedores fallaron
antes de que alguno respondiera.

**Cómo leer una corrida.** Queries concretas, de la más útil a la más específica. La
primera responde "¿esta corrida terminó bien?"; la última, "¿qué nodo se comió el tiempo?".

**El watchdog.** `tools/watchdog.py` ya parsea cualquier línea con `event_type=metric`, así
que las métricas nuevas se ven sin tocar el parser. Lo que sí hay que declarar es la
severidad de cada una, y el umbral por valor para `llm_fallback_depth`.

## El test de la 1 AM

Cada fila de `runs` tiene que alcanzar para decidir sin abrir el código: si `status` es
`crashed`, `error` dice qué pasó; si los tiers vinieron en cero, `domains_aborted` y
`breaker_state_json` dicen si fue anti-bot, drift o una cadena de proveedores agotada.

## Condiciones de borde documentadas

**Escribir telemetría nunca puede tumbar un scrape.** Todos los `record_*` son fail-safe,
misma disciplina que `metrics.emit`. Una falla registrando una falla no escala.

**El timing por nodo miente si se lee sin contexto.** `Send` despacha todas las ramas y el
semáforo las frena adentro del nodo, así que 17 nodos pueden figurar "corriendo" mientras
esperan un tab de Chromium. Por eso `queue_wait_ms` va separado de `exec_ms`, y cualquier
lectura de latencia que sume los dos está mal.
