"""
tests/test_drift_snapshot.py — el capturador de drift (Fase 5).

Estos archivos dejaron de ser material de debug puntual: SON el corpus de evaluación de
la Fase 7. Eso cambia qué tiene que ser cierto de ellos.

v2 los escribía rotos en tres puntos, y los tres se prueban acá:

  1. Guardaba sent_text[:8000] mientras la extracción trunca su input en 20000. El
     grounding se juzga contra el texto enviado, así que la URL que decide el veredicto
     podía estar en el tramo que nunca se escribió — el corpus marcaría como alucinación
     una URL que sí estaba en la fuente.
  2. Guardaba str(llm_output)[:4000]: el repr de Python de una lista de ExtractedJob,
     legible para un humano y muerto para cualquier herramienta.
  3. No registraba prompt_version, así que dos corridas a través de un cambio de prompt
     no se podían comparar — que es la comparación para la que existe el corpus.
"""

import json

import pytest

import config
import scraper
from models import ExtractedJob


@pytest.fixture
def quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DRIFT_QUARANTINE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DRIFT_QUARANTINE_MAX_FILES", 300)
    return tmp_path


def _only_snapshot(path):
    files = list(path.glob("drift_*.json"))
    assert len(files) == 1, f"esperaba 1 snapshot, hay {len(files)}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def _job(**over):
    base = dict(title="Backend Engineer", company="TestCo",
                source_url="https://b.com/j/1")
    base.update(over)
    return ExtractedJob(**base)


class TestInputIsStoredWhole:

    def test_text_longer_than_the_old_8000_cap_is_not_truncated(self, quarantine):
        # El caso exacto que rompía el corpus: la URL que decide el grounding vive
        # más allá del viejo corte de 8000.
        texto = ("x" * 9000) + " https://b.com/j/tardia"
        scraper._snapshot_drift_failure("b.com", "https://b.com/s", texto, [], "r")
        payload = _only_snapshot(quarantine)
        assert payload["sent_text"] == texto
        assert "https://b.com/j/tardia" in payload["sent_text"]

    def test_declared_length_matches_the_stored_text(self, quarantine):
        texto = "y" * 25000
        scraper._snapshot_drift_failure("b.com", "s", texto, [], "r")
        payload = _only_snapshot(quarantine)
        assert payload["sent_text_len"] == 25000
        assert len(payload["sent_text"]) == 25000


class TestOutputIsParseable:

    def test_llm_output_round_trips_as_json(self, quarantine):
        # v2 guardaba el repr: "[ExtractedJob(title='...', ...)]". El harness de la
        # Fase 7 no puede reconstruir nada de eso.
        scraper._snapshot_drift_failure("b.com", "s", "t", [_job()], "r")
        payload = _only_snapshot(quarantine)
        assert isinstance(payload["llm_output"], list)
        assert payload["llm_output"][0]["title"] == "Backend Engineer"
        assert payload["llm_output"][0]["source_url"] == "https://b.com/j/1"

    def test_empty_output_is_an_empty_list_not_a_string(self, quarantine):
        scraper._snapshot_drift_failure("b.com", "s", "t", [], "r")
        assert _only_snapshot(quarantine)["llm_output"] == []

    def test_unserializable_output_degrades_instead_of_losing_the_snapshot(self, quarantine):
        # Perder el snapshot entero porque un campo no serializa sería cambiar un
        # problema chico por uno grande.
        scraper._snapshot_drift_failure("b.com", "s", "t", object(), "r")
        assert _only_snapshot(quarantine)["llm_output"]


class TestProvenance:

    def test_prompt_version_and_provider_are_recorded(self, quarantine):
        scraper._snapshot_drift_failure("b.com", "s", "t", [], "r",
                                        prompt_version="abc123", provider="groq")
        payload = _only_snapshot(quarantine)
        assert payload["prompt_version"] == "abc123"
        assert payload["provider"] == "groq"

    def test_schema_version_separates_v2_snapshots(self, quarantine):
        # Los 22 archivos viejos son sintéticos y con el input truncado. El harness
        # tiene que poder descartarlos sin adivinar.
        scraper._snapshot_drift_failure("b.com", "s", "t", [], "r")
        assert _only_snapshot(quarantine)["schema_version"] == 2

    def test_reason_is_carried_into_the_metric(self, quarantine, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            scraper._snapshot_drift_failure("b.com", "s", "t", [], "sampled_success")
        metric = [json.loads(r.getMessage()) for r in caplog.records
                  if r.name == "gema.metrics"][-1]
        assert metric["labels"]["reason"] == "sampled_success"


class TestSuccessSampling:

    def test_samples_one_in_n_deterministically(self, monkeypatch):
        # Determinístico y no aleatorio: con muestreo al azar dos corridas del mismo
        # board pueden no dejar ninguna muestra, y el corpus queda sesgado por suerte.
        import itertools
        monkeypatch.setattr(config, "DRIFT_SNAPSHOT_SUCCESS_SAMPLE", 5)
        monkeypatch.setattr(scraper, "_success_snapshot_counter", itertools.count())
        hits = [scraper._should_snapshot_success() for _ in range(15)]
        assert hits.count(True) == 3
        assert hits[0] is True and hits[5] is True and hits[10] is True

    def test_zero_disables_sampling(self, monkeypatch):
        import itertools
        monkeypatch.setattr(config, "DRIFT_SNAPSHOT_SUCCESS_SAMPLE", 0)
        monkeypatch.setattr(scraper, "_success_snapshot_counter", itertools.count())
        assert not any(scraper._should_snapshot_success() for _ in range(20))


class TestNeverBreaksTheScrape:

    def test_an_unwritable_directory_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(config, "DRIFT_QUARANTINE_DIR", "\x00ruta/invalida")
        scraper._snapshot_drift_failure("b.com", "s", "t", [], "r")   # no debe lanzar

    def test_the_cap_evicts_the_oldest(self, quarantine, monkeypatch):
        monkeypatch.setattr(config, "DRIFT_QUARANTINE_MAX_FILES", 3)
        for i in range(6):
            scraper._snapshot_drift_failure(f"b{i}.com", "s", "t", [], "r")
        assert len(list(quarantine.glob("drift_*.json"))) <= 3
