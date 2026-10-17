"""DSH launch isolation and strict headless event handling; no live API calls."""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import environ
from tools.agent_runner.adapters import (
    AdapterContext,
    AdapterOutcome,
    LaunchSpec,
    load_adapter,
    runtime_supported,
)
from tools.agent_runner.models import (
    Agent,
    Payload,
    ReceiptStatus,
    Request,
    ResultShape,
    Role,
)
from tools.test_support.dsh import write_session


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterContext:
    monkeypatch.setattr(
        environ,
        "_literal",
        {
            "COMMANDCODE_API_KEY": "credential-not-for-files",
            "PI_PROVIDER": "parent-provider",
            "PI_MODEL": "parent-model",
            "DSH_HOME": str(tmp_path / "shared-dsh"),
        },
    )
    work = tmp_path / "worktree"
    work.mkdir()
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    return AdapterContext(
        Request(
            run_id="run",
            request_id="request",
            agent_id="dsh-a",
            role="inspect",
            work_dir=work,
            prompt="一行目\n二行目 ☃\n",
            deadline_seconds=60.0,
            result_shape="json",
        ),
        Agent.model_validate(
            {
                "runtime": "dsh",
                "command": ["dsh"],
                "provider": "commandcode",
                "model": "a-model",
                "effort": "high",
                "env": ["COMMANDCODE_API_KEY"],
            }
        ),
        Role(writes=False, deadline_seconds=60.0, result_shape="json"),
        work,
        attempt / "receipt.json",
    )


def test_adapter_cache_paths_are_writable_with_its_scoped_runner(context: AdapterContext) -> None:
    import subprocess
    import sys

    spec = load_adapter("dsh").prepare(context)
    command = _patch(spec)["sandbox"]["config"]["runnerCommand"]
    work = str(context.work_dir.resolve())
    profile = [
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--die-with-parent",
        "--tmpfs",
        "/tmp",
        "--bind",
        work,
        work,
        "--",
    ]
    cache, venv = Path(spec.env["UV_CACHE_DIR"]), Path(spec.env["UV_PROJECT_ENVIRONMENT"])
    assert cache.is_relative_to(spec.env["HOME"]) and venv.is_relative_to(spec.env["HOME"])
    state = Path(spec.env["DSH_HOME"]) / "visible"
    state.write_text("observed")
    program = "from pathlib import Path; " + "; ".join(f"Path({str(path)!r}).mkdir()" for path in (cache, venv))
    program += f"; assert Path({str(state)!r}).read_text() == 'observed'"
    result = subprocess.run([*command, *profile, sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert cache.is_dir() and venv.is_dir()


def test_runtime_is_registered() -> None:
    assert runtime_supported("dsh")


def _patch(spec: LaunchSpec) -> dict[str, Any]:
    return {row["id"]: row for row in json.loads(Path(spec.argv[spec.argv.index("--patch") + 1]).read_text())}


@pytest.mark.parametrize(
    "provider,key",
    [("commandcode", "COMMANDCODE_API_KEY"), ("opencode-go", "OPENCODE_API_KEY")],
)
def test_prepare_isolates_state_and_passes_config_not_credentials(
    context: AdapterContext, monkeypatch: pytest.MonkeyPatch, provider: str, key: str
) -> None:
    monkeypatch.setattr(environ, "_literal", {key: "credential-not-for-files"})
    context.agent.provider = provider
    context.agent.env = [key]
    spec = load_adapter("dsh").prepare(context)
    assert spec.argv[:3] == ("dsh", "--profile", "headless")
    assert spec.argv[-2:] == ("--json", "-")
    assert spec.stdin == context.request.prompt.encode()
    assert all("credential-not-for-files" not in arg for arg in spec.argv)
    root = context.receipt_path.parent / "dsh"
    assert Path(spec.env["DSH_HOME"]).is_relative_to(root)
    assert Path(spec.env["HOME"]).is_relative_to(root)
    # Spills of large command output must land in the managed, retained tree,
    # never in the disposable HOME or an ambient temporary directory.
    tmpdir = Path(spec.env["TMPDIR"])
    assert tmpdir.is_relative_to(root) and not tmpdir.is_relative_to(spec.env["HOME"])
    assert tmpdir.is_dir() and not any(tmpdir.iterdir())
    assert tmpdir.stat().st_mode & 0o077 == 0
    assert spec.env["DSH_TELEMETRY_MODE"] == "DISABLED"
    patch = _patch(spec)
    assert "credential-not-for-files" not in json.dumps(patch)
    selection = patch["agent-default-model"]["config"]
    assert selection == {
        "provider": provider,
        "model": context.agent.model,
        "reasoningEffort": context.agent.effort,
    }
    route = patch["llm-pi-ai"]["config"]["providers"][provider]
    assert spec.env[route["apiKeyEnv"]] == "credential-not-for-files"
    assert route["apiKeyEnv"] != key  # no project .env / shared credential fallback
    assert route["models"][0]["id"] == context.agent.model
    assert route["retryPolicy"] == {"mode": "normal", "maxRetries": 0}
    if provider == "commandcode":
        assert route["baseURL"] == "https://api.commandcode.ai/provider/v1"
        assert route["compat"]["thinkingFormat"] == "deepseek"
    else:
        assert "baseURL" not in route
    assert patch["approval"]["config"]["policy"] == "never"
    assert patch["permission"]["config"]["presets"]["runner"]["approval"] == "never"
    assert patch["session-persistence-jsonl"]["config"]["compression"] == "none"
    assert patch["session-title-llm"]["disabled"] is True
    assert patch["plugin-package-inventory-deepseek"]["config"]["enabled"] is False
    with pytest.raises(FileExistsError):
        load_adapter("dsh").prepare(context)  # existing launch state is not reused


def test_unallowlisted_key_is_not_inherited(context: AdapterContext) -> None:
    context.agent.env = []
    spec = load_adapter("dsh").prepare(context)
    route = _patch(spec)["llm-pi-ai"]["config"]["providers"]["commandcode"]
    assert spec.env[route["apiKeyEnv"]] == ""


def test_skill_source_is_used_in_stdin(context: AdapterContext) -> None:
    skill = context.work_dir / "custom" / "check" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("read-only rules")
    context.agent.skill_sources = ["custom"]
    context.role.skills = ["check"]
    spec = load_adapter("dsh").prepare(context)
    assert str(skill) in spec.stdin.decode()
    assert context.request.prompt in spec.stdin.decode()


@pytest.mark.parametrize(
    "field,value",
    [("provider", None), ("provider", "other"), ("args", ["--profile", "web"])],
)
def test_unsupported_launch_options_are_rejected(context: AdapterContext, field: str, value: object) -> None:
    setattr(context.agent, field, value)
    with pytest.raises(ValueError):
        load_adapter("dsh").prepare(context)
    assert not (context.receipt_path.parent / "dsh").exists()


def _stream(text: str = '{"ok":true}', reason: dict[str, object] | None = None) -> list[dict[str, Any]]:
    return [
        {"type": "session", "sessionId": "observed-session", "cwd": "/work"},
        {"type": "status", "phase": "turn_start", "turn": 1},
        {
            "type": "status",
            "phase": "turn_end",
            "turn": 1,
            "reason": reason or {"kind": "completed"},
        },
        {"type": "final", "text": text},
    ]


def _collect(context: AdapterContext, events: Iterable[object], stderr: str = "") -> AdapterOutcome:
    stdout_path = context.receipt_path.parent / "stdout.log"
    stdout_path.write_text("".join(event if isinstance(event, str) else json.dumps(event) + "\n" for event in events))
    stderr_path = context.receipt_path.parent / "stderr.log"
    stderr_path.write_text(stderr)
    return load_adapter("dsh").collect(context, stdout_path, stderr_path)


def test_collector_binds_stdout_session_to_observed_model(context: AdapterContext) -> None:
    write_session(
        context.receipt_path.parent / "dsh" / "state",
        context.work_dir,
        session_id="observed-session",
        provider="live-provider",
        model="live-model",
    )
    outcome = _collect(context, _stream())
    assert outcome.receipt is not None and outcome.receipt.status == "success"
    assert (outcome.identity.provider, outcome.identity.model, outcome.identity.effort) == (
        "live-provider",
        "live-model",
        "high",
    )


def _status(context: AdapterContext, events: Iterable[object]) -> ReceiptStatus:
    outcome = _collect(context, events)
    assert outcome.receipt is not None
    return outcome.receipt.status


@pytest.mark.parametrize(
    "shape,text,expected",
    [
        ("json", '{"ok":true}', {"ok": True}),
        ("json", '```json\n{"ok":true}\n```', {"ok": True}),
        (
            "json",
            'レビュー結果です。\n```json\n{"status":"success","active_findings":0}\n```',
            {"status": "success", "active_findings": 0},
        ),
        ("json", '```json\n{"ok":true}\n```\n以上です。\n', {"ok": True}),
        (
            "json",
            '前置き ☃\n複数行\n```json\n{\n  "note": "日本語\\n☃"\n}\n```\n後書き\n',
            {"note": "日本語\n☃"},
        ),
        ("json", '結果:\n```json\n{"ok":true}```', {"ok": True}),
        ("json", '```json\n{"first":1}\n```\n```json\n{"second":2}\n```', {"first": 1}),
        (
            "text",
            "複数行\n☃ authentication is a topic\n",
            "複数行\n☃ authentication is a topic\n",
        ),
        ("text", '説明\n```json\n{"ok":true}\n```\n', '説明\n```json\n{"ok":true}\n```\n'),
    ],
)
def test_success_payload_and_observed_identity_only(
    context: AdapterContext, shape: ResultShape, text: str, expected: Payload
) -> None:
    context.role.result_shape = shape
    outcome = _collect(context, _stream(text))
    assert outcome.receipt is not None
    assert outcome.receipt.status == "success"
    assert outcome.receipt.payload == expected
    assert outcome.identity.session_id == "observed-session"
    assert outcome.identity.model is outcome.identity.provider is outcome.identity.effort is None
    assert outcome.identity.cli_version is None
    assert outcome.identity.skill_versions == {}


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"type": "final", "text": '{"ok":true}'}],
        _stream()[:-1],
        _stream()[1:],
        [_stream()[0], *_stream()[2:]],
        _stream() + [_stream()[-1]],
        [_stream()[0], _stream()[-1], *_stream()[1:3]],
        _stream() + ["broken {\n"],
        [_stream()[0], "broken {\n", *_stream()[1:]],
        _stream(""),
        _stream("{}"),
        _stream("[]"),
        _stream('{"n":NaN}'),
        _stream('{"a":1,"a":2}'),
        _stream('結果: {"ok":true}'),
        _stream("説明\n```json\n{}\n```"),
        _stream("説明\n```json\n[]\n```"),
        _stream('説明\n```json\n{"a":1,"a":2}\n```'),
        _stream('説明\n```json\n{"n":NaN}\n```'),
        _stream("説明\n```json\n{broken\n```"),
        _stream(reason={"kind": "unknown"}),
        [_stream()[0], _stream()[1], {**_stream()[2], "turn": 2}, _stream()[3]],
        [{**_stream()[0], "truncated": True}, *_stream()[1:]],
    ],
)
def test_missing_malformed_or_inconsistent_stream_is_not_success(
    context: AdapterContext, events: list[object]
) -> None:
    assert _status(context, events) == "response_failed"


@pytest.mark.parametrize(
    "code,state",
    [
        ("MISSING_CREDENTIAL", "awaiting_authentication"),
        ("INVALID_CREDENTIAL", "awaiting_authentication"),
        ("UNAUTHORIZED", "awaiting_authentication"),
        ("APPROVAL_REQUIRED", "awaiting_approval"),
        ("NETWORK_ERROR", "response_failed"),
    ],
)
def test_error_reason_overrides_even_nonempty_final(context: AdapterContext, code: str, state: str) -> None:
    outcome = _collect(
        context,
        _stream(reason={"kind": "error", "error": {"code": code, "message": "diagnostic"}}),
    )
    assert outcome.receipt is not None
    assert outcome.receipt.status == state
    assert outcome.receipt.payload is None


def test_empty_final_with_missing_credential_is_authentication(context: AdapterContext) -> None:
    assert (
        _status(
            context,
            _stream("", {"kind": "error", "error": {"code": "MISSING_CREDENTIAL"}}),
        )
        == "awaiting_authentication"
    )


def test_abort_maps_to_interrupted(context: AdapterContext) -> None:
    assert _status(context, _stream("", {"kind": "aborted"})) == "interrupted"


@pytest.mark.parametrize(
    "diagnostic",
    [
        'the user rejected tool "bash"',
        (
            'the user rejected escalating this command to "danger-full-access"; '
            "it stays denied, so stop and explain instead of working around it"
        ),
    ],
)
def test_approval_error_in_tool_result_is_not_success(context: AdapterContext, diagnostic: str) -> None:
    events = _stream()
    events.insert(
        2,
        {
            "type": "tool_result",
            "callId": "call",
            "status": "error",
            "result": diagnostic,
        },
    )
    assert _status(context, events) == "awaiting_approval"


@pytest.mark.parametrize(
    "diagnostic",
    [
        'Error: cannot read "src/pages/Unauthorized.tsx": not found',
        'Error: offset 410 is out of range for "tools/x.py" (403 lines)',
        'Error: cannot read "docs/errors/401.md": not found',
        "HTTP 403 Forbidden",
        "invalid api key",
        'Error: cannot read "資料/unauthorized メモ.md": not found\n詳細',
    ],
)
def test_recovered_tool_error_does_not_mark_completed_turn_as_authentication(
    context: AdapterContext, diagnostic: str
) -> None:
    # DSH's tool errors carry only free text; a completed turn with a valid final is the answer.
    events = _stream()
    events.insert(
        2,
        {
            "type": "tool_result",
            "callId": "call",
            "status": "error",
            "result": diagnostic,
        },
    )
    outcome = _collect(context, events)
    assert outcome.receipt is not None
    assert outcome.receipt.status == "success"
    assert outcome.receipt.payload == {"ok": True}


def test_startup_error_without_final_is_classified(context: AdapterContext) -> None:
    outcome = _collect(context, [{"type": "error", "message": "MISSING_CREDENTIAL"}])
    assert outcome.receipt is not None
    assert outcome.receipt.status == "awaiting_authentication"


def test_error_events_cannot_be_hidden_by_completed_final(context: AdapterContext) -> None:
    events = _stream()
    events.insert(2, {"type": "error", "message": "failed"})
    assert _status(context, events) == "response_failed"


def test_unicode_line_separator_inside_json_string_is_preserved(context: AdapterContext) -> None:
    context.role.result_shape = "text"
    text = "日本語\u2028次行 ☃"
    events = [json.dumps(event, ensure_ascii=False) + "\n" for event in _stream(text)]
    outcome = _collect(context, events)
    assert outcome.receipt is not None
    assert outcome.receipt.status == "success"
    assert outcome.receipt.payload == text


@pytest.mark.parametrize("event", [{"type": []}, {"type": "status", "phase": []}, []])
def test_invalid_event_type_does_not_escape_collector(context: AdapterContext, event: object) -> None:
    events: list[object] = list(_stream())
    events.insert(2, event)
    assert _status(context, events) == "response_failed"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "text"},
        {"type": "thinking", "text": 3},
        {"type": "status", "phase": "unknown", "turn": 1},
        {"type": "status", "phase": "step_start", "turn": 2, "step": 1},
        {"type": "status", "phase": "step_end", "turn": 1, "step": True},
        {"type": "tool_call", "callId": "call", "tool": "read"},
        {"type": "tool_result", "callId": "call", "status": "unknown", "result": "x"},
        {"type": "tool_result", "callId": "call", "status": "completed"},
    ],
)
def test_invalid_intermediate_event_is_not_ignored(context: AdapterContext, event: dict[str, object]) -> None:
    events = _stream()
    events.insert(2, event)
    assert _status(context, events) == "response_failed"


def test_documented_truncated_tool_input_and_reasoning_are_accepted(context: AdapterContext) -> None:
    events = _stream()
    events[2:2] = [
        {"type": "thinking", "text": "bounded reasoning", "truncated": True},
        {"type": "tool_call", "callId": "call", "tool": "read", "truncated": True},
        {
            "type": "tool_result",
            "callId": "call",
            "status": "completed",
            "result": "bounded result",
            "truncated": True,
        },
    ]
    assert _status(context, events) == "success"
