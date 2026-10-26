"""親checkoutの永続記録とプロセス間の排他。"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from .models import Run, now


class QueueError(Exception):
    """副作用へ進めない入力・観測・順序の不整合。next_actionは司令塔が次に行う操作。"""

    def __init__(self, message: str, *, next_action: str = "inspect"):
        super().__init__(message)
        self.next_action = next_action


LOCK_BUSY = "別のqueue操作（子のack・report・publishなど）が進行中です。何も実行していません"


class LockBusy(QueueError):
    """別のqueue操作がロックを持っていて、コマンドを何も実行しなかった。一時的な競合で、待ってから照合し直す。"""

    def __init__(self, message: str):
        super().__init__(message, next_action="wait")


class Store:
    def __init__(self, parent: Path):
        self.parent = parent.resolve()
        self.base = self.parent / "tmp/agents"
        if not self.base.resolve().is_relative_to(self.parent):
            raise QueueError("tmp/agents が親checkoutの外を指しています")
        self.base.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, *, wait: bool = False) -> Generator[None]:
        # unlink しない。同じ inode を全プロセスが使う。
        with (self.base / ".issue-queue.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise LockBusy(LOCK_BUSY) from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def path(self, run_dir: str) -> Path:
        path = Path(run_dir).resolve()
        if path.parent != self.base.resolve() or not path.name.startswith("issue-queue."):
            raise QueueError("runは親main checkoutのtmp/agents/issue-queue.*で指定してください")
        if (path / "state.json").is_symlink():
            raise QueueError("state.jsonのsymlinkは使用できません")
        return path

    def load(self, run_dir: str) -> Run:
        run = Run.model_validate_json((self.path(run_dir) / "state.json").read_text())
        if Path(run.parent).resolve() != self.parent:
            raise QueueError("保存された親checkoutが一致しません")
        return run

    def create(self, run: Run) -> Path:
        for file in self.base.glob("issue-queue.*/state.json"):
            previous = self.load(str(file.parent))
            if not previous.complete and previous.ended_reason is None:
                raise QueueError(f"未完了runを再開するか、続けられなければendで終えてください: {file.parent}")
        path = Path(tempfile.mkdtemp(prefix="issue-queue.", dir=self.base))
        self.save(path, run)
        return path

    def save(self, path: Path, run: Run) -> None:
        path = self.path(str(path))
        run.updated_at = now()
        fd, name = tempfile.mkstemp(prefix=".state-", dir=path)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(run.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path / "state.json")
            directory = os.open(path, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
