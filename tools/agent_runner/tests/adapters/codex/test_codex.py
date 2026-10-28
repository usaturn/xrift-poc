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
) -> AdapterContext:
    request = Request(
        run_id="run1",
        request_id="req1",
        agent_id="codex",
        role="inspect",
        work_dir=base / "worktree",
        prompt="PROMPT-UNIQUE-TEXT",
        deadline_seconds=60.0,
        result_shape="json",
    )
    agent = Agent(
        runtime="codex",
        command=["codex", "exec"],
        model="gpt-6.1-sol",
        effort=effort,
        args=list(args),
        skill_sources=list(sources),
    )
    role = Role(skills=list(skills), writes=writes, deadline_seconds=60.0, result_shape="json")
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


def test_collect_duplicate_answer_is_not_success(tmp_path: Path) -> None:
    """重複キーの回答やその内側の object を採用すると、外側の指摘が黙って消える。"""
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '説明\n{"findings":[{"id":"消える指摘"}],"findings":[]}\n完了'},
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state == "response_failed"
    assert outcome.receipt is None


def test_prepare_write_role_uses_workspace_write_effort_and_args(tmp_path: Path) -> None:
    """依頼や設定の欠落・読み取りsandboxの欠けで、codexが黙って別のモデルや範囲で作業する。"""
    contexts = [
        _context(tmp_path / "r", effort="high", args=("--flag", "v")),
        _context(tmp_path / "w", writes=True, effort="high", args=("--flag", "v")),
    ]
    for context in contexts:
        context.request.prompt = " \t日本語の依頼 '引用' $HOME; $(echo forbidden)\n二行目 & <>\n末尾 \t\n"
        spec = ADAPTER.prepare(context)
        assert spec.stdin.startswith(context.request.prompt.encode())
    read_only, writable = (ADAPTER.prepare(context).argv for context in contexts)
    assert "read-only" in read_only
    assert "read-only" not in writable
    for argv in (read_only, writable):
        assert "gpt-6.1-sol" in argv
        assert any("high" in argument for argument in argv)
        assert argv[argv.index("--flag") + 1] == "v"


def test_collect_success_builds_receipt_and_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """thread_id と rollout の値を読み誤る・設定の値で埋めると、payload と実行記録の identity が黙って欠ける・誤る。"""
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
            # The configured model is gpt-6.1-sol; the observed one must come from the rollout.
            {"type": "turn_context", "payload": {"model": "gpt-6.1-observed", "effort": "xhigh"}},
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
    assert outcome.identity.model == "gpt-6.1-observed"
    assert outcome.identity.effort == "xhigh"
    assert outcome.identity.provider == "openai"
    assert outcome.identity.cli_version == "0.160.1"


def test_collect_turn_failed_is_response_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """turn.failed の実行に残った agent_message の JSON を、成功の回答として黙って採用する。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
        {"type": "turn.failed", "error": {"message": "boom"}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_nested_json_object_returns_outer_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前置きのある回答から内側の object を payload にすると、外側の指摘の一覧が黙って消える。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    text = 'Here is the result:\n```json\n{"findings": [{"id": "F1"}], "count": 1}\n```\nDone.'
    stdout = _stdout_events(
        tmp_path,
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"findings": [{"id": "F1"}], "count": 1}


def test_collect_identity_fields_stay_null_without_rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rollout が無いとき設定の model・effort を実測値として記録すると、実行記録の identity が黙って誤る。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    monkeypatch.delenv("HOME", raising=False)
    stdout = _stdout_events(
        tmp_path,
        {"type": "thread.started", "thread_id": "t-absent"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path, effort="high"), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.identity.session_id == "t-absent"
    assert outcome.identity.model is None
    assert outcome.identity.effort is None
    assert outcome.identity.cli_version is None


def test_collect_skill_versions_hash_successfully_read_skill_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cat で読んだ SKILL.md の版を記録しない・生バイト（CRLF・非 ASCII）と違う hash にすると、記録が黙って誤る。"""
    observed = "スキル本文\r\n本文の続き\n"
    skill = tmp_path / "skills" / "pr-review"
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_bytes(observed.encode("utf-8"))
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
                "aggregated_output": observed,
                "exit_code": 0,
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    expected = hashlib.sha256(observed.encode("utf-8")).hexdigest()
    assert outcome.identity.skill_versions == {"pr-review": expected}


def test_collect_skill_version_stays_unknown_when_observed_content_differs(tmp_path: Path) -> None:
    """読んだ後に書き換わった SKILL.md の今の hash を、読んだ版として黙って記録する。"""
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


def test_prepare_symlink_resolving_outside_source_is_not_adopted(tmp_path: Path) -> None:
    """読み込み元の中の symlink がたどる外のファイルを、設定したスキルとして担当に読ませる。"""
    source_dir = tmp_path / "worktree" / "skill-src"
    external = tmp_path / "external-home" / "alpha" / "SKILL.md"
    external.parent.mkdir(parents=True)
    external.write_text("external body", encoding="utf-8")
    source_dir.mkdir(parents=True)
    (source_dir / "alpha").mkdir()
    (source_dir / "alpha" / "SKILL.md").symlink_to(external)
    # Control: a regular SKILL.md in the same source is adopted, so the check is not vacuous.
    regular = source_dir / "beta" / "SKILL.md"
    regular.parent.mkdir()
    regular.write_text("regular body", encoding="utf-8")
    spec = ADAPTER.prepare(_context(tmp_path, skills=("alpha", "beta"), sources=("skill-src",)))
    text = spec.stdin.decode()
    assert str(regular) in text
    assert str(source_dir / "alpha") not in text and str(external) not in text
