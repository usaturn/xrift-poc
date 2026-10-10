"""`tools.test_support.repos` の factory が実環境の形を再現することを固定する。

実親 checkout の `public` gitlink（mode 160000）・`git submodule status` の
先頭記号（`+` / ` ` / `-`）・`origin/main` の detached HEAD（ローカル `main`
無し）・venv の無い owned worktree を、ローカルの bare remote だけで作る
fixture の特性テスト。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.test_support.repos import (
    SubmoduleState,
    TestSupportError,
    create_owned_worktree,
    create_parent_repo,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # 先頭の空白は PINNED 状態の marker（' '）自体なので strip は末尾改行だけ。
    return result.stdout.rstrip("\n")


def _public_status_line(root: Path) -> str:
    status_lines = _git(root, "submodule", "status").splitlines()
    return next(line for line in status_lines if " public" in line)


@pytest.mark.parametrize(
    ("state", "marker"),
    [
        (SubmoduleState.DIVERGED, "+"),
        (SubmoduleState.PINNED, " "),
        (SubmoduleState.UNINITIALIZED, "-"),
    ],
)
def test_parent_repo_public_submodule_state(tmp_path: Path, state: SubmoduleState, marker: str) -> None:
    parent = create_parent_repo(tmp_path / "case", submodule_state=state)

    public_line = _public_status_line(parent.root)
    assert public_line[0] == marker
    entry = _git(parent.root, "ls-files", "-s", "public")
    assert entry.split()[0] == "160000"
    assert parent.gitlink_sha in entry


def test_parent_repo_has_local_origin_and_devenv_project_files(tmp_path: Path) -> None:
    parent = create_parent_repo(tmp_path / "case", submodule_state=SubmoduleState.DIVERGED)

    assert _git(parent.root, "remote", "get-url", "origin") == str(parent.origin)
    assert (parent.origin / "HEAD").exists()
    tracked = _git(parent.root, "ls-files").splitlines()
    assert "tools/devenv/pyproject.toml" in tracked
    assert "tools/devenv/uv.lock" in tracked
    assert ".gitmodules" in tracked


@pytest.mark.parametrize("state", list(SubmoduleState))
def test_parent_repo_head_is_detached_at_origin_main_without_local_main(tmp_path: Path, state: SubmoduleState) -> None:
    """親 checkout は実 checkout と同じく `origin/main` の detached HEAD で、ローカルの `main` を持たない。"""
    parent = create_parent_repo(tmp_path / "case", submodule_state=state)

    assert _git(parent.root, "branch", "--show-current") == ""
    assert _git(parent.root, "rev-parse", "HEAD") == _git(parent.root, "rev-parse", "refs/remotes/origin/main")
    assert _git(parent.root, "for-each-ref", "refs/heads/main") == ""
    assert _git(parent.root, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"


def test_pinned_head_matches_gitlink_and_diverged_differs(tmp_path: Path) -> None:
    pinned = create_parent_repo(tmp_path / "pinned", submodule_state=SubmoduleState.PINNED)
    assert pinned.submodule_head == pinned.gitlink_sha

    diverged = create_parent_repo(tmp_path / "diverged", submodule_state=SubmoduleState.DIVERGED)
    assert diverged.submodule_head is not None
    assert diverged.submodule_head != diverged.gitlink_sha

    uninit = create_parent_repo(tmp_path / "uninit", submodule_state=SubmoduleState.UNINITIALIZED)
    assert uninit.submodule_head is None
    assert (uninit.root / "public").is_dir()
    assert list((uninit.root / "public").iterdir()) == []


def test_create_parent_repo_rejects_non_empty_base_dir(tmp_path: Path) -> None:
    """`TestSupportError` を import した test module でも、pytest がこの例外をテストクラスとして収集しない。"""
    base_dir = tmp_path / "case"
    base_dir.mkdir()
    (base_dir / "leftover").write_text("", encoding="utf-8")

    with pytest.raises(TestSupportError, match="base_dir is not empty"):
        create_parent_repo(base_dir, submodule_state=SubmoduleState.PINNED)


def test_parent_repo_builds_and_commits_when_global_config_requires_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """global の `commit.gpgsign=true` でも、fixture の構築と利用側の追加 commit が署名を要求しない。

    fixture 内の commit は submodule の状態による分岐より前に済むため、状態は既定の DIVERGED だけで確かめる。
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    parent = create_parent_repo(tmp_path / "case", submodule_state=SubmoduleState.DIVERGED)

    _git(parent.root, "commit", "-q", "--allow-empty", "-m", "consumer commit")


def test_parent_repo_keeps_origin_remotes_when_global_config_renames_default_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """global の `clone.defaultRemoteName` があっても、親と `public` submodule の remote は `origin` になる。

    実 checkout と同じ remote 名にそろえる。submodule を clone する DIVERGED で、親の clone と
    submodule の clone の両方を確かめる。
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[clone]\n\tdefaultRemoteName = upstream\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    parent = create_parent_repo(tmp_path / "case", submodule_state=SubmoduleState.DIVERGED)

    assert _git(parent.root, "remote") == "origin"
    assert _git(parent.root / "public", "remote") == "origin"
    assert _git(parent.root, "branch", "--show-current") == ""


def test_owned_worktree_is_detached_and_has_no_venv(tmp_path: Path) -> None:
    parent = create_parent_repo(tmp_path / "case", submodule_state=SubmoduleState.PINNED)
    head = _git(parent.root, "rev-parse", "HEAD")

    worktree = create_owned_worktree(parent.root, tmp_path / "wt")

    assert _git(worktree, "rev-parse", "HEAD") == head
    assert _git(worktree, "branch", "--show-current") == ""
    assert not (worktree / ".venv").exists()
    assert not (worktree / "tools" / "devenv" / ".venv").exists()
    assert _git(worktree, "submodule", "status").splitlines()[0][0] == "-"
