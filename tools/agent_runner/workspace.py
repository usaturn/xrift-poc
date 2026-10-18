"""Detached worktrees and observational audits, never automatic cleanup.

Audits cover HEAD, index entries, tracked files and nonignored untracked files
(including initialized submodule checkouts). Gitignored resources, symlink
referents, and arbitrary directories outside registered checkouts are outside
this boundary. A changed fingerprint is evidence of change, not attribution.
"""

import hashlib
import os
import re
import stat
import subprocess
import threading
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
    """Keep the historical mapping API while preserving checkout-local diagnostics."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: dict[str, CheckoutAudit] = {}
        self.flags: dict[str, tuple[bool, bool]] = {}


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
    have the same semantics as agent execution.
    """
    if (
        re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit) is None
        or not repository.is_dir()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise WorkspaceError()
    return await execute(
        LaunchSpec(
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
        ),
        cwd=repository.absolute(),
        env={key: value for key, value in literal_environ().items() if not key.startswith("GIT_")},
        deadline_seconds=deadline_seconds,
        cancel=cancel,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        secrets=secrets,
    )


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
                    result[path] = _fingerprint(Path(path))
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
            current = _fingerprint(Path(path))
        except WorkspaceError as error:
            findings.append(
                CheckoutAudit(
                    path=path,
                    kind="unavailable_after",
                    reason=error.reason,
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


def changed_checkouts(before: dict[str, str | None]) -> list[str]:
    """Historical conservative warning list, without changing the audit boundary."""
    return [finding.path for finding in audit_checkouts(before)]
