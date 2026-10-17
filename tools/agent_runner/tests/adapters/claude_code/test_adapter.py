"""claude-code adapter: launch spec shape and stream-json collection."""

import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.claude_code import ADAPTER
from tools.agent_runner.models import Agent, Assignment, Config, Role, Workflow
from tools.agent_runner.runner import run_workflow

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")


def _init(model: str = "claude-opus-5-5") -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "init",
        "claude_code_version": "2.1.291",
        "model": model,
        "session_id": "session-1",
        "tools": [],
        "skills": [],
        "slash_commands": [],
    }


def _assistant(text: str, model: str = "claude-opus-5-5") -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": text}]},
    }


def _result(
    text: str = '{"ok": true}',
    *,
    subtype: str = "success",
    is_error: bool = False,
    denials: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "permission_denials": denials or [],
        "session_id": "session-1",
        "num_turns": 1,
    }


def _stream(events: Iterable[dict[str, object]]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _write_stdout(context: AdapterContext, *events: dict[str, object]) -> Path:
    path = context.receipt_path.parent / "stdout.log"
    path.write_text(_stream(events), encoding="utf-8")
    return path


def _write_stderr(context: AdapterContext, text: str) -> Path:
    path = context.receipt_path.parent / "stderr.log"
    path.write_text(text, encoding="utf-8")
    return path


def _collect(context: AdapterContext, *events: dict[str, object], stderr: str = ""):
    return ADAPTER.collect(context, _write_stdout(context, *events), _write_stderr(context, stderr))


def test_prepare_builds_stream_json_launch(context: AdapterContext) -> None:
    spec = ADAPTER.prepare(context)
    command = context.agent.command
    assert spec.argv[: len(command)] == tuple(command)
    rest = spec.argv[len(command) :]
    assert rest[:6] == ("-p", "--output-format", "stream-json", "--verbose", "--model", "opus")
    assert "--dangerously-skip-permissions" in rest
    assert "--effort" not in rest
    assert spec.stdin == context.request.prompt.encode()
    assert all(context.request.prompt != arg for arg in spec.argv)


def test_prepare_maps_effort_and_appends_agent_args(context: AdapterContext) -> None:
    context.agent.effort = "high"
    context.agent.args = ["--bare", "--add-dir", "/extra"]
    spec = ADAPTER.prepare(context)
    assert spec.argv[spec.argv.index("--effort") + 1] == "high"
    assert spec.argv[-3:] == ("--bare", "--add-dir", "/extra")


def test_collect_success_extracts_identity_and_json_payload(context: AdapterContext) -> None:
    outcome = _collect(context, _init(), _assistant('{"ok": true}'), _result())
    receipt = outcome.receipt
    assert receipt is not None and receipt.status == "success"
    assert receipt.payload == {"ok": True}
    assert (receipt.run_id, receipt.request_id, receipt.agent_id, receipt.role) == (
        context.request.run_id,
        context.request.request_id,
        context.request.agent_id,
        context.request.role,
    )
    assert outcome.identity.model == "claude-opus-5-5"
    assert outcome.identity.session_id == "session-1"
    assert outcome.identity.cli_version == "2.1.291"
    assert outcome.identity.provider is None and outcome.identity.effort is None


def test_assistant_model_overrides_init_model(context: AdapterContext) -> None:
    outcome = _collect(context, _init("router-alias"), _assistant("{}", "claude-opus-5-5"), _result("{}"))
    assert outcome.identity.model == "claude-opus-5-5"


def test_collect_success_text_shape(context: AdapterContext) -> None:
    context.role.result_shape = "text"
    outcome = _collect(context, _init(), _result("plain answer"))
    assert outcome.receipt is not None and outcome.receipt.status == "success"
    assert outcome.receipt.payload == "plain answer"


def test_non_object_answer_fails_json_shape(context: AdapterContext) -> None:
    outcome = _collect(context, _init(), _result("not json"))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"
    assert outcome.receipt.payload is None


def test_json_fence_answer_parses(context: AdapterContext) -> None:
    outcome = _collect(context, _init(), _result('Here:\n```json\n{"x": 1}\n```\n'))
    assert outcome.receipt is not None and outcome.receipt.status == "success"
    assert outcome.receipt.payload == {"x": 1}


def test_permission_denials_report_awaiting_approval(context: AdapterContext) -> None:
    denials: list[dict[str, object]] = [{"tool": "Bash", "command": "rm -rf /"}]
    outcome = _collect(context, _init(), _result(denials=denials))
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_approval"


@pytest.mark.parametrize("is_error", [True, False])
def test_error_subtype_reports_response_failed(context: AdapterContext, is_error: bool) -> None:
    outcome = _collect(context, _init(), _result("boom", subtype="error_during_execution", is_error=is_error))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_auth_error_text_reports_awaiting_authentication(context: AdapterContext) -> None:
    outcome = _collect(
        context,
        _result("Error: not logged in. Run claude login.", subtype="error", is_error=True),
    )
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_authentication"


def test_missing_result_with_auth_stderr_reports_authentication(context: AdapterContext) -> None:
    outcome = _collect(context, _init(), stderr="401 Unauthorized: invalid api key")
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_authentication"


def test_missing_result_reports_response_failed(context: AdapterContext) -> None:
    outcome = _collect(context, _init(), _assistant("partial"))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_garbled_and_non_json_lines_are_skipped(context: AdapterContext) -> None:
    path = context.receipt_path.parent / "stdout.log"
    init = _init("m0")
    init["session_id"] = "s"
    init["claude_code_version"] = "9"
    lines = [
        "\x1b]777;notify;done\x07",
        json.dumps(init),
        "not json at all",
        json.dumps(_result('{"ok":1}')),
        "truncated {",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    outcome = ADAPTER.collect(context, path, _write_stderr(context, ""))
    assert outcome.receipt is not None and outcome.receipt.status == "success"
    assert outcome.identity.model == "m0" and outcome.identity.cli_version == "9"


def test_skill_tool_use_records_file_hash(context: AdapterContext) -> None:
    skill = context.work_dir / ".claude" / "skills" / "find-docs" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: find-docs\n---\nbody\n", encoding="utf-8")
    use: dict[str, object] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "m",
            "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "find-docs"}}],
        },
    }
    outcome = _collect(context, _init(), use, _result())
    expected = "sha256:" + hashlib.sha256(skill.read_bytes()).hexdigest()
    assert outcome.identity.skill_versions == {"find-docs": expected}


def test_init_skill_listing_is_not_load_evidence(context: AdapterContext) -> None:
    init = _init()
    init["skills"] = ["installed-but-not-read"]
    outcome = _collect(context, init, _result())
    assert outcome.identity.skill_versions == {}


def test_end_to_end_launch_and_collection(repository: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(_stream([_init(), _assistant('{"ok": true}'), _result()]))
    config = Config(
        agents={
            "a": Agent(
                runtime="claude-code",
                command=[
                    sys.executable,
                    str(Path(__file__).with_name("fake_cli.py")),
                    "--fake-capture",
                    str(capture),
                    "--fake-stdout",
                    str(fixture),
                ],
                model="opus",
            )
        },
        roles={"inspect": Role(writes=False, deadline_seconds=60.0, result_shape="json")},
        workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["a"])})},
    )
    batch = run_workflow(config, "audit", "PROMPT", repository, output_dir=tmp_path / "run")
    assert batch.state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "success" and attempt.identity.model == "claude-opus-5-5"
    assert attempt.identity.cli_version == "2.1.291"
    recorded = json.loads(capture.read_text())
    assert "PROMPT" in recorded["stdin"]
    assert all("PROMPT" not in arg for arg in recorded["argv"])
    assert recorded["argv"][-1] == "--dangerously-skip-permissions"
    assert attempt.payload == {"ok": True}
