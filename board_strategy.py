"""
board_strategy.py — Per-board scraping strategy registry (Fase 0).

ARCHITECTURAL ROLE:
    The single shared contract that the three resilience layers write to.
    Defining it FIRST is what keeps 27/28/29 from refactoring each other:

        Phase 27 (extraction) writes  -> extraction_mode
        Phase 28 (access)     writes  -> protection, access_vector, cookies_ref
        Phase 29 (self-heal)  reads   -> health, last_probed (to decide re-probes)

SAFE DEFAULTS:
    Every board starts in the free/CSS path: access_vector=direct,
    extraction_mode=css, protection=unknown, health=healthy. Nothing escalates
    to paid vectors or the LLM fallback until a layer explicitly writes it.

PERSISTENCE:
    Stored as JSON (idempotent, diffable, versionable). A bare read never
    writes to disk; only update()/seed() persist. This keeps the scraper's
    hot path free of accidental I/O.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

import config


# ── Constrained vocabularies (validated by pydantic on assignment) ───────────

class Protection(str, Enum):
    UNKNOWN = "unknown"
    NONE = "none"
    CLOUDFLARE = "cloudflare"
    RATE_LIMIT = "rate_limit"
    JS_CHALLENGE = "js_challenge"


class AccessVector(str, Enum):
    DIRECT = "direct"            # plain request, no anti-bot
    # OJO: DIRECT significa "navegador sin evasion", NO "sin navegador". Abre una
    # pestana de Chromium igual que CAMOUFOX. El unico vector que no lanza un
    # navegador es ENDPOINT.
    ENDPOINT = "endpoint"        # GET a un endpoint publico — CERO navegador (Fase 1)
    CAMOUFOX = "camoufox"        # Firefox fork — free Cloudflare bypass
    EXTERNAL_PROXY = "external_proxy"  # optional future slot, vendor-agnostic —
    # reserved for a hard-board vector GEMA does NOT yet use. No provider is
    # committed (AnyIP was a concept reference only). Keeps the router extensible.


class ExtractionMode(str, Enum):
    CSS = "css"                # selector extraction, 0 tokens (default)
    STRUCTURED = "structured"  # campos nombrados de un endpoint — 0 tokens, 0 selectores
    LLM_FALLBACK = "llm_fallback"  # drift → LLM reads text (Phase 27)


class AccessPolicy(str, Enum):
    """Que permite el sitio, declarado por el sitio.

    No es una opinion legal: es lo que dice su robots.txt y lo que devolvio cuando se
    lo probo. Existe para que un board que no se puede scrapear deje de reaparecer
    como un fallo tecnico que alguien va a intentar arreglar por cuarta vez.
    """
    OPEN = "open"                      # robots.txt permite el path
    PERSONAL_USE_ONLY = "personal_use_only"   # ToS restrictivo, uso personal
    AUTH_REQUIRED = "auth_required"    # muro de login: no hay nada que rescatar sin auth
    RATE_LIMITED = "rate_limited"      # responde 429 al primer pedido
    DECLINED = "declined"              # no se scrapea, y la razon esta escrita


class Health(str, Enum):
    HEALTHY = "healthy"
    PROBATION = "probation"    # recipe just changed — verify next run (Phase 29)
    DEGRADED = "degraded"      # failing — needs re-probe (Phase 29)


class BoardStrategy(BaseModel):
    """Complete scraping strategy for one board domain. All fields default to
    the free/CSS path so an unseen board is safe out of the box."""

    model_config = ConfigDict(validate_assignment=True)

    domain: str
    protection: Protection = Protection.UNKNOWN
    access_vector: AccessVector = AccessVector.DIRECT
    cookies_ref: Optional[str] = None
    extraction_mode: ExtractionMode = ExtractionMode.CSS
    health: Health = Health.HEALTHY
    last_probed: Optional[float] = None                       # epoch seconds
    last_updated: float = Field(default_factory=lambda: time.time())

    # ── Fase 1 (2026-09-11): ingesta por endpoint publico ────────────────────
    # Solo se leen cuando access_vector == ENDPOINT. Un board que no lo use queda
    # exactamente como estaba: los defaults son inertes.
    endpoint_template: Optional[str] = None
    """URL del endpoint. Con `{title}` se pide una vez por titulo buscado; sin
    `{title}` es un feed fijo y se pide una sola vez (remoteok.com/api no acepta
    busqueda: devuelve los ultimos avisos y el filtrado pasa del lado de GEMA)."""

    response_format: str = "json"
    """`json` o `rss`. Determina el parser, no el Content-Type que declare el server —
    hay feeds servidos como text/html que son XML perfectamente valido."""

    items_path: str = ""
    """Donde vive la lista dentro del JSON, con puntos (`"data.jobs"`). Vacio deja que
    el parser pruebe las formas conocidas: lista suelta, `jobs`, `results`, `postings`."""

    field_map: dict = Field(default_factory=dict)
    """Campo de JobResult -> path en la respuesta. El limite es deliberado (riesgo R2
    del plan): mapeo plano y `path` con puntos, nada mas. Cualquier transformacion real
    va en codigo — un formato declarativo sin techo termina siendo un lenguaje."""

    crawl_delay_s: float = 0.0
    """Segundos entre pedidos, declarados por el robots.txt del sitio. api.lever.co
    pide 1. Es un limite, no una sugerencia."""

    # ── Fase 9 (2026-09-17): politica de acceso declarada ────────────────────
    access_policy: AccessPolicy = AccessPolicy.OPEN
    policy_reason: str = ""
    """Por que. Una politica sin razon escrita se vuelve a cuestionar cada vez que
    alguien mira la lista de boards muertos — que es exactamente lo que paso con
    remote.co y welcometothejungle.com entre julio y septiembre."""

    titulo_trae_empresa: bool = False
    """El feed mete la empresa en el titulo (`"Reddit: Backend Engineer"`) porque no
    tiene campo propio. Medido en weworkremotely.com: 87 de 87 items. Transformacion
    con nombre, activada por un booleano — el `field_map` mapea, el codigo transforma."""

    ubicacion_es_lista: bool = False
    """La ubicacion llega como LISTA de paises admitidos (Himalayas,
    `locationRestrictions`): se une con "; " y la lista vacia se lee "Worldwide",
    porque una lista de restricciones vacia es la ausencia de restriccion. Ver
    `endpoint_scraper._ubicacion_de_lista`. , Fase 1."""

    def is_stale(self, ttl_seconds: float) -> bool:
        """True if the strategy was never probed or its last probe is older
        than ttl. Phase 28/29 use this to decide when to re-probe a board."""
        if self.last_probed is None:
            return True
        return (time.time() - self.last_probed) > ttl_seconds


class BoardStrategyRegistry:
    """Loads/persists BoardStrategy records keyed by domain. get() is read-only
    (auto-creates a safe default in memory without touching disk); update() and
    seed_from_domains() are the only methods that write."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.BOARD_STRATEGY_PATH)
        self._strategies: dict[str, BoardStrategy] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for domain, data in raw.items():
                self._strategies[domain] = BoardStrategy.model_validate(data)
        self._aplicar_endpoints()

    def _aplicar_endpoints(self) -> None:
        """
        Aplica las definiciones de `endpoint_registry` EN MEMORIA, sin escribir disco.

        La division es deliberada: `board_strategy.json` es estado local —salud,
        ultimo probe— y esta gitignoreado, asi que se pierde al clonar. Que un board
        exponga una API publica no es estado de una maquina: es conocimiento del
        proyecto, y vive en un archivo commiteado. Si viviera solo en el JSON, un clon
        limpio volveria a abrir Chromium para leer un endpoint que ya sabemos que existe.

        Pisa el `access_vector` guardado a proposito. El JSON puede decir `direct`
        porque se escribio antes de que el board tuviera endpoint; el registro
        commiteado es la fuente de verdad sobre la capacidad. La salud y el ultimo
        probe se conservan.

        No escribe: un `_load` que persiste convierte cada arranque en una escritura.
        """
        try:
            import endpoint_registry
        except Exception:      # el modulo es opcional: sin el, todo sigue como antes
            return
        # La politica se aplica a TODOS los boards que tengan una escrita, tengan
        # endpoint o no: remote.co y welcometothejungle.com no tienen endpoint y son
        # justamente los dos que hay que dejar de intentar.
        for domain, pol in getattr(endpoint_registry, "POLITICAS", {}).items():
            strat = self._strategies.get(domain) or BoardStrategy(domain=domain)
            for campo, valor in pol.items():
                if campo in BoardStrategy.model_fields:
                    setattr(strat, campo, valor)
            self._strategies[domain] = strat

        for domain, cfg in endpoint_registry.ENDPOINTS.items():
            strat = self._strategies.get(domain) or BoardStrategy(domain=domain)
            strat.access_vector = AccessVector.ENDPOINT
            strat.extraction_mode = ExtractionMode.STRUCTURED
            for campo, valor in cfg.items():
                if campo in BoardStrategy.model_fields:
                    setattr(strat, campo, valor)
            self._strategies[domain] = strat

    def _save(self) -> None:
        data = {
            domain: strat.model_dump(mode="json")
            for domain, strat in self._strategies.items()
        }
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def get(self, domain: str) -> BoardStrategy:
        """Return the strategy for a domain, creating a safe default in memory
        if unseen. Does NOT persist — a bare read never writes."""
        if domain not in self._strategies:
            self._strategies[domain] = BoardStrategy(domain=domain)
        return self._strategies[domain]

    def update(self, domain: str, **fields) -> BoardStrategy:
        """Update fields on a board's strategy and persist. Unknown fields raise;
        enum fields are validated by pydantic (validate_assignment)."""
        strat = self.get(domain)
        for key, value in fields.items():
            if key not in BoardStrategy.model_fields:
                raise AttributeError(f"BoardStrategy has no field {key!r}")
            setattr(strat, key, value)  # validates enum/type on assignment
        strat.last_updated = time.time()
        self._save()
        return strat

    def seed_from_domains(self, domains: list[str]) -> None:
        """Seed missing boards with safe defaults (direct/css) and persist.
        Existing entries are left untouched. Call once with the domains from
        selectors_registry so every known board has a strategy row."""
        changed = False
        for domain in domains:
            if domain not in self._strategies:
                self._strategies[domain] = BoardStrategy(domain=domain)
                changed = True
        if changed:
            self._save()

    def all_domains(self) -> list[str]:
        return list(self._strategies.keys())
