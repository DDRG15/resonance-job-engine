"""
tests/test_title_prefilter.py — AUDIT NEW #1 (2026-07-10).

El SRP genérico devuelve cards off-topic (File Clerk, Crypto Trader) que
ensuciaban Tier 3. bucket_jobs ahora los descarta ANTES de puntuar, de forma
CONSERVADORA: solo cae lo que no comparte ningún token significativo.
"""

from matcher import title_is_relevant, bucket_jobs
from models import JobResult, SearchConfig

TARGETS = ["backend python developer", "sdet", "qa automation", "sre"]


class TestTitleIsRelevant:
    def test_matching_title_kept(self):
        assert title_is_relevant("Senior Python Backend Engineer", TARGETS)

    def test_partial_token_kept(self):
        # comparte "sdet"
        assert title_is_relevant("SDET II", TARGETS)

    def test_off_topic_dropped(self):
        assert not title_is_relevant("File Clerk", TARGETS)
        assert not title_is_relevant("Virtual Executive Assistant", TARGETS)
        assert not title_is_relevant("Junior Crypto Trader", TARGETS)

    def test_stopword_only_overlap_dropped(self):
        # "Senior Manager" vs "Senior Python" NO deben matchear por "senior"
        assert not title_is_relevant("Senior Manager", ["Senior Python Developer"])

    def test_profile_skill_rescues_relevance(self):
        # sin match en títulos pero sí en skills del perfil
        prof = {"core_skills": ["Django", "PostgreSQL"]}
        assert title_is_relevant("Django Specialist", ["sdet"], prof)

    def test_empty_search_keeps_everything(self):
        assert title_is_relevant("Anything At All", [])


class TestBucketFiltersOffTopic:
    def _job(self, title):
        return JobResult(title=title, company="X", url=f"https://e.com/{title}",
                         source_domain="e.com")

    def test_off_topic_excluded_from_all_tiers(self):
        cfg = SearchConfig(target_titles=TARGETS)
        jobs = [
            self._job("Senior Python Backend Engineer"),
            self._job("File Clerk"),
            self._job("Human Resources Assistant"),
            self._job("SDET II"),
        ]
        t1, t2, t3, t4 = bucket_jobs(jobs, cfg)
        all_titles = [tj.job.title for tj in (t1 + t2 + t3 + t4)]
        assert "File Clerk" not in all_titles
        assert "Human Resources Assistant" not in all_titles
        assert "Senior Python Backend Engineer" in all_titles
        assert "SDET II" in all_titles
