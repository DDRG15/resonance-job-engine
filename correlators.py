"""
correlators.py — la identidad de un aviso deja de ser un campo y pasa a ser un pipeline.

Fase 11 del . Idea tomada del Version Tracking de Ghidra
(``): patrones, no código.

`compute_hash()` es SHA-256 de la URL canónica, así que un aviso es «el mismo» si y
solo si su URL es la misma. Medido el 2026-09-11 sobre el registry: **1.536 filas,
1.407 pares `(título, empresa)` únicos — 129 de más, 8,4%.** Y la duplicación no es
entre boards (un solo caso en toda la base): es el mismo board relistando el mismo
puesto con IDs nuevos.

    2026-06-05  .../remote-script-writer-martell-media-1132829
    2026-06-05  .../remote-script-writer-martell-media-1132830
    2026-06-05  .../remote-script-writer-martell-media-1132831
    2026-06-05  .../remote-script-writer-martell-media-1132832
    2026-06-05  .../remote-script-writer-martell-media-1132833

Cinco IDs consecutivos, mismo día, mismo título, misma empresa. Un aviso, cinco filas.

Lo que Ghidra resuelve y acá se copia: **no elegir un criterio de identidad, correr
varios.** Cada correlator propone candidatos con su confianza; ninguno decide solo. Y
cuando dos compiten, aceptar uno **bloquea** al rival en vez de borrarlo — el duplicado
sigue ahí como evidencia de que el board relista, y la decisión es reversible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


# ── Estados, calcados del modelo de Version Tracking ─────────────────────────
#
# AVAILABLE  candidato, nadie decidió todavía
# ACCEPTED   es el canónico del grupo
# BLOCKED    perdió contra un canónico — NO se borra
# REJECTED   el humano dijo que no son el mismo aviso
#
# `BLOCKED` es el que importa: encaja con el principio de no descartar data
# rescatable que ya rige el proyecto. Un duplicado borrado no se puede auditar; uno
# bloqueado explica por qué dejó de contarse.
AVAILABLE = "AVAILABLE"
ACCEPTED = "ACCEPTED"
BLOCKED = "BLOCKED"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class Candidato:
    """Dos avisos que un correlator cree que son el mismo."""
    canonico_url: str
    duplicado_url: str
    correlator: str
    confianza: float
    motivo: str = ""


@dataclass(frozen=True)
class Correlator:
    """
    Un criterio de identidad.

    `prioridad` menor gana: cuando dos correlators proponen el mismo par, el de
    prioridad más baja es el que queda registrado. Sin ese desempate, cuál de los dos
    figura dependería del orden de iteración de un dict.
    """
    nombre: str
    descripcion: str
    prioridad: int
    confianza: float
    fn: Callable[[list[dict]], list[Candidato]] = field(repr=False)


# ── Normalización compartida ─────────────────────────────────────────────────

def _norm(texto: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (texto or "")).strip().lower()


def _dia(iso: Optional[str]) -> str:
    return (iso or "")[:10]


#: `.../remote-script-writer-martell-media-1132829` -> (prefijo, 1132829)
_SUFIJO_NUM = re.compile(r"^(.*?)[-/](\d{4,})/?$")


def _prefijo_y_id(url: str) -> Optional[tuple[str, int]]:
    m = _SUFIJO_NUM.match((url or "").rstrip("/"))
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2))


# ── Los correlators ──────────────────────────────────────────────────────────

def _por_titulo_empresa_dia(filas: list[dict]) -> list[Candidato]:
    """
    Mismo título, misma empresa, mismo board, mismo día.

    Inequívoco en la práctica: un board no publica dos veces el mismo puesto de la
    misma empresa el mismo día salvo que sea el mismo puesto. El caso peor de la base
    —`principal backend engineer` en `jeeves`— son cuatro filas así en un día.

    El canónico es el de URL más corta, no el más viejo: cuando un board relista, la
    URL suele crecer con sufijos de tracking, y `scraped_at` empata porque es el mismo
    día. Un criterio determinista importa más que cuál gane.
    """
    grupos: dict[tuple, list[dict]] = {}
    for f in filas:
        if not _norm(f.get("title")) or not _norm(f.get("company")):
            continue
        clave = (_norm(f.get("title")), _norm(f.get("company")),
                 f.get("source_domain"), _dia(f.get("scraped_at")))
        grupos.setdefault(clave, []).append(f)

    salida: list[Candidato] = []
    for clave, g in grupos.items():
        if len(g) < 2:
            continue
        g = sorted(g, key=lambda f: (len(f["url"]), f["url"]))
        canonico = g[0]
        for dup in g[1:]:
            salida.append(Candidato(
                canonico["url"], dup["url"], "titulo_empresa_dia", 0.95,
                f"mismo titulo/empresa en {clave[2]} el {clave[3]}"))
    return salida


def _por_id_consecutivo(filas: list[dict]) -> list[Candidato]:
    """
    Mismo prefijo de URL con sufijos numéricos cercanos, y mismo título.

    `...-1132829` a `...-1132833`: cinco IDs consecutivos del mismo día. Que el
    prefijo coincida ya es fuerte —incluye el slug del puesto y la empresa— y exigir
    además que el título coincida evita el caso en que dos puestos distintos de la
    misma empresa caigan en IDs vecinos.

    La ventana de 50 es deliberada: los IDs de un relist son consecutivos o casi, y
    una ventana grande empezaría a agrupar avisos genuinamente distintos publicados el
    mismo día.
    """
    grupos: dict[tuple, list[tuple[int, dict]]] = {}
    for f in filas:
        pid = _prefijo_y_id(f.get("url") or "")
        if pid is None:
            continue
        prefijo, num = pid
        grupos.setdefault((prefijo, _norm(f.get("title"))), []).append((num, f))

    salida: list[Candidato] = []
    for (prefijo, _titulo), g in grupos.items():
        if len(g) < 2:
            continue
        g.sort()
        if g[-1][0] - g[0][0] > 50:
            continue
        canonico = g[0][1]
        for _num, dup in g[1:]:
            salida.append(Candidato(
                canonico["url"], dup["url"], "id_consecutivo", 0.90,
                f"mismo prefijo y titulo, IDs {g[0][0]}..{g[-1][0]}"))
    return salida


def _por_relist_en_otro_dia(filas: list[dict]) -> list[Candidato]:
    """
    Mismo título, misma empresa, mismo board — **en días distintos**.

    Este es el caso ambiguo, y por eso su confianza es baja: no se auto-acepta, va a
    `AVAILABLE` para que lo decida un humano.

    Medido el 2026-09-17 sobre el registry: de 130 duplicados `(título, empresa)`,
    **52 son del mismo día** —inequívocos— y los 78 restantes son relistados en días
    distintos. `principal backend engineer` en `jeeves` son cuatro filas del 2026-07-09
    y cuatro del 2026-09-04.

    ¿Es el mismo puesto o la empresa reabrió la búsqueda dos meses después? No se
    puede saber desde acá. Auto-aceptarlo convertiría un problema de duplicados en uno
    de avisos desaparecidos, que es peor; ignorarlo dejaría el 60% de la duplicación
    sin registrar. Proponerlo con confianza baja es la única respuesta honesta.
    """
    grupos: dict[tuple, list[dict]] = {}
    for f in filas:
        if not _norm(f.get("title")) or not _norm(f.get("company")):
            continue
        clave = (_norm(f.get("title")), _norm(f.get("company")), f.get("source_domain"))
        grupos.setdefault(clave, []).append(f)

    salida: list[Candidato] = []
    for clave, g in grupos.items():
        dias = {_dia(f.get("scraped_at")) for f in g}
        if len(g) < 2 or len(dias) < 2:
            continue     # mismo día: lo cubre el correlator de arriba, con más confianza
        g = sorted(g, key=lambda f: (f.get("scraped_at") or "", f["url"]))
        canonico = g[0]
        for dup in g[1:]:
            if _dia(dup.get("scraped_at")) == _dia(canonico.get("scraped_at")):
                continue
            salida.append(Candidato(
                canonico["url"], dup["url"], "relist_otro_dia", 0.60,
                f"mismo titulo/empresa en {clave[2]}, {len(dias)} dias distintos "
                f"— puede ser una busqueda reabierta"))
    return salida


CORRELATORS: list[Correlator] = [
    # La URL canónica es el criterio que ya existe —`compute_hash`— y por eso no
    # aparece acá: no propone nada, ES la clave primaria. Estos dos son los que
    # encuentran lo que esa clave no ve.
    Correlator(
        nombre="titulo_empresa_dia",
        descripcion="Mismo titulo, empresa, board y dia — un board no publica dos "
                    "veces el mismo puesto el mismo dia salvo que sea el mismo.",
        prioridad=10, confianza=0.95, fn=_por_titulo_empresa_dia,
    ),
    Correlator(
        nombre="id_consecutivo",
        descripcion="Mismo prefijo de URL y titulo, con sufijos numericos cercanos "
                    "— la firma de un board que relista.",
        prioridad=20, confianza=0.90, fn=_por_id_consecutivo,
    ),
    Correlator(
        nombre="relist_otro_dia",
        descripcion="Mismo titulo, empresa y board en dias distintos — puede ser el "
                    "mismo puesto relistado o una busqueda reabierta. Ambiguo por "
                    "definicion: propone, no decide.",
        prioridad=30, confianza=0.60, fn=_por_relist_en_otro_dia,
    ),
]


def correr(filas: Iterable[dict]) -> list[Candidato]:
    """
    Corre todos los correlators y devuelve los candidatos, sin duplicar pares.

    Cuando dos correlators proponen el mismo par gana el de prioridad más baja. Sin
    ese desempate, cuál queda registrado dependería del orden de iteración.
    """
    filas = [f for f in filas if f.get("url")]
    vistos: dict[tuple, Candidato] = {}
    for c in sorted(CORRELATORS, key=lambda c: c.prioridad):
        for cand in c.fn(filas):
            par = tuple(sorted((cand.canonico_url, cand.duplicado_url)))
            if par not in vistos:
                vistos[par] = cand
    return list(vistos.values())
