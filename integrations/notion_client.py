"""
notion_client.py — Notion Kanban Integration for Project GEMA.

Pushes TieredJob results to a Notion database as Kanban cards.
Each card is tagged with its Tier, color-coded, and contains company + link.

Status: Scaffolded — complete interface, TODO internals marked clearly.
"""

import logging
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

import config
import metrics
from models import TieredJob

logger = logging.getLogger(__name__)


class NotionClient:
    """
    Wraps the Notion API for GEMA's Kanban output.

    Database schema expected in Notion:
        Name         (title)   — Job title
        Company      (text)    — Company name
        URL          (url)     — Direct job link
        Tier         (select)  — "Tier 1" | "Tier 2" | "Tier 3"
        Match Score  (number)  — 0–100
        Salary       (text)    — Raw salary string
        Source       (text)    — Job board domain
        Status       (select)  — "To Review" | "Manual Review" | "Applied" | "Interview" | "Rejected"
    """

    # Estado del TOKEN, distinto de "hay una key en el .env". Hasta el 2026-09-30
    # `/api/status` mostraba Notion activo con solo que la key existiera: el token de
    # el usuario estaba vencido y la tarjeta decía que todo andaba. Cuatro estados, y
    # "no verificado" NO es "inválido": sin red o con timeout no se sabe, y declarar
    # roto algo que quizá anda es el mismo error en el sentido opuesto.
    ESTADO_APAGADO = "apagado"              # sin key: la integración no está configurada
    ESTADO_VALIDO = "valido"                # Notion aceptó el token (users/me -> 200)
    ESTADO_INVALIDO = "invalido"            # Notion respondió 401
    ESTADO_NO_VERIFICADO = "no_verificado"  # sin red, timeout, 5xx, 429 o aún sin consultar

    # Atributos de clase y no de __init__: tests/test_notion_client.py construye el
    # cliente con __new__ (sin pasar por __init__) y el estado tiene que existir igual.
    _estado_token: str = ESTADO_NO_VERIFICADO
    _detalle_token: str = "sin verificar"
    _verificado_en: Optional[str] = None

    def __init__(self, api_key: Optional[str] = None, database_id: Optional[str] = None):
        self.api_key = api_key or config.NOTION_API_KEY
        self.database_id = database_id or config.NOTION_DATABASE_ID
        self._client = None
        self._initialize()

    @property
    def estado_token(self) -> str:
        """El estado cacheado del token. Sin key es siempre `apagado`."""
        if not getattr(self, "api_key", ""):
            return self.ESTADO_APAGADO
        return self._estado_token

    def info_token(self) -> dict:
        """Lo que sirve `/api/status`: estado, razón en una línea y cuándo se verificó."""
        return {
            "estado": self.estado_token,
            "detalle": self._detalle_token if self.estado_token != self.ESTADO_APAGADO
                       else "NOTION_API_KEY no está configurada",
            "verificado_en": self._verificado_en,
        }

    def _fijar_estado(self, estado: str, detalle: str) -> None:
        from datetime import datetime, timezone
        self._estado_token = estado
        self._detalle_token = detalle
        self._verificado_en = datetime.now(timezone.utc).isoformat()

    def verificar_token(self, *, cliente=None) -> str:
        """
        Valida el token con UNA consulta barata (`users/me`) y cachea el resultado.

        Una sola, sin reintentos y con timeout corto (`NOTION_VERIFY_TIMEOUT_MS`): el
        arranque de la API espera esta llamada, y un reintento con backoff convertiría
        una red caída en 10 s de arranque. `cliente` permite inyectar un cliente de
        Notion armado sobre un transporte falso; en producción se crea uno propio con
        el timeout corto, sin tocar el que usa `push_job`.

        Sin red, con timeout, 429 o 5xx -> `no_verificado`. Solo un 401 es `invalido`.
        El token no se escribe en ningún log ni en el detalle.
        """
        if not self.api_key:
            return self.ESTADO_APAGADO
        try:
            if cliente is None:
                from notion_client import Client
                cliente = Client(auth=self.api_key,
                                 timeout_ms=config.NOTION_VERIFY_TIMEOUT_MS)
            cliente.users.me()
        except ImportError:
            self._fijar_estado(self.ESTADO_NO_VERIFICADO, "notion-client no instalado")
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 401:
                self._fijar_estado(self.ESTADO_INVALIDO,
                                   "Notion rechazó el token (401): vencido o revocado")
                logger.error("Notion rechazó el token al arrancar (401). Renová "
                             "NOTION_API_KEY en .env y reiniciá la API.")
                metrics.emit("notion_token_invalido", 1, origen="arranque")
            else:
                motivo = f"HTTP {status}" if status else type(exc).__name__
                self._fijar_estado(self.ESTADO_NO_VERIFICADO,
                                   f"no se pudo verificar ({motivo})")
                logger.warning("Notion: token sin verificar (%s).", motivo)
        else:
            self._fijar_estado(self.ESTADO_VALIDO, "Notion aceptó el token")
            logger.info("Notion: token válido.")
        return self.estado_token

    def _initialize(self) -> None:
        if not self.api_key:
            logger.warning("Notion API key not set. Integration disabled.")
            return
        try:
            from notion_client import Client
            self._client = Client(auth=self.api_key)
            logger.info("Notion client initialized.")
        except ImportError:
            logger.error("notion-client not installed. Run: pip install notion-client")

    @property
    def is_enabled(self) -> bool:
        return bool(self._client and self.api_key and self.database_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def push_job(self, tiered_job: TieredJob) -> Optional[str]:
        """
        Creates a Notion page (Kanban card) for one tiered job.

        Returns the created page ID, or None if the push failed permanently.
        A 401 disables the integration immediately — no retries, no further calls.
        """
        if not self.is_enabled:
            logger.warning("Notion integration disabled — skipping push.")
            return None

        job = tiered_job.job
        color = config.NOTION_TIER_COLORS.get(tiered_job.tier, "default")

        properties = {
            "Name": {
                "title": [{"text": {"content": job.title}}]
            },
            "Company": {
                "rich_text": [{"text": {"content": job.company}}]
            },
            "URL": {
                "url": job.url
            },
            "Tier": {
                "select": {"name": tiered_job.tier, "color": color}
            },
            "Match Score": {
                # Tier 4 sentinel is -1 — store as None in Notion (no score)
                "number": tiered_job.match_score if tiered_job.match_score >= 0 else None
            },
            "Salary": {
                "rich_text": [{"text": {"content": job.salary_raw or "Not published"}}]
            },
            "Comments / Raw Salary": {
                # Tier 4: surfaces the unstructured salary text for manual review.
                # For Tier 1/2/3: populated only if salary_raw is present and
                # non-numeric (defensive — belt-and-suspenders for edge cases).
                "rich_text": [{
                    "text": {
                        "content": (
                            f"[MANUAL REVIEW] Employer stated: '{job.salary_raw}' — "
                            "evaluate compensation manually before applying."
                            if tiered_job.tier == "Tier 4" and job.salary_raw
                            else job.salary_raw or ""
                        )
                    }
                }]
            },
            "Source": {
                "rich_text": [{"text": {"content": job.source_domain}}]
            },
            "Status": {
                "select": {
                    "name": "Manual Review" if tiered_job.tier == "Tier 4" else "To Review"
                }
            },
        }

        try:
            response = self._client.pages.create(
                parent={"database_id": self.database_id},
                properties=properties,
            )
            page_id = response["id"]
            logger.info("Notion card created: %s | %s (%s)", job.title, job.company, tiered_job.tier)
            return page_id
        except Exception as exc:
            # 401 = expired or revoked token — retrying will always fail.
            # Disable integration immediately to avoid hammering the API.
            if getattr(exc, "status", None) == 401:
                logger.error(
                    "Notion 401 Unauthorized — token expired or revoked. "
                    "Disabling Notion integration for this session. "
                    "Update NOTION_API_KEY in .env to re-enable."
                )
                self._client = None
                # El 401 de un push es la misma verdad que el de users/me: el indicador
                # no puede seguir diciendo "válido" porque la verificación de arranque
                # corrió antes de que el token se revocara.
                self._fijar_estado(self.ESTADO_INVALIDO,
                                   "Notion rechazó el token (401): vencido o revocado")
                metrics.emit("notion_token_invalido", 1, origen="push")
                return None
            logger.error("Notion push failed for %s: %s", job.title, exc)
            raise

    def push_batch(self, tiered_jobs: list[TieredJob]) -> dict:
        """
        Pushes a list of TieredJobs to Notion.
        Returns summary: {"pushed": N, "failed": N, "skipped": N}
        """
        if not self.is_enabled:
            return {"pushed": 0, "failed": 0, "skipped": len(tiered_jobs)}

        pushed, failed = 0, 0
        for tj in tiered_jobs:
            try:
                result = self.push_job(tj)
                if result is not None:
                    pushed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            # 401 disables the client mid-batch — no point continuing
            if not self.is_enabled:
                remaining = len(tiered_jobs) - pushed - failed
                failed += remaining
                logger.warning(
                    "Notion integration disabled mid-batch — skipping %d remaining jobs.", remaining
                )
                break

        logger.info("Notion batch: pushed=%d, failed=%d", pushed, failed)
        return {"pushed": pushed, "failed": failed, "skipped": 0}
