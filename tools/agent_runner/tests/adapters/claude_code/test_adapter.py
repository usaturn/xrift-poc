"""claude-code adapter: launch spec shape and stream-json collection."""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.claude_code import ADAPTER


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


def test_collect_duplicate_answer_is_not_success(context: AdapterContext) -> None:
    """重複キーの回答を採用すると指摘が黙って消え、後続フェンスでも成功に救済される。"""
    text = '説明\n```json\n{"findings":[{"id":"消える指摘"}],"findings":[]}\n```\n```json\n{"findings":[]}\n```'
    outcome = _collect(context, _init(), _result(text))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"
    assert outcome.receipt.payload is None


def test_prepare_maps_effort_and_appends_agent_args(context: AdapterContext) -> None:
    """依頼文や設定の値が落ちると、claude が既定のモデルや別の依頼で黙って作業する。"""
    context.request.prompt = " \t日本語の依頼 '引用' $HOME; $(echo forbidden)\n二行目 & <>\n末尾 \t\n"
    context.agent.model = "model-sentinel"
    context.agent.effort = "effort-sentinel"
    context.agent.args = ["--arg-sentinel", "value-sentinel"]
    spec = ADAPTER.prepare(context)
    assert "model-sentinel" in spec.argv
    assert "effort-sentinel" in spec.argv
    assert spec.argv[-2:] == ("--arg-sentinel", "value-sentinel")
    assert spec.stdin == context.request.prompt.encode()
    assert all(context.request.prompt not in argument for argument in spec.argv)


def test_collect_success_extracts_identity_and_json_payload(context: AdapterContext) -> None:
    """本文と init の model・session・版を読み誤ると、payload と実行記録の identity が黙って欠ける・誤る。"""
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
    """init の model（別名）を実測の model として記録すると、実行記録の model が黙って誤る。"""
    outcome = _collect(context, _init("router-alias"), _assistant("{}", "claude-opus-5-5"), _result("{}"))
    assert outcome.identity.model == "claude-opus-5-5"


def test_permission_denials_report_awaiting_approval(context: AdapterContext) -> None:
    """ツールの実行を拒まれた実行（subtype は success）の回答を、成功として黙って採用する。"""
    denials: list[dict[str, object]] = [{"tool": "Bash", "command": "rm -rf /"}]
    outcome = _collect(context, _init(), _result(denials=denials))
    assert outcome.receipt is not None and outcome.receipt.status == "awaiting_approval"


@pytest.mark.parametrize(
    ("subtype", "is_error"),
    [
        # is_error を見ないと、subtype が success の error の結果を成功にする
        ("success", True),
        # subtype を見ないと、is_error の無い error の結果を成功にする
        ("error_during_execution", False),
    ],
    ids=["is-error", "error-subtype"],
)
def test_error_subtype_reports_response_failed(context: AdapterContext, subtype: str, is_error: bool) -> None:
    """error の結果（is_error、または success 以外の subtype）の本文を、成功の回答として黙って採用する。"""
    outcome = _collect(context, _init(), _result('{"ok": true}', subtype=subtype, is_error=is_error))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_skill_tool_use_records_file_hash(context: AdapterContext) -> None:
    """Skill で使ったスキルの版を記録しない・init の一覧（読んでいない）を版として記録すると、記録が黙って誤る。"""
    skill = context.work_dir / ".claude" / "skills" / "find-docs" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: find-docs\n---\nbody\n", encoding="utf-8")
    unread = context.work_dir / ".claude" / "skills" / "installed-but-not-read" / "SKILL.md"
    unread.parent.mkdir(parents=True)
    unread.write_text("---\nname: installed-but-not-read\n---\nbody\n", encoding="utf-8")
    init = _init()
    init["skills"] = ["installed-but-not-read"]
    use: dict[str, object] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "m",
            "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "find-docs"}}],
        },
    }
    outcome = _collect(context, init, use, _result())
    expected = "sha256:" + hashlib.sha256(skill.read_bytes()).hexdigest()
    assert outcome.identity.skill_versions == {"find-docs": expected}
