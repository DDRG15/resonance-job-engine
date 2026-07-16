"""
tests/test_manual_review.py — Phase 30a (salvage-to-manual-review).

Jobs con title+company REALES pero URL no verificable → pila de revisión manual
en vez de la basura. Descarta solo los verdaderamente inventados (título que
tampoco está en la fuente). SEAM 30b: se guardan original_url + srp_url +
resolved_url para que 30b aprenda el patrón por board.
"""

import scraper
from database import GemaDatabase
from models import JobResult


def _job(title, company="ACME", domain="builtin.com",
         url="https://builtin.com/job/relative-bad"):
    return JobResult(title=title, company=company, url=url, source_domain=domain)


class TestReviewDB:
    def _db(self, tmp_path):
        return GemaDatabase(db_path=str(tmp_path / "r.db"))

    def test_add_and_list(self, tmp_path):
        db = self._db(tmp_path)
        assert db.add_to_manual_review(
            _job("Senior Python Engineer"), "https://builtin.com/jobs/remote?search=python",
            reason="url_no_verificable_titulo_real",
        ) is True
        rows = db.get_manual_review()
        assert len(rows) == 1
        r = rows[0]
        assert r["title"] == "Senior Python Engineer"
        assert r["srp_url"].endswith("search=python")
        assert r["original_url"] == "https://builtin.com/job/relative-bad"  # SEAM 30b
        assert r["resolved"] == 0

    def test_idempotent(self, tmp_path):
        db = self._db(tmp_path)
        db.add_to_manual_review(_job("QA Engineer"), "http://x", "r")
        # mismo title+company+domain → no duplica
        assert db.add_to_manual_review(_job("QA Engineer"), "http://x", "r") is False
        assert len(db.get_manual_review()) == 1

    def test_resolve_with_url(self, tmp_path):
        db = self._db(tmp_path)
        db.add_to_manual_review(_job("SDET"), "http://srp", "r")
        h = db.get_manual_review()[0]["review_hash"]
        assert db.resolve_manual_review(h, "https://builtin.com/job/sdet-real-123") is True
        # ya no aparece en pendientes
        assert db.get_manual_review() == []
        # aparece con include_resolved, con la URL humana (SEAM 30b)
        allrows = db.get_manual_review(include_resolved=True)
        assert allrows[0]["resolved"] == 1
        assert allrows[0]["resolved_url"] == "https://builtin.com/job/sdet-real-123"

    def test_resolve_dismiss_none(self, tmp_path):
        db = self._db(tmp_path)
        db.add_to_manual_review(_job("Ruby Dev"), "http://srp", "r")
        h = db.get_manual_review()[0]["review_hash"]
        assert db.resolve_manual_review(h, None) is True  # descartado
        assert db.get_manual_review(include_resolved=True)[0]["resolved_url"] is None

    def test_resolve_unknown_returns_false(self, tmp_path):
        db = self._db(tmp_path)
        assert db.resolve_manual_review("nope", "http://x") is False

    def test_stats(self, tmp_path):
        db = self._db(tmp_path)
        db.add_to_manual_review(_job("A"), "http://s", "r")
        db.add_to_manual_review(_job("B"), "http://s", "r")
        h = db.get_manual_review()[0]["review_hash"]
        db.resolve_manual_review(h, "http://real")
        assert db.get_review_stats() == {"pending": 1, "resolved": 1}


class TestScraperSalvageDiscriminator:
    """El discriminador: título en la fuente → salvar; título ausente → basura."""

    def _scraper(self, db):
        s = scraper.GemaScraper.__new__(scraper.GemaScraper)
        s.db = db
        s._log = lambda msg: None
        return s

    def test_real_title_salvaged_fake_discarded(self, tmp_path, monkeypatch):
        db = GemaDatabase(db_path=str(tmp_path / "s.db"))
        s = self._scraper(db)
        # fuente contiene "Senior Backend Engineer" pero NO "Ghost Job Inventado"
        source = "... Senior Backend Engineer at Encora, remote, apply now ..."
        dropped = [
            _job("Senior Backend Engineer", company="Encora"),   # real → salvar
            _job("Ghost Job Inventado", company="Nowhere"),       # inventado → basura
        ]
        s._salvage_dropped_jobs(dropped, source, "builtin.com", "http://srp")
        rows = db.get_manual_review()
        assert len(rows) == 1
        assert rows[0]["title"] == "Senior Backend Engineer"
        assert rows[0]["company"] == "Encora"

    def test_salvage_is_fail_safe(self, tmp_path):
        # db que lanza no debe romper el scrape
        class _BadDB:
            def add_to_manual_review(self, *a, **k):
                raise RuntimeError("db down")
        s = scraper.GemaScraper.__new__(scraper.GemaScraper)
        s.db = _BadDB()
        s._log = lambda msg: None
        # título en fuente → intenta salvar → db lanza → NO propaga
        s._salvage_dropped_jobs(
            [_job("Real Title")], "Real Title here", "x.com", "http://s"
        )
