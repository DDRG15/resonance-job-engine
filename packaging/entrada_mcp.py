"""Entrada de resonance-mcp.exe (con consola: stdio). Equivale a `python -m mcp_server`."""

import multiprocessing
import sys


if __name__ == "__main__":
    multiprocessing.freeze_support()
    import entrada_comun

    codigo = entrada_comun.atender_flags(sys.argv[1:])
    if codigo is None:
        entrada_comun.preparar_entorno()
        from mcp_server.__main__ import main
        main()
        codigo = 0
    sys.exit(codigo)
