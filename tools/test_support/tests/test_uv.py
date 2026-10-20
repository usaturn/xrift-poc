"""`tools.test_support.uv` のオフライン uv 実行補助と C-M1 の特性テスト。

C-M1: venv の無い owned worktree で `uv run --project tools/devenv --no-sync
pytest` を実行すると、uv は空の `.venv` を作ったあと `Failed to spawn` で
失敗する。controller 側（`tools/devenv/.venv` 同期済み）と同じようには
動かないことを、実 uv をオフラインで起動して固定する。
"""

from __future__ import annotations

import os
import shutil
import sys
import venv
from pathlib import Path

import pytest

from tools.test_support.repos import create_env_repo, create_owned_worktree
from tools.test_support.uv import offline_uv_env, run_uv_offline


def _make_venv(root: Path) -> Path:
    """`root` に pip 無しの実際の venv を作り、その `bin` を返す。"""
    venv.create(root, with_pip=False, symlinks=True)
    return root / "bin"


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


def test_offline_uv_env_strips_other_venv_bins_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外側の `uv run --project <別の checkout>` が子の PATH に足す、別の venv の bin も除く（#633）。
    venv でないディレクトリ・相対パス・空のエントリは、順序を保って残す。"""
    other_bin = _make_venv(tmp_path / "other-venv")
    plain_bin = tmp_path / "plain" / "bin"
    plain_bin.mkdir(parents=True)
    # 相対パスはテストの cwd からは venv の bin を指すが、子の cwd では指す先が変わるので判定しない
    monkeypatch.chdir(tmp_path)
    entries = [str(plain_bin), str(other_bin), f"{other_bin}/", "other-venv/bin", "", "/bin"]

    env = offline_uv_env(cache_dir=tmp_path / "uv-cache", base={"PATH": os.pathsep.join(entries)})

    assert env["PATH"].split(os.pathsep) == [str(plain_bin), "other-venv/bin", "", "/bin"]


def test_uv_no_sync_in_venvless_worktree_reproduces_cm1(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "repo")
    worktree = create_owned_worktree(env.root, tmp_path / "wt")
    assert not (worktree / "tools" / "devenv" / ".venv").exists()

    result = run_uv_offline(
        ("run", "--project", "tools/devenv", "--no-sync", "pytest", "--version"),
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

    assert result.returncode != 0
    assert "Failed to spawn" in result.stderr
    assert (worktree / "tools" / "devenv" / ".venv").is_dir()


def test_uv_no_sync_reproduces_cm1_with_another_venv_bin_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別の checkout の venv の bin が PATH の先頭にあっても C-M1 を再現する（#633）。

    agent-runner から起動した担当は、外側の `uv run --project <別の checkout>/tools/devenv`
    が足した venv の bin と `VIRTUAL_ENV` を受け継ぐ。そこに `pytest` があると、
    venv の無い worktree の `uv run --no-sync pytest` が成功してしまっていた。
    """
    env = create_env_repo(tmp_path / "repo")
    worktree = create_owned_worktree(env.root, tmp_path / "wt")
    other_venv = tmp_path / "other-venv"
    other_bin = _make_venv(other_venv)
    # 別の checkout の venv にある pytest の代わり。起動されれば終了コード 0 で終わる
    fake_pytest = other_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pytest.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(other_bin), os.environ["PATH"])))
    monkeypatch.setenv("VIRTUAL_ENV", str(other_venv))

    result = run_uv_offline(
        ("run", "--project", "tools/devenv", "--no-sync", "pytest", "--version"),
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

    assert result.returncode != 0
    assert "Failed to spawn" in result.stderr


def test_uv_no_sync_reproduces_cm1_with_project_environment_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`UV_PROJECT_ENVIRONMENT` が別の venv を指していても C-M1 を再現する（#81）。

    DSH の adapter は隔離した HOME の venv を `UV_PROJECT_ENVIRONMENT` に設定する。
    そこに `pytest` があると、venv の無い worktree の `uv run --no-sync pytest` が成功してしまっていた。
    """
    env = create_env_repo(tmp_path / "repo")
    worktree = create_owned_worktree(env.root, tmp_path / "wt")
    project_environment = tmp_path / "isolated-venv"
    # 隔離した venv にある pytest の代わり。起動されれば終了コード 0 で終わる
    fake_pytest = _make_venv(project_environment) / "pytest"
    fake_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pytest.chmod(0o755)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(project_environment))

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
    env = create_env_repo(tmp_path / "repo")
    worktree = create_owned_worktree(env.root, tmp_path / "wt")

    result = run_uv_offline(
        ("run", "--project", "tools/devenv", "pytest", "--version"),
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

    assert result.returncode != 0
    assert "Failed to download" in result.stderr or "Failed to spawn" in result.stderr


def test_run_uv_offline_runs_uv_found_only_in_a_venv_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`uv` が venv の bin にしか無い PATH でも起動する。venv の bin は子の PATH から除くので、
    `uv` は絞る前の PATH で解決する（#633）。"""
    real_uv = shutil.which("uv")
    assert real_uv is not None
    uv_bin = _make_venv(tmp_path / "uv-venv")
    (uv_bin / "uv").symlink_to(real_uv)
    monkeypatch.setenv("PATH", str(uv_bin))

    result = run_uv_offline(("--version",), cwd=tmp_path, cache_dir=tmp_path / "uv-cache")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("uv ")


def test_run_uv_offline_raises_when_uv_is_not_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        run_uv_offline(("--version",), cwd=tmp_path, cache_dir=tmp_path / "uv-cache")
