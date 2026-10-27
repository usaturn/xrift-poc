"""Cursor CLI (`agent -p`) adapter: stream-json events and failure classification."""

import hashlib
import json
from pathlib import Path

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.cursor import ADAPTER
from tools.agent_runner.models import Agent, Request, Role


def _context(
    base: Path,
    *,
    writes: bool = False,
    effort: str | None = None,
    args: tuple[str, ...] = (),
) -> AdapterContext:
    request = Request(
        run_id="run1",
        request_id="req1",
        agent_id="cursor",
        role="inspect",
        work_dir=base / "worktree",
        prompt="PROMPT-UNIQUE-TEXT",
        deadline_seconds=60.0,
        result_shape="json",
    )
    agent = Agent(
        runtime="cursor",
        command=["agent"],
        model="auto",
        effort=effort,
        args=list(args),
    )
    role = Role(skills=[], writes=writes, deadline_seconds=60.0, result_shape="json")
    attempt = base / "attempt"
    attempt.mkdir(parents=True, exist_ok=True)
    return AdapterContext(request, agent, role, base / "worktree", attempt / "receipt.json")


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _stdout_events(tmp_path: Path, *events: dict[str, object]) -> Path:
    return _write(tmp_path / "stdout.log", "".join(json.dumps(e) + "\n" for e in events))


def test_collect_duplicate_answer_is_not_success(tmp_path: Path) -> None:
    """重複キーの回答やその内側の object を採用すると、外側の指摘が黙って消える。"""
    stdout = _stdout_events(
        tmp_path,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '説明\n{"findings":[{"id":"消える指摘"}],"findings":[]}\n完了',
        },
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state == "response_failed"
    assert outcome.receipt is None


def test_prepare_write_role_forces_and_effort_uses_bracket(tmp_path: Path) -> None:
    """依頼文や設定の値が落ちると、cursor が既定のモデルや別の依頼で黙って作業する。"""
    context = _context(tmp_path, writes=True, effort="high", args=("--flag",))
    context.request.prompt = " \t日本語の依頼 '引用' $HOME; $(echo forbidden)\n二行目 & <>\n末尾 \t\n"
    spec = ADAPTER.prepare(context)
    assert spec.stdin.startswith(context.request.prompt.encode())
    assert all(context.request.prompt not in argument for argument in spec.argv)
    assert any("auto" in argument for argument in spec.argv)
    assert any("high" in argument for argument in spec.argv)
    assert "--flag" in spec.argv


def test_collect_success_builds_receipt_and_identity(tmp_path: Path) -> None:
    """init の session・model と result の本文を読み誤ると、payload と実行記録の identity が黙って欠ける・誤る。"""
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
    """is_error の result の本文を、成功の回答として黙って採用する。"""
    stdout = _stdout_events(
        tmp_path,
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": '{"ok": true}'},
    )
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_nested_json_result_returns_outer_object(tmp_path: Path) -> None:
    """前置きのある回答から内側の object を payload にすると、外側の指摘の一覧が黙って消える。"""
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


def test_collect_skill_versions_from_successful_read_completion(tmp_path: Path) -> None:
    """readToolCall の完了で読んだ SKILL.md の版を記録しないと、実行記録の provenance が黙って欠ける。"""
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


def test_collect_reading_skill_backup_does_not_record_unread_skill(tmp_path: Path) -> None:
    """SKILL.md.bak の読み込みを、親ディレクトリのスキルの版として黙って記録する。"""
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
