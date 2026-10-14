"""Entrada de Resonance.exe (sin consola). Equivale a `python -m desktop.app`."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    import entrada_comun

    codigo = entrada_comun.atender_flags(sys.argv[1:])
    if codigo is None:
        entrada_comun.preparar_entorno()
        from desktop.app import main
        codigo = main()
    sys.exit(codigo)
