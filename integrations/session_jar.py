"""
session_jar.py — Cookie Jar auto-persistente (2026-07-18).

Distinto del CookieVault (cookies de login manuales, exportadas de Chrome). El
SessionJar es AUTOMÁTICO: captura las cookies del navegador DESPUÉS de un scrape
y las reusa en la corrida siguiente, para que el clearance anti-bot (Cloudflare
`cf_clearance`, `__cf_bm`, etc.) sobreviva SIN exportar nada ni usar credenciales.

Por qué ataca el anti-bot barato: Cloudflare emite `cf_clearance` DESPUÉS de que
el navegador pasa el challenge UNA vez. Si persistimos esa cookie, las visitas
siguientes se saltan el desafío. No es un login — es un ticket de clearance.

Letra chica (documentada, no un bug): `cf_clearance` suele estar atado a
IP + User-Agent → vida limitada, no transferible entre máquinas/IPs. Sirve en la
MISMA máquina por horas/días. Por eso el jar es best-effort: si la cookie ya
expiró, no ayuda, pero tampoco rompe nada.

Archivos: cookies/session/<domain>.json (auto-gestionados, gitignored). Separados
de cookies/<domain>.json (manuales del CookieVault) para no pisarlos nunca.

Todo fail-safe: una falla de captura o inyección se loguea y el scrape sigue.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from rutas import ruta_datos

logger = logging.getLogger(__name__)

# Cookies que valen la pena persistir para saltar anti-bot. Guardamos solo las
# de los dominios que scrapeamos (evita basura de terceros). Prefijos conocidos
# de clearance/anti-bot para loguear cuántas capturamos de las útiles.
_CLEARANCE_HINTS = ("cf_clearance", "__cf_bm", "cf_chl", "_cfuvid", "datadome",
                    "incap_ses", "visid_incap", "ak_bmsc", "bm_sv")


class SessionJar:
    """Persistencia automática de cookies de sesión/clearance por dominio."""

    def __init__(self, jar_dir: Optional[str] = None) -> None:
        if jar_dir is None:
            jar_dir = str(ruta_datos("cookies", "session"))
        self._dir = Path(jar_dir)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def inject(self, domain: str, context) -> int:
        """Inyecta las cookies auto-guardadas del dominio en el context.
        Retorna cuántas inyectó (0 si no hay archivo). Nunca lanza."""
        path = self._path(domain)
        if not path.exists():
            return 0
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cookies, list) or not cookies:
                return 0
            await context.add_cookies(cookies)
            logger.info("[SessionJar] Reusé %d cookies de sesión para %s",
                        len(cookies), domain)
            return len(cookies)
        except Exception as exc:
            logger.warning("[SessionJar] No pude inyectar %s: %s", domain, exc)
            return 0

    async def capture(self, context, domains: list[str]) -> int:
        """Captura del context las cookies de los dominios dados y las persiste,
        una por archivo cookies/session/<domain>.json. Retorna cuántas guardó en
        total. Nunca lanza — es best-effort."""
        try:
            all_cookies = await context.cookies()
        except Exception as exc:
            logger.warning("[SessionJar] No pude leer cookies del context: %s", exc)
            return 0

        saved_total = 0
        for domain in domains:
            group = [c for c in all_cookies if self._belongs(c, domain)]
            if not group:
                continue
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                self._path(domain).write_text(
                    json.dumps(group, ensure_ascii=False), encoding="utf-8"
                )
                clearance = sum(
                    1 for c in group
                    if any(c.get("name", "").startswith(h) for h in _CLEARANCE_HINTS)
                )
                logger.info(
                    "[SessionJar] Guardé %d cookies para %s (%d de clearance)",
                    len(group), domain, clearance,
                )
                saved_total += len(group)
            except Exception as exc:
                logger.warning("[SessionJar] No pude guardar %s: %s", domain, exc)
        return saved_total

    # -------------------------------------------------------------------------
    # Private
    # -------------------------------------------------------------------------

    def _path(self, domain: str) -> Path:
        clean = domain.lstrip(".")
        return self._dir / f"{clean}.json"

    @staticmethod
    def _belongs(cookie: dict, domain: str) -> bool:
        """True si la cookie pertenece al dominio del board (o un subdominio).
        El cookie domain de Cloudflare es el del board (ej. '.wellfound.com')."""
        cdom = (cookie.get("domain") or "").lstrip(".").lower()
        board = domain.lstrip(".").lower()
        return cdom == board or cdom.endswith("." + board)
