"""pi adapter: launcher argv shape and NDJSON event collection."""

import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.pi import ADAPTER
from tools.agent_runner.models import Agent, Assignment, Config, Role, Workflow
from tools.agent_runner.runner import run_workflow

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")


def _session(session_id: str = "sess-1") -> dict[str, object]:
    return {"type": "session", "version": 3, "id": session_id, "timestamp": "t", "cwd": "/w"}


def _assistant(
    text: str = '{"ok": true}',
    *,
    model: str = "gpt-6.1-sol",
    provider: str = "openai-codex",
    thinking: str = "xhigh",
    stop_reason: str = "stop",
) -> dict[str, object]:
    return {
        "role": "assistant",
        "model": model,
        "provider": provider,
        "thinkingLevel": thinking,
        "stopReason": stop_reason,
        "content": [{"type": "thinking", "text": ""}, {"type": "text", "text": text}],
    }


def _agent_end(messages: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "type": "agent_end",
        "messages": messages
        or [
            {"role": "system", "content": [{"type": "text", "text": "sys"}]},
            {"role": "user", "content": [{"type": "text", "text": "THE PROMPT"}]},
            _assistant(),
        ],
    }


def _write_stdout(context: AdapterContext, lines: Iterable[str | dict[str, object]]) -> Path:
    path = context.receipt_path.parent / "stdout.log"
    path.write_text(
        "".join(line if isinstance(line, str) else json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


def _write_stderr(context: AdapterContext, text: str) -> Path:
    path = context.receipt_path.parent / "stderr.log"
    path.write_text(text, encoding="utf-8")
    return path


def _collect(context: AdapterContext, lines: Iterable[str | dict[str, object]], stderr: str = ""):
    return ADAPTER.collect(context, _write_stdout(context, lines), _write_stderr(context, stderr))


def test_prepare_builds_launcher_argv(context: AdapterContext) -> None:
    spec = ADAPTER.prepare(context)
    command = context.agent.command
    assert spec.argv[: len(command)] == tuple(command)
    rest = spec.argv[len(command) :]
    assert rest[:7] == ("--print", "--mode", "json", "--no-session", "--no-approve", "--provider", "openai-codex")
    assert spec.argv[spec.argv.index("--model") + 1] == "gpt-6.1-sol"
    assert spec.argv[spec.argv.index("--thinking") + 1] == "xhigh"
    assert spec.stdin == context.request.prompt.encode()
    assert all(context.request.prompt != arg for arg in spec.argv)


def test_prepare_maps_skill_sources_and_args(context: AdapterContext) -> None:
    context.agent.provider = None
    context.agent.effort = None
    context.agent.skill_sources = ["/skills/a", "/skills/b"]
    context.agent.args = ["--offline"]
    spec = ADAPTER.prepare(context)
    assert "--provider" not in spec.argv and "--thinking" not in spec.argv
    skills = [spec.argv[i + 1] for i, arg in enumerate(spec.argv) if arg == "--skill"]
    assert skills == ["/skills/a", "/skills/b"]
    assert spec.argv[-1] == "--offline"


def test_collect_success_extracts_identity_and_payload(context: AdapterContext) -> None:
    outcome = _collect(context, [_session(), {"type": "agent_start"}, _agent_end(), {"type": "agent_settled"}])
    receipt = outcome.receipt
    assert receipt is not None and receipt.status == "success"
    assert receipt.payload == {"ok": True}
    assert outcome.identity.model == "gpt-6.1-sol"
    assert outcome.identity.provider == "openai-codex"
    assert outcome.identity.effort == "xhigh"
    assert outcome.identity.session_id == "sess-1"
    assert outcome.identity.cli_version is None


def test_exit_zero_without_agent_end_is_response_failed(context: AdapterContext) -> None:
    outcome = _collect(context, [_session(), {"type": "agent_start"}])
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_missing_agent_settled_is_response_failed(context: AdapterContext) -> None:
    outcome = _collect(context, [_session(), _agent_end()])
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_aborted_stop_reason_is_interrupted(context: AdapterContext) -> None:
    outcome = _collect(
        context,
        [_session(), _agent_end([_assistant(stop_reason="aborted")]), {"type": "agent_settled"}],
    )
    assert outcome.receipt is not None and outcome.receipt.status == "interrupted"


def test_unknown_stop_reason_is_response_failed(context: AdapterContext) -> None:
    outcome = _collect(
        context,
        [_session(), _agent_end([_assistant(stop_reason="length")]), {"type": "agent_settled"}],
    )
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_auth_stderr_reports_awaiting_authentication(context: AdapterContext) -> None:
    outcome = _collect(context, [_session()], stderr="provider login required: not authenticated")
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_authentication"


def test_non_json_lines_and_osc_sequences_are_skipped(context: AdapterContext) -> None:
    outcome = _collect(
        context,
        [
            "\x1b]777;notify;pi;done\x07\n",
            _session(),
            "plain progress line\n",
            _agent_end(),
            "truncated {\n",
            {"type": "agent_settled"},
        ],
    )
    assert outcome.receipt is not None and outcome.receipt.status == "success"


def test_text_shape_returns_concatenated_text(context: AdapterContext) -> None:
    context.role.result_shape = "text"
    message = _assistant("first")
    message["content"] = [{"type": "text", "text": "first "}, {"type": "text", "text": "second"}]
    outcome = _collect(context, [_session(), _agent_end([message]), {"type": "agent_settled"}])
    assert outcome.receipt is not None and outcome.receipt.payload == "first second"


def test_non_object_answer_fails_json_shape(context: AdapterContext) -> None:
    outcome = _collect(context, [_session(), _agent_end([_assistant("not json")]), {"type": "agent_settled"}])
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_skill_source_and_observed_skill_md_are_hashed(context: AdapterContext, tmp_path: Path) -> None:
    loaded = tmp_path / "skills" / "loaded" / "SKILL.md"
    loaded.parent.mkdir(parents=True)
    loaded.write_text("loaded\n", encoding="utf-8")
    context.agent.skill_sources = [str(loaded.parent)]
    seen = context.work_dir / ".claude" / "skills" / "read-one" / "SKILL.md"
    seen.parent.mkdir(parents=True)
    seen.write_text("read\n", encoding="utf-8")
    tool_call: dict[str, object] = {
        "type": "message_update",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": "read", "arguments": {"path": str(seen)}}],
        },
    }
    outcome = _collect(context, [_session(), tool_call, _agent_end(), {"type": "agent_settled"}])
    expected = {
        "loaded": "sha256:" + hashlib.sha256(loaded.read_bytes()).hexdigest(),
        "read-one": "sha256:" + hashlib.sha256(seen.read_bytes()).hexdigest(),
    }
    assert outcome.identity.skill_versions == expected


def test_end_to_end_launch_and_collection(repository: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [_session(), {"type": "agent_start"}, _agent_end(), {"type": "agent_settled"}]
        )
    )
    config = Config(
        agents={
            "a": Agent(
                runtime="pi",
                command=[
                    sys.executable,
                    str(Path(__file__).with_name("fake_launcher.py")),
                    "--fake-capture",
                    str(capture),
                    "--fake-stdout",
                    str(fixture),
                ],
                model="gpt-6.1-sol",
                provider="openai-codex",
                effort="xhigh",
            )
        },
        roles={"inspect": Role(writes=False, deadline_seconds=60.0, result_shape="json")},
        workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["a"])})},
    )
    batch = run_workflow(config, "audit", "PROMPT", repository, output_dir=tmp_path / "run")
    assert batch.state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "success"
    assert attempt.identity.model == "gpt-6.1-sol"
    assert attempt.identity.provider == "openai-codex"
    assert attempt.identity.effort == "xhigh"
    recorded = json.loads(capture.read_text())
    assert "PROMPT" in recorded["stdin"]
    assert all("PROMPT" not in arg for arg in recorded["argv"])
    assert "--print" in recorded["argv"] and "--mode" in recorded["argv"]
    assert recorded["argv"][recorded["argv"].index("--provider") + 1] == "openai-codex"
    assert attempt.payload == {"ok": True}
