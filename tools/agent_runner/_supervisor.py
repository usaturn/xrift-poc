"""Per-command session anchor; status uses a private socket, never CLI streams.

Executed by absolute path with the current interpreter's isolated, no-site mode.
The command inherits literal argv/environment/stdio, but not the control socket.
The controller retains PID/starttime ownership until descendants are stopped.
"""

import os
import signal
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType


def _hold_anchor(_signal: int, _frame: FrameType | None) -> None:
    # A caught handler (unlike SIG_IGN) resets to default on command exec.
    # Keep the anchor alive while the controller signals command descendants.
    pass


def _start_command(argv: list[str]) -> Callable[[], int]:
    # Python may coerce LC_CTYPE at startup; /proc retains the original exec bytes.
    env = {
        os.fsdecode(name): os.fsdecode(value)
        for entry in Path("/proc/self/environ").read_bytes().split(b"\0")
        if entry and b"=" in entry
        for name, value in [entry.split(b"=", 1)]
    }
    if hasattr(os, "POSIX_SPAWN_CLOSEFROM"):
        pid = os.posix_spawnp(
            argv[0],
            argv,
            env,
            file_actions=((os.POSIX_SPAWN_CLOSEFROM, 3),),
            setsigdef=(signal.SIGINT, signal.SIGTERM, signal.SIGPIPE, signal.SIGXFSZ),
        )

        def wait() -> int:
            return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

        return wait
    # Keep close_fds semantics on Linux builds lacking the closefrom extension.
    # Import only in this path: repeated supervisor startup is part of the role
    # deadline, including Git preparation and fallback attempts.
    import subprocess

    return subprocess.Popen(argv, env=env, close_fds=True).wait


def main() -> int:
    signal.signal(signal.SIGTERM, _hold_anchor)
    signal.signal(signal.SIGINT, _hold_anchor)
    with socket.socket(fileno=int(sys.argv[1])) as control:
        wait_command: Callable[[], int] | None = None
        outcome = b"launch_failed\n"
        try:
            wait_command = _start_command(sys.argv[2:])
        except OSError, ValueError, TypeError, IndexError:
            pass
        finally:
            # Only the command may keep the controller's stdin/log pipes open.
            for descriptor in (0, 1, 2):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if wait_command is not None:
            outcome = f"exit {wait_command()}\n".encode("ascii")
        try:
            control.sendall(outcome)
            # Original command exit is not permission to abandon its group.
            # Release/EOF is sent only after controller descendant teardown.
            control.recv(1)
        except OSError:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
