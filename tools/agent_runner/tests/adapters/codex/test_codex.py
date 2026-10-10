"""Codex adapter: argv/stdin construction and JSONL/rollout collection."""

import hashlib
import json
from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.codex import ADAPTER
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
        agent_id="codex",
        role="inspect",
        work_dir=base / "worktree",
        prompt=prompt,
        deadline_seconds=60.0,
        result_shape=result_shape,  # type: ignore[arg-type]
    )
    agent = Agent(
        runtime="codex",
        command=["codex", "exec"],
        model="gpt-6.1-sol",
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


def _rollout(home: Path, thread_id: str, records: list[dict[str, object]]) -> None:
    session_dir = home / "sessions" / "2026" / "10" / "06"
    session_dir.mkdir(parents=True)
    body = "".join(json.dumps(r) + "\n" for r in records)
    (session_dir / f"rollout-2026-10-06T00-00-00-{thread_id}.jsonl").write_text(body, encoding="utf-8")


def test_prepare_passes_prompt_over_stdin_with_read_only_sandbox(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path))
    assert spec.argv[:2] == ("codex", "exec")
    assert "--json" in spec.argv
    assert spec.argv[spec.argv.index("-s") + 1] == "read-only"
    assert spec.argv[spec.argv.index("-m") + 1] == "gpt-6.1-sol"
    assert spec.argv[-1] == "-"
    assert all("PROMPT-UNIQUE-TEXT" not in argument for argument in spec.argv)
    assert "PROMPT-UNIQUE-TEXT" in spec.stdin.decode()


def test_prepare_write_role_uses_workspace_write_effort_and_args(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path, writes=True, effort="high", args=("--flag", "v")))
    assert spec.argv[spec.argv.index("-s") + 1] == "workspace-write"
    pair = spec.argv[spec.argv.index("-c") + 1]
    assert pair == "model_reasoning_effort=high"
    flag_at = spec.argv.index("--flag")
    assert spec.argv[flag_at + 1] == "v"
    assert flag_at < spec.argv.index("-")


def test_prepare_appends_json_shape_and_skill_instruction(tmp_path: Path) -> None:
    spec = ADAPTER.prepare(_context(tmp_path, skills=("pr-review",)))
    text = spec.stdin.decode()
    assert "JSON" in text
    assert "$pr-review" in text


def test_collect_success_builds_receipt_and_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    thread_id = "01a11250-9702-73b2-aebb-2f8f7434d5d6"
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": '{"answer": 1}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    )
    stderr = _write(tmp_path / "stderr.log", "")
    home = tmp_path / "codex-home"
    _rollout(
        home,
        thread_id,
        [
            {
                "type": "session_meta",
                "payload": {"session_id": thread_id, "cli_version": "0.160.1", "model_provider": "openai"},
            },
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "xhigh"}},
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    context = _context(tmp_path)
    outcome = ADAPTER.collect(context, stdout, stderr)
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.status == "success"
    assert outcome.receipt.payload == {"answer": 1}
    assert (outcome.receipt.run_id, outcome.receipt.request_id, outcome.receipt.agent_id, outcome.receipt.role) == (
        "run1",
        "req1",
        "codex",
        "inspect",
    )
    assert outcome.identity.session_id == thread_id
    assert outcome.identity.model == "gpt-6.1-sol"
    assert outcome.identity.effort == "xhigh"
    assert outcome.identity.provider == "openai"
    assert outcome.identity.cli_version == "0.160.1"


def test_collect_text_shape_returns_raw_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "free form answer"}},
    )
    outcome = ADAPTER.collect(_context(tmp_path, result_shape="text"), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == "free form answer"


def test_collect_turn_failed_is_response_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.failed", "error": {"message": "boom"}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_authentication_message_is_awaiting_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _write(tmp_path / "stdout.log", "")
    stderr = _write(tmp_path / "stderr.log", "ERROR: Not logged in. Please run 'codex login'.\n")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, stderr)
    assert outcome.receipt is None
    assert outcome.state == "awaiting_authentication"


def test_collect_missing_agent_message_is_response_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.completed", "usage": {}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_non_dict_json_shape_is_response_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {"type": "item.completed", "item": {"type": "agent_message", "text": "plain prose, not json"}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_embedded_json_object_is_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": 'Here is the result:\n{"k": "v", "n": 2}\nDone.'},
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"k": "v", "n": 2}


def test_collect_nested_json_object_returns_outer_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    text = 'Here is the result:\n```json\n{"findings": [{"id": "F1"}], "count": 1}\n```\nDone.'
    stdout = _stdout_events(
        tmp_path,
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"findings": [{"id": "F1"}], "count": 1}


def test_collect_authentication_words_in_output_do_not_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "curl -sI https://api.example.com/",
                "aggregated_output": "HTTP/1.1 401 Unauthorized\nlogin required\n",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"finding": "API returns 401 Unauthorized without token"}'},
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state is None
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"finding": "API returns 401 Unauthorized without token"}


def test_collect_identity_fields_stay_null_without_rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    monkeypatch.delenv("HOME", raising=False)
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "t-absent"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.identity.session_id == "t-absent"
    assert outcome.identity.model is None
    assert outcome.identity.effort is None
    assert outcome.identity.cli_version is None


def test_collect_skill_versions_hash_successfully_read_skill_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skills" / "pr-review"
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text("skill body", encoding="utf-8")
    thread_id = "t-skill"
    home = tmp_path / "codex-home"
    mentioned = tmp_path / "skills" / "mentioned-only"
    mentioned.mkdir(parents=True)
    (mentioned / "SKILL.md").write_text("mentioned body", encoding="utf-8")
    _rollout(
        home,
        thread_id,
        [
            {
                "type": "response_item",
                "payload": {"type": "message", "content": [{"text": f"see {mentioned / 'SKILL.md'}"}]},
            },
            {"type": "turn_context", "payload": {"model": "m1"}},
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": thread_id},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"/bin/bash -lc 'cat {skill_md}'",
                "aggregated_output": "skill body",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    expected = hashlib.sha256(b"skill body").hexdigest()
    assert outcome.identity.skill_versions == {"pr-review": expected}


def test_collect_skill_path_mentioned_in_message_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skills" / "pr-review"
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text("skill body", encoding="utf-8")
    thread_id = "t-mention"
    home = tmp_path / "codex-home"
    _rollout(
        home,
        thread_id,
        [
            {"type": "response_item", "payload": {"type": "message", "content": [{"text": f"read {skill_md}"}]}},
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": f'{{"used": "{skill_md}"}}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


@pytest.mark.parametrize(
    ("event_type", "status", "exit_code"),
    [
        ("item.started", "in_progress", None),
        ("item.completed", "failed", 1),
        ("item.completed", "completed", None),
        ("item.completed", "failed", 0),
    ],
    ids=["started-only", "failed", "unknown-exit", "failed-status"],
)
def test_collect_unproven_cat_reads_are_not_recorded(
    tmp_path: Path, event_type: str, status: str, exit_code: int | None
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": event_type,
            "item": {
                "type": "command_execution",
                "command": f"cat {skill}",
                "aggregated_output": "",
                "status": status,
                "exit_code": exit_code,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


@pytest.mark.parametrize("command", ["echo {path}", "cat {path} && echo done"], ids=["echo", "compound-command"])
def test_collect_commands_without_unambiguous_read_evidence_are_not_recorded(tmp_path: Path, command: str) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": command.format(path=skill),
                "aggregated_output": str(skill),
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


@pytest.mark.parametrize("kind", ["prompt-echo", "tool-invocation", "parsed-command"])
def test_collect_rollout_without_successful_execution_is_not_read_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"actual skill")
    payloads = {
        "prompt-echo": {
            "type": "message",
            "role": "user",
            "content": [
                {"command": f"cat {skill}", "arguments": {"path": str(skill), "input": f"cat {skill}"}},
            ],
        },
        "tool-invocation": {
            "type": "custom_tool_call",
            "name": "shell",
            "input": f"cat {skill}",
            "status": "completed",
            "call_id": "read1",
        },
        "parsed-command": {"type": "exec_command_begin", "parsed_cmd": [{"type": "read", "path": str(skill)}]},
    }
    home = tmp_path / "codex-home"
    _rollout(home, "synthetic-read", [{"type": "response_item", "payload": payloads[kind]}])
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("HOME", raising=False)
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "synthetic-read"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_observed_content_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"present body")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"/bin/bash -lc 'cat {skill}'",
                "aggregated_output": "observed body",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_file_deleted_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"skill body")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"cat {skill}",
                "aggregated_output": "skill body",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    skill.unlink()
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_observed_content_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"first line\nsecond line\n")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"cat {skill}",
                "aggregated_output": "first line\n",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_version_stays_unknown_when_file_has_no_utf8_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"caf\xe9")
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"cat {skill}",
                "aggregated_output": "caf\u00e9",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.identity.skill_versions == {}


def test_collect_skill_versions_hash_present_raw_utf8_bytes_with_newlines_and_nonascii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = "スキル本文\r\n本文の続き\n"
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(observed.encode("utf-8"))
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"/bin/bash -lc 'cat {skill}'",
                "aggregated_output": observed,
                "status": "completed",
                "exit_code": 0,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    expected = hashlib.sha256(observed.encode("utf-8")).hexdigest()
    assert outcome.identity.skill_versions == {"alpha": expected}


def test_prepare_tells_model_to_read_configured_skill_source_paths(tmp_path: Path) -> None:
    skill = tmp_path / "worktree" / "skill-src" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("configured body", encoding="utf-8")
    context = _context(tmp_path, skills=("alpha",), sources=("skill-src",))
    spec = ADAPTER.prepare(context)
    text = spec.stdin.decode()
    assert f'Read the configured source of skill "alpha" before using it: {skill}' in text
    assert f"{skill}. This absolute path takes priority over native discovery." in text
    assert "$alpha" in text
    assert all(str(skill) not in argument for argument in spec.argv)
    assert skill.is_file() and (tmp_path / "worktree" / "skill-src" / "alpha").is_dir()


def test_prepare_prefers_first_existing_source_in_order(tmp_path: Path) -> None:
    first = tmp_path / "worktree" / "primary" / "alpha" / "SKILL.md"
    first.parent.mkdir(parents=True)
    first.write_text("first body", encoding="utf-8")
    second = tmp_path / "worktree" / "secondary" / "alpha" / "SKILL.md"
    second.parent.mkdir(parents=True)
    second.write_text("second body", encoding="utf-8")
    spec = ADAPTER.prepare(_context(tmp_path, skills=("alpha",), sources=("secondary", "primary")))
    text = spec.stdin.decode()
    assert str(second) in text
    assert str(first) not in text


def test_prepare_absolute_source_directory_is_used(tmp_path: Path) -> None:
    skill = tmp_path / "abs-source" / "delta" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("abs body", encoding="utf-8")
    spec = ADAPTER.prepare(_context(tmp_path, skills=("delta",), sources=(str(tmp_path / "abs-source"),)))
    assert str(skill) in spec.stdin.decode()


def test_prepare_keeps_native_fallback_when_no_configured_source_exists(tmp_path: Path) -> None:
    source = tmp_path / "worktree" / "empty-src"
    source.mkdir(parents=True)
    spec = ADAPTER.prepare(_context(tmp_path, skills=("alpha",), sources=("empty-src",)))
    text = spec.stdin.decode()
    assert "Use the following skills: $alpha." in text
    assert str(source / "alpha" / "SKILL.md") not in text


@pytest.mark.parametrize("name", ["/etc/passwd", "../outside", "alpha/../outside"])
def test_prepare_absolute_or_parent_components_do_not_resolve_configured_sources(tmp_path: Path, name: str) -> None:
    source = tmp_path / "worktree" / "skill-src"
    existing = source / "alpha" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("configured body", encoding="utf-8")
    spec = ADAPTER.prepare(_context(tmp_path, skills=(name,), sources=("skill-src",)))
    text = spec.stdin.decode()
    assert "Use the following skills:" in text
    assert str(source) not in text and str(existing) not in text


def test_prepare_symlink_resolving_outside_source_is_not_adopted(tmp_path: Path) -> None:
    source_dir = tmp_path / "worktree" / "skill-src"
    external = tmp_path / "external-home" / "alpha" / "SKILL.md"
    external.parent.mkdir(parents=True)
    external.write_text("external body", encoding="utf-8")
    source_dir.mkdir(parents=True)
    (source_dir / "alpha").mkdir()
    (source_dir / "alpha" / "SKILL.md").symlink_to(external)
    spec = ADAPTER.prepare(_context(tmp_path, skills=("alpha",), sources=("skill-src",)))
    text = spec.stdin.decode()
    assert "Use the following skills: $alpha." in text
    assert str(source_dir) not in text and str(external) not in text


def test_prepare_relative_work_dir_resolves_absolute_prompt_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    base = tmp_path / "base"
    work_dir = base / "wanted-worktree"
    skill = work_dir / "skill-src" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("configured body", encoding="utf-8")
    monkeypatch.chdir(base)
    context = replace(_context(tmp_path, skills=("alpha",), sources=("skill-src",)), work_dir=Path("wanted-worktree"))
    spec = ADAPTER.prepare(context)
    assert str(skill) in spec.stdin.decode()
