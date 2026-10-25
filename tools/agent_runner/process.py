"""Linux process-group execution with bounded streaming and observed descendants.

This is not a sandbox: a malicious detached double-fork that escapes observation
cannot be contained without a cgroup. PID/starttime checks protect observed PIDs
against reuse; zombies are stopped for purposes of workspace retention.
"""

import asyncio
import os
import re
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from tools.agent_runner.adapters import LaunchSpec
from tools.agent_runner.models import State


@dataclass(frozen=True, slots=True)
class ProcessResult:
    state: State | None
    exit_code: int | None
    stop_confirmed: bool
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class _Proc:
    pid: int
    starttime: int
    parent: int
    group: int
    state: str


def _read_proc(pid: int) -> _Proc | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        return _Proc(pid, int(fields[19]), int(fields[1]), int(fields[2]), fields[0].decode("ascii"))
    except OSError, ValueError, IndexError:
        return None


class _Tree:
    def __init__(self, pid: int) -> None:
        self.group = pid
        self.observed: dict[int, int] = {}
        leader = _read_proc(pid)
        self.leader_starttime = leader.starttime if leader is not None else None
        self.group_members: set[int] = set()
        self.uncertain = False
        if leader is not None:
            self.observed[pid] = leader.starttime
            if leader.group == self.group:
                self.group_members.add(pid)

    def live(self) -> list[_Proc]:
        snapshot: dict[int, _Proc] = {}
        for path in Path("/proc").iterdir():
            if path.name.isdecimal():
                entry = _read_proc(int(path.name))
                if entry is not None:
                    snapshot[entry.pid] = entry
        # A numeric PGID is not an identity: it may belong to a replacement
        # leader. Only original identities can anchor discovery of group peers.
        parents = {pid for pid, start in self.observed.items() if pid in snapshot and snapshot[pid].starttime == start}
        leader = snapshot.get(self.group)
        replacement = leader is not None and leader.starttime != self.leader_starttime
        anchored = not replacement and any(
            pid in parents and snapshot[pid].group == self.group for pid in self.group_members
        )
        group = {entry.pid for entry in snapshot.values() if entry.group == self.group}
        if any(
            snapshot[pid].state not in ("Z", "X")
            and (not anchored or self.observed.get(pid, snapshot[pid].starttime) != snapshot[pid].starttime)
            for pid in group
        ):
            # Ownership was lost. Retain this uncertainty even if the unknown
            # processes subsequently disappear; never claim a proven teardown.
            self.uncertain = True
        if anchored:
            members = {
                pid for pid in group if self.observed.get(pid, snapshot[pid].starttime) == snapshot[pid].starttime
            }
            parents.update(members)
            self.group_members.update(members)
        while True:
            descendants = {
                entry.pid
                for entry in snapshot.values()
                if entry.parent in parents and self.observed.get(entry.pid, entry.starttime) == entry.starttime
            }
            if descendants <= parents:
                break
            parents.update(descendants)
        for pid in parents:
            self.observed.setdefault(pid, snapshot[pid].starttime)
        return [
            entry
            for pid, start in self.observed.items()
            if (entry := snapshot.get(pid)) is not None and entry.starttime == start and entry.state not in ("Z", "X")
        ]

    def signal(self, sig: signal.Signals, *, preserve: int | None = None) -> None:
        live = self.live()
        # Never broadcast to a numeric group: even a currently genuine group
        # could contain a PID whose recorded identity was replaced. Signal only
        # individually observed identities, checking again immediately beforehand.
        for entry in live:
            if entry.pid == preserve:
                continue
            # Recheck immediately before signaling an individually tracked PID.
            current = _read_proc(entry.pid)
            if current is not None and current.starttime == entry.starttime and current.state not in ("Z", "X"):
                try:
                    os.kill(entry.pid, sig)
                except OSError:
                    pass


def _stop_confirmed(tree: _Tree) -> bool:
    """Independent stop evidence; intentionally replaceable by safety tests."""
    return not tree.live() and not tree.uncertain


async def _stop(tree: _Tree, grace: float, *, preserve: int | None = None) -> None:
    def stopped() -> bool:
        if preserve is None:
            return _stop_confirmed(tree)
        return not any(entry.pid != preserve for entry in tree.live())

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if stopped():
            return
        tree.signal(sig, preserve=preserve)
        until = time.monotonic() + max(0, grace)
        while time.monotonic() < until:
            if stopped():
                return
            await asyncio.sleep(0.01)


async def _command_outcome(control: socket.socket) -> tuple[State | None, int | None]:
    """Read one bounded original-command status, separately from CLI output."""
    message = bytearray()
    while not message.endswith(b"\n"):
        chunk = await asyncio.get_running_loop().sock_recv(control, 64 - len(message))
        if not chunk:
            raise ValueError("supervisor outcome unavailable")
        message.extend(chunk)
        if len(message) >= 64:
            raise ValueError("invalid supervisor outcome")
    frame = bytes(message)
    if frame == b"launch_failed\n":
        return "launch_failed", None
    matched = re.fullmatch(rb"exit (-?\d+)\n", frame)
    if matched is None:
        raise ValueError("invalid supervisor outcome")
    exit_code = int(matched[1])
    return (None if exit_code == 0 else "response_failed"), exit_code


class _Redactor:
    def __init__(self, secrets: tuple[str, ...]) -> None:
        values = sorted({value.encode() for value in secrets if value}, key=len, reverse=True)
        self.pattern = re.compile(b"|".join(re.escape(value) for value in values)) if values else None
        self.keep = max((len(value) for value in values), default=1) - 1
        self.pending = b""

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        data = self.pending + chunk
        cutoff = len(data) if final else max(0, len(data) - self.keep)
        if self.pattern is None:
            self.pending = b""
            return data
        output = bytearray()
        position = 0
        for match in self.pattern.finditer(data):
            if match.start() >= cutoff:
                break
            output.extend(data[position : match.start()])
            output.extend(b"[REDACTED]")
            position = match.end()
        consumed = max(position, cutoff)
        output.extend(data[position:consumed])
        self.pending = data[consumed:]
        return bytes(output)


def redact_file(path: Path, secrets: tuple[str, ...]) -> bool:
    """Replace an owned, stopped regular artifact with a streaming redacted copy.

    Never follow links or block opening special files. False means the original
    may still contain credentials and must be reported, not silently archived.
    """
    temporary: Path | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                return False
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix="redacted-", delete=False) as output:
                temporary = Path(output.name)
                redactor = _Redactor(secrets)
                while chunk := source.read(65536):
                    output.write(redactor.feed(chunk))
                output.write(redactor.feed(b"", final=True))
                output.flush()
                os.fsync(output.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            return False
        os.replace(temporary, path)
        temporary = None
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


async def _drain(reader: asyncio.StreamReader, output: BinaryIO, secrets: tuple[str, ...]) -> None:
    redactor = _Redactor(secrets)
    try:
        while chunk := await reader.read(65536):
            output.write(redactor.feed(chunk))
            output.flush()
    finally:
        output.write(redactor.feed(b"", final=True))
        output.flush()


async def _input(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        # Don't enqueue an arbitrarily large prompt in the transport in one go.
        for offset in range(0, len(data), 65536):
            writer.write(data[offset : offset + 65536])
            await writer.drain()
    except BrokenPipeError, ConnectionResetError:
        pass
    finally:
        writer.close()


async def _finish(
    proc: asyncio.subprocess.Process,
    tree: _Tree,
    tasks: list[asyncio.Task[None]],
    grace: float,
    control: socket.socket,
    outcome: asyncio.Task[tuple[State | None, int | None]] | None,
) -> None:
    try:
        # Preserve a live identity while finding/stopping ordinary group peers,
        # even if the original command has already exited and been reaped.
        await _stop(tree, grace, preserve=proc.pid)
        try:
            control.send(b"x")
        except OSError:
            pass
        try:
            # Release should reap a cooperative anchor without waiting for the
            # next /proc polling interval. Bound this if inherited pipes linger.
            await asyncio.wait_for(proc.wait(), max(0.05, grace))
        except TimeoutError:
            pass
        await _stop(tree, grace)
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        if outcome is not None:
            if not outcome.done():
                # A reaped supervisor may have a buffered command status that
                # the event loop has not delivered yet. Do not discard it.
                try:
                    await asyncio.wait_for(asyncio.shield(outcome), max(0.05, grace))
                except TimeoutError, OSError, ValueError:
                    pass
            if not outcome.done():
                outcome.cancel()
            await asyncio.gather(outcome, return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)
        control.close()
        # asyncio's public Process API cannot close inherited pipes. Closing the
        # underlying transport is necessary if an unobserved descendant holds
        # them open; never await proc.wait() indefinitely in that case.
        transport = cast(
            asyncio.SubprocessTransport,
            getattr(proc, "_transport"),  # noqa: B009 - Process has no public pipe-close API
        )
        transport.close()
        try:
            await asyncio.wait_for(proc.wait(), max(0.05, grace))
        except TimeoutError:
            pass


async def _shield_finish(task: asyncio.Task[None]) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def execute(
    spec: LaunchSpec,
    *,
    cwd: Path,
    env: dict[str, str],
    deadline_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
    cancel: threading.Event,
    secrets: tuple[str, ...] = (),
    stop_grace_seconds: float = 0.3,
) -> ProcessResult:
    """Execute argv (never a shell), returning only safe status/stop evidence.

    The deadline includes stdin and pipe drains. Termination and bounded cleanup
    may extend elapsed time beyond that deadline. Logs contain redacted bytes
    only; retained artifacts are managed by the caller.
    """
    started = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    tree: _Tree | None = None
    tasks: list[asyncio.Task[None]] = []
    outcome: asyncio.Task[tuple[State | None, int | None]] | None = None
    exit_code: int | None = None
    state: State | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            if cancel.is_set():
                return ProcessResult("interrupted", None, True, time.monotonic() - started)
            control, inherited = socket.socketpair()
            control.setblocking(False)
            try:
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-S",
                        str(Path(__file__).with_name("_supervisor.py")),
                        str(inherited.fileno()),
                        *spec.argv,
                        cwd=cwd,
                        env=env,
                        start_new_session=True,
                        pass_fds=(inherited.fileno(),),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                )
                # Keep shielding even repeated cancellation until the launched
                # process identity is available for finally-based teardown.
                launch_cancelled = False
                while not spawning.done():
                    try:
                        await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        launch_cancelled = True
                proc = spawning.result()
                inherited.close()
                tree = _Tree(proc.pid)
                outcome = asyncio.create_task(_command_outcome(control))
                if launch_cancelled:
                    raise asyncio.CancelledError
                assert proc.stdout is not None and proc.stderr is not None and proc.stdin is not None
                tasks = [
                    asyncio.create_task(_drain(proc.stdout, stdout, secrets)),
                    asyncio.create_task(_drain(proc.stderr, stderr, secrets)),
                    asyncio.create_task(_input(proc.stdin, spec.stdin)),
                ]
                leader_stopped = False
                while True:
                    tree.live()
                    if cancel.is_set():
                        state = "interrupted"
                        break
                    if time.monotonic() - started >= deadline_seconds:
                        state = "timed_out"
                        break
                    if any(task.done() and not task.cancelled() and task.exception() is not None for task in tasks):
                        state = "response_failed"
                        break
                    if outcome.done():
                        state, exit_code = outcome.result()
                        if not leader_stopped:
                            await _stop(tree, stop_grace_seconds, preserve=proc.pid)
                            control.send(b"x")
                            leader_stopped = True
                        if all(task.done() for task in tasks):
                            break
                    pending = [task for task in (outcome, *tasks) if not task.done()]
                    await asyncio.wait(pending, timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
            except OSError, ValueError, TypeError:
                state = "launch_failed" if proc is None else "response_failed"
            finally:
                inherited.close()
                if proc is not None:
                    if tree is None:
                        tree = _Tree(proc.pid)
                    await _shield_finish(
                        asyncio.create_task(_finish(proc, tree, tasks, stop_grace_seconds, control, outcome))
                    )
                else:
                    control.close()
    except OSError:
        state = "response_failed"
    if outcome is not None and outcome.done() and not outcome.cancelled() and outcome.exception() is None:
        _, exit_code = outcome.result()
    return ProcessResult(
        state,
        exit_code,
        _stop_confirmed(tree) if tree is not None else True,
        time.monotonic() - started,
    )
