"""devin adapter: --prompt-file launch and ATIF export collection."""

import hashlib
import json
from pathlib import Path

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.devin import ADAPTER


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


def _collect(context: AdapterContext, stdout: str, *, export: dict[str, object] | None = None):
    out = _write(context, "stdout.log", stdout)
    err = _write(context, "stderr.log", "")
    _write(context, "export.json", json.dumps(export if export is not None else _export()))
    return ADAPTER.collect(context, out, err)


def test_collect_duplicate_answer_is_not_success(context: AdapterContext) -> None:
    """重複キーの回答を採用すると指摘が黙って消え、後続フェンスでも成功に救済される。"""
    text = '説明\n```json\n{"findings":[{"id":"消える指摘"}],"findings":[]}\n```\n```json\n{"findings":[]}\n```'
    outcome = _collect(context, text, export=_export(message=text))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"
    assert outcome.receipt.payload is None


def test_prepare_appends_agent_args(context: AdapterContext) -> None:
    """依頼文や設定の値が落ちると、devin が既定のモデルや別の依頼で黙って作業する。"""
    context.request.prompt = " \t日本語の依頼 '引用' $HOME; $(echo forbidden)\n二行目 & <>\n末尾 \t\n"
    context.agent.model = "model-sentinel"
    context.agent.args = ["--arg-sentinel"]
    spec = ADAPTER.prepare(context)
    assert "model-sentinel" in spec.argv
    assert spec.argv[-1] == "--arg-sentinel"
    prompt_file = Path(spec.argv[spec.argv.index("--prompt-file") + 1])
    assert prompt_file.read_bytes() == context.request.prompt.encode()
    assert all(context.request.prompt not in argument for argument in spec.argv)


def test_collect_success_extracts_identity(context: AdapterContext) -> None:
    """export の model_name・version・session_id と stdout を読み誤ると、payload と identity が黙って欠ける・誤る。"""
    outcome = _collect(context, '{"ok": true}\n')
    receipt = outcome.receipt
    assert receipt is not None and receipt.status == "success"
    assert receipt.payload == {"ok": True}
    assert outcome.identity.model == "SWE-2 Max"
    assert outcome.identity.cli_version == "3000.11.3"
    assert outcome.identity.session_id == "session-1"
    assert outcome.identity.provider is None and outcome.identity.effort is None


def test_export_without_agent_message_fails(context: AdapterContext) -> None:
    """transcript に担当の最後の回答が無い実行を、stdout の JSON だけで成功として黙って採用する。"""
    outcome = _collect(context, '{"ok": true}', export=_export(message=None))
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_skill_tool_call_records_file_hash(context: AdapterContext, tmp_path: Path) -> None:
    """tool_calls に現れた SKILL.md の版を記録しないと、実行記録の provenance が黙って欠ける。"""
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


def test_relative_skill_md_in_tool_call_is_hashed(context: AdapterContext) -> None:
    """作業ディレクトリからの相対パスで読んだ SKILL.md の版を記録しないと、実行記録の provenance が黙って欠ける。"""
    skill = context.work_dir / "docs" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: docs\n---\nbody\n", encoding="utf-8")
    calls: list[dict[str, object]] = [
        {"type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "docs/SKILL.md"})}}
    ]
    outcome = _collect(context, '{"ok": true}', export=_export(tool_calls=calls))
    expected = "sha256:" + hashlib.sha256(skill.read_bytes()).hexdigest()
    assert outcome.identity.skill_versions == {"docs": expected}
