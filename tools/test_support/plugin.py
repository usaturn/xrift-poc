"""`tools.test_support` の pytest fixture。

利用側の test module で ``pytest_plugins = ("tools.test_support.plugin",)``
を宣言して読み込む。fixture は factory 関数への薄い wrapper であり、
状態の指定・複数個の構築が要る場合は ``parent_repo_factory`` を使うか
``tools.test_support.repos`` の関数を直接呼ぶ。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from tools.test_support.repos import (
    ParentRepo,
    SubmoduleState,
    create_owned_worktree,
    create_parent_repo,
)


@pytest.fixture
def parent_repo_factory(tmp_path: Path) -> Callable[..., ParentRepo]:
    """`tmp_path` 配下に連番ディレクトリを作って親 repository を構築する factory。"""
    counter = itertools.count()

    def factory(*, submodule_state: SubmoduleState = SubmoduleState.DIVERGED) -> ParentRepo:
        return create_parent_repo(tmp_path / f"case{next(counter)}", submodule_state=submodule_state)

    return factory


@pytest.fixture
def parent_repo(parent_repo_factory: Callable[..., ParentRepo]) -> ParentRepo:
    """`SubmoduleState.DIVERGED`（実 Dev Container の親 checkout と同じ '+' 状態）の親 repository。"""
    return parent_repo_factory()


@pytest.fixture
def owned_worktree(parent_repo: ParentRepo, tmp_path: Path) -> Path:
    """`parent_repo` から作る、`.venv` を持たない detached worktree。"""
    return create_owned_worktree(parent_repo.root, tmp_path / "owned")
