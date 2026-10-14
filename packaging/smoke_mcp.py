"""
packaging/smoke_mcp.py — handshake MCP contra resonance-mcp.exe, por stdio.

    python packaging/smoke_mcp.py <ruta a resonance-mcp.exe> [tope_segundos]

Manda `initialize` y exige un `result` con el mismo id. Si responde, el exe congelado
importó el SDK de MCP y su servidor; si falta un módulo oculto, muere antes de contestar.
Exit 0 = OK, 1 = falló (el motivo va a stderr).

Está en Python y no en PowerShell a propósito: Process.StandardInput de Windows PowerShell
5.1 no entrega el primer mensaje de forma fiable y el handshake queda colgado.
"""

import json
import subprocess
import sys
import threading

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "0"}}}


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: smoke_mcp.py <resonance-mcp.exe> [tope_s]", file=sys.stderr)
        return 1
    exe = sys.argv[1]
    tope = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    proc = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    respuesta: list[bytes] = []
    listo = threading.Event()
    stderr_buf: list[bytes] = []

    def leer_stdout() -> None:
        for linea in proc.stdout:  # type: ignore[union-attr]
            respuesta.append(linea)
            listo.set()
            return

    def leer_stderr() -> None:
        for linea in proc.stderr:  # type: ignore[union-attr]
            stderr_buf.append(linea)

    threading.Thread(target=leer_stdout, daemon=True).start()
    threading.Thread(target=leer_stderr, daemon=True).start()
    try:
        proc.stdin.write((json.dumps(INIT) + "\n").encode("utf-8"))  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        if not listo.wait(tope):
            print(f"sin respuesta a initialize en {tope:.0f} s", file=sys.stderr)
            print(b"".join(stderr_buf[-20:]).decode("utf-8", "replace"), file=sys.stderr)
            return 1
        try:
            msg = json.loads(respuesta[0].decode("utf-8"))
        except ValueError:
            print(f"respuesta que no es JSON: {respuesta[0][:200]!r}", file=sys.stderr)
            return 1
        if msg.get("id") != 1 or "result" not in msg:
            print(f"respuesta inesperada: {str(msg)[:300]}", file=sys.stderr)
            return 1
        info = msg["result"].get("serverInfo", {})
        print(f"initialize OK (servidor: {info.get('name', '?')})")
        return 0
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
