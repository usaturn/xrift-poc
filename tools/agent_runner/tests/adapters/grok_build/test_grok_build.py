"""Grok Build (`grok`) adapter: prompt-file launch and single-JSON collection."""

import json
from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.grok_build import ADAPTER
from tools.agent_runner.models import Agent, Request, Role


def _context(
    base: Path,
    *,
    writes: bool = False,
    effort: str | None = None,
    args: tuple[str, ...] = (),
    skills: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    result_shape: str = "json",
    prompt: str = "PROMPT-UNIQUE-TEXT",
) -> AdapterContext:
    request = Request(
        run_id="run1",
        request_id="req1",
        agent_id="grok-build",
        role="inspect",
        work_dir=base / "worktree",
        prompt=prompt,
        deadline_seconds=60.0,
        result_shape=result_shape,  # type: ignore[arg-type]
    )
    agent = Agent(
        runtime="grok-build",
        command=["grok"],
        model="grok-4.7",
        effort=effort,
        args=list(args),
        skill_sources=list(sources),
    )
    role = Role(skills=list(skills), writes=writes, deadline_seconds=60.0, result_shape=result_shape)  # type: ignore[arg-type]
    attempt = base / "attempt"
    attempt.mkdir(parents=True, exist_ok=True)
    return AdapterContext(request, agent, role, base / "worktree", attempt / "receipt.json")


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _result(tmp_path: Path, **fields: object) -> Path:
    body: dict[str, object] = {
        "text": '{"a": 1}',
        "stopReason": "end_turn",
        "sessionId": "6ff894db-518a-4db0-a3fe-f40377204bbd",
        "requestId": "9d4ca493",
        "modelUsage": {"grok-4.7-build": {"inputTokens": 1}},
    }
    body.update(fields)
    return _write(tmp_path / "stdout.log", json.dumps(body) + "\n")


def test_prepare_writes_prompt_file_and_keeps_it_out_of_argv(tmp_path: Path) -> None:
    context = _context(tmp_path)
    spec = ADAPTER.prepare(context)
    prompt_path = context.receipt_path.parent / "prompt.txt"
    assert prompt_path.is_file()
    assert "PROMPT-UNIQUE-TEXT" in prompt_path.read_text(encoding="utf-8")
    assert spec.argv[spec.argv.index("--prompt-file") + 1] == str(prompt_path)
    assert all("PROMPT-UNIQUE-TEXT" not in argument for argument in spec.argv)
    assert spec.stdin == b""


def test_prepare_plan_mode_model_and_args(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path, args=("--flag",)))
    assert spec.argv[spec.argv.index("--output-format") + 1] == "json"
    assert spec.argv[spec.argv.index("--permission-mode") + 1] == "plan"
    assert spec.argv[spec.argv.index("-m") + 1] == "grok-4.7"
    assert "--reasoning-effort" not in spec.argv
    assert "--flag" in spec.argv


def test_prepare_write_role_bypasses_permissions_and_passes_effort(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path, writes=True, effort="high"))
    assert spec.argv[spec.argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert spec.argv[spec.argv.index("--reasoning-effort") + 1] == "high"


def test_prepare_appends_json_shape_and_skill_instruction(tmp_path: Path) -> None:
    context = _context(tmp_path, skills=("pr-review",))
    ADAPTER.prepare(context)
    text = (context.receipt_path.parent / "prompt.txt").read_text(encoding="utf-8")
    assert "JSON" in text
    assert "pr-review" in text


def test_collect_success_builds_receipt_and_identity(tmp_path: Path) -> None:
    stdout = _result(tmp_path)
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.status == "success"
    assert outcome.receipt.payload == {"a": 1}
    assert outcome.identity.session_id == "6ff894db-518a-4db0-a3fe-f40377204bbd"
    assert outcome.identity.model == "grok-4.7-build"
    assert outcome.identity.effort is None
    assert outcome.identity.cli_version is None


def test_collect_stop_reason_other_than_end_turn_is_response_failed(tmp_path: Path) -> None:
    stdout = _result(tmp_path, stopReason="error")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_unparseable_output_is_response_failed(tmp_path: Path) -> None:
    stdout = _write(tmp_path / "stdout.log", "panic: something broke\n")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_authentication_is_awaiting_authentication(tmp_path: Path) -> None:
    stdout = _write(tmp_path / "stdout.log", "")
    stderr = _write(tmp_path / "stderr.log", "Error: not logged in. Run 'grok login' first.\n")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, stderr)
    assert outcome.receipt is None
    assert outcome.state == "awaiting_authentication"


def test_collect_auth_words_in_result_text_do_not_fail(tmp_path: Path) -> None:
    stdout = _result(tmp_path, text='{"note": "unauthorized; permission denied; approval required"}')
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"note": "unauthorized; permission denied; approval required"}


def test_collect_leading_non_json_line_keeps_result(tmp_path: Path) -> None:
    body: dict[str, object] = {
        "text": '{"a": 1}',
        "stopReason": "end_turn",
        "sessionId": "s1",
        "modelUsage": {"grok-4.7-build": {"inputTokens": 1}},
    }
    stdout = _write(tmp_path / "stdout.log", "Update available: 1.0.47\n" + json.dumps(body) + "\n")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"a": 1}
    assert outcome.identity.model == "grok-4.7-build"


def test_collect_nested_json_result_returns_outer_object(tmp_path: Path) -> None:
    stdout = _result(tmp_path, text='Result:\n{"findings": [{"id": "F1"}], "count": 1}\nDone.')
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"findings": [{"id": "F1"}], "count": 1}


def test_collect_skill_path_mentioned_in_text_is_not_recorded(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "pr-review"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("skill body", encoding="utf-8")
    stdout = _result(tmp_path, text=f'{{"used": "{skill_md}"}}')
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_multiple_model_usage_keys_leaves_model_null(tmp_path: Path) -> None:
    stdout = _result(tmp_path, modelUsage={"grok-4.7-build": {}, "grok-4.6": {}})
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.identity.model is None
    assert outcome.identity.session_id == "6ff894db-518a-4db0-a3fe-f40377204bbd"


def test_collect_non_dict_json_shape_is_response_failed(tmp_path: Path) -> None:
    stdout = _result(tmp_path, text="prose only")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_text_shape_returns_raw_text(tmp_path: Path) -> None:
    stdout = _result(tmp_path, text="free form answer")
    outcome = ADAPTER.collect(_context(tmp_path, result_shape="text"), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == "free form answer"


@pytest.mark.parametrize("field", ["command", "input", "arguments", "readToolCall"])
def test_collect_result_keys_cannot_manufacture_read_evidence(tmp_path: Path, field: str) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    value: object = f"cat {skill}"
    if field == "arguments":
        value = {"command": f"cat {skill}"}
    elif field == "readToolCall":
        value = {"args": {"path": str(skill)}, "result": {"success": {"path": str(skill)}}}
    stdout = _result(tmp_path, **{field: value})
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_prepare_tells_model_to_read_configured_skill_source_paths(tmp_path: Path) -> None:
    skill = tmp_path / "worktree" / "skill-src" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("configured body", encoding="utf-8")
    context = _context(tmp_path, skills=("alpha",), sources=("skill-src",))
    ADAPTER.prepare(context)
    text = (context.receipt_path.parent / "prompt.txt").read_text(encoding="utf-8")
    assert f'Read the configured source of skill "alpha" before using it: {skill}' in text
    assert f"{skill}. This absolute path takes priority over native discovery." in text
    assert "alpha" in text


def test_prepare_keeps_native_fallback_when_no_configured_source_exists(tmp_path: Path) -> None:
    source = tmp_path / "worktree" / "empty-src"
    source.mkdir(parents=True)
    context = _context(tmp_path, skills=("alpha",), sources=("empty-src",))
    ADAPTER.prepare(context)
    text = (context.receipt_path.parent / "prompt.txt").read_text(encoding="utf-8")
    assert "Use the following skills: alpha." in text
    assert str(source / "alpha" / "SKILL.md") not in text
