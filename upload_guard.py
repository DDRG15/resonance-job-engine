"""
upload_guard.py — tope de bytes para un endpoint de subida, aplicado ANTES de leer el cuerpo.

`/api/cv` leia el archivo entero y el tope posterior de texto
(8 000 caracteres) no limita bytes. Con FastAPI el problema es mas hondo que un read()
sin argumento: el multipart se parsea y se vuelca a un archivo temporal ANTES de que el
endpoint corra, asi que ninguna validacion dentro del endpoint llega a tiempo. Un POST
de 2 GB ya escribio 2 GB a disco cuando `upload_cv` empieza. El unico punto donde el
cuerpo todavia no se leyo es antes del router: un middleware ASGI.

Dos defensas, porque Content-Length lo declara el cliente y puede faltar (chunked) o
mentir:
  1. Content-Length declarado sobre el tope -> 413 sin pedir un solo chunk del cuerpo.
  2. Sin Content-Length (o con uno falso) -> se cuentan los bytes que entran y se corta
     al cruzar el tope; lo que falte por llegar nunca se lee.

Solo toca el metodo y la ruta configurados; el resto del trafico pasa intacto.
"""

import json
from typing import Awaitable, Callable

# Margen para el sobre multipart (boundary, cabeceras de parte, campos de texto como el
# consentimiento). El tope de config es del ARCHIVO; el cuerpo lo excede por unos cientos
# de bytes, y rechazar un CV de exactamente 5 MiB por el sobre seria un error de frontera.
_SOBRE_MULTIPART = 64 * 1024


class _CuerpoExcedido(Exception):
    """Senal interna: el cuerpo cruzo el tope. FastAPI la convierte en 400 al parsear el
    form, asi que el middleware no depende de verla propagarse — usa la bandera."""


class MaxBodySizeMiddleware:
    def __init__(
        self,
        app,
        *,
        path: str,
        method: str,
        max_bytes: Callable[[], int],
        mensaje: Callable[[int], str],
    ) -> None:
        # max_bytes y mensaje son callables para leer config en cada peticion: un limite
        # fijado al construir la app no se puede cambiar por entorno ni probar sin reiniciar.
        self.app = app
        self.path = path
        self.method = method
        self.max_bytes = max_bytes
        self.mensaje = mensaje

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != self.method
            or scope["path"] != self.path
        ):
            await self.app(scope, receive, send)
            return

        tope = self.max_bytes()
        limite_cuerpo = tope + _SOBRE_MULTIPART

        for nombre, valor in scope.get("headers", []):
            if nombre == b"content-length":
                try:
                    declarado = int(valor)
                except ValueError:
                    declarado = -1
                if declarado > limite_cuerpo:
                    await self._rechazar(send, tope)
                    return
                break

        recibidos = 0
        excedido = False
        respuesta_iniciada = False

        async def receive_acotado():
            nonlocal recibidos, excedido
            mensaje = await receive()
            if mensaje["type"] == "http.request":
                recibidos += len(mensaje.get("body", b""))
                if recibidos > limite_cuerpo:
                    excedido = True
                    raise _CuerpoExcedido()
            return mensaje

        async def send_filtrado(mensaje):
            nonlocal respuesta_iniciada
            if excedido:
                # La app ya reacciono al corte (normalmente con un 400 "error parsing the
                # body"); esa respuesta se descarta y sale el 413 que corresponde.
                if mensaje["type"] == "http.response.start" and not respuesta_iniciada:
                    respuesta_iniciada = True
                    await self._rechazar(send, tope)
                return
            if mensaje["type"] == "http.response.start":
                respuesta_iniciada = True
            await send(mensaje)

        try:
            await self.app(scope, receive_acotado, send_filtrado)
        except _CuerpoExcedido:
            if not respuesta_iniciada:
                await self._rechazar(send, tope)

    async def _rechazar(self, send: Callable[[dict], Awaitable[None]], tope: int) -> None:
        cuerpo = json.dumps({"detail": self.mensaje(tope)}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(cuerpo)).encode()),
                # El cuerpo del request quedo sin leer: cerrar la conexion evita que el
                # servidor intente reutilizarla con bytes basura pendientes.
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": cuerpo})
