"""
tests/test_session_jar.py — cookie jar auto-persistente (el usuario, 2026-07-18).

Verifica la lógica que NO necesita browser vivo: persistencia por dominio,
agrupado correcto de cookies, round-trip capture→inject, fail-safe. La
verificación anti-bot REAL (¿el cf_clearance salta el challenge?) es del LIVE
TEST — no se puede probar sin un scrape real que pase el desafío.
"""

import json
import pytest

from integrations.session_jar import SessionJar


class FakeContext:
    """Context Playwright mínimo: cookies() devuelve lo sembrado; add_cookies
    acumula. Async como el real."""
    def __init__(self, cookies=None):
        self._cookies = cookies or []
        self.added = []

    async def cookies(self):
        return self._cookies

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


def _ck(name, domain, value="x"):
    return {"name": name, "value": value, "domain": domain, "path": "/"}


@pytest.mark.asyncio
class TestCaptureAndInject:
    async def test_capture_groups_by_domain(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        ctx = FakeContext([
            _ck("cf_clearance", ".wellfound.com"),
            _ck("session", "wellfound.com"),
            _ck("cf_clearance", ".startup.jobs"),
            _ck("_ga", ".google-analytics.com"),   # tercero → NO se guarda
        ])
        saved = await jar.capture(ctx, ["wellfound.com", "startup.jobs"])
        assert saved == 3  # 2 de wellfound + 1 de startup, google excluido
        wf = json.loads((tmp_path / "wellfound.com.json").read_text("utf-8"))
        assert {c["name"] for c in wf} == {"cf_clearance", "session"}
        assert (tmp_path / "startup.jobs.json").exists()
        assert not (tmp_path / "google-analytics.com.json").exists()

    async def test_subdomain_cookie_belongs_to_board(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        ctx = FakeContext([_ck("cf_bm", "www.himalayas.app")])
        saved = await jar.capture(ctx, ["himalayas.app"])
        assert saved == 1  # subdominio pertenece al board

    async def test_roundtrip_capture_then_inject(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        src = FakeContext([_ck("cf_clearance", ".wellfound.com")])
        await jar.capture(src, ["wellfound.com"])
        dst = FakeContext()
        n = await jar.inject("wellfound.com", dst)
        assert n == 1
        assert dst.added[0]["name"] == "cf_clearance"

    async def test_inject_missing_file_is_zero(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        assert await jar.inject("nunca.com", FakeContext()) == 0

    async def test_capture_no_matching_domain_saves_nothing(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        ctx = FakeContext([_ck("x", ".otro.com")])
        assert await jar.capture(ctx, ["wellfound.com"]) == 0
        assert not (tmp_path / "wellfound.com.json").exists()

    async def test_capture_failsafe_on_bad_context(self, tmp_path):
        class Boom:
            async def cookies(self):
                raise RuntimeError("context muerto")
        jar = SessionJar(jar_dir=str(tmp_path))
        # no propaga — best-effort
        assert await jar.capture(Boom(), ["wellfound.com"]) == 0

    async def test_inject_failsafe_on_bad_context(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        (tmp_path / "wellfound.com.json").write_text(
            json.dumps([_ck("cf_clearance", ".wellfound.com")]), encoding="utf-8"
        )
        class Boom:
            async def add_cookies(self, c):
                raise RuntimeError("no se pudo")
        assert await jar.inject("wellfound.com", Boom()) == 0

    async def test_leading_dot_domain_normalized(self, tmp_path):
        jar = SessionJar(jar_dir=str(tmp_path))
        ctx = FakeContext([_ck("cf_clearance", ".wellfound.com")])
        await jar.capture(ctx, [".wellfound.com"])
        # el archivo usa el dominio sin punto
        assert (tmp_path / "wellfound.com.json").exists()
