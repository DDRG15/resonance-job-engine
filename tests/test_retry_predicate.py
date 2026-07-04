"""
tests/test_retry_predicate.py — Tests for the LLM retry predicate (Phase 23, AUDIT #4).

The old predicate retried EVERYTHING except rate-limits — so a deterministic
error (401 auth, 400 malformed) was retried 3× identically, burning calls +
backoff before falling to the next provider. `_is_transient` narrows retry to
failures a fresh attempt can win: timeouts, connection blips, 5xx. Deterministic
4xx now fail fast → next provider immediately.
"""

import pytest
import requests

import nlp_engine
from nlp_engine import _is_transient, _ProviderRateLimited


class _WithStatus(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _WithResponse(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = type("R", (), {"status_code": status_code})()


class TestIsTransient:

    def test_rate_limited_not_retried(self):
        assert _is_transient(_ProviderRateLimited("429")) is False

    def test_requests_timeout_is_transient(self):
        assert _is_transient(requests.exceptions.Timeout("timed out")) is True

    def test_requests_connection_error_is_transient(self):
        assert _is_transient(requests.exceptions.ConnectionError("conn reset")) is True

    def test_5xx_is_transient_direct_status(self):
        assert _is_transient(_WithStatus(500)) is True
        assert _is_transient(_WithStatus(503)) is True

    def test_5xx_is_transient_nested_response(self):
        assert _is_transient(_WithResponse(502)) is True

    def test_401_is_deterministic_fail_fast(self):
        assert _is_transient(_WithStatus(401)) is False

    def test_400_is_deterministic_fail_fast(self):
        assert _is_transient(_WithResponse(400)) is False

    def test_429_status_is_not_transient(self):
        # 429 is a rate limit, handled by the skip flag — never a retry.
        assert _is_transient(_WithStatus(429)) is False

    def test_value_error_no_status_fail_fast(self):
        assert _is_transient(ValueError("bad json")) is False


class TestBackoffBehaviour:

    def test_deterministic_error_called_once(self):
        # The whole point of the fix: a deterministic error is NOT retried 3×.
        calls = {"n": 0}

        @nlp_engine._backoff
        def f():
            calls["n"] += 1
            raise _WithStatus(401)   # deterministic → _is_transient False

        with pytest.raises(_WithStatus):
            f()
        assert calls["n"] == 1   # one attempt, no wasted retries

    def test_rate_limited_called_once(self):
        calls = {"n": 0}

        @nlp_engine._backoff
        def f():
            calls["n"] += 1
            raise _ProviderRateLimited("429")

        with pytest.raises(_ProviderRateLimited):
            f()
        assert calls["n"] == 1
