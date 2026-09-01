# Evaluación de extracción — GEMA v3

> **ESQUELETO (Fase 1).** Se completa en la Fase 7. Plan vigente:
> []().

Un cambio de prompt que mejora un board y rompe otros tres no se nota hasta la corrida
siguiente, y para entonces ya está commiteado. El harness existe para que ese cambio falle
antes de mergear, con un número al lado.

## El corpus no se hereda: se construye

La premisa con la que se arrancó esta migración era reusar lo ya capturado. No existe. Los
22 archivos de `drift_quarantine/` son artefactos de la suite — `domain: board.com`,
`sent_text: "x"`, y un `llm_output` que es el repr de Python de un
`ExtractedJob(title='Junk')`. `manual_review` tiene dos filas, ambas de test.

Peor: el capturador de v2 guardaba `sent_text[:8000]` mientras la extracción truncaba su
input a 20000. El grounding se juzga contra el texto que se mandó, así que la URL que
decide el veredicto podía caer en el tramo que nunca se escribió. Un corpus construido
sobre esa captura mide ruido.

Por eso la Fase 5 arregla la captura antes de que la Fase 7 la use, y el corpus inicial se
cura a mano.

## Secciones a completar

**Las fuentes.** Fixtures curados a mano en `tests/fixtures/extraction_corpus/`, más los
snapshots de `drift_quarantine/` que ya lleven `prompt_version`, más las filas de
`manual_review` resueltas por humano — esas son los casos de reparación de URL, donde hay
una respuesta correcta conocida.

**Las cuatro métricas.** Grounding rate, field completeness, hallucination rate, y
agreement contra la URL que resolvió el humano. Qué mide cada una y qué significa que baje.

**Cómo agregar un caso.** El formato, de dónde sacar el HTML, y qué cuenta como
`expected_jobs`.

**El gate.** `--baseline <eval_run_id>` para diffear contra una corrida previa, `--gate`
para exit 1 si el grounding cae o la alucinación sube.

## Condiciones de borde documentadas

**El gate no corre en pytest.** Pega a proveedores LLM reales: es lento, cuesta cuota y no
es determinista. Va como paso de `scripts/build_beta.bat` y como comando manual antes de
mergear. Lo que sí corre en pytest es el harness con un caller mockeado, que valida el
scoring — no el modelo.

**Un corpus de puras fallas mide solo el peor caso.** Por eso la Fase 5 snapshotea también
en éxito, muestreado. Sin esos casos, el gate no detecta una regresión que empeora lo que
ya funcionaba.
