# Corpus de evaluación de extracción

Esta carpeta es la **única fuente del corpus con respuestas correctas conocidas**. Las
otras dos (`drift_quarantine/` y `manual_review`) se llenan solas, pero no traen anotado
qué debería haber salido.

Se cura a mano. No hay atajo, y hubo un intento de saltearlo que costó caro: durante meses
el corpus fueron 38 archivos que resultaron ser **100% artefactos de la suite**
(`domain: board.com`, `sent_text: "x"`). Las métricas calculadas sobre eso habrían salido
plausibles y falsas.

---

## Formato

Un JSON por caso. El nombre del archivo es el identificador que aparece en los reportes,
así que conviene que diga de qué board viene:

```json
{
  "source_text": "el texto EXACTO que se le manda al modelo, con los href inlineados",
  "source_url": "https://arc.dev/remote-jobs?q=backend",
  "notes": "por que este caso esta en el corpus: que rompe, que verifica",
  "expected_jobs": [
    {
      "title": "Senior Backend Engineer",
      "company": "Acme",
      "source_url": "https://arc.dev/j/senior-backend-acme"
    }
  ]
}
```

`expected_jobs` es opcional. Sin él, el caso sigue midiendo grounding y completitud —que
son autoverificables contra `source_text`— pero no cuenta para `agreement`.

---

## De dónde sacar `source_text`

**No lo escribas a mano.** Tiene que ser el texto real que produce
`scraper._strip_html_to_text` sobre el HTML del board, porque el grounding compara URLs
por presencia literal: un texto inventado mide otra cosa.

La forma directa es tomarlo de un snapshot de producción:

```bash
python -c "
import json, pathlib
p = pathlib.Path('drift_quarantine').glob('*.json')
d = json.loads(next(p).read_text(encoding='utf-8'))
print(d['sent_text'][:2000])
"
```

Un snapshot con `schema_version: 2` ya trae el texto completo y sin truncar. Copiarlo,
anotar los `expected_jobs` mirando el board, y guardarlo acá lo convierte de "caso sin
anotar" en "caso con respuesta correcta".

---

## Qué casos vale la pena curar

Priorizar por lo que rompe, no por cobertura:

- **Un board por cada patrón de URL distinto.** Los relativos (`/job/x`) son los que
  fuerzan la reparación de Phase 30b.
- **Un caso donde el modelo históricamente alucinó.** Si algún board produjo
  `example.com` en la salida, ese texto es oro: verifica que el grounding lo siga
  rechazando.
- **Un caso con salario en el texto del card**, que es de donde sale el Tier 1.
- **Un caso vacío legítimo** — una búsqueda sin resultados. Debe extraer 0 jobs sin
  inventar ninguno, y es el que detecta un modelo que rellena para complacer.

Quince a veinticinco casos alcanzan. Más que eso no mejora la señal y hace el gate lento.

---

## Cómo correrlo

```bash
python tools/eval_extraction.py --dry-run   # lista el corpus, sin llamar a ningun LLM
python tools/eval_extraction.py             # corre y guarda en la tabla eval_results
python tools/eval_extraction.py --gate      # exit 1 si empeoro vs la corrida previa
```

El gate **no corre en pytest**: pega a proveedores reales, es lento y no es determinista.
Va como paso de `scripts/build_beta.bat` y como comando manual antes de mergear un cambio
de prompt.
