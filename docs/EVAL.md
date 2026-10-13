# Evaluación de extracción — Resonance (motor GEMA)

Un cambio de prompt que mejora un board y rompe otros tres no se nota hasta la corrida
siguiente, y para entonces ya está commiteado. El harness existe para que ese cambio falle
antes de mergear, con un número al lado.

```bash
python tools/eval_extraction.py --dry-run   # lista el corpus, sin llamar a ningun LLM
python tools/eval_extraction.py             # corre y guarda en eval_results
python tools/eval_extraction.py --gate      # exit 1 si empeoro vs la corrida previa
python tools/eval_extraction.py --baseline ID
```

---

## Qué mide y qué no

Mide **una sola cosa**: la calidad de la extracción por LLM, el camino de emergencia que
recupera un board CSS cuando sus selectores driftearon (`extract` en
[ARCHITECTURE.md](ARCHITECTURE.md)). Es el único lugar donde un modelo escribe datos que
después se guardan en el registry, y por eso es el único que necesita un gate con modelo.

No mide, porque no necesita un gate con modelo:

- **La ingesta por endpoint y por ATS.** Un GET a una API pública no pasa por ningún LLM; su
  salud la dice el conteo por fuente (`fuentes_estado`, `GET /api/fuentes`).
- **El scoring.** `matcher.py` y `requisitos.py` son deterministas: mismo aviso y mismo
  perfil, mismo puntaje. Su regresión la atrapa `tests/test_score_version_congelada.py`,
  que falla si alguien cambia la salida de `score_job` sin subir `SCORE_VERSION` (hoy `v7`).

---

## Las cuatro métricas

**`grounding_rate`** — de los jobs devueltos, cuántos tienen su URL **literalmente** en el
texto que se le mandó al modelo. Es la única que **no necesita respuestas anotadas**: la
fuente es su propio ground truth. Por eso el corpus puede crecer solo. Si baja, el modelo
empezó a inventar URLs.

**`hallucination_rate`** — el complemento. Se reporta aparte porque un grounding de 0.9
sobre 40 jobs y sobre 4 no son el mismo riesgo.

**`fields_complete`** — promedio de campos no vacíos sobre los cuatro que el matcher
realmente usa (`title`, `company`, `source_url`, `salary_min`). Medirlo sobre los 15 campos
del schema diluiría la señal: que falte `experience_max_years` no cambia nada.

Existe para el **modo de falla silencioso**: cantidad correcta de jobs, todos grounded, y
la mitad de los campos en `null`. `grounding_rate` diría 1.0 y todo parecería perfecto.

**`agreement`** — solo para casos de `manual_review` resueltos: ¿la URL que produjo el
sistema es la que el humano corrigió? La única de las cuatro con respuesta correcta
conocida, y apunta al campo que más falla.

---

## Dos decisiones de medición que cambian el resultado

**Las tasas se calculan sobre TOTALES, no promediando tasas por caso.** Un caso con 1 job
perfecto y otro con 40 alucinados: promediar tasas da 0.5; sobre totales da 1/41. La
segunda describe lo que pasó. Con la primera, un board chico que anda bien tapa a uno
grande que se rompió.

**Los casos con 0 jobs no entran en `fields_complete`.** Un board legítimamente vacío tiene
completitud 0 por definición, y contarlo castigaría al sistema por hacer lo correcto.

Y del gate: **tolerancia de 0.02 por defecto** (`--tolerancia`). Los modelos no son
determinísticos y dos corridas del mismo corpus difieren un poco. Un gate sin margen falla
por ruido, y **un gate que falla por ruido se termina desactivando** — que es peor que no
tenerlo.

El gate solo bloquea lo que **empeora**. Una mejora no es una regresión. Sin `--gate` el
harness imprime las regresiones pero sale con 0; con `--gate` sale con 1.

---

## El corpus: tres fuentes

| Fuente | Origen | Tiene respuestas anotadas |
|---|---|---|
| `fixture` | `tests/fixtures/extraction_corpus/*.json` — curados a mano | Sí |
| `drift` | `drift_quarantine/*.json` con `schema_version >= 2` | No |
| `review` | filas de `manual_review` con `resolved=1` | Sí, para la URL |

**Estado hoy.** La carpeta de fixtures tiene el README y **ningún caso curado**: la curación
(fase 7a del plan de migración) es trabajo del usuario y sigue pendiente. El corpus de drift
tiene 6 snapshots con `schema_version: 2`, todos de `greenhouse.com` con el mismo largo de
input (11.727 caracteres), capturados el 2026-09-29. `greenhouse.com` es un board CSS retirado
(`active=False`); la ingesta de Greenhouse hoy va por su API de ATS. Consecuencia: el gate
corre, pero sobre un corpus de un solo board y sin respuestas anotadas. Mide grounding y
completitud de ese caso, no un abanico de patrones de URL. Hasta curar los fixtures, un
`--gate` verde no dice nada sobre el resto de los boards.

Los snapshots **sin `schema_version` se descartan**. Son de v2: guardaban
`sent_text[:8000]` contra un input truncado a 20000, y medir grounding contra un texto
incompleto marca como alucinación una URL que **sí estaba** en la fuente. Un corpus
construido sobre eso no mide poco: **mide al revés**.

El corpus de drift **crece solo**. Cada board drifteado deja un snapshot con el texto
completo, y una de cada diez extracciones exitosas también
(`DRIFT_SNAPSHOT_SUCCESS_SAMPLE=10`) — eso último importa: un corpus hecho solo de fallas
produce un gate que mide el peor caso y no puede detectar que un cambio empeoró lo que ya
funcionaba. El directorio guarda hasta 300 archivos (`DRIFT_QUARANTINE_MAX_FILES`): a 3
boards drifteados por corrida, 50 se reciclaban en dos semanas, justo el período que hay
que comparar cuando se cambia un prompt.

---

## Cómo agregar un caso curado

El formato y el procedimiento están en
[`tests/fixtures/extraction_corpus/README.md`](../tests/fixtures/extraction_corpus/README.md).

Lo esencial: **no escribas el `source_text` a mano.** Tiene que ser el texto real que
produce `scraper._strip_html_to_text`, porque el grounding compara URLs por presencia
literal — un texto inventado mide otra cosa. Se saca de un snapshot con
`schema_version: 2`, que ya lo trae completo.

Quince a veinticinco casos alcanzan. Priorizar por lo que rompe: un board por patrón de URL
distinto, uno donde el modelo históricamente alucinó, uno con salario en el card, y **uno
legítimamente vacío** — ese detecta al modelo que rellena para complacer.

---

## Por qué esto se cura a mano y no hay atajo

Durante meses el corpus fueron 38 archivos que resultaron ser **100% artefactos de la
suite**: `domain: board.com`, `sent_text: "x"`, y un `llm_output` que era el repr de Python
de un `ExtractedJob(title='Junk')`. Las métricas calculadas sobre eso habrían salido
plausibles y falsas.

La causa era que `tests/test_llm_extraction_fallback.py` no monkeypatcheaba
`DRIFT_QUARANTINE_DIR` y escribía en el directorio real. Hoy hay un fixture autouse en
`conftest.py` que lo redirige a un directorio temporal, y un test
(`test_el_corpus_real_no_contiene_dominios_de_test`) que falla si un snapshot de test llega
al corpus. Los 38 archivos viejos quedaron aparte en `drift_quarantine/_sinteticos_pre_fix`.

---

## Condiciones de borde documentadas

**El gate no corre en pytest.** Pega a proveedores LLM reales: es lento, gasta cuota y no
es determinista. Va como paso opcional (`[3/7]`) de `scripts/build_beta.bat` —que lo omite si
`--dry-run` informa corpus vacío— y como comando manual antes de mergear un cambio de
prompt.

Lo que **sí** corre en pytest es el scoring (`graph/eval_scoring.py`) con callers
mockeados: 28 tests que validan la **medición**, no el modelo. Esa separación es
deliberada — un gate que mide mal es peor que no tener gate, porque bloquea cambios buenos
y deja pasar malos.

**`prompt_version` es un hash del prompt efectivo**, que cambia con el perfil del CV
cargado. Sin él, una caída del grounding se lee igual si la causó un cambio de prompt o una
regresión del modelo — y son diagnósticos opuestos. Desde el cerco del texto de terceros la
huella incluye también la instrucción del cerco, que viaja en el turno del usuario y no en
el system prompt: cambiarla mueve el comportamiento del modelo, y sin esa inclusión dos
corridas con la misma versión no serían comparables. Esa inclusión movió la huella una vez,
así que la primera corrida del corpus después del cambio es una línea base nueva.

**Lo que el eval mide es el camino real.** El texto del board llega al modelo cercado
(`cercar_texto_de_terceros`), igual que en producción; el harness no usa un camino
distinto.
