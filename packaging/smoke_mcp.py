"""
packaging/smoke_mcp.py — handshake MCP contra resonance-mcp.exe, por stdio.

    python packaging/smoke_mcp.py <ruta a resonance-mcp.exe> [tope_segundos] [tope_estado_s]

Dos pasos:
  1. `initialize`: exige un `result` con el mismo id. Si responde, el exe congelado importó
     el SDK de MCP y su servidor; si falta un módulo oculto, muere antes de contestar.
  2. `tools/call estado`: exige una respuesta con el mismo id, sea un resultado o un error de
     herramienta (`isError`). Lo que no vale es colgarse o romper el protocolo: esta tool
     recorre el camino real del MCP (descubrir la app, lanzarla en headless si no corre,
     pedir /api/status), que el `initialize` solo no toca. `tope_estado_s` por defecto es
     200 s porque el MCP espera hasta 180 s a que la app arranque.

Si el entorno no trae RESONANCE_HOME, el smoke usa un directorio temporal: sin eso, `estado`
lanzaría la app contra los datos reales de quien corre el script. En ese caso la app que
`estado` haya lanzado se termina al final (con la variable puesta, como en build.ps1, la
app es del llamador y se la deja).

Exit 0 = OK, 1 = falló (el motivo va a stderr).

Está en Python y no en PowerShell a propósito: Process.StandardInput de Windows PowerShell
5.1 no entrega el primer mensaje de forma fiable y el handshake queda colgado.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "0"}}}
INICIALIZADO = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LLAMAR_ESTADO = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "estado", "arguments": {}}}


def _enviar(proc: subprocess.Popen, msg: dict) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))  # type: ignore[union-attr]
    proc.stdin.flush()  # type: ignore[union-attr]


def _esperar_respuesta(lineas: "queue.Queue[bytes]", id_: int, tope: float):
    """
    El mensaje JSON-RPC con ese id, o None si venció el tope. Levanta ValueError si una
    línea de stdout no es JSON (el protocolo está roto: algo ajeno escribió en el canal).
    Ignora las notificaciones y los mensajes de otro id.
    """
    limite = time.monotonic() + tope
    while True:
        resto = limite - time.monotonic()
        if resto <= 0:
            return None
        try:
            linea = lineas.get(timeout=resto)
        except queue.Empty:
            return None
        if not linea.strip():
            continue
        try:
            msg = json.loads(linea.decode("utf-8"))
        except ValueError:
            raise ValueError(f"respuesta que no es JSON: {linea[:200]!r}")
        if isinstance(msg, dict) and msg.get("id") == id_:
            return msg


def _terminar_app_lanzada(home: str) -> None:
    """Termina la app que `estado` lanzó contra el home temporal del smoke."""
    try:
        with open(os.path.join(home, "runtime.json"), encoding="utf-8") as fh:
            pid = int(json.load(fh)["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: smoke_mcp.py <resonance-mcp.exe> [tope_s] [tope_estado_s]", file=sys.stderr)
        return 1
    exe = sys.argv[1]
    tope = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    tope_estado = float(sys.argv[3]) if len(sys.argv) > 3 else 200.0

    env = dict(os.environ)
    home_temporal = None
    if not env.get("RESONANCE_HOME", "").strip():
        home_temporal = tempfile.mkdtemp(prefix="resonance_smoke_mcp_")
        env["RESONANCE_HOME"] = home_temporal

    proc = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    lineas: "queue.Queue[bytes]" = queue.Queue()
    stderr_buf: list[bytes] = []

    def leer_stdout() -> None:
        for linea in proc.stdout:  # type: ignore[union-attr]
            lineas.put(linea)

    def leer_stderr() -> None:
        for linea in proc.stderr:  # type: ignore[union-attr]
            stderr_buf.append(linea)

    threading.Thread(target=leer_stdout, daemon=True).start()
    threading.Thread(target=leer_stderr, daemon=True).start()

    def cola_stderr() -> str:
        return b"".join(stderr_buf[-20:]).decode("utf-8", "replace")

    try:
        _enviar(proc, INIT)
        try:
            msg = _esperar_respuesta(lineas, 1, tope)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if msg is None:
            print(f"sin respuesta a initialize en {tope:.0f} s", file=sys.stderr)
            print(cola_stderr(), file=sys.stderr)
            return 1
        if "result" not in msg:
            print(f"respuesta inesperada: {str(msg)[:300]}", file=sys.stderr)
            return 1
        info = msg["result"].get("serverInfo", {})
        print(f"initialize OK (servidor: {info.get('name', '?')})")

        _enviar(proc, INICIALIZADO)
        _enviar(proc, LLAMAR_ESTADO)
        try:
            msg = _esperar_respuesta(lineas, 2, tope_estado)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if msg is None:
            print(f"sin respuesta a tools/call estado en {tope_estado:.0f} s", file=sys.stderr)
            print(cola_stderr(), file=sys.stderr)
            return 1
        if "result" in msg:
            es_error = bool(msg["result"].get("isError"))
            print("tools/call estado OK (" +
                  ("error de herramienta, esperado sin app" if es_error else "resultado") + ")")
            return 0
        if "error" in msg:
            # Un error JSON-RPC (método o params inválidos) NO es «la tool falló»: es que el
            # servidor no entiende la llamada.
            print(f"tools/call estado devolvió un error de protocolo: {str(msg['error'])[:300]}",
                  file=sys.stderr)
            return 1
        print(f"respuesta inesperada a estado: {str(msg)[:300]}", file=sys.stderr)
        return 1
    finally:
        proc.kill()
        if home_temporal is not None:
            _terminar_app_lanzada(home_temporal)
            time.sleep(0.5)
            shutil.rmtree(home_temporal, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
