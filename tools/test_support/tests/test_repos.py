"""xrift-poc の環境リポジトリと制作物の fixture が、実環境と同じ形になること。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.test_support.repos import (
    ENV_ORIGIN,
    TestSupportError,
    create_env_repo,
    create_owned_worktree,
    create_product,
)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def test_env_repo_is_a_detached_main_checkout_with_the_github_origin(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "base")
    assert git(env.root, "config", "--get", "remote.origin.url").stdout.strip() == ENV_ORIGIN
    assert git(env.root, "symbolic-ref", "-q", "HEAD").returncode != 0
    assert git(env.root, "rev-parse", "HEAD").stdout.strip() == env.head
    assert git(env.root, "rev-parse", "origin/main").stdout.strip() == env.head
    assert git(env.root, "show-ref", "--verify", "--quiet", "refs/heads/main").returncode != 0
    assert git(env.root, "ls-files", "tools/devenv/pyproject.toml").stdout.strip() == "tools/devenv/pyproject.toml"


def test_env_repo_ignores_the_work_directories(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "base")
    for kind in ("items", "worlds"):
        assert (env.root / kind).is_dir()
        probe = env.root / kind / "probe" / "file.txt"
        probe.parent.mkdir()
        probe.write_text("x", encoding="utf-8")
        assert git(env.root, "check-ignore", "-q", str(probe.relative_to(env.root))).returncode == 0
    assert git(env.root, "status", "--porcelain").stdout == ""


def test_create_product_with_and_without_git(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "base")
    plain = create_product(env.root, "worlds", "空白 ワールド", git=False)
    assert plain == env.root / "worlds" / "空白 ワールド"
    assert not (plain / ".git").exists()
    repo = create_product(env.root, "items", "my-item", origin_url="git@github.com:someone/my-item.git")
    assert git(repo, "rev-parse", "--show-toplevel").stdout.strip() == str(repo.resolve())
    assert git(repo, "config", "--get", "remote.origin.url").stdout.strip() == "git@github.com:someone/my-item.git"
    no_origin = create_product(env.root, "items", "local-only")
    assert git(no_origin, "config", "--get", "remote.origin.url").returncode != 0


def test_create_product_rejects_an_existing_directory(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "base")
    create_product(env.root, "worlds", "w", git=False)
    with pytest.raises(TestSupportError):
        create_product(env.root, "worlds", "w", git=False)


def test_owned_worktree_has_no_venv(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "base")
    worktree = create_owned_worktree(env.root, tmp_path / "owned")
    assert worktree == tmp_path / "owned" / "worktree"
    assert not (worktree / ".venv").exists()
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == env.head


def test_create_env_repo_rejects_a_non_empty_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "stale").write_text("x", encoding="utf-8")
    with pytest.raises(TestSupportError):
        create_env_repo(base)
