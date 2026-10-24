"""Issue #598: task_watch の判定の優先順と、進捗 JSONL の読み取り。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

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
    """0 以外で終わっても結果ファイルがあれば完了とし、失敗した worker の結果で次へ進む。"""
    policy = watch.Policy(deadline_seconds=10, stop="TERM", result_file="/r.json")

    assert watch.decide(policy, obs(exited=True, exit_code=0, result_present=True)) == ("completed", "")
    assert watch.decide(policy, obs(exited=True, exit_code=0)) == ("exited_incomplete", "")
    assert watch.decide(policy, obs(exited=True, exit_code=1, result_present=True)) == ("exited_incomplete", "")


def test_exit_without_declared_result_is_judged_by_exit_code() -> None:
    """結果ファイルを宣言しない worker がシグナルで終わっても完了とし、途中の成果で次へ進む。"""
    policy = watch.Policy(deadline_seconds=10, stop="TERM")

    assert watch.decide(policy, obs(exited=True, exit_code=0)) == ("completed", "")
    assert watch.decide(policy, obs(exited=True, exit_code=-15)) == ("exited_incomplete", "")


def test_safety_failure_precedes_deadline_and_other_reasons() -> None:
    """安全の失敗が期限などの理由に隠れ、全体停止すべき失敗を部分成果の保存と再試行で扱う。"""
    policy = watch.Policy(deadline_seconds=1, stop="TERM", max_repeated_failures=1)

    decision = watch.decide(policy, obs(elapsed=5, safety_signature="leak", max_failure_count=3))

    assert decision == ("stop", "safety")


def test_safety_failure_recorded_before_exit_is_not_completed() -> None:
    """安全の失敗を記録して exit 0 で終えた worker を、完了として次へ進める。"""
    policy = watch.Policy(deadline_seconds=10, stop="TERM", result_file="/r.json")

    decision = watch.decide(policy, obs(exited=True, exit_code=0, result_present=True, safety_signature="leak"))

    assert decision == ("exited_incomplete", "safety")


def test_progress_reader_counts_only_new_items_and_complete_lines(tmp_path: Path) -> None:
    """書きかけの末尾行を不正な記録として読み捨て、分けて書かれた記録（安全の失敗を含む）を落とす。"""
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


def test_line_longer_than_read_limit_is_skipped_and_later_records_are_read(tmp_path: Path) -> None:
    """上限を超える 1 行で読み取りが止まり、その後の安全の失敗・保存の記録を読まない。"""
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
    """1 回の読み取りで上限を超えた分を読み残し、終了の直前の安全の失敗を読まずに完了とする。"""
    path = tmp_path / "progress.jsonl"
    lines = [f'{{"kind": "progress", "items": ["item {number:06d}"]}}\n' for number in range(40000)]
    path.write_text("".join(lines), encoding="utf-8")
    assert path.stat().st_size > watch.READ_LIMIT_BYTES

    state = watch.ProgressState()
    watch.read_progress(path, state, elapsed=1.0, reserve_from=None)

    assert len(state.items) == 40000 and state.offset == path.stat().st_size
