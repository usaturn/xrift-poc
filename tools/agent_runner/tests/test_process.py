"""Real fake-CLI subprocesses; no agent runtime is invoked."""

import asyncio
import os
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import process
from tools.agent_runner.adapters import LaunchSpec
from tools.agent_runner.process import (
    ProcessResult,
    _Proc,  # pyright: ignore[reportPrivateUsage] - deterministic process-identity regression
    _read_proc,  # pyright: ignore[reportPrivateUsage] - identity-safe regression cleanup
    _stop_confirmed,  # pyright: ignore[reportPrivateUsage]
    _Tree,  # pyright: ignore[reportPrivateUsage]
    execute,
)


def launch(code: str, stdin: bytes = b"") -> LaunchSpec:
    return LaunchSpec((sys.executable, "-u", "-c", code), stdin)


def run(tmp_path: Path, spec: LaunchSpec, *, deadline: float = 5, secrets: tuple[str, ...] = ()) -> ProcessResult:
    return asyncio.run(
        execute(
            spec,
            cwd=tmp_path,
            env={"PATH": os.defpath, "OVERRIDE": "original"},
            deadline_seconds=deadline,
            stdout_path=tmp_path / "stdout",
            stderr_path=tmp_path / "stderr",
            cancel=threading.Event(),
            secrets=secrets,
            stop_grace_seconds=0.1,
        )
    )


def test_stdin_quotes_multiline_cwd_and_environment_are_literal(tmp_path: Path) -> None:
    """依頼文（stdin）が途中で欠ける・シェルに解釈されると、担当は書き換わった依頼で黙って作業する。"""
    # 1 回の書き込み（64 KiB）を超える依頼文で、分けて送る部分の欠落も検出する
    prompt = b"'quoted' \"double\" $HOME; $(echo forbidden)\nsecond line\n" + b"x" * 70_000 + b"\nlast line\n"
    spec = launch(
        "import os,sys; assert os.getcwd() == os.environ['EXPECTED_CWD']; "
        "sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write(os.environ['EXTRA'])",
        prompt,
    )
    result = asyncio.run(
        execute(
            spec,
            cwd=tmp_path,
            env={"EXPECTED_CWD": str(tmp_path), "EXTRA": "literal"},
            deadline_seconds=5,
            stdout_path=tmp_path / "stdout",
            stderr_path=tmp_path / "stderr",
            cancel=threading.Event(),
        )
    )
    assert result.exit_code == 0
    assert (tmp_path / "stdout").read_bytes() == prompt
    assert (tmp_path / "stderr").read_text() == "literal"


def test_secrets_split_across_reads_are_redacted_on_both_pipes(tmp_path: Path) -> None:
    """pipe の読み取りごとに伏せると、読み取りの境界をまたぐ秘密が stdout・stderr のログに黙って残る。"""
    secret = "unique-credential-é-12345"
    code = """import os, sys, time
secret = sys.stdin.buffer.read()
for fd in (1, 2):
    os.write(fd, b'prefix|')
    for byte in secret:
        os.write(fd, bytes([byte]))
        time.sleep(0.01)
    os.write(fd, b'|tail:unique-cred')
"""
    run(tmp_path, launch(code, secret.encode()), secrets=(secret, "credential", ""))
    for name in ("stdout", "stderr"):
        output = (tmp_path / name).read_bytes()
        assert secret.encode() not in output
        assert b"credential" not in output
        # 秘密の前後と、秘密に似ているだけの末尾は残る
        assert output.startswith(b"prefix|") and output.endswith(b"|tail:unique-cred")


def test_signal_death_is_never_reported_as_success(tmp_path: Path) -> None:
    """シグナルで死んだ CLI・後処理を終了 0 と報告すると、runner と loop が成功として黙って扱う。"""
    result = run(tmp_path, launch("import os,signal; os.kill(os.getpid(),signal.SIGTERM)"))
    assert (result.state, result.exit_code) != (None, 0)


def test_control_eof_never_uses_successful_helper_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """監督プロセスが報告せずに終了 0 したとき、実行していないコマンド（後処理）を成功と黙って報告する。"""
    real_launch = asyncio.create_subprocess_exec

    async def helper_without_report(*_: str, **kwargs: Any) -> asyncio.subprocess.Process:
        return await real_launch(sys.executable, "-c", "pass", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", helper_without_report)
    result = run(tmp_path, launch("print('must not execute')"))
    assert (result.state, result.exit_code) != (None, 0)
    assert (tmp_path / "stdout").read_bytes() == b""


def alive(pid: int) -> bool:
    try:
        # Zombies cannot execute or mutate retained workspaces.
        # A pid reaped between open and read fails with ESRCH instead.
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
    except FileNotFoundError, ProcessLookupError:
        return False
    return state not in (b"Z", b"X")


async def wait_for(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or not path.read_text():
            await asyncio.sleep(0.01)


def test_immediate_exit_before_ownership_observation_stops_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """すぐ終わった CLI の子（ログの pipe を持たない）を止めずに、停止を確かめたと黙って報告する。"""
    real_launch = asyncio.create_subprocess_exec
    children: list[_Proc] = []

    async def after_original_exit(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_launch(*args, **kwargs)
        await wait_for(tmp_path / "leader.pid")
        leader = int((tmp_path / "leader.pid").read_text())
        # Hold delivery, not the command: the original CLI must be reaped before
        # execute constructs its ownership tree. The supervisor must still live.
        async with asyncio.timeout(2):
            while _read_proc(leader) is not None:
                await asyncio.sleep(0.001)
        child = _read_proc(int((tmp_path / "child.pid").read_text()))
        assert child is not None
        children.append(child)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", after_original_exit)
    spec = launch(
        "import os,subprocess,sys; open('leader.pid','w').write(str(os.getpid())); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "open('child.pid','w').write(str(child.pid)); sys.exit(0)"
    )
    try:
        result = run(tmp_path, spec, deadline=3)
        assert result.stop_confirmed
        assert all(not alive(child.pid) for child in children)
    finally:
        for child in children:
            current = _read_proc(child.pid)
            if current is not None and current.starttime == child.starttime and alive(child.pid):
                os.kill(child.pid, signal.SIGKILL)


def test_coroutine_cancellation_stops_leader_and_escaped_descendant(tmp_path: Path) -> None:
    """task の取り消しで finally の停止が飛ばされると、CLI と逃げた子が run の後も黙って動き続ける。"""
    code = """import os, subprocess, sys, time
open('leader.pid','w').write(str(os.getpid()))
subprocess.Popen([sys.executable, '-u', '-c',
    'import os,time; os.setsid(); open("child.pid","w").write(str(os.getpid())); time.sleep(60)'])
time.sleep(60)
"""

    async def exercise() -> None:
        task = asyncio.create_task(
            execute(
                launch(code),
                cwd=tmp_path,
                env={"PATH": os.defpath, "OVERRIDE": "original"},
                deadline_seconds=10,
                stdout_path=tmp_path / "stdout",
                stderr_path=tmp_path / "stderr",
                cancel=threading.Event(),
            )
        )
        await wait_for(tmp_path / "child.pid")
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)

    asyncio.run(exercise())
    pids = [int((tmp_path / name).read_text()) for name in ("leader.pid", "child.pid")]
    try:
        assert all(not alive(pid) for pid in pids)
    finally:
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_unconfirmed_stop_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """終了 0 から停止を推すと、runner が生きている担当のファイルを読み書きし、fallback を黙って始める。"""

    def unconfirmed(*_: object) -> bool:
        return False

    monkeypatch.setattr(process, "_stop_confirmed", unconfirmed)
    result = run(tmp_path, launch("print('finished')"))
    assert result.state is None
    assert result.exit_code == 0
    assert not result.stop_confirmed


def test_repeated_cancellation_during_launch_still_stops_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動の途中の取り消しで PID を失うと、起動済みの CLI を止めずに黙って残す。"""
    real_launch = asyncio.create_subprocess_exec
    delivery = asyncio.Event()

    async def delayed_launch(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_launch(*args, **kwargs)
        # Emulate a launch boundary that has created the OS process but has not
        # yet delivered it to execute. Preserve every real subprocess side effect.
        await delivery.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_launch)

    async def exercise() -> None:
        task = asyncio.create_task(
            execute(
                launch("import os,time; open('leader.pid','w').write(str(os.getpid())); time.sleep(60)"),
                cwd=tmp_path,
                env={"PATH": os.defpath, "OVERRIDE": "original"},
                deadline_seconds=10,
                stdout_path=tmp_path / "stdout",
                stderr_path=tmp_path / "stderr",
                cancel=threading.Event(),
            )
        )
        await wait_for(tmp_path / "leader.pid")
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        delivery.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)

    asyncio.run(exercise())
    pid = int((tmp_path / "leader.pid").read_text())
    try:
        assert not alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_non_utf8_descendant_name_does_not_escape_tracking(tmp_path: Path) -> None:
    """/proc の comm（任意のバイト列）の解析を誤ると子孫が見えなくなり、止めずに停止を確かめたと黙って報告する。"""
    child_code = (
        "import ctypes,os,time; os.setsid(); "
        "ctypes.CDLL(None).prctl(15, b'\\xff ) child', 0, 0, 0); "
        "open('child.pid','w').write(str(os.getpid())); time.sleep(60)"
    )
    code = (
        f"import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child_code!r}]); time.sleep(0.3)"
    )
    # 期限は性質に関係しない。子を見失うと、子が持つログの pipe の drain がこの期限まで終わらない
    result = run(tmp_path, launch(code), deadline=5)
    pid = int((tmp_path / "child.pid").read_text())
    try:
        assert result.stop_confirmed
        assert not alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def proc_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[int, _Proc],
) -> list[tuple[str, int]]:
    """Model /proc identities and intercept signals; never signal real victims."""
    signals: list[tuple[str, int]] = []

    def read(pid: int) -> _Proc | None:
        return snapshot.get(pid)

    def entries(_: Path) -> Iterator[Path]:
        return iter(Path(f"/proc/{pid}") for pid in snapshot)

    def kill(pid: int, _: int) -> None:
        signals.append(("pid", pid))

    def killpg(pid: int, _: int) -> None:
        signals.append(("group", pid))

    monkeypatch.setattr(process, "_read_proc", read)
    monkeypatch.setattr(Path, "iterdir", entries)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return signals


def test_reused_leader_group_is_not_signaled(monkeypatch: pytest.MonkeyPatch) -> None:
    """数値の PID・PGID だけで照合すると、PID が再利用された無関係なプロセスへ TERM・KILL を黙って送る。"""
    snapshot = {100: _Proc(100, 10, 1, 100, "S")}
    signals = proc_snapshot(monkeypatch, snapshot)
    tree = _Tree(100)
    snapshot.clear()
    snapshot.update(
        {
            100: _Proc(100, 20, 1, 100, "S"),
            101: _Proc(101, 21, 100, 100, "S"),
        }
    )
    tree.signal(signal.SIGTERM)
    assert signals == []
    assert tree.observed[100] == 10
    assert not _stop_confirmed(tree)


def test_reused_descendant_identity_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """子孫の starttime を照合し直さないと、再利用された PID の無関係なプロセスを自分の子として黙って止める。"""
    snapshot = {
        100: _Proc(100, 10, 1, 100, "S"),
        101: _Proc(101, 11, 100, 100, "S"),
    }
    signals = proc_snapshot(monkeypatch, snapshot)
    tree = _Tree(100)
    tree.live()
    snapshot[101] = _Proc(101, 30, 100, 100, "S")
    snapshot[102] = _Proc(102, 31, 101, 999, "S")
    tree.signal(signal.SIGTERM)
    assert signals == [("pid", 100)]
    assert tree.observed[101] == 11
    assert 102 not in tree.observed
    snapshot[100] = _Proc(100, 10, 1, 100, "Z")
    assert not _stop_confirmed(tree)


@pytest.mark.parametrize("observed_survivor", [False, True])
def test_group_after_leader_exit_requires_identity_anchor(
    monkeypatch: pytest.MonkeyPatch,
    observed_survivor: bool,
) -> None:
    """anchor の無い PGID だけで無関係なプロセスを止める（False）、生き残りがいるのに新しい孤児を見逃す（True）。"""
    snapshot = {
        100: _Proc(100, 10, 1, 100, "S"),
        101: _Proc(101, 11, 100, 100, "S"),
    }
    proc_snapshot(monkeypatch, snapshot)
    tree = _Tree(100)
    if observed_survivor:
        tree.live()
    snapshot.clear()
    if observed_survivor:
        snapshot[101] = _Proc(101, 11, 1, 100, "S")
    snapshot[102] = _Proc(102, 12, 1, 100, "S")
    assert {entry.pid for entry in tree.live()} == ({101, 102} if observed_survivor else set())
    assert not _stop_confirmed(tree)
    snapshot.clear()
    assert _stop_confirmed(tree) is observed_survivor
