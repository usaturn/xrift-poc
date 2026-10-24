"""Issue #598: issue_budget.py が能動時間・checkpoint の期日・同じ失敗の回数を数える仕様。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "issue_budget.py"
SPEC = importlib.util.spec_from_file_location("issue_budget", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
budget = importlib.util.module_from_spec(SPEC)
sys.modules["issue_budget"] = budget
SPEC.loader.exec_module(budget)

T0 = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def entries(*items: dict[str, Any]) -> list[Any]:
    return [budget.parse_entry(json.dumps(item, default=str)) for item in items]


def start(minutes: float = 0, **values: Any) -> dict[str, Any]:
    return {"event": "start", "at": at(minutes).isoformat(), "budget_minutes": 60, **values}


def event(name: str, minutes: float, **values: Any) -> dict[str, Any]:
    return {"event": name, "at": at(minutes).isoformat(), **values}


def test_user_wait_is_excluded_from_active_time() -> None:
    """利用者待ちの区間の数え方を誤ると、
    能動時間を黙って少なく数え、予算の 100% の停止（issue-to-pr の停止条件）を越えて進む。"""
    ledger = entries(start(), event("await-user", 10), event("resume", 40), event("end", 50))

    summary = budget.summarize(ledger, now=at(90))

    assert (summary.wall_seconds, summary.user_wait_seconds, summary.active_seconds) == (3000, 1800, 1200)
    assert summary.state == "ended"


def test_checkpoint_is_due_after_threshold_until_recorded() -> None:
    """閾値を越えた checkpoint を期日として返さなくなると、checkpoint と予算の 100% の停止が黙って来ない。"""
    ledger = entries(start())
    assert budget.summarize(ledger, now=at(29)).due_checkpoints == ()
    assert budget.summarize(ledger, now=at(31)).due_checkpoints == (50,)

    ledger += entries(event("checkpoint", 32, decision="continue", reason="成果が増えている"))
    summary = budget.summarize(ledger, now=at(33))

    assert summary.due_checkpoints == ()
    assert summary.last_checkpoint is not None and summary.last_checkpoint.decision == "continue"
    assert budget.summarize(ledger, now=at(61)).due_checkpoints == (100,)


def test_retry_limit_counts_same_reliability_signature() -> None:
    """同じ失敗の回数が上限に達したことを返さなくなると、controller が同じ手段の再試行を黙って続ける。"""
    ledger = entries(
        start(retry_limit=1),
        event("failure", 1, failure_class="reliability", signature="timeout"),
        event("failure", 2, failure_class="reliability", signature="format"),
    )
    assert budget.summarize(ledger, now=at(3)).retry_limit_reached == ()

    ledger += entries(event("failure", 4, failure_class="reliability", signature="timeout"))
    summary = budget.summarize(ledger, now=at(5))

    assert summary.retry_limit_reached == ("timeout",)
    assert summary.failures["timeout"].count == 2
    assert summary.safety_failure is False


def test_single_safety_failure_is_reported() -> None:
    """安全の失敗を返さなくなると、全体停止の材料が記録から黙って欠け、再開した controller が続行する。"""
    ledger = entries(start(), event("failure", 1, failure_class="safety", signature="unknown-send"))

    assert budget.summarize(ledger, now=at(2)).safety_failure is True


def test_inconsistent_ledger_is_rejected_with_line() -> None:
    """利用者待ちを閉じずに記録を足せると、
    待ちの区間が閉じないまま能動時間が止まり、checkpoint と予算の停止が黙って来ない。"""
    ledger = entries(start(), event("await-user", 1), event("checkpoint", 2, decision="stop", reason="x"))

    with pytest.raises(budget.LedgerError) as raised:
        budget.summarize(ledger, now=at(10))

    assert raised.value.line == 3


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_cli_rejects_a_corrupted_ledger_line(tmp_path: Path) -> None:
    """壊れた行を黙って読み飛ばすと、記録の欠けた ledger（失われた安全の失敗・利用者待ち）で数える。"""
    ledger = tmp_path / "budget.jsonl"
    assert run("start", "--ledger", str(ledger), "--budget-minutes", "30").returncode == 0
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")

    shown = run("show", "--ledger", str(ledger))

    assert shown.returncode != 0
    assert shown.stdout == ""
