"""devin adapter: --prompt-file launch and ATIF export collection."""

import hashlib
import json
import sys
from pathlib import Path
from typing import cast

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.devin import ADAPTER
from tools.agent_runner.models import Agent, Assignment, Config, Role, Workflow
from tools.agent_runner.runner import run_workflow


def _export(
    message: str | None = '{"ok": true}', tool_calls: list[dict[str, object]] | None = None
) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {"step_id": 1, "source": "system", "message": "system prompt"},
        {"step_id": 2, "source": "user", "message": "THE PROMPT"},
    ]
    if message is not None:
        steps.append(
            {
                "step_id": 3,
                "source": "agent",
                "message": message,
                "model_name": "swe-2-max",
                "tool_calls": tool_calls or [],
            }
        )
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-1",
        "agent": {"name": "devin", "version": "3000.11.3", "model_name": "SWE-2 Max"},
        "steps": steps,
    }


def _write(context: AdapterContext, name: str, text: str) -> Path:
    path = context.receipt_path.parent / name
    path.write_text(text, encoding="utf-8")
    return path


def _collect(
    context: AdapterContext,
    stdout: str,
    *,
    export: dict[str, object] | None | object = "unset",
    stderr: str = "",
):
    out = _write(context, "stdout.log", stdout)
    err = _write(context, "stderr.log", stderr)
    if export != "unset":
        if export is None:
            pass  # devin left no export file
        else:
            _write(context, "export.json", json.dumps(export) if not isinstance(export, str) else export)
    else:
        _write(context, "export.json", json.dumps(_export()))
    return ADAPTER.collect(context, out, err)


def test_prepare_writes_prompt_file_and_maps_flags(context: AdapterContext) -> None:
    spec = ADAPTER.prepare(context)
    prompt_path = spec.argv[spec.argv.index("--prompt-file") + 1]
    assert Path(prompt_path).read_text(encoding="utf-8") == context.request.prompt
    assert Path(prompt_path).parent == context.receipt_path.parent
    assert spec.argv[spec.argv.index("--export") + 1].endswith("export.json")
    assert spec.argv[spec.argv.index("--model") + 1] == "swe-2-max"
    assert spec.argv[spec.argv.index("--permission-mode") + 1] == "dangerous"
    assert spec.argv[spec.argv.index("--respect-workspace-trust") + 1] == "false"
    assert spec.stdin == b""
    assert all(context.request.prompt != arg for arg in spec.argv)


def test_prepare_appends_agent_args(context: AdapterContext) -> None:
    context.agent.args = ["--sandbox"]
    spec = ADAPTER.prepare(context)
    assert spec.argv[-1] == "--sandbox"


def test_collect_success_extracts_identity(context: AdapterContext) -> None:
    outcome = _collect(context, '{"ok": true}\n')
    receipt = outcome.receipt
    assert receipt is not None and receipt.status == "success"
    assert receipt.payload == {"ok": True}
    assert outcome.identity.model == "SWE-2 Max"
    assert outcome.identity.cli_version == "3000.11.3"
    assert outcome.identity.session_id == "session-1"
    assert outcome.identity.provider is None and outcome.identity.effort is None


def test_collect_success_text_shape(context: AdapterContext) -> None:
    context.role.result_shape = "text"
    outcome = _collect(context, "plain answer\n")
    assert outcome.receipt is not None and outcome.receipt.status == "success"
    assert outcome.receipt.payload == "plain answer\n"


def test_blank_stdout_fails_even_with_export(context: AdapterContext) -> None:
    outcome = _collect(context, "")
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_missing_export_fails(context: AdapterContext) -> None:
    outcome = _collect(context, '{"ok": true}', export=None)
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"
    assert outcome.identity.model is None


def test_malformed_export_fails(context: AdapterContext) -> None:
    outcome = _collect(context, '{"ok": true}', export="not json {")
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_export_without_agent_message_fails(context: AdapterContext) -> None:
    outcome = _collect(context, '{"ok": true}', export=_export(message=None))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_auth_stderr_reports_awaiting_authentication(context: AdapterContext) -> None:
    outcome = _collect(context, "", export=None, stderr="Error: not logged in")
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_authentication"


def test_workspace_trust_stderr_reports_awaiting_approval(context: AdapterContext) -> None:
    outcome = _collect(context, "", export=None, stderr="workspace trust is required; pass --respect-workspace-trust")
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_approval"


def test_non_object_stdout_fails_json_shape(context: AdapterContext) -> None:
    outcome = _collect(context, "plain text")
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_skill_tool_call_records_file_hash(context: AdapterContext, tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text("---\nname: review\n---\nbody\n", encoding="utf-8")
    context.agent.skill_sources = [str(tmp_path / "skills")]
    calls: list[dict[str, object]] = [
        {"type": "function", "function": {"name": "read", "arguments": json.dumps({"path": str(skill)})}}
    ]
    outcome = _collect(context, '{"ok": true}', export=_export(tool_calls=calls))
    expected = "sha256:" + hashlib.sha256(skill.read_bytes()).hexdigest()
    assert outcome.identity.skill_versions == {"review": expected}


def test_end_to_end_launch_and_collection(repository: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    fixture = tmp_path / "export.json"
    fixture.write_text(json.dumps(_export()))
    out = tmp_path / "out.txt"
    out.write_text('{"ok": true}\n')
    config = Config(
        agents={
            "a": Agent(
                runtime="devin",
                command=[
                    sys.executable,
                    str(Path(__file__).with_name("fake_cli.py")),
                    "--fake-capture",
                    str(capture),
                    "--fake-export",
                    str(fixture),
                    "--fake-stdout",
                    str(out),
                ],
                model="swe-2-max",
            )
        },
        roles={"inspect": Role(writes=False, deadline_seconds=60.0, result_shape="json")},
        workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["a"])})},
    )
    batch = run_workflow(config, "audit", "PROMPT", repository, output_dir=tmp_path / "run")
    assert batch.state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "success" and attempt.identity.model == "SWE-2 Max"
    assert attempt.identity.session_id == "session-1"
    recorded = json.loads(capture.read_text())
    assert "PROMPT" in recorded["prompt"]
    assert all("PROMPT" not in arg for arg in recorded["argv"])
    assert "--prompt-file" in recorded["argv"]
    assert attempt.payload == {"ok": True}


# The runner keeps <attempt>/export.json as a managed retained artifact and
# must redact inherited secret values from it before collection sees it.
def _devin_config(agent: Agent) -> Config:
    return Config(
        agents={"a": agent},
        roles={"inspect": Role(writes=False, deadline_seconds=60.0, result_shape="json")},
        workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["a"])})},
    )


def _runtime_agent(tmp_path: Path, *extra_args: str) -> Agent:
    return Agent(
        runtime="devin",
        command=[
            sys.executable,
            str(Path(__file__).with_name("fake_cli.py")),
            *extra_args,
        ],
        model="swe-2-max",
    )


def test_retained_export_with_secrets_is_redacted_before_collection(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-cli-token-98765"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    fixture = tmp_path / "export.json"
    export = _export(message="answer " + secret)
    extra: dict[str, object] = {
        "step_id": 4,
        "source": "agent",
        "message": "still " + secret,
        "tool_calls": [
            {
                "type": "function",
                "function": {"name": "run", "arguments": json.dumps({"shell": "token=" + secret})},
            }
        ],
    }
    cast(list[object], export["steps"]).append(extra)
    fixture.write_text(json.dumps(export), encoding="utf-8")
    out = tmp_path / "out.txt"
    out.write_text('{"ok": true}\n')
    config = _devin_config(
        _runtime_agent(
            tmp_path,
            "--fake-capture",
            str(tmp_path / "capture.json"),
            "--fake-export",
            str(fixture),
            "--fake-stdout",
            str(out),
        )
    )
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(config, "audit", "PROMPT", repository, output_dir=tmp_path / "run")
    assert batch.state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "success"
    assert attempt.payload == {"ok": True}
    # Observed identity survives redaction of the transcript itself.
    assert attempt.identity.model == "SWE-2 Max"
    assert attempt.identity.session_id == "session-1"
    assert attempt.request_path is not None
    retained = attempt.request_path.parent / "export.json"
    text = retained.read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text


def test_missing_retained_export_keeps_response_failed(repository: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    out.write_text('{"ok": true}\n')
    config = _devin_config(
        _runtime_agent(tmp_path, "--fake-capture", str(tmp_path / "capture.json"), "--fake-stdout", str(out))
    )
    batch = run_workflow(config, "audit", "PROMPT", repository, output_dir=tmp_path / "run")
    result = batch.results[0]
    assert result.state == "response_failed"
    attempt = result.attempts[0]
    assert attempt.stop_confirmed and attempt.sensitive_paths == []
    assert not (attempt.request_path or tmp_path).parent.joinpath("export.json").exists()
    assert attempt.identity.model is None


def test_secret_free_multiline_export_is_retained_unchanged(repository: Path, tmp_path: Path) -> None:
    export = json.dumps(
        {
            "schema_version": "ATIF-v1.7",
            "session_id": "session-1",
            "agent": {"name": "devin", "version": "3000.11.3", "model_name": "SWE-2 Max"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "日本語\n複数行の依頼"},
                {"step_id": 2, "source": "agent", "message": "回答\n日本語", "tool_calls": []},
            ],
        },
        ensure_ascii=False,
    )
    fixture = tmp_path / "export.json"
    fixture.write_text(export, encoding="utf-8")
    out = tmp_path / "out.txt"
    out.write_text('{"ok": true}\n')
    config = _devin_config(_runtime_agent(tmp_path, "--fake-export", str(fixture), "--fake-stdout", str(out)))
    batch = run_workflow(config, "audit", "PROMPT", repository, output_dir=tmp_path / "run")
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "success"
    assert attempt.payload == {"ok": True}
    assert attempt.identity.model == "SWE-2 Max"
    assert attempt.request_path is not None
    retained = attempt.request_path.parent / "export.json"
    assert retained.read_text(encoding="utf-8") == export
    assert "[REDACTED]" not in retained.read_text(encoding="utf-8")
