"""Issue #598: task_watch の判定の優先順と、進捗 JSONL の読み取り。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "task_watch.py"
SPEC = importlib.util.spec_from_file_location("task_watch", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
# dataclass と pydantic は注釈の解決にモジュールの登録を要する。
sys.modules["task_watch"] = watch
SPEC.loader.exec_module(watch)


def obs(**values: Any) -> Any:
    fields: dict[str, Any] = {
        "elapsed": 0.0,
        "exited": False,
        "exit_code": None,
        "result_present": False,
        "since_substantive": 0.0,
        "safety_signature": None,
        "max_failure_count": 0,
        "investigation_in_reserve": False,
    }
    fields.update(values)
    return watch.Observation(**fields)


def test_exit_is_completed_only_with_zero_and_declared_result() -> None:
    policy = watch.Policy(deadline_seconds=10, stop="TERM", result_file="/r.json")

    assert watch.decide(policy, obs(exited=True, exit_code=0, result_present=True)) == ("completed", "")
    assert watch.decide(policy, obs(exited=True, exit_code=0)) == ("exited_incomplete", "")
    assert watch.decide(policy, obs(exited=True, exit_code=1, result_present=True)) == ("exited_incomplete", "")


def test_exit_without_declared_result_is_judged_by_exit_code() -> None:
    policy = watch.Policy(deadline_seconds=10, stop="TERM")

    assert watch.decide(policy, obs(exited=True, exit_code=0)) == ("completed", "")
    assert watch.decide(policy, obs(exited=True, exit_code=-15)) == ("exited_incomplete", "")


def test_safety_failure_precedes_deadline_and_other_reasons() -> None:
    policy = watch.Policy(deadline_seconds=1, stop="TERM", max_repeated_failures=1)

    decision = watch.decide(policy, obs(elapsed=5, safety_signature="leak", max_failure_count=3))

    assert decision == ("stop", "safety")


def test_deadline_stops_even_while_progressing() -> None:
    policy = watch.Policy(deadline_seconds=10, no_progress_seconds=5, progress_file="/p.jsonl", stop="INT")

    assert watch.decide(policy, obs(elapsed=10, since_substantive=0)) == ("stop", "deadline")


def test_repeated_failure_stops_at_the_declared_count() -> None:
    policy = watch.Policy(deadline_seconds=10, stop="TERM", max_repeated_failures=2)

    assert watch.decide(policy, obs(elapsed=1, max_failure_count=1)) == ("continue", "")
    assert watch.decide(policy, obs(elapsed=1, max_failure_count=2)) == ("stop", "repeated_failure")


def test_new_investigation_in_reserve_is_stopped() -> None:
    policy = watch.Policy(deadline_seconds=10, reserve_seconds=4, stop="TERM")

    assert watch.decide(policy, obs(elapsed=7)) == ("finalize", "")
    assert watch.decide(policy, obs(elapsed=7, investigation_in_reserve=True)) == ("stop", "investigation_in_reserve")


def test_no_progress_applies_only_before_research_deadline() -> None:
    policy = watch.Policy(
        deadline_seconds=10, reserve_seconds=4, no_progress_seconds=2, progress_file="/p.jsonl", stop="TERM"
    )

    assert watch.decide(policy, obs(elapsed=5, since_substantive=1)) == ("continue", "")
    assert watch.decide(policy, obs(elapsed=5, since_substantive=2)) == ("stop", "no_progress")
    assert watch.decide(policy, obs(elapsed=7, since_substantive=6)) == ("finalize", "")


def test_safety_failure_recorded_before_exit_is_not_completed() -> None:
    policy = watch.Policy(deadline_seconds=10, stop="TERM", result_file="/r.json")

    decision = watch.decide(policy, obs(exited=True, exit_code=0, result_present=True, safety_signature="leak"))

    assert decision == ("exited_incomplete", "safety")


def test_undeclared_stop_holds_instead_of_stopping() -> None:
    policy = watch.Policy(deadline_seconds=1, stop="none")

    assert watch.decide(policy, obs(elapsed=2)) == ("hold", "deadline")


@pytest.mark.parametrize(
    "values",
    [
        {"deadline_seconds": 0, "stop": "TERM"},
        {"deadline_seconds": 10, "reserve_seconds": 10, "stop": "TERM"},
        {"deadline_seconds": 10, "no_progress_seconds": 2, "stop": "TERM"},
        {"deadline_seconds": 10, "stop": "TERM", "stop_grace_seconds": 31},
        {"deadline_seconds": 10, "stop": "TERM", "max_repeated_failures": 0},
        {"deadline_seconds": 10, "stop": "KILL"},
    ],
)
def test_policy_rejects_values_outside_the_declared_ranges(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        watch.Policy(**values)


def test_progress_reader_counts_only_new_items_and_complete_lines(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    path.write_text(
        '{"kind": "progress", "items": ["src/x.py:120 の分岐を確認", " src/x.py:120 の分岐を確認 "]}\n'
        '{"kind": "progress", "items": ["src/x.py:120 の分岐を確認", "   "]}\n'
        '{"kind": "heartbeat"}\n'
        '{"kind": "bogus"}\n'
        "not json\n"
        '{"kind": "progress", "items": ["空入力で例外"',
        encoding="utf-8",
    )
    state = watch.ProgressState()

    watch.read_progress(path, state, elapsed=1.0, reserve_from=None)

    assert state.items == {"src/x.py:120 の分岐を確認"}
    assert (state.heartbeats, state.invalid_records, state.last_substantive) == (2, 2, 1.0)

    with path.open("a", encoding="utf-8") as stream:
        stream.write("]}\n")
    watch.read_progress(path, state, elapsed=2.5, reserve_from=None)

    assert state.items == {"src/x.py:120 の分岐を確認", "空入力で例外"}
    assert state.last_substantive == 2.5


def test_records_before_the_reserve_offset_are_not_investigation(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    path.write_text('{"kind": "progress", "items": ["a"]}\n', encoding="utf-8")
    state = watch.ProgressState()
    watch.read_progress(path, state, elapsed=1.0, reserve_from=None)

    # 保存への移行を求めた直後（猶予の間）に書かれた新しい項目は、予約枠の新規調査に数えない。
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "progress", "items": ["b"]}\n{"kind": "saved", "path": "/tmp/partial.md"}\n')
    reserve_from = path.stat().st_size
    watch.read_progress(path, state, elapsed=2.0, reserve_from=reserve_from)
    assert state.investigation_in_reserve is False
    assert state.items == {"a", "b"} and state.saved == ["/tmp/partial.md"]

    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "progress", "items": ["b"]}\n')
    watch.read_progress(path, state, elapsed=3.0, reserve_from=reserve_from)
    assert state.investigation_in_reserve is False

    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "progress", "items": ["c"]}\n')
    watch.read_progress(path, state, elapsed=4.0, reserve_from=reserve_from)
    assert state.investigation_in_reserve is True


def test_line_longer_than_read_limit_is_skipped_and_later_records_are_read(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    long_item = "x" * (watch.READ_LIMIT_BYTES * 3 // 2)
    path.write_text(
        f'{{"kind": "progress", "items": ["{long_item}"]}}\n'
        '{"kind": "failure", "class": "safety", "signature": "unknown-send"}\n',
        encoding="utf-8",
    )
    state = watch.ProgressState()

    watch.read_progress(path, state, elapsed=1.0, reserve_from=None)

    assert (state.invalid_records, state.safety_signature, state.offset) == (1, "unknown-send", path.stat().st_size)


def test_backlog_beyond_read_limit_is_read_in_one_poll(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    lines = [f'{{"kind": "progress", "items": ["item {number:06d}"]}}\n' for number in range(40000)]
    path.write_text("".join(lines), encoding="utf-8")
    assert path.stat().st_size > watch.READ_LIMIT_BYTES

    state = watch.ProgressState()
    watch.read_progress(path, state, elapsed=1.0, reserve_from=None)

    assert len(state.items) == 40000 and state.offset == path.stat().st_size


def test_failures_are_counted_per_signature_and_first_safety_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    path.write_text(
        '{"kind": "failure", "class": "reliability", "signature": "timeout"}\n'
        '{"kind": "failure", "class": "reliability", "signature": "timeout"}\n'
        '{"kind": "failure", "class": "safety", "signature": "unknown-send"}\n'
        '{"kind": "failure", "class": "safety", "signature": "later"}\n'
        '{"kind": "failure", "class": "other", "signature": "x"}\n'
        '{"kind": "saved", "path": "relative.md"}\n',
        encoding="utf-8",
    )
    state = watch.ProgressState()

    watch.read_progress(path, state, elapsed=1.0, reserve_from=None)

    assert {name: (item.failure_class, item.count) for name, item in state.failures.items()} == {
        "timeout": ("reliability", 2),
        "unknown-send": ("safety", 1),
        "later": ("safety", 1),
    }
    assert state.safety_signature == "unknown-send"
    assert state.invalid_records == 2


def test_missing_or_special_progress_file_is_not_progress(tmp_path: Path) -> None:
    state = watch.ProgressState()
    watch.read_progress(tmp_path / "missing.jsonl", state, elapsed=1.0, reserve_from=None)
    watch.read_progress(None, state, elapsed=1.0, reserve_from=None)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    watch.read_progress(fifo, state, elapsed=1.0, reserve_from=None)

    assert (state.items, state.heartbeats, state.invalid_records, state.offset) == (set(), 0, 0, 0)
