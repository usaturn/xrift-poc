"""`tools.test_support.uv` のオフライン uv 実行補助と C-M1 の特性テスト。

C-M1: venv の無い owned worktree で `uv run --project tools/devenv --no-sync
pytest` を実行すると、uv は空の `.venv` を作ったあと `Failed to spawn` で
失敗する。controller 側（`tools/devenv/.venv` 同期済み）と同じようには
動かないことを、実 uv をオフラインで起動して固定する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tools.test_support.repos import SubmoduleState, create_owned_worktree, create_parent_repo
from tools.test_support.uv import offline_uv_env, run_uv_offline


def test_offline_uv_env_pins_offline_isolated_cache_and_test_interpreter() -> None:
    env = offline_uv_env(cache_dir=Path("/tmp/x-cache"))
    assert env["UV_OFFLINE"] == "1"
    assert env["UV_CACHE_DIR"] == "/tmp/x-cache"
    assert env["UV_PYTHON"] == sys.executable

    base = {"PATH": "/bin", "UV_OFFLINE": "0"}
    overridden = offline_uv_env(cache_dir=Path("/tmp/x-cache"), base=base)
    assert overridden["UV_OFFLINE"] == "1"
    assert base["UV_OFFLINE"] == "0"


def test_offline_uv_env_strips_the_running_venv_bin_from_path() -> None:
    """テストを動かす venv の bin が PATH に残ると、venv 無し worktree で
    `uv run` が spawn するコマンドがそこから解決され、C-M1 の失敗を再現できない。"""
    venv_bin = str(Path(sys.executable).parent)
    base = {"PATH": os.pathsep.join(("/usr/bin", venv_bin, "/bin"))}
    env = offline_uv_env(cache_dir=Path("/tmp/x-cache"), base=base)
    assert env["PATH"].split(os.pathsep) == ["/usr/bin", "/bin"]


def test_uv_no_sync_in_venvless_worktree_reproduces_cm1(tmp_path: Path) -> None:
    parent = create_parent_repo(tmp_path / "repo", submodule_state=SubmoduleState.DIVERGED)
    worktree = create_owned_worktree(parent.root, tmp_path / "wt")
    assert not (worktree / "tools" / "devenv" / ".venv").exists()

    result = run_uv_offline(
        ("run", "--project", "tools/devenv", "--no-sync", "pytest", "--version"),
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

    assert result.returncode != 0
    assert "Failed to spawn" in result.stderr
    assert (worktree / "tools" / "devenv" / ".venv").is_dir()


def test_uv_run_without_no_sync_fails_on_download_offline(tmp_path: Path) -> None:
    """`--no-sync` 無し（plan-complete が要求する形）は lock の同期でオフラインに失敗する。"""
    parent = create_parent_repo(tmp_path / "repo", submodule_state=SubmoduleState.PINNED)
    worktree = create_owned_worktree(parent.root, tmp_path / "wt")

    result = run_uv_offline(
        ("run", "--project", "tools/devenv", "pytest", "--version"),
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

    assert result.returncode != 0
    assert "Failed to download" in result.stderr or "Failed to spawn" in result.stderr
