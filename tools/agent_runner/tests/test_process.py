"""Real fake-CLI subprocesses; no agent runtime is invoked."""

import asyncio
import os
import signal
import socket
import sys
import threading
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import process
from tools.agent_runner.adapters import LaunchSpec
from tools.agent_runner.models import State
from tools.agent_runner.process import (
    ProcessResult,
    _command_outcome,  # pyright: ignore[reportPrivateUsage] - private control-channel boundary
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


# Shell interpolation, not closing stdin, or the wrong cwd/env loses the exact prompt.
def test_stdin_quotes_multiline_cwd_and_environment_are_literal(tmp_path: Path) -> None:
    prompt = b"'quoted' \"double\" $HOME; $(echo forbidden)\nsecond line\n"
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
    assert result.state is None
    assert result.exit_code == 0
    assert result.stop_confirmed
    assert result.elapsed_seconds > 0
    assert (tmp_path / "stdout").read_bytes() == prompt
    assert (tmp_path / "stderr").read_text() == "literal"


# Failing to start an independent session risks signaling the controller's process group.
def test_process_has_independent_session(tmp_path: Path) -> None:
    result = run(tmp_path, launch("import os; print(os.getpid(), os.getppid(), os.getpgrp(), os.getsid(0))"))
    pid, parent, group, session = map(int, (tmp_path / "stdout").read_text().split())
    assert parent == group == session
    assert pid != parent
    assert group != os.getpgrp()
    assert result.stop_confirmed


# Sequential or communicate()-style draining deadlocks or stores arbitrary output in memory.
def test_large_parallel_output_is_streamed_to_files(tmp_path: Path) -> None:
    code = """import os, threading
block = b'x' * 65536
def emit(fd):
    for _ in range(160):
        os.write(fd, block)
thread = threading.Thread(target=emit, args=(2,))
thread.start()
emit(1)
thread.join()
"""
    tracemalloc.start()
    try:
        result = run(tmp_path, launch(code), deadline=10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.state is None
    assert result.stop_confirmed
    assert (tmp_path / "stdout").stat().st_size == 160 * 65536
    assert (tmp_path / "stderr").stat().st_size == 160 * 65536
    # A communicate()-style implementation must retain at least one entire log.
    assert peak < (tmp_path / "stdout").stat().st_size // 2


# Redacting each received chunk independently leaks a secret split across pipe reads.
def test_secrets_split_across_reads_are_redacted_on_both_pipes(tmp_path: Path) -> None:
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
    result = run(tmp_path, launch(code, secret.encode()), secrets=(secret, "credential", ""))
    assert result.state is None
    for name in ("stdout", "stderr"):
        output = (tmp_path / name).read_bytes()
        assert secret.encode() not in output
        assert b"credential" not in output
        assert output == b"prefix|[REDACTED]|tail:unique-cred"


@pytest.mark.parametrize("code", ["raise SystemExit(7)", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"])
def test_abnormal_exit_returns_response_failed(tmp_path: Path, code: str) -> None:
    result = run(tmp_path, launch(code))
    assert result.state == "response_failed"
    assert result.exit_code in (7, -signal.SIGTERM)
    assert result.stop_confirmed


def test_launch_oserror_is_safe_result(tmp_path: Path) -> None:
    result = run(tmp_path, LaunchSpec((str(tmp_path / "SECRET-missing"), "SECRET-argv"), b"SECRET-prompt"))
    assert result.state == "launch_failed"
    assert result.exit_code is None
    assert result.stop_confirmed
    assert "SECRET" not in repr(result)
    assert (tmp_path / "stdout").read_bytes() == b""
    assert (tmp_path / "stderr").read_bytes() == b""


@pytest.mark.parametrize("frame", [b"", b"SECRET-invalid\n", b"exit bad\n", b"exit 0\nextra", b"x" * 64])
def test_invalid_control_outcome_is_safe_failure(frame: bytes) -> None:
    async def exercise() -> None:
        reader, writer = socket.socketpair()
        with reader, writer:
            reader.setblocking(False)
            writer.sendall(frame)
            writer.shutdown(socket.SHUT_WR)
            with pytest.raises(ValueError) as caught:
                await _command_outcome(reader)
            assert "SECRET" not in str(caught.value)

    asyncio.run(exercise())


def test_control_eof_never_uses_successful_helper_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_launch = asyncio.create_subprocess_exec

    async def helper_without_report(*_: str, **kwargs: Any) -> asyncio.subprocess.Process:
        return await real_launch(sys.executable, "-c", "pass", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", helper_without_report)
    result = run(tmp_path, launch("print('must not execute')"))
    assert result.state == "response_failed"
    assert result.exit_code is None
    assert result.stop_confirmed
    assert (tmp_path / "stdout").read_bytes() == b""


def test_buffered_original_exit_survives_anchor_reap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_outcome = _command_outcome
    real_stop = process._stop  # pyright: ignore[reportPrivateUsage] - deterministic cleanup boundary
    anchor_stopped = asyncio.Event()

    async def held_outcome(control: socket.socket) -> tuple[State | None, int | None]:
        await anchor_stopped.wait()
        return await real_outcome(control)

    async def release_after_stop(tree: _Tree, grace: float, *, preserve: int | None = None) -> None:
        await real_stop(tree, grace, preserve=preserve)
        if preserve is None:
            anchor_stopped.set()

    monkeypatch.setattr(process, "_command_outcome", held_outcome)
    monkeypatch.setattr(process, "_stop", release_after_stop)
    result = run(tmp_path, launch("raise SystemExit(7)"), deadline=0.2)
    assert result.state == "timed_out"
    assert result.exit_code == 7
    assert result.stop_confirmed


@pytest.mark.parametrize(
    "locale",
    [
        {},
        {"LANG": "C"},
        {"LANG": "POSIX"},
        {"LC_CTYPE": "C"},
        {"LC_ALL": "C"},
        {"LANG": "C.UTF-8"},
        {"LC_CTYPE": "C.UTF-8"},
        {"LC_ALL": "C.UTF-8"},
    ],
)
def test_supervisor_preserves_literal_environment(
    tmp_path: Path, supervisor_bootstrap: Callable[[str], None], locale: dict[str, str]
) -> None:
    env = {
        "PATH": os.defpath,
        "OVERRIDE": "original",
        "EMPTY": "",
        "TEXT": "日本語\nkey=value",
        "OPAQUE": os.fsdecode(b"\xff"),
        **locale,
    }
    additions = {"OVERRIDE": "adapter=日本語", "ADAPTER_ONLY": "line\nwith=value"}
    result = asyncio.run(
        execute(
            LaunchSpec(("/usr/bin/env", "-0"), b"", env=additions),
            cwd=tmp_path,
            env=env,
            deadline_seconds=5,
            stdout_path=tmp_path / "stdout",
            stderr_path=tmp_path / "stderr",
            cancel=threading.Event(),
        )
    )
    actual = {
        os.fsdecode(name): os.fsdecode(value)
        for entry in (tmp_path / "stdout").read_bytes().split(b"\0")
        if entry
        for name, value in [entry.split(b"=", 1)]
    }
    assert result.state is None and result.exit_code == 0 and result.stop_confirmed
    assert actual == {**env, **additions}


def test_supervisor_rejects_unreadable_original_environment(
    tmp_path: Path, supervisor_bootstrap: Callable[[str], None]
) -> None:
    supervisor_bootstrap(
        "\nimport io\n"
        "real_open = io.open\n"
        "def checked_open(path, *args, **kwargs):\n"
        "    if os.fspath(path) == '/proc/self/environ':\n"
        "        raise OSError('PRIVATE_ENV_ERROR')\n"
        "    return real_open(path, *args, **kwargs)\n"
        "io.open = checked_open\n"
    )
    marker = tmp_path / "started"
    result = run(tmp_path, LaunchSpec(("/usr/bin/touch", str(marker)), b""))
    assert result.state == "launch_failed" and result.stop_confirmed
    assert not marker.exists()
    assert (tmp_path / "stdout").read_bytes() == b""
    assert (tmp_path / "stderr").read_bytes() == b""


def test_supervisor_launch_paths_preserve_stdin_and_exit(
    tmp_path: Path, supervisor_bootstrap: Callable[[str], None]
) -> None:
    result = run(
        tmp_path, launch("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)", b"literal")
    )
    assert result.state == "response_failed" and result.exit_code == 7
    assert result.stop_confirmed
    assert (tmp_path / "stdout").read_bytes() == b"literal"


def test_command_does_not_inherit_private_control_socket(tmp_path: Path) -> None:
    code = """import os
for fd in os.listdir('/proc/self/fd'):
    try:
        target = os.readlink('/proc/self/fd/' + fd)
    except FileNotFoundError:
        continue
    if target.startswith('socket:'):
        print(target)
"""
    result = run(tmp_path, launch(code))
    assert result.state is None and result.stop_confirmed
    assert (tmp_path / "stdout").read_bytes() == b""


# A pre-set event must not spawn even a short-lived process with side effects.
def test_already_cancelled_does_not_spawn(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    result = asyncio.run(
        execute(
            launch("from pathlib import Path; Path('spawned').touch()"),
            cwd=tmp_path,
            env={"PATH": os.defpath, "OVERRIDE": "original"},
            deadline_seconds=5,
            stdout_path=tmp_path / "stdout",
            stderr_path=tmp_path / "stderr",
            cancel=cancel,
        )
    )
    assert result.state == "interrupted"
    assert result.exit_code is None
    assert result.stop_confirmed
    assert not (tmp_path / "spawned").exists()


def alive(pid: int) -> bool:
    try:
        # Zombies cannot execute or mutate retained workspaces.
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state not in (b"Z", b"X")


async def wait_for(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or not path.read_text():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("runtime", ["python", "shell"])
@pytest.mark.parametrize("closed_pipes", [False, True])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_immediate_exit_before_ownership_observation_stops_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, closed_pipes: bool, exit_code: int
) -> None:
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
    if runtime == "shell":
        redirects = " >/dev/null 2>&1" if closed_pipes else ""
        spec = LaunchSpec(
            ("/bin/sh", "-c", f"echo $$ > leader.pid; sleep 60{redirects} & echo $! > child.pid; exit {exit_code}"),
            b"",
        )
    else:
        pipes = ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if closed_pipes else ""
        spec = launch(
            "import os,subprocess,sys; open('leader.pid','w').write(str(os.getpid())); "
            f"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']{pipes}); "
            f"open('child.pid','w').write(str(child.pid)); sys.exit({exit_code})"
        )
    try:
        result = run(tmp_path, spec, deadline=3)
        assert result.stop_confirmed
        assert result.exit_code == exit_code
        assert result.state == (None if exit_code == 0 else "response_failed")
        assert result.elapsed_seconds < 2
        assert all(not alive(child.pid) for child in children)
    finally:
        for child in children:
            current = _read_proc(child.pid)
            if current is not None and current.starttime == child.starttime and alive(child.pid):
                os.kill(child.pid, signal.SIGKILL)


# A normal leader exit is not completion when live children still hold the log pipes.
@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.parametrize("leader_exit", [0, 9])
def test_leader_exit_stops_observed_descendants(tmp_path: Path, escaped: bool, leader_exit: int) -> None:
    code = f"""import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-u', '-c',
    'import os,signal,time; ' + ('os.setsid(); ' if {escaped!r} else '') +
    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'open("child.pid", "w").write(str(os.getpid())); time.sleep(60)'])
while not os.path.exists('child.pid'):
    time.sleep(0.01)
# Give observation multiple scheduling opportunities before the leader exits.
time.sleep(0.3)
sys.exit({leader_exit})
"""
    result = run(tmp_path, launch(code), deadline=3)
    pid = int((tmp_path / "child.pid").read_text())
    try:
        assert result.state == (None if leader_exit == 0 else "response_failed")
        assert result.exit_code == leader_exit
        assert result.stop_confirmed
        assert not alive(pid)
        assert result.elapsed_seconds < 3
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


# Deadlines must cover unread stdin and output drains, not only proc.wait().
@pytest.mark.parametrize("blocked_stdin", [False, True])
def test_deadline_kills_term_resistant_leader(tmp_path: Path, blocked_stdin: bool) -> None:
    spec = launch(
        "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "open('leader.pid','w').write(str(os.getpid())); time.sleep(60)",
        b"p" * 2_000_000 if blocked_stdin else b"",
    )
    result = run(tmp_path, spec, deadline=0.4)
    pid = int((tmp_path / "leader.pid").read_text())
    try:
        assert result.state == "timed_out"
        assert result.stop_confirmed
        assert result.exit_code == -signal.SIGKILL
        assert not alive(pid)
        assert result.elapsed_seconds < 2
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_thread_event_cancels_running_process(tmp_path: Path) -> None:
    async def exercise() -> ProcessResult:
        cancel = threading.Event()
        task = asyncio.create_task(
            execute(
                launch("import os,time; open('leader.pid','w').write(str(os.getpid())); time.sleep(60)"),
                cwd=tmp_path,
                env={"PATH": os.defpath, "OVERRIDE": "original"},
                deadline_seconds=10,
                stdout_path=tmp_path / "stdout",
                stderr_path=tmp_path / "stderr",
                cancel=cancel,
            )
        )
        await wait_for(tmp_path / "leader.pid")
        cancel.set()
        return await asyncio.wait_for(task, 3)

    result = asyncio.run(exercise())
    pid = int((tmp_path / "leader.pid").read_text())
    try:
        assert result.state == "interrupted"
        assert result.stop_confirmed
        assert not alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


# Cancellation of the coroutine must not skip the finally-based process teardown.
def test_coroutine_cancellation_stops_leader_and_escaped_descendant(tmp_path: Path) -> None:
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


# Stop confirmation is an independent safety signal, never inferred from exit=0.
def test_unconfirmed_stop_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unconfirmed(*_: object) -> bool:
        return False

    monkeypatch.setattr(process, "_stop_confirmed", unconfirmed)
    result = run(tmp_path, launch("print('finished')"))
    assert result.state is None
    assert result.exit_code == 0
    assert not result.stop_confirmed


# Cancellation while create_subprocess_exec is completing must never lose its PID.
def test_repeated_cancellation_during_launch_still_stops_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


# Adapter launch mistakes must return a safe failure, not raw exception diagnostics.
@pytest.mark.parametrize("argv", [(), ("",), ("bad\x00SECRET",)])
def test_invalid_launch_arguments_return_safe_failure(tmp_path: Path, argv: tuple[str, ...]) -> None:
    result = run(tmp_path, LaunchSpec(argv, b"SECRET-prompt", {"SECRET_ENV": "SECRET-value"}))
    assert result.state == "launch_failed"
    assert result.exit_code is None
    assert result.stop_confirmed
    assert "SECRET" not in repr(result)


# /proc comm names are arbitrary bytes; decoding the whole stat record hides live children.
def test_non_utf8_descendant_name_does_not_escape_tracking(tmp_path: Path) -> None:
    child_code = (
        "import ctypes,os,time; os.setsid(); "
        "ctypes.CDLL(None).prctl(15, b'\\xff ) child', 0, 0, 0); "
        "open('child.pid','w').write(str(os.getpid())); time.sleep(60)"
    )
    code = (
        f"import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child_code!r}]); time.sleep(0.3)"
    )
    result = run(tmp_path, launch(code), deadline=1)
    pid = int((tmp_path / "child.pid").read_text())
    try:
        assert result.state is None
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


# Numeric PID/PGID matches alone must never adopt or signal a replacement process.
def test_reused_leader_group_is_not_signaled(monkeypatch: pytest.MonkeyPatch) -> None:
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


# Even in the genuine group, a recorded descendant's replacement is not ours.
def test_reused_descendant_identity_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
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


# A surviving observed group member anchors new orphans after the leader exits;
# without any identity anchor, stop must stay unconfirmed instead of killing them.
@pytest.mark.parametrize("observed_survivor", [False, True])
def test_group_after_leader_exit_requires_identity_anchor(
    monkeypatch: pytest.MonkeyPatch,
    observed_survivor: bool,
) -> None:
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
