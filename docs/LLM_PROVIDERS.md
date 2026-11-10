# Proveedores LLM — cadena, reparto, presupuesto y verificación

Actualizado: **2026-10-12**. La tabla de modelos verificados conserva su fecha de
verificación (2026-09-03).

Resonance usa siete proveedores de free tier. Ninguno es indispensable: la cadena recorre
todos antes de darse por vencida, y la ingesta de avisos —endpoints públicos, APIs de ATS
y selectores CSS— **no usa LLM en absoluto, 0 tokens**. El LLM es el camino de recuperación
cuando un board CSS driftea, más el parseo del prompt, el perfil del CV y una auditoría
cruzada del config. El scoring (`matcher.py`, `requisitos.py`) es determinista y no llama a
ningún proveedor.

---

## El reparto de carga

Hasta la v3, las cuatro tareas arrancaban en `PRIMARY_LLM` y solo pasaban al siguiente
cuando ese fallaba. Un proveedor se comía el 100% del tráfico hasta agotarse mientras los
otros seis miraban. Con siete free tiers disponibles, eso es tirar seis y esperar el 429 —
que es exactamente lo que pasó el 2026-09-01.

Ahora cada propósito arranca en un proveedor distinto. El criterio cruza dos ejes: el
perfil de consumo de la tarea y el **sobrecosto de razonamiento** del modelo, medido el
2026-09-03 con el prompt real (cuántos tokens gasta antes de emitir el JSON útil).

| Propósito | Primario | Perfil de consumo | Por qué ese proveedor |
|---|---|---|---|
| `parse` | **mistral** | ~500 tokens, 1 por búsqueda, el usuario espera | `mistral-small`: 25% de sobrecosto, 2,4 s — el más eficiente |
| `audit` | **gemini** | 128 tokens máximo, 1 por búsqueda, corre pegado al parse | `flash-lite`: 43% de sobrecosto, 2,1 s — el más rápido |
| `extract` | **groq** | ~20.000 chars de entrada, hasta 3 por corrida, en background | tolera 3,5 s y es el modelo más grande |
| `profile` | **cohere** | el CV entero, ~8.000 chars, 1 por carga | 23,8 s es aceptable para una carga manual con spinner |

El eje del sobrecosto domina donde la tarea se repite o el usuario espera. Un modelo de
razonamiento paga su sobrecosto en cada llamada: `groq gpt-oss-120b` gasta 742 tokens para
emitir 37 de JSON (95%) y `openrouter nemotron` tarda 66,8 s (91%). Por eso Groq es primario
de `extract`, que corre en background y tolera la latencia, y no de `parse`, que el usuario
espera.

**Cerebras y SambaNova no son primarios de nada.** El 2026-09-03 Cerebras devolvió
402 Payment Required (el free tier exige pago) y SambaNova 429 en el primer intento, y los
dos estaban asignados como primarios. Siguen en la cadena como respaldo: si se recuperan,
entran solos.

**La cadena completa sigue detrás de cada uno.** Esto cambia por dónde EMPIEZA una tarea,
no qué respaldo tiene: un propósito cuyo primario cae recorre los otros seis. El orden por
defecto es `PRIMARY_LLM` (groq), gemini, openrouter, cohere, cerebras, sambanova, mistral, y
el primario del propósito pasa al frente. Ejemplo real:

```
parse    → mistral → groq → gemini → openrouter → cohere → cerebras → sambanova
extract  → groq → gemini → openrouter → cohere → cerebras → sambanova → mistral
profile  → cohere → groq → gemini → openrouter → cerebras → sambanova → mistral
```

Es determinístico y no rotativo a propósito: siempre se sabe qué proveedor atendió qué sin
mirar un contador. Un round-robin reparte más parejo pero vuelve irreproducible cualquier
diagnóstico.

**Configurable sin tocar código**, en `.env` (los valores de abajo son los por defecto):

```
LLM_PRIMARY_PARSE=mistral
LLM_PRIMARY_AUDIT=gemini
LLM_PRIMARY_EXTRACT=groq
LLM_PRIMARY_PROFILE=cohere
```

Un nombre mal escrito o un proveedor sin key degradan al orden por defecto. No rompen la
cadena — hay test.

---

## Presupuesto: contar antes de llamar

`_cooldowns` castiga **después** de comerse el 429: cada descubrimiento cuesta una llamada
perdida, un round-trip y un salto de fallback. `provider_budget.py` cuenta **antes**.
`select_provider` consulta `hay_presupuesto` por proveedor y lo saltea sin gastar la llamada
si ya consumió el 95% (`UMBRAL`) de su ventana más apretada.

Los cuatro contadores (RPM, RPD, TPM, TPD) son queries sobre `llm_calls`, que ya graba
`provider`, `prompt_tokens`, `completion_tokens` y `outcome` en cada llamada. No hay
contadores en memoria que se pierdan al reiniciar. Una llamada con `auth_error` no cuenta:
no consumió cuota del proveedor.

Los topes viven en `provider_budget.LIMITES`, **con fecha** (2026-09-17), porque un límite
sin fecha es un número que va a estar mal sin que nadie sepa desde cuándo:

| Proveedor | RPM | RPD | TPM |
|---|---|---|---|
| groq | 30 | 14.400 | 6.000 |
| gemini | 15 | 1.500 | 1.000.000 |
| mistral | 60 | — | — |
| cohere | 20 | 1.000 | — |

OpenRouter, Cerebras y SambaNova no publican topes estables para el free tier y **no tienen
fila**. Un proveedor sin límite declarado siempre alcanza: el presupuesto restringe donde
hay dato, no donde falta. Si la tabla no se puede leer, la llamada se hace igual; se
degrada a descubrir el límite chocándolo.

---

## El auditor excluye a `PRIMARY_LLM`, no al primario de `parse`

La cadena de auditoría excluye a `config.PRIMARY_LLM` (groq por defecto) y arranca en
`gemini`. Es lo que quedó de la única diferenciación de rol que existía antes de la v3.
Escenario: desde el reparto de carga `parse` arranca en mistral, no en `PRIMARY_LLM`.
Consecuencia: el auditor no puede ser groq, pero sí puede ser mistral —que generó el
config— cuando gemini, openrouter, cohere, cerebras y sambanova fallan antes. La
independencia entre generador y auditor se garantiza contra `PRIMARY_LLM`, no contra el
proveedor que atendió el `parse`. La auditoría es advisory y nunca bloquea, así que el costo
es un chequeo menos independiente en el caso de cadena casi agotada; la decisión es dejarlo
documentado en lugar de fingir que la regla sigue siendo "un proveedor no valida su propia
salida".

---

## Estado verificado — 2026-09-03

| Proveedor | Modelo (pin en `config.py`) | Verificado |
|---|---|---|
| groq | `openai/gpt-oss-120b` | completion real, 729 ms, 129/165 tokens |
| gemini | `gemini-flash-lite-latest` | presente en listado v1beta |
| openrouter | `nvidia/nemotron-3-ultra-550b-a55b:free` | respondió en el E2E, 470/408 tokens |
| cohere | `command-r-plus-08-2024` | listado |
| cerebras | `gemma-4-31b` | listado; free tier devolvió 402 |
| sambanova | `Meta-Llama-3.3-70B-Instruct` | listado; devolvió 429 |
| mistral | `mistral-small-latest` | listado |

Esta tabla envejece. Para saber el estado de hoy, correr `tools/check_provider_models.py`.

---

## La lección que este archivo existe para no repetir

**Validez de key ≠ que la completion funcione. Y ambas cosas fallan por separado.**

El 2026-09-01, en una misma sesión, se vieron los dos modos:

- `llama-3.3-70b-versatile` devolvía **404 con la key todavía válida**. Groq había dado de
  baja toda su familia llama de chat. El log decía `network` y mandaba a revisar la
  conectividad.
- Cuarenta minutos después, **la misma key pasó a 401**. Venció a mitad de sesión.

Son diagnósticos opuestos —cambiar una línea de config contra renovar una credencial— y
desde adentro del sistema se veían idénticos. Por eso existe:

```bash
python tools/check_provider_models.py          # exit 1 si algo esta mal
python tools/check_provider_models.py --list   # ademas imprime los modelos disponibles
```

Distingue `[KEY]` (renovar credencial) de `[GONE]` (cambiar pin) y sale con exit 1 en
cualquiera de los dos casos. Correrlo después de renovar una key es lo que evita el
escenario de "credencial nueva, modelo muerto, 404 en silencio".

**Y el smoke va por proveedor, no por nombre de modelo.** `gpt-oss-120b` devolvía content
vacío en Cerebras y en Groq responde limpio. El mismo id sirve o no según quién lo sirva,
así que `--list` no alcanza: hay que pedir una completion de verdad.

---

## Qué pasa cuando una credencial vence

El sistema lo dice en vez de disfrazarlo de error de red:

1. `_is_auth_error` detecta 401/403 en las shapes de los siete SDKs.
2. El nodo `auth_failed` parkea ese proveedor **24 horas** (`_AUTH_COOLDOWN_S = 86400`) —
   renovar una key exige intervención humana, no que pase el tiempo. Reintentarlo cada 60
   segundos solo agrega latencia a un fallo seguro.
3. Emite `llm_provider_auth_failed_total` y loguea qué variable de `.env` actualizar.
4. `tools/watchdog.py` clasifica esa línea de log como **CRITICAL**, categoría `credencial`.

Antes de esto, el log decía `groq exhausted (network)` y cada llamada recorría los siete
proveedores completos antes de fallar.

La integración de Notion sigue el mismo patrón: un 401 marca la integración como inválida,
nombra `NOTION_API_KEY` y emite `notion_token_invalido`.

---

## Rate limits

Un 429 parkea al proveedor `LLM_PROVIDER_COOLDOWN_S` segundos (60 por defecto) y la cadena
sigue. Es una **ventana**, no un booleano: en v2 un 429 transitorio mataba al proveedor
hasta reiniciar el proceso, así que un pico a las 9 de la mañana costaba ese proveedor
durante toda la corrida del cron.

Los cooldowns son un mapa con **un solo dueño** (`nlp_engine._cooldowns`), compartido con
el grafo por referencia. Tener dos copias fue un bug real de la migración al grafo: se limpiaba una y
los proveedores seguían parkeados en la otra.

---

## Qué datos salen a un proveedor

Dos entradas distintas llegan a un LLM externo, y las dos tienen una guarda.

**El CV.** Sale a un proveedor externo solo si el request de `POST /api/cv` trae la versión
de consentimiento vigente (`CV_CONSENT_VERSION`). El archivo se corta antes de leerse al
pasar `CV_MAX_UPLOAD_BYTES` (5 MiB), el PDF a `CV_MAX_PDF_PAGES` (10 páginas) y el parseo a
`CV_PARSE_TIMEOUT_SECONDS` (20 s). Al modelo llega un texto recortado, y lo que se guarda
es el perfil derivado (rol, skills, señales), nunca el documento.

**El texto de un board.** Es texto de un tercero. Entra al modelo cercado como dato no
confiable (`nlp_engine.cercar_texto_de_terceros`): delimitador, instrucción en el mismo
turno y escape de cualquier corrida de tres o más corchetes iguales dentro del contenido.
Ver [ARCHITECTURE.md](ARCHITECTURE.md#el-subgrafo-de-extracción--graphextract_graphpy).
