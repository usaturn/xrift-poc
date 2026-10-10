"""pytest fixture が factory への薄い wrapper として動くこと。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tools.test_support.repos import EnvRepo

pytest_plugins = ("tools.test_support.plugin",)


def test_env_repo_fixture(env_repo: EnvRepo) -> None:
    assert (env_repo.root / ".git").is_dir()
    assert (env_repo.root / "items").is_dir()


def test_env_repo_factory_builds_independent_repositories(env_repo_factory: Callable[[], EnvRepo]) -> None:
    first = env_repo_factory()
    second = env_repo_factory()
    assert first.root != second.root


def test_owned_worktree_fixture(owned_worktree: Path, env_repo: EnvRepo) -> None:
    assert owned_worktree.is_dir()
    assert not (owned_worktree / ".venv").exists()
