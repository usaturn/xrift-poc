"""`tools.test_support.plugin` の pytest fixture が利用できることを固定する。"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from tools.test_support.repos import ParentRepo, SubmoduleState

pytest_plugins = ("tools.test_support.plugin",)


def _status_marker(root: Path) -> str:
    stdout = subprocess.run(
        ["git", "-C", str(root), "submodule", "status"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return next(line for line in stdout.splitlines() if " public" in line)[0]


def test_parent_repo_fixture_is_diverged_by_default(parent_repo: ParentRepo) -> None:
    assert _status_marker(parent_repo.root) == "+"


def test_parent_repo_factory_overrides_state(
    parent_repo_factory: Callable[..., ParentRepo],
) -> None:
    parent = parent_repo_factory(submodule_state=SubmoduleState.UNINITIALIZED)
    assert parent.submodule_head is None


def test_owned_worktree_fixture_has_no_venv(owned_worktree: Path) -> None:
    assert not (owned_worktree / ".venv").exists()
