"""Real SQLite cursors survive failed nodes, ephemeral runtimes and process restarts."""

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import config
from graph.checkpoints import ResumeError, checkpoint_path, resume_info
from graph.run_graph import run_pipeline
from graph.runtime import RuntimeContext
from models import JobResult, SearchConfig
from scraper import GemaScraper


def job(n):
    return JobResult(title=f"Backend Engineer {n}", company="Acme",
                     url=f"https://jobs.example/{n}", source_domain="jobs.example")


@pytest.fixture
def phases(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BOARD_STRATEGY_PATH", str(tmp_path / "strategies.json"))
    calls = []
    fail = {"ats": True}

    def start(self):
        calls.append("init")
        return {"endpoint": [], "ats": [], "navegador": ["one.example", "two.example"]}

    def endpoint(self, plan):
        calls.append("endpoint")
        self.db.mark_seen_batch([job(1)], run_id=self.summary.run_id)
        self._all_jobs.append(job(1))
        return self.nuevos_avisos()

    def ats(self, plan):
        calls.append("ats")
        if fail["ats"]:
            # Durable data written after the previous node's checkpoint.
            self.db.mark_seen_batch([job(2)], run_id=self.summary.run_id)
            raise ValueError("interrupted after commit")
        return []

    def board(self, domain):
        calls.append(domain)
        self._all_jobs.append(job(3 if domain == "one.example" else 4))
        self.circuit.open_circuit(domain)
        if domain == "two.example":
            raise RuntimeError("board failed after collecting a job")
        return self.nuevos_avisos()

    monkeypatch.setattr(GemaScraper, "iniciar", start)
    monkeypatch.setattr(GemaScraper, "fase_endpoint", endpoint)
    monkeypatch.setattr(GemaScraper, "fase_ats", ats)
    monkeypatch.setattr(GemaScraper, "fase_gmail", lambda self: [])
    monkeypatch.setattr(GemaScraper, "preparar_navegador", lambda self, plan: plan["navegador"])
    monkeypatch.setattr(GemaScraper, "fase_board", board)
    return calls, fail


def crash(db, profile=None):
    with pytest.raises(ValueError, match="interrupted after commit"):
        run_pipeline(config=SearchConfig(target_titles=["Backend Engineer"]),
                     ctx=RuntimeContext(db=db), run_id="resume-test", profile=profile)


def test_resume_keeps_completed_sources_and_recovers_mid_node_commits(tmp_db, phases):
    calls, fail = phases
    crash(tmp_db)
    original = tmp_db.get_run("resume-test")
    assert resume_info(tmp_db, "resume-test")["resumable"]
    fail["ats"] = False
    result = run_pipeline(config=None, ctx=RuntimeContext(db=tmp_db),
                          run_id="resume-test", resume=True)
    assert calls.count("init") == calls.count("endpoint") == 1
    assert calls.count("ats") == 2
    assert {j.url for j in result["jobs"]} == {job(n).url for n in range(1, 5)}
    assert result["summary"].new_processed == 4
    assert set(result["summary"].domains_aborted) == {"one.example", "two.example"}
    assert any("board failed" in error for error in result["errors"])
    assert tmp_db.get_run("resume-test")["started_at"] == original["started_at"]
    assert tmp_db.get_run("resume-test")["status"] == "ok"
    assert not resume_info(tmp_db, "resume-test")["resumable"]
    assert tmp_db.get_run_jobs("resume-test") == []


def test_cv_is_absent_from_checkpoint_and_must_match_before_resume(tmp_db, phases):
    profile = {"role": "ULTRASECRET_CV_SENTINEL", "core_skills": ["python"]}
    crash(tmp_db, profile)
    path = checkpoint_path(tmp_db)
    assert b"ULTRASECRET_CV_SENTINEL" not in path.read_bytes()
    assert resume_info(tmp_db, "resume-test")["resume_requires_cv"]
    assert not resume_info(tmp_db, "resume-test", profile)["resume_requires_cv"]
    with pytest.raises(ResumeError, match="mismo perfil"):
        run_pipeline(config=None, ctx=RuntimeContext(db=tmp_db), run_id="resume-test",
                     resume=True, profile={"role": "different"})
    assert tmp_db.get_run("resume-test")["status"] == "crashed"


def test_same_cv_can_be_reparsed_without_requiring_identical_llm_wording():
    from graph.checkpoints import profile_fingerprint
    first = {"_cv_source_hash": "a" * 64, "role": "Backend", "core_skills": ["Python", "SQL"]}
    reuploaded = {"_cv_source_hash": "a" * 64, "role": "Backend Engineer", "core_skills": ["SQL", "Python"]}
    assert profile_fingerprint(first) == profile_fingerprint(reuploaded)
    assert profile_fingerprint(first) != profile_fingerprint({**reuploaded, "_cv_source_hash": "b" * 64})


def test_completed_run_rejects_replay_without_overwriting_success(tmp_db, phases):
    phases[1]["ats"] = False
    run_pipeline(config=SearchConfig(target_titles=["Backend Engineer"]),
                 ctx=RuntimeContext(db=tmp_db), run_id="done-test")
    with pytest.raises(ResumeError):
        run_pipeline(config=None, ctx=RuntimeContext(db=tmp_db), run_id="done-test", resume=True)
    assert tmp_db.get_run("done-test")["status"] == "ok"


def test_approval_hook_pauses_and_resumes_with_a_decision(tmp_db, phases):
    phases[1]["ats"] = False
    result = run_pipeline(config=SearchConfig(target_titles=["Backend Engineer"]),
        ctx=RuntimeContext(db=tmp_db), run_id="approval-test",
        approval_request={"action": "review", "message": "Continue?"})
    assert result["status"] == "paused"
    assert result["__interrupt__"][0].value["action"] == "review"
    result = run_pipeline(config=None, ctx=RuntimeContext(db=tmp_db), run_id="approval-test",
                          resume=True, approval_response=False)
    assert result["approval_result"] is False
    assert result["status"] == "ok"


def test_runtime_snapshot_restores_breaker_and_shared_fallback_budget(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BOARD_STRATEGY_PATH", str(tmp_path / "strategies.json"))
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_BOARDS_PER_RUN", 2)
    sc = GemaScraper(SearchConfig(target_titles=["Backend"]), tmp_db, None)
    sc.circuit.open_circuit("jobs.example")
    sc._rate_hits_by_domain["jobs.example"] = 3
    sc._visited_srp_urls.add("https://jobs.example/search")
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(lambda _: sc._reserve_fallback(), range(10))) == 2
    restored = GemaScraper(sc.config, tmp_db, None)
    restored.restaurar_runtime(sc.snapshot_runtime())
    assert restored.circuit.is_open("jobs.example")
    assert restored._rate_hits_by_domain["jobs.example"] == 3
    assert restored._visited_srp_urls == sc._visited_srp_urls
    assert not restored._reserve_fallback()


def test_reading_missing_checkpoint_creates_no_files(tmp_path):
    from types import SimpleNamespace
    db = SimpleNamespace(db_path=str(tmp_path / "absent.db"))
    assert not resume_info(db, "missing")["resumable"]
    assert list(tmp_path.iterdir()) == []


def test_fallback_reservations_survive_a_new_runtime(tmp_db):
    assert tmp_db.reserve_run_fallback("budget", 2)
    assert tmp_db.reserve_run_fallback("budget", 2)
    from database import GemaDatabase
    recreated = GemaDatabase(tmp_db.db_path)
    assert not recreated.reserve_run_fallback("budget", 2)


def test_registry_and_run_journal_commit_together(tmp_db):
    tmp_db.mark_seen_batch([job(1)], run_id="atomic")
    assert tmp_db.is_seen(job(1).url)[0]
    assert tmp_db.get_run_jobs("atomic")[0].url == job(1).url
    with sqlite3.connect(tmp_db.db_path) as conn:
        conn.execute("CREATE TRIGGER reject_run_job BEFORE INSERT ON run_jobs "
                     "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.mark_seen_batch([job(2)], run_id="atomic")
    assert not tmp_db.is_seen(job(2).url)[0]


def test_api_resumes_saved_run_and_exposes_availability(tmp_db, phases, monkeypatch, tmp_path):
    import api
    import run_manager
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "GEMA_DB_PATH", tmp_db.db_path)
    monkeypatch.setattr(api, "API_STATE_FILE", tmp_path / "api-state.json")
    monkeypatch.setattr(run_manager, "_fire_discord", lambda message: None)
    crash(tmp_db)
    phases[1]["ats"] = False
    with TestClient(api.app) as client:
        response = client.get("/api/results").json()
        assert response["last_run"]["resumable"] is True
        result = client.post("/api/scrape/resume-test/resume")
        assert result.status_code == 202, result.text
        api.app.state.runs.thread.join(timeout=60)
        response = client.get("/api/results").json()
        assert response["is_running"] is False
        assert response["has_results"] is True
        assert phases[0].count("endpoint") == 1


def test_api_missing_cv_releases_gate_without_rewriting_failure(tmp_db, phases, monkeypatch, tmp_path):
    import api
    import run_manager
    from fastapi.testclient import TestClient
    from scraper import scrape_gate_is_busy
    monkeypatch.setattr(config, "GEMA_DB_PATH", tmp_db.db_path)
    monkeypatch.setattr(api, "API_STATE_FILE", tmp_path / "api-state.json")
    monkeypatch.setattr(run_manager, "_fire_discord", lambda message: None)
    crash(tmp_db, {"role": "Backend", "core_skills": ["python"]})
    with TestClient(api.app) as client:
        assert client.get("/api/results").json()["last_run"]["resume_requires_cv"]
        result = client.post("/api/scrape/resume-test/resume")
        assert result.status_code == 409
        assert "mismo perfil" in result.json()["detail"]
        assert not scrape_gate_is_busy()
        assert tmp_db.get_run("resume-test")["status"] == "crashed"


def test_upload_cv_keeps_a_stable_private_identity_for_resume(tmp_db, monkeypatch, tmp_path):
    import api
    from fastapi.testclient import TestClient
    from graph.checkpoints import profile_fingerprint
    monkeypatch.setattr(config, "GEMA_DB_PATH", tmp_db.db_path)
    monkeypatch.setattr(api, "API_STATE_FILE", tmp_path / "api-state.json")
    monkeypatch.setattr(api, "extract_cv_text", lambda filename, data: data.decode())
    roles = iter(["Backend", "Backend Engineer"])
    monkeypatch.setattr(api, "generate_ephemeral_profile", lambda *args, **kwargs: {"role": next(roles)})
    with TestClient(api.app) as client:
        hashes = []
        for _ in range(2):
            response = client.post("/api/cv", files={"file": ("cv.pdf", b"same CV text", "application/pdf")},
                                   data={"llm_consent": config.CV_CONSENT_VERSION})
            assert response.status_code == 200, response.text
            assert "_cv_source_hash" not in response.json()["profile"]
            assert "_cv_source_hash" not in client.get("/api/cv").json()["profile"]
            hashes.append(profile_fingerprint(api.app.state.profile))
        assert hashes[0] == hashes[1]
        assert not (tmp_path / "cv_profile.json").exists()


def test_cursor_survives_abrupt_process_exit(tmp_path):
    script = tmp_path / "run_process.py"
    script.write_text(textwrap.dedent('''
        import os, sys, json
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        import config
        config.BOARD_STRATEGY_PATH = str(Path(sys.argv[2]).with_suffix('.json'))
        from database import GemaDatabase
        from scraper import GemaScraper
        from graph.run_graph import run_pipeline
        from graph.runtime import RuntimeContext
        from models import SearchConfig, JobResult
        db = GemaDatabase(sys.argv[2])
        def job(n):
            return JobResult(title='Backend Engineer', company='Acme',
                             url=f'https://jobs.example/{n}', source_domain='jobs.example')
        def endpoint(self, plan):
            self.db.mark_seen_batch([job(1)], run_id=self.summary.run_id)
            self._all_jobs.append(job(1))
            return self.nuevos_avisos()
        def ats(self, plan):
            if sys.argv[3] == 'crash':
                self.db.mark_seen_batch([job(2)], run_id=self.summary.run_id)
                os._exit(23)
            return []
        GemaScraper.iniciar = lambda self: {'endpoint': [], 'ats': [], 'navegador': []}
        GemaScraper.fase_endpoint = endpoint
        GemaScraper.fase_ats = ats
        GemaScraper.fase_gmail = lambda self: []
        GemaScraper.preparar_navegador = lambda self, plan: []
        result = run_pipeline(config=SearchConfig(target_titles=['Backend Engineer']),
            ctx=RuntimeContext(db=db), run_id='process-test', resume=sys.argv[3] == 'resume')
        print(json.dumps({'status': result['status'], 'urls': [j.url for j in result['jobs']]}))
    '''), encoding="utf-8")
    root = str(Path(__file__).resolve().parent.parent)
    command = [sys.executable, str(script), root, str(tmp_path / "registry.db")]
    first = subprocess.run(command + ["crash"], capture_output=True, text=True, timeout=120)
    assert first.returncode == 23, first.stderr[-2500:]
    second = subprocess.run(command + ["resume"], capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr[-2500:]
    import json
    result = json.loads(second.stdout.strip().splitlines()[-1])
    assert result["status"] == "ok"
    assert set(result["urls"]) == {"https://jobs.example/1", "https://jobs.example/2"}
