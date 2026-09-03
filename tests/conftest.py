"""
conftest.py — pytest configuration for the GEMA test suite.

Adds the parent directory (gema/) to sys.path so test modules can import
project packages (models, matcher, nlp_engine, config) without installing
the project as a package.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _aislar_drift_quarantine(tmp_path, monkeypatch):
    """
    Ningún test escribe en el drift_quarantine real.

    Ese directorio es el corpus de evaluación de la Fase 7. Un snapshot sintético que
    caiga ahí —domain=board.com, sent_text_len=1— lleva schema_version 2 y prompt_version
    real, así que pasa el mismo filtro que el harness usa para descartar basura:
    contamina la medición en vez de fallar ruidosamente.

    Medido antes de existir este fixture: 22 archivos en el corpus al empezar una sesión,
    38 al terminarla. Los 16 nuevos eran de la suite.

    Autouse a propósito. Ponerlo test por test significa que el próximo test que ejercite
    el camino del fallback se olvide, y la contaminación vuelve sin que nada la señale.
    """
    import config
    monkeypatch.setattr(
        config, "DRIFT_QUARANTINE_DIR", str(tmp_path / "drift_quarantine")
    )
