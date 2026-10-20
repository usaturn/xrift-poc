"""Git workspace isolation and audits against real repositories."""

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agent_runner import environ, process, workspace
from tools.agent_runner.workspace import (
    WorkspaceError,
    _git,  # pyright: ignore[reportPrivateUsage] - exercise the bounded helper directly
    changed_checkouts,
    create_worktree,
    resolve_revision,
    snapshot_checkouts,
)
from tools.test_support.worktree_race import plant_racer


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "tracked.txt").write_text("committed\n")
    (root / ".gitignore").write_text("ignored/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


# Returning a symbolic revision would let a later branch movement change the worktree commit.
def test_resolve_revision_pins_commit_not_branch(repository: Path) -> None:
    initial = git(repository, "rev-parse", "HEAD")
    assert resolve_revision(repository) == initial
    (repository / "tracked.txt").write_text("second\n")
    git(repository, "commit", "-qam", "second")
    assert resolve_revision(repository, "HEAD~1") == initial


# Accepting options, noncommits, or nonexistent repos/revisions breaks the launch boundary.
@pytest.mark.parametrize("revision", ["--help", "-q", "absent_SECRET", "HEAD:tracked.txt", "bad\x00SECRET"])
def test_revision_errors_are_safe(repository: Path, revision: str) -> None:
    with pytest.raises(WorkspaceError) as caught:
        resolve_revision(repository, revision)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


def test_repository_must_exist_and_be_git(tmp_path: Path) -> None:
    for repository in (tmp_path / "absent_SECRET", tmp_path):
        with pytest.raises(WorkspaceError) as caught:
            resolve_revision(repository)
        assert "SECRET" not in "".join(traceback.format_exception(caught.value))


# Copying the dirty original or checking out a branch would violate detached isolation.
def test_worktree_is_fresh_detached_and_preserves_dirty_original(repository: Path, tmp_path: Path) -> None:
    commit = resolve_revision(repository)
    (repository / "tracked.txt").write_text("local dirty\n")
    destination = tmp_path / "retained-worktree"
    assert create_worktree(repository, commit, destination) == destination
    assert (destination / "tracked.txt").read_text() == "committed\n"
    assert git(destination, "rev-parse", "HEAD") == commit
    assert git(destination, "status", "--porcelain") == ""
    assert (
        subprocess.run(["git", "-C", str(destination), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode
        != 0
    )
    assert (repository / "tracked.txt").read_text() == "local dirty\n"


def test_worktree_never_overwrites_existing_directory(repository: Path, tmp_path: Path) -> None:
    destination = tmp_path / "existing_SECRET"
    destination.mkdir()
    (destination / "retained").write_text("original")
    with pytest.raises(WorkspaceError) as caught:
        create_worktree(repository, "HEAD", destination)
    assert (destination / "retained").read_text() == "original"
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


# Hashing porcelain status alone misses edits to files that were already dirty at entry.
@pytest.mark.parametrize("kind", ["tracked", "untracked", "staged", "symlink"])
def test_audit_detects_existing_dirty_content_edits(repository: Path, tmp_path: Path, kind: str) -> None:
    external = create_worktree(repository, "HEAD", tmp_path / "external checkout\nwith newline")
    path = external / ("tracked.txt" if kind in ("tracked", "staged") else "untracked.txt")
    if kind == "symlink":
        path.symlink_to("first-target")
    else:
        path.write_text("dirty before\n")
    if kind == "staged":
        git(external, "add", path.name)
    before = snapshot_checkouts(repository)
    assert set(before) == {str(repository), str(external)}
    assert changed_checkouts(before) == []
    if kind == "symlink":
        path.unlink()
        path.symlink_to("different-target")
    else:
        path.write_text("dirty after!\n")
    assert changed_checkouts(before) == [str(external)]
    assert all(value is not None and "dirty" not in value for value in before.values())


@pytest.mark.parametrize("mutation", ["delete", "add", "chmod", "index", "head"])
def test_audit_detects_checkout_mutations(repository: Path, mutation: str) -> None:
    before = snapshot_checkouts(repository)
    path = repository / "tracked.txt"
    if mutation == "delete":
        path.unlink()
    elif mutation == "add":
        (repository / "new.txt").write_text("new file")
    elif mutation == "chmod":
        path.chmod(0o755)
    elif mutation == "index":
        git(repository, "update-index", "--assume-unchanged", "tracked.txt")
        # Changing the staged blob, not merely an index flag, is audited.
        path.write_text("staged change")
        git(repository, "update-index", "--no-assume-unchanged", "tracked.txt")
        git(repository, "add", "tracked.txt")
        path.write_text("committed\n")
    else:
        git(repository, "commit", "--allow-empty", "-qm", "advance HEAD")
    assert changed_checkouts(before) == [str(repository)]


# New runner checkouts must not be confused with alterations to the pre-existing set.
def test_audit_ignores_new_checkouts_and_gitignored_resources(repository: Path, tmp_path: Path) -> None:
    before = snapshot_checkouts(repository)
    ignored = repository / "ignored"
    ignored.mkdir()
    (ignored / "secret.log").write_text("SECRET credential")
    create_worktree(repository, "HEAD", tmp_path / "new runner checkout")
    assert changed_checkouts(before) == []


# A vanished checkout cannot be silently treated as an unchanged one.
def test_audit_reports_disappeared_checkout(repository: Path, tmp_path: Path) -> None:
    external = create_worktree(repository, "HEAD", tmp_path / "external")
    before = snapshot_checkouts(repository)
    external.rename(tmp_path / "moved")
    assert changed_checkouts(before) == [str(external)]


def test_repair_of_moved_main_and_linked_worktree_preserves_all_resources(repository: Path, tmp_path: Path) -> None:
    old_parent = tmp_path / "旧 root"
    external = create_worktree(repository, "HEAD", old_parent / "checkout\n日本語")
    (external / "tracked.txt").write_text("dirty tracked")
    (external / "tracked.txt").chmod(0o751)
    (external / "untracked.txt").write_text("untracked content")
    (external / "ignored").mkdir()
    (external / "ignored" / "resource").write_text("ignored content")
    (external / "link").symlink_to("untracked.txt")
    head = git(repository, "rev-parse", "HEAD")
    moved_repository = tmp_path / "new-repository"
    repository.rename(moved_repository)
    moved_parent = tmp_path / "new-root"
    old_parent.rename(moved_parent)
    moved = moved_parent / external.name
    before = snapshot_checkouts(moved_repository)
    assert workspace.audit_checkouts(before)[0].kind == "unavailable_before"
    assert before[str(external)] is None
    git(moved_repository, "worktree", "repair", str(moved))
    assert git(moved, "rev-parse", "HEAD") == head
    assert git(moved, "rev-parse", "--git-common-dir") == str(moved_repository / ".git")
    assert (moved / "tracked.txt").read_text() == "dirty tracked"
    assert (moved / "tracked.txt").stat().st_mode & 0o777 == 0o751
    assert (moved / "untracked.txt").read_text() == "untracked content"
    assert (moved / "ignored" / "resource").read_text() == "ignored content"
    assert os.readlink(moved / "link") == "untracked.txt"
    fresh = workspace.require_auditable_checkouts(moved_repository)
    assert set(fresh) == {str(moved_repository), str(moved)}
    assert workspace.audit_checkouts(fresh) == []
    assert changed_checkouts(before) == [str(external)]


def test_audit_does_not_follow_untracked_symlink(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("before")
    os.symlink(outside, repository / "link")
    before = snapshot_checkouts(repository)
    outside.write_text("after")
    assert changed_checkouts(before) == []


# A symlink replacing a tracked directory must not make its referents audit inputs.
def test_audit_does_not_follow_symlink_ancestors(repository: Path, tmp_path: Path) -> None:
    nested = repository / "nested"
    nested.mkdir()
    (nested / "tracked.txt").write_text("committed nested")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "nested file")
    outside = tmp_path / "outside"
    nested.rename(outside)
    nested.symlink_to(outside, target_is_directory=True)
    before = snapshot_checkouts(repository)
    (outside / "tracked.txt").write_text("outside content changed")
    assert changed_checkouts(before) == []


# Initialized submodule working files remain tracked/nonignored checkout content.
def test_audit_detects_submodule_dirty_content(repository: Path, tmp_path: Path) -> None:
    subrepo = tmp_path / "subrepo"
    subrepo.mkdir()
    git(subrepo, "init", "-q")
    git(subrepo, "config", "user.name", "Test")
    git(subrepo, "config", "user.email", "test@example.invalid")
    (subrepo / "tracked.txt").write_text("initial")
    git(subrepo, "add", ".")
    git(subrepo, "commit", "-qm", "initial")
    git(repository, "-c", "protocol.file.allow=always", "submodule", "add", str(subrepo), "sub")
    git(repository, "commit", "-qm", "submodule")
    (repository / "sub" / "tracked.txt").write_text("already dirty")
    before = snapshot_checkouts(repository)
    (repository / "sub" / "tracked.txt").write_text("dirty edit")
    assert changed_checkouts(before) == [str(repository)]


@pytest.mark.parametrize("kind", ["prunable", "locked", "broken_gitdir", "regular_file", "unreadable"])
def test_unavailable_registered_checkout_is_a_warning(repository: Path, tmp_path: Path, kind: str) -> None:
    external = create_worktree(repository, "HEAD", tmp_path / "unavailable")
    if kind == "locked":
        git(repository, "worktree", "lock", str(external))
    if kind in ("prunable", "locked", "regular_file"):
        shutil.rmtree(external)
        if kind == "regular_file":
            external.write_text("not a checkout")
    elif kind == "broken_gitdir":
        (external / ".git").write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
    else:
        external.chmod(0)
    try:
        before = snapshot_checkouts(repository)
        assert before[str(external)] is None
        assert before[str(repository)] is not None
        assert changed_checkouts(before) == [str(external)]
    finally:
        if kind == "unreadable":
            external.chmod(0o755)


def test_checkout_recovered_after_snapshot_still_warns(repository: Path, tmp_path: Path) -> None:
    external = create_worktree(repository, "HEAD", tmp_path / "recovered")
    gitfile = external / ".git"
    original = gitfile.read_bytes()
    gitfile.write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
    try:
        before = snapshot_checkouts(repository)
    finally:
        gitfile.write_bytes(original)
    assert changed_checkouts(before) == [str(external)]


@pytest.mark.parametrize(
    ("kind", "reason", "locked", "prunable"),
    [
        ("missing", "checkout_missing", False, True),
        ("locked", "checkout_missing", True, False),
        ("broken", "git_unavailable", False, False),
        ("unreadable", "git_unavailable", False, True),
        ("file", "not_directory", False, True),
    ],
)
def test_audit_diagnoses_unavailable_baseline(
    repository: Path,
    tmp_path: Path,
    kind: str,
    reason: str,
    locked: bool,
    prunable: bool,
) -> None:
    external = create_worktree(repository, "HEAD", tmp_path / "登録 checkout\n新しい場所")
    if kind == "locked":
        git(
            repository,
            "worktree",
            "lock",
            "--reason",
            "SECRET lock reason",
            str(external),
        )
    if kind in {"missing", "locked", "file"}:
        shutil.rmtree(external)
        if kind == "file":
            external.write_text("SECRET not a directory")
    elif kind == "broken":
        (external / ".git").write_text(f"gitdir: {tmp_path / 'SECRET-broken'}\n")
    else:
        external.chmod(0)
    try:
        before = snapshot_checkouts(repository)
        diagnostics = workspace.audit_checkouts(before)
        assert [item.model_dump() for item in diagnostics] == [
            {
                "path": str(external),
                "kind": "unavailable_before",
                "reason": reason,
                "locked": locked,
                "prunable": prunable,
            }
        ]
        with pytest.raises(workspace.CheckoutAuditError) as caught:
            workspace.require_auditable_checkouts(repository)
        assert caught.value.findings == diagnostics
        assert "SECRET" not in str(caught.value)
        assert changed_checkouts(before) == [str(external)]
    finally:
        if kind == "unreadable":
            external.chmod(0o755)


@pytest.mark.parametrize("mutation", ["change", "disappear", "recover"])
def test_audit_distinguishes_changes_from_unavailability(repository: Path, tmp_path: Path, mutation: str) -> None:
    external = create_worktree(repository, "HEAD", tmp_path / "external")
    gitfile = external / ".git"
    original = gitfile.read_bytes()
    if mutation == "recover":
        gitfile.write_text(f"gitdir: {tmp_path / 'absent'}\n")
    before = snapshot_checkouts(repository)
    if mutation == "recover":
        gitfile.write_bytes(original)
    elif mutation == "disappear":
        shutil.rmtree(external)
    else:
        (external / "tracked.txt").write_text("modified")
    diagnostics = workspace.audit_checkouts(before)
    assert [item.path for item in diagnostics] == [str(external)]
    assert (
        diagnostics[0].kind
        == {
            "change": "content_changed",
            "disappear": "unavailable_after",
            "recover": "unavailable_before",
        }[mutation]
    )
    assert (
        diagnostics[0].reason
        == {
            "change": "content_changed",
            "disappear": "checkout_missing",
            "recover": "git_unavailable",
        }[mutation]
    )
    assert changed_checkouts(before) == [str(external)]
    if mutation == "recover":
        assert workspace.require_auditable_checkouts(repository)[str(external)] is not None
        assert workspace.audit_checkouts(snapshot_checkouts(repository)) == []


def test_preflight_registration_failure_is_environment_diagnostic(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_: object) -> bytes:
        raise WorkspaceError()

    monkeypatch.setattr(workspace, "_git", unavailable)
    with pytest.raises(workspace.CheckoutAuditError) as caught:
        workspace.require_auditable_checkouts(repository)
    assert caught.value.findings[0].path == str(repository)
    assert caught.value.findings[0].kind == "unavailable_before"


def test_unavailable_fingerprint_is_attempted_once(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def unavailable(_: Path) -> str:
        nonlocal calls
        calls += 1
        raise WorkspaceError()

    monkeypatch.setattr(workspace, "_fingerprint", unavailable)
    before = snapshot_checkouts(repository)
    assert changed_checkouts(before) == [str(repository)]
    assert calls == 1


def test_registration_list_failure_still_stops_snapshot(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_: object) -> bytes:
        raise WorkspaceError()

    monkeypatch.setattr(workspace, "_git", unavailable)
    with pytest.raises(WorkspaceError):
        snapshot_checkouts(repository)


# Missing subprocess timeouts allow repository helpers to hang even preflight.
def test_git_helper_timeout_has_safe_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text(f"#!{sys.executable}\nimport time; time.sleep(0.3); print('SECRET')\n")
    fake_git.chmod(0o755)
    # Git helpers run under the exec environment, not os.environ.
    monkeypatch.setattr(environ, "_literal", {"PATH": str(tmp_path)})
    real_run = subprocess.run

    def shortened(
        args: list[str],
        *,
        check: bool,
        stdout: int,
        stderr: int,
        env: dict[str, str],
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return real_run(
            args, check=check, stdout=stdout, stderr=stderr, env=env, timeout=0.05 if timeout is not None else None
        )

    monkeypatch.setattr(subprocess, "run", shortened)
    arguments = ("rev-parse", "SECRET")
    with pytest.raises(WorkspaceError) as caught:
        _git(tmp_path, *arguments)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


# Preparation must consume the already-pinned commit, without synchronous Git
# resolution, and ignore ambient Git routing when creating a detached worktree.
def test_async_preparation_uses_pinned_commit_and_safe_environment(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = resolve_revision(repository)
    monkeypatch.setenv("GIT_DIR", "/bogus-ambient-git")
    # Ambient Git routing in the exec environment must not reach the helpers.
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"GIT_DIR": "/bogus-ambient-git"})

    def forbidden(*_: object) -> bytes:
        raise AssertionError("synchronous Git in async preparation")

    monkeypatch.setattr(workspace, "_git", forbidden)
    assert callable(getattr(workspace, "prepare_worktree", None))
    destination = tmp_path / "async-worktree"
    result = asyncio.run(
        workspace.prepare_worktree(
            repository,
            commit,
            destination,
            deadline_seconds=3,
            cancel=threading.Event(),
            stdout_path=tmp_path / "worktree.stdout.log",
            stderr_path=tmp_path / "worktree.stderr.log",
        )
    )
    assert result.state is None and result.exit_code == 0 and result.stop_confirmed
    assert (destination / "tracked.txt").read_text() == "committed\n"
    monkeypatch.delenv("GIT_DIR")
    assert git(destination, "rev-parse", "HEAD") == commit
    assert subprocess.run(["git", "-C", str(destination), "symbolic-ref", "-q", "HEAD"]).returncode != 0


def test_git_preparation_stops_term_resistant_child_of_exited_hook(repository: Path, tmp_path: Path) -> None:
    identity = tmp_path / "hook-child.identity"
    code = (
        "import os,pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "pid=os.getpid(); start=pathlib.Path(f'/proc/{pid}/stat').read_bytes().rsplit(b')',1)[1].split()[19]; "
        f"pathlib.Path({str(identity)!r}).write_text(str(pid)+' '+start.decode()); time.sleep(60)"
    )
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\n{shlex.quote(sys.executable)} -c {shlex.quote(code)} &\n"
        f"while [ ! -s {shlex.quote(str(identity))} ]; do sleep 0.01; done\n"
    )
    hook.chmod(0o755)
    try:
        result = asyncio.run(
            workspace.prepare_worktree(
                repository,
                resolve_revision(repository),
                tmp_path / "hook-worktree",
                deadline_seconds=3,
                cancel=threading.Event(),
                stdout_path=tmp_path / "worktree.stdout.log",
                stderr_path=tmp_path / "worktree.stderr.log",
            )
        )
        assert result.state is None and result.exit_code == 0 and result.stop_confirmed
        pid = int(identity.read_text().split()[0])
        current = process._read_proc(pid)  # pyright: ignore[reportPrivateUsage] - real hook child evidence
        assert current is None or current.state in ("Z", "X")
    finally:
        if identity.exists():
            pid, start = map(int, identity.read_text().split())
            current = process._read_proc(pid)  # pyright: ignore[reportPrivateUsage] - identity-safe fixture cleanup
            if current is not None and current.starttime == start and current.state not in ("Z", "X"):
                os.kill(pid, signal.SIGKILL)


# Callers list where a writes=False agent may write in a checkout's shared Git data (Issue #658).
def test_git_common_dir_of_a_linked_worktree_is_the_main_git_directory(repository: Path, tmp_path: Path) -> None:
    linked = create_worktree(repository, "HEAD", tmp_path / "linked")
    expected = (repository / ".git").resolve()
    assert workspace.git_common_dir(repository) == expected
    assert workspace.git_common_dir(linked) == expected


def test_git_common_dir_ignores_ambient_git_routing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"GIT_DIR": str(other / ".git")})
    assert workspace.git_common_dir(repository) == (repository / ".git").resolve()


def test_git_common_dir_of_a_non_repository_is_a_workspace_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError):
        workspace.git_common_dir(plain)


def replace_pause(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[threading.Event], object] = lambda _: None
) -> list[float]:
    """Record each pause before a retry and run action in its place (the other writer finishing, a cancel, ...)."""
    pauses: list[float] = []

    async def pause(seconds: float, cancel: threading.Event) -> None:
        pauses.append(seconds)
        action(cancel)

    monkeypatch.setattr(workspace, "_pause", pause)
    return pauses


def prepare(repository: Path, destination: Path, logs: Path) -> process.ProcessResult:
    return asyncio.run(
        workspace.prepare_worktree(
            repository,
            resolve_revision(repository),
            destination,
            deadline_seconds=30,
            cancel=threading.Event(),
            stdout_path=logs / "worktree.stdout.log",
            stderr_path=logs / "worktree.stderr.log",
        )
    )


def retained_logs(logs: Path) -> list[str]:
    return sorted(path.name for path in logs.glob("worktree.std*.log.*"))


# Simultaneous preparations make each runner name its admin directory worktree, worktree1, ...;
# from a linked worktree Git prints the common directory as an absolute path.
@pytest.mark.parametrize(("racer", "linked"), [("racer", False), ("worktree1", False), ("racer", True)])
def test_preparation_retries_after_reading_a_half_written_commondir(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, racer: str, linked: bool
) -> None:
    source = create_worktree(repository, "HEAD", tmp_path / "linked") if linked else repository
    commondir = plant_racer(repository, racer)
    pauses = replace_pause(monkeypatch, lambda _: commondir.write_text("../..\n"))
    logs = tmp_path / "出力 先"
    logs.mkdir()
    destination = logs / "worktree"

    result = prepare(source, destination, logs)

    assert (result.state, result.exit_code, result.stop_confirmed) == (None, 0, True)
    assert git(destination, "rev-parse", "HEAD") == resolve_revision(repository)
    assert len(pauses) == 1 and 0.05 <= pauses[0] <= 0.2
    # The lost try's logs stay next to the successful try's.
    assert retained_logs(logs) == ["worktree.stderr.log.1", "worktree.stdout.log.1"]
    assert f"worktrees/{racer}/commondir: " in (logs / "worktree.stderr.log.1").read_text()
    assert "commondir" not in (logs / "worktree.stderr.log").read_text()


def test_preparation_gives_up_after_three_lost_races(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_racer(repository)
    pauses = replace_pause(monkeypatch)
    destination = tmp_path / "worktree"

    result = prepare(repository, destination, tmp_path)

    assert (result.state, result.exit_code, result.stop_confirmed) == ("response_failed", 128, True)
    assert len(pauses) == 2
    assert retained_logs(tmp_path) == [
        "worktree.stderr.log.1",
        "worktree.stderr.log.2",
        "worktree.stdout.log.1",
        "worktree.stdout.log.2",
    ]
    assert "commondir: " in (tmp_path / "worktree.stderr.log").read_text()
    assert not destination.exists()


# The same exit code without the race: retrying would only repeat a lasting failure.
def test_preparation_does_not_retry_other_failures(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pauses = replace_pause(monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("")

    result = prepare(repository, blocker / "worktree", tmp_path)

    assert (result.state, result.exit_code, result.stop_confirmed) == ("response_failed", 128, True)
    assert pauses == []
    assert retained_logs(tmp_path) == []


# Files of a preparation without independent stop evidence are never moved or rewritten.
def test_preparation_never_retries_an_unconfirmed_stop(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commondir = plant_racer(repository)
    pauses = replace_pause(monkeypatch, lambda _: commondir.write_text("../..\n"))

    def no_stop(_: object) -> bool:
        return False

    monkeypatch.setattr("tools.agent_runner.process._stop_confirmed", no_stop)
    destination = tmp_path / "worktree"

    result = prepare(repository, destination, tmp_path)

    assert (result.exit_code, result.stop_confirmed) == (128, False)
    assert pauses == []
    assert retained_logs(tmp_path) == []
    assert "commondir: " in (tmp_path / "worktree.stderr.log").read_text()
    assert not destination.exists()


# A cancel or an exhausted deadline during the pause returns the lost try, exactly as without retries.
@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_preparation_is_not_retried_after_cancel_or_deadline_during_the_pause(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    commondir = plant_racer(repository)
    offset = [0.0]
    real = time.monotonic
    # Advance only the preparation's clock; supervised execution keeps the real one.
    monkeypatch.setattr(workspace, "time", SimpleNamespace(monotonic=lambda: real() + offset[0]))

    def interrupt(cancel: threading.Event) -> None:
        commondir.write_text("../..\n")  # a retry would now succeed
        if interruption == "cancel":
            cancel.set()
        else:
            offset[0] = 60.0

    pauses = replace_pause(monkeypatch, interrupt)
    destination = tmp_path / "worktree"

    result = prepare(repository, destination, tmp_path)

    assert (result.state, result.exit_code, result.stop_confirmed) == ("response_failed", 128, True)
    assert len(pauses) == 1
    assert retained_logs(tmp_path) == []
    assert "commondir: " in (tmp_path / "worktree.stderr.log").read_text()
    assert not destination.exists()
