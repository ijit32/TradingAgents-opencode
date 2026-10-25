"""The versioned protocol and the persistent worker's framing.

These tests are the contract: frames are validated against the schema the
TypeScript types are generated from, and one-shot output is compared with the
server's output for the same action.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from tradingagents.opencode import cli
from tradingagents.opencode.contract import EXIT_BAD_INPUT, EXIT_VALIDATION
from tradingagents.opencode.protocol import (
    CONTROL_ACTIONS,
    PROTOCOL_VERSION,
    RequestFrame,
    error_frame,
    reply_frame,
)
from tradingagents.opencode.server import serve

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / ".opencode"
    / "plugins"
    / "tradingagents"
    / "protocol.schema.json"
)

ENVELOPE_KEYS = {"protocol_version", "id", "exit_code", "duration_ms"}


def _schema():
    jsonschema = pytest.importorskip("jsonschema")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), jsonschema


def _request(**overrides):
    frame = {"protocol_version": PROTOCOL_VERSION, "id": 1, "action": "status"}
    frame.update(overrides)
    return frame


def _serve(frames, *, workers=2):
    payload = "".join(
        frame + "\n" if isinstance(frame, str) else json.dumps(frame) + "\n" for frame in frames
    )
    out = StringIO()
    code = serve(StringIO(payload), out, workers=workers)
    replies = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return code, replies


def _write_request(root: Path, **overrides):
    request = {"ticker": "NVDA", "trade_date": "2026-10-23", **overrides}
    (root / "request.json").write_text(json.dumps(request), encoding="utf-8")


def _one_shot(capsys, *argv):
    code = cli.main(list(argv))
    lines = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(lines[-1]) if lines else {}


def _strip_envelope(reply):
    return {key: value for key, value in reply.items() if key not in ENVELOPE_KEYS}


# ---- schema ----------------------------------------------------------------


def test_request_and_reply_frames_match_the_schema():
    schema, jsonschema = _schema()
    jsonschema.validate(_request(run_dir="/tmp/run"), schema)
    jsonschema.validate(
        reply_frame(1, action="status", payload={"ok": True}, exit_code=0, duration_ms=3),
        schema,
    )


def test_request_frame_with_unknown_field_is_refused():
    schema, jsonschema = _schema()
    invalid = _request(run_dir="/tmp/run", extra=1)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


def test_python_accepts_every_action_in_the_schema():
    schema, _ = _schema()
    declared = set(schema["$defs"]["WorkerAction"]["enum"])
    assert set(cli.ACTIONS) == declared - set(CONTROL_ACTIONS)
    assert set(CONTROL_ACTIONS) <= declared


# ---- request validation ----------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"protocol_version": 2}, "protocol_version"),
        ({"id": 0}, "id must be"),
        ({"id": "1"}, "id must be"),
        ({"action": "nope"}, "unknown action"),
        ({"run_dir": None}, "run_dir is required"),
        ({"run_dir": "relative/run"}, "absolute"),
        ({"lenient": "yes"}, "boolean"),
        ({"json_args": []}, "JSON object"),
        ({"stage": ""}, "cannot be empty"),
        ({"extra": True}, "unknown request field"),
    ],
)
def test_invalid_frames_raise_protocol_error(overrides, message):
    from tradingagents.opencode.protocol import ProtocolError

    with pytest.raises(ProtocolError, match=message):
        RequestFrame.from_mapping({**_request(run_dir="/tmp/run"), **overrides})


def test_control_actions_need_no_run_dir():
    ping = RequestFrame.from_mapping(_request(action="ping", run_dir=None))
    assert ping.is_control
    assert ping.run_dir is None


def test_reply_frame_keeps_payload_and_envelope_separate():
    frame = reply_frame(
        7,
        action="plan",
        payload={"ok": True, "groups": [], "id": 999, "protocol_version": 99},
        exit_code=0,
        duration_ms=12,
    )
    assert frame["id"] == 7
    assert frame["protocol_version"] == PROTOCOL_VERSION
    assert frame["groups"] == []
    assert frame["duration_ms"] == 12


# ---- server ----------------------------------------------------------------


def test_ping_and_shutdown_are_answered_in_order():
    code, replies = _serve(
        [
            _request(id=1, action="ping"),
            _request(id=2, action="shutdown"),
        ]
    )
    assert code == 0
    assert [reply["id"] for reply in replies] == [1, 2]
    assert replies[0]["ready"] is True
    assert replies[1]["ready"] is False
    assert all(reply["protocol_version"] == PROTOCOL_VERSION for reply in replies)


def test_malformed_json_fails_one_frame_and_keeps_serving(tmp_path):
    _write_request(tmp_path)
    code, replies = _serve(
        [
            "{not json",
            _request(id=2, action="init", run_dir=str(tmp_path), json_args={}),
            _request(id=3, action="ping"),
            _request(id=4, action="shutdown"),
        ]
    )
    assert code == 0
    ids = [reply["id"] for reply in replies]
    assert None in ids
    assert set(ids) == {None, 2, 3, 4}
    assert len(ids) == 4
    first = next(reply for reply in replies if reply["id"] is None)
    assert first["ok"] is False
    assert first["exit_code"] == EXIT_BAD_INPUT


def test_version_mismatch_is_refused_without_touching_the_run(tmp_path):
    code, replies = _serve(
        [
            _request(id=1, action="status", run_dir=str(tmp_path), protocol_version=2),
            _request(id=2, action="ping"),
            _request(id=3, action="shutdown"),
        ]
    )
    assert code == 0
    assert replies[0]["id"] == 1
    assert "protocol_version" in replies[0]["error"]
    assert replies[1]["ready"] is True


def test_unknown_action_is_bad_input(tmp_path):
    code, replies = _serve([_request(id=1, action="nope", run_dir=str(tmp_path))])
    assert code == 0
    assert replies[0]["ok"] is False
    assert replies[0]["exit_code"] == EXIT_BAD_INPUT


def test_concurrent_requests_keep_their_ids(tmp_path):
    _write_request(tmp_path)
    frames = [_request(id=1, action="init", run_dir=str(tmp_path), json_args={})]
    frames += [_request(id=index, action="status", run_dir=str(tmp_path)) for index in range(2, 6)]
    frames.append(_request(id=9, action="shutdown"))
    code, replies = _serve(frames, workers=4)
    assert code == 0
    by_id = {reply["id"]: reply for reply in replies}
    assert set(by_id) == {1, 2, 3, 4, 5, 9}
    assert all(by_id[index]["ok"] for index in range(1, 6))


def test_validation_failure_carries_exit_code_and_errors(tmp_path):
    _write_request(tmp_path)
    _serve([_request(id=1, action="init", run_dir=str(tmp_path), json_args={})])
    (tmp_path / "stages" / "bull_researcher_1.out.txt").write_text("", encoding="utf-8")

    code, replies = _serve(
        [
            _request(
                id=1,
                action="validate",
                run_dir=str(tmp_path),
                stage="bull_researcher_1",
            ),
            _request(id=2, action="shutdown"),
        ]
    )
    assert code == 0
    assert replies[0]["ok"] is False
    assert replies[0]["exit_code"] == EXIT_VALIDATION
    assert replies[0]["errors"]


# ---- one-shot parity -------------------------------------------------------


@pytest.mark.parametrize("action", ["plan", "status"])
def test_server_reply_matches_one_shot(tmp_path, capsys, action):
    _write_request(tmp_path)
    assert cli.main(["init", "--run-dir", str(tmp_path)]) == 0
    capsys.readouterr()

    one_code, one_payload = _one_shot(capsys, action, "--run-dir", str(tmp_path))
    serve_code, replies = _serve(
        [
            _request(id=1, action=action, run_dir=str(tmp_path)),
            _request(id=2, action="shutdown"),
        ]
    )

    assert one_code == 0
    assert serve_code == 0
    assert _strip_envelope(replies[0]) == one_payload


def test_server_error_payload_matches_one_shot(tmp_path, capsys):
    _write_request(tmp_path)
    assert cli.main(["init", "--run-dir", str(tmp_path)]) == 0
    capsys.readouterr()

    one_code, one_payload = _one_shot(
        capsys, "prompt", "--run-dir", str(tmp_path), "--stage", "nope"
    )
    _, replies = _serve(
        [
            _request(id=1, action="prompt", run_dir=str(tmp_path), stage="nope"),
            _request(id=2, action="shutdown"),
        ]
    )

    assert one_code == EXIT_BAD_INPUT
    assert replies[0]["exit_code"] == EXIT_BAD_INPUT
    assert _strip_envelope(replies[0]) == one_payload


def test_structured_logs_go_to_stderr_not_stdout(tmp_path, capsys):
    _write_request(tmp_path)
    out = StringIO()
    serve(
        StringIO(
            "\n".join(
                json.dumps(frame)
                for frame in [
                    _request(id=1, action="init", run_dir=str(tmp_path), json_args={}),
                    _request(id=2, action="shutdown"),
                ]
            )
            + "\n"
        ),
        out,
    )
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    names = [event["event"] for event in events]
    assert "worker.ready" in names
    assert "worker.request" in names
    assert names[-1] == "worker.exit"
    request_log = next(event for event in events if event["event"] == "worker.request")
    assert request_log["action"] == "init"
    assert request_log["ok"] is True
    assert isinstance(request_log["duration_ms"], int)
    assert all(reply["protocol_version"] == PROTOCOL_VERSION for reply in map(json.loads, out.getvalue().splitlines()))


def test_error_frame_keeps_a_null_id_for_unparsable_requests():
    frame = error_frame(None, "bad frame")
    assert frame["id"] is None
    assert frame["ok"] is False


def test_serve_entry_point_works_over_real_pipes(tmp_path):
    """The module entry point, not just the in-process `serve` call."""
    import subprocess
    import sys

    _write_request(tmp_path)
    frames = [
        _request(id=1, action="ping"),
        _request(id=2, action="init", run_dir=str(tmp_path), json_args={}),
        _request(id=3, action="status", run_dir=str(tmp_path)),
        _request(id=4, action="shutdown"),
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "tradingagents.opencode_worker", "serve"],
        input="".join(json.dumps(frame) + "\n" for frame in frames),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )

    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 0
    assert {reply["id"] for reply in replies} == {1, 2, 3, 4}
    assert all(reply["ok"] for reply in replies)
    assert all(reply["protocol_version"] == PROTOCOL_VERSION for reply in replies)

    events = [json.loads(line) for line in proc.stderr.splitlines() if line.strip()]
    assert any(event["event"] == "worker.request" for event in events)
