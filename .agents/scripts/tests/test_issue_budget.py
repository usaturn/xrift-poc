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
    ledger = entries(start(), event("await-user", 10), event("resume", 40), event("end", 50))

    summary = budget.summarize(ledger, now=at(90))

    assert (summary.wall_seconds, summary.user_wait_seconds, summary.active_seconds) == (3000, 1800, 1200)
    assert summary.state == "ended"


def test_open_user_wait_counts_until_now() -> None:
    summary = budget.summarize(entries(start(), event("await-user", 10)), now=at(25))

    assert (summary.state, summary.user_wait_seconds, summary.active_seconds) == ("waiting_user", 900, 600)


def test_checkpoint_is_due_after_threshold_until_recorded() -> None:
    ledger = entries(start())
    assert budget.summarize(ledger, now=at(29)).due_checkpoints == ()
    assert budget.summarize(ledger, now=at(31)).due_checkpoints == (50,)

    ledger += entries(event("checkpoint", 32, decision="continue", reason="成果が増えている"))
    summary = budget.summarize(ledger, now=at(33))

    assert summary.due_checkpoints == ()
    assert summary.last_checkpoint is not None and summary.last_checkpoint.decision == "continue"
    assert budget.summarize(ledger, now=at(61)).due_checkpoints == (100,)


def test_user_wait_delays_checkpoint_due_time() -> None:
    ledger = entries(start(), event("await-user", 10), event("resume", 40))

    assert budget.summarize(ledger, now=at(55)).due_checkpoints == ()
    assert budget.summarize(ledger, now=at(61)).due_checkpoints == (50,)


def test_retry_limit_counts_same_reliability_signature() -> None:
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
    ledger = entries(start(), event("failure", 1, failure_class="safety", signature="unknown-send"))

    assert budget.summarize(ledger, now=at(2)).safety_failure is True


@pytest.mark.parametrize(
    ("items", "line"),
    [
        ((event("resume", 1),), 1),
        ((start(), start(1)), 2),
        ((start(5), event("checkpoint", 4, decision="continue", reason="x")), 2),
        ((start(), event("resume", 1)), 2),
        ((start(), event("await-user", 1), event("checkpoint", 2, decision="stop", reason="x")), 3),
        ((start(), event("end", 1), event("checkpoint", 2, decision="stop", reason="x")), 3),
    ],
)
def test_inconsistent_ledger_is_rejected_with_line(items: tuple[dict[str, Any], ...], line: int) -> None:
    with pytest.raises(budget.LedgerError) as raised:
        budget.summarize(entries(*items), now=at(10))

    assert raised.value.line == line


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_cli_records_and_shows_budget(tmp_path: Path) -> None:
    ledger = str(tmp_path / "budget.jsonl")
    assert run("start", "--ledger", ledger, "--budget-minutes", "240").returncode == 0
    assert run("start", "--ledger", ledger, "--budget-minutes", "240").returncode == 2

    assert run("mark", "--ledger", ledger, "--event", "await-user", "--reason", "設計の確認").returncode == 0
    rejected = run("mark", "--ledger", ledger, "--event", "checkpoint", "--decision", "continue", "--reason", "x")
    assert rejected.returncode == 2
    assert run("mark", "--ledger", ledger, "--event", "resume").returncode == 0
    reason = "前回から成果が無い\n付随処理が本体を上回った"
    checkpoint = run("mark", "--ledger", ledger, "--event", "checkpoint", "--decision", "shrink", "--reason", reason)
    assert checkpoint.returncode == 0
    missing = run("mark", "--ledger", ledger, "--event", "failure", "--signature", "timeout")
    assert missing.returncode == 2

    shown = run("show", "--ledger", ledger)
    summary = json.loads(shown.stdout)

    assert shown.returncode == 0
    assert (summary["state"], summary["budget_seconds"]) == ("active", 14400)
    assert summary["last_checkpoint"]["decision"] == "shrink"
    assert summary["last_checkpoint"]["reason"] == reason
    assert len(Path(ledger).read_text(encoding="utf-8").splitlines()) == 4


def test_cli_rejects_corrupted_ledger_with_line_number(tmp_path: Path) -> None:
    ledger = tmp_path / "budget.jsonl"
    assert run("start", "--ledger", str(ledger), "--budget-minutes", "30").returncode == 0
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")

    shown = run("show", "--ledger", str(ledger))

    assert shown.returncode == 2
    assert "line 2" in shown.stderr


def test_cli_keeps_ledger_readable_with_unicode_line_separators(tmp_path: Path) -> None:
    ledger = str(tmp_path / "budget.jsonl")
    assert run("start", "--ledger", ledger, "--budget-minutes", "30").returncode == 0
    reason = "貼り付けた文\u2028段落\u2029次\x85終わり"

    marked = run("mark", "--ledger", ledger, "--event", "checkpoint", "--decision", "continue", "--reason", reason)
    shown = run("show", "--ledger", ledger)

    assert (marked.returncode, shown.returncode) == (0, 0)
    assert json.loads(shown.stdout)["last_checkpoint"]["reason"] == reason
    assert run("mark", "--ledger", ledger, "--event", "end").returncode == 0
