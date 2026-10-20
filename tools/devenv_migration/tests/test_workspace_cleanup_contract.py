"""定期 cleanup が保護対象を残し、保存済み helper session だけを reaper に渡すことを検証する。

- pr-review-* directory は経過日数・名前だけで削除しない。helper が完全な保存済み session と確認した場合のみ削除する。
- 未知の session state、追加ファイル、dirty/ignored worktree、loose pr-review-* file は保護して理由とパスを報告する。
- review-fix-planning.* と yarn の既存 cleanup、定期実行の間引きは維持する。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
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
ZSHRC = ROOT / ".devcontainer/zshrc.txt"
HELPER_SCRIPT = ROOT / ".agents/skills/pr-review/scripts/pr_review_workspace.py"
HELPER_SPEC = importlib.util.spec_from_file_location("pr_review_workspace", HELPER_SCRIPT)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
pr_review_workspace = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(pr_review_workspace)


def _make_old(path: Path, days: float = 2) -> None:
    old = time.time() - days * 24 * 60 * 60
    os.utime(path, (old, old))


def _run_cleanup(env_overrides: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(CLEANUP_SCRIPT)],
        env=env,
        check=False,
        text=True,
        capture_output=True,
        cwd=cwd,
    )


def _periodic_cleanup_function() -> str:
    lines = ZSHRC.read_text().splitlines(keepends=True)
    captured: list[str] = []
    depth = 0
    for line in lines:
        if not captured and not line.startswith("function xrift_poc_periodic_cleanup"):
            continue
        captured.append(line)
        depth += line.count("{") - line.count("}")
        if captured and depth == 0:
            break
    assert captured, "xrift_poc_periodic_cleanup not found in zshrc.txt"
    return "".join(captured)


def _run_periodic_cleanup(state_dir: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            f"export PATH={shlex.quote(str(bin_dir))}:$PATH",
            f"export XRIFT_POC_CLEANUP_STATE_DIR={shlex.quote(str(state_dir))}",
            _periodic_cleanup_function(),
            "xrift_poc_periodic_cleanup",
            "sleep 0.3",
        ]
    )
    return subprocess.run(["zsh", "-c", script], check=False, text=True, capture_output=True)


def test_periodic_cleanup_skips_while_the_stamp_is_fresh(tmp_path: Path) -> None:
    """前回の実行から 24 時間たっていなければ、シェルを開いても掃除を起動しない。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "cleanup-workspace.stamp").write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dummy = bin_dir / "cleanup-workspace.bash"
    dummy.write_text('#!/bin/bash\necho invoked > "${XRIFT_POC_CLEANUP_STATE_DIR}/invoked"\n')
    dummy.chmod(0o755)

    result = _run_periodic_cleanup(state_dir, bin_dir)

    assert result.returncode == 0, result.stderr
    assert not (state_dir / "invoked").exists()


def test_periodic_cleanup_recovers_stale_lock_and_runs(tmp_path: Path) -> None:
    """stamp が 24h 以上前なのに lock だけ残っているときは回収して掃除を再開する。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    stamp = state_dir / "cleanup-workspace.stamp"
    stamp.write_text("")
    _make_old(stamp)
    (state_dir / "cleanup-workspace.stamp.lock").mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dummy = bin_dir / "cleanup-workspace.bash"
    dummy.write_text('#!/bin/bash\necho invoked > "${XRIFT_POC_CLEANUP_STATE_DIR}/invoked"\n')
    dummy.chmod(0o755)

    result = _run_periodic_cleanup(state_dir, bin_dir)

    assert result.returncode == 0, result.stderr
    assert (state_dir / "invoked").is_file()


def test_periodic_cleanup_runs_once_when_two_shells_pass_the_age_gate(tmp_path: Path) -> None:
    """aged stamp で 2 シェルが同時にゲートを通っても掃除実体は 1 回だけ起動する。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    stamp = state_dir / "cleanup-workspace.stamp"
    stamp.write_text("")
    _make_old(stamp)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dummy = bin_dir / "cleanup-workspace.bash"
    dummy.write_text(
        "#!/bin/bash\n"
        'echo START >> "${XRIFT_POC_CLEANUP_STATE_DIR}/invocations"\n'
        "sleep 1.5\n"
        'echo END >> "${XRIFT_POC_CLEANUP_STATE_DIR}/invocations"\n'
    )
    dummy.chmod(0o755)

    script = "\n".join(
        [
            f"export PATH={shlex.quote(str(bin_dir))}:$PATH",
            f"export XRIFT_POC_CLEANUP_STATE_DIR={shlex.quote(str(state_dir))}",
            _periodic_cleanup_function(),
            "xrift_poc_periodic_cleanup",
            "sleep 2.2",
        ]
    )

    def _launch() -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["zsh", "-c", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    first = _launch()
    second = _launch()
    rc_first = first.wait(timeout=15)
    rc_second = second.wait(timeout=15)
    assert rc_first == 0, first.stderr.read() if first.stderr else ""
    assert rc_second == 0, second.stderr.read() if second.stderr else ""

    invocations = (state_dir / "invocations").read_text().splitlines()
    starts = [line for line in invocations if line.startswith("START")]
    assert len(starts) == 1, invocations


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


def _create_helper_session(
    isolated_root: Path,
    tmp_path: Path,
    *,
    save: bool = True,
    write_draft: bool = True,
    ignored_file: bool = False,
) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "helper-main-repo"
    _init_repo(repo)
    if ignored_file:
        (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add ignored file rule"], cwd=repo, check=True)
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
    draft = Path(created["draft"])
    if write_draft:
        draft.write_text("saved review draft\n", encoding="utf-8")
    if save:
        pr_review_workspace.save(argparse.Namespace(state=str(state_file)))
    _make_old(session_dir)
    return repo, session_dir, state_file, draft


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


def _init_repo_with_submodule(main_repo: Path, sub_src: Path) -> None:
    _init_repo(sub_src)
    _init_repo(main_repo)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(sub_src), "public"],
        cwd=main_repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "add public submodule"], cwd=main_repo, check=True)


def _init_submodule_in_worktree(worktree: Path) -> None:
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--quiet"],
        cwd=worktree,
        check=True,
    )


def _old_registered_submodule_worktree(isolated_root: Path, tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    main_repo = tmp_path / f"main-repo-{name}"
    _init_repo_with_submodule(main_repo, tmp_path / f"sub-src-{name}")
    review_dir = isolated_root / f"pr-review-999-{name}"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    _init_submodule_in_worktree(worktree)
    _make_old(review_dir)
    _make_old(worktree)
    return main_repo, review_dir, worktree


def test_reaps_saved_helper_session(isolated_root: Path, tmp_path: Path) -> None:
    repo, session_dir, state_file, _ = _create_helper_session(isolated_root, tmp_path)
    worktree = session_dir / "worktree"

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert not session_dir.exists()
    assert not worktree.exists()
    assert str(session_dir) in result.stdout
    assert "removed pr-review debris" in result.stdout
    assert not state_file.exists()
    assert _registered_worktrees(repo) == [str(repo)]


def test_installed_cleanup_launcher_reaps_saved_session_and_protects_unknown_files(
    isolated_root: Path, tmp_path: Path
) -> None:
    repo, session_dir, state_file, _ = _create_helper_session(isolated_root, tmp_path)
    unknown_dir = isolated_root / "pr-review-999-session"
    unknown_dir.mkdir()
    unknown_file = unknown_dir / "unique-user-content.txt"
    unknown_file.write_text("keep this directory\n", encoding="utf-8")
    _make_old(unknown_dir)
    loose_file = isolated_root / "pr-review-999-session.env"
    loose_file.write_text("unique loose content\n", encoding="utf-8")
    _make_old(loose_file)

    home = tmp_path / "home-without-agents"
    launcher_dir = home / "bin"
    launcher_dir.mkdir(parents=True)
    launcher = launcher_dir / "cleanup-workspace.bash"
    launcher.symlink_to(CLEANUP_SCRIPT.resolve())
    assert launcher.is_symlink()
    assert launcher.resolve() == CLEANUP_SCRIPT.resolve()
    assert not (home / ".agents").exists()

    env = os.environ.copy()
    env.update(_env_for(isolated_root, tmp_path / "no-yarn-cache"))
    env["HOME"] = str(home)
    dry = subprocess.run(
        [str(launcher)],
        env={**env, "XRIFT_POC_CLEANUP_DRY_RUN": "1"},
        check=False,
        text=True,
        capture_output=True,
        cwd=tmp_path,
    )

    assert dry.returncode == 0, dry.stderr
    assert session_dir.exists()
    assert state_file.exists()
    assert "dry-run: would remove pr-review debris" in dry.stdout
    assert str(session_dir) in dry.stdout
    assert unknown_file.read_text(encoding="utf-8") == "keep this directory\n"
    assert str(unknown_dir) in dry.stdout
    assert loose_file.read_text(encoding="utf-8") == "unique loose content\n"
    assert str(loose_file) in dry.stdout

    live = subprocess.run(
        [str(launcher)],
        env=env,
        check=False,
        text=True,
        capture_output=True,
        cwd=tmp_path,
    )

    assert live.returncode == 0, live.stderr
    assert not session_dir.exists()
    assert not state_file.exists()
    assert "removed pr-review debris" in live.stdout
    assert str(session_dir) in live.stdout
    assert unknown_file.read_text(encoding="utf-8") == "keep this directory\n"
    assert str(unknown_dir) in live.stdout
    assert loose_file.read_text(encoding="utf-8") == "unique loose content\n"
    assert str(loose_file) in live.stdout
    assert _registered_worktrees(repo) == [str(repo)]


def test_skips_copied_session_state(isolated_root: Path, tmp_path: Path) -> None:
    _, source_dir, state_file, _ = _create_helper_session(isolated_root, tmp_path)
    copied_dir = isolated_root / "pr-review-999-copied00"
    copied_dir.mkdir()
    copied_state = copied_dir / "session.json"
    copied_state.write_bytes(state_file.read_bytes())
    _make_old(copied_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert copied_dir.exists()
    assert copied_state.exists()
    assert "skip (unverifiable pr-review session)" in result.stdout
    assert str(copied_dir) in result.stdout
    assert not source_dir.exists()


def test_keeps_old_unverifiable_loose_file(isolated_root: Path, tmp_path: Path) -> None:
    loose = isolated_root / "pr-review-999-session.env"
    loose.write_text("unique user content\n", encoding="utf-8")
    _make_old(loose)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert loose.read_text(encoding="utf-8") == "unique user content\n"
    assert str(loose) in result.stdout
    assert "protected loose pr-review file" in result.stdout


def test_keeps_unknown_scratch_directory_suffixes(isolated_root: Path, tmp_path: Path) -> None:
    candidates = [
        isolated_root / "pr-review-999-session",
        isolated_root / "pr-review-999-progress",
        isolated_root / "pr-review-999-collect",
    ]
    for candidate in candidates:
        candidate.mkdir()
        (candidate / "user-data.txt").write_text("keep me\n", encoding="utf-8")
        _make_old(candidate)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    for candidate in candidates:
        assert (candidate / "user-data.txt").read_text(encoding="utf-8") == "keep me\n"
        assert str(candidate) in result.stdout
    assert "skip (unverifiable pr-review session)" in result.stdout


def test_keeps_unsaved_helper_session(isolated_root: Path, tmp_path: Path) -> None:
    _, session_dir, _, draft = _create_helper_session(isolated_root, tmp_path, save=False)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert session_dir.exists()
    assert draft.read_text(encoding="utf-8") == "saved review draft\n"
    assert "review destination does not hold the pinned draft" in result.stdout
    assert str(session_dir) in result.stdout


def test_keeps_saved_session_with_extra_file(isolated_root: Path, tmp_path: Path) -> None:
    _, session_dir, _, _ = _create_helper_session(isolated_root, tmp_path)
    extra = session_dir / "user-notes.txt"
    extra.write_text("unique note\n", encoding="utf-8")
    _make_old(session_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert extra.read_text(encoding="utf-8") == "unique note\n"
    assert "session contains unaccounted-for files" in result.stdout
    assert str(session_dir) in result.stdout


def test_dry_run_preserves_saved_session_then_live_cleanup_reaps_it(isolated_root: Path, tmp_path: Path) -> None:
    repo, session_dir, _, _ = _create_helper_session(isolated_root, tmp_path)
    worktree = session_dir / "worktree"
    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    dry_env = {**env, "XRIFT_POC_CLEANUP_DRY_RUN": "1"}

    dry = _run_cleanup(dry_env)
    assert dry.returncode == 0, dry.stderr
    assert session_dir.exists()
    assert worktree.exists()
    assert "dry-run: would remove pr-review debris" in dry.stdout
    assert "pr-review cleanup: removed=1 skipped=0" in dry.stdout

    live = _run_cleanup(env)
    assert live.returncode == 0, live.stderr
    assert not session_dir.exists()
    assert not worktree.exists()
    assert "removed pr-review debris" in live.stdout
    assert _registered_worktrees(repo) == [str(repo)]


def test_keeps_helper_session_with_dirty_worktree(isolated_root: Path, tmp_path: Path) -> None:
    _, session_dir, _, _ = _create_helper_session(isolated_root, tmp_path)
    secret = session_dir / "worktree" / "untracked-secret.txt"
    secret.write_text("retain this\n", encoding="utf-8")
    _make_old(session_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert secret.read_text(encoding="utf-8") == "retain this\n"
    assert "dirty, untracked, or ignored files" in result.stdout
    assert str(session_dir) in result.stdout


def test_keeps_helper_session_with_ignored_worktree_file(isolated_root: Path, tmp_path: Path) -> None:
    _, session_dir, _, _ = _create_helper_session(isolated_root, tmp_path, ignored_file=True)
    worktree = session_dir / "worktree"
    ignored = worktree / "ignored.log"
    ignored.write_text("retain ignored data\n", encoding="utf-8")
    _make_old(session_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert ignored.read_text(encoding="utf-8") == "retain ignored data\n"
    assert "dirty, untracked, or ignored files" in result.stdout
    assert str(session_dir) in result.stdout


def test_skips_symlink_pr_review_directory(isolated_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-session"
    outside.mkdir()
    marker = outside / "unique-user-data.txt"
    marker.write_text("keep outside\n", encoding="utf-8")
    candidate = isolated_root / "pr-review-999-symlink"
    candidate.symlink_to(outside, target_is_directory=True)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert candidate.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep outside\n"
    assert "symlink" in result.stdout
    assert str(candidate) in result.stdout


def test_keeps_old_unverifiable_pr_review_directory(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "pr-review-999-aaaaaaaa"
    (debris / "review-backup").mkdir(parents=True)
    (debris / "session.json").write_text("{}")
    _make_old(debris)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert debris.exists()
    assert (debris / "session.json").read_text() == "{}"
    assert str(debris) in result.stdout
    assert "unverifiable pr-review session" in result.stdout


def test_keeps_fresh_debris_directory_younger_than_min_age(isolated_root: Path, tmp_path: Path) -> None:
    fresh = isolated_root / "pr-review-999-bbbbbbbb"
    fresh.mkdir()
    # mtime is "now" (not aged)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert fresh.exists()
    assert "younger than" in result.stdout
    assert str(fresh) in result.stdout


def test_keeps_matching_pr_loose_file_when_saved_helper_session_is_reaped(isolated_root: Path, tmp_path: Path) -> None:
    _, session_dir, state_file, _ = _create_helper_session(isolated_root, tmp_path)
    loose_file = isolated_root / "pr-review-999-diff.patch"
    loose_file.write_text("unique matching-PR patch content\n", encoding="utf-8")
    _make_old(loose_file)
    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    dry_env = {**env, "XRIFT_POC_CLEANUP_DRY_RUN": "1"}

    dry = _run_cleanup(dry_env)

    assert dry.returncode == 0, dry.stderr
    assert session_dir.exists()
    assert state_file.exists()
    assert loose_file.read_text(encoding="utf-8") == "unique matching-PR patch content\n"
    assert "dry-run: would remove pr-review debris" in dry.stdout
    assert str(session_dir) in dry.stdout
    assert "protected loose pr-review file" in dry.stdout
    assert str(loose_file) in dry.stdout

    live = _run_cleanup(env)

    assert live.returncode == 0, live.stderr
    assert not session_dir.exists()
    assert not state_file.exists()
    assert loose_file.read_text(encoding="utf-8") == "unique matching-PR patch content\n"
    assert "removed pr-review debris" in live.stdout
    assert str(session_dir) in live.stdout
    assert "protected loose pr-review file" in live.stdout
    assert str(loose_file) in live.stdout


def test_keeps_old_loose_files_alongside_unverifiable_sibling_directory(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "pr-review-999-aaaaaaaa"
    debris.mkdir()
    _make_old(debris)
    patch_file = isolated_root / "pr-review-999-diff.patch"
    patch_file.write_text("diff\n")
    env_file = isolated_root / "pr-review-999-session.env"
    env_file.write_text("PR_NUMBER=999\n")
    _make_old(patch_file)
    _make_old(env_file)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert debris.exists()
    assert patch_file.read_text() == "diff\n"
    assert env_file.read_text() == "PR_NUMBER=999\n"
    assert str(patch_file) in result.stdout
    assert str(env_file) in result.stdout
    assert "protected loose pr-review file" in result.stdout


def test_keeps_fresh_loose_files_and_reports_them(isolated_root: Path, tmp_path: Path) -> None:
    patch_file = isolated_root / "pr-review-999-diff.patch"
    patch_file.write_text("fresh patch\n")

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert patch_file.read_text() == "fresh patch\n"
    assert str(patch_file) in result.stdout


def test_keeps_loose_files_while_a_sibling_directory_remains(isolated_root: Path, tmp_path: Path) -> None:
    still_active = isolated_root / "pr-review-999-bbbbbbbb"
    still_active.mkdir()
    patch_file = isolated_root / "pr-review-999-diff.patch"
    patch_file.write_text("diff\n")

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert still_active.exists()
    assert patch_file.read_text() == "diff\n"
    assert str(still_active) in result.stdout
    assert str(patch_file) in result.stdout


def test_keeps_old_loose_files_when_no_directory_ever_existed(isolated_root: Path, tmp_path: Path) -> None:
    patch_file = isolated_root / "pr-review-777-diff.patch"
    patch_file.write_text("diff\n")
    env_file = isolated_root / "pr-review-777-session.env"
    env_file.write_text("PR_NUMBER=777\n")
    _make_old(patch_file)
    _make_old(env_file)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert patch_file.read_text() == "diff\n"
    assert env_file.read_text() == "PR_NUMBER=777\n"
    assert str(patch_file) in result.stdout
    assert str(env_file) in result.stdout


def test_keeps_old_loose_files_with_dot_suffix(isolated_root: Path, tmp_path: Path) -> None:
    dotted = isolated_root / "pr-review-778.diff"
    dotted.write_text("diff\n")
    bare = isolated_root / "pr-review-779"
    bare.write_text("shorthead\n")
    _make_old(dotted)
    _make_old(bare)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert dotted.read_text() == "diff\n"
    assert bare.read_text() == "shorthead\n"
    assert str(dotted) in result.stdout
    assert str(bare) in result.stdout


def test_ignores_loose_files_that_do_not_match_the_pr_number_pattern(isolated_root: Path, tmp_path: Path) -> None:
    """`pr_number_of` の対象外パターンは PR 番号を取り出さず、削除されない。

    `pr_number_of` は「何を rm するかを決める唯一のゲート」であるため、
    正規表現を `^pr-review-([0-9]+)(.*)?$` のように緩めて
    `pr-review-777x` や `pr-review-777_a` まで PR 番号を取り出せるように
    しても検出できないと困る。緩和の実測は Task 3 の修正レポートを参照。
    """
    not_digit_boundary = isolated_root / "pr-review-777x"
    not_digit_boundary.write_text("x\n")
    underscore_suffix = isolated_root / "pr-review-777_a"
    underscore_suffix.write_text("x\n")
    non_numeric = isolated_root / "pr-review-abc"
    non_numeric.write_text("x\n")
    save_lock = isolated_root / "pr-review-save.lock"
    save_lock.write_text("x\n")
    for path in (not_digit_boundary, underscore_suffix, non_numeric, save_lock):
        _make_old(path)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert not_digit_boundary.exists()
    assert underscore_suffix.exists()
    assert non_numeric.exists()
    assert save_lock.exists()


def test_keeps_loose_session_files_while_a_directory_remains_without_removal(
    isolated_root: Path, tmp_path: Path
) -> None:
    """ディレクトリ削除が一切起きなくても、生存ディレクトリのある PR の
    loose ファイルには触れない（既存の安全条件の維持）。"""
    still_active = isolated_root / "pr-review-780-bbbbbbbb"
    still_active.mkdir()
    # mtime is "now": too young to remove, so nothing is deleted this run.
    patch_file = isolated_root / "pr-review-780-diff.patch"
    patch_file.write_text("diff\n")
    _make_old(patch_file)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert still_active.exists()
    assert patch_file.exists()
    assert str(patch_file) in result.stdout


def test_all_matching_loose_files_are_protected_independent_of_pr_number(isolated_root: Path, tmp_path: Path) -> None:
    still_active = isolated_root / "pr-review-790-bbbbbbbb"
    still_active.mkdir()
    patch_790 = isolated_root / "pr-review-790-diff.patch"
    patch_790.write_text("diff-790\n")
    _make_old(patch_790)
    patch_791 = isolated_root / "pr-review-791-diff.patch"
    patch_791.write_text("diff-791\n")
    _make_old(patch_791)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert still_active.exists()
    assert patch_790.read_text() == "diff-790\n"
    assert patch_791.read_text() == "diff-791\n"
    assert str(patch_790) in result.stdout
    assert str(patch_791) in result.stdout


def test_dry_run_reports_protected_loose_files_without_deleting(isolated_root: Path, tmp_path: Path) -> None:
    patch_file = isolated_root / "pr-review-782-diff.patch"
    patch_file.write_text("diff\n")
    _make_old(patch_file)

    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    result = _run_cleanup(env)

    assert result.returncode == 0, result.stderr
    assert patch_file.read_text() == "diff\n"
    assert "protected loose pr-review file" in result.stdout
    assert str(patch_file) in result.stdout


def test_dry_run_and_live_keep_unverified_directory_and_loose_file(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "pr-review-783-aaaaaaaa"
    debris.mkdir()
    _make_old(debris)
    patch_file = isolated_root / "pr-review-783-diff.patch"
    patch_file.write_text("diff\n")
    _make_old(patch_file)

    dry_env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    dry_env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    dry = _run_cleanup(dry_env)
    live = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert dry.returncode == 0, dry.stderr
    assert live.returncode == 0, live.stderr
    assert debris.exists()
    assert patch_file.read_text() == "diff\n"
    assert str(debris) in dry.stdout and str(debris) in live.stdout
    assert str(patch_file) in dry.stdout and str(patch_file) in live.stdout


def test_dry_run_and_live_retain_legacy_worktree_and_loose_file(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "pr-review-784-aaaaaaaa"
    debris.mkdir()
    (debris / "worktree").mkdir()
    _make_old(debris)
    patch_file = isolated_root / "pr-review-784-diff.patch"
    patch_file.write_text("diff\n")
    _make_old(patch_file)

    dry_env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    dry_env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    dry = _run_cleanup(dry_env)
    live = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert dry.returncode == 0, dry.stderr
    assert live.returncode == 0, live.stderr
    assert debris.exists()
    assert (debris / "worktree").exists()
    assert patch_file.read_text() == "diff\n"
    assert str(debris) in dry.stdout and str(debris) in live.stdout
    assert str(patch_file) in dry.stdout and str(patch_file) in live.stdout


def test_keeps_registered_old_worktree_without_helper_state(isolated_root: Path, tmp_path: Path) -> None:
    """登録済みでクリーンでも、helper state が無い worktree は自動削除しない。"""
    main_repo = tmp_path / "main-repo"
    _init_repo(main_repo)

    review_dir = isolated_root / "pr-review-999-cccccccc"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    _make_old(review_dir)
    _make_old(worktree)

    assert str(worktree) in _registered_worktrees(main_repo)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert review_dir.exists()
    assert worktree.exists()
    assert str(review_dir) in result.stdout
    assert "unverifiable pr-review session" in result.stdout
    assert main_repo.exists()
    assert str(worktree) in _registered_worktrees(main_repo)


def test_dry_run_keeps_unverified_registered_worktree(isolated_root: Path, tmp_path: Path) -> None:
    """dry-run でも helper state の無い worktree は削除候補として報告しない。"""
    main_repo = tmp_path / "main-repo-dry"
    _init_repo(main_repo)

    review_dir = isolated_root / "pr-review-999-dryrun0"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    _make_old(review_dir)
    _make_old(worktree)

    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    result = _run_cleanup(env)

    assert result.returncode == 0, result.stderr
    assert "would run: git worktree remove" not in result.stdout
    assert review_dir.exists()
    assert worktree.exists()
    assert str(review_dir) in result.stdout
    assert str(worktree) in _registered_worktrees(main_repo)


def test_keeps_registered_old_submodule_worktree_without_helper_state(isolated_root: Path, tmp_path: Path) -> None:
    """submodule を初期化した legacy worktree も helper state が無ければ保持する。"""
    main_repo, review_dir, worktree = _old_registered_submodule_worktree(isolated_root, tmp_path, "submodaa")
    assert (worktree / "public" / ".git").exists()
    assert str(worktree) in _registered_worktrees(main_repo)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert review_dir.exists()
    assert str(worktree) in _registered_worktrees(main_repo)
    assert str(review_dir) in result.stdout
    assert "unverifiable pr-review session" in result.stdout
    assert main_repo.exists()


def test_dry_run_and_live_preserve_unverified_submodule_worktree(isolated_root: Path, tmp_path: Path) -> None:
    main_repo, review_dir, worktree = _old_registered_submodule_worktree(isolated_root, tmp_path, "subdry01")
    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    dry_env = {**env, "XRIFT_POC_CLEANUP_DRY_RUN": "1"}

    dry = _run_cleanup(dry_env)
    assert dry.returncode == 0, dry.stderr
    assert review_dir.exists()
    assert str(worktree) in _registered_worktrees(main_repo)
    assert "pr-review cleanup: removed=0 skipped=1" in dry.stdout
    assert str(review_dir) in dry.stdout

    live = _run_cleanup(env)
    assert live.returncode == 0, live.stderr
    assert review_dir.exists()
    assert str(worktree) in _registered_worktrees(main_repo)
    assert "pr-review cleanup: removed=0 skipped=1" in live.stdout
    assert str(review_dir) in live.stdout


def test_keeps_directory_when_git_status_is_unverifiable(isolated_root: Path, tmp_path: Path) -> None:
    """status --porcelain が失敗する worktree は判定不能として残す（--force の前提）。"""
    main_repo = tmp_path / "main-repo-unreadable-index"
    _init_repo(main_repo)
    review_dir = isolated_root / "pr-review-999-statuscc"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    (worktree / "secret.txt").write_text("uncommitted-secret\n")
    _make_old(review_dir)
    _make_old(worktree)

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
        index.chmod(0o644)

    assert result.returncode == 0, result.stderr
    assert review_dir.exists()
    assert (worktree / "secret.txt").exists()
    assert str(review_dir) in result.stdout
    assert "unverifiable" in result.stdout or "skip" in result.stdout
    assert str(worktree) in _registered_worktrees(main_repo)


def test_keeps_directory_with_uncommitted_changes_inside_submodule(isolated_root: Path, tmp_path: Path) -> None:
    """submodule 内の未追跡ファイルは親の porcelain で見え、親ディレクトリは残す。"""
    main_repo, review_dir, worktree = _old_registered_submodule_worktree(isolated_root, tmp_path, "subdirty")
    (worktree / "public" / "secret.txt").write_text("dirty-in-submodule\n")

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert review_dir.exists()
    assert (worktree / "public" / "secret.txt").exists()
    assert str(review_dir) in result.stdout
    assert "unverifiable" in result.stdout or "skip" in result.stdout
    assert str(worktree) in _registered_worktrees(main_repo)


def test_keeps_directory_with_uncommitted_changes_in_worktree(isolated_root: Path, tmp_path: Path) -> None:
    main_repo = tmp_path / "main-repo-dirty"
    _init_repo(main_repo)

    review_dir = isolated_root / "pr-review-999-dddddddd"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    (worktree / "untracked.txt").write_text("dirty\n")
    _make_old(review_dir)
    _make_old(worktree)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert review_dir.exists()
    assert worktree.exists()
    assert str(worktree) in _registered_worktrees(main_repo)
    assert str(review_dir) in result.stdout


def test_skips_directory_currently_in_use_by_a_process(isolated_root: Path, tmp_path: Path) -> None:
    busy = isolated_root / "pr-review-999-eeeeeeee"
    busy.mkdir()
    _make_old(busy)

    proc = subprocess.Popen(["sleep", "20"], cwd=busy)
    try:
        time.sleep(0.3)
        result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))
        assert result.returncode == 0, result.stderr
        assert busy.exists(), "directory in use by a live process must not be removed"
        assert "in use by a running process" in result.stdout
        assert str(busy) in result.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_cleanup_repeatedly_reports_legacy_directory_as_protected(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "pr-review-999-ffffffff"
    debris.mkdir()
    _make_old(debris)
    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")

    first = _run_cleanup(env)
    assert first.returncode == 0
    assert debris.exists()
    assert str(debris) in first.stdout

    second = _run_cleanup(env)
    assert second.returncode == 0
    assert debris.exists()
    assert "pr-review cleanup: removed=0 skipped=1" in second.stdout
    assert "orphaned session file cleanup: protected=0" in second.stdout
    assert "review-fix-planning cleanup: removed=0" in second.stdout


def test_removes_yarn_cache_when_present(isolated_root: Path, tmp_path: Path) -> None:
    yarn_cache = tmp_path / "cache" / "yarn"
    yarn_cache.mkdir(parents=True)
    (yarn_cache / "some-package.tgz").write_bytes(b"x" * 1024)

    result = _run_cleanup(_env_for(isolated_root, yarn_cache))

    assert result.returncode == 0, result.stderr
    assert not yarn_cache.exists()
    assert "yarn cache" in result.stdout.lower()


def test_dry_run_reports_without_deleting(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "pr-review-999-99999999"
    debris.mkdir()
    _make_old(debris)
    yarn_cache = tmp_path / "cache" / "yarn"
    yarn_cache.mkdir(parents=True)
    (yarn_cache / "pkg.tgz").write_bytes(b"x")

    env = _env_for(isolated_root, yarn_cache)
    env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    result = _run_cleanup(env)

    assert result.returncode == 0, result.stderr
    assert debris.exists()
    assert yarn_cache.exists()
    assert str(debris) in result.stdout
    assert "skip" in result.stdout
    assert "would remove pr-review debris" not in result.stdout


def test_yarn_cache_absent_is_a_no_op(isolated_root: Path, tmp_path: Path) -> None:
    yarn_cache = tmp_path / "cache" / "does-not-exist"

    result = _run_cleanup(_env_for(isolated_root, yarn_cache))

    assert result.returncode == 0, result.stderr
    assert not yarn_cache.exists()


def test_removes_old_review_fix_planning_directory(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "review-fix-planning.AbCdEf"
    (debris / "scratch").mkdir(parents=True)
    (debris / "scratch" / "note.txt").write_text("tmp\n")
    _make_old(debris)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert not debris.exists()
    assert "removed review-fix-planning debris" in result.stdout
    assert str(debris) in result.stdout
    assert "review-fix-planning cleanup: removed=1" in result.stdout


def test_keeps_fresh_review_fix_planning_directory(isolated_root: Path, tmp_path: Path) -> None:
    fresh = isolated_root / "review-fix-planning.Fresh0"
    fresh.mkdir()

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert fresh.exists()
    assert "review-fix-planning cleanup: removed=0" in result.stdout


def test_releases_registered_review_fix_planning_worktree(isolated_root: Path, tmp_path: Path) -> None:
    main_repo = tmp_path / "main-repo-rfp"
    _init_repo(main_repo)

    review_dir = isolated_root / "review-fix-planning.Wt0001"
    review_dir.mkdir()
    worktree = review_dir / "worktree"
    _add_worktree(main_repo, worktree)
    _make_old(review_dir)
    _make_old(worktree)

    assert str(worktree) in _registered_worktrees(main_repo)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert "released registered worktree via git worktree remove" in result.stdout
    assert not review_dir.exists()
    assert not worktree.exists()
    assert str(worktree) not in _registered_worktrees(main_repo)
    assert _registered_worktrees(main_repo) == [str(main_repo)]


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


def test_keeps_review_fix_planning_worktree_with_item_files(isolated_root: Path, tmp_path: Path) -> None:
    """一時 worktree の items/<name>/ にファイルがあれば、日数条件を満たしても worktree ごと残す（Issue #5）。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "Items1")
    model = worktree / "items" / "my-item" / "model.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"glTF")
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert model.read_bytes() == b"glTF"
    assert str(worktree) in _registered_worktrees(main_repo)
    assert f"product data in items/ or worlds/): {worktree.resolve()}" in result.stderr
    assert str(review_dir) in result.stdout
    assert "review-fix-planning cleanup: removed=0 skipped=1" in result.stdout


def test_keeps_review_fix_planning_worktree_with_uncommitted_world_repository(
    isolated_root: Path, tmp_path: Path
) -> None:
    """worlds/<name>/ に git init した制作物の未コミットの作業があれば残す。"""
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
    assert "product data in items/ or worlds/" in result.stderr


def test_dry_run_keeps_review_fix_planning_worktree_with_product_data(isolated_root: Path, tmp_path: Path) -> None:
    """dry-run でも、制作物のデータがある worktree を消す予定として報告しない。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "DryPrd")
    scene = worktree / "worlds" / "my-world" / "scene.glb"
    scene.parent.mkdir(parents=True)
    scene.write_bytes(b"glTF")
    _make_old(review_dir)

    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    result = _run_cleanup(env)

    assert result.returncode == 0, result.stderr
    assert "would run: git worktree remove" not in result.stdout
    assert "would remove review-fix-planning debris" not in result.stdout
    assert scene.read_bytes() == b"glTF"
    assert str(worktree) in _registered_worktrees(main_repo)


def test_releases_review_fix_planning_worktree_with_empty_product_directories(
    isolated_root: Path, tmp_path: Path
) -> None:
    """items/・worlds/ が空（空のサブディレクトリだけ）なら、これまでどおり撤去する。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "Empty1")
    (worktree / "items").mkdir()
    (worktree / "worlds" / "my-world" / "public").mkdir(parents=True)
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert not review_dir.exists()
    assert _registered_worktrees(main_repo) == [str(main_repo)]


def test_releases_review_fix_planning_worktree_with_only_other_ignored_files(
    isolated_root: Path, tmp_path: Path
) -> None:
    """items/・worlds/ 以外の無視されたファイル（.venv/ など）だけなら、これまでどおり撤去する。"""
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
    """status.showUntrackedFiles=no の設定でも、items/ のデータを見落とさない。"""
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
    """status.showUntrackedFiles=no の設定でも、未追跡ファイルを --force で消さない。"""
    main_repo, review_dir, worktree = _rfp_worktree(isolated_root, tmp_path, "NoUnt2")
    subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=main_repo, check=True)
    secret = worktree / "untracked.txt"
    secret.write_text("dirty\n", encoding="utf-8")
    _make_old(review_dir)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert secret.read_text(encoding="utf-8") == "dirty\n"
    assert str(worktree) in _registered_worktrees(main_repo)
    assert "block (uncommitted changes)" in result.stderr


def test_dry_run_does_not_remove_review_fix_planning_directory(isolated_root: Path, tmp_path: Path) -> None:
    debris = isolated_root / "review-fix-planning.DryRun"
    debris.mkdir()
    _make_old(debris)

    env = _env_for(isolated_root, tmp_path / "no-yarn-cache")
    env["XRIFT_POC_CLEANUP_DRY_RUN"] = "1"
    result = _run_cleanup(env)

    assert result.returncode == 0, result.stderr
    assert debris.exists()
    assert "dry-run: would remove review-fix-planning debris" in result.stdout
    assert "review-fix-planning cleanup: removed=1" in result.stdout


def test_ignores_hyphenated_review_fix_planning_names(isolated_root: Path, tmp_path: Path) -> None:
    hyphen_dir = isolated_root / "review-fix-planning-wt"
    hyphen_dir.mkdir()
    head_txt = isolated_root / "review-fix-planning-head.txt"
    head_txt.write_text("/tmp/review-fix-planning-wt\n")
    _make_old(hyphen_dir)
    _make_old(head_txt)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert hyphen_dir.exists()
    assert head_txt.exists()


def test_ignores_review_fix_planning_regular_file(isolated_root: Path, tmp_path: Path) -> None:
    loose = isolated_root / "review-fix-planning.abcdef"
    loose.write_text("not a directory\n")
    _make_old(loose)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert loose.exists()
    assert "removed review-fix-planning debris" not in result.stdout


def test_review_fix_planning_removal_does_not_break_pr_review_survivor(isolated_root: Path, tmp_path: Path) -> None:
    rfp = isolated_root / "review-fix-planning.Surviv"
    rfp.mkdir()
    _make_old(rfp)
    still_active = isolated_root / "pr-review-801-bbbbbbbb"
    still_active.mkdir()
    patch_file = isolated_root / "pr-review-801-diff.patch"
    patch_file.write_text("diff\n")
    _make_old(patch_file)

    result = _run_cleanup(_env_for(isolated_root, tmp_path / "no-yarn-cache"))

    assert result.returncode == 0, result.stderr
    assert not rfp.exists()
    assert still_active.exists()
    assert patch_file.exists()
    assert "removed orphaned session file" not in result.stdout


def test_cleanup_leaves_work_directories_untouched(tmp_path: Path) -> None:
    # スクリプトは自身の位置から SCRIPT_ROOT（= root）を求める。root に配置したコピーを実行し、
    # root の items/・worlds/ と子リポジトリの未コミットの変更に触らないことを確かめる。
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
