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


def call(*args: str, cwd: Path | None = None) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60, cwd=cwd
    )
    return completed.returncode, json.loads(completed.stdout)


def wait_for(path: Path, timeout: float = 10.0) -> None:
    until = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < until, f"{path} did not appear"
        time.sleep(0.05)


def test_progressing_worker_completes_without_stop(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path,
        "progress",
        "--deadline-seconds",
        "20",
        "--no-progress-seconds",
        "2",
        "--result-file",
        str(tmp_path / "result.json"),
        "--stop",
        "TERM",
    )

    assert (code, final["decision"], final["reason"]) == (0, "completed", "")
    assert final["stop"] is None and final["substantive_items"] == 5
    assert final["target"]["exit_code"] == 0


def test_heartbeat_only_worker_is_stopped_for_no_progress(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path, "heartbeat", "--deadline-seconds", "20", "--no-progress-seconds", "0.6", "--stop", "TERM"
    )

    assert (code, final["decision"], final["reason"]) == (1, "stopped", "no_progress")
    assert final["stop"]["confirmed"] is True and final["stop"]["signal"] == "SIGTERM"
    assert final["substantive_items"] == 1 and final["heartbeats"] > 2
    assert final["elapsed_seconds"] < 10
    assert not alive(final["target"]["pid"])


def test_repeated_failure_stops_worker(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path, "failure", "--deadline-seconds", "20", "--max-repeated-failures", "2", "--stop", "TERM"
    )

    assert (code, final["decision"], final["reason"]) == (1, "stopped", "repeated_failure")
    assert final["failures"]["same-timeout"]["failure_class"] == "reliability"
    assert final["failures"]["same-timeout"]["count"] >= 2


def test_safety_failure_stops_at_first_record(tmp_path: Path) -> None:
    code, final = run_watch(tmp_path, "safety", "--deadline-seconds", "20", "--stop", "TERM")

    assert (code, final["decision"], final["reason"]) == (1, "stopped", "safety")
    assert final["failures"] == {"unknown-send": {"failure_class": "safety", "count": 1}}


def test_deadline_stops_even_while_progressing(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path, "endless", "--deadline-seconds", "2", "--no-progress-seconds", "1", "--stop", "INT"
    )

    assert (code, final["decision"], final["reason"]) == (1, "stopped", "deadline")
    assert final["stop"]["signal"] == "SIGINT" and final["stop"]["confirmed"] is True
    assert final["elapsed_seconds"] >= 2 and final["substantive_items"] > 3
    assert final["deadline_exceeded"] is True


def test_unstoppable_worker_is_held_and_left_running(tmp_path: Path) -> None:
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


def test_escalate_kill_confirms_stop_of_term_ignoring_worker(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path,
        "ignore-term",
        "--deadline-seconds",
        "1",
        "--stop",
        "TERM",
        "--stop-grace-seconds",
        "0.5",
        "--escalate-kill",
    )

    assert (code, final["decision"], final["reason"]) == (1, "stopped", "deadline")
    assert final["stop"]["confirmed"] is True and final["residual_pids"] == []
    assert not alive(final["target"]["pid"])


def test_undeclared_stop_never_signals(tmp_path: Path) -> None:
    code, final = run_watch(tmp_path, "undeclared", "--deadline-seconds", "0.3", "--stop", "none")

    assert (code, final["decision"]) == (0, "completed")
    assert final["deadline_exceeded"] is True and final["stop"] is None
    assert not (tmp_path / "got-term").exists()
    events = [json.loads(line) for line in (tmp_path / "state" / "events.jsonl").read_text().splitlines()]
    assert any(event["decision"] == "hold" and event["reason"] == "deadline" for event in events)


def test_partial_result_survives_stop_for_investigation_in_reserve(tmp_path: Path) -> None:
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


def test_finalize_aware_worker_saves_and_completes(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path,
        "finalize-aware",
        "--deadline-seconds",
        "10",
        "--reserve-seconds",
        "9.5",
        "--result-file",
        str(tmp_path / "result.json"),
        "--stop",
        "TERM",
    )

    assert (code, final["decision"], final["reason"]) == (0, "completed", "")
    assert final["finalize_requested"] is True and final["stop"] is None
    assert final["partial_results"] == [str(tmp_path / "result.json")]


def test_item_written_right_after_finalize_request_is_within_grace(tmp_path: Path) -> None:
    code, final = run_watch(
        tmp_path,
        "finalize-late",
        "--deadline-seconds",
        "10",
        "--reserve-seconds",
        "9.5",
        "--result-file",
        str(tmp_path / "result.json"),
        "--stop",
        "TERM",
    )

    assert (code, final["decision"], final["reason"]) == (0, "completed", "")
    assert final["finalize_requested"] is True


def test_safety_failure_then_exit_is_not_completed(tmp_path: Path) -> None:
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


def test_operator_stop_confirms_and_reports_operator(tmp_path: Path) -> None:
    argv = watch_argv(tmp_path, "endless", "--deadline-seconds", "60", "--stop", "TERM")
    argv[argv.index("--poll-seconds") + 1] = "5"
    watcher = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(tmp_path / "state" / "status.json")
        started = time.monotonic()

        code, final = call("stop", "--state-dir", str(tmp_path / "state"))

        assert time.monotonic() - started < 4
        assert (code, final["decision"], final["reason"]) == (1, "stopped", "operator")
        assert final["stop"]["confirmed"] is True
        assert watcher.wait(timeout=10) == 1
    finally:
        if watcher.poll() is None:
            watcher.kill()
            watcher.wait()


def test_relative_state_dir_round_trip(tmp_path: Path) -> None:
    argv = watch_argv(tmp_path, "endless", "--deadline-seconds", "60", "--stop", "TERM")
    argv[argv.index("--state-dir") + 1] = "state"
    watcher = subprocess.Popen(argv, cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(tmp_path / "state" / "status.json")

        code, status = call("status", "--state-dir", "state", cwd=tmp_path)
        assert (code, status["source"], status["watcher_alive"], status["target_alive"]) == (0, "status", True, True)
        assert status["decision"] == "continue"

        code, final = call("stop", "--state-dir", "state", cwd=tmp_path)
        assert (code, final["reason"]) == (1, "operator")
        assert final["paths"]["state_dir"] == str(tmp_path / "state")
    finally:
        if watcher.poll() is None:
            watcher.kill()
        watcher.wait()


def test_state_dir_with_spaces_round_trip(tmp_path: Path) -> None:
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

        code, final = call("stop", "--state-dir", str(base / "state"))
        assert (code, final["reason"]) == (1, "operator")
        assert final["paths"]["state_dir"] == str(base / "state")
    finally:
        if watcher.poll() is None:
            watcher.kill()
        watcher.wait()


def test_status_of_finished_run_reports_final_and_dead_processes(tmp_path: Path) -> None:
    run_watch(tmp_path, "progress", "--deadline-seconds", "20", "--stop", "TERM")

    code, status = call("status", "--state-dir", str(tmp_path / "state"))

    assert (code, status["source"], status["decision"]) == (0, "final", "completed")
    assert (status["watcher_alive"], status["target_alive"]) == (False, False)


def test_launch_failure_is_reported_as_incomplete(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--state-dir",
            str(tmp_path / "state"),
            "--deadline-seconds",
            "5",
            "--stop",
            "TERM",
            "--",
            str(tmp_path / "missing-command"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    final = json.loads((tmp_path / "state" / "final.json").read_text(encoding="utf-8"))

    assert (completed.returncode, final["decision"], final["reason"]) == (1, "exited_incomplete", "launch_failed")
    assert final["target"]["pid"] is None


def test_invalid_arguments_exit_2_without_creating_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    base = [sys.executable, str(SCRIPT), "run", "--state-dir", str(state), "--stop", "TERM"]
    invalid = [
        [*base, "--deadline-seconds", "5", "--reserve-seconds", "5", "--", "true"],
        [*base, "--deadline-seconds", "5", "--no-progress-seconds", "1", "--", "true"],
        [*base, "--deadline-seconds", "5", "--stop-grace-seconds", "31", "--", "true"],
        [*base, "--deadline-seconds", "5", "--"],
        [*base, "--deadline-seconds", "5"],
    ]
    for argv in invalid:
        assert subprocess.run(argv, capture_output=True, timeout=30).returncode == 2, argv
        assert not state.exists()

    state.mkdir()
    (state / "keep").write_text("x", encoding="utf-8")
    assert subprocess.run([*base, "--deadline-seconds", "5", "--", "true"], capture_output=True).returncode == 2
    assert [path.name for path in state.iterdir()] == ["keep"]
