"""Settle an owned attempt's temporary files only after its writers have stopped."""

import os
import stat
from pathlib import Path

from tools.agent_runner.process import redact_file

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def settle_scratch(root: Path, secrets: tuple[str, ...]) -> list[Path]:
    """Prune special entries without following them, redact files, report anything unsafe.

    Directory descriptors anchor every operation. A hardlinked regular file is
    replaced by redact_file, not rewritten in place, preserving its other names.
    Nothing outside this stopped attempt's scratch is removed or rewritten.
    """
    unsafe: list[Path] = []
    try:
        descriptor = os.open(root, _DIRECTORY)
    except OSError:
        return [root]
    owner, device = os.getuid(), os.fstat(descriptor).st_dev
    stack: list[tuple[int, Path, list[str]]] = [(descriptor, root, [])]

    def entries(fd: int, path: Path) -> list[str]:
        info = os.fstat(fd)
        if info.st_uid != owner or info.st_dev != device:
            unsafe.append(path)
            return []
        try:
            return sorted(os.listdir(fd), reverse=True)
        except OSError:
            unsafe.append(path)
            return []

    try:
        stack[0][2].extend(entries(descriptor, root))
        while stack:
            parent, path, pending = stack[-1]
            if not pending:
                os.close(parent)
                stack.pop()
                continue
            name = pending.pop()
            child_path = path / name
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if info.st_uid != owner or info.st_dev != device:
                    unsafe.append(child_path)
                elif stat.S_ISDIR(info.st_mode):
                    child = os.open(name, _DIRECTORY, dir_fd=parent)
                    stack.append((child, child_path, []))
                    stack[-1][2].extend(entries(child, child_path))
                elif stat.S_ISREG(info.st_mode):
                    # Use the anchored directory even if its pathname was replaced.
                    if not redact_file(Path(f"/proc/self/fd/{parent}") / name, secrets):
                        unsafe.append(child_path)
                else:
                    # pytest/tool links, sockets and FIFOs carry no output to retain.
                    os.unlink(name, dir_fd=parent)
            except OSError:
                unsafe.append(child_path)
    finally:
        for parent, _, _ in stack:
            os.close(parent)
    return unsafe
