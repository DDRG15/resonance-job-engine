"""
host_guard.py — TrustedHostMiddleware con metrica y con la lista leida de config.

La API de GEMA no tiene autenticacion (CV, setup, scheduler y reset abiertos a quien
alcance el puerto). El contenedor ya publica solo en 127.0.0.1, pero eso no frena a un
navegador de la propia maquina: con DNS rebinding, una pagina de `evil.example` hace
que el navegador mande `Host: evil.example` a localhost:8000, y para el navegador la
respuesta es del mismo origen que la pagina. El atacante controla el DNS y el contenido;
el Host que el navegador pone en el request no lo controla. Rechazar todo Host que no
sea uno propio corta ese camino con 3 lineas.

Por que no `TrustedHostMiddleware` a secas. Hay dos cosas que el de starlette no hace:

  1. Decir que rechazo. Un 400 que nadie cuenta es un ataque (o un proxy mal
     configurado) que no se ve nunca: pasa el test de la 1 AM solo si hay una metrica,
     `host_rechazado_total`, y quien la lea sabe que Host fue.
  2. Leer la lista al momento del request. El de starlette fija `allowed_hosts` al
     construirse, que es en el import de api.py; entonces el fixture de los tests que
     necesita `testserver` (el Host del TestClient) no tiene como agregarlo sin tocar
     la configuracion de produccion. Aca la lista se lee de `config.ALLOWED_HOSTS` en
     cada request: un atributo de modulo, sin I/O.

Hereda de TrustedHostMiddleware para reusar la validacion de los patrones del
constructor y para que `isinstance` y las herramientas que inspeccionan el stack lo
reconozcan. La decision de pasar o rechazar es de esta clase, no de la base.

Comparacion: solo el host, sin puerto (`localhost:8000` y `localhost:5173` son el mismo
host), y sin distinguir mayusculas (RFC 3986: el host es case-insensitive; la base de
starlette compara exacto y rechazaria `Host: LOCALHOST`).
"""

from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette._utils import parse_host_header
from starlette.types import ASGIApp, Receive, Scope, Send

import config
import metrics

# Tope del Host que va a la metrica. El header lo escribe quien ataca: sin tope, un
# request con 8 KB de Host mete 8 KB en cada linea de log.
_MAX_HOST_EN_LOG = 80


def host_permitido(host: str, patrones: list[str]) -> bool:
    """
    True si `host` (ya sin puerto) matchea algun patron de `patrones`.

    Misma regla que TrustedHostMiddleware: igualdad exacta o `*.dominio` como sufijo.
    Un `*` pelado no matchea nada aca: config.parse_allowed_hosts ya lo descarta, y esta
    funcion no es el lugar donde se decide apagar la proteccion.
    """
    host = host.lower()
    for pat in patrones:
        if pat == "*":
            continue
        if host == pat or (pat.startswith("*.") and host.endswith(pat[1:])):
            return True
    return False


class HostGuardMiddleware(TrustedHostMiddleware):
    def __init__(self, app: ASGIApp, allowed_hosts: list[str] | None = None) -> None:
        # www_redirect=False: GEMA es una herramienta de loopback. Redirigir un Host
        # desconocido a "www.<host>" no tiene ningun caso de uso aca.
        super().__init__(app, allowed_hosts=allowed_hosts or list(config.ALLOWED_HOSTS),
                         www_redirect=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            # lifespan y demas: no hay Host que mirar.
            await self.app(scope, receive, send)
            return

        raw = Headers(scope=scope).get("host")
        parsed = parse_host_header(raw)
        if parsed is not None and host_permitido(parsed.host, config.ALLOWED_HOSTS):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        # `host` es el header tal cual llego (truncado), no el parseado: un header
        # invalido no tiene parseo y es justo el que interesa ver.
        metrics.emit(
            "host_rechazado_total", 1,
            host=(raw or "")[:_MAX_HOST_EN_LOG],
            tipo=scope["type"],
            cliente=client[0] if client else None,
        )
        if scope["type"] == "websocket":
            # Un 400 en texto no es respuesta valida a un handshake websocket; el
            # protocolo ASGI pide cerrar con `websocket.close`.
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
