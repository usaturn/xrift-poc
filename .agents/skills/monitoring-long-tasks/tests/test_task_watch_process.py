"""Issue #598: task_watch が実プロセスを宣言どおりに判定・停止することを fake CLI で確かめる。"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "task_watch.py"
FAKE = Path(__file__).resolve().parent / "fake_worker.py"
SPEC = importlib.util.spec_from_file_location("task_watch", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
sys.modules["task_watch"] = watch
SPEC.loader.exec_module(watch)


def proc_starttime(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[19])


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in (b"Z", b"X")


def fake_argv(tmp_path: Path, mode: str, *, partial: bool = False) -> list[str]:
    argv = [
        sys.executable,
        str(FAKE),
        mode,
        str(tmp_path / "progress.jsonl"),
        str(tmp_path / "result.json"),
        str(tmp_path / "state"),
    ]
    if partial:
        argv.append(str(tmp_path / "partial.md"))
    return argv


def watch_argv(tmp_path: Path, mode: str, *options: str, partial: bool = False) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "run",
        "--state-dir",
        str(tmp_path / "state"),
        "--poll-seconds",
        "0.05",
        "--progress-file",
        str(tmp_path / "progress.jsonl"),
        *options,
        "--",
        *fake_argv(tmp_path, mode, partial=partial),
    ]


def run_watch(tmp_path: Path, mode: str, *options: str, partial: bool = False) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        watch_argv(tmp_path, mode, *options, partial=partial), capture_output=True, text=True, timeout=60
    )
    final = json.loads((tmp_path / "state" / "final.json").read_text(encoding="utf-8"))
    return completed.returncode, final


def call(*args: str) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)
    return completed.returncode, json.loads(completed.stdout)


def wait_for(path: Path, timeout: float = 10.0) -> None:
    until = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < until, f"{path} did not appear"
        time.sleep(0.05)


def test_unstoppable_worker_is_held_and_left_running(tmp_path: Path) -> None:
    """止まらない worker を停止済みと報告し、司令塔が動いている worker の作業領域を消して依存作業を始める。"""
    code, final = run_watch(
        tmp_path, "ignore-term", "--deadline-seconds", "1", "--stop", "TERM", "--stop-grace-seconds", "0.5"
    )
    pid = final["target"]["pid"]
    try:
        assert (code, final["decision"], final["reason"]) == (3, "hold", "deadline")
        assert final["stop"]["confirmed"] is False and pid in final["residual_pids"]
        assert alive(pid)
        assert (tmp_path / "state").is_dir()
    finally:
        os.killpg(pid, signal.SIGKILL)


def test_undeclared_stop_never_signals(tmp_path: Path) -> None:
    """停止を宣言していない対象へシグナルを送り、止めてよいと言われていないプロセスを止める。"""
    code, final = run_watch(tmp_path, "undeclared", "--deadline-seconds", "0.3", "--stop", "none")

    assert (code, final["decision"]) == (0, "completed")
    assert final["deadline_exceeded"] is True and final["stop"] is None
    assert not (tmp_path / "got-term").exists()
    events = [json.loads(line) for line in (tmp_path / "state" / "events.jsonl").read_text().splitlines()]
    assert any(event["decision"] == "hold" and event["reason"] == "deadline" for event in events)


def test_partial_result_survives_stop_for_investigation_in_reserve(tmp_path: Path) -> None:
    """停止した worker が保存した部分成果が final.json から欠け、司令塔が部分成果を見落とす。"""
    code, final = run_watch(
        tmp_path,
        "reserve-investigate",
        "--deadline-seconds",
        "10",
        "--reserve-seconds",
        "9.5",
        "--stop",
        "TERM",
        partial=True,
    )

    assert (code, final["decision"], final["reason"]) == (1, "stopped", "investigation_in_reserve")
    assert final["finalize_requested"] is True and (tmp_path / "state" / "finalize.requested").is_file()
    assert final["partial_results"] == [str(tmp_path / "partial.md")]
    assert (tmp_path / "partial.md").read_text(encoding="utf-8") == "途中までの結論\n"


def test_safety_failure_then_exit_is_not_completed(tmp_path: Path) -> None:
    """観測が安全の失敗の記録を判定へ渡さず、安全の失敗の後に終えた worker を完了とする。"""
    code, final = run_watch(
        tmp_path,
        "safety-exit",
        "--deadline-seconds",
        "20",
        "--result-file",
        str(tmp_path / "result.json"),
        "--stop",
        "TERM",
    )

    # watcher が先に読めば停止、終了が先なら終了の判定になる。どちらでも完了にはしない。
    assert (code, final["reason"]) == (1, "safety")
    assert final["decision"] in ("exited_incomplete", "stopped")


def test_processes_left_in_the_group_after_exit_are_held(tmp_path: Path) -> None:
    """終了後も同じ group のプロセスが残るのに完了とし、動いているプロセスの作業領域を片付けさせる。"""
    code, final = run_watch(
        tmp_path, "orphan-child", "--deadline-seconds", "20", "--stop", "TERM", "--stop-grace-seconds", "1"
    )
    try:
        assert (code, final["decision"], final["reason"]) == (3, "hold", "residual_processes")
        assert final["target"]["exit_code"] == 0 and final["residual_pids"]
        assert all(alive(pid) for pid in final["residual_pids"])
    finally:
        os.killpg(final["target"]["pid"], signal.SIGKILL)


def test_stop_refuses_unrelated_and_caller_pids(tmp_path: Path) -> None:
    """PID の再利用・無関係なプロセス・呼び出し元自身へ SIGINT を送り、別のプロセスを止める。"""
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        cases = {
            "unrelated": (sleeper.pid, proc_starttime(sleeper.pid)),
            "reused": (sleeper.pid, proc_starttime(sleeper.pid) + 1),
            "caller": (os.getpid(), proc_starttime(os.getpid())),
        }
        for name, (pid, starttime) in cases.items():
            state = tmp_path / name
            state.mkdir()
            declaration = watch.Declaration(
                schema_version=1,
                created_at="2026-10-10T00:00:00Z",
                state_dir=str(state),
                cwd=str(tmp_path),
                argv=("sleep", "30"),
                policy=watch.Policy(deadline_seconds=10, stop="TERM"),
                watcher=watch.Identity(pid=pid, starttime=starttime),
                target=watch.Identity(pid=sleeper.pid, starttime=proc_starttime(sleeper.pid)),
            )
            (state / "declaration.json").write_text(declaration.model_dump_json(), encoding="utf-8")

            code, output = call("stop", "--state-dir", str(state))

            assert (code, output["refused"]) == (2, True), name
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_state_dir_with_spaces_round_trip(tmp_path: Path) -> None:
    """生きている watcher・対象を status が終了済みと報告し、動いている対象の作業領域を片付けさせる。"""
    # xrift-poc #21 の受け入れ基準の実入力（空白を含む state dir のパス）。
    base = tmp_path / "作業 ディレクトリ"
    base.mkdir()
    watcher = subprocess.Popen(
        watch_argv(base, "endless", "--deadline-seconds", "60", "--stop", "TERM"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(base / "state" / "status.json")

        code, status = call("status", "--state-dir", str(base / "state"))
        assert (code, status["watcher_alive"], status["target_alive"]) == (0, True, True)
    finally:
        # 後始末。宣言どおりに対象を止めてから watcher を回収する。
        subprocess.run(
            [sys.executable, str(SCRIPT), "stop", "--state-dir", str(base / "state")], capture_output=True, timeout=60
        )
        if watcher.poll() is None:
            watcher.kill()
        watcher.wait()


def test_existing_state_dir_is_never_reused(tmp_path: Path) -> None:
    """既存の state dir を使い、別の run の宣言・状態・最終結果を上書きする。"""
    state = tmp_path / "state"
    state.mkdir()
    (state / "keep").write_text("x", encoding="utf-8")
    argv = [sys.executable, str(SCRIPT), "run", "--state-dir", str(state), "--stop", "TERM", "--deadline-seconds", "5"]

    assert subprocess.run([*argv, "--", "true"], capture_output=True, timeout=30).returncode == 2
    assert [path.name for path in state.iterdir()] == ["keep"]
