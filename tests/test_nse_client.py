"""NSE client: spacing, retry-once, cache freshness and the failure budget.

All tests are offline: ``_SESSION.request`` is replaced, the clock and sleep are
injected, and the cache lives in the test's own directory.
"""

from __future__ import annotations

import json
import os
import re
import time
from copy import deepcopy

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.nse import client

_NS = "test-namespace"


class _Clock:
    """A monotonic clock that only moves when a test sleeps on purpose."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 0.0)


class _Response:
    """The slice of ``requests.Response`` the client reads."""

    def __init__(self, status_code=200, payload=None, content=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self._content = content
        self.headers = headers or {}

    @property
    def content(self) -> bytes:
        if self._content is not None:
            return self._content
        return json.dumps(self._payload).encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("response has no JSON")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok(payload):
    return _Response(payload=payload)


def _install(monkeypatch, clock, responses):
    """Answer each request from ``responses`` (the last one repeats) and log the time."""
    calls: list[float] = []

    def request(method, url, **kwargs):
        calls.append(clock.monotonic())
        item = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(client._SESSION, "request", request)
    return calls


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(client, "_monotonic", clock.monotonic)
    monkeypatch.setattr(client, "_sleep", clock.sleep)
    return clock


@pytest.fixture(autouse=True)
def _own_cache(monkeypatch, tmp_path):
    """The suite's cache directory is shared by all tests; this test gets its own."""
    cfg = deepcopy(default_config.DEFAULT_CONFIG)
    cfg["data_cache_dir"] = str(tmp_path / "cache")
    monkeypatch.setattr(config_module, "get_config", lambda: deepcopy(cfg))
    client.reset_state()
    yield tmp_path / "cache"
    client.reset_state()


@pytest.mark.unit
def test_requests_to_one_bucket_are_spaced_by_the_configured_interval(monkeypatch, clock):
    calls = _install(monkeypatch, clock, [_ok({"ok": 1})])

    client.get_json("/api/a", ttl_seconds=3600, namespace=_NS, key="a")
    client.get_json("/api/b", ttl_seconds=3600, namespace=_NS, key="b")

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 2.0


@pytest.mark.unit
def test_a_429_is_retried_once_after_retry_after_then_succeeds(monkeypatch, clock):
    calls = _install(monkeypatch, clock, [
        _Response(429, headers={"Retry-After": "5"}),
        _ok({"ok": True}),
    ])

    assert client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key="a") == {"ok": True}
    assert len(calls) == 2
    assert 5.0 in clock.slept


@pytest.mark.unit
def test_a_second_429_is_unavailable_not_retried_forever(monkeypatch, clock):
    calls = _install(monkeypatch, clock, [_Response(429, headers={"Retry-After": "1"})])

    with pytest.raises(VendorUnavailableError):
        client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key="a")
    assert len(calls) == 2


@pytest.mark.unit
def test_a_403_is_unavailable_and_never_echoes_the_response_body(monkeypatch, clock):
    body = b'{"detail": "SECRET-BODY-MARKER"}'
    _install(monkeypatch, clock, [_Response(403, content=body)])

    with pytest.raises(VendorUnavailableError) as excinfo:
        client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key="a")

    message = str(excinfo.value)
    assert "403" in message
    assert "SECRET-BODY-MARKER" not in message


@pytest.mark.unit
def test_unreadable_json_is_unavailable(monkeypatch, clock):
    _install(monkeypatch, clock, [_Response(200, content=b"<html>not json</html>")])

    with pytest.raises(VendorUnavailableError, match="unreadable JSON"):
        client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key="a")


@pytest.mark.unit
def test_a_cache_hit_makes_no_request_until_the_ttl_expires(monkeypatch, clock, _own_cache):
    calls = _install(monkeypatch, clock, [_ok({"v": 1}), _ok({"v": 2})])

    first = client.get_json("/api/a", ttl_seconds=3600, namespace=_NS, key="a")
    second = client.get_json("/api/a", ttl_seconds=3600, namespace=_NS, key="a")
    assert first == second == {"v": 1}
    assert len(calls) == 1

    cached = _own_cache / "nse" / _NS / client._cache_key("a")
    stale = time.time() - 7200
    cached.touch()
    os.utime(cached, (stale, stale))

    assert client.get_json("/api/a", ttl_seconds=3600, namespace=_NS, key="a") == {"v": 2}
    assert len(calls) == 2


@pytest.mark.unit
def test_a_truncated_cache_file_is_a_miss_not_a_failure(monkeypatch, clock, _own_cache):
    calls = _install(monkeypatch, clock, [_ok({"v": 9})])
    cached = _own_cache / "nse" / _NS / client._cache_key("a")
    cached.parent.mkdir(parents=True)
    cached.write_text('{"truncated": ', encoding="utf-8")

    assert client.get_json("/api/a", ttl_seconds=3600, namespace=_NS, key="a") == {"v": 9}
    assert len(calls) == 1


@pytest.mark.unit
def test_cache_keys_are_safe_distinct_and_bounded():
    ampersand = client._cache_key("M&M")
    underscore = client._cache_key("M_M")

    assert ampersand != underscore
    for key in (ampersand, underscore, client._cache_key("x" * 500)):
        assert len(key) <= 90
        assert re.fullmatch(r"[A-Za-z0-9._-]+", key)


@pytest.mark.unit
def test_three_failures_open_the_circuit_and_reset_state_closes_it(monkeypatch, clock):
    calls = _install(monkeypatch, clock, [
        _Response(403), _Response(403), _Response(403), _ok({"ok": True}),
    ])

    for index in range(3):
        with pytest.raises(VendorUnavailableError):
            client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key=f"a{index}")
    assert len(calls) == 3

    with pytest.raises(VendorUnavailableError, match="circuit open after 3 failures"):
        client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key="blocked")
    assert len(calls) == 3

    client.reset_state()
    assert client.get_json("/api/a", ttl_seconds=0, namespace=_NS, key="after") == {"ok": True}
    assert len(calls) == 4


@pytest.mark.unit
def test_a_news_host_failure_is_not_labelled_nse(monkeypatch, clock):
    """The same transport serves Google News and GDELT; the reader must not be
    sent looking at NSE when one of those throttled."""
    _install(monkeypatch, clock, [
        _Response(503, headers={"Retry-After": "1"}),
        _Response(503, headers={"Retry-After": "1"}),
    ])

    with pytest.raises(
        VendorUnavailableError, match=r"^google_news returned HTTP 503 after one retry$"
    ):
        client.get_bytes(
            "https://news.google.com/rss/search?q=x",
            bucket="google_news",
            ttl_seconds=0,
            namespace=_NS,
            key="google",
        )
