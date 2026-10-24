"""Issue #333: agent_tmp.py が親の main worktree と tmp/agents/ のセッションディレクトリを決める仕様。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "agent_tmp.py"
SPEC = importlib.util.spec_from_file_location("agent_tmp", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
agent_tmp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_tmp)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    (path / ".gitignore").write_text("tmp\n", encoding="utf-8")
    git(path, "add", ".gitignore")
    git(path, "commit", "-m", "fixture")
    return path.resolve()


def test_resolve_main_root_from_subdirectory_and_linked_worktree(tmp_path: Path) -> None:
    """linked worktree・サブディレクトリから main worktree を返さなくなると、
    Spec・handoff・SDD の作業領域を feature worktree の中に黙って作り、worktree の片付けで消える。"""
    main = make_repo(tmp_path / "main")
    nested = main / "nested" / "dir"
    nested.mkdir(parents=True)
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", str(linked), "HEAD")

    assert agent_tmp.resolve_main_root(nested) == main
    assert agent_tmp.resolve_main_root(linked) == main


def test_make_session_dir_creates_unique_dirs_under_tmp_agents(tmp_path: Path) -> None:
    """同じ topic で作るたびに別のディレクトリにならないと、
    並行するセッションが同じ作業領域を共有し、片方の片付けがもう片方の成果を黙って消す。"""
    main = make_repo(tmp_path / "main")

    first = agent_tmp.make_session_dir(main, "issue-333")
    second = agent_tmp.make_session_dir(main, "issue-333")

    assert first != second
    for path in (first, second):
        assert path.is_dir()
        assert path.is_relative_to(main)
        # Git は空の未追跡ディレクトリを status に出さない。ファイルを置いて、無視されていることを確かめる。
        (path / "note.txt").write_text("x\n", encoding="utf-8")
    assert git(main, "status", "--porcelain") == ""


def test_make_session_dir_refuses_symlink_into_unignored_directory(tmp_path: Path) -> None:
    """`tmp/agents` が追跡対象のディレクトリへの symlink のとき、文字列の `tmp/agents` で無視を判定すると、
    作業領域を追跡対象の中に黙って作り、スクラッチが commit に混ざる。"""
    # `tmp/agents` itself matches the `tmp` ignore rule, so only the resolved target shows it is tracked.
    main = make_repo(tmp_path / "main")
    src = main / "src"
    src.mkdir()
    (src / "keep.py").write_text("x = 1\n", encoding="utf-8")
    git(main, "add", "src/keep.py")
    git(main, "commit", "-m", "tracked src")
    (main / "tmp").mkdir()
    (main / "tmp/agents").symlink_to(Path("../src"), target_is_directory=True)

    with pytest.raises(agent_tmp.AgentTmpError):
        agent_tmp.make_session_dir(main, "issue-333")
    assert [path.name for path in src.iterdir()] == ["keep.py"]


def test_cli_root_is_independent_of_cwd(tmp_path: Path) -> None:
    """root を cwd から解決するよう変わると、
    制作物などの Git の中から呼んだときに別の root を黙って返し、Spec・handoff が制作物の中に置かれる。"""
    common_dir = subprocess.run(
        ["git", "-C", str(SCRIPT.parent), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected = Path(common_dir).resolve().parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "root"],
        cwd=elsewhere,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"
