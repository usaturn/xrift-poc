"""コミットしないファイルの置き場所（親の main worktree）を解決する helper。

Issue #333。AGENTS.md の「コミットしないファイルの置き場所」の規則を実装する。

- ``root``: 親リポジトリの main worktree の絶対パスを出力する
- ``mkdir <topic>``: ``<main worktree>/tmp/agents/<topic>.XXXXXX`` を作り、その絶対パスを出力する

main worktree は呼び出し元の cwd ではなく、このスクリプト自身の置き場所から解決する。
制作物の worktree やほかの worktree から呼ばれても、親の main worktree を返す。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

AGENTS_DIR = Path("tmp/agents")
TOPIC_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class AgentTmpError(Exception):
    """置き場所を安全に決められないときに送出する。"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], check=False, capture_output=True, text=True)


def resolve_main_root(anchor: Path) -> Path:
    """``anchor`` を含む checkout の main worktree を返す。"""
    common = _git(anchor, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        raise AgentTmpError(f"not inside a git checkout: {anchor}")
    common_dir = Path(common.stdout.strip()).resolve()
    if common_dir.name != ".git":
        raise AgentTmpError(f"git common dir is not the .git directory of a main worktree: {common_dir}")
    root = common_dir.parent
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != root:
        raise AgentTmpError(f"main worktree could not be confirmed: {root}")
    return root


def make_session_dir(main_root: Path, topic: str) -> Path:
    """``<main_root>/tmp/agents/<topic>.XXXXXX`` を作り、その絶対パスを返す。"""
    if TOPIC_PATTERN.fullmatch(topic) is None:
        raise AgentTmpError(f"invalid topic {topic!r}: use {TOPIC_PATTERN.pattern}")
    return Path(tempfile.mkdtemp(prefix=f"{topic}.", dir=scratch_root(main_root))).resolve()


def scratch_root(main_root: Path) -> Path:
    """``<main_root>/tmp/agents`` が main worktree の中で無視されていることを確かめて作り、その実パスを返す。

    runner の外部の担当に、セッションディレクトリを作る書き込み先として許可するときにも使う。
    """
    agents_dir = main_root / AGENTS_DIR
    resolved = agents_dir.resolve()
    main_resolved = main_root.resolve()
    if not resolved.is_relative_to(main_resolved):
        raise AgentTmpError(f"{AGENTS_DIR.as_posix()} resolves outside the main worktree: {resolved}")
    # symlink の先で判定する。`tmp/agents` の文字列だけでは、追跡対象のディレクトリを指す symlink を見逃す。
    target = resolved.relative_to(main_resolved).as_posix()
    ignored = _git(main_root, "check-ignore", "-q", target)
    if ignored.returncode != 0:
        raise AgentTmpError(f"{target} is not ignored by git in {main_root}")
    agents_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir.resolve()


def runner_scratch(repository: Path) -> tuple[Path, ...]:
    """runner の外部の担当が ``mkdir`` でセッションディレクトリを作れるよう、書き込みを許可する親の tmp/agents。

    runner は ``repository`` に登録された checkout を読み取り専用にする。親の main worktree がその 1 つの
    ときだけ返す。別のリポジトリ（制作物）や、親・tmp/agents を確かめられないときは何も許可しない。
    """
    try:
        root = resolve_main_root(Path(__file__).resolve().parent)
    except AgentTmpError:
        return ()
    common = _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0 or Path(common.stdout.strip()).resolve() != (root / ".git").resolve():
        return ()
    try:
        return (scratch_root(root),)
    except AgentTmpError, OSError:
        return ()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_tmp.py",
        description="Resolve the parent main worktree and create agent scratch directories under tmp/agents/.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("root", help="print the parent repository's main worktree")
    mkdir_parser = commands.add_parser("mkdir", help="create tmp/agents/<topic>.XXXXXX and print its path")
    mkdir_parser.add_argument("topic")
    args = parser.parse_args(argv)
    try:
        root = resolve_main_root(Path(__file__).resolve().parent)
        path = root if args.command == "root" else make_session_dir(root, args.topic)
    except (AgentTmpError, OSError) as error:
        print(f"agent_tmp: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
