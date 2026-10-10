"""Git workspace isolation and audits against real repositories."""

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import traceback
from pathlib import Path

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
