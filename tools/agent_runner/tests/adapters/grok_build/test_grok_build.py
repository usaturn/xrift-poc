"""Grok Build (`grok`) adapter: prompt-file launch and single-JSON collection."""

import json
from pathlib import Path

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.adapters.grok_build import ADAPTER
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
        agent_id="grok-build",
        role="inspect",
        work_dir=base / "worktree",
        prompt="PROMPT-UNIQUE-TEXT",
        deadline_seconds=60.0,
        result_shape="json",
    )
    agent = Agent(
        runtime="grok-build",
        command=["grok"],
        model="grok-4.7",
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


def test_collect_duplicate_answer_is_not_success(tmp_path: Path) -> None:
    """重複キーの回答やその内側の object を採用すると、外側の指摘が黙って消える。"""
    stdout = _result(tmp_path, text='説明\n{"findings":[{"id":"消える指摘"}],"findings":[]}\n完了')
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.state == "response_failed"
    assert outcome.receipt is None


def test_prepare_write_role_bypasses_permissions_and_passes_effort(tmp_path: Path) -> None:
    """読み取りの役割で権限の確認を飛ばす・設定の model・effort・args が渡らないと黙って動く。"""
    spec = ADAPTER.prepare(_context(tmp_path, effort="high", args=("--flag",)))
    assert "bypassPermissions" not in spec.argv
    assert "grok-4.7" in spec.argv
    assert "high" in spec.argv
    assert "--flag" in spec.argv


def test_collect_success_builds_receipt_and_identity(tmp_path: Path) -> None:
    """sessionId・modelUsage・text を読み誤ると、payload と実行記録の identity が黙って欠ける・誤る。"""
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
    """stopReason が end_turn 以外（途中で止まった）でも、text の JSON を成功の回答として黙って採用する。"""
    stdout = _result(tmp_path, stopReason="error")
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is None
    assert outcome.state == "response_failed"


def test_collect_nested_json_result_returns_outer_object(tmp_path: Path) -> None:
    """前置きのある回答から内側の object を payload にすると、外側の指摘の一覧が黙って消える。"""
    stdout = _result(tmp_path, text='Result:\n{"findings": [{"id": "F1"}], "count": 1}\nDone.')
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.receipt is not None
    assert outcome.receipt.payload == {"findings": [{"id": "F1"}], "count": 1}


def test_collect_multiple_model_usage_keys_leaves_model_null(tmp_path: Path) -> None:
    """modelUsage の複数のモデルのどれかを、実測の model として黙って記録する。"""
    stdout = _result(tmp_path, modelUsage={"grok-4.7-build": {}, "grok-4.6": {}})
    outcome = ADAPTER.collect(_context(tmp_path), stdout, _write(tmp_path / "stderr.log", ""))
    assert outcome.identity.model is None
    assert outcome.identity.session_id == "6ff894db-518a-4db0-a3fe-f40377204bbd"
