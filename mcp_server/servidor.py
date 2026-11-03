"""
servidor.py — las 8 tools del MCP de Resonance (SDK `mcp` 2.3.0, transporte stdio).

Cada tool es una llamada HTTP a la API local (ver cliente.py). Las respuestas son JSON
chico en texto: un LLM que recibe 300 avisos completos gasta su contexto en ruido y
pierde el hilo, así que `resultados()` recorta y `detalle_aviso()` trae UN aviso.

Las tools son `async` y mandan el HTTP a un hilo (`asyncio.to_thread`): arrancar la app
puede tardar hasta 60 s y bloquear el loop dejaría al servidor sordo a los pings.
La lógica vive en funciones síncronas `_xxx` para poder probarlas sin protocolo.
"""

import asyncio
import json
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import cliente
from mcp_server.cliente import ErrorApp

logger = logging.getLogger("mcp_server.servidor")

#: Techo de avisos por llamada a `resultados()`, pida lo que pida el modelo.
LIMITE_MAX = 50
#: Tope de requisitos y de caracteres de evidencia en `detalle_aviso()`.
REQUISITOS_MAX = 15
EVIDENCIA_MAX_CHARS = 200
#: Fuentes no sanas que `salud_fuentes()` lista una por una; el resto va en conteo.
FUENTES_MAX = 15

#: Alias de lo que una persona dice («apliqué») hacia el estado real de la máquina de estados.
_ALIAS_ESTADO = {"aplicado": "postulado", "aplique": "postulado", "apliqué": "postulado"}

mcp = MCPServer(
    "resonance",
    instructions=(
        "Resonance busca trabajo remoto y puntúa cada aviso contra el perfil de la persona. "
        "Flujo típico: buscar_trabajos(pedido) lanza una corrida (tarda minutos, no espera), "
        "estado_corrida() muestra el avance, resultados() lista los mejores avisos y "
        "detalle_aviso(url) explica el puntaje de uno."
    ),
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _recortar(texto: Any, n: int) -> str:
    t = str(texto or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _norm_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


async def _correr(fn, *args) -> str:
    """Corre la lógica síncrona en un hilo y traduce `ErrorApp` a error de tool."""
    try:
        return _json(await asyncio.to_thread(fn, *args))
    except ErrorApp as exc:
        raise ToolError(str(exc)) from exc


def _aviso_resumido(tj: dict) -> dict:
    job = tj.get("job") or {}
    return {
        "titulo": job.get("title"),
        "empresa": job.get("company"),
        "score": tj.get("match_score"),
        "ubicacion": job.get("location_raw"),
        "url": job.get("url"),
    }


def _todos_los_avisos(resultados: dict) -> list[tuple[int, dict]]:
    tiers = resultados.get("tiers") or {}
    return [(n, tj) for n in (1, 2, 3, 4) for tj in tiers.get(f"tier{n}", [])]


# ── lógica (síncrona, testeable) ─────────────────────────────────────────────

def _estado() -> dict:
    s = cliente.get("/api/status")
    out = {
        "boards_activos": s.get("boards_active"),
        "boards_registrados": s.get("boards"),
        "selectores_stale": len(s.get("stale_selectors") or []),
        "providers_llm_configurados": s.get("providers_configured"),
        "warnings": (s.get("warnings") or [])[:5],
        "nominal": s.get("nominal"),
        "corre_algo": bool(s.get("is_running") or s.get("scrape_gate_busy")),
        "hay_resultados": s.get("has_results"),
        "necesita_setup": s.get("needs_setup"),
        "cron": s.get("scheduler"),
        "integraciones": s.get("integrations"),
    }
    if s.get("progreso"):
        out["progreso"] = s["progreso"]
    return out


def _estado_corrida() -> dict:
    s = cliente.get("/api/status")
    corriendo = bool(s.get("is_running"))
    out = {
        "is_running": corriendo,
        # El gate lo toman también las corridas del cron: is_running solo cubre las manuales.
        "otra_corrida_en_curso": bool(s.get("scrape_gate_busy")) and not corriendo,
        "hay_resultados": s.get("has_results"),
    }
    if s.get("progreso"):  # lo agrega el seguimiento de progreso; sin él, no se inventa
        out["progreso"] = s["progreso"]
    if not corriendo and s.get("has_results"):
        out["siguiente"] = "Hay resultados: usá resultados()."
    return out


def _buscar_trabajos(pedido: str) -> dict:
    if not str(pedido or "").strip():
        raise ErrorApp("El pedido está vacío: describí qué puesto buscás.")

    # Primero la consulta barata: si ya corre algo, no se gasta una llamada al LLM en
    # parsear un pedido que no se va a poder lanzar. El gate cubre también las corridas
    # del cron, que no marcan is_running: sin mirarlo, el parseo se pagaba y /api/scrape
    # devolvía 409 igual.
    s = cliente.get("/api/status")
    if s.get("is_running") or s.get("scrape_gate_busy"):
        return {"iniciada": False,
                "motivo": "Ya hay una corrida activa; esperá a que termine.",
                "siguiente": "Consultá estado_corrida()."}

    parseo = cliente.post("/api/parse", {"prompt": pedido}, timeout=cliente.TIMEOUT_PARSE_S)
    cfg = parseo.get("config") or {}

    # La API guarda la config parseada en su estado: /api/scrape no recibe cuerpo con ella.
    try:
        run = cliente.post("/api/scrape", {})
    except ErrorApp as exc:
        if exc.status == 409:
            return {"iniciada": False,
                    "motivo": "Ya hay una corrida activa (otra se lanzó mientras tanto).",
                    "siguiente": "Consultá estado_corrida()."}
        raise

    return {
        "iniciada": True,
        "run_id": run.get("run_id"),
        "busqueda": {
            "titulos": (cfg.get("target_titles") or [])[:10],
            "incluir": (cfg.get("must_include") or [])[:10],
            "excluir": (cfg.get("must_exclude") or [])[:10],
            "salario_minimo": cfg.get("min_salary"),
        },
        "auditoria": parseo.get("audit_verdict"),
        "siguiente": "La corrida tarda minutos y no se espera aquí: consultá estado_corrida().",
    }


def _resultados(tier: int, limite: int) -> dict:
    if tier not in (1, 2, 3, 4):
        raise ErrorApp("tier debe ser 1, 2, 3 o 4.")
    limite = max(1, min(int(limite), LIMITE_MAX))

    r = cliente.get("/api/results")
    if not r.get("tiers"):
        if r.get("is_running"):
            return {"hay_resultados": False, "mensaje": "La corrida sigue en curso; "
                    "los resultados aparecen al terminar. Consultá estado_corrida()."}
        ult = r.get("last_run") or {}
        return {"hay_resultados": False,
                "mensaje": ult.get("message") or "No hay resultados en esta sesión: "
                "lanzá una búsqueda con buscar_trabajos()."}

    avisos = sorted(r["tiers"].get(f"tier{tier}", []),
                    key=lambda tj: tj.get("match_score", 0), reverse=True)
    return {
        "hay_resultados": True,
        "run_id": r.get("run_id"),
        "tier": tier,
        "total_en_tier": len(avisos),
        "mostrados": min(len(avisos), limite),
        "avisos": [_aviso_resumido(tj) for tj in avisos[:limite]],
    }


def _requisito(req: dict) -> dict:
    return {
        "requisito": _recortar(req.get("requisito"), 120),
        "estado": req.get("estado"),
        "peso": req.get("peso"),
        "evidencia": _recortar(req.get("evidencia"), EVIDENCIA_MAX_CHARS),
    }


def _detalle_aviso(url: str) -> dict:
    objetivo = _norm_url(url)
    if not objetivo:
        raise ErrorApp("Falta la url del aviso.")

    # 1) Los resultados de la sesión traen requisitos, evidencia y razones del score.
    for n, tj in _todos_los_avisos(cliente.get("/api/results")):
        job = tj.get("job") or {}
        if _norm_url(job.get("url")) != objetivo:
            continue
        reqs = tj.get("requisitos") or []
        return {
            **_aviso_resumido(tj),
            "tier": n,
            "salario": job.get("salary_raw"),
            "fuente": job.get("source_domain"),
            "ubicacion_veredicto": tj.get("location_verdict"),
            "ubicacion_nota": tj.get("location_note"),
            "idioma_veredicto": tj.get("language_verdict"),
            "idioma_nota": tj.get("language_note"),
            "razones_a_favor": (tj.get("match_reasons") or [])[:10],
            "razones_en_contra": (tj.get("miss_reasons") or [])[:10],
            "cobertura_requisitos": tj.get("cobertura_requisitos"),
            "requisitos": [_requisito(x) for x in reqs[:REQUISITOS_MAX]],
            "requisitos_omitidos": max(0, len(reqs) - REQUISITOS_MAX),
        }

    # 2) El registry guarda todo lo visto, pero sin requisitos ni evidencia.
    filas = cliente.get("/api/registry/jobs", limit=1000).get("jobs") or []
    for fila in filas:
        if _norm_url(fila.get("url")) == objetivo:
            return {
                "titulo": fila.get("title"), "empresa": fila.get("company"),
                "score": fila.get("match_score"), "tier": fila.get("tier"),
                "ubicacion": fila.get("location_raw"), "salario": fila.get("salary_raw"),
                "fuente": fila.get("source_domain"), "url": fila.get("url"),
                "nota": "Solo datos del registry: el aviso no está en los resultados de "
                        "la sesión, así que no hay requisitos ni evidencia.",
            }
    raise ErrorApp("No encontré ese aviso ni en los resultados ni en el registry. "
                   "Revisá que la url sea la que devolvió resultados().")


def _registrar_postulacion(url: str, estado: str, nota: str) -> dict:
    if not _norm_url(url):
        raise ErrorApp("Falta la url del aviso.")
    estado = _ALIAS_ESTADO.get(str(estado).strip().lower(), str(estado).strip().lower())
    cuerpo: dict[str, Any] = {"url": _norm_url(url), "status": estado}
    if str(nota or "").strip():
        cuerpo["notes"] = nota.strip()
    try:
        app = cliente.post("/api/applications", cuerpo)
    except ErrorApp as exc:
        if exc.status in (400, 422):
            raise ErrorApp(f"La API rechazó la postulación: {exc}") from exc
        raise
    return {
        "registrada": True,
        "app_id": app.get("app_id"),
        "titulo": app.get("title"),
        "empresa": app.get("company"),
        "estado": app.get("status"),
        "tier_al_aplicar": app.get("tier_al_aplicar"),
        "score_al_aplicar": app.get("score_al_aplicar"),
        "nota": ("Ya existía una postulación para este aviso; se devolvió sin cambios."
                 if app.get("status") != estado else None),
    }


def _salud_fuentes() -> dict:
    f = cliente.get("/api/fuentes")
    fuentes = f.get("fuentes") or []  # ya vienen ordenadas: sospechosas primero
    malas = [x for x in fuentes if x.get("estado") != "sana"]
    return {
        "resumen": f.get("resumen"),
        "ventana_corridas": f.get("ventana_corridas"),
        "requieren_atencion": [
            {"fuente": x.get("fuente"), "estado": x.get("estado"), "tipo": x.get("tipo"),
             "items": x.get("items"), "historial": (x.get("historial") or [])[:5],
             "motivo": _recortar(x.get("motivo"), 200)}
            for x in malas[:FUENTES_MAX]
        ],
        "omitidas": max(0, len(malas) - FUENTES_MAX),
    }


def _abrir_panel() -> dict:
    inst = cliente.asegurar_instancia()
    como = cliente.abrir_ventana(inst["url"])
    return {"abierto": True, "url": inst["url"], "como": como}


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def estado() -> str:
    """Resumen del estado de Resonance: boards activos, proveedores de LLM configurados,
    advertencias de configuración, si hay una corrida en curso y cuándo corre el cron.
    Úsala para saber si todo está listo antes de buscar, o ante «¿cómo está Resonance?».
    Arranca la app si no estaba corriendo."""
    return await _correr(_estado)


@mcp.tool()
async def buscar_trabajos(pedido: str) -> str:
    """Lanza una búsqueda de empleo a partir de un pedido en lenguaje natural, por ejemplo
    «ingeniero de datos remoto, mínimo 4000 USD, sin consultoras». Interpreta el pedido,
    arranca la corrida y devuelve el run_id SIN esperar: tarda varios minutos. Después
    consultá estado_corrida() y, al terminar, resultados(). Si ya hay una corrida activa
    no lanza otra."""
    return await _correr(_buscar_trabajos, pedido)


@mcp.tool()
async def estado_corrida() -> str:
    """Avance de la corrida de búsqueda: si sigue en curso y, cuando la app lo informa,
    el progreso (fase, unidades hechas, tiempo restante estimado, última actividad).
    Úsala después de buscar_trabajos() hasta que is_running sea false."""
    return await _correr(_estado_corrida)


@mcp.tool()
async def resultados(tier: int = 1, limite: int = 20) -> str:
    """Lista los avisos de la última corrida de un tier (1 = mejor ajuste ... 4 = revisión
    manual), ordenados por score. Devuelve solo título, empresa, score, ubicación y url;
    para el porqué de un puntaje usá detalle_aviso(url). `limite` tiene tope de 50."""
    return await _correr(_resultados, tier, limite)


@mcp.tool()
async def detalle_aviso(url: str) -> str:
    """Detalle de UN aviso, identificado por su url (la que devuelve resultados()):
    razones a favor y en contra del puntaje, veredictos de ubicación e idioma, y los
    requisitos con la evidencia del texto del aviso y si el CV los cubre."""
    return await _correr(_detalle_aviso, url)


@mcp.tool()
async def registrar_postulacion(url: str, estado: str = "postulado", nota: str = "") -> str:
    """Registra que la persona se postuló a un aviso (url de resultados()). `estado` es
    uno de: borrador, postulado (por defecto; «aplicado» se acepta como sinónimo),
    screening, tecnica, final, oferta, rechazado, sin_respuesta, retirado, aceptado.
    `nota` es opcional. Es idempotente: si el aviso ya tenía postulación, la devuelve."""
    return await _correr(_registrar_postulacion, url, estado, nota)


@mcp.tool()
async def salud_fuentes() -> str:
    """Estado de las fuentes de avisos (boards, APIs, ATS) según su corrida más reciente.
    Lista primero las sospechosas (rindieron cero o mucho menos que su historial), luego
    las sin rendimiento y sin medir; las sanas solo van en el conteo del resumen."""
    return await _correr(_salud_fuentes)


@mcp.tool()
async def abrir_panel() -> str:
    """Abre el panel (la interfaz gráfica de Resonance) en una ventana. Arranca la app si
    no estaba corriendo."""
    return await _correr(_abrir_panel)
