"""定期の掃除（`.devcontainer/cleanup-workspace.bash`）が、消してはいけないものを消さない仕様のテスト。

- `pr-review-*` のディレクトリは、helper が完全な保存済みのセッションと確かめたものだけを helper に消させる。
  名前・日数だけでは消さない。loose な `pr-review-*` ファイルは消さない
- `review-fix-planning.*` のディレクトリは、使用中・日数・dirty・`git status` の失敗・制作物（`items/`・`worlds/`）の
  検査を通ったものだけを消す
- checkout の `items/`・`worlds/` に触れない

呼び出し側（導入と `zshrc.txt` の定期実行）は掃除の失敗を表に出さないので、守るのは消しすぎの向きである。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

# Codex の中でもモデルを固定したテストが通るように、実行環境の CODEX_THREAD_ID を外す（#81）。
pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_codex")

ROOT = Path(__file__).resolve().parents[3]
CLEANUP_SCRIPT = ROOT / ".devcontainer/cleanup-workspace.bash"
HELPER_SCRIPT = ROOT / ".agents/skills/pr-review/scripts/pr_review_workspace.py"
HELPER_SPEC = importlib.util.spec_from_file_location("pr_review_workspace", HELPER_SCRIPT)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
pr_review_workspace = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(pr_review_workspace)


def _make_old(path: Path, days: float = 2) -> None:
    old = time.time() - days * 24 * 60 * 60
    os.utime(path, (old, old))


def _run_cleanup(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(CLEANUP_SCRIPT)],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


@pytest.fixture
def isolated_root(tmp_path: Path) -> Path:
    root = tmp_path / "tmp-root"
    root.mkdir()
    return root


def _env_for(root: Path, yarn_cache: Path, min_age_days: int = 1) -> dict[str, str]:
    return {
        "XRIFT_POC_CLEANUP_TMP_ROOT": str(root),
        "XRIFT_POC_CLEANUP_YARN_CACHE_DIR": str(yarn_cache),
        "XRIFT_POC_CLEANUP_MIN_AGE_DAYS": str(min_age_days),
    }


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _create_helper_session(isolated_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    """helper で保存済みの pr-review のセッションを古くして作り、(repo, session_dir, state_file) を返す。"""
    repo = tmp_path / "helper-main-repo"
    _init_repo(repo)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()
    created = pr_review_workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=999,
            commit=commit,
            head_branch="feature/periodic-cleanup",
            model_name="test-model",
            temp_root=str(isolated_root),
        )
    )
    session_dir = Path(created["state_file"]).parent
    state_file = Path(created["state_file"])
    Path(created["draft"]).write_text("saved review draft\n", encoding="utf-8")
    pr_review_workspace.save(argparse.Namespace(state=str(state_file)))
    _make_old(session_dir)
    return repo, session_dir, state_file


def _add_worktree(main_repo: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree)],
        cwd=main_repo,
        check=True,
    )


def _registered_worktrees(main_repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return [line.removeprefix("worktree ") for line in completed.stdout.splitlines() if line.startswith("worktree ")]


def test_installed_cleanup_launcher_reaps_saved_session_and_protects_unknown_files(
    isolated_root: Path, tmp_path: Path
) -> None:
    """名前で消す向きに壊れると、helper の状態の無いディレクトリと loose なファイルの利用者のデータを黙って消す。"""
    # pr-review の経路の対照も兼ねる: 導入が ~/bin に置く symlink から起動した掃除が helper を解決し、保存済みの
    # セッションを消す（helper を解決できない実装では何も消さず、保護の確認だけでは見分けられない）。
    repo, session_dir, _ = _create_helper_session(isolated_root, tmp_path)
    unknown_dir = isolated_root / "pr-review-999-session"
    unknown_dir.mkdir()
    unknown_file = unknown_dir / "unique-user-content.txt"
    unknown_file.write_text("keep this directory\n", encoding="utf-8")
    _make_old(unknown_dir)
    loose_file = isolated_root / "pr-review-999-session.env"
    loose_file.write_text("unique loose content\n", encoding="utf-8")
    _make_old(loose_file)

    home = tmp_path / "home-without-agents"
    launcher = home / "bin" / "cleanup-workspace.bash"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(CLEANUP_SCRIPT.resolve())
    env = os.environ.copy()
    env.update(_env_for(isolated_root, tmp_path / "no-yarn-cache"))
    env["HOME"] = str(home)
    result = subprocess.run([str(launcher)], env=env, check=False, text=True, capture_output=True, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert not session_dir.exists()
    assert _registered_worktrees(repo) == [str(repo)]
    assert unknown_file.read_text(encoding="utf-8") == "keep this directory\n"
    assert loose_file.read_text(encoding="utf-8") == "unique loose content\n"


def test_skips_copied_session_state(isolated_root: Path, tmp_path: Path) -> None:
    """session.json の写しのディレクトリを、検証できたセッションとして消す（#101 の 5 章: #141 の判定まで残す）。"""
    _, source_dir, state_file = _create_helper_session(isolated_root, tmp_path)
    copied_dir = isolated_root / "pr-review-999-copied00"
    copied_dir.mkdir()
    copied_state = copied_dir / "session.json"
    copied_state.write_bytes(state_file.read_bytes())
    _make_old(copied_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert copied_state.exists()
    assert not source_dir.exists()


def test_keeps_helper_session_with_dirty_worktree(isolated_root: Path, tmp_path: Path) -> None:
    """reap-check の失敗を無視し --strict-reap も外すと、保存済みのセッションの未追跡ファイルを --force で消す。"""
    _, session_dir, _ = _create_helper_session(isolated_root, tmp_path)
    secret = session_dir / "worktree" / "untracked-secret.txt"
    secret.write_text("retain this\n", encoding="utf-8")
    _make_old(session_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert secret.read_text(encoding="utf-8") == "retain this\n"


def test_keeps_matching_pr_loose_file_when_saved_helper_session_is_reaped(isolated_root: Path, tmp_path: Path) -> None:
    """同じ PR のセッションを消した後に loose なファイルを孤児として消すと、出所の分からない利用者のファイルを消す。"""
    _, session_dir, _ = _create_helper_session(isolated_root, tmp_path)
    loose_file = isolated_root / "pr-review-999-diff.patch"
    loose_file.write_text("unique matching-PR patch content\n", encoding="utf-8")
    _make_old(loose_file)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    # 同じ PR のディレクトリが 1 つも残っていない状態で、loose なファイルが残ることを確かめる。
    assert not session_dir.exists()
    assert loose_file.read_text(encoding="utf-8") == "unique matching-PR patch content\n"


def _init_parent_like_repo(path: Path) -> None:
    """親と同じく /items/・/worlds/・/.venv/ を無視するリポジトリを作る。"""
    _init_repo(path)
    (path / ".gitignore").write_text("/items/\n/worlds/\n/.venv/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ignore products"], cwd=path, check=True)


def _rfp_worktree(isolated_root: Path, tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    """review-fix-planning.<name>/worktree に親のような登録済み worktree を作る。

    中身を置いたあとで、呼び出し側が _make_old(review_dir) で日数条件を満たす。
    """
    main_repo = tmp_path / f"main-repo-{name}"
    _init_parent_like_repo(main_repo)
    review_dir = isolated_root / f"review-fix-planning.{name}"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    return main_repo, review_dir, worktree


def test_keeps_fresh_review_fix_planning_directory(isolated_root: Path, tmp_path: Path) -> None:
    """日数の判定が外れると、作ったばかりの作業中の一時ディレクトリを黙って消す。"""
    fresh = isolated_root / "review-fix-planning.Fresh0"
    fresh.mkdir()

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert fresh.exists()


def test_keeps_review_fix_planning_directory_in_use_by_a_process(isolated_root: Path, tmp_path: Path) -> None:
    """使用中の判定が外れると、日数を過ぎた作業中の一時ディレクトリ（検証ログなど）を黙って消す。"""
    busy = isolated_root / "review-fix-planning.InUse1"
    busy.mkdir()
    log = busy / "verify.log"
    log.write_text("検証のログ\n", encoding="utf-8")
    _make_old(busy)

    proc = subprocess.Popen(["sleep", "20"], cwd=busy)
    try:
        result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8") == "検証のログ\n"


def test_keeps_review_fix_planning_worktree_when_git_status_fails(isolated_root: Path, tmp_path: Path) -> None:
    """git status の失敗を握りつぶして clean と見なすと、未コミットのファイルを worktree remove --force で消す。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "Stat01")
    secret = worktree / "secret.txt"
    secret.write_text("uncommitted-secret\n", encoding="utf-8")
    _make_old(review_dir)
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=worktree,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    index = Path(git_dir) / "index"
    index.chmod(0o000)

    try:
        result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))
    finally:
        # worktree ごと消された場合は index も無い。
        if index.exists():
            index.chmod(0o644)

    assert result.returncode == 0, result.stderr
    assert secret.read_text(encoding="utf-8") == "uncommitted-secret\n"
    assert str(worktree) in _registered_worktrees(main_repo)


def test_keeps_review_fix_planning_worktree_with_uncommitted_world_repository(
    isolated_root: Path, tmp_path: Path
) -> None:
    """制作物の検査から worlds が外れると、worlds/ の独立したリポジトリ（find の深さの外）の未コミットの作業を消す。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "World1")
    world = worktree / "worlds" / "my-world"
    world.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(world)], check=True)
    draft = world / "下書き.txt"
    draft.write_text("未コミットの作業\n", encoding="utf-8")
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert draft.read_text(encoding="utf-8") == "未コミットの作業\n"
    assert str(worktree) in _registered_worktrees(main_repo)


def test_releases_review_fix_planning_worktree_with_only_other_ignored_files(
    isolated_root: Path, tmp_path: Path
) -> None:
    """rfp の経路の対照: 制作物の検査が .venv などまで広がると、一時 worktree を 1 つも消さなくなる。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "Venv01")
    venv_cfg = worktree / ".venv" / "pyvenv.cfg"
    venv_cfg.parent.mkdir()
    venv_cfg.write_text("home = /usr/bin\n", encoding="utf-8")
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert not review_dir.exists()
    assert _registered_worktrees(main_repo) == [str(main_repo)]


def test_keeps_item_files_when_untracked_files_are_hidden_by_config(isolated_root: Path, tmp_path: Path) -> None:
    """制作物の検査が status.showUntrackedFiles=no に左右されると、items/ のデータを --force で黙って消す（#5）。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "NoUnt1")
    subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=main_repo, check=True)
    model = worktree / "items" / "my-item" / "model.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"glTF")
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert model.read_bytes() == b"glTF"
    assert str(worktree) in _registered_worktrees(main_repo)


def test_keeps_untracked_file_when_untracked_files_are_hidden_by_config(isolated_root: Path, tmp_path: Path) -> None:
    """dirty の判定が status.showUntrackedFiles=no に左右されると、未追跡ファイルを --force で黙って消す。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "NoUnt2")
    subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=main_repo, check=True)
    secret = worktree / "untracked.txt"
    secret.write_text("dirty\n", encoding="utf-8")
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert secret.read_text(encoding="utf-8") == "dirty\n"
    assert str(worktree) in _registered_worktrees(main_repo)


def test_cleanup_leaves_work_directories_untouched(tmp_path: Path) -> None:
    """掃除が checkout の items/・worlds/ に触れる変更が入ると、Git 未初期化の制作物の未コミットの作業を黙って消す。"""
    # スクリプトは自身の位置から SCRIPT_ROOT（= root）を求める。root に配置したコピーを実行する。
    root = tmp_path / "root"
    script = root / ".devcontainer" / "cleanup-workspace.bash"
    script.parent.mkdir(parents=True)
    shutil.copy2(CLEANUP_SCRIPT, script)
    helper_dir = root / ".agents" / "skills" / "pr-review" / "scripts"
    helper_dir.parent.mkdir(parents=True)
    helper_dir.symlink_to(HELPER_SCRIPT.parent)
    world = root / "worlds" / "my-world"
    world.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(world)], check=True)
    (world / "draft.txt").write_text("未コミットの作業\n", encoding="utf-8")
    item = root / "items" / "my-item"
    item.mkdir(parents=True)
    (item / "model.glb").write_bytes(b"glTF")
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    old = tmp_root / "review-fix-planning.abc123"
    old.mkdir()
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    assert script.resolve().parent.parent == root.resolve()
    assert script.resolve() != CLEANUP_SCRIPT.resolve()

    env = os.environ.copy()
    env.update(
        {
            "XRIFT_POC_CLEANUP_TMP_ROOT": str(tmp_root),
            "XRIFT_POC_CLEANUP_MIN_AGE_DAYS": "0",
            "XRIFT_POC_CLEANUP_YARN_CACHE_DIR": str(tmp_path / "yarn"),
        }
    )
    result = subprocess.run(["bash", str(script)], env=env, check=False, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == before
    assert (world / "draft.txt").read_text(encoding="utf-8") == "未コミットの作業\n"
    assert (item / "model.glb").read_bytes() == b"glTF"
