"""実環境の形をした親 repository と owned worktree の factory。

親 repository はローカルの bare remote 経由の clone として作り、`public`
submodule の gitlink（mode 160000）を持つ。HEAD は実 checkout と同じく
`origin/main` の detached HEAD で、ローカルの `main` ブランチは無い。
submodule の状態は `git submodule status` の先頭記号（`+` / ` ` / `-`）で
選べる。いずれもネットワークを使わない。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

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
    # global の clone.defaultRemoteName に左右されず、親と submodule の clone の remote 名を
    # 実 checkout と同じ origin にする。コマンドラインの -c は submodule update が起動する clone にも伝わる。
    "-c",
    "clone.defaultRemoteName=origin",
)


class TestSupportError(Exception):
    """fixture の構築結果が期待する実環境の形と一致しないときに送出する。"""

    # 名前が `Test*` に一致するため、import した test module で pytest が収集しないようにする。
    __test__ = False


class SubmoduleState(Enum):
    """`git submodule status` の先頭記号に対応する submodule の状態。"""

    DIVERGED = auto()  # 初期化済み・HEAD が gitlink と異なる（'+'）
    PINNED = auto()  # 初期化済み・HEAD が gitlink と一致（' '）
    UNINITIALIZED = auto()  # 未初期化（'-'）

    @property
    def marker(self) -> str:
        return {
            SubmoduleState.DIVERGED: "+",
            SubmoduleState.PINNED: " ",
            SubmoduleState.UNINITIALIZED: "-",
        }[self]


@dataclass(frozen=True, slots=True)
class ParentRepo:
    """`create_parent_repo` が作る親 repository の checkout とその remote。"""

    root: Path
    origin: Path
    public_origin: Path
    gitlink_sha: str
    submodule_head: str | None


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *_GIT_FLAGS, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    # 先頭の空白は `submodule status` の PINNED 記号（' '）を壊すため strip は末尾改行だけ。
    return result.stdout.rstrip("\n")


def _gitlink_sha(root: Path) -> str:
    return _git(root, "ls-files", "-s", "public").split()[1]


def _submodule_head(root: Path) -> str | None:
    # 未初期化の public/ には .git が無く、`rev-parse` は親 repo の HEAD を返してしまう。
    if not (root / "public" / ".git").exists():
        return None
    return _git(root / "public", "rev-parse", "--verify", "HEAD")


def _assert_public_state(root: Path, state: SubmoduleState) -> None:
    status_lines = _git(root, "submodule", "status").splitlines()
    public_line = next((line for line in status_lines if " public" in line), None)
    if public_line is None:
        raise TestSupportError(f"no public submodule in {root}")
    if public_line[0] != state.marker:
        raise TestSupportError(f"public submodule marker {public_line[0]!r} != {state.marker!r} in {root}")
    if _git(root, "ls-files", "-s", "public").split()[0] != "160000":
        raise TestSupportError(f"public is not a gitlink (mode 160000) in {root}")


def create_parent_repo(base_dir: Path, *, submodule_state: SubmoduleState) -> ParentRepo:
    """ローカルの bare remote を持つ親 repository の clone を `base_dir` に作る。

    親は `public` gitlink と実物をコピーした `tools/devenv/pyproject.toml`・
    `tools/devenv/uv.lock` を tracked に持ち、clone の repo config には
    `user.name`/`user.email`/`protocol.file.allow=always`/`commit.gpgsign=false`
    を設定済みとする。clone は実 checkout と同じく `origin/main` の detached
    HEAD で、ローカルの `main` ブランチを持たない（`origin/HEAD` は
    `origin/main` を指す）。ブランチが必要な利用者は自分で作る。
    """
    if base_dir.exists() and any(base_dir.iterdir()):
        raise TestSupportError(f"base_dir is not empty: {base_dir}")
    base_dir.mkdir(parents=True, exist_ok=True)

    public_src = base_dir / "public-src"
    public_origin = base_dir / "public-origin.git"
    parent_src = base_dir / "parent-src"
    parent_origin = base_dir / "parent-origin.git"
    root = base_dir / "parent"

    _git(base_dir, "init", "-q", "-b", "main", str(public_src))
    (public_src / "README.md").write_text("# fixture public repository\n", encoding="utf-8")
    _git(public_src, "add", "README.md")
    _git(public_src, "commit", "-qm", "public commit A")
    sha_a = _git(public_src, "rev-parse", "HEAD")
    (public_src / "README.md").write_text("# fixture public repository\n\nsecond commit\n", encoding="utf-8")
    _git(public_src, "commit", "-qam", "public commit B")
    sha_b = _git(public_src, "rev-parse", "HEAD")
    _git(base_dir, "clone", "-q", "--bare", str(public_src), str(public_origin))

    _git(base_dir, "init", "-q", "-b", "main", str(parent_src))
    (parent_src / "tools" / "devenv").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(SOURCE_ROOT / "tools" / "devenv" / name, parent_src / "tools" / "devenv" / name)
    (parent_src / "README.md").write_text("# fixture parent repository\n", encoding="utf-8")
    _git(parent_src, "add", "README.md", "tools/devenv/pyproject.toml", "tools/devenv/uv.lock")
    _git(parent_src, "submodule", "add", "-q", str(public_origin), "public")
    # gitlink は commit A を指すように記録し、B との差分で '+' 状態を作れるようにする。
    _git(parent_src / "public", "checkout", "-q", sha_a)
    _git(parent_src, "add", "public")
    _git(parent_src, "commit", "-qm", "parent with public submodule at A")
    gitlink = _gitlink_sha(parent_src)

    _git(base_dir, "clone", "-q", "--bare", str(parent_src), str(parent_origin))
    _git(base_dir, "clone", "-q", str(parent_origin), str(root))
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("protocol.file.allow", "always"),
        ("commit.gpgsign", "false"),
    ):
        _git(root, "config", key, value)
    # 実 checkout と同じく origin/main の detached HEAD にし、ローカルの main（と branch.main.* の設定）を消す。
    # global の submodule.recurse=true でも submodule に触れないよう --no-recurse-submodules を付ける。
    _git(root, "checkout", "-q", "--no-recurse-submodules", "--detach", "origin/main")
    _git(root, "branch", "-q", "-D", "main")

    if submodule_state is not SubmoduleState.UNINITIALIZED:
        _git(root, "submodule", "update", "--init", "-q")
        if submodule_state is SubmoduleState.DIVERGED:
            _git(root / "public", "checkout", "-q", sha_b)

    _assert_public_state(root, submodule_state)
    return ParentRepo(
        root=root,
        origin=parent_origin,
        public_origin=public_origin,
        gitlink_sha=gitlink,
        submodule_head=_submodule_head(root),
    )


def create_owned_worktree(repo_root: Path, dest_parent: Path, *, sha: str = "HEAD", name: str = "worktree") -> Path:
    """`git worktree add --detach` で `<dest_parent>/<name>` を作り、`.venv` が無いことを確認して返す。"""
    dest_parent.mkdir(parents=True, exist_ok=True)
    worktree = dest_parent / name
    _git(repo_root, "worktree", "add", "--detach", str(worktree), sha)
    if (worktree / ".venv").exists():
        raise TestSupportError(f"owned worktree unexpectedly has .venv: {worktree}")
    return worktree
