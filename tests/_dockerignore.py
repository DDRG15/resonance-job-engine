"""
Evaluador de `.dockerignore` con la semántica de Docker, para los tests que verifican
que un secreto no entra a la imagen.

Reglas que importan (las mismas que aplica el demonio):
- Gana el ÚLTIMO patrón que coincide; `!` re-incluye.
- Un archivo también coincide si coincide cualquiera de sus carpetas padre.
- `*` y `?` no cruzan `/`; `**` sí.
- Un `/` final no cambia el patrón (`backups/` == `backups`).
- Un `#` solo es comentario al inicio de línea; después de un patrón forma parte de él.
"""

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _regex(patron: str) -> re.Pattern:
    out, i = [], 0
    while i < len(patron):
        c = patron[i]
        if c == "*":
            if patron[i:i + 2] == "**":
                i += 2
                if patron[i:i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def leer_patrones(ruta: Path | None = None) -> list[tuple[bool, re.Pattern, str]]:
    """(excluye, regex, patron_original) por cada línea efectiva del archivo."""
    ruta = ruta or RAIZ / ".dockerignore"
    patrones = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        p = linea.strip()
        if not p or p.startswith("#"):
            continue
        excluye = not p.startswith("!")
        p = p.lstrip("!").strip().strip("/")
        patrones.append((excluye, _regex(p), linea.strip()))
    return patrones


def entra_a_la_imagen(ruta: str, patrones=None) -> bool:
    """True si `ruta` (relativa a la raíz del contexto) llega a la imagen."""
    patrones = patrones if patrones is not None else leer_patrones()
    partes = ruta.replace("\\", "/").strip("/").split("/")
    candidatas = ["/".join(partes[:n]) for n in range(1, len(partes) + 1)]
    excluida = False
    for excluye, rx, _ in patrones:
        if any(rx.match(c) for c in candidatas):
            excluida = excluye
    return not excluida
