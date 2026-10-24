"""The run directory: the whole interface between the plugin and the worker.

Everything a run produces lives under one directory so the plugin can resume,
inspect, or delete a run without touching a database:

    request.json  run.json  meta.json  checkpoint.json
    data/  stages/  memory/  reports/  final_state.json

Writes are atomic (``tmp`` + rename): a worker killed mid-action leaves the
previous file intact, which is what makes resume safe.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.opencode.contract import AnalysisRequest, BadInput

RUN_DIRS = ("data", "stages", "memory", "reports")
_STAMP_RE = re.compile(r"^\d{8}_\d{4}$")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M")


def _unique_run_dir(base: Path, run_id: str) -> Path:
    """``base/run_id``, with a numeric suffix if that run already exists.

    Two runs of the same ticker and date a minute apart must not overwrite each
    other; resume is explicit, never implicit.
    """
    candidate = base / run_id
    if not candidate.exists():
        return candidate
    for index in range(2, 100):
        candidate = base / f"{run_id}_{index}"
        if not candidate.exists():
            return candidate
    raise BadInput(f"too many runs named {run_id}; clean up {base}")


class RunDir:
    """One run's directory and the reads/writes the worker needs on it."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    def __fspath__(self) -> str:
        return str(self.root)

    def __str__(self) -> str:
        return str(self.root)

    @classmethod
    def create(cls, base: Path | str, request: AnalysisRequest) -> RunDir:
        safe_ticker_component(request.ticker)
        run_id = f"{request.ticker}_{request.trade_date}_{_stamp()}"
        run = cls(_unique_run_dir(Path(base).expanduser(), run_id))
        run.ensure()
        return run

    @classmethod
    def open(cls, root: Path | str) -> RunDir:
        run = cls(root)
        if not run.root.is_dir():
            raise BadInput(f"run directory does not exist: {run.root}")
        return run

    @property
    def run_id(self) -> str:
        return self.root.name

    def ensure(self) -> RunDir:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in RUN_DIRS:
            (self.root / name).mkdir(exist_ok=True)
        return self

    def path(self, *parts: str) -> Path:
        target = self.root.joinpath(*parts)
        # A stage or analyst name is caller input; never let it escape the run.
        if self.root not in target.resolve().parents and target.resolve() != self.root:
            raise BadInput(f"path escapes the run directory: {'/'.join(parts)}")
        return target

    # ---- JSON -----------------------------------------------------------

    def read_json(self, *parts: str, default: Any = None) -> Any:
        target = self.path(*parts)
        if not target.exists():
            return default
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BadInput(f"{target.name} is not valid JSON: {exc}") from exc

    def write_json(self, payload: Any, *parts: str) -> Path:
        return self.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", *parts)

    # ---- text -----------------------------------------------------------

    def read_text(self, *parts: str) -> str | None:
        target = self.path(*parts)
        return target.read_text(encoding="utf-8") if target.exists() else None

    def write_text(self, text: str, *parts: str) -> Path:
        target = self.path(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, target)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return target

    # ---- request --------------------------------------------------------

    def load_request(self) -> AnalysisRequest:
        raw = self.read_json("request.json")
        if raw is None:
            raise BadInput(f"request.json is missing in {self.root}")
        return AnalysisRequest.from_mapping(raw)

    def save_request(self, request: AnalysisRequest) -> Path:
        return self.write_json(request.to_dict(), "request.json")

    def save_meta(self, meta: dict[str, Any]) -> Path:
        return self.write_json(meta, "meta.json")

    def save_settings(self, settings: dict[str, Any]) -> Path:
        return self.write_json(settings, "run.json")

    def stage_path(self, stage: str, suffix: str) -> Path:
        return self.path("stages", f"{_stage_component(stage)}.{suffix}")

    def checkpoint(self) -> dict[str, Any]:
        """The plugin-owned checkpoint, or an empty one.

        The worker only reads it (``status``) so a resumed run can report what it
        skipped; the plugin writes it.
        """
        data = self.read_json("checkpoint.json", default=None) or {}
        return data if isinstance(data, dict) else {}


def _stage_component(stage: str) -> str:
    """A stage key that is safe as a filename (``bull_researcher[1]`` -> ``bull_researcher_1``)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage).strip("._")
    if not cleaned:
        raise BadInput(f"invalid stage name: {stage!r}")
    return cleaned
