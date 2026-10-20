"""並行した ``git worktree add`` の書きかけの登録を仕込む補助（#68・#88）。

同じリポジトリへ並行した ``git worktree add`` は、相手の登録の ``commondir`` を書き終える前に読むと、
``failed to read .../worktrees/<id>/commondir: Success``（終了コード 128）で、何も作らずに止まる。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def plant_racer(repository: Path, name: str = "racer") -> Path:
    """``commondir`` がまだ空の登録を ``<repository>/.git/worktrees/<name>`` に置き、その ``commondir`` を返す。

    書き終えるまで、このリポジトリの ``git worktree add`` は登録の一覧を読むところで、何も作らずに止まる。
    ``commondir`` に ``../..`` を書くと、相手が書き終えた状態になる。

    登録の ``gitdir`` は存在しない場所を指し、``index`` も無いので、``locked`` が無いと
    git の prune が期限によらず消す。テストのリポジトリを作る ``git commit`` は切り離した自動 maintenance を起動し、
    git 2.55 ではその中で worktree を prune する（#88）。本物の ``git worktree add`` と同じく ``locked`` を置き、
    別の場所で作ってから rename で一括で置くので、仕込みの途中にも prune できる形は見えない。
    """
    git_dir = repository / ".git"
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    staging = Path(tempfile.mkdtemp(prefix=f"{name}.", dir=git_dir))
    (staging / "locked").write_text("initializing\n", encoding="utf-8")
    (staging / "gitdir").write_text(f"{repository.parent / name / '.git'}\n", encoding="utf-8")
    (staging / "HEAD").write_text(f"{head}\n", encoding="utf-8")
    (staging / "commondir").write_bytes(b"")
    worktrees = git_dir / "worktrees"
    worktrees.mkdir(exist_ok=True)
    return staging.rename(worktrees / name) / "commondir"
