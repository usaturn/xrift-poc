"""Detached worktrees and observational audits, never automatic cleanup.

Audits cover HEAD, index entries, tracked files and nonignored untracked files
(including initialized submodule checkouts). Gitignored resources, symlink
referents, and arbitrary directories outside registered checkouts are outside
this boundary. A changed fingerprint is evidence of change, not attribution.
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import os
import random
import re
import stat
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from tools.agent_runner.adapters import LaunchSpec
from tools.agent_runner.environ import literal_environ
from tools.agent_runner.models import AuditReason, CheckoutAudit
from tools.agent_runner.process import ProcessResult, execute


class WorkspaceError(Exception):
    """Workspace operation failed without exposing Git diagnostics."""

    def __init__(self, reason: AuditReason = "audit_unavailable") -> None:
        super().__init__("agent-runner workspace operation failed")
        self.reason: AuditReason = reason


class CheckoutSnapshot(dict[str, str | None]):
    """Keep the historical mapping API while preserving checkout-local diagnostics.

    identities holds the device/inode of each checkout path and its ancestors, so an
    audit can tell a checkout replaced at the same path from a changed one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.failures: dict[str, CheckoutAudit] = {}
        self.flags: dict[str, tuple[bool, bool]] = {}
        self.identities: dict[str, tuple[tuple[str, int, int], ...]] = {}


@dataclasses.dataclass(frozen=True)
class CheckoutIsolation:
    """bwrap arguments, and whether every registered checkout is protected under them."""

    argv: tuple[str, ...]
    complete: bool


class CheckoutAuditError(Exception):
    """Strict callers reject an unauditable baseline before any paid work or reservation."""

    def __init__(self, findings: list[CheckoutAudit]) -> None:
        super().__init__("registered checkouts cannot be audited before execution")
        self.findings = findings


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            timeout=10,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # Ambient Git routing must not redirect the requested repository.
            env={key: value for key, value in literal_environ().items() if not key.startswith("GIT_")},
        ).stdout
    except OSError, ValueError, subprocess.SubprocessError:
        raise WorkspaceError("git_unavailable") from None


def resolve_revision(repository: Path, revision: str = "HEAD") -> str:
    """Resolve an existing repository's revision to an immutable commit ID."""
    if not revision or revision.startswith("-") or "\x00" in revision or not repository.is_dir():
        raise WorkspaceError()
    # This also validates the repository, and rejects trees/blobs rather than commits.
    result = _git(repository, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    return result.decode("ascii").strip()


def git_common_dir(repository: Path) -> Path:
    """The Git directory a checkout shares with its linked worktrees (the main checkout's ``.git``)."""
    if not repository.is_dir():
        raise WorkspaceError()
    result = _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(os.fsdecode(result.rstrip(b"\n"))).resolve()


def create_worktree(repository: Path, revision: str, destination: Path) -> Path:
    """Create a new detached checkout; retain it even if creation fails partway."""
    commit = resolve_revision(repository, revision)
    if destination.exists() or destination.is_symlink():
        raise WorkspaceError()
    _git(repository, "worktree", "add", "--detach", "--", str(destination.absolute()), commit)
    return destination


# A concurrent `git worktree add` in the same repository creates its commondir before
# writing it. Listing the registered worktrees in between reads it empty and dies with
# "failed to read .../worktrees/<id>/commondir: Success" (exit 128) before creating
# anything (#68). The path and the ": " before the error text are never translated.
_COMMONDIR_RACE = re.compile(rb"worktrees/[^/\n]+/commondir: ")
_ADD_TRIES = 3


def _lost_commondir_race(result: ProcessResult, stderr_path: Path, destination: Path) -> bool:
    """Only a stopped `git worktree add` that created nothing and names the race is retried."""
    if (result.state, result.exit_code, result.stop_confirmed) != ("response_failed", 128, True):
        return False
    if destination.exists() or destination.is_symlink():
        return False
    try:
        with stderr_path.open("rb") as stream:
            return _COMMONDIR_RACE.search(stream.read(65536)) is not None
    except OSError:
        return False


async def _pause(seconds: float, cancel: threading.Event) -> None:
    """Wait before retrying; return early once cancel is set."""
    until = time.monotonic() + seconds
    while time.monotonic() < until and not cancel.is_set():
        await asyncio.sleep(0.01)


async def prepare_worktree(
    repository: Path,
    commit: str,
    destination: Path,
    *,
    deadline_seconds: float,
    cancel: threading.Event,
    stdout_path: Path,
    stderr_path: Path,
    secrets: tuple[str, ...] = (),
) -> ProcessResult:
    """Supervise checkout and its helpers; retain partial directories and logs.

    Accept only an already-resolved full commit ID. No synchronous Git calls or
    worker threads are used; deadline/cancellation and independent stop evidence
    have the same semantics as agent execution. A try that lost the commondir race
    with a concurrent `git worktree add` is retried within the same deadline, up to
    _ADD_TRIES tries; each lost try's logs are kept as ``<log>.<try>``.
    """
    if (
        re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit) is None
        or not repository.is_dir()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise WorkspaceError()
    launch = LaunchSpec(
        (
            "git",
            "-C",
            str(repository.absolute()),
            "worktree",
            "add",
            "--detach",
            "--",
            str(destination.absolute()),
            commit,
        ),
        b"",
    )
    env = {key: value for key, value in literal_environ().items() if not key.startswith("GIT_")}
    started = time.monotonic()
    tries = 1
    while True:
        result = await execute(
            launch,
            cwd=repository.absolute(),
            env=env,
            deadline_seconds=deadline_seconds - (time.monotonic() - started),
            cancel=cancel,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            secrets=secrets,
        )
        if tries == _ADD_TRIES or not _lost_commondir_race(result, stderr_path, destination):
            break
        await _pause(random.uniform(0.05, 0.2), cancel)
        if cancel.is_set() or time.monotonic() - started >= deadline_seconds:
            break
        for path in (stdout_path, stderr_path):
            # Keep the lost try's logs; the retry writes the named logs afresh.
            with contextlib.suppress(OSError):
                path.rename(path.with_name(f"{path.name}.{tries}"))
        tries += 1
    return dataclasses.replace(result, elapsed_seconds=time.monotonic() - started)


def _fingerprint(checkout: Path) -> str:
    digest = hashlib.sha256()

    def add(data: bytes) -> None:
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    try:
        mode = checkout.stat().st_mode
    except FileNotFoundError:
        raise WorkspaceError("checkout_missing") from None
    except OSError:
        raise WorkspaceError("filesystem_unavailable") from None
    if not stat.S_ISDIR(mode):
        raise WorkspaceError("not_directory")
    add(_git(checkout, "rev-parse", "--verify", "HEAD"))
    index = _git(checkout, "ls-files", "--stage", "-z")
    add(index)
    paths = {entry.split(b"\t", 1)[1] for entry in index.split(b"\x00") if entry}
    untracked = _git(checkout, "ls-files", "--others", "--exclude-standard", "-z")
    paths.update(entry for entry in untracked.split(b"\x00") if entry)
    try:
        for name in sorted(paths):
            add(name)
            relative = Path(os.fsdecode(name))
            path = checkout / relative
            parent = checkout
            linked_parent = False
            for part in relative.parts[:-1]:
                parent /= part
                if parent.is_symlink():
                    add(b"symlink-parent")
                    add(os.fsencode(os.readlink(parent)))
                    linked_parent = True
                    break
            if linked_parent:
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError, NotADirectoryError:
                add(b"missing")
                continue
            add(str(stat.S_IFMT(mode)).encode())
            add(str(stat.S_IMODE(mode)).encode())
            if stat.S_ISLNK(mode):
                add(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                # Stream content; timestamps and porcelain status are insufficient.
                content = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        content.update(chunk)
                add(content.digest())
            elif stat.S_ISDIR(mode) and (path / ".git").exists():
                add(_fingerprint(path).encode())
    except OSError:
        raise WorkspaceError("filesystem_unavailable") from None
    return digest.hexdigest()


def _identities(checkout: Path) -> tuple[tuple[str, int, int], ...]:
    """Device/inode of the checkout path and each ancestor, as registered (not resolved)."""
    try:
        return tuple(
            (str(path), info.st_dev, info.st_ino)
            for path in (checkout, *checkout.parents)
            if path != path.parent
            for info in (os.lstat(path),)
        )
    except FileNotFoundError:
        raise WorkspaceError("checkout_missing") from None
    except OSError:
        raise WorkspaceError("filesystem_unavailable") from None


def _replaced(identities: Sequence[tuple[str, int, int]]) -> bool:
    """Whether a recorded path now exists as another file; a removed path is not a replacement."""
    for path, device, inode in identities:
        try:
            info = os.lstat(path)
        except FileNotFoundError, NotADirectoryError:
            continue
        except OSError:
            raise WorkspaceError("filesystem_unavailable") from None
        if (info.st_dev, info.st_ino) != (device, inode):
            return True
    return False


def snapshot_checkouts(repository: Path) -> CheckoutSnapshot:
    """Fingerprint all registered checkouts, retaining None and safe failure metadata."""
    records = _git(repository, "worktree", "list", "--porcelain", "-z").split(b"\x00\x00")
    result = CheckoutSnapshot()
    for record in records:
        fields = record.split(b"\x00")
        if b"bare" in fields:
            continue
        locked = any(field == b"locked" or field.startswith(b"locked ") for field in fields)
        prunable = any(field == b"prunable" or field.startswith(b"prunable ") for field in fields)
        for field in fields:
            if field.startswith(b"worktree "):
                path = os.fsdecode(field[len(b"worktree ") :])
                result.flags[path] = (locked, prunable)
                try:
                    identities = _identities(Path(path))
                    result[path] = _fingerprint(Path(path))
                    result.identities[path] = identities
                except WorkspaceError as error:
                    result[path] = None
                    result.failures[path] = CheckoutAudit(
                        path=path,
                        kind="unavailable_before",
                        reason=error.reason,
                        locked=locked,
                        prunable=prunable,
                    )
    return result


def checkout_isolation(
    repository: Path,
    before: CheckoutSnapshot,
    work_dir: Path,
    artifact_dir: Path,
    writable_paths: Sequence[Path] = (),
    private_review_refs: bool = False,
    scratch_dir: Path | None = None,
) -> CheckoutIsolation:
    """Protect registered checkouts from the agent while retaining detached Git worktree writes.

    Mounts apply from the shallowest path down, so a writable directory never hides a
    protected checkout or Git directory inside it. Each ancestor of a protected checkout
    becomes a mountpoint with the access it already had, so the agent cannot rename it
    and put another directory at the checkout's path. A registration that cannot be
    protected now (missing, unauditable at the snapshot, reached through a symlink, or
    without a resolvable Git directory) is skipped or protected only partly, and the
    isolation is incomplete; the others stay protected. Another session may still remove
    a protected checkout or its ancestors before the mounts are made.
    """
    complete = True
    checkouts: set[Path] = set()
    gitdirs: set[Path] = set()
    for path, fingerprint in before.items():
        try:
            checkout = Path(path).resolve(strict=True)
        except OSError, RuntimeError:
            complete = False
            continue
        checkouts.add(checkout)
        if fingerprint is None or checkout != Path(path):
            complete = False
        try:
            output = _git(checkout, "rev-parse", "--path-format=absolute", "--git-dir")
            gitdirs.add(Path(os.fsdecode(output.removesuffix(b"\n"))).resolve(strict=True))
        except WorkspaceError, OSError, RuntimeError:
            complete = False
    try:
        common = git_common_dir(repository)
        own_gitdir = Path(
            os.fsdecode(_git(work_dir, "rev-parse", "--path-format=absolute", "--git-dir").removesuffix(b"\n"))
        ).resolve(strict=True)
        if not own_gitdir.is_relative_to(common / "worktrees"):
            raise WorkspaceError()
        allowed = {artifact_dir.resolve(strict=True)}
        for requested in writable_paths:
            if requested.name == "pr-review-save.lock" and requested.parent.resolve(strict=True) == common:
                descriptor = os.open(requested, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise WorkspaceError()
                finally:
                    os.close(descriptor)
                allowed.add(requested.resolve(strict=True))
            else:
                allowed.add(requested.resolve(strict=True))
        for path in allowed:
            if path == common / "pr-review-save.lock":
                continue
            if not path.is_dir() or path.is_symlink() or path.is_relative_to(common):
                raise WorkspaceError()
            for checkout in checkouts:
                if path.is_relative_to(checkout) and not path.is_relative_to(work_dir):
                    _git(checkout, "check-ignore", "-q", "--", str(path))
        objects = (common / "objects").resolve(strict=True)
        registrations = (common / "worktrees").resolve(strict=True)
    except OSError, ValueError:
        raise WorkspaceError() from None

    # Registered checkouts, their Git directories and ancestors may vanish with another
    # session's cleanup before bwrap mounts them; the -try forms skip what is gone.
    modes: dict[Path, str] = {checkout: "ro-try" for checkout in checkouts}
    modes.update({gitdir: "ro-try" for gitdir in gitdirs if gitdir != common})
    # Git may create packed-refs.lock while deleting a temporary review ref.
    # An invisible overlay lets those operations complete without changing host refs.
    modes[common] = "overlay" if private_review_refs else "ro"
    modes.update({objects: "rw", registrations: "rw", own_gitdir: "rw"})
    modes.update({path: "rw" for path in allowed})
    explicit = dict(modes)
    for protected in (*checkouts, common):
        for ancestor in protected.parents:
            if (
                ancestor in modes
                or ancestor == ancestor.parent
                or ancestor.is_relative_to(common)
                or (scratch_dir is not None and ancestor == Path("/tmp"))
            ):
                continue
            enclosing = max((path for path in explicit if ancestor.is_relative_to(path)), key=_depth, default=None)
            modes[ancestor] = "rw-try" if enclosing is None or explicit[enclosing] == "rw" else "ro-try"
    options = {
        "ro": ("--ro-bind",),
        "ro-try": ("--ro-bind-try",),
        "rw": ("--bind",),
        "rw-try": ("--bind-try",),
        "overlay": ("--overlay-src", "--tmp-overlay"),
    }
    arguments = ["bwrap", "--unshare-pid", "--die-with-parent", "--bind", "/", "/"]
    arguments += ["--dev-bind", "/dev", "/dev", "--proc", "/proc"]
    if scratch_dir is not None:
        arguments += ["--bind", str(scratch_dir), "/tmp"]
    for path in sorted(modes, key=lambda path: (_depth(path), str(path))):
        for option in options[modes[path]]:
            arguments += [option, str(path)]
        if modes[path] != "overlay":
            arguments.append(str(path))
    return CheckoutIsolation(tuple(arguments), complete)


def _depth(path: Path) -> int:
    return len(path.parts)


def require_auditable_checkouts(repository: Path) -> CheckoutSnapshot:
    """Preflight for strict consumers; never repair, prune, reserve or launch."""
    try:
        snapshot = snapshot_checkouts(repository)
    except WorkspaceError as error:
        raise CheckoutAuditError(
            [CheckoutAudit(path=str(repository), kind="unavailable_before", reason=error.reason)]
        ) from None
    if snapshot.failures:
        raise CheckoutAuditError([snapshot.failures[path] for path in sorted(snapshot.failures)])
    return snapshot


def audit_checkouts(before: dict[str, str | None]) -> list[CheckoutAudit]:
    """Distinguish unavailable baselines, later failures and observed content changes.

    A recovered checkout cannot retroactively prove a run's missing baseline.
    A checkout path, or one of its ancestors, that now exists as another file is a
    replacement rather than a content change; checkout_missing means the checkout
    itself no longer exists, its ancestors kept or removed.
    Ordinary historical dictionaries remain accepted with an unknown fixed reason.
    Flags describe the captured registration, not permission to delete it.
    """
    findings: list[CheckoutAudit] = []
    for path, fingerprint in sorted(before.items()):
        locked, prunable = (
            before.flags.get(path, (False, False)) if isinstance(before, CheckoutSnapshot) else (False, False)
        )
        if fingerprint is None:
            failure = before.failures.get(path) if isinstance(before, CheckoutSnapshot) else None
            findings.append(failure or CheckoutAudit(path=path, kind="unavailable_before", reason="audit_unavailable"))
            continue
        try:
            if _replaced(before.identities.get(path, ()) if isinstance(before, CheckoutSnapshot) else ()):
                findings.append(
                    CheckoutAudit(
                        path=path, kind="replaced", reason="checkout_replaced", locked=locked, prunable=prunable
                    )
                )
                continue
            current = _fingerprint(Path(path))
        except WorkspaceError as error:
            reason = error.reason
            if reason == "checkout_missing" and os.path.lexists(path):
                # A nested checkout vanished, not this one.
                reason = "filesystem_unavailable"
            findings.append(
                CheckoutAudit(
                    path=path,
                    kind="unavailable_after",
                    reason=reason,
                    locked=locked,
                    prunable=prunable,
                )
            )
        else:
            if current != fingerprint:
                findings.append(
                    CheckoutAudit(
                        path=path,
                        kind="content_changed",
                        reason="content_changed",
                        locked=locked,
                        prunable=prunable,
                    )
                )
    return findings


def blocking_checkout_audit(
    outside_changes: Sequence[str], diagnostics: Sequence[CheckoutAudit], *, isolated: bool
) -> bool:
    """Treat only changes a fully protected, stopped agent could not make as warnings.

    Under complete isolation the agent can neither write a registered checkout nor
    move it or an ancestor, so a content change or a removed checkout is another
    session's. Replacement at the same path, any other unauditable state, a path
    mismatch and incomplete isolation still block.
    """
    if not isolated:
        return True
    if not outside_changes and not diagnostics:
        return False
    return list(outside_changes) != [finding.path for finding in diagnostics] or any(
        (finding.kind, finding.reason)
        not in {("content_changed", "content_changed"), ("unavailable_after", "checkout_missing")}
        for finding in diagnostics
    )


def changed_checkouts(before: dict[str, str | None]) -> list[str]:
    """Historical conservative warning list, without changing the audit boundary."""
    return [finding.path for finding in audit_checkouts(before)]
