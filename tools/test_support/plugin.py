"""`tools.test_support` の pytest fixture。

利用側の test module で ``pytest_plugins = ("tools.test_support.plugin",)``
を宣言して読み込む。fixture は factory 関数への薄い wrapper である。
制作物は ``tools.test_support.repos.create_product`` を直接呼んで作る。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from tools.test_support.repos import EnvRepo, create_env_repo, create_owned_worktree


@pytest.fixture
def env_repo_factory(tmp_path: Path) -> Callable[[], EnvRepo]:
    """`tmp_path` 配下に連番ディレクトリを作って環境リポジトリを構築する factory。"""
    counter = itertools.count()

    def factory() -> EnvRepo:
        return create_env_repo(tmp_path / f"case{next(counter)}")

    return factory


@pytest.fixture
def env_repo(env_repo_factory: Callable[[], EnvRepo]) -> EnvRepo:
    """環境リポジトリの main checkout。"""
    return env_repo_factory()


@pytest.fixture
def owned_worktree(env_repo: EnvRepo, tmp_path: Path) -> Path:
    """`env_repo` から作る、`.venv` を持たない detached worktree。"""
    return create_owned_worktree(env_repo.root, tmp_path / "owned")
