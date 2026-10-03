"""
origin_guard.py — rechaza con 403 los metodos que mutan estado cuando el Origin es ajeno.

host_guard.py corta el DNS rebinding (el Host del request es el de la pagina atacante).
No corta el otro camino, mas barato: la pagina de `evil.example` manda un formulario
`POST http://localhost:8000/api/reset`. El Host de ese request es `localhost:8000` —el
del destino, legitimo—, no hay preflight de CORS para un formulario simple, y CORS solo
decide si la pagina puede LEER la respuesta, no si el request se ejecuta: el reset ya
corrio cuando el navegador bloquea la lectura. Lo mismo vale para PUT /api/scheduler con
`fetch(..., {mode: "no-cors"})` y un cuerpo text/plain que FastAPI igual parsea.

Lo que delata el request es el header `Origin`: el navegador lo pone en todo POST, PUT,
PATCH y DELETE (tambien en los del mismo origen), la pagina no puede falsearlo ni quitarlo,
y vale `null` para iframes con sandbox y redirects entre origenes. Un request SIN Origin
no viene de un navegador (curl, la CLI, el TestClient, el healthcheck de Docker): un
cliente que no es un navegador no es vector de CSRF, porque para mandarle un request el
atacante ya tendria que estar ejecutando codigo en la maquina. Eso se permite.

Se rechaza solo lo que muta (POST/PUT/PATCH/DELETE): GET, HEAD y OPTIONS no cambian
estado, y el preflight de CORS tiene que poder llegar a CORSMiddleware.

Residuo documentado: esto NO es autenticacion. Un proceso local sin Origin sigue pudiendo
llamar a la API, y un navegador con una extension que reescribe headers tambien. Abrir la
API a otra maquina pide autenticacion primero.

La lista se lee de `config.ALLOWED_ORIGINS` en cada request, igual que en host_guard.py.
"""

import json

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

import config
import metrics

_METODOS_QUE_MUTAN = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Tope de lo que viaja a la metrica: el header y la ruta los escribe quien ataca.
_MAX_EN_LOG = 80

# El cuerpo dice como arreglarlo SIN reflejar el Origin recibido: si es el frontend
# legitimo en otro puerto (Vite salta a 5174 cuando 5173 esta ocupado), el mensaje lo
# llega a ver en pantalla —api.ts muestra `HTTP 403 — <cuerpo>`—.
_CUERPO_403 = json.dumps({
    "detail": "Origin no permitido. Si es tu frontend, agregalo a GEMA_ALLOWED_ORIGINS."
}).encode("utf-8")


class OriginGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _METODOS_QUE_MUTAN:
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin is None or origin.strip().lower() in config.ALLOWED_ORIGINS:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        metrics.emit(
            "origen_rechazado_total", 1,
            origin=origin[:_MAX_EN_LOG],
            metodo=scope["method"],
            ruta=scope.get("path", "")[:_MAX_EN_LOG],
            cliente=client[0] if client else None,
        )
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_CUERPO_403)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": _CUERPO_403})
