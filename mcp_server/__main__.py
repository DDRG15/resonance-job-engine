"""`python -m mcp_server` — sirve las tools por stdio.

stdout es el canal del protocolo: cualquier byte ajeno lo corrompe y el cliente MCP
corta la sesión sin decir por qué. Por eso el logging se configura ACÁ, antes de importar
nada más, y va solo a stderr y a `RESONANCE_HOME/logs/mcp.log`.
"""

import logging
import sys


def _configurar_logging() -> None:
    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    for h in list(raiz.handlers):
        raiz.removeHandler(h)

    a_stderr = logging.StreamHandler(sys.stderr)
    a_stderr.setFormatter(formato)
    raiz.addHandler(a_stderr)

    try:
        from desktop import runtime
        carpeta = runtime.resonance_home() / "logs"
        carpeta.mkdir(parents=True, exist_ok=True)
        a_archivo = logging.FileHandler(carpeta / "mcp.log", encoding="utf-8")
        a_archivo.setFormatter(formato)
        raiz.addHandler(a_archivo)
    except Exception as exc:  # sin archivo de log el servidor igual tiene que servir
        logging.getLogger("mcp_server").warning("sin mcp.log: %s", exc)


def main() -> None:
    _configurar_logging()
    from mcp_server.servidor import mcp
    mcp.run("stdio")


if __name__ == "__main__":
    main()
