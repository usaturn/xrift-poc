"""pytest fixture が factory への薄い wrapper として動くこと。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

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


@pytest.fixture
def leaked_codex_thread(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Codex の中のツールの実行と同じく、テストの前から `CODEX_THREAD_ID` がある状態で `outside_codex` を使う。"""
    monkeypatch.setenv("CODEX_THREAD_ID", "01a1536c-bab4-7511-b18d-bd7f4340769c")
    request.getfixturevalue("outside_codex")


@pytest.mark.usefixtures("leaked_codex_thread")
def test_outside_codex_removes_leaked_thread_id() -> None:
    assert "CODEX_THREAD_ID" not in os.environ
