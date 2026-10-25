"""The HTTP transport every NSE adapter shares: one session, a cache and a budget.

NSE exposes its filings through two hosts: ``www.nseindia.com`` serves JSON APIs
and ``nsearchives.nseindia.com`` serves static files (the equity master CSV,
XBRL instances, PDFs). Both are unauthenticated and rate-limited, and a backtest
asks for dozens of windows in a row, so the details a single adapter would get
wrong live here once:

* requests to a bucket are spaced by at least the configured interval, with the
  last request time held in the process rather than in each caller;
* a throttled response (429/503) is retried once after ``Retry-After``, never
  hammered;
* every successful payload is cached on disk, so a repeated date window costs
  no request at all;
* three consecutive transport failures open a circuit for ten minutes, so a
  60-cell sweep reports "unavailable" instead of spending 60 timeouts.

Identification is a client name, not a disguise: the ``User-Agent`` says
``TradingAgents/<version>`` so the exchange can see who is calling. No browser
impersonation, cookie harvesting or login is involved. ``NSE_USER_AGENT``
replaces that string for a network that needs a different identification
string; it changes the label, not the identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

from tradingagents import __version__
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.files import replace_file

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
ARCHIVES_BASE = "https://nsearchives.nseindia.com"

_BASE_URLS = {"nse": NSE_BASE, "archives": ARCHIVES_BASE}

# A payment is a request, and these two answers mean "not now": honour the
# server's own delay when it names one.
_RETRYABLE_STATUSES = (429, 503)
_MAX_RETRY_AFTER_SECONDS = 60.0

# Three failures in a row is a block, not bad luck. Ten minutes is long enough
# for a sweep to finish reporting what it has and short enough that an operator
# who fixes their network does not wait an hour.
_FAILURE_THRESHOLD = 3
_CIRCUIT_SECONDS = 600.0

# One session for the process: connection reuse keeps the throttle meaningful,
# and a caller never has to construct its own with the right identification.
_SESSION = requests.Session()

# Injected at module scope so tests can advance time without sleeping and a
# caller can replace the clock without reaching into the request path.
_monotonic = time.monotonic
_sleep = time.sleep

# Last request per bucket, guarded by _LOCK. The sleep happens while holding it:
# two threads asking the same host must queue, not race to be the one that waits.
_LOCK = threading.Lock()
_LAST_REQUEST: dict[str, float] = {}

# Circuit-breaker state, process-local because a block hits this address, not a
# particular run. Its own lock keeps a failing request from stalling a reader.
_BREAKER_LOCK = threading.Lock()
_failures = 0
_open_until = 0.0

# The readable part of a cache key is capped so a long query string does not
# become a path too long for the filesystem; the digest keeps the full key
# distinct after truncation.
_MAX_READABLE_KEY = 80
_KEY_DIGEST_HEX = 8
_UNSAFE_KEY_CHAR = re.compile(r"[^A-Za-z0-9._-]")


def _user_agent() -> str:
    """Who NSE sees: the package name and version, never a browser's."""
    configured = os.getenv("NSE_USER_AGENT", "").strip()
    return configured or f"TradingAgents/{__version__}"


def _headers(accept: str) -> dict[str, str]:
    return {"User-Agent": _user_agent(), "Accept": accept}


def _interval(bucket: str) -> float:
    """Seconds between requests to ``bucket``; a stricter value never hurts."""
    from tradingagents.dataflows.config import get_config

    india = get_config().get("india") or {}
    configured = (india.get("min_interval_seconds") or {}).get(bucket)
    if configured is None:
        return {"archives": 1.0}.get(bucket, 2.0)
    return float(configured)


def _timeout() -> float:
    from tradingagents.dataflows.config import get_config

    return float((get_config().get("india") or {}).get("request_timeout_seconds", 30))


def _cache_key(key: str) -> str:
    """A filesystem-safe name for ``key`` that two different keys cannot share.

    The digest is what makes this safe: ``M&M`` and ``M_M`` both read ``M_M``
    after sanitising, and a cache that joined them would serve one issuer's
    payload for the other.
    """
    readable = _UNSAFE_KEY_CHAR.sub("_", key)[:_MAX_READABLE_KEY]
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:_KEY_DIGEST_HEX]
    return f"{readable}-{digest}"


def _cache_path(namespace: str, key: str) -> Path:
    from tradingagents.dataflows.config import get_config

    return Path(get_config()["data_cache_dir"]) / "nse" / namespace / _cache_key(key)


def _read_cache(path: Path, ttl_seconds: int) -> bytes | None:
    """The cached bytes while younger than ``ttl_seconds``, else None.

    A missing, truncated or unreadable file is a miss, not a failure: the cache
    is an optimisation and a partial write from an older version must not turn
    a normal request into an error.
    """
    try:
        if time.time() - path.stat().st_mtime >= ttl_seconds:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _write_cache(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_bytes(data))


def _note_failure() -> None:
    global _failures, _open_until
    with _BREAKER_LOCK:
        _failures += 1
        if _failures >= _FAILURE_THRESHOLD:
            _open_until = _monotonic() + _CIRCUIT_SECONDS


def _note_success() -> None:
    global _failures, _open_until
    with _BREAKER_LOCK:
        _failures = 0
        _open_until = 0.0


def _check_breaker() -> None:
    """Refuse the request while the circuit is open; let it half-close after."""
    global _failures, _open_until
    with _BREAKER_LOCK:
        if not _open_until:
            return
        if _monotonic() < _open_until:
            raise VendorUnavailableError("NSE circuit open after 3 failures")
        _failures = 0
        _open_until = 0.0


def _fail(message: str) -> None:
    _note_failure()
    raise VendorUnavailableError(message)


def _throttle(bucket: str) -> None:
    """Wait out the remainder of ``bucket``'s interval, then claim this request's slot."""
    interval = _interval(bucket)
    with _LOCK:
        now = _monotonic()
        last = _LAST_REQUEST.get(bucket)
        if last is not None:
            target = last + interval
            if now < target:
                _sleep(target - now)
                # A real sleep can overshoot or the injected one can be a no-op;
                # book the slot at or past the target either way.
                now = max(_monotonic(), target)
        _LAST_REQUEST[bucket] = now


def _retry_delay(response: Any, bucket: str) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        raw = response.headers.get("retry-after")
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return _interval(bucket)
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)


def _request_once(url: str, params: dict | None, bucket: str, accept: str) -> Any:
    _throttle(bucket)
    try:
        return _SESSION.request(
            "GET", url, params=params, headers=_headers(accept), timeout=_timeout()
        )
    except requests.RequestException as exc:
        # The router reacts to a typed error, not to a requests exception and
        # not to a message that happens to contain a response body.
        _note_failure()
        raise VendorUnavailableError(
            f"NSE {bucket} request failed ({type(exc).__name__})"
        ) from exc


def _fetch(url: str, *, params: dict | None, bucket: str, accept: str) -> Any:
    """One request, retried once on 429/503; every other failure is terminal."""
    _check_breaker()
    response = _request_once(url, params, bucket, accept)
    if response.status_code in _RETRYABLE_STATUSES:
        delay = _retry_delay(response, bucket)
        logger.warning(
            "NSE %s returned HTTP %s; retrying once in %.0fs",
            bucket, response.status_code, delay,
        )
        _sleep(delay)
        response = _request_once(url, params, bucket, accept)
        if response.status_code in _RETRYABLE_STATUSES:
            _fail(f"NSE {bucket} returned HTTP {response.status_code} after one retry")
    if response.status_code >= 400:
        _fail(f"NSE {bucket} returned HTTP {response.status_code}")
    return response


def _absolute(url: str, bucket: str) -> str:
    if url.lower().startswith(("http://", "https://")):
        return url
    return _BASE_URLS.get(bucket, NSE_BASE) + (url if url.startswith("/") else f"/{url}")


def get_json(
    path: str,
    params: dict | None = None,
    *,
    bucket: str = "nse",
    ttl_seconds: int,
    namespace: str,
    key: str,
) -> dict | list:
    """A cached JSON payload from ``bucket``.

    ``path`` is a path under the host (``/api/corporate-announcements``);
    ``key`` names the cache entry and must include everything that changes the
    answer (symbol, window, page), because the URL is not hashed for you.
    """
    cache_path = _cache_path(namespace, key)
    cached = _read_cache(cache_path, ttl_seconds)
    if cached is not None:
        try:
            payload = json.loads(cached)
        except ValueError:
            payload = None  # a truncated file is a miss, not a failure
        if isinstance(payload, (dict, list)):
            return payload

    response = _fetch(
        _absolute(path, bucket), params=params, bucket=bucket,
        accept="application/json, text/xml, */*",
    )
    try:
        payload = response.json()
    except ValueError as exc:
        _note_failure()
        raise VendorUnavailableError(f"NSE {bucket} returned unreadable JSON") from exc
    if not isinstance(payload, (dict, list)):
        _note_failure()
        raise VendorUnavailableError(f"NSE {bucket} returned unreadable JSON")

    _note_success()
    _write_cache(cache_path, json.dumps(payload).encode("utf-8"))
    return payload


def get_bytes(
    url: str,
    *,
    bucket: str = "archives",
    ttl_seconds: int,
    namespace: str,
    key: str,
) -> bytes:
    """A cached static file from ``bucket`` (CSV, XBRL, a PDF).

    ``url`` may be absolute or a path on the bucket's host. Static filing URLs
    are immutable, so a caller can safely give them a long TTL.
    """
    cache_path = _cache_path(namespace, key)
    cached = _read_cache(cache_path, ttl_seconds)
    if cached is not None:
        return cached

    response = _fetch(_absolute(url, bucket), params=None, bucket=bucket, accept="*/*")
    try:
        data = response.content
    except requests.RequestException as exc:
        _note_failure()
        raise VendorUnavailableError(
            f"NSE {bucket} response could not be read ({type(exc).__name__})"
        ) from exc

    _note_success()
    _write_cache(cache_path, data)
    return data


def reset_state() -> None:
    """Forget the breaker and the throttle clock.

    For a test that wants a clean process, and for a long-lived worker after an
    operator has changed the network or the interval config.
    """
    global _failures, _open_until
    with _LOCK:
        _LAST_REQUEST.clear()
    with _BREAKER_LOCK:
        _failures = 0
        _open_until = 0.0
