"""The long-lived worker: newline-delimited JSON in, newline-delimited JSON out.

The plugin spawns this once per OpenCode session and sends one action per line.
Requests are answered on a small thread pool, so a slow data collection does not
block a status poll, and each reply echoes its request id. A malformed frame
fails only that request; the server stays up.

    python -m tradingagents.opencode_worker serve
"""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TextIO

from tradingagents.opencode.cli import WorkerArgs, dispatch
from tradingagents.opencode.contract import EXIT_INTERNAL, EXIT_OK
from tradingagents.opencode.protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    RequestFrame,
    error_frame,
    log_event,
    monotonic_ms,
    parse_line,
    reply_frame,
    write_frame,
)

DEFAULT_WORKERS = 4


def _worker_count() -> int:
    raw = os.environ.get("TA_WORKER_THREADS", "").strip()
    if not raw:
        return DEFAULT_WORKERS
    try:
        return max(1, min(16, int(raw)))
    except ValueError:
        return DEFAULT_WORKERS


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    workers: int | None = None,
) -> int:
    """Serve frames until stdin closes or a ``shutdown`` request arrives."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    write_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=workers or _worker_count(), thread_name_prefix="ta-worker")
    shutdown_id: int | None = None
    shutdown_started = 0.0

    def emit(frame: dict[str, Any]) -> None:
        write_frame(sink, frame, write_lock)

    def run_request(frame: RequestFrame) -> None:
        started = time.perf_counter()
        payload, code = dispatch(
            frame.action,
            WorkerArgs(
                run_dir=frame.run_dir,
                stage=frame.stage,
                analyst=frame.analyst,
                lenient=frame.lenient,
                json_args=frame.json_args,
            ),
        )
        duration_ms = monotonic_ms(started)
        emit(
            reply_frame(
                frame.id,
                action=frame.action,
                payload=payload,
                exit_code=code,
                duration_ms=duration_ms,
            )
        )
        log_event(
            "worker.request",
            id=frame.id,
            action=frame.action,
            run_dir=frame.run_dir,
            ok=code == EXIT_OK,
            exit_code=code,
            duration_ms=duration_ms,
        )

    log_event("worker.ready", protocol_version=PROTOCOL_VERSION, pid=os.getpid())

    try:
        for line in source:
            stripped = line.strip()
            if not stripped:
                continue
            raw, recovered_id = parse_line(stripped)
            try:
                frame = RequestFrame.from_mapping(raw)
            except ProtocolError as exc:
                emit(
                    error_frame(
                        recovered_id,
                        str(exc),
                        action=raw.get("action") if isinstance(raw, dict) else None,
                    )
                )
                log_event(
                    "worker.protocol_error",
                    id=recovered_id,
                    error=str(exc),
                )
                continue

            if frame.action == "ping":
                emit(
                    reply_frame(
                        frame.id,
                        action="ping",
                        payload={"ok": True, "ready": True},
                        exit_code=EXIT_OK,
                        duration_ms=0,
                        ready=True,
                    )
                )
                log_event("worker.ping", id=frame.id, protocol_version=PROTOCOL_VERSION)
                continue

            if frame.action == "shutdown":
                shutdown_id = frame.id
                shutdown_started = time.perf_counter()
                log_event("worker.shutdown", id=frame.id)
                break

            pool.submit(run_request, frame)
    except BrokenPipeError:
        log_event("worker.disconnect")
        return EXIT_INTERNAL
    finally:
        pool.shutdown(wait=True)

    if shutdown_id is not None:
        emit(
            reply_frame(
                shutdown_id,
                action="shutdown",
                payload={"ok": True, "ready": False},
                exit_code=EXIT_OK,
                duration_ms=monotonic_ms(shutdown_started),
                ready=False,
            )
        )
    log_event("worker.exit")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(serve())
