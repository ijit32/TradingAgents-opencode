"""The newline-delimited JSON protocol between the plugin and ``serve``.

One frame per line. A request names an action and a run directory; a reply
echoes the request id so concurrent actions cannot be confused. The protocol is
versioned explicitly: a client that speaks a newer or older version is refused
instead of being served a reply it may misread.

The schema is canonical in
``.opencode/plugins/tradingagents/protocol.schema.json``. The TypeScript types
in ``protocol.generated.ts`` are generated from it; this module is the Python
side of the same contract, and ``tests/test_opencode_protocol.py`` validates
both against the schema.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

PROTOCOL_VERSION = 1

# Control actions are answered by the server itself and never touch a run.
CONTROL_ACTIONS = ("ping", "shutdown")

# Every action the server accepts. Keep in sync with WorkerAction in
# protocol.schema.json.
REQUEST_ACTIONS = (
    "init",
    "plan",
    "collect",
    "prompt",
    "validate",
    "debates",
    "memory-begin",
    "memory-apply",
    "settle",
    "backtest-plan",
    "backtest-summary",
    "status",
)

ACTIONS = REQUEST_ACTIONS + CONTROL_ACTIONS

_REQUEST_KEYS = frozenset(
    {
        "protocol_version",
        "id",
        "action",
        "run_dir",
        "stage",
        "analyst",
        "lenient",
        "json_args",
    }
)

_log_lock = threading.Lock()


class ProtocolError(ValueError):
    """A frame the server cannot accept; carries a one-shot exit code."""

    exit_code = 3


@dataclass(frozen=True)
class RequestFrame:
    """One validated request line."""

    id: int
    action: str
    run_dir: str | None = None
    stage: str | None = None
    analyst: str | None = None
    lenient: bool = False
    json_args: dict[str, Any] = field(default_factory=dict)

    @property
    def is_control(self) -> bool:
        return self.action in CONTROL_ACTIONS

    @classmethod
    def from_mapping(cls, raw: Any) -> RequestFrame:
        if not isinstance(raw, dict):
            raise ProtocolError("request frame must be a JSON object")

        unknown = sorted(set(raw) - _REQUEST_KEYS)
        if unknown:
            raise ProtocolError(f"unknown request field(s): {', '.join(unknown)}")

        version = raw.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"protocol_version {version!r} is not supported; expected {PROTOCOL_VERSION}"
            )

        request_id = raw.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
            raise ProtocolError("id must be a positive integer")

        action = raw.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ProtocolError(f"unknown action {action!r}; known: {', '.join(ACTIONS)}")

        run_dir = _optional_text(raw.get("run_dir"), "run_dir")
        if action not in CONTROL_ACTIONS:
            if not run_dir:
                raise ProtocolError(f"run_dir is required for action {action!r}")
            if not run_dir.startswith(("/", "\\")) and not _is_windows_absolute(run_dir):
                raise ProtocolError(f"run_dir must be absolute, got {run_dir!r}")

        lenient = raw.get("lenient", False)
        if not isinstance(lenient, bool):
            raise ProtocolError("lenient must be a boolean")

        json_args = raw.get("json_args", {})
        if not isinstance(json_args, dict):
            raise ProtocolError("json_args must be a JSON object")

        return cls(
            id=request_id,
            action=action,
            run_dir=run_dir,
            stage=_optional_text(raw.get("stage"), "stage"),
            analyst=_optional_text(raw.get("analyst"), "analyst"),
            lenient=lenient,
            json_args=json_args,
        )


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ProtocolError(f"{name} cannot be empty")
    return text


def _is_windows_absolute(path: str) -> bool:
    return len(path) >= 3 and path[1] == ":" and path[2] in "\\/"


def reply_frame(
    request_id: int | None,
    *,
    action: str | None,
    payload: dict[str, Any],
    exit_code: int,
    duration_ms: int,
    ready: bool | None = None,
) -> dict[str, Any]:
    """A reply with the envelope on top of the action's own payload."""
    frame: dict[str, Any] = dict(payload)
    frame.pop("protocol_version", None)
    frame["protocol_version"] = PROTOCOL_VERSION
    frame["id"] = request_id
    frame["ok"] = bool(payload.get("ok", exit_code == 0))
    frame["exit_code"] = exit_code
    frame["duration_ms"] = max(0, int(duration_ms))
    if action is not None:
        frame["action"] = action
    if ready is not None:
        frame["ready"] = ready
    return frame


def error_frame(
    request_id: int | None,
    message: str,
    *,
    action: str | None = None,
    exit_code: int = 3,
) -> dict[str, Any]:
    return reply_frame(
        request_id,
        action=action,
        payload={"ok": False, "error": message},
        exit_code=exit_code,
        duration_ms=0,
    )


def encode(frame: dict[str, Any]) -> str:
    """One frame, newline-terminated, with no embedded newlines."""
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_frame(stream: TextIO, frame: dict[str, Any], lock: threading.Lock | None = None) -> None:
    line = encode(frame)
    if lock is None:
        stream.write(line)
        stream.flush()
        return
    with lock:
        stream.write(line)
        stream.flush()


def log_event(event: str, *, stream: TextIO | None = None, **fields: Any) -> None:
    """One structured log line on stderr.

    stdout is the protocol channel, so logs never go there. The line is JSON so
    the plugin can parse it instead of scraping human text.
    """
    record: dict[str, Any] = {
        "event": event,
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
    record.update(fields)
    target = stream if stream is not None else sys.stderr
    line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
    with _log_lock:
        target.write(line)
        target.flush()


def parse_line(line: str) -> tuple[Any, int | None]:
    """Decode one line, recovering the id when the rest of the frame is bad.

    The id lets the client fail exactly one request instead of dropping the
    session when a single frame is malformed.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    recovered = None
    if isinstance(raw, dict):
        candidate = raw.get("id")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 1:
            recovered = candidate
    return raw, recovered


def monotonic_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
