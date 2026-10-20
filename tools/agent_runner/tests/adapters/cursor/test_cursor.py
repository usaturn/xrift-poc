"""Cursor CLI (`agent -p`) adapter: stream-json events and failure classification."""

import hashlib
import json
from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome
from tools.agent_runner.adapters.cursor import ADAPTER
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
        agent_id="cursor",
        role="inspect",
        work_dir=base / "worktree",
        prompt=prompt,
        deadline_seconds=60.0,
        result_shape=result_shape,  # type: ignore[arg-type]
    )
    agent = Agent(
        runtime="cursor",
        command=["agent"],
        model="auto",
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


def _stdout_events(tmp_path: Path, *events: dict[str, object]) -> Path:
    return _write(tmp_path / "stdout.log", "".join(json.dumps(e) + "\n" for e in events))


def test_prepare_reads_prompt_from_stdin_and_trusts_worktree(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path))
    assert spec.argv[:2] == ("agent", "-p")
    assert spec.argv[spec.argv.index("--output-format") + 1] == "stream-json"
    assert "--trust" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "auto"
    assert all("PROMPT-UNIQUE-TEXT" not in argument for argument in spec.argv)
    assert "PROMPT-UNIQUE-TEXT" in spec.stdin.decode()


# #75: with --mode ask, Cursor CLI rejected shell commands that did not ask for its sandbox and ran the others inside
# it, so PR review roles could not run the pr-review helpers: the sandbox denies their writes outside the worktree and
# /tmp (their save directory, uv's cache) and the network. -f skips a sandbox the model asks for, but that is not
# documented, so the launch states --sandbox disabled (runtimes/cursor.rst). When changing these flags, re-check them
# on a real CLI and update runtimes/cursor.rst together with this test
def test_prepare_read_only_role_allows_shell_without_cursor_sandbox(tmp_path: Path) -> None:
    argv = ADAPTER.prepare(_context(tmp_path, writes=False)).argv
    assert "-f" in argv
    assert argv[argv.index("--sandbox") + 1] == "disabled"
    assert "--mode" not in argv and "--plan" not in argv


# #75 keeps the write roles' launch as it was (-f only; Cursor's sandbox follows the user's setting), as the issue asks
def test_prepare_write_role_forces_and_effort_uses_bracket(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path, writes=True, effort="high", args=("--flag",)))
    assert "-f" in spec.argv
    assert "--mode" not in spec.argv and "--sandbox" not in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "auto[effort=high]"
    assert "--flag" in spec.argv


def test_prepare_appends_json_shape_and_skill_instruction(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path, skills=("pr-review",)))
    text = spec.stdin.decode()
    assert "JSON" in text
    assert "pr-review" in text


def test_collect_success_builds_receipt_and_identity(tmp_path: Path) -> None:
    session_id = "a6b41ff6-7ad3-4d07-a21d-1a4fa2ca3b47"
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "login",
            "session_id": session_id,
            "model": "Grok 4.7 256K Extra High",
            "permissionMode": "default",
        },
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"findings": []}',
            "session_id": session_id,
            "duration_ms": 10,
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.status == "success"
    assert outcome.receipt.payload == {"findings": []}
    assert outcome.identity.session_id == session_id
    assert outcome.identity.model == "Grok 4.7 256K Extra High"
    assert outcome.identity.provider is None
    assert outcome.identity.cli_version is None


def test_collect_result_is_error_is_response_failed(tmp_path: Path) -> None:
    stdout = _stdout_events(
        tmp_path,
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "failed hard"},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_trust_required_is_awaiting_approval(tmp_path: Path) -> None:
    stdout = _write(tmp_path / "stdout.log", "\nWorkspace Trust Required\n\n  Run 'agent' interactively to decide\n")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "awaiting_approval"


def test_collect_authentication_is_awaiting_authentication(tmp_path: Path) -> None:
    stdout = _write(tmp_path / "stdout.log", "")
    stderr = _write(tmp_path / "stderr.log", "Error: not logged in. Run 'agent login' to authenticate.\n")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, stderr)
    assert outcome.receipt is None
    assert outcome.state == "awaiting_authentication"


def test_collect_auth_words_in_prompt_echo_and_result_do_not_fail(tmp_path: Path) -> None:
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Check the login flow; report if approval required"}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"note": "token expired causes 401 Unauthorized"}',
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"note": "token expired causes 401 Unauthorized"}


def test_collect_nested_json_result_returns_outer_object(tmp_path: Path) -> None:
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": 'Result:\n{"findings": [{"id": "F1"}], "count": 1}\nDone.',
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"findings": [{"id": "F1"}], "count": 1}


def test_collect_missing_result_event_is_response_failed(tmp_path: Path) -> None:
    stdout = _stdout_events(
        tmp_path,
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "m"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"
    assert outcome.identity.session_id == "s1"
    assert outcome.identity.model == "m"


def test_collect_non_dict_json_shape_is_response_failed(tmp_path: Path) -> None:
    stdout = _stdout_events(tmp_path, {"type": "result", "subtype": "success", "is_error": False, "result": "nope"})
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_text_shape_returns_raw_text(tmp_path: Path) -> None:
    stdout = _stdout_events(
        tmp_path, {"type": "result", "subtype": "success", "is_error": False, "result": "free form"}
    )
    outcome = ADAPTER.collect(_context(tmp_path, result_shape="text"), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == "free form"


def test_collect_skill_versions_from_successful_read_completion(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "pr-review"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("skill body", encoding="utf-8")
    stdout = _stdout_events(
        tmp_path,
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "readToolCall": {
                    "args": {"path": str(skill_md)},
                    "result": {
                        "success": {
                            "content": "skill body",
                            "isEmpty": False,
                            "exceededLimit": False,
                            "totalLines": 1,
                            "fileSize": 10,
                            "path": str(skill_md),
                            "readRange": {"startLine": 1, "endLine": 1},
                            "relatedCursorRulePaths": [],
                            "relatedCursorRules": [],
                        }
                    },
                }
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"a": 1}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    expected = hashlib.sha256(b"skill body").hexdigest()
    assert outcome.identity.skill_versions == {"pr-review": expected}


def test_collect_skill_path_mentioned_in_text_is_not_recorded(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "pr-review"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("skill body", encoding="utf-8")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": f"read {skill_md}"}]},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"using {skill_md}"}]},
        },
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"a": 1}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


@pytest.mark.parametrize(
    ("subtype", "result"),
    [
        ("started", None),
        ("completed", {"error": {"message": "read failed"}}),
        ("completed", None),
        ("completed", {"success": None}),
    ],
    ids=["started-only", "failed", "no-result", "unknown-success"],
)
def test_collect_incomplete_or_failed_reads_are_not_recorded(
    tmp_path: Path, subtype: str, result: dict[str, object] | None
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    read: dict[str, object] = {"args": {"path": str(skill)}}
    if result is not None:
        read["result"] = result
    stdout = _stdout_events(
        tmp_path,
        {"type": "tool_call", "subtype": subtype, "tool_call": {"readToolCall": read}},
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"ok": true}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_shell_path_mention_is_not_recorded(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "shellToolCall": {"args": {"command": f"echo {skill}"}, "result": {"success": {"output": str(skill)}}},
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"ok": true}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_structured_prompt_echo_is_not_read_evidence(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"command": f"cat {skill}", "arguments": {"input": f"cat {skill}", "path": str(skill)}},
                ],
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"ok": true}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_reading_skill_backup_does_not_record_unread_skill(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"unread current skill")
    backup = skill.with_suffix(".md.bak")
    backup.write_bytes(b"read backup")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "readToolCall": {
                    "args": {"path": str(backup)},
                    "result": {
                        "success": {
                            "content": "read backup",
                            "isEmpty": False,
                            "exceededLimit": False,
                            "totalLines": 1,
                            "fileSize": 11,
                            "path": str(backup),
                            "readRange": {"startLine": 1, "endLine": 1},
                            "relatedCursorRulePaths": [],
                            "relatedCursorRules": [],
                        }
                    },
                }
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"ok": true}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def _read_success(path: Path, **overrides: object) -> dict[str, object]:
    success: dict[str, object] = {
        "content": "actual skill",
        "isEmpty": False,
        "exceededLimit": False,
        "totalLines": 1,
        "fileSize": 12,
        "path": str(path),
        "readRange": {"startLine": 1, "endLine": 1},
        "relatedCursorRulePaths": [],
        "relatedCursorRules": [],
    }
    drop = overrides.pop("drop", ())
    success.update(overrides)
    for key in drop:  # type: ignore[union-attr]
        success.pop(key, None)  # type: ignore[union-attr]
    return {
        "type": "tool_call",
        "subtype": "completed",
        "tool_call": {"readToolCall": {"args": {"path": str(path)}, "result": {"success": success}}},
    }


def _collect_with_read(tmp_path: Path, read: dict[str, object]) -> AdapterOutcome:
    stdout = _stdout_events(
        tmp_path,
        {"type": "system", "subtype": "init", "session_id": "s1"},
        read,
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"ok": true}'},
    )
    return ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))


def test_collect_skill_version_stays_unknown_when_read_range_is_partial(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"line1\nline2\nline3\n")
    outcome = _collect_with_read(
        tmp_path,
        _read_success(skill, readRange={"startLine": 1, "endLine": 2}, totalLines=3),
    )
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_read_limit_is_exceeded(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    outcome = _collect_with_read(tmp_path, _read_success(skill, exceededLimit=True))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_content_is_missing(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    outcome = _collect_with_read(tmp_path, _read_success(skill, drop=("content",)))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_range_evidence_is_missing(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    outcome = _collect_with_read(tmp_path, _read_success(skill, drop=("readRange",)))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_prepare_tells_model_to_read_configured_skill_source_paths(tmp_path: Path) -> None:
    skill = tmp_path / "worktree" / "skill-src" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("configured body", encoding="utf-8")
    spec = ADAPTER.prepare(_context(tmp_path, skills=("alpha",), sources=("skill-src",)))
    text = spec.stdin.decode()
    assert f'Read the configured source of skill "alpha" before using it: {skill}' in text
    assert f"{skill}. This absolute path takes priority over native discovery." in text
    assert "alpha" in text
    assert all(str(skill) not in argument for argument in spec.argv)


def test_prepare_keeps_native_fallback_when_no_configured_source_exists(tmp_path: Path) -> None:
    source = tmp_path / "worktree" / "empty-src"
    source.mkdir(parents=True)
    spec = ADAPTER.prepare(_context(tmp_path, skills=("alpha",), sources=("empty-src",)))
    text = spec.stdin.decode()
    assert "Use the following skills: alpha." in text
    assert str(source / "alpha" / "SKILL.md") not in text
