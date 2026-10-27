"""pi adapter: launcher argv shape and NDJSON event collection."""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.pi import ADAPTER


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


def test_collect_duplicate_answer_is_not_success(context: AdapterContext) -> None:
    """重複キーの回答を採用すると指摘が黙って消え、後続フェンスでも成功に救済される。"""
    text = '説明\n```json\n{"findings":[{"id":"消える指摘"}],"findings":[]}\n```\n```json\n{"findings":[]}\n```'
    outcome = _collect(context, [_session(), _agent_end([_assistant(text)]), {"type": "agent_settled"}])
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"
    assert outcome.receipt.payload is None


def test_prepare_maps_skill_sources_and_args(context: AdapterContext) -> None:
    """依頼文・設定・skill の読み込み元が落ちると、pi が別の依頼やモデルで動き、版を黙って誤記録する。"""
    context.request.prompt = " \t日本語の依頼 '引用' $HOME; $(echo forbidden)\n二行目 & <>\n末尾 \t\n"
    context.agent.provider = "provider-sentinel"
    context.agent.model = "model-sentinel"
    context.agent.effort = "effort-sentinel"
    context.agent.skill_sources = ["/skills/a", "/skills/b"]
    context.agent.args = ["--arg-sentinel"]
    spec = ADAPTER.prepare(context)
    for value in ("provider-sentinel", "model-sentinel", "effort-sentinel", "/skills/a", "/skills/b"):
        assert value in spec.argv
    assert spec.argv[-1] == "--arg-sentinel"
    assert spec.stdin == context.request.prompt.encode()
    assert all(context.request.prompt not in argument for argument in spec.argv)


def test_collect_success_extracts_identity_and_payload(context: AdapterContext) -> None:
    """最後の assistant の値と session の id を読み誤る・設定の値で埋めると、payload と identity が黙って誤る。"""
    # The configured values are gpt-6.1-sol / openai-codex / xhigh; the observed ones differ.
    observed = _assistant(model="observed-model", provider="observed-provider", thinking="observed-level")
    outcome = _collect(
        context, [_session(), {"type": "agent_start"}, _agent_end([observed]), {"type": "agent_settled"}]
    )
    receipt = outcome.receipt
    assert receipt is not None and receipt.status == "success"
    assert receipt.payload == {"ok": True}
    assert outcome.identity.model == "observed-model"
    assert outcome.identity.provider == "observed-provider"
    assert outcome.identity.effort == "observed-level"
    assert outcome.identity.session_id == "sess-1"
    assert outcome.identity.cli_version is None


def test_missing_agent_settled_is_response_failed(context: AdapterContext) -> None:
    """agent_settled の無い（きれいに終わっていない）実行の回答を、成功として黙って採用する。"""
    outcome = _collect(context, [_session(), _agent_end()])
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_unknown_stop_reason_is_response_failed(context: AdapterContext) -> None:
    """stopReason が length など正常でない終わり方でも、本文の JSON を成功の回答として黙って採用する。"""
    outcome = _collect(
        context,
        [_session(), _agent_end([_assistant(stop_reason="length")]), {"type": "agent_settled"}],
    )
    assert outcome.receipt is not None and outcome.receipt.status == "response_failed"


def test_skill_source_and_observed_skill_md_are_hashed(context: AdapterContext, tmp_path: Path) -> None:
    """--skill の読み込み元とツールで読んだ SKILL.md の版を記録しないと、実行記録の provenance が黙って欠ける。"""
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
