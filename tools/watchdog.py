#!/usr/bin/env python
"""
tools/watchdog.py — monitor COMPLETO para el LIVE TEST integral (el usuario, 07-18).

Principio (corregido a pedido de el usuario): analiza TODO, no se pierde NADA. Cada
línea se ingiere y clasifica; lo que no matchea un patrón conocido NO se descarta
— cae a un catch-all con detección heurística de cualquier cosa que huela a
problema (error/fail/timeout/403/5xx/drift…), aunque no la hayamos previsto. El
log crudo completo queda SIEMPRE en disco para drill-down; el digest es una VISTA
agregada SIN pérdida (líneas idénticas colapsan a un conteo; las distintas se
retienen todas), no un reemplazo ni un muestreo.

Eficiencia: una sola pasada O(n); las líneas repetidas se deduplican a un
contador, así la memoria queda acotada por la cardinalidad de líneas distintas.

Consume de la misma corrida:
  - eventos de métrica estructurados (metrics.py): {"event_type":"metric","metric":
    ...,"value":...,"labels":{...}}
  - toda línea de log, con o sin marcador ([ERROR], [NULL-SRP], [JAR], [SALVAGE],
    [30b], [LLM-FALLBACK], [CAMOUFOX], [CARDS]…), Y lo que no tenga marcador.

Uso:
  python tools/watchdog.py run.log                      # digest de un log escrito
  python tools/watchdog.py run.log --full               # + detalle exhaustivo
  python tools/watchdog.py run.log --watch --interval 15  # en vivo
  uvicorn api:app --port 8000 --workers 1 > run.log 2>&1  # (en otra terminal)

La lógica de parseo/agregación es pura e importable → testeable sin correr nada.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

# ── Severidad ────────────────────────────────────────────────────────────────
CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"

# Métricas cuyo disparo es una señal a mirar (default: INFO).
_METRIC_SEVERITY = {
    "llm_fallback_hallucinated_url": WARN,   # el LLM inventó URLs (30a las filtra)
    "drift_snapshot_saved":          WARN,   # drift real detectado
    "camoufox_scraper_missing":      WARN,   # board necesita camoufox y no hay
    "ingest_records_dropped_total":  WARN,
    "jobs_salvaged_to_review":       INFO,   # 30a rescató a revisión manual
    "url_auto_repaired":             INFO,   # 30b reparó una URL
    "llm_fallback_jobs_recovered":   INFO,
    "jobs_filtered_off_topic":       INFO,
    # v3 Fase 4 — métricas del lado LLM y de nodos del grafo.
    "node_failed_total":             CRITICAL,  # un nodo se cayó y degradó
    "llm_provider_cooldown_total":   WARN,      # 429 real, el proveedor quedó fuera
    "llm_self_correction_total":     INFO,      # el modelo tuvo que reparar su JSON
    "llm_call_latency_ms":           INFO,
    "llm_tokens_total":              INFO,
    "node_exec_ms":                  INFO,
    # llm_fallback_depth NO va acá: su severidad depende del VALOR, no del nombre.
    # Ver _severity_by_value.
}

# Métricas cuya severidad depende del valor, no del nombre. _METRIC_SEVERITY es un
# lookup por nombre y no puede expresar un umbral.
#
# El caso que lo motiva: llm_fallback_depth=0 significa que el proveedor primario
# contestó — la corrida normal, ruido puro. Un 3 significa que tres proveedores fallaron
# antes de que alguno respondiera, y eso es exactamente lo que hay que ver cuando la
# corrida del martes tardó el triple.
_METRIC_VALUE_THRESHOLDS = {
    "llm_fallback_depth": ((3, CRITICAL), (1, WARN)),   # (mínimo, severidad), desc
}


def _severity_by_value(name: str, value: float) -> Optional[str]:
    """Severidad por umbral de valor, o None si la métrica no usa umbrales."""
    rules = _METRIC_VALUE_THRESHOLDS.get(name)
    if not rules:
        return None
    for threshold, severity in rules:
        if value >= threshold:
            return severity
    return INFO

# Marcadores de log conocidos → (regex, severidad, etiqueta). Orden = prioridad.
_LOG_MARKERS = [
    (re.compile(r"\bTraceback\b|\bException\b|\[ERROR\]|raised:", re.I), CRITICAL, "error"),
    (re.compile(r"\b5\d\d\b.*(error|internal)|internal server error", re.I), CRITICAL, "http-5xx"),
    (re.compile(r"\[NULL-SRP\]"),                                       WARN, "null-srp"),
    (re.compile(r"breaker|circuit.*open|\bOPEN\b", re.I),              WARN, "breaker"),
    (re.compile(r"\[CAMOUFOX\].*(skip|error)", re.I),                  WARN, "camoufox-skip"),
    (re.compile(r"\[VAULT\].*(fail|error)", re.I),                     WARN, "vault-fail"),
    (re.compile(r"\[SALVAGE\]"),                                        INFO, "salvage"),
    (re.compile(r"\[JAR\]"),                                            INFO, "jar"),
    (re.compile(r"\[30b\]"),                                            INFO, "url-repair"),
    (re.compile(r"\[CARDS\]"),                                          INFO, "cards"),
    (re.compile(r"\[LLM-FALLBACK\]"),                                   INFO, "llm-fallback"),
    (re.compile(r"\[BLITZ\]|\[PROBE\]|\[VAULT\]|\[CAMOUFOX\]"),         INFO, "pipeline"),
]

# Heurística de peligro para líneas SIN marcador conocido — que nada se escape.
# CRÍTICO: fallas duras / trazas. WARN: señales blandas.
# NOTA (live test 07-18): NO usar `\b5\d\d\b` a secas acá — los milisegundos de
# los timestamps de logging (",534") matchean y todo INFO se vuelve CRIT. El 5xx
# real ya lo cubre _LOG_MARKERS (exige contexto error/internal).
_DANGER_CRIT = re.compile(
    r"\btraceback\b|\bexception\b|\bcritical\b|\bfatal\b|\bpanic\b|"
    r"\bunhandled\b|segfault|\bassert(ionerror)?\b", re.I)
_DANGER_WARN = re.compile(
    r"\berror\b|\bfail(ed|ure)?\b|\btimed?out\b|\btimeout\b|refused|denied|"
    r"\b403\b|\b429\b|unauthorized|forbidden|retry|dropped|null|drift|"
    r"hallucinat|stale|warn", re.I)

_METRIC_RE = re.compile(r'\{.*"event_type"\s*:\s*"metric".*\}')
_DOMAIN_RE = re.compile(r"\b([a-z0-9-]+\.[a-z]{2,}(?:\.[a-z]{2,})?)\b")


@dataclass
class Event:
    severity: str
    kind: str                    # "metric:<name>", la etiqueta del marcador, o "unclassified"
    domain: Optional[str] = None
    value: float = 1.0
    message: str = ""            # línea recortada, retenida para WARN/CRIT y unclassified
    classified: bool = True      # False = no matcheó ningún patrón conocido


def _domain_in(line: str) -> Optional[str]:
    m = _DOMAIN_RE.search(line)
    return m.group(1) if m else None


def parse_line(line: str) -> Optional[Event]:
    """Convierte una línea en un Event. Devuelve None SOLO para líneas en blanco
    (verdadero vacío). TODA línea con contenido produce un Event — si no matchea
    nada conocido, cae a 'unclassified' con severidad heurística y se RETIENE.
    Pura."""
    line = line.rstrip("\n")
    if not line.strip():
        return None

    # 1) evento de métrica estructurado
    m = _METRIC_RE.search(line)
    if m:
        try:
            obj = json.loads(m.group())
            name = obj.get("metric", "?")
            labels = obj.get("labels") or {}
            value = float(obj.get("value", 1) or 0)
            severity = _severity_by_value(name, value)
            if severity is None:
                severity = _METRIC_SEVERITY.get(name, INFO)
            return Event(
                severity=severity,
                kind=f"metric:{name}",
                domain=labels.get("domain"),
                value=value,
            )
        except Exception:
            pass  # JSON corrupto → sigue; se clasificará por marcador/heurística

    # 2) marcador de log conocido
    for rx, severity, label in _LOG_MARKERS:
        if rx.search(line):
            return Event(
                severity=severity,
                kind=label,
                domain=_domain_in(line),
                message=line.strip()[:240] if severity in (CRITICAL, WARN) else "",
            )

    # 3) SIN marcador conocido — NO se descarta. Heurística de peligro + retención.
    if _DANGER_CRIT.search(line):
        sev = CRITICAL
    elif _DANGER_WARN.search(line):
        sev = WARN
    else:
        sev = INFO
    return Event(
        severity=sev,
        kind="unclassified",
        domain=_domain_in(line),
        message=line.strip()[:240],
        classified=False,
    )


@dataclass
class Digest:
    """Agrega Events SIN pérdida. Todo conteo reconcilia con las líneas vistas."""
    lines_seen: int = 0
    blank: int = 0
    events: int = 0
    by_severity: Counter = field(default_factory=Counter)
    by_kind: Counter = field(default_factory=Counter)
    metric_totals: Counter = field(default_factory=Counter)
    by_domain: Counter = field(default_factory=Counter)
    null_domains: Counter = field(default_factory=Counter)
    card_domains: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)          # msg CRÍTICO → veces
    warnings: Counter = field(default_factory=Counter)        # msg WARN → veces
    unclassified: Counter = field(default_factory=Counter)    # línea distinta → veces

    def add(self, line_or_event) -> None:
        # acepta str (línea cruda) o Event ya parseado
        if isinstance(line_or_event, Event) or line_or_event is None:
            self.lines_seen += 1
            ev = line_or_event
        else:
            self.lines_seen += 1
            ev = parse_line(line_or_event)
        if ev is None:
            self.blank += 1
            return
        self.events += 1
        self.by_severity[ev.severity] += 1
        self.by_kind[ev.kind] += 1
        if ev.kind.startswith("metric:"):
            self.metric_totals[ev.kind[7:]] += ev.value
        if ev.domain:
            self.by_domain[ev.domain] += 1
            if ev.kind == "null-srp":
                self.null_domains[ev.domain] += 1
            if ev.kind == "cards":
                self.card_domains[ev.domain] += 1
        if ev.severity == CRITICAL and ev.message:
            self.errors[ev.message] += 1
        elif ev.severity == WARN and ev.message and not ev.classified:
            self.warnings[ev.message] += 1
        if not ev.classified:
            self.unclassified[ev.message or "(vacío)"] += 1

    def reconciles(self) -> bool:
        """Invariante de no-pérdida: cada línea está contada en algún lado."""
        return self.lines_seen == self.events + self.blank

    def render(self, full: bool = False) -> str:
        out = []
        c = self.by_severity
        recon = "OK" if self.reconciles() else "‼ DESCUADRE"
        out.append(
            f"═ WATCHDOG DIGEST ═ {self.lines_seen} líneas "
            f"({self.events} eventos + {self.blank} vacías, reconcilia:{recon}) · "
            f"{c[CRITICAL]} CRIT · {c[WARN]} WARN · {c[INFO]} info"
        )
        # Errores: TODOS los distintos (sin cap por defecto — no perder nada).
        if self.errors:
            out.append(f"  ⛔ ERRORES CRÍTICOS ({len(self.errors)} distintos):")
            items = self.errors.most_common(None if full else 25)
            for msg, n in items:
                out.append(f"     ×{n}  {msg}")
            if not full and len(self.errors) > 25:
                out.append(f"     … +{len(self.errors) - 25} más (usá --full)")
        # boards mudos: NULL-SRP sin cards → sospechoso (anti-bot/selector stale)
        mute = [d for d in self.null_domains if d not in self.card_domains]
        if mute:
            out.append(f"  ⚠ boards con NULL-SRP y 0 cards: {', '.join(sorted(mute))}")
        if self.metric_totals:
            top = ", ".join(f"{k}={int(v)}" for k, v in self.metric_totals.most_common(None))
            out.append(f"  ▸ métricas (todas): {top}")
        if self.card_domains:
            got = ", ".join(f"{d}:{n}" for d, n in self.card_domains.most_common(None))
            out.append(f"  ✓ boards con cards: {got}")
        warn_kinds = {k: v for k, v in self.by_kind.items()
                      if k in ("breaker", "camoufox-skip", "vault-fail", "null-srp")}
        if warn_kinds:
            out.append("  ▸ warnings por tipo: " +
                       ", ".join(f"{k}={v}" for k, v in sorted(warn_kinds.items())))
        # Sin clasificar: lo que ningún patrón previó — SIEMPRE se muestra.
        if self.unclassified:
            danger = sum(1 for m in self.unclassified
                         if _DANGER_CRIT.search(m) or _DANGER_WARN.search(m))
            out.append(
                f"  ❓ sin clasificar: {sum(self.unclassified.values())} líneas "
                f"({len(self.unclassified)} distintas, {danger} con señal de peligro)"
            )
            flagged = [(m, n) for m, n in self.unclassified.items()
                       if _DANGER_CRIT.search(m) or _DANGER_WARN.search(m)]
            for msg, n in sorted(flagged, key=lambda x: -x[1])[:None if full else 15]:
                out.append(f"     ×{n}  {msg}")
            if full:
                out.append("  ── sin clasificar (todas las distintas) ──")
                for msg, n in self.unclassified.most_common(None):
                    out.append(f"     ×{n}  {msg}")
        return "\n".join(out)


def build_digest(lines) -> Digest:
    d = Digest()
    for line in lines:
        d.add(line)
    return d


def _tail(path: str, digest: Digest, interval: float, full: bool) -> None:
    """Sigue el archivo, refrescando el digest cada `interval` s. Ctrl-C corta."""
    with open(path, encoding="utf-8", errors="replace") as f:
        last_emit = 0.0
        while True:
            line = f.readline()
            if line:
                digest.add(line)
            else:
                now = time.time()
                if now - last_emit >= interval:
                    print("\n" + digest.render(full=full), flush=True)
                    last_emit = now
                time.sleep(0.4)


def main(argv=None) -> int:
    # Windows: la consola por defecto es cp1252 y los glifos del digest (═ ⛔ …)
    # la harían crashear. Nunca morir por la propia salida.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Watchdog completo del live test")
    ap.add_argument("logfile", help="archivo de log a leer (o '-' para stdin)")
    ap.add_argument("--watch", action="store_true", help="seguir el archivo en vivo")
    ap.add_argument("--interval", type=float, default=15.0, help="segundos entre digests")
    ap.add_argument("--full", action="store_true", help="detalle exhaustivo, sin caps")
    args = ap.parse_args(argv)

    if args.watch and args.logfile != "-":
        digest = Digest()
        try:
            _tail(args.logfile, digest, args.interval, args.full)
        except KeyboardInterrupt:
            print("\n" + digest.render(full=args.full))
        return 0

    src = sys.stdin if args.logfile == "-" else open(args.logfile, encoding="utf-8", errors="replace")
    try:
        digest = build_digest(src)
    finally:
        if src is not sys.stdin:
            src.close()
    print(digest.render(full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
