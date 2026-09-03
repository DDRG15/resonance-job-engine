# Proveedores LLM — cadena, reparto y verificación

Actualizado: **2026-09-03**. Reemplaza al `LLM_PROVIDERS.md` de la raíz (2026-07-05).

GEMA usa siete proveedores de free tier. Ninguno es indispensable: la cadena recorre todos
antes de darse por vencida, y el motor principal del scraper —los 10 boards con selectores
CSS— **no usa LLM en absoluto, 0 tokens**. El LLM es el camino de recuperación cuando un
board driftea, más el parseo del prompt y del CV.

---

## El reparto de carga

Hasta la v3, las cuatro tareas arrancaban en `PRIMARY_LLM` y solo pasaban al siguiente
cuando ese fallaba. Un proveedor se comía el 100% del tráfico hasta agotarse mientras los
otros seis miraban. Con siete free tiers disponibles, eso es tirar seis y esperar el 429 —
que es exactamente lo que pasó el 2026-09-01.

Ahora cada propósito arranca en un proveedor distinto:

| Propósito | Primario | Perfil de consumo |
|---|---|---|
| `extract` | **cerebras** | ~20.000 chars de entrada, hasta 3 por corrida — el más pesado |
| `profile` | **mistral** | el CV entero, ~8.000 chars, 1 por carga |
| `parse` | **groq** | ~500 tokens, 1 por búsqueda |
| `audit` | **gemini** | 128 tokens máximo, 1 por búsqueda — el más liviano |

**La cadena completa sigue detrás de cada uno.** Esto cambia por dónde EMPIEZA una tarea,
no qué respaldo tiene: un propósito cuyo primario cae recorre los otros seis igual que
antes. Ejemplo real:

```
extract  → cerebras → groq → gemini → openrouter → cohere → sambanova → mistral
parse    → groq → gemini → openrouter → cohere → cerebras → sambanova → mistral
```

Es determinístico y no rotativo a propósito: siempre se sabe qué proveedor atendió qué sin
mirar un contador. Un round-robin reparte más parejo pero vuelve irreproducible cualquier
diagnóstico.

**Configurable sin tocar código**, en `.env`:

```
LLM_PRIMARY_PARSE=groq
LLM_PRIMARY_EXTRACT=cerebras
LLM_PRIMARY_PROFILE=mistral
LLM_PRIMARY_AUDIT=gemini
```

Un nombre mal escrito o un proveedor sin key degradan al orden por defecto. No rompen la
cadena — hay test.

---

## El auditor no puede auditarse a sí mismo

La cadena de auditoría **excluye al primario de `parse`**. Es la única diferenciación de
rol que existía antes de la v3, y sigue vigente: un proveedor que valida su propia salida
no valida nada.

---

## Estado verificado — 2026-09-03

| Proveedor | Key | Modelo | Verificado |
|---|---|---|---|
| groq | ✅ | `openai/gpt-oss-120b` | completion real, 729ms, 129/165 tokens |
| gemini | ✅ | `gemini-flash-lite-latest` | presente en listado v1beta |
| openrouter | ✅ | `nvidia/nemotron-3-ultra-550b-a55b:free` | respondió en el E2E, 470/408 tokens |
| cohere | ✅ | `command-r-plus-08-2024` | listado |
| cerebras | ✅ | `gemma-4-31b` | listado |
| sambanova | ✅ | `Meta-Llama-3.3-70B-Instruct` | listado |
| mistral | ✅ | `mistral-small-latest` | listado |

---

## La lección que este archivo existe para no repetir

**Validez de key ≠ que la completion funcione. Y ambas cosas fallan por separado.**

El 2026-09-01, en una misma sesión, se vieron los dos modos:

- `llama-3.3-70b-versatile` devolvía **404 con la key todavía válida**. Groq había dado de
  baja toda su familia llama de chat. El log de GEMA decía `network` y mandaba a revisar la
  conectividad.
- Cuarenta minutos después, **la misma key pasó a 401**. Venció a mitad de sesión.

Son diagnósticos opuestos —cambiar una línea de config contra renovar una credencial— y
desde adentro de GEMA se veían idénticos. Por eso existe:

```bash
python tools/check_provider_models.py          # exit 1 si algo esta mal
python tools/check_provider_models.py --list   # ademas imprime los modelos disponibles
```

Distingue `[KEY]` (renovar credencial) de `[GONE]` (cambiar pin) y sale con exit 1 en
cualquiera de los dos casos. Correrlo después de renovar una key es lo que evita el
escenario de "credencial nueva, modelo muerto, 404 en silencio".

**Y el smoke va por proveedor, no por nombre de modelo.** `gpt-oss-120b` devolvía content
vacío en Cerebras —está documentado desde julio— y en Groq responde limpio. El mismo id
sirve o no según quién lo sirva, así que `--list` no alcanza: hay que pedir una completion
de verdad.

---

## Qué pasa cuando una credencial vence

Desde la v3 el sistema lo dice en vez de disfrazarlo de error de red:

1. `_is_auth_error` detecta 401/403 en las shapes de los siete SDKs.
2. El nodo `auth_failed` parkea ese proveedor **24 horas** — renovar una key exige
   intervención humana, no que pase el tiempo. Reintentarlo cada 60 segundos solo agrega
   latencia a un fallo seguro.
3. Emite `llm_provider_auth_failed_total` y loguea qué variable de `.env` actualizar.
4. `tools/watchdog.py` clasifica esa línea como **CRITICAL**, categoría `credencial`.

Antes de esto, el log decía `groq exhausted (network)` y cada llamada recorría los siete
proveedores completos antes de fallar.

---

## Rate limits

Un 429 parkea al proveedor `LLM_PROVIDER_COOLDOWN_S` segundos (60 por defecto) y la cadena
sigue. Es una **ventana**, no un booleano: en v2 un 429 transitorio mataba al proveedor
hasta reiniciar el proceso, así que un pico a las 9 de la mañana costaba ese proveedor
durante toda la corrida del cron.

Los cooldowns son un mapa con **un solo dueño** (`nlp_engine._cooldowns`), compartido con
el grafo por referencia. Tener dos copias fue un bug real de la Fase 6: se limpiaba una y
los proveedores seguían parkeados en la otra.
