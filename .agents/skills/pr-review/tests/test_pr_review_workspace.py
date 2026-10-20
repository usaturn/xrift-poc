from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.worktree_race import plant_racer

# Codex の中でもモデルを固定したテストが通るように、実行環境の CODEX_THREAD_ID を外す（#81）。
pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_codex")

SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_review_workspace.py"
SPEC = importlib.util.spec_from_file_location("pr_review_workspace", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
workspace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace)

THREAD_ID = "019fd23a-4723-7e02-83d9-58f32849f5ab"
# Allow Python startup and Git operations under full-suite load after releasing the lock.
POST_LOCK_TIMEOUT = 60


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
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5", "gpt-5.6-sol"])
    (codex_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    assert workspace.resolve_codex_model_name() == "gpt-5.6-sol"


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("missing-thread", "CODEX_THREAD_ID"),
        ("missing-rollout", "rollout"),
        ("missing-context", "turn_context"),
        ("unsafe-model", "safe filename"),
        ("non-unique-rollout", "exactly one"),
        ("invalid-json", "invalid rollout JSON"),
    ],
)
def test_resolve_codex_model_name_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    message: str,
) -> None:
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    if setup != "missing-thread":
        monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    if setup == "missing-context":
        rollout = write_rollout(codex_home, [])
        rollout.write_text('{"type":"event_msg","payload":{}}\n', encoding="utf-8")
    if setup == "unsafe-model":
        # `:` and other non-filename characters remain rejected after `/` → `-`
        # and whitespace → `-` sanitization.
        write_rollout(codex_home, ["gpt:5.6-sol"])
    if setup == "non-unique-rollout":
        write_rollout(codex_home, ["gpt-5.6-sol"])
        write_rollout(
            codex_home / "sessions" / "2026" / "08" / "05" / "other",
            ["gpt-5.6-sol"],
        )
    if setup == "invalid-json":
        write_rollout(codex_home, ["gpt-5.6-sol"]).write_text("this is not json\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.resolve_codex_model_name()


def test_resolve_codex_model_name_replaces_whitespace_with_hyphen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["Cursor Grok 4.6"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    assert workspace.resolve_codex_model_name() == "Cursor-Grok-4.6"


def test_resolve_codex_model_name_replaces_slash_with_hyphen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["opencode/deepseek-v4-flash-free"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    assert workspace.resolve_codex_model_name() == "opencode-deepseek-v4-flash-free"


def test_resolve_codex_model_name_ignores_unterminated_partial_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    complete = json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}})
    write_rollout_text(codex_home, complete + '\n{"type":"event_msg"')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    assert workspace.resolve_codex_model_name() == "gpt-5.6-sol"


@pytest.mark.parametrize(
    "unexpected_record",
    [
        123,
        ["turn_context"],
        {"type": "turn_context", "payload": None},
        {"type": "turn_context", "payload": []},
        {"type": "turn_context", "payload": {"model": None}},
    ],
)
def test_resolve_codex_model_name_skips_records_outside_expected_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unexpected_record: object,
) -> None:
    codex_home = tmp_path / "codex"
    lines = [
        json.dumps(unexpected_record),
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}),
    ]
    write_rollout_text(codex_home, "\n".join(lines) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    assert workspace.resolve_codex_model_name() == "gpt-5.6-sol"


def test_resolve_codex_model_name_rejects_malformed_completed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    write_rollout_text(
        codex_home,
        '{"type":"turn_context","payload":{"model":"gpt-5.6-sol"}}\nnot-json\n',
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    with pytest.raises(workspace.WorkspaceError, match="invalid rollout JSON at line 2"):
        workspace.resolve_codex_model_name()


def test_resolve_runtime_model_name_uses_explicit_name_outside_codex() -> None:
    assert workspace.resolve_runtime_model_name("opus-5", {}) == (
        "opus-5",
        "explicit",
    )


def test_resolve_runtime_model_name_replaces_slash_with_hyphen() -> None:
    assert workspace.resolve_runtime_model_name("opencode/deepseek-v4-flash-free", {}) == (
        "opencode-deepseek-v4-flash-free",
        "explicit",
    )


def test_resolve_runtime_model_name_replaces_whitespace_with_hyphen() -> None:
    assert workspace.resolve_runtime_model_name("Cursor Grok 4.6", {}) == (
        "Cursor-Grok-4.6",
        "explicit",
    )


@pytest.mark.parametrize(
    ("model_name", "message"),
    [
        (None, "model name is required outside Codex"),
        ("opus:5", "safe filename"),
        ("../evil", "safe filename"),
        (" Composer", "safe filename"),
        ("Composer ", "safe filename"),
    ],
)
def test_resolve_runtime_model_name_fails_closed_outside_codex(
    model_name: str | None,
    message: str,
) -> None:
    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.resolve_runtime_model_name(model_name, {})


def test_resolve_runtime_model_name_rejects_codex_override(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5.6-sol"])
    environ = {
        "CODEX_HOME": str(codex_home),
        "CODEX_THREAD_ID": THREAD_ID,
    }

    with pytest.raises(workspace.WorkspaceError, match="does not match Codex runtime"):
        workspace.resolve_runtime_model_name("gpt-5", environ)


def test_resolve_runtime_model_name_accepts_slash_equivalent_explicit_in_codex(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["opencode/deepseek-v4-flash-free"])
    environ = {
        "CODEX_HOME": str(codex_home),
        "CODEX_THREAD_ID": THREAD_ID,
    }

    assert workspace.resolve_runtime_model_name("opencode/deepseek-v4-flash-free", environ) == (
        "opencode-deepseek-v4-flash-free",
        "codex-rollout",
    )


def test_review_artifact_paths_preserve_exact_model_name(tmp_path: Path) -> None:
    draft, destination = workspace.review_artifact_paths(
        tmp_path / "review-backup",
        "fix/herdr-session-cwd",
        "gpt-5.6-sol",
        pr_number=4,
    )

    assert draft == tmp_path / "review-backup" / "gpt-5.6-sol.md"
    assert destination == "reviews/#4-fix-herdr-session-cwd/gpt-5.6-sol.md"


def test_review_artifact_paths_use_slash_sanitized_model_name(tmp_path: Path) -> None:
    draft, destination = workspace.review_artifact_paths(
        tmp_path / "review-backup",
        "fix/herdr-session-cwd",
        "opencode-deepseek-v4-flash-free",
        pr_number=4,
    )

    assert draft == (tmp_path / "review-backup" / "opencode-deepseek-v4-flash-free.md")
    assert destination == "reviews/#4-fix-herdr-session-cwd/opencode-deepseek-v4-flash-free.md"


def test_destination_fingerprint_distinguishes_symlink_entries(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("review\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target.name)
    dangling = tmp_path / "dangling.md"
    dangling.symlink_to("missing.md")

    target_fingerprint = workspace.destination_fingerprint(target)
    link_fingerprint = workspace.destination_fingerprint(link)
    dangling_fingerprint = workspace.destination_fingerprint(dangling)

    assert target_fingerprint["kind"] == "file"
    assert link_fingerprint["kind"] == "symlink"
    assert dangling_fingerprint["kind"] == "symlink"
    assert link_fingerprint["sha256"] != dangling_fingerprint["sha256"]


def test_snapshot_existing_review_copies_regular_file(tmp_path: Path) -> None:
    backup_dir = tmp_path / "review-backup"
    backup_dir.mkdir()
    destination = tmp_path / "reviews/branch/gpt-5.6-sol.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("existing review\n", encoding="utf-8")

    fingerprint, previous = workspace.snapshot_existing_review(destination, backup_dir)

    assert fingerprint == workspace.destination_fingerprint(destination)
    assert previous == backup_dir / "previous-review.md"
    assert previous.read_text(encoding="utf-8") == "existing review\n"


def test_snapshot_existing_review_reports_missing(tmp_path: Path) -> None:
    backup_dir = tmp_path / "review-backup"
    backup_dir.mkdir()
    destination = tmp_path / "reviews/branch/gpt-5.6-sol.md"

    fingerprint, previous = workspace.snapshot_existing_review(destination, backup_dir)

    assert fingerprint == {"path": str(destination), "kind": "missing"}
    assert previous is None
    assert list(backup_dir.iterdir()) == []


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_snapshot_existing_review_rejects_non_regular(tmp_path: Path, kind: str) -> None:
    backup_dir = tmp_path / "review-backup"
    backup_dir.mkdir()
    destination = tmp_path / "reviews/branch/gpt-5.6-sol.md"
    destination.parent.mkdir(parents=True)
    if kind == "directory":
        destination.mkdir()
    elif kind == "symlink":
        target = tmp_path / "other-model.md"
        target.write_text("must not be read\n", encoding="utf-8")
        destination.symlink_to(target)
    else:
        os.mkfifo(destination)

    with pytest.raises(
        workspace.WorkspaceError,
        match="existing review destination must be a regular file",
    ):
        workspace.snapshot_existing_review(destination, backup_dir)
    assert list(backup_dir.iterdir()) == []


def test_snapshot_existing_review_rejects_source_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_dir = tmp_path / "review-backup"
    backup_dir.mkdir()
    destination = tmp_path / "reviews/branch/gpt-5.6-sol.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("existing review\n", encoding="utf-8")
    original = workspace.destination_fingerprint
    calls = 0

    def changing(path: Path) -> dict[str, str]:
        nonlocal calls
        result = original(path)
        if path == destination:
            calls += 1
            if calls == 2:
                return {**result, "sha256": "0" * 64}
        return result

    monkeypatch.setattr(workspace, "destination_fingerprint", changing)
    with pytest.raises(
        workspace.WorkspaceError,
        match="existing review changed while snapshotting",
    ):
        workspace.snapshot_existing_review(destination, backup_dir)
    assert not (backup_dir / "previous-review.md").exists()


def test_atomic_copy_replaces_internal_destination_symlink_entry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    branch_dir = repo / "reviews" / "branch"
    branch_dir.mkdir(parents=True)
    target = branch_dir / "actual.md"
    target.write_text("old target\n", encoding="utf-8")
    link = branch_dir / "model.md"
    link.symlink_to(target.name)
    draft = tmp_path / "draft.md"
    draft.write_text("new review\n", encoding="utf-8")

    destination = workspace.safe_destination(repo, "reviews/branch/model.md")
    assert destination == link

    workspace.atomic_copy(draft, destination)

    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "new review\n"
    assert target.read_text(encoding="utf-8") == "old target\n"


def test_safe_destination_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    reviews = repo / "reviews"
    outside = repo / "outside"
    reviews.mkdir(parents=True)
    outside.mkdir()
    (reviews / "branch").symlink_to(outside, target_is_directory=True)

    with pytest.raises(workspace.WorkspaceError, match="escapes"):
        workspace.safe_destination(repo, "reviews/branch/model.md")


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


def test_create_requires_explicit_model_outside_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    temp_root = tmp_path / "sessions"

    with pytest.raises(workspace.WorkspaceError, match="required outside Codex"):
        workspace.create(
            argparse.Namespace(
                repo=str(tmp_path / "unused-repo"),
                pr_number=5,
                commit="unused-commit",
                head_branch="fix/model-name",
                model_name=None,
                temp_root=str(temp_root),
            )
        )

    assert not temp_root.exists()


def test_create_pins_slash_model_name_as_hyphenated_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    repo = tmp_path / "repo"
    commit = make_repo(repo)

    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=42,
            commit=commit,
            head_branch="fix/model-name",
            model_name="opencode/deepseek-v4-flash-free",
            temp_root=str(tmp_path / "sessions"),
        )
    )

    assert result["model_name"] == "opencode-deepseek-v4-flash-free"
    assert result["destination"] == "reviews/#42-fix-model-name/opencode-deepseek-v4-flash-free.md"
    assert result["draft"].endswith("/opencode-deepseek-v4-flash-free.md")


def test_create_destination_uses_hash_pr_number_and_sanitized_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    repo = tmp_path / "repo"
    commit = make_repo(repo, tracked_reviews=False)

    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=160,
            commit=commit,
            head_branch="feature/issue-148-fail-fast",
            model_name="grok-4.6",
            temp_root=str(tmp_path / "sessions"),
        )
    )

    assert result["destination"] == "reviews/#160-feature-issue-148-fail-fast/grok-4.6.md"


@pytest.mark.parametrize(
    ("head_branch", "directory"),
    [
        ("feature/foo:bar", "#7-feature-foo-bar"),
        (r"win\path", "#7-win-path"),
        ('a<>:"|?*b', "#7-a-------b"),
        ("ends-with-dot.", "#7-ends-with-dot"),
        ("trailing-space ", "#7-trailing-space"),
        ("has space", "#7-has space"),
        ("foo\nbar", "#7-foo-bar"),
    ],
)
def test_create_destination_replaces_unsafe_directory_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_branch: str,
    directory: str,
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    repo = tmp_path / "repo"
    commit = make_repo(repo, tracked_reviews=False)

    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=7,
            commit=commit,
            head_branch=head_branch,
            model_name="grok-4.6",
            temp_root=str(tmp_path / "sessions"),
        )
    )

    assert result["destination"] == f"reviews/{directory}/grok-4.6.md"


def test_create_pins_explicit_model_outside_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    repo = tmp_path / "repo"
    commit = make_repo(repo)

    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=5,
            commit=commit,
            head_branch="fix/model-name",
            model_name="opus-5",
            temp_root=str(tmp_path / "sessions"),
        )
    )

    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert result["model_name"] == "opus-5"
    assert result["model_source"] == "explicit"
    assert result["destination"] == "reviews/#5-fix-model-name/opus-5.md"
    assert state["version"] == 6
    assert state["model_source"] == "explicit"


def test_save_uses_pinned_explicit_model_without_codex_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    repo = tmp_path / "repo-save"
    commit = make_repo(repo)
    result = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=5,
            commit=commit,
            head_branch="fix/model-name",
            model_name="opus-5",
            temp_root=str(tmp_path / "save-sessions"),
        )
    )
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert saved["destination"].endswith("/reviews/#5-fix-model-name/opus-5.md")
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"


def test_save_rejects_unknown_model_source(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    state_file = Path(result["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["model_source"] = "unknown"
    state_file.write_text(json.dumps(state) + "\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="invalid PR-review session state"):
        workspace.save(argparse.Namespace(state=str(state_file)))


def test_create_pins_model_and_artifact_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5.6-sol"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    repo = tmp_path / "repo"
    commit = make_repo(repo)

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

    assert result["model_name"] == "gpt-5.6-sol"
    assert result["destination"] == "reviews/#4-fix-herdr-session-cwd/gpt-5.6-sol.md"
    assert Path(result["draft"]).name == "gpt-5.6-sol.md"
    assert (repo / "reviews/#4-fix-herdr-session-cwd/gpt-5.md").read_text(encoding="utf-8") == "existing alias\n"
    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert state["version"] == 6
    assert result["previous_review"] is None
    assert state["previous_review"] is None
    assert state["model_source"] == "codex-rollout"
    assert result["model_source"] == "codex-rollout"
    assert state["head_branch"] == "fix/herdr-session-cwd"
    assert state["model_name"] == "gpt-5.6-sol"
    assert state["draft"] == result["draft"]
    assert state["destination"] == result["destination"]


def test_create_pins_existing_review_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5.6-sol"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    repo = tmp_path / "repo"
    commit = make_repo(repo)
    own_review = repo / "reviews/#4-fix-herdr-session-cwd/gpt-5.6-sol.md"
    own_review.write_text("own existing review\n", encoding="utf-8")

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

    previous = Path(str(result["previous_review"]))
    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert previous.name == "previous-review.md"
    assert previous.read_text(encoding="utf-8") == "own existing review\n"
    assert state["version"] == 6
    assert state["previous_review"] == str(previous)
    assert state["destination_fingerprint"] == workspace.destination_fingerprint(own_review)


def test_load_state_rejects_tampered_previous_review(
    created_session: dict[str, Any],
) -> None:
    state_file = Path(created_session["result"]["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    outside = state_file.parent.parent / "another-review.md"
    outside.write_text("outside session\n", encoding="utf-8")
    state["previous_review"] = str(outside)
    state_file.write_text(json.dumps(state) + "\n", encoding="utf-8")

    with pytest.raises(
        workspace.WorkspaceError,
        match="previous review snapshot does not match its session",
    ):
        workspace.load_state(str(state_file))


@pytest.mark.parametrize("head_branch", ["", "/", ".", "..", "..."])
def test_review_artifact_paths_reject_invalid_branch(tmp_path: Path, head_branch: str) -> None:
    with pytest.raises(workspace.WorkspaceError, match="head branch"):
        workspace.review_artifact_paths(tmp_path, head_branch, "gpt-5.6-sol", pr_number=1)


@pytest.mark.parametrize("pr_number", [0, -1, True, "160"])
def test_review_artifact_paths_reject_invalid_pr_number(tmp_path: Path, pr_number: object) -> None:
    with pytest.raises(workspace.WorkspaceError, match="PR number"):
        workspace.review_artifact_paths(
            tmp_path,
            "feature/ok",
            "gpt-5.6-sol",
            pr_number=pr_number,  # type: ignore[arg-type]
        )


def test_save_parser_rejects_caller_selected_paths() -> None:
    with pytest.raises(SystemExit):
        workspace.parser().parse_args(
            [
                "save",
                "--state",
                "/tmp/session.json",
                "--draft",
                "/tmp/gpt-5.md",
                "--destination",
                "reviews/branch/gpt-5.md",
            ]
        )


def test_create_parser_accepts_explicit_model_name() -> None:
    args = workspace.parser().parse_args(
        [
            "create",
            "--repo",
            "/repo",
            "--pr-number",
            "5",
            "--commit",
            "deadbeef",
            "--head-branch",
            "fix/model-name",
            "--model-name",
            "opus-5",
        ]
    )

    assert args.model_name == "opus-5"


def create_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tracked_reviews: bool = True,
    with_submodule: bool = False,
) -> dict[str, Any]:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5.6-sol"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    repo = tmp_path / "repo"
    commit = make_repo(repo, tracked_reviews=tracked_reviews)
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


def test_save_uses_only_pinned_paths(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    draft = Path(result["draft"])
    draft.write_text("review\n", encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert saved["destination"].endswith("/reviews/#4-fix-herdr-session-cwd/gpt-5.6-sol.md")
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"


def test_save_allows_another_review_destination_to_change(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    Path(result["draft"]).write_text("codex review\n", encoding="utf-8")
    other = repo / "reviews/#4-fix-herdr-session-cwd/gemini-3.6-flash.md"
    other.write_text("gemini review\n", encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert Path(saved["destination"]).read_text(encoding="utf-8") == "codex review\n"
    assert other.read_text(encoding="utf-8") == "gemini review\n"


def test_save_allows_untracked_reviews_directory_to_appear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_session(tmp_path, monkeypatch, tracked_reviews=False)
    result = session["result"]
    repo = session["repo"]
    Path(result["draft"]).write_text("codex review\n", encoding="utf-8")
    other = repo / "reviews" / "#4-fix-herdr-session-cwd" / "gemini.md"
    other.parent.mkdir(parents=True)
    other.write_text("gemini review\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain") == "?? reviews/"

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert Path(saved["destination"]).read_text(encoding="utf-8") == "codex review\n"
    assert other.read_text(encoding="utf-8") == "gemini review\n"


@pytest.mark.parametrize("collision", ["reviews", "branch"])
def test_save_rejects_non_directory_destination_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    session = create_session(tmp_path, monkeypatch, tracked_reviews=False)
    result = session["result"]
    repo = session["repo"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    if collision == "reviews":
        (repo / "reviews").write_text("collision\n", encoding="utf-8")
    else:
        reviews = repo / "reviews"
        reviews.mkdir()
        (reviews / "#4-fix-herdr-session-cwd").write_text("collision\n", encoding="utf-8")

    with pytest.raises(
        workspace.WorkspaceError,
        match="review destination parent is not a directory",
    ):
        workspace.save(argparse.Namespace(state=result["state_file"]))


def test_save_rejects_changed_pinned_destination(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    Path(result["draft"]).write_text("new review\n", encoding="utf-8")
    destination = repo / result["destination"]
    destination.write_text("concurrent review\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="review destination changed"):
        workspace.save(argparse.Namespace(state=result["state_file"]))

    assert destination.read_text(encoding="utf-8") == "concurrent review\n"


def test_first_session_wins_same_destination(
    created_session: dict[str, Any],
) -> None:
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
    with pytest.raises(workspace.WorkspaceError, match="review destination changed"):
        workspace.save(argparse.Namespace(state=second["state_file"]))

    destination = repo / first["destination"]
    assert destination.read_text(encoding="utf-8") == "first review\n"


def test_save_allows_unrelated_main_changes(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    scratch = repo / ".pr-review-scratch"
    scratch.mkdir()
    (scratch / "diff.txt").write_text("scratch\n", encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"


def test_create_state_omits_main_snapshot(created_session: dict[str, Any]) -> None:
    state_file = Path(created_session["result"]["state_file"])

    state = json.loads(state_file.read_text(encoding="utf-8"))

    assert state["version"] == 6
    assert "main_snapshot" not in state


def test_load_state_rejects_previous_version(created_session: dict[str, Any]) -> None:
    state_file = Path(created_session["result"]["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["version"] = 5
    state_file.write_text(json.dumps(state) + "\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="invalid PR-review session state"):
        workspace.load_state(str(state_file))


def test_save_rejects_changed_runtime_model(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    codex_home = created_session["codex_home"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    write_rollout(codex_home, ["gpt-5.6-sol", "gpt-5"])

    with pytest.raises(workspace.WorkspaceError, match="runtime model changed"):
        workspace.save(argparse.Namespace(state=result["state_file"]))


def test_save_rejects_tampered_destination(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    state_file = Path(result["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["destination"] = "reviews/#4-fix-herdr-session-cwd/gpt-5.md"
    state_file.write_text(json.dumps(state) + "\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="destination does not match"):
        workspace.save(argparse.Namespace(state=str(state_file)))


def test_save_waits_for_repository_review_lock(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    destination = repo / result["destination"]

    with workspace.review_save_lock(repo):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "save", "--state", result["state_file"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        assert not destination.exists()

    stdout, stderr = process.communicate(timeout=POST_LOCK_TIMEOUT)
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["destination"] == str(destination)
    assert destination.read_text(encoding="utf-8") == "review\n"


def saved_session(session: dict[str, Any], body: str = "review\n") -> dict[str, str]:
    result = session["result"]
    Path(result["draft"]).write_text(body, encoding="utf-8")
    return workspace.save(argparse.Namespace(state=result["state_file"]))


def test_cleanup_removes_session_after_successful_save(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    session_dir = Path(result["state_file"]).parent
    saved = saved_session(created_session)

    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert removed["session_dir"] == str(session_dir)
    assert removed["destination"] == saved["destination"]
    assert not session_dir.exists()
    assert result["worktree"] not in git(repo, "worktree", "list")
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"


def test_cleanup_removes_worktree_holding_verification_artifacts(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    worktree = Path(result["worktree"])
    build = worktree / "_build"
    build.mkdir()
    (build / "pytest.log").write_text("verification output\n", encoding="utf-8")
    (worktree / "README.md").write_text("edited during review\n", encoding="utf-8")
    saved_session(created_session)

    workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert not worktree.exists()
    assert result["worktree"] not in git(repo, "worktree", "list")


def test_cleanup_refuses_before_save(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    session_dir = Path(result["state_file"]).parent
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="not saved"):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert session_dir.is_dir()
    assert Path(result["worktree"]).is_dir()
    assert result["worktree"] in git(repo, "worktree", "list")
    assert not (repo / result["destination"]).exists()


def test_cleanup_refuses_when_destination_diverges_from_draft(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    session_dir = Path(result["state_file"]).parent
    saved = saved_session(created_session)
    Path(saved["destination"]).write_text("hand edited\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="not saved"):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert session_dir.is_dir()
    assert result["worktree"] in git(repo, "worktree", "list")
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "hand edited\n"


def test_cleanup_keeps_other_worktrees_and_main_checkout(tmp_path: Path, created_session: dict[str, Any]) -> None:
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


def test_cleanup_refuses_when_session_path_is_not_its_worktree(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    worktree = Path(result["worktree"])
    saved_session(created_session)
    git(repo, "worktree", "remove", "--force", str(worktree))
    worktree.mkdir()
    (worktree / "precious.txt").write_text("not a worktree\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="linked worktree"):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert (worktree / "precious.txt").is_file()
    assert Path(result["state_file"]).is_file()


def test_cleanup_refuses_worktree_of_another_repository(tmp_path: Path, created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    worktree = Path(result["worktree"])
    saved_session(created_session)
    git(repo, "worktree", "remove", "--force", str(worktree))
    stranger = tmp_path / "stranger"
    stranger_commit = make_repo(stranger, tracked_reviews=False)
    git(stranger, "worktree", "add", "--detach", str(worktree), stranger_commit)

    with pytest.raises(workspace.WorkspaceError, match="linked worktree"):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert worktree.is_dir()
    assert str(worktree) in git(stranger, "worktree", "list")


def test_cleanup_fails_closed_when_rerun(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    saved = saved_session(created_session)
    workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    with pytest.raises((workspace.WorkspaceError, OSError)):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"


def test_cleanup_parser_rejects_caller_selected_paths() -> None:
    with pytest.raises(SystemExit):
        workspace.parser().parse_args(
            [
                "cleanup",
                "--state",
                "/tmp/session.json",
                "--worktree",
                "/tmp/pr-review-4-abcd/worktree",
            ]
        )


def test_cleanup_waits_for_repository_review_lock(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    session_dir = Path(result["state_file"]).parent
    saved_session(created_session)

    with workspace.review_save_lock(repo):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "cleanup", "--state", result["state_file"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        assert session_dir.is_dir()

    stdout, stderr = process.communicate(timeout=POST_LOCK_TIMEOUT)
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["session_dir"] == str(session_dir)
    assert not session_dir.exists()


def test_pinned_review_ref_derives_only_pr_review_namespace() -> None:
    assert workspace.pinned_review_ref(4) == "refs/pr-review/4"

    for invalid in (0, -1, "4", "refs/heads/main", None):
        with pytest.raises(workspace.WorkspaceError, match="invalid PR number"):
            workspace.pinned_review_ref(invalid)


def test_cleanup_can_explicitly_preserve_unowned_pinned_review_ref(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", commit)
    saved_session(created_session)

    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"], preserve_unowned_ref=True))

    assert removed["pr_ref_status"] == "preserved-unowned"
    assert git(repo, "rev-parse", "refs/pr-review/4") == commit
    assert not Path(result["worktree"]).exists()


def test_create_uses_only_a_preclaimed_session_directory(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    session_dir = Path(result["state_file"]).parent.parent / "pr-review-4-abcdefgh"
    commit = git(repo, "rev-parse", "HEAD")

    created = workspace.create(
        argparse.Namespace(
            repo=str(repo),
            pr_number=4,
            commit=commit,
            head_branch="feature",
            model_name=None,
            temp_root=str(session_dir.parent),
            session_dir=str(session_dir),
            preserve_unowned_ref=True,
        )
    )

    state = json.loads(Path(created["state_file"]).read_text(encoding="utf-8"))
    assert Path(created["state_file"]).parent == session_dir
    assert state["preserve_unowned_ref"] is True


def test_cleanup_removes_pinned_review_ref(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", commit)
    saved_session(created_session)

    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert removed["pr_ref"] == "refs/pr-review/4"
    assert removed["pr_ref_status"] == "deleted"
    assert git(repo, "for-each-ref", "refs/pr-review") == ""


def test_cleanup_keeps_session_when_pinned_ref_delete_fails(
    created_session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", commit)
    saved_session(created_session)
    original_git = workspace.git

    def fail_delete(path: Path, *args: str, binary: bool = False) -> str | bytes:
        if args[:2] == ("update-ref", "-d"):
            raise workspace.WorkspaceError("ref lock held")
        return original_git(path, *args, binary=binary)

    monkeypatch.setattr(workspace, "git", fail_delete)
    with pytest.raises(workspace.WorkspaceError, match="ref lock held"):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert Path(result["worktree"]).exists()
    assert Path(result["state_file"]).exists()
    assert git(repo, "rev-parse", "refs/pr-review/4") == commit

    monkeypatch.setattr(workspace, "git", original_git)
    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"]))
    assert removed["pr_ref_status"] == "deleted"
    assert not Path(result["state_file"]).exists()
    assert not Path(result["worktree"]).exists()


def test_cleanup_keeps_ref_repinned_by_another_session(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    first_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", first_commit)
    (repo / "README.md").write_text("updated head\n", encoding="utf-8")
    git(repo, "commit", "-am", "updated head")
    new_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", new_commit)
    saved_session(created_session)

    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert removed["pr_ref_status"] == "repinned"
    assert git(repo, "rev-parse", "refs/pr-review/4") == new_commit
    assert not Path(result["state_file"]).exists()
    assert result["worktree"] not in git(repo, "worktree", "list")


def test_cleanup_preserves_ref_changed_during_delete(
    created_session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    original_commit = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("new head\n", encoding="utf-8")
    git(repo, "commit", "-am", "new head")
    new_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", original_commit)
    saved_session(created_session)
    original_git = workspace.git

    def repin_before_delete(path: Path, *args: str, binary: bool = False) -> str | bytes:
        if args[:2] == ("update-ref", "-d"):
            git(repo, "update-ref", "refs/pr-review/4", new_commit)
        return original_git(path, *args, binary=binary)

    monkeypatch.setattr(workspace, "git", repin_before_delete)
    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert removed["pr_ref_status"] == "repinned"
    assert git(repo, "rev-parse", "refs/pr-review/4") == new_commit
    assert not Path(result["state_file"]).exists()


def test_cleanup_succeeds_when_pinned_review_ref_is_missing(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    saved_session(created_session)

    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert removed["pr_ref"] == "refs/pr-review/4"
    assert removed["pr_ref_status"] == "missing"
    assert git(repo, "for-each-ref", "refs/pr-review") == ""


def test_cleanup_removes_only_own_pr_review_ref(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", commit)
    git(repo, "update-ref", "refs/pr-review/77", commit)
    saved_session(created_session)

    workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert git(repo, "for-each-ref", "--format=%(refname)", "refs/pr-review") == "refs/pr-review/77"
    assert git(repo, "rev-parse", "--verify", "refs/heads/main")


def test_cleanup_refusal_before_save_keeps_pinned_ref(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pr-review/4", commit)
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="not saved"):
        workspace.cleanup(argparse.Namespace(state=result["state_file"]))

    assert git(repo, "rev-parse", "--verify", "refs/pr-review/4") == commit


def test_reap_check_accepts_saved_clean_session_without_changing_it(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    session_dir = Path(result["state_file"]).parent
    saved = saved_session(created_session)
    entries_before = sorted(path.name for path in session_dir.iterdir())

    checked = workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert checked == {"status": "eligible"}
    assert sorted(path.name for path in session_dir.iterdir()) == entries_before
    assert Path(result["worktree"]).is_dir()
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"


@pytest.mark.parametrize(
    "problem",
    [
        "changed-destination",
        "copied-state",
        "unknown-session-entry",
        "unknown-backup-entry",
        "dirty-worktree",
        "untracked-worktree",
        "ignored-worktree",
        "state-symlink",
        "backup-symlink",
        "worktree-symlink",
        "draft-symlink",
        "session-symlink",
    ],
)
def test_reap_check_rejects_unverified_session_without_removing_data(
    tmp_path: Path,
    created_session: dict[str, Any],
    problem: str,
) -> None:
    result = created_session["result"]
    repo = created_session["repo"]
    state_file = Path(result["state_file"])
    session_dir = state_file.parent
    backup_dir = Path(result["backup_dir"])
    worktree = Path(result["worktree"])
    saved = saved_session(created_session)

    if problem == "changed-destination":
        Path(saved["destination"]).write_text("hand-edited review\n", encoding="utf-8")
    elif problem == "copied-state":
        copied_session = session_dir.parent / "pr-review-4-abcdefgh"
        copied_session.mkdir()
        copied_state = copied_session / "session.json"
        copied_state.write_bytes(state_file.read_bytes())
        state_file = copied_state
    elif problem == "unknown-session-entry":
        (session_dir / "operator-note.txt").write_text("keep me\n", encoding="utf-8")
    elif problem == "unknown-backup-entry":
        (backup_dir / "operator-note.txt").write_text("keep me\n", encoding="utf-8")
    elif problem == "dirty-worktree":
        (worktree / "README.md").write_text("edited\n", encoding="utf-8")
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
    elif problem in {"backup-symlink", "worktree-symlink"}:
        target = tmp_path / f"external-{problem}"
        path = backup_dir if problem == "backup-symlink" else worktree
        path.rename(target)
        path.symlink_to(target, target_is_directory=True)
    elif problem == "draft-symlink":
        draft = Path(result["draft"])
        external_draft = tmp_path / "external-draft.md"
        external_draft.write_bytes(draft.read_bytes())
        draft.unlink()
        draft.symlink_to(external_draft)
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
    result = created_session["result"]
    worktree = Path(result["worktree"])
    saved_session(created_session)
    tracked_file = worktree / "README.md"

    git(worktree, "update-index", index_flag, "README.md")
    tracked_file.write_text("unsaved local work\n", encoding="utf-8")
    assert git(worktree, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(worktree, "ls-files", "--others").strip() == ""

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert tracked_file.read_text(encoding="utf-8") == "unsaved local work\n"
    assert worktree.is_dir()


@pytest.mark.parametrize("problem", ["ignored", "--skip-worktree", "--assume-unchanged"])
def test_reap_check_rejects_ignored_or_flagged_files_inside_initialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
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
    saved_session({"result": result})

    with pytest.raises(workspace.WorkspaceError):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    if problem == "ignored":
        assert artifact.is_file()
    else:
        assert tracked_file.read_text(encoding="utf-8") == "unsaved submodule work\n"
    assert worktree.is_dir()


def test_reap_check_accepts_empty_uninitialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = create_session(tmp_path, monkeypatch, with_submodule=True)
    result = session["result"]
    saved_session(session)

    checked = workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert checked == {"status": "eligible"}


@pytest.mark.parametrize("entry_point", ["reap-check", "strict-cleanup"])
def test_strict_reap_rejects_files_inside_uninitialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
) -> None:
    session = create_session(tmp_path, monkeypatch, with_submodule=True)
    result = session["result"]
    session_dir = Path(result["state_file"]).parent
    worktree = Path(result["worktree"])
    saved_session(session)
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}
    hidden = worktree / "vendor" / "hidden.txt"
    hidden.write_text("keep me\n", encoding="utf-8")
    assert git(worktree, "status", "--porcelain", "--untracked-files=all") == ""

    with pytest.raises(workspace.WorkspaceError, match="uninitialized submodule"):
        if entry_point == "reap-check":
            workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))
        else:
            workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))

    assert hidden.read_text(encoding="utf-8") == "keep me\n"
    assert session_dir.is_dir()
    assert worktree.is_dir()


def test_reap_check_rejects_files_inside_nested_uninitialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}
    hidden = worktree / "vendor" / "nested" / "hidden.txt"
    hidden.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="uninitialized submodule"):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))

    assert hidden.read_text(encoding="utf-8") == "keep me\n"


def test_strict_cleanup_reports_malformed_destination_without_traceback(
    created_session: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = created_session["result"]
    state_file = Path(result["state_file"])
    session_dir = state_file.parent
    saved_session(created_session)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["destination"] = 123
    state_file.write_text(json.dumps(state), encoding="utf-8")

    exit_code = workspace.main(["cleanup", "--strict-reap", "--state", str(state_file)])

    assert exit_code == 2
    assert "invalid or incomplete PR-review session" in capsys.readouterr().err
    assert session_dir.is_dir()
    assert Path(result["worktree"]).is_dir()


def test_reap_check_parser_accepts_state_and_reports_eligibility(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    saved_session(created_session)

    args = workspace.parser().parse_args(["reap-check", "--state", result["state_file"]])

    assert args.handler(args) == {"status": "eligible"}


def test_strict_cleanup_parser_requires_explicit_guard_flag() -> None:
    args = workspace.parser().parse_args(["cleanup", "--strict-reap", "--state", "/tmp/session.json"])

    assert args.strict_reap is True


def test_strict_cleanup_refuses_state_changed_after_successful_reap_check(
    created_session: dict[str, Any],
) -> None:
    result = created_session["result"]
    session_dir = Path(result["state_file"]).parent
    saved = saved_session(created_session)
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}
    Path(saved["destination"]).write_text("changed after preflight\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))

    assert session_dir.is_dir()
    assert Path(result["worktree"]).is_dir()
    assert Path(saved["destination"]).read_text(encoding="utf-8") == "changed after preflight\n"


@pytest.mark.parametrize("index_flag", ["--skip-worktree", "--assume-unchanged"])
def test_strict_cleanup_rechecks_for_flagged_modified_tracked_files(
    created_session: dict[str, Any],
    index_flag: str,
) -> None:
    result = created_session["result"]
    session_dir = Path(result["state_file"]).parent
    worktree = Path(result["worktree"])
    saved_session(created_session)
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}

    git(worktree, "update-index", index_flag, "README.md")
    tracked_file = worktree / "README.md"
    tracked_file.write_text("unsaved local work after preflight\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))

    assert session_dir.is_dir()
    assert worktree.is_dir()
    assert tracked_file.read_text(encoding="utf-8") == "unsaved local work after preflight\n"


@pytest.mark.parametrize("problem", ["ignored", "--skip-worktree", "--assume-unchanged"])
def test_strict_cleanup_rechecks_hidden_data_in_initialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    session = create_session(tmp_path, monkeypatch, with_submodule=True)
    result = session["result"]
    session_dir = Path(result["state_file"]).parent
    worktree = Path(result["worktree"])
    git(worktree, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
    submodule = worktree / "vendor"
    artifact = submodule / "ignored-build" / "result.bin"
    tracked_file = submodule / "README.md"
    saved_session(session)
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}

    if problem == "ignored":
        excludes_file = tmp_path / "submodule-excludes"
        excludes_file.write_text("ignored-build/\n", encoding="utf-8")
        git(submodule, "config", "core.excludesFile", str(excludes_file))
        artifact.parent.mkdir()
        artifact.write_bytes(b"keep me")
        assert git(submodule, "status", "--porcelain", "--untracked-files=all") == ""
    else:
        git(submodule, "update-index", problem, "README.md")
        tracked_file.write_text("unsaved submodule work after preflight\n", encoding="utf-8")
        assert git(submodule, "status", "--porcelain", "--untracked-files=all") == ""

    with pytest.raises(workspace.WorkspaceError):
        workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))

    assert session_dir.is_dir()
    assert worktree.is_dir()
    if problem == "ignored":
        assert artifact.read_bytes() == b"keep me"
    else:
        assert tracked_file.read_text(encoding="utf-8") == "unsaved submodule work after preflight\n"


def test_strict_cleanup_removes_an_eligible_session(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    saved = saved_session(created_session)
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}

    removed = workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))

    assert removed["destination"] == saved["destination"]
    assert not Path(result["state_file"]).parent.exists()
    assert not Path(result["worktree"]).exists()


# --- Issue #547: 候補一覧の JSON（review-findings v1） ---------------------------------------------------------

FINDINGS_MARKDOWN = """- 対象: PR #4 / head {head12} / 2026-10-05
- レビュアー: gpt-5.6-sol（ランタイム: codex）

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


def write_findings_pair(session: dict[str, Any], document: dict[str, Any] | None = None) -> tuple[Path, Path]:
    result = session["result"]
    commit = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))["commit"]
    draft = Path(result["draft"])
    draft.write_text(FINDINGS_MARKDOWN.format(head12=commit[:12]), encoding="utf-8")
    findings_draft = Path(result["findings_draft"])
    findings_draft.write_text(json.dumps(document or findings_document(commit), ensure_ascii=False), encoding="utf-8")
    return draft, findings_draft


def findings_destination(session: dict[str, Any]) -> Path:
    result = session["result"]
    commit = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))["commit"]
    return session["repo"] / f"reviews/#4-fix-herdr-session-cwd/findings/gpt-5.6-sol-{commit[:12]}.json"


def test_create_returns_the_pinned_findings_draft(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    assert result["findings_draft"] == str(Path(result["backup_dir"]) / "gpt-5.6-sol.findings.json")


def test_save_with_findings_saves_the_review_and_the_candidates(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    draft, findings_draft = write_findings_pair(created_session)

    saved = workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    assert Path(saved["destination"]).read_text(encoding="utf-8") == draft.read_text(encoding="utf-8")
    assert saved["findings_destination"] == str(findings_destination(created_session))
    assert findings_destination(created_session).read_bytes() == findings_draft.read_bytes()


def _break_property(document: dict[str, Any]) -> None:
    del document["findings"][0]["property"]


def _move_head(document: dict[str, Any]) -> None:
    document["reviewed_head"] = "f" * 40


def _other_pr(document: dict[str, Any]) -> None:
    document["pr"]["number"] = 5


def _other_review_file(document: dict[str, Any]) -> None:
    document["reviewer"]["review_file"] = "other-model.md"


def _extra_finding(document: dict[str, Any]) -> None:
    extra = json.loads(json.dumps(document["findings"][0]))
    extra["id"] = "M-2"
    document["findings"].append(extra)


def _markdown_source(document: dict[str, Any]) -> None:
    document["source"] = "markdown"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_break_property, "findings[0].property: required"),
        (_move_head, "reviewed_head does not match the pinned commit"),
        (_other_pr, "PR number does not match"),
        (_other_review_file, "review_file does not match"),
        (_extra_finding, "M-2 is in the JSON but not in the Markdown"),
        (_markdown_source, "written by the reviewer"),
    ],
)
def test_save_with_invalid_findings_saves_nothing(created_session: dict[str, Any], mutate: Any, message: str) -> None:
    result = created_session["result"]
    commit = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))["commit"]
    document = findings_document(commit)
    mutate(document)
    write_findings_pair(created_session, document)

    with pytest.raises(workspace.WorkspaceError, match=re.escape(message)):
        workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    assert not (created_session["repo"] / "reviews/#4-fix-herdr-session-cwd/gpt-5.6-sol.md").exists()
    assert not findings_destination(created_session).exists()


def test_save_with_findings_requires_the_draft(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="findings draft is missing"):
        workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    assert not (created_session["repo"] / "reviews/#4-fix-herdr-session-cwd/gpt-5.6-sol.md").exists()


def test_save_with_findings_restores_the_candidates_when_the_review_copy_fails(
    created_session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = created_session["result"]
    write_findings_pair(created_session)
    previous = findings_destination(created_session)
    previous.parent.mkdir(parents=True)
    previous.write_text('{"old": true}\n', encoding="utf-8")
    real_copy = workspace.atomic_copy

    def failing_copy(source: Path, destination: Path) -> None:
        if destination.suffix == ".md":
            raise OSError("disk full")
        real_copy(source, destination)

    monkeypatch.setattr(workspace, "atomic_copy", failing_copy)

    with pytest.raises(OSError, match="disk full"):
        workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    assert previous.read_text(encoding="utf-8") == '{"old": true}\n'


def test_save_with_findings_removes_new_candidates_when_the_review_copy_fails(
    created_session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = created_session["result"]
    write_findings_pair(created_session)
    real_copy = workspace.atomic_copy

    def failing_copy(source: Path, destination: Path) -> None:
        if destination.suffix == ".md":
            raise OSError("disk full")
        real_copy(source, destination)

    monkeypatch.setattr(workspace, "atomic_copy", failing_copy)

    with pytest.raises(OSError, match="disk full"):
        workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    assert not findings_destination(created_session).exists()


def test_save_without_findings_leaves_a_findings_draft_cleanable(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    write_findings_pair(created_session)

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert "findings_destination" not in saved
    assert not findings_destination(created_session).exists()
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}
    cleaned = workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))
    assert not Path(cleaned["session_dir"]).exists()


def test_save_with_findings_keeps_the_session_reapable(created_session: dict[str, Any]) -> None:
    result = created_session["result"]
    write_findings_pair(created_session)
    workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}


def test_save_parser_takes_findings_as_a_flag_without_a_path() -> None:
    args = workspace.parser().parse_args(["save", "--state", "/tmp/session.json", "--findings"])
    assert args.findings is True
    assert workspace.parser().parse_args(["save", "--state", "/tmp/session.json"]).findings is False
    with pytest.raises(SystemExit):
        workspace.parser().parse_args(["save", "--state", "/tmp/session.json", "--findings", "/tmp/x.json"])


@pytest.mark.parametrize(
    "value",
    [
        "reviews/#4-x/findings/../escape.json",
        "reviews/#4-x/other/model-abc.json",
        "reviews/#4-x/findings/model.md",
        "elsewhere/#4-x/findings/model.json",
    ],
)
def test_findings_destination_must_stay_under_the_review_findings_directory(tmp_path: Path, value: str) -> None:
    with pytest.raises(workspace.WorkspaceError):
        workspace.safe_findings_destination(tmp_path, value)


def test_save_without_findings_removes_the_stale_candidates_of_the_same_head(
    created_session: dict[str, Any],
) -> None:
    # 最終レビュー I-2: 同じ HEAD を Markdown だけで保存し直したとき、古い候補一覧を review-verify に読ませない
    result = created_session["result"]
    stale = findings_destination(created_session)
    stale.parent.mkdir(parents=True)
    stale.write_text('{"old": true}\n', encoding="utf-8")
    other = stale.with_name("other-model-000000000000.json")
    other.write_text("{}\n", encoding="utf-8")
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert Path(saved["destination"]).read_text(encoding="utf-8") == "review\n"
    assert not stale.exists()
    assert other.exists()
    assert saved["removed_stale_findings"] == str(stale)


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


def test_target_create_saves_below_owner_and_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"]))

    assert result["destination"] == f"reviews/{TARGET_REPOSITORY}/#3-hogehoge/{TARGET_MODEL}.md"
    expected = session["parent"] / "reviews" / "usaturn" / "wabun-rst-ulint" / "#3-hogehoge" / f"{TARGET_MODEL}.md"
    assert saved["destination"] == str(expected)
    assert expected.read_text(encoding="utf-8") == "review\n"
    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert state["version"] == 7
    assert state["target_kind"] == "clone"
    assert state["target_root"] == str(Path(result["state_file"]).parent / "repo")
    assert "main_root" not in state
    assert git(Path(result["worktree"]), "rev-parse", "HEAD") == session["head"]


def test_target_create_uses_existing_checkout_without_changing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, _head = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "config", "remote.origin.url", f"https://github.com/{TARGET_REPOSITORY}.git")
    (checkout / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    before = (
        git(checkout, "branch", "--show-current"),
        git(checkout, "rev-parse", "HEAD"),
        git(checkout, "status", "--porcelain"),
    )

    session = create_target_session(tmp_path, monkeypatch, target_root=checkout.resolve())

    result = session["result"]
    worktree = Path(result["worktree"])
    assert (
        Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")) == (checkout / ".git").resolve()
    )
    assert git(checkout, "rev-parse", "refs/pr-review/3") == session["head"]
    after = (
        git(checkout, "branch", "--show-current"),
        git(checkout, "rev-parse", "HEAD"),
        git(checkout, "status", "--porcelain"),
    )
    assert after == before
    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert state["target_kind"] == "checkout"
    assert state["target_root"] == str(checkout.resolve())


def test_target_create_keeps_repositories_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = create_target_session(tmp_path, monkeypatch, repository="usaturn/a", remote_name="a")
    Path(first["result"]["draft"]).write_text("a\n", encoding="utf-8")
    first_saved = workspace.save(argparse.Namespace(state=first["result"]["state_file"]))
    second = create_target_session(tmp_path, monkeypatch, repository="usaturn/b", remote_name="b")
    Path(second["result"]["draft"]).write_text("b\n", encoding="utf-8")
    second_saved = workspace.save(argparse.Namespace(state=second["result"]["state_file"]))

    assert second["result"]["previous_review"] is None
    assert Path(first_saved["destination"]).read_text(encoding="utf-8") == "a\n"
    assert Path(second_saved["destination"]).read_text(encoding="utf-8") == "b\n"
    assert "/reviews/usaturn/a/#3-hogehoge/" in first_saved["destination"]
    assert "/reviews/usaturn/b/#3-hogehoge/" in second_saved["destination"]


def test_target_create_rejects_symlinked_repository_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = make_parent(tmp_path / "parent")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "wabun-rst-ulint").mkdir(parents=True)
    (parent / "reviews").mkdir()
    (parent / "reviews" / "usaturn").symlink_to(elsewhere)

    with pytest.raises(workspace.WorkspaceError, match="symlink"):
        create_target_session(tmp_path, monkeypatch)


def test_target_create_stops_when_pull_head_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_parent(tmp_path / "parent")
    _remote, head = make_remote(tmp_path)
    moved = git(tmp_path / "remote-work", "rev-parse", "main")
    assert moved != head

    with pytest.raises(workspace.WorkspaceError, match="変わりました"):
        create_target_session(tmp_path, monkeypatch, head_sha=moved)


def test_target_create_rejects_mixed_arguments(tmp_path: Path) -> None:
    with pytest.raises(workspace.WorkspaceError, match="--target"):
        workspace.create(
            argparse.Namespace(
                target="{}",
                repo=str(tmp_path),
                pr_number=None,
                commit=None,
                head_branch=None,
                model_name=TARGET_MODEL,
                temp_root=str(tmp_path / "sessions"),
            )
        )
    assert not (tmp_path / "sessions").exists()


def test_create_without_target_requires_legacy_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(workspace.WorkspaceError, match="--repo"):
        workspace.create(
            argparse.Namespace(
                repo=None,
                pr_number=4,
                commit="deadbeef",
                head_branch="x",
                model_name=TARGET_MODEL,
                temp_root=str(tmp_path / "sessions"),
            )
        )


def test_target_create_rejects_checkout_of_another_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, _head = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "config", "remote.origin.url", "https://github.com/usaturn/other.git")

    with pytest.raises(workspace.WorkspaceError, match="origin"):
        create_target_session(tmp_path, monkeypatch, target_root=checkout.resolve())


def test_target_create_rejects_parent_without_ignored_reviews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_parent(tmp_path / "parent", ignore_reviews=False)

    with pytest.raises(workspace.WorkspaceError, match="無視されていません"):
        create_target_session(tmp_path, monkeypatch)


def test_target_update_reads_previous_review_and_cleanup_removes_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_target_cleanup_waits_for_parent_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    session_dir = Path(result["state_file"]).parent
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    workspace.save(argparse.Namespace(state=result["state_file"]))

    with workspace.review_save_lock(session["parent"]):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "cleanup", "--state", result["state_file"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        assert session_dir.is_dir()

    stdout, stderr = process.communicate(timeout=POST_LOCK_TIMEOUT)
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["session_dir"] == str(session_dir)
    assert not session_dir.exists()


def test_target_reap_check_accepts_saved_clone_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    workspace.save(argparse.Namespace(state=result["state_file"]))

    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}
    workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))
    assert not Path(result["state_file"]).parent.exists()


def test_target_reap_check_rejects_extra_files_in_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")
    workspace.save(argparse.Namespace(state=result["state_file"]))
    (Path(result["state_file"]).parent / "repo" / "notes.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="clone"):
        workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"]))


def test_target_save_with_findings_uses_owner_and_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    head = session["head"]
    Path(result["draft"]).write_text(FINDINGS_MARKDOWN.format(head12=head[:12]), encoding="utf-8")
    document = findings_document(head, pr={"repository": TARGET_REPOSITORY, "number": 3})
    Path(result["findings_draft"]).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    saved = workspace.save(argparse.Namespace(state=result["state_file"], findings=True))

    expected = session["parent"] / f"reviews/{TARGET_REPOSITORY}/#3-hogehoge/findings/{TARGET_MODEL}-{head[:12]}.json"
    assert saved["findings_destination"] == str(expected)
    assert expected.is_file()


def test_parser_accepts_target_without_legacy_arguments() -> None:
    args = workspace.parser().parse_args(["create", "--target", "{}", "--model-name", TARGET_MODEL])

    assert args.target == "{}"
    assert args.repo is None


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


def test_target_local_commit_uses_full_diff_and_separate_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, source, commit = local_commit_target(tmp_path, monkeypatch)
    before = workspace.snapshot(source)
    result = workspace.create(session["args"])
    worktree = Path(result["worktree"])
    assert git(worktree, "rev-parse", "HEAD") == commit
    base = git(worktree, "merge-base", "main", commit)
    assert set(git(worktree, "diff", "--name-only", f"{base}..{commit}").splitlines()) == {"change.txt", "local.txt"}
    assert result["destination"].endswith(f"/{TARGET_MODEL}-commit-{commit}.md")
    assert result["pr_head"] == session["head"]
    assert result["reviewed_head"] == commit
    assert workspace.snapshot(source) == before


@pytest.mark.parametrize("commit", ["HEAD", "abc123", "f" * 40])
def test_target_explicit_commit_rejects_invalid_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit: str,
) -> None:
    session, _source, _commit = local_commit_target(tmp_path, monkeypatch)
    session["args"].review_commit = commit
    with pytest.raises(workspace.WorkspaceError, match="commit"):
        workspace.create(session["args"])


def test_target_explicit_commit_rejects_wrong_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, source, _commit = local_commit_target(tmp_path, monkeypatch)
    git(source, "config", "remote.origin.url", "https://github.com/usaturn/other.git")
    with pytest.raises(workspace.WorkspaceError, match="origin"):
        workspace.create(session["args"])
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize("failure", ["moved", "api"])
def test_target_save_refuses_moved_live_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    session = create_target_session(tmp_path, monkeypatch)
    result = session["result"]
    Path(result["draft"]).write_text("review\n", encoding="utf-8")

    def live_head(_repository: str, _number: int) -> str:
        if failure == "api":
            raise workspace.WorkspaceError("API unavailable")
        return "f" * 40

    monkeypatch.setattr(workspace, "current_pr_head", live_head, raising=False)
    with pytest.raises(workspace.WorkspaceError, match="PR head changed|API unavailable"):
        workspace.save(argparse.Namespace(state=result["state_file"]))
    assert not (session["parent"] / result["destination"]).exists()
    assert Path(result["state_file"]).exists()


def test_target_commit_save_and_cleanup_keep_head_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert workspace.check_reap_candidate(argparse.Namespace(state=result["state_file"])) == {"status": "eligible"}
    cleaned = workspace.cleanup(argparse.Namespace(state=result["state_file"], strict_reap=True))
    assert cleaned["pr_ref_status"] == "deleted"
    assert head_review.read_text(encoding="utf-8") == "live head review\n"
    assert not Path(result["state_file"]).parent.exists()
    assert git(source, "rev-parse", "HEAD") == commit


def test_parser_accepts_pinned_commit_and_source_root() -> None:
    args = workspace.parser().parse_args(
        [
            "create",
            "--target",
            "{}",
            "--model-name",
            TARGET_MODEL,
            "--review-commit",
            "a" * 40,
            "--source-root",
            "/example/source",
        ]
    )
    assert args.review_commit == "a" * 40
    assert args.source_root == "/example/source"


# --- Issue #68: 同じリポジトリへの並行した git worktree add の commondir の競合 --------------------------------------


def replace_wait(monkeypatch: pytest.MonkeyPatch, action: Any = lambda: None) -> list[float]:
    """やり直しの前の待ちを記録し、待つ代わりに action（相手が書き終える など）を実行する。"""
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        action()

    monkeypatch.setattr(workspace, "wait_before_retry", wait)
    return waits


def test_create_retries_worktree_after_half_written_commondir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex"
    write_rollout(codex_home, ["gpt-5.6-sol"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    repo = tmp_path / "repo"
    commit = make_repo(repo)
    commondir = plant_racer(repo)
    waits = replace_wait(monkeypatch, lambda: commondir.write_text("../..\n", encoding="utf-8"))

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

    assert git(Path(str(result["worktree"])), "rev-parse", "HEAD") == commit
    assert len(waits) == 1 and 0.05 <= waits[0] <= 0.2


def test_add_detached_worktree_gives_up_after_three_lost_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    commit = make_repo(repo)
    plant_racer(repo)
    waits = replace_wait(monkeypatch)
    worktree = tmp_path / "session" / "worktree"

    with pytest.raises(workspace.WorkspaceError, match="commondir: "):
        workspace.add_detached_worktree(repo, worktree, commit)

    assert len(waits) == 2
    assert not worktree.exists()


# 終了コードが同じでも競合でない失敗は、やり直しても同じ失敗を繰り返すだけ
def test_add_detached_worktree_does_not_retry_other_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    commit = make_repo(repo)
    waits = replace_wait(monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError):
        workspace.add_detached_worktree(repo, blocker / "worktree", commit)

    assert waits == []
