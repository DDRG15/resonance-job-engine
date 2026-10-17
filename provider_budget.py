"""
provider_budget.py — contar antes de llamar, no castigar después.

GEMA se entera de que un proveedor está limitado chocándolo. `_cooldowns`
(`nlp_engine.py`) pone 60 segundos de castigo **después** de comerse el 429, así que
cada descubrimiento cuesta una llamada perdida, un round-trip de latencia y un salto de
fallback. El 2026-09-03, midiendo sobrecosto de razonamiento, sambanova devolvió 429 en
el primer intento y cerebras 402 Payment Required — y los dos estaban asignados como
primarios de un propósito. La cuenta no se lleva; se paga.

Sale barato porque la data ya está: `llm_calls` graba `provider`, `model`,
`prompt_tokens`, `completion_tokens`, `outcome` y `created_at` en cada llamada desde el
2026-09-03. Los cuatro contadores son queries sobre una tabla que ya se escribe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Uso:
    """Lo consumido en las dos ventanas que declaran los free tiers."""
    rpm: int = 0    # requests por minuto
    rpd: int = 0    # requests por día
    tpm: int = 0    # tokens por minuto
    tpd: int = 0    # tokens por día


@dataclass(frozen=True)
class Limite:
    """
    Los topes publicados de un proveedor, **con fecha**.

    La fecha no es decorativa. Un límite sin fecha es un número que va a estar mal y
    nadie va a saber desde cuándo — el mismo criterio que las tasas de cambio de la
    Fase 4. `None` en un campo significa «no declarado», y un campo no declarado
    **nunca bloquea**: el presupuesto restringe donde hay dato, no donde falta.
    """
    rpm: Optional[int] = None
    rpd: Optional[int] = None
    tpm: Optional[int] = None
    tpd: Optional[int] = None
    fecha: str = ""
    nota: str = ""


#: Topes de free tier, verificados contra la documentación pública el 2026-09-17.
#:
#: Solo entran los que están publicados. Inventar un tope para tener la fila completa
#: sería peor que dejarla vacía: un límite inventado bloquea llamadas que sí se podían
#: hacer, y nadie va a sospechar del número porque parece dato.
LIMITES: dict[str, Limite] = {
    "groq":     Limite(rpm=30,  rpd=14400, tpm=6000,   fecha="2026-09-17",
                       nota="free tier publicado"),
    "gemini":   Limite(rpm=15,  rpd=1500,  tpm=1000000, fecha="2026-09-17",
                       nota="flash-lite free tier"),
    "mistral":  Limite(rpm=60,             fecha="2026-09-17", nota="free tier"),
    "cohere":   Limite(rpm=20,  rpd=1000,  fecha="2026-09-17", nota="trial key"),
    # openrouter, cerebras y sambanova no publican topes estables para el free tier.
    # Sin dato declarado, no se restringen — se descubren por 429 como antes.
}

#: Por encima de esto se saltea el proveedor sin gastar la llamada. No 1.0: el
#: contador mira lo que YA se gastó, y entre la consulta y la llamada puede entrar
#: otra. Dejar margen evita chocar el tope justo al final de la ventana.
UMBRAL = 0.95


def uso_desde_llm_calls(db, provider: str, ahora: Optional[datetime] = None) -> Uso:
    """
    Cuenta lo consumido leyendo `llm_calls`. Sin tabla nueva y sin contadores en
    memoria que se pierdan al reiniciar.

    Solo cuenta llamadas que **salieron**: un `auth_error` no consumió cuota del
    proveedor, así que descontarlo del presupuesto sería castigarse dos veces por el
    mismo problema.
    """
    ahora = ahora or datetime.now(timezone.utc)
    hace_1m = (ahora - timedelta(minutes=1)).isoformat()
    hace_1d = (ahora - timedelta(days=1)).isoformat()

    try:
        with db._conn() as conn:          # type: ignore[attr-defined]
            fila = conn.execute(
                "SELECT "
                " SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), "
                " SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), "
                " SUM(CASE WHEN created_at >= ? THEN "
                "      COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) "
                "      ELSE 0 END), "
                " SUM(CASE WHEN created_at >= ? THEN "
                "      COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) "
                "      ELSE 0 END) "
                "FROM llm_calls "
                "WHERE provider = ? AND COALESCE(outcome,'') != 'auth_error'",
                (hace_1m, hace_1d, hace_1m, hace_1d, provider),
            ).fetchone()
    except Exception as exc:
        # Que el presupuesto no se pueda leer no puede impedir una llamada: se degrada
        # al comportamiento anterior, que es descubrir el limite chocandolo.
        logger.debug("[BUDGET] no se pudo leer el uso de %s: %s", provider, exc)
        return Uso()

    if fila is None:
        return Uso()
    return Uso(rpm=fila[0] or 0, rpd=fila[1] or 0,
               tpm=fila[2] or 0, tpd=fila[3] or 0)


def fraccion_consumida(uso: Uso, limite: Limite) -> float:
    """La ventana más apretada de las cuatro. Un proveedor al 20% de su RPD pero al
    99% de su RPM está agotado ahora mismo: el mínimo no sirve, manda el máximo."""
    fracciones = [
        (uso.rpm / limite.rpm) if limite.rpm else 0.0,
        (uso.rpd / limite.rpd) if limite.rpd else 0.0,
        (uso.tpm / limite.tpm) if limite.tpm else 0.0,
        (uso.tpd / limite.tpd) if limite.tpd else 0.0,
    ]
    return max(fracciones) if fracciones else 0.0


def hay_presupuesto(db, provider: str, umbral: float = UMBRAL) -> tuple[bool, float]:
    """
    `(alcanza, fracción_consumida)`.

    Un proveedor **sin límite declarado siempre alcanza**. El presupuesto restringe
    donde hay dato; donde falta, el comportamiento es el de antes.
    """
    limite = LIMITES.get(provider)
    if limite is None:
        return True, 0.0
    frac = fraccion_consumida(uso_desde_llm_calls(db, provider), limite)
    return frac < umbral, frac
