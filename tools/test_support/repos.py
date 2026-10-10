"""実環境の形をした xrift-poc の checkout と制作物の factory。

環境リポジトリは ``git init`` した main checkout で、origin に GitHub の URL を設定し、
``refs/remotes/origin/main`` を指す detached HEAD にする（ローカルの ``main`` は無い）。
``items/``・``worlds/`` は ``.gitignore`` でルートから無視し、ディレクトリを作っておく。
制作物は ``items/<name>``・``worlds/<name>`` に作り、Git を初期化するかどうかと origin を選べる。
いずれもネットワークを使わない。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

SOURCE_ROOT = Path(__file__).resolve().parents[2]

_GIT_FLAGS = (
    "-c",
    "protocol.file.allow=always",
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    # global の clone.defaultRemoteName に左右されず、clone の remote 名を origin にする。
    "-c",
    "clone.defaultRemoteName=origin",
)

ENV_ORIGIN: Final[str] = "https://github.com/usaturn/xrift-poc.git"
_ENV_GITIGNORE: Final[str] = "/items/\n/worlds/\n/tmp/\n/reviews/\n/.worktrees/\n/.claude/worktrees/\n"
_GIT_CONFIG: Final[tuple[tuple[str, str], ...]] = (
    ("user.name", "Test"),
    ("user.email", "test@example.invalid"),
    ("commit.gpgsign", "false"),
)


class TestSupportError(Exception):
    """fixture の構築結果が期待する実環境の形と一致しないときに送出する。"""

    # 名前が `Test*` に一致するため、import した test module で pytest が収集しないようにする。
    __test__ = False


@dataclass(frozen=True, slots=True)
class EnvRepo:
    """`create_env_repo` が作る環境リポジトリの main checkout。"""

    root: Path
    head: str


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *_GIT_FLAGS, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    # 先頭の空白は `submodule status` の PINNED 記号（' '）を壊すため strip は末尾改行だけ。
    return result.stdout.rstrip("\n")


def _configure(repo: Path) -> None:
    for key, value in _GIT_CONFIG:
        _git(repo, "config", key, value)


def create_env_repo(
    base_dir: Path,
    *,
    origin_url: str = ENV_ORIGIN,
    copy_paths: Sequence[str] = ("tools/devenv/pyproject.toml", "tools/devenv/uv.lock"),
) -> EnvRepo:
    """`base_dir/xrift-poc` に環境リポジトリの main checkout を作る。

    `copy_paths` は実 checkout（`SOURCE_ROOT`）から写して commit するパス（ファイルかディレクトリ）。
    """
    if base_dir.exists() and any(base_dir.iterdir()):
        raise TestSupportError(f"base_dir is not empty: {base_dir}")
    root = base_dir / "xrift-poc"
    root.mkdir(parents=True)
    _git(base_dir, "init", "-q", "-b", "main", str(root))
    _configure(root)
    (root / ".gitignore").write_text(_ENV_GITIGNORE, encoding="utf-8")
    (root / "README.md").write_text("# fixture xrift-poc\n", encoding="utf-8")
    for relative in copy_paths:
        source = SOURCE_ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture env commit")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", origin_url)
    _git(root, "update-ref", "refs/remotes/origin/main", head)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(root, "checkout", "-q", "--detach", "origin/main")
    _git(root, "branch", "-q", "-D", "main")
    for kind in ("items", "worlds"):
        (root / kind).mkdir()
    return EnvRepo(root=root, head=head)


def create_product(
    env_root: Path,
    kind: Literal["items", "worlds"],
    name: str,
    *,
    git: bool = True,
    origin_url: str | None = None,
) -> Path:
    """`<env_root>/<kind>/<name>` に制作物を作る。`git` が真なら commit を 1 つ持つ Git リポジトリにする。"""
    project = env_root / kind / name
    if project.exists():
        raise TestSupportError(f"product already exists: {project}")
    project.mkdir(parents=True)
    (project / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    if not git:
        return project
    _git(project.parent, "init", "-q", "-b", "main", str(project))
    _configure(project)
    _git(project, "add", "README.md")
    _git(project, "commit", "-qm", "fixture product commit")
    if origin_url is not None:
        _git(project, "remote", "add", "origin", origin_url)
    return project


def create_owned_worktree(repo_root: Path, dest_parent: Path, *, sha: str = "HEAD", name: str = "worktree") -> Path:
    """`git worktree add --detach` で `<dest_parent>/<name>` を作り、`.venv` が無いことを確認して返す。"""
    dest_parent.mkdir(parents=True, exist_ok=True)
    worktree = dest_parent / name
    _git(repo_root, "worktree", "add", "--detach", str(worktree), sha)
    if (worktree / ".venv").exists():
        raise TestSupportError(f"owned worktree unexpectedly has .venv: {worktree}")
    return worktree
