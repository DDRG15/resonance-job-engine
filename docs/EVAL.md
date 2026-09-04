# Evaluación de extracción — GEMA v3

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

Y del gate: **tolerancia de 0.02 por defecto**. Los modelos no son determinísticos y dos
corridas del mismo corpus difieren un poco. Un gate sin margen falla por ruido, y **un gate
que falla por ruido se termina desactivando** — que es peor que no tenerlo.

El gate solo bloquea lo que **empeora**. Una mejora no es una regresión.

---

## El corpus: tres fuentes

| Fuente | Origen | Tiene respuestas anotadas |
|---|---|---|
| `fixture` | `tests/fixtures/extraction_corpus/*.json` — curados a mano | Sí |
| `drift` | `drift_quarantine/*.json` con `schema_version >= 2` | No |
| `review` | filas de `manual_review` con `resolved=1` | Sí, para la URL |

Los snapshots **sin `schema_version` se descartan**. Son de v2: guardaban
`sent_text[:8000]` contra un input truncado a 20000, y medir grounding contra un texto
incompleto marca como alucinación una URL que **sí estaba** en la fuente. Un corpus
construido sobre eso no mide poco: **mide al revés**.

El corpus de drift **crece solo**. Cada board drifteado deja un snapshot con el texto
completo, y una de cada diez extracciones exitosas también — eso último importa: un corpus
hecho solo de fallas produce un gate que mide el peor caso y no puede detectar que un
cambio empeoró lo que ya funcionaba.

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
`conftest.py` que lo impide, y un test que falla si un snapshot de test llega al corpus.

---

## Condiciones de borde documentadas

**El gate no corre en pytest.** Pega a proveedores LLM reales: es lento, gasta cuota y no
es determinista. Va como paso opcional de `scripts/build_beta.bat` y como comando manual
antes de mergear un cambio de prompt.

Lo que **sí** corre en pytest es el scoring (`graph/eval_scoring.py`) con callers
mockeados: 28 tests que validan la **medición**, no el modelo. Esa separación es
deliberada — un gate que mide mal es peor que no tener gate, porque bloquea cambios buenos
y deja pasar malos.

**`prompt_version` es un hash del prompt efectivo**, que cambia con el perfil del CV
cargado. Sin él, una caída del grounding se lee igual si la causó un cambio de prompt o una
regresión del modelo — y son diagnósticos opuestos.
