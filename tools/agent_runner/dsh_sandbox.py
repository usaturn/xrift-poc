"""Scoped bubblewrap runner for DSH: grants are fixed by the caller before launch."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

BASE = ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent"]


def grant_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("grant must be an existing canonical absolute path")
    mode = path.lstat().st_mode
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)) or path == Path("/"):
        raise ValueError("grant must be a regular file or directory, not filesystem root")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    try:
        values = sys.argv[1:] if argv is None else argv
        boundary = values.index("--")
        parser = argparse.ArgumentParser()
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--grant", action="append", default=[])
        parser.add_argument("--read-root", action="append", default=[])
        args = parser.parse_args(values[:boundary])
        remaining = values[boundary + 1 :]
        end = remaining.index("--")
        profile, command = remaining[:end], remaining[end + 1 :]
        workspace = grant_path(args.workspace)
        grants = [grant_path(value) for value in args.grant]
        read_paths = [grant_path(value) for value in args.read_root]
        reads = [argument for path in read_paths for argument in ("--ro-bind", path, path)]
        writable = [*BASE, "--tmpfs", "/tmp", "--bind", workspace, workspace]
        if profile not in (BASE, writable) or not command:
            raise ValueError("unexpected DSH sandbox profile")
        runner = shutil.which("bwrap")
        if runner is None:
            raise ValueError("bubblewrap is unavailable; cannot run unconfined")
        additions = [argument for path in dict.fromkeys(grants) for argument in ("--bind", path, path)]
        mounts = (
            [*BASE, "--tmpfs", "/tmp", *reads, "--bind", workspace, workspace, *additions]
            if profile == writable
            else [*BASE, *reads]
        )
        os.execv(runner, [runner, *mounts, "--", *command])
    except (OSError, ValueError) as error:
        print(f"dsh-sandbox: {error}", file=sys.stderr)
        return 127
    return 127  # execv only returns by raising.


if __name__ == "__main__":
    raise SystemExit(main())
