from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

# Codex の中でもモデルを固定したテストが通るように、実行環境の CODEX_THREAD_ID を外す（#81）。
pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_codex")

SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_review_workspace.py"
SPEC = importlib.util.spec_from_file_location("pr_review_workspace", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
workspace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace)

THREAD_ID = "019fd23a-4723-7e02-83d9-58f32849f5ab"


def write_rollout(codex_home: Path, models: list[str], thread_id: str = THREAD_ID) -> Path:
    rollout = codex_home / "sessions" / "2026" / "08" / "05" / f"rollout-{thread_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("w", encoding="utf-8") as stream:
        for model in models:
            record = {
                "type": "turn_context",
                "payload": {"model": model},
            }
            stream.write(json.dumps(record) + "\n")
    return rollout


def write_rollout_text(codex_home: Path, text: str) -> Path:
    rollout = codex_home / "sessions" / "2026" / "08" / "05" / f"rollout-{THREAD_ID}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(text, encoding="utf-8")
    return rollout


def test_resolve_codex_model_name_uses_latest_turn_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """古いモデルを選ぶと、他のレビュアーの `<model>.md` を保存先に固定し、その前回のレビューを読んで上書きする。"""
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5", "gpt-5.6-sol"])
    (codex_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    assert workspace.resolve_codex_model_name() == "gpt-5.6-sol"


def test_resolve_codex_model_name_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同じ thread の rollout が 2 つあるとき 1 つを選んで進むと、別のセッションのモデルで保存先を黙って決める。"""
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    write_rollout(codex_home, ["gpt-5.6-sol"])
    write_rollout(codex_home / "sessions" / "2026" / "08" / "05" / "other", ["gpt-5.6-sol"])

    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_codex_model_name()


def test_resolve_codex_model_name_rejects_malformed_completed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改行まで書かれた不正な行を読み飛ばすと、最後の turn_context が壊れたとき古いモデルで保存先を黙って決める。"""
    codex_home = tmp_path / "codex"
    write_rollout_text(
        codex_home,
        '{"type":"turn_context","payload":{"model":"gpt-5.6-sol"}}\nnot-json\n',
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_codex_model_name()


def test_resolve_runtime_model_name_fails_closed_outside_codex() -> None:
    """末尾の空白を `-` にして通すと、保存先が `Composer-.md` に黙って移り、前回のレビューを引き継がない。"""
    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_runtime_model_name("Composer ", {})


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repo(path: Path, *, tracked_reviews: bool = True) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    git(path, "add", "README.md")
    if tracked_reviews:
        old_review = path / "reviews" / "#4-fix-herdr-session-cwd" / "gpt-5.md"
        old_review.parent.mkdir(parents=True)
        old_review.write_text("existing alias\n", encoding="utf-8")
        git(path, "add", "reviews/#4-fix-herdr-session-cwd/gpt-5.md")
    git(path, "commit", "-m", "fixture")
    return git(path, "rev-parse", "HEAD")


def create_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_submodule: bool = False,
) -> dict[str, Any]:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5.6-sol"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    repo = tmp_path / "repo"
    commit = make_repo(repo)
    if with_submodule:
        submodule_source = tmp_path / "submodule-source"
        make_repo(submodule_source, tracked_reviews=False)
        git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(submodule_source), "vendor")
        git(repo, "commit", "-am", "add submodule")
        commit = git(repo, "rev-parse", "HEAD")
    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=4,
            commit=commit,
            head_branch="fix/herdr-session-cwd",
            model_name=None,
            temp_root=str(tmp_path / "sessions"),
        )
    )
    return {"result": result, "codex_home": codex_home, "repo": repo}


@pytest.fixture
def created_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return create_session(tmp_path, monkeypatch)


def saved_session(session: dict[str, Any], body: str = "review\n") -> dict[str, str]:
    result = session["result"]
    Path(result["draft"]).write_text(body, encoding="utf-8")
    return workspace.save(argparse.Namespace(state=result["state_file"]))


def is_reapable(state_file: str | Path) -> bool:
    """定期削除の検査（reap-check）が受け付けるか。拒否のテストの対照に使う。"""
    return workspace.check_reap_candidate(argparse.Namespace(state=str(state_file))) == {"status": "eligible"}


def test_first_session_wins_same_destination(
    created_session: dict[str, Any],
) -> None:
    """保存先の指紋を照合しないと、同じ保存先への後のセッションの保存が、先のレビューを黙って上書きする。"""
    first = created_session["result"]
    repo = created_session["repo"]
    commit = git(repo, "rev-parse", "HEAD")
    second = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=4,
            commit=commit,
            head_branch="fix/herdr-session-cwd",
            model_name=None,
            temp_root=str(Path(first["state_file"]).parents[1]),
        )
    )
    Path(first["draft"]).write_text("first review\n", encoding="utf-8")
    Path(second["draft"]).write_text("second review\n", encoding="utf-8")

    workspace.save(argparse.Namespace(state=first["state_file"]))
    with pytest.raises(workspace.WorkspaceError):
        workspace.save(argparse.Namespace(state=second["state_file"]))

    destination = repo / first["destination"]
    assert destination.read_text(encoding="utf-8") == "first review\n"


def test_concurrent_save_keeps_the_first_review_and_losing_draft(
    created_session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """保存の検査と書込みをロックしないと、両セッションが成功して先のレビューと draft を黙って失う。"""
    first = created_session["result"]
    repo = created_session["repo"]
    second = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=4,
            commit=git(repo, "rev-parse", "HEAD"),
            head_branch="fix/herdr-session-cwd",
            model_name=None,
            temp_root=str(Path(first["state_file"]).parents[1]),
        )
    )
    first_body, second_body = "先のレビュー\n  保存する本文\n", "後のレビュー\n  未保存の本文\n"
    Path(first["draft"]).write_text(first_body, encoding="utf-8")
    Path(second["draft"]).write_text(second_body, encoding="utf-8")
    first_checked, proceed, first_finished = (threading.Event() for _ in range(3))
    role = threading.local()
    original_verify, original_flock = workspace.verify_state, workspace.fcntl.flock

    def verify(state: dict[str, Any], destination: Path) -> None:
        original_verify(state, destination)
        if state["draft"] == first["draft"]:
            first_checked.set()
            assert proceed.wait(10), "second saver never reached the lock or the destination check"
        else:
            # ロックが無い場合だけ、両検査を通してから first の保存・cleanup を先に終える。
            proceed.set()
            assert first_finished.wait(10), "first saver did not finish"

    def flock(fd: int, operation: int) -> None:
        if operation == workspace.fcntl.LOCK_EX and getattr(role, "second", False):
            try:
                original_flock(fd, operation | workspace.fcntl.LOCK_NB)
            except BlockingIOError:
                # 実ロックの競合を観測してから first を再開する。時間の長さでは推測しない。
                proceed.set()
                original_flock(fd, operation)
        else:
            original_flock(fd, operation)

    def save_and_cleanup(result: dict[str, Any], *, is_second: bool) -> bool:
        role.second = is_second
        try:
            try:
                workspace.save(argparse.Namespace(state=result["state_file"]))
            except workspace.WorkspaceError:
                return False
            workspace.cleanup(argparse.Namespace(state=result["state_file"]))
            return True
        finally:
            if not is_second:
                first_finished.set()

    monkeypatch.setattr(workspace, "verify_state", verify)
    monkeypatch.setattr(workspace.fcntl, "flock", flock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(save_and_cleanup, first, is_second=False)
        try:
            assert first_checked.wait(10), "first saver never checked the destination"
            loser = executor.submit(save_and_cleanup, second, is_second=True)
            assert winner.result(timeout=20)
            assert not loser.result(timeout=20), "both saves succeeded and discarded the first review/draft"
        finally:
            proceed.set()
            first_finished.set()

    assert (repo / first["destination"]).read_text(encoding="utf-8") == first_body
    assert not Path(first["state_file"]).parent.exists()
    assert Path(second["draft"]).read_text(encoding="utf-8") == second_body
    assert Path(second["worktree"]).is_dir()


def test_cleanup_refuses_before_save(created_session: dict[str, Any]) -> None:
    """保存先に draft が無いのに消すと、保存に失敗して残したセッション（draft だけが成果）を黙って消す。"""
    result = created_session["result"]
    repo = created_session["repo"]
    session_dir = Path(result["state_file"]).parent
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert session_dir.is_dir()
    assert Path(result["worktree"]).is_dir()
    assert result["worktree"] in git(repo, "worktree", "list")
    assert not (repo / result["destination"]).exists()


def test_cleanup_keeps_other_worktrees_and_main_checkout(tmp_path: Path, created_session: dict[str, Any]) -> None:
    """cleanup は毎回のレビューで走る。対象を取り違えると、他の worktree・main checkout・他のレビューを黙って消す。"""
    result = created_session["result"]
    repo = created_session["repo"]
    other = tmp_path / "unrelated-worktree"
    git(repo, "worktree", "add", "--detach", str(other), "HEAD")
    other_review = repo / "reviews/#4-fix-herdr-session-cwd/gemini-3.6-flash.md"
    other_review.parent.mkdir(parents=True, exist_ok=True)
    other_review.write_text("gemini review\n", encoding="utf-8")
    saved_session(created_session)

    workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert other.is_dir()
    assert str(other) in git(repo, "worktree", "list")
    assert (repo / "README.md").read_text(encoding="utf-8") == "fixture\n"
    assert other_review.read_text(encoding="utf-8") == "gemini review\n"


@pytest.mark.parametrize(
    "problem",
    [
        "ignored-worktree",
        "unknown-session-entry",
        "session-symlink",
        "untracked-worktree",
        "unknown-backup-entry",
        "state-symlink",
    ],
)
def test_reap_check_rejects_unverified_session_without_removing_data(
    tmp_path: Path,
    created_session: dict[str, Any],
    problem: str,
) -> None:
    """定期削除の検査が外れると、worktree の無視・未追跡ファイル、人が置いたファイル、symlink の先を黙って消す。"""
    result = created_session["result"]
    repo = created_session["repo"]
    state_file = Path(result["state_file"])
    session_dir = state_file.parent
    backup_dir = Path(result["backup_dir"])
    worktree = Path(result["worktree"])
    saved = saved_session(created_session)
    assert is_reapable(state_file)

    if problem == "unknown-session-entry":
        (session_dir / "operator-note.txt").write_text("keep me\n", encoding="utf-8")
    elif problem == "unknown-backup-entry":
        (backup_dir / "operator-note.txt").write_text("keep me\n", encoding="utf-8")
    elif problem == "untracked-worktree":
        (worktree / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    elif problem == "ignored-worktree":
        excludes = tmp_path / "git-excludes"
        excludes.write_text("build/\n", encoding="utf-8")
        git(worktree, "config", "core.excludesFile", str(excludes))
        build = worktree / "build"
        build.mkdir()
        (build / "artifact.log").write_text("keep me\n", encoding="utf-8")
    elif problem == "state-symlink":
        external_state = tmp_path / "external-session.json"
        external_state.write_bytes(state_file.read_bytes())
        state_file.unlink()
        state_file.symlink_to(external_state)
    else:
        external_session = tmp_path / "external-session"
        session_dir.rename(external_session)
        session_dir.symlink_to(external_session, target_is_directory=True)

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=str(state_file)))

    assert Path(saved["destination"]).exists()
    assert repo.is_dir()
    if problem == "session-symlink":
        assert session_dir.is_symlink()
        assert (tmp_path / "external-session" / "session.json").is_file()
        assert worktree.is_dir()
    else:
        assert state_file.exists() or state_file.is_symlink()
        assert session_dir.exists()
        assert worktree.exists()


@pytest.mark.parametrize("index_flag", ["--skip-worktree", "--assume-unchanged"])
def test_reap_check_rejects_flagged_modified_tracked_files(
    created_session: dict[str, Any],
    index_flag: str,
) -> None:
    """index の flag（2 つは別の条件）で status に出ない追跡ファイルの変更を、定期削除が黙って消す。"""
    result = created_session["result"]
    worktree = Path(result["worktree"])
    saved_session(created_session)
    assert is_reapable(result["state_file"])
    tracked_file = worktree / "README.md"

    git(worktree, "update-index", index_flag, "README.md")
    tracked_file.write_text("unsaved local work\n", encoding="utf-8")
    assert git(worktree, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(worktree, "ls-files", "--others").strip() == ""

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert tracked_file.read_text(encoding="utf-8") == "unsaved local work\n"
    assert worktree.is_dir()


@pytest.mark.parametrize("problem", ["ignored", "--skip-worktree"])
def test_reap_check_rejects_ignored_or_flagged_files_inside_initialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    """submodule の中の無視ファイルと index の flag の付いた変更（別の検査）を、定期削除が黙って消す。"""
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    repo = tmp_path / "repo-with-submodule"
    make_repo(repo, tracked_reviews=False)
    submodule_source = tmp_path / "submodule-source"
    make_repo(submodule_source, tracked_reviews=False)
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(submodule_source), "vendor")
    git(repo, "commit", "-am", "add submodule")
    commit = git(repo, "rev-parse", "HEAD")
    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=4,
            commit=commit,
            head_branch="fix/submodule",
            model_name="gpt-5.6-sol",
            temp_root=str(tmp_path / "sessions"),
        )
    )
    worktree = Path(result["worktree"])
    git(worktree, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
    saved_session({"result": result})
    assert is_reapable(result["state_file"])
    submodule = worktree / "vendor"
    artifact = submodule / "ignored-build" / "result.bin"
    tracked_file = submodule / "README.md"
    if problem == "ignored":
        ignored = tmp_path / "submodule-excludes"
        ignored.write_text("ignored-build/\n", encoding="utf-8")
        git(submodule, "config", "core.excludesFile", str(ignored))
        artifact.parent.mkdir()
        artifact.write_bytes(b"keep me")
    else:
        git(submodule, "update-index", problem, "README.md")
        tracked_file.write_text("unsaved submodule work\n", encoding="utf-8")
    assert git(submodule, "status", "--porcelain", "--untracked-files=all") == ""

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    if problem == "ignored":
        assert artifact.is_file()
    else:
        assert tracked_file.read_text(encoding="utf-8") == "unsaved submodule work\n"
    assert worktree.is_dir()


def test_strict_reap_rejects_files_inside_uninitialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未初期化の submodule の中は status も foreach も見ないので、検査が外れるとそのファイルを黙って消す。"""
    session = create_session(tmp_path, monkeypatch, with_submodule=True)
    result = session["result"]
    session_dir = Path(result["state_file"]).parent
    worktree = Path(result["worktree"])
    saved_session(session)
    assert is_reapable(result["state_file"])
    hidden = worktree / "vendor" / "hidden.txt"
    hidden.write_text("keep me\n", encoding="utf-8")
    assert git(worktree, "status", "--porcelain", "--untracked-files=all") == ""

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert hidden.read_text(encoding="utf-8") == "keep me\n"
    assert session_dir.is_dir()
    assert worktree.is_dir()


def test_reap_check_rejects_files_inside_nested_uninitialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """再帰の分岐が外れると、入れ子の未初期化の submodule の中のファイルを、定期削除が黙って消す。"""
    nested_source = tmp_path / "nested-source"
    make_repo(nested_source, tracked_reviews=False)
    session = create_session(tmp_path, monkeypatch, with_submodule=True)
    repo = session["repo"]
    submodule_source = tmp_path / "submodule-source"
    git(submodule_source, "-c", "protocol.file.allow=always", "submodule", "add", str(nested_source), "nested")
    git(submodule_source, "commit", "-am", "add nested submodule")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "update", "--remote", "vendor")
    git(repo, "commit", "-am", "bump submodule")
    workspace_root = tmp_path / "nested-sessions"
    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=4,
            commit=git(repo, "rev-parse", "HEAD"),
            head_branch="fix/nested-submodule",
            model_name=None,
            temp_root=str(workspace_root),
        )
    )
    worktree = Path(result["worktree"])
    git(worktree, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "vendor")
    saved_session({"result": result})
    assert is_reapable(result["state_file"])
    hidden = worktree / "vendor" / "nested" / "hidden.txt"
    hidden.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert hidden.read_text(encoding="utf-8") == "keep me\n"


# changed-destination は保存先と draft の不一致（`require_saved_review`）、untracked-after-check はロックの中で
# 検査し直すこと（`_validate_reapable_session`。不一致の検査だけでは見つからない）を確かめる
@pytest.mark.parametrize("change", ["changed-destination", "untracked-after-check"])
def test_strict_cleanup_refuses_state_changed_after_successful_reap_check(
    created_session: dict[str, Any],
    change: str,
) -> None:
    """reap-check の後に変わった保存先・置かれたファイルを検査し直さないと、事前の検査の結果のまま黙って消す。"""
    result = created_session["result"]
    session_dir = Path(result["state_file"]).parent
    worktree = Path(result["worktree"])
    saved = saved_session(created_session)
    assert is_reapable(result["state_file"])
    if change == "changed-destination":
        Path(saved["destination"]).write_text("changed after preflight\n", encoding="utf-8")
    else:
        (worktree / "late.txt").write_text("written after preflight\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))

    assert session_dir.is_dir()
    assert worktree.is_dir()
    if change == "changed-destination":
        assert Path(saved["destination"]).read_text(encoding="utf-8") == "changed after preflight\n"
    else:
        assert (worktree / "late.txt").read_text(encoding="utf-8") == "written after preflight\n"


# --- Issue #547: 候補一覧の JSON（review-findings v1） ---------------------------------------------------------

FINDINGS_MARKDOWN = """- 対象: PR #4 / head {head12} / 2026-10-05
- レビュアー: gpt-5.6-sol（ランタイム: codex）

## Review history

| Reviewed at (UTC) | HEAD | Mode |
|---|---|---|
| 2026-10-05T00:00:00Z | `{head12}` | Initial |

## Medium

### M-1: 例の指摘【検証済み】

- 位置: `app.py:1`
- 検出時 HEAD: `{head12}`
"""


def findings_document(commit: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "review-findings/v1",
        "kind": "candidates",
        "source": "reviewer",
        "pr": {"repository": "usaturn/xrift-poc", "number": 4},
        "reviewed_head": commit,
        "base": commit,
        "reviewer": {"model": "gpt-5.6-sol", "runtime": "codex", "review_file": "gpt-5.6-sol.md"},
        "skill": {"path": ".agents/skills/pr-review/SKILL.md", "sha256": "0" * 64},
        "timing": {"started_at": "2026-10-05T01:00:00Z", "finished_at": "2026-10-05T01:30:00Z", "seconds": 1800},
        "coverage": {"read": ["PR の差分全体"], "checks": [], "declined": []},
        "findings": [
            {
                "id": "M-1",
                "severity": "Medium",
                "confidence": "verified",
                "title": "例の指摘",
                "locations": ["app.py:1"],
                "detected_head": commit[:12],
                "result_kind": {"value": "maintainability"},
                "pr_relation": {"value": "introduced", "basis": "この PR が入れた"},
                "property": "例の性質",
                "variants": [],
                "statement_sites": [],
            }
        ],
        "unverified_candidates": [],
    }
    value.update(overrides)
    return value


# Issue #483: 保存先を親の reviews/<owner>/<repo>/ にする呼び方（create --target、state version 7）

TARGET_REPOSITORY = "usaturn/wabun-rst-ulint"
TARGET_MODEL = "gpt-5.6-sol"


def make_parent(path: Path, *, ignore_reviews: bool = True) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    (path / ".gitignore").write_text("/reviews/\n" if ignore_reviews else "/build/\n", encoding="utf-8")
    git(path, "add", ".gitignore")
    git(path, "commit", "-m", "parent")
    git(path, "remote", "add", "origin", "https://github.com/usaturn/xrift-poc.git")
    return path.resolve()


def make_remote(
    tmp_path: Path, *, name: str = "remote", number: int = 3, branch: str = "hogehoge"
) -> tuple[Path, str]:
    """bare remote に main と head ブランチを置き、refs/pull/<number>/head を head に向ける。"""
    work = tmp_path / f"{name}-work"
    work.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    git(work, "config", "user.name", "Test User")
    git(work, "config", "user.email", "test@example.com")
    (work / "README.md").write_text(f"{name}\n", encoding="utf-8")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "base")
    git(work, "switch", "-c", branch)
    (work / "change.txt").write_text("change\n", encoding="utf-8")
    git(work, "add", "change.txt")
    git(work, "commit", "-m", "change")
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    git(work, "push", "-q", str(remote), "main", branch, f"{branch}:refs/pull/{number}/head")
    return remote.resolve(), git(work, "rev-parse", "HEAD")


def redirect(monkeypatch: pytest.MonkeyPatch, remote: Path, repository: str) -> None:
    """GitHub の URL の fetch・clone をローカルの bare remote へ向ける（URL の文字列は変えない）。"""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"https://github.com/{repository}.git")

    def live_head(_repository: str, number: int) -> str:
        return git(remote, "rev-parse", f"refs/pull/{number}/head")

    monkeypatch.setattr(workspace, "current_pr_head", live_head, raising=False)


def target_json(
    parent: Path,
    head_sha: str,
    *,
    repository: str = TARGET_REPOSITORY,
    number: int = 3,
    head_ref: str = "hogehoge",
    target_root: Path | None = None,
) -> str:
    return json.dumps(
        {
            "parent_root": str(parent),
            "repository": repository,
            "number": number,
            "url": f"https://github.com/{repository}/pull/{number}",
            "head_ref": head_ref,
            "head_sha": head_sha,
            "head_repository": repository,
            "base_ref": "main",
            "state": "open",
            "clone_url": f"https://github.com/{repository}.git",
            "review_dir": str(parent / "reviews" / repository / f"#{number}-{head_ref}"),
            "target_root": str(target_root) if target_root is not None else None,
        }
    )


def create_target_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: str = TARGET_REPOSITORY,
    remote_name: str = "remote",
    target_root: Path | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    parent = tmp_path / "parent"
    if not parent.exists():
        make_parent(parent)
    remote = tmp_path / f"{remote_name}.git"
    if remote.exists():
        head = git(remote, "rev-parse", "refs/pull/3/head")
    else:
        remote, head = make_remote(tmp_path, name=remote_name)
    redirect(monkeypatch, remote, repository)
    result = workspace.create(
        argparse.Namespace(
            target=target_json(parent.resolve(), head_sha or head, repository=repository, target_root=target_root),
            model_name=TARGET_MODEL,
            temp_root=str(tmp_path / "sessions"),
        )
    )
    return {"result": result, "parent": parent.resolve(), "head": head, "remote": remote}


def test_target_update_reads_previous_review_and_cleanup_removes_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 回目のレビューに自分の前回のレビューを渡さないと、判断を引き継がない Initial のレビューを黙って作る。"""
    first = create_target_session(tmp_path, monkeypatch)
    Path(first["result"]["draft"]).write_text("first\n", encoding="utf-8")
    saved = workspace.save(argparse.Namespace(state=first["result"]["state_file"]))
    first_session = Path(first["result"]["state_file"]).parent
    cleaned = workspace.cleanup(argparse.Namespace(state=first["result"]["state_file"]))

    assert cleaned["pr_ref_status"] == "deleted"
    assert not first_session.exists()
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "first\n"

    second = create_target_session(tmp_path, monkeypatch)
    previous = second["result"]["previous_review"]
    assert previous is not None
    assert Path(previous).read_text(encoding="utf-8") == "first\n"


def test_target_reap_check_rejects_extra_files_in_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """専用 clone の中の検査（別の条件）が外れると、セッションの repo/ に置かれたファイルを定期削除が黙って消す。"""
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    workspace.save(argparse.Namespace(state=result["state_file"]))
    assert is_reapable(result["state_file"])
    notes = Path(result["state_file"]).parent / "repo" / "notes.txt"
    notes.write_text("keep\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert notes.read_text(encoding="utf-8") == "keep\n"


def local_commit_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], Path, str]:
    parent = make_parent(tmp_path / "parent")
    remote, head = make_remote(tmp_path)
    redirect(monkeypatch, remote, TARGET_REPOSITORY)
    source = tmp_path / "remote-work"
    git(source, "remote", "add", "origin", f"https://github.com/{TARGET_REPOSITORY}.git")
    (source / "local.txt").write_text("not pushed\n", encoding="utf-8")
    git(source, "add", "local.txt")
    git(source, "commit", "-m", "local-only change")
    commit = git(source, "rev-parse", "HEAD")
    absent = subprocess.run(["git", "-C", str(remote), "cat-file", "-e", commit], capture_output=True)
    assert absent.returncode != 0
    args = argparse.Namespace(
        target=target_json(parent, head),
        model_name=TARGET_MODEL,
        temp_root=str(tmp_path / "sessions"),
        review_commit=commit,
        source_root=str(source),
    )
    return {"args": args, "parent": parent, "head": head, "remote": remote}, source, commit


@pytest.mark.parametrize("failure", ["moved", "api"])
def test_target_save_refuses_moved_live_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """head が進んだのに保存すると古い head のレビューを最新として黙って残し、API の失敗を「変わっていない」と扱う。"""
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    def live_head(_repository: str, _number: int) -> str:
        if failure == "api":
            raise workspace.WorkspaceError("API unavailable")
        return "f" * 40

    monkeypatch.setattr(workspace, "current_pr_head", live_head, raising=False)
    with pytest.raises(workspace.WorkspaceError):
        workspace.save(argparse.Namespace(state=result["state_file"]))
    assert not (session["parent"] / result["destination"]).exists()
    assert Path(result["state_file"]).exists()


def test_target_commit_save_and_cleanup_keep_head_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """指定 commit のレビューの保存と後片付けが、同じモデルの head のレビューを黙って上書き・削除する。"""
    session, source, commit = local_commit_target(tmp_path, monkeypatch)
    directory = session["parent"] / f"reviews/{TARGET_REPOSITORY}/#3-hogehoge"
    directory.mkdir(parents=True)
    head_review = directory / f"{TARGET_MODEL}.md"
    head_review.write_text("live head review\n", encoding="utf-8")
    result = workspace.create(session["args"])
    document = findings_document(commit, pr={"repository": TARGET_REPOSITORY, "number": 3})
    document["reviewer"]["review_file"] = f"{TARGET_MODEL}-commit-{commit}.md"
    Path(result["draft"]).write_text(FINDINGS_MARKDOWN.format(head12=commit[:12]), encoding="utf-8")
    Path(result["findings_draft"]).write_text(json.dumps(document), encoding="utf-8")
    saved = workspace.save(argparse.Namespace(state=result["state_file"], findings=True))
    assert Path(saved["destination"]).name == document["reviewer"]["review_file"]
    assert Path(saved["findings_destination"]).name == f"{TARGET_MODEL}-commit-{commit}-{commit[:12]}.json"
    assert is_reapable(result["state_file"])
    cleaned = workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))
    assert cleaned["pr_ref_status"] == "deleted"
    assert head_review.read_text(encoding="utf-8") == "live head review\n"
    assert not Path(result["state_file"]).parent.exists()
    assert git(source, "rev-parse", "HEAD") == commit
