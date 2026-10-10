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


def make_repo(path: Path, *, ignore_tmp: bool = True) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    (path / ".gitignore").write_text("tmp\n" if ignore_tmp else "build\n", encoding="utf-8")
    git(path, "add", ".gitignore")
    git(path, "commit", "-m", "fixture")
    return path.resolve()


def test_resolve_main_root_from_main_worktree(tmp_path: Path) -> None:
    main = make_repo(tmp_path / "main")

    assert agent_tmp.resolve_main_root(main) == main


def test_resolve_main_root_from_subdirectory_and_linked_worktree(tmp_path: Path) -> None:
    main = make_repo(tmp_path / "main")
    nested = main / "nested" / "dir"
    nested.mkdir(parents=True)
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", str(linked), "HEAD")

    assert agent_tmp.resolve_main_root(nested) == main
    assert agent_tmp.resolve_main_root(linked) == main


def test_resolve_main_root_rejects_submodule_checkout(tmp_path: Path) -> None:
    source = make_repo(tmp_path / "source")
    main = make_repo(tmp_path / "main")
    git(main, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "public")

    with pytest.raises(agent_tmp.AgentTmpError, match="common dir"):
        agent_tmp.resolve_main_root(main / "public")


def test_resolve_main_root_rejects_non_git_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    with pytest.raises(agent_tmp.AgentTmpError, match="not inside a git checkout"):
        agent_tmp.resolve_main_root(plain)


def test_make_session_dir_creates_unique_dirs_under_tmp_agents(tmp_path: Path) -> None:
    main = make_repo(tmp_path / "main")

    first = agent_tmp.make_session_dir(main, "issue-333")
    second = agent_tmp.make_session_dir(main, "issue-333")

    assert first != second
    for path in (first, second):
        assert path.is_dir()
        assert path.parent == main / "tmp" / "agents"
        assert path.name.startswith("issue-333.")
    assert git(main, "status", "--porcelain") == ""


def test_make_session_dir_accepts_version_like_topic(tmp_path: Path) -> None:
    main = make_repo(tmp_path / "main")

    path = agent_tmp.make_session_dir(main, "version-export-0.4.0rc1")

    assert path.name.startswith("version-export-0.4.0rc1.")


@pytest.mark.parametrize("topic", ["", "a/b", "../x", ".x", "-x", "Issue", "x" * 65, "a b"])
def test_make_session_dir_rejects_invalid_topic(tmp_path: Path, topic: str) -> None:
    main = make_repo(tmp_path / "main")

    with pytest.raises(agent_tmp.AgentTmpError, match="invalid topic"):
        agent_tmp.make_session_dir(main, topic)
    assert not (main / "tmp").exists()


def test_make_session_dir_refuses_when_tmp_is_not_ignored(tmp_path: Path) -> None:
    main = make_repo(tmp_path / "main", ignore_tmp=False)

    with pytest.raises(agent_tmp.AgentTmpError, match="not ignored"):
        agent_tmp.make_session_dir(main, "issue-333")
    assert not (main / "tmp").exists()


@pytest.mark.parametrize("link", ["tmp", "tmp/agents"])
def test_make_session_dir_refuses_symlink_escaping_main_worktree(tmp_path: Path, link: str) -> None:
    main = make_repo(tmp_path / "main")
    outside = tmp_path / "outside"
    outside.mkdir()
    link_path = main / link
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(agent_tmp.AgentTmpError):
        agent_tmp.make_session_dir(main, "issue-333")
    assert list(outside.iterdir()) == []


def test_make_session_dir_refuses_symlink_into_unignored_directory(tmp_path: Path) -> None:
    # `tmp/agents` itself matches the `tmp` ignore rule, so only the resolved target shows it is tracked.
    main = make_repo(tmp_path / "main")
    src = main / "src"
    src.mkdir()
    (src / "keep.py").write_text("x = 1\n", encoding="utf-8")
    git(main, "add", "src/keep.py")
    git(main, "commit", "-m", "tracked src")
    (main / "tmp").mkdir()
    (main / "tmp/agents").symlink_to(Path("../src"), target_is_directory=True)

    with pytest.raises(agent_tmp.AgentTmpError, match="not ignored"):
        agent_tmp.make_session_dir(main, "issue-333")
    assert [path.name for path in src.iterdir()] == ["keep.py"]


def test_cli_rejects_invalid_topic_without_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_tmp.main(["mkdir", "bad/topic"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agent_tmp: ")


def test_cli_root_is_independent_of_cwd(tmp_path: Path) -> None:
    common_dir = subprocess.run(
        ["git", "-C", str(SCRIPT.parent), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected = Path(common_dir).resolve().parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "root"],
        cwd=elsewhere,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"
