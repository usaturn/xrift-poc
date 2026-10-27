#!/usr/bin/env python3
"""Create, save and retire disposable PR-review worktrees for review_run.py; the CLI serves periodic cleanup."""

from __future__ import annotations

import argparse
import base64
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, cast


class WorkspaceError(RuntimeError):
    """Report a safety invariant violation without changing repository state."""


MODEL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

MODEL_SOURCE_CODEX = "codex-rollout"
MODEL_SOURCE_EXPLICIT = "explicit"
MODEL_SOURCES = {MODEL_SOURCE_CODEX, MODEL_SOURCE_EXPLICIT}
# 候補一覧（review-findings v1）の検査。親リポジトリの .agents/scripts にある（Issue #547）
REVIEW_FINDINGS_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "review_findings.py"
# PR の解決と保存先の名前の規則。親リポジトリの .agents/scripts にある（Issue #483）
REVIEW_TARGET_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "review_target.py"
STATE_VERSION = 7
# 撤去した create --repo（#119）が作った state。残っていても後片付けが消さないよう、名前を挙げて拒む
REMOVED_STATE_VERSION = 6
TARGET_KINDS = {"checkout", "clone"}


def _load_script(name: str, path: Path) -> ModuleType:
    """.agents/scripts のスクリプトを読み込む。sys.modules には登録しない。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise WorkspaceError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def load_review_target() -> ModuleType:
    """review_target.py を一度だけ読み込む（TargetError の型を呼び出しの間で同じにする）。"""
    return _load_script("review_target", REVIEW_TARGET_SCRIPT)


@contextmanager
def target_errors() -> Generator[ModuleType]:
    """review_target を渡し、その TargetError を WorkspaceError にする。"""
    module = load_review_target()
    try:
        yield module
    except module.TargetError as error:
        raise WorkspaceError(str(error)) from error


def _target_call(function: str, *args: Any, **kwargs: Any) -> Any:
    """review_target の関数を呼び、TargetError を WorkspaceError にする。"""
    with target_errors() as module:
        return getattr(module, function)(*args, **kwargs)


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def validate_model_name(value: str) -> str:
    # Provider-style identifiers (e.g. opencode/deepseek-v4-flash-free) are
    # common; map `/` to `-` the same way head branches become review dirs.
    # Runtime display names may contain whitespace (e.g. `Cursor Grok 4.6`);
    # map each whitespace run to `-` so the agent proposal is deterministic.
    # Edge whitespace would survive as a leading/trailing `-` and silently
    # move the artifact path, so reject it before the substitution runs.
    # The rule lives in review_target.py so every reader of reviews/ shares it.
    return cast(str, _target_call("validate_model_name", value))


def resolve_codex_model_name(environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    thread_id = environment.get("CODEX_THREAD_ID", "")
    try:
        uuid.UUID(thread_id)
    except ValueError as error:
        raise WorkspaceError("CODEX_THREAD_ID is missing or invalid") from error

    codex_home = (Path(environment["CODEX_HOME"]) if "CODEX_HOME" in environment else Path.home() / ".codex").resolve()
    matches = sorted((codex_home / "sessions").rglob(f"*{thread_id}.jsonl"))
    if len(matches) != 1:
        raise WorkspaceError(f"expected exactly one Codex rollout for {thread_id}, found {len(matches)}")

    model_name: str | None = None
    with matches[0].open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                parsed: object = json.loads(line)
            except json.JSONDecodeError as error:
                if not line.endswith("\n"):
                    continue
                raise WorkspaceError(f"invalid rollout JSON at line {line_number}") from error
            record = _json_object(parsed)
            if record is None or record.get("type") != "turn_context":
                continue
            payload = _json_object(record.get("payload"))
            if payload is None:
                continue
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate:
                model_name = candidate

    if model_name is None:
        raise WorkspaceError("Codex rollout has no turn_context model")
    return validate_model_name(model_name)


def resolve_runtime_model_name(
    explicit_model_name: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    environment = os.environ if environ is None else environ
    if environment.get("CODEX_THREAD_ID"):
        runtime_model = resolve_codex_model_name(environment)
        if explicit_model_name is not None and (validate_model_name(explicit_model_name) != runtime_model):
            raise WorkspaceError("explicit model does not match Codex runtime model")
        return runtime_model, MODEL_SOURCE_CODEX
    if explicit_model_name is None:
        raise WorkspaceError("model name is required outside Codex")
    return validate_model_name(explicit_model_name), MODEL_SOURCE_EXPLICIT


def normalize_head_branch(value: str) -> str:
    return cast(str, _target_call("normalize_head_branch", value))


def review_directory_name(pr_number: int, head_branch: str) -> str:
    return cast(str, _target_call("review_directory_name", pr_number, head_branch))


def reviews_prefix(repository: str) -> str:
    """保存先の先頭 reviews/<owner>/<repo>/（Issue #483）。"""
    return f"reviews/{_target_call('validate_repository', repository)}"


def artifact_model(model_name: str, review_commit: str | None = None) -> str:
    """Keep explicit-commit reviews separate from live-head reviews, including equal SHAs."""
    if MODEL_FILENAME.fullmatch(model_name) is None:
        raise WorkspaceError("model identifier is not a safe filename")
    if review_commit is None:
        return model_name
    if re.fullmatch(r"[0-9a-f]{40}", review_commit) is None:
        raise WorkspaceError("review commit must be a full 40-character SHA")
    return f"{model_name}-commit-{review_commit}"


def review_artifact_paths(
    backup_dir: Path,
    head_branch: str,
    model_name: str,
    *,
    pr_number: int,
    repository: str,
    review_commit: str | None = None,
) -> tuple[Path, str]:
    directory = review_directory_name(pr_number, head_branch)
    filename = f"{artifact_model(model_name, review_commit)}.md"
    return backup_dir / filename, f"{reviews_prefix(repository)}/{directory}/{filename}"


def findings_artifact_paths(
    backup_dir: Path,
    head_branch: str,
    model_name: str,
    *,
    pr_number: int,
    commit: str,
    repository: str,
    review_commit: str | None = None,
) -> tuple[Path, str]:
    """候補一覧の下書きと保存先。指定commitの候補も通常headと区別する。"""
    model = artifact_model(model_name, review_commit)
    directory = review_directory_name(pr_number, head_branch)
    findings = f"{reviews_prefix(repository)}/{directory}/findings/{model}-{commit[:12]}.json"
    return backup_dir / f"{model}.findings.json", findings


def git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(f"git {' '.join(args)} failed: {message}")
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", errors="strict").strip()


# 同じリポジトリへ並行した git worktree add は、別の登録の commondir を書き終える前に読むと、何も作らずに
# "failed to read .../worktrees/<id>/commondir: Success"（終了コード 128）で止まる（#68）。パスと、エラーの
# 文の前の ": " は翻訳されない。tools/agent_runner/workspace.py の prepare_worktree と同じ判定と回数
WORKTREE_ADD_RACE = re.compile(r"worktrees/[^/\n]+/commondir: ")
WORKTREE_ADD_TRIES = 3


def wait_before_retry(seconds: float) -> None:
    time.sleep(seconds)


def add_detached_worktree(repo: Path, worktree: Path, commit: str) -> None:
    """detached の worktree を作る。並行した worktree add との競合で何も作らずに止まったときだけやり直す。"""
    for tries in range(1, WORKTREE_ADD_TRIES + 1):
        try:
            git(repo, "worktree", "add", "--detach", str(worktree), commit)
            return
        except WorkspaceError as error:
            lost = WORKTREE_ADD_RACE.search(str(error)) is not None
            if tries == WORKTREE_ADD_TRIES or not lost or worktree.exists() or worktree.is_symlink():
                raise
        wait_before_retry(random.uniform(0.05, 0.2))


def git_path(repo: Path, argument: str) -> Path:
    value = Path(str(git(repo, "rev-parse", argument)))
    if not value.is_absolute():
        value = repo / value
    return value.resolve(strict=True)


def require_main_checkout(repo_value: str) -> Path:
    repo = Path(repo_value).resolve(strict=True)
    top_level = Path(str(git(repo, "rev-parse", "--show-toplevel"))).resolve(strict=True)
    if top_level != repo:
        raise WorkspaceError(f"repo must name the exact repository root: {repo}")
    if git_path(repo, "--git-dir") != git_path(repo, "--git-common-dir"):
        raise WorkspaceError("repo must be the main checkout, not a linked worktree")
    return repo


def snapshot(repo: Path) -> dict[str, Any]:
    status = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "-z",
        "--",
        ".",
        ":(exclude)reviews",
        ":(exclude)reviews/**",
        binary=True,
    )
    assert isinstance(status, bytes)
    return {
        "branch": str(git(repo, "branch", "--show-current")),
        "head": str(git(repo, "rev-parse", "HEAD")),
        "status": base64.b64encode(status).decode("ascii"),
    }


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def destination_fingerprint(path: Path) -> dict[str, str]:
    fingerprint = {"path": str(path)}
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {**fingerprint, "kind": "missing"}
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        return {
            **fingerprint,
            "kind": "symlink",
            "sha256": hashlib.sha256(target).hexdigest(),
        }
    if stat.S_ISREG(metadata.st_mode):
        return {**fingerprint, "kind": "file", "sha256": file_digest(path)}
    if stat.S_ISDIR(metadata.st_mode):
        return {**fingerprint, "kind": "directory"}
    return {**fingerprint, "kind": "other"}


def snapshot_existing_review(
    destination: Path,
    backup_dir: Path,
) -> tuple[dict[str, str], Path | None]:
    before = destination_fingerprint(destination)
    if before["kind"] == "missing":
        return before, None
    if before["kind"] != "file":
        raise WorkspaceError("existing review destination must be a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(destination, flags)
    except OSError as error:
        raise WorkspaceError("existing review changed while snapshotting") from error

    previous = backup_dir / "previous-review.md"
    handle, temporary = tempfile.mkstemp(prefix=".previous-review.md.", dir=backup_dir)
    digest = hashlib.sha256()
    try:
        with os.fdopen(source_fd, "rb") as source, os.fdopen(handle, "wb") as output:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise WorkspaceError("existing review destination must be a regular file")
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        after = destination_fingerprint(destination)
        if after != before or digest.hexdigest() != before.get("sha256"):
            raise WorkspaceError("existing review changed while snapshotting")
        os.replace(temporary, previous)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        previous.unlink(missing_ok=True)
        raise
    return before, previous


def require_destination_parent(destination: Path) -> None:
    current = destination.parent
    while True:
        if current.exists():
            if not current.is_dir():
                raise WorkspaceError(f"review destination parent is not a directory: {current}")
            return
        if current.is_symlink():
            raise WorkspaceError(f"review destination parent is not a directory: {current}")
        current = current.parent


def current_pr_head(repository: str, number: int) -> str:
    """Fail closed on API errors; use the same fully qualified resolver as PR selection."""
    return str(_target_call("fetch_pull_request", repository, number).head_sha)


def verify_state(state: dict[str, Any], destination: Path) -> None:
    require_destination_parent(destination)
    if destination_fingerprint(destination) != state["destination_fingerprint"]:
        raise WorkspaceError("review destination changed during PR review; stop without saving or cleanup")
    if git(Path(state["worktree"]), "rev-parse", "HEAD") != state["commit"]:
        raise WorkspaceError("review worktree no longer matches the pinned commit")
    if current_pr_head(state["pr_repo"], state["pr_number"]) != state["pr_head"]:
        raise WorkspaceError("PR head changed during review; resolve again without saving")


@contextmanager
def review_save_lock(repo: Path) -> Generator[None]:
    lock_path = git_path(repo, "--git-common-dir") / "pr-review-save.lock"
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def new_session_dir(args: argparse.Namespace, pr_number: int) -> Path:
    temp_root_value = getattr(args, "temp_root", None)
    temp_root = Path(temp_root_value).resolve() if temp_root_value else Path(tempfile.gettempdir()).resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    session_dir_value = getattr(args, "session_dir", None)
    if session_dir_value is None:
        return Path(tempfile.mkdtemp(prefix=f"pr-review-{pr_number}-", dir=temp_root))
    session_dir = Path(session_dir_value).absolute()
    if (
        session_dir.parent.resolve(strict=True) != temp_root
        or re.fullmatch(rf"pr-review-{pr_number}-[a-z0-9_]{{8}}", session_dir.name) is None
        or session_dir.exists()
        or session_dir.is_symlink()
    ):
        raise WorkspaceError("requested session directory is not a fresh direct child of the temp root")
    session_dir.mkdir(mode=0o700)
    return session_dir


def require_created_worktree(worktree: Path, commit: str) -> None:
    actual_top = Path(str(git(worktree, "rev-parse", "--show-toplevel"))).resolve(strict=True)
    if actual_top != worktree.resolve(strict=True):
        raise WorkspaceError("created worktree root does not match the requested unique path")
    if git_path(worktree, "--git-dir") == git_path(worktree, "--git-common-dir"):
        raise WorkspaceError("created review directory is not a linked worktree")
    if git(worktree, "branch", "--show-current"):
        raise WorkspaceError("created review worktree is not detached")
    if git(worktree, "rev-parse", "HEAD") != commit:
        raise WorkspaceError("created review worktree does not point at the requested commit")


def session_output(state: Mapping[str, Any], findings_draft: Path, state_file: Path) -> dict[str, str | None]:
    return {
        "backup_dir": state["backup_dir"],
        "destination": state["destination"],
        "draft": state["draft"],
        "findings_draft": str(findings_draft.resolve(strict=False)),
        "model_name": state["model_name"],
        "model_source": state["model_source"],
        "previous_review": state["previous_review"],
        "review_file": PurePosixPath(state["destination"]).name,
        "reviewed_head": state["commit"],
        "pr_head": state["pr_head"],
        "base_ref": state.get("base_ref"),
        "state_file": str(state_file.resolve(strict=True)),
        "worktree": state["worktree"],
    }


def require_target_checkout(path: Path, repository: str) -> Path:
    """既存の checkout を Git 操作の対象にするとき、その origin の設定値が PR のリポジトリであること。"""
    checkout = require_main_checkout(str(path))
    with target_errors() as review_target:
        actual = review_target.origin_repository(checkout)
        if not review_target.same_repository(actual, repository):
            raise WorkspaceError(
                f"target checkout origin {actual or 'unknown'} does not match {repository}: {checkout}"
            )
    return checkout


def parse_target(value: str) -> dict[str, Any]:
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"target is not JSON: {error.msg}") from error
    target = _json_object(parsed)
    if target is None:
        raise WorkspaceError("target must be the JSON object printed by review_target.py resolve")
    target_root = target.get("target_root")
    if not isinstance(target.get("parent_root"), str) or (
        target_root is not None and not isinstance(target_root, str)
    ):
        raise WorkspaceError("target needs parent_root and target_root (null for a temporary clone)")
    return target


def require_review_commit(source: Path, commit: str) -> None:
    try:
        if git(source, "cat-file", "-t", commit) == "commit":
            return
    except WorkspaceError as error:
        raise WorkspaceError(f"review commit is not available in {source}: {commit}") from error
    raise WorkspaceError("review commit must identify a commit object")


def create(args: argparse.Namespace) -> dict[str, str | None]:
    """review_target.py resolve の JSON（args.target）の PR のセッションを作る。

    親のreviewsへ保存し、PR headと指定commitを別に固定する。
    """
    model_name, model_source = resolve_runtime_model_name(getattr(args, "model_name", None))
    target = parse_target(str(args.target))
    with target_errors() as review_target:
        clone = review_target.CloneTarget.from_json(target)
        parent = cast(Path, review_target.require_parent_root(Path(str(target["parent_root"]))))
    repository = str(clone.repository)
    number = int(clone.number)
    head_branch = str(clone.head_ref)
    pr_head = str(clone.head_sha)
    review_commit = getattr(args, "review_commit", None)
    artifact_model(model_name, review_commit)
    commit = review_commit or pr_head
    review_directory_name(number, head_branch)
    source_root = getattr(args, "source_root", None) or target["target_root"]
    checkout = require_target_checkout(Path(str(source_root)), repository) if source_root else None
    if checkout is not None and review_commit is not None:
        require_review_commit(checkout, commit)
    session_dir = new_session_dir(args, number)
    try:
        worktree = session_dir / "worktree"
        backup_dir = session_dir / "review-backup"
        backup_dir.mkdir()
        draft, destination = review_artifact_paths(
            backup_dir, head_branch, model_name, pr_number=number, repository=repository, review_commit=review_commit
        )
        destination_path = safe_destination(parent, destination, repository=repository)
        require_destination_parent(destination_path)
        findings_draft, _findings_destination = findings_artifact_paths(
            backup_dir,
            head_branch,
            model_name,
            pr_number=number,
            commit=commit,
            repository=repository,
            review_commit=review_commit,
        )
        if checkout is None:
            source = session_dir / "repo"
            with target_errors() as review_target:
                review_target.clone_repository(clone, source, checkout_branch=False)
            require_review_commit(source, commit)
            add_detached_worktree(source, worktree, commit)
        else:
            source = checkout
            before = snapshot(source)
            with target_errors() as review_target:
                review_target.fetch_pull_head(source, number, pr_head)
            add_detached_worktree(source, worktree, commit)
            if snapshot(source) != before:
                raise WorkspaceError("creating the review worktree changed the target checkout")
        if current_pr_head(repository, number) != pr_head:
            raise WorkspaceError("PR head changed after fetch; resolve again without saving")
        require_created_worktree(worktree, commit)
        state_file = session_dir / "session.json"
        with review_save_lock(parent):
            fingerprint, previous = snapshot_existing_review(destination_path, backup_dir)
            state: dict[str, Any] = {
                "version": STATE_VERSION,
                "parent_root": str(parent),
                "target_root": str(source.resolve(strict=True)),
                "target_kind": "clone" if checkout is None else "checkout",
                "pr_repo": repository,
                "worktree": str(worktree.resolve(strict=True)),
                "backup_dir": str(backup_dir.resolve(strict=True)),
                "commit": commit,
                "pr_head": pr_head,
                "review_commit": review_commit,
                "base_ref": target.get("base_ref"),
                "head_branch": head_branch,
                "pr_number": number,
                "model_name": model_name,
                "model_source": model_source,
                "draft": str(draft.resolve(strict=False)),
                "previous_review": (str(previous.resolve(strict=True)) if previous is not None else None),
                "destination": destination,
                "destination_fingerprint": fingerprint,
            }
            write_json(state_file, state)
    except WorkspaceError as error:
        raise WorkspaceError(f"{error} (session kept for inspection: {session_dir})") from error
    return session_output(state, findings_draft, state_file)


def load_state(state_value: str) -> tuple[Path, dict[str, Any]]:
    state_file = Path(state_value).resolve(strict=True)
    with state_file.open(encoding="utf-8") as stream:
        state = json.load(stream)
    required = {
        "backup_dir",
        "commit",
        "destination",
        "destination_fingerprint",
        "draft",
        "head_branch",
        "pr_number",
        "model_name",
        "model_source",
        "previous_review",
        "worktree",
    }
    version = state.get("version")
    if version == REMOVED_STATE_VERSION:
        raise WorkspaceError(
            "PR-review session state version 6 (made by the removed create --repo) is no longer supported;"
            " the session is kept: check that its review is saved, then remove it by hand"
        )
    if version != STATE_VERSION:
        raise WorkspaceError("invalid PR-review session state")
    required |= {"parent_root", "target_root", "target_kind", "pr_repo", "pr_head"}
    if not required.issubset(state) or state.get("model_source") not in MODEL_SOURCES:
        raise WorkspaceError("invalid PR-review session state")
    if re.fullmatch(r"[0-9a-f]{40}", str(state["pr_head"])) is None:
        raise WorkspaceError("invalid pinned PR head")
    artifact_model(state["model_name"], state.get("review_commit"))
    if state.get("review_commit") is not None and state["review_commit"] != state["commit"]:
        raise WorkspaceError("review commit does not match the pinned commit")
    if state_file.name != "session.json":
        raise WorkspaceError("invalid PR-review session state")
    session_dir = state_file.parent
    if not session_dir.name.startswith("pr-review-"):
        raise WorkspaceError("session state is outside a PR-review session directory")

    with target_errors() as review_target:
        repo = cast(Path, review_target.require_parent_root(Path(str(state["parent_root"]))))
    require_session_target(state, session_dir)
    backup_dir = Path(str(state.get("backup_dir", ""))).resolve(strict=True)
    worktree = Path(str(state.get("worktree", ""))).resolve(strict=True)
    if backup_dir != (session_dir / "review-backup").resolve(strict=True):
        raise WorkspaceError("session backup directory does not match its state file")
    if worktree != (session_dir / "worktree").resolve(strict=True):
        raise WorkspaceError("session worktree does not match its state file")
    previous_value = state["previous_review"]
    if previous_value is not None:
        try:
            previous = Path(str(previous_value)).resolve(strict=True)
        except OSError as error:
            raise WorkspaceError("previous review snapshot does not match its session") from error
        expected = (backup_dir / "previous-review.md").resolve()
        if previous != expected or not previous.is_file():
            raise WorkspaceError("previous review snapshot does not match its session")
    return repo, state


def require_session_target(state: Mapping[str, Any], session_dir: Path) -> None:
    """version 7 の Git 操作の対象。clone はセッションの repo/、checkout は PR のリポジトリの checkout。"""
    kind = state["target_kind"]
    target = Path(str(state["target_root"]))
    if kind == "clone":
        if target.resolve(strict=True) != (session_dir / "repo").resolve(strict=True):
            raise WorkspaceError("session clone does not match its state file")
        require_main_checkout(str(target))
    elif kind == "checkout":
        require_target_checkout(target, str(state["pr_repo"]))
    else:
        raise WorkspaceError("invalid PR-review session state")


def session_target(state: Mapping[str, Any]) -> Path:
    """worktree と pinned ref を持つリポジトリ（target_root）。"""
    return Path(str(state["target_root"])).resolve(strict=True)


def session_repository(state: Mapping[str, Any]) -> str:
    """保存先の reviews/<owner>/<repo>/ の owner/repo。"""
    return str(state["pr_repo"])


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def require_repository_layout(repo: Path, relative: PurePosixPath, repository: str, *, length: int) -> None:
    """保存先は reviews/<owner>/<repo>/#<PR>-<branch>/... に限る。

    reviews から下の既存の階層が symlink なら、別のリポジトリの保存先と混ざりうるので拒否する。
    """
    expected = PurePosixPath(reviews_prefix(repository)).parts
    if len(relative.parts) != length or relative.parts[:3] != expected or not relative.parts[3].startswith("#"):
        raise WorkspaceError(f"destination must be below {'/'.join(expected)}/#<PR>-<branch>/")
    for index in range(1, len(relative.parts)):
        component = repo.joinpath(*relative.parts[:index])
        if component.is_symlink():
            raise WorkspaceError(f"review destination component is a symlink: {component}")


def safe_destination(repo: Path, destination_value: str, *, repository: str) -> Path:
    relative = PurePosixPath(destination_value)
    if relative.is_absolute() or len(relative.parts) < 3:
        raise WorkspaceError("destination must be below reviews/<directory>/")
    if relative.parts[0] != "reviews" or any(part in {"", ".", ".."} for part in relative.parts):
        raise WorkspaceError("destination must be a normalized path below reviews/")
    if relative.suffix != ".md":
        raise WorkspaceError("review destination must end in .md")
    require_repository_layout(repo, relative, repository, length=5)

    destination = repo / Path(*relative.parts)
    resolved_destination = destination.resolve(strict=False)
    allowed_root = repo / "reviews"
    if not is_within(resolved_destination, allowed_root):
        raise WorkspaceError("destination escapes the main repository reviews/ directory")
    return destination


def safe_findings_destination(repo: Path, destination_value: str, *, repository: str) -> Path:
    """候補一覧の保存先。reviews/<owner>/<repo>/<directory>/findings/<name>.json だけを許す。"""
    relative = PurePosixPath(destination_value)
    layout = "findings destination must be reviews/<owner>/<repo>/<directory>/findings/<name>.json"
    if relative.is_absolute() or len(relative.parts) != 6:
        raise WorkspaceError(layout)
    if relative.parts[0] != "reviews" or relative.parts[4] != "findings":
        raise WorkspaceError(layout)
    if any(part in {"", ".", ".."} for part in relative.parts) or relative.suffix != ".json":
        raise WorkspaceError("findings destination must be a normalized .json path below reviews/")
    require_repository_layout(repo, relative, repository, length=6)
    destination = repo / Path(*relative.parts)
    if not is_within(destination.resolve(strict=False), repo / "reviews"):
        raise WorkspaceError("findings destination escapes the main repository reviews/ directory")
    return destination


def load_review_findings() -> ModuleType:
    """review_findings.py を読み込む。sys.modules には登録しない。"""
    return _load_script("review_findings", REVIEW_FINDINGS_SCRIPT)


def check_findings_draft(findings_draft: Path, draft: Path, state: Mapping[str, Any], model_name: str) -> None:
    """候補一覧の下書きを検査し、レビューの下書きと突き合わせる。問題があれば何も保存させない。"""
    if not findings_draft.is_file():
        raise WorkspaceError(f"findings draft is missing: {findings_draft}")
    review_findings = load_review_findings()
    try:
        value = review_findings.validate_document(json.loads(findings_draft.read_text(encoding="utf-8")))
        if value["kind"] != "candidates" or value["source"] != "reviewer":
            raise WorkspaceError("findings draft must be a candidates document written by the reviewer")
        if value["reviewed_head"] != state["commit"]:
            raise WorkspaceError("findings draft reviewed_head does not match the pinned commit")
        if value["pr"]["number"] != state["pr_number"]:
            raise WorkspaceError("findings draft PR number does not match the pinned review")
        if value["reviewer"]["review_file"] != f"{artifact_model(model_name, state.get('review_commit'))}.md":
            raise WorkspaceError("findings draft review_file does not match the pinned review file")
        if value["pr"]["repository"] != session_repository(state):
            raise WorkspaceError("findings draft repository does not match the pinned review")
        markdown = draft.read_text(encoding="utf-8")
        review_findings.check_pair(markdown, value)
        # PR 修正ループと同じ判定。保存して success を返したレビューを loop が却下しないようにする
        problem = review_findings.review_history_problem(markdown, state["commit"])
        if problem is not None:
            raise WorkspaceError(f"review draft: {problem}")
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"findings draft is not valid JSON: {error.msg}") from error
    except review_findings.FindingsError as error:
        raise WorkspaceError("findings draft is invalid: " + "; ".join(error.problems)) from error


def atomic_copy(source: Path, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as error:
        raise WorkspaceError(f"review destination parent is not a directory: {destination.parent}") from error
    handle, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with source.open("rb") as input_stream, os.fdopen(handle, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save(args: argparse.Namespace) -> dict[str, str]:
    """候補一覧を検査し、本文と両方を保存する。

    review_run.py が渡す findings=True は読まない（#119 で候補一覧なしの保存を撤去した）。
    """
    repo, state = load_state(args.state)

    with review_save_lock(repo):
        if state["model_source"] == MODEL_SOURCE_CODEX:
            runtime_model = resolve_codex_model_name()
            if runtime_model != state["model_name"]:
                raise WorkspaceError("runtime model changed during PR review; stop without saving")
        else:
            runtime_model = state["model_name"]

        backup_dir = Path(state["backup_dir"]).resolve(strict=True)
        repository = session_repository(state)
        expected_draft, expected_destination = review_artifact_paths(
            backup_dir,
            state["head_branch"],
            runtime_model,
            pr_number=state["pr_number"],
            repository=repository,
            review_commit=state.get("review_commit"),
        )
        if str(expected_draft) != state["draft"]:
            raise WorkspaceError("draft does not match the pinned review path")
        if expected_destination != state["destination"]:
            raise WorkspaceError("destination does not match the pinned review path")

        draft = expected_draft.resolve(strict=False)
        if not draft.is_file() or not is_within(draft, backup_dir):
            raise WorkspaceError("pinned draft must be a regular file inside the session backup directory")
        destination = safe_destination(repo, expected_destination, repository=repository)
        verify_state(state, destination)
        findings_draft, findings_value = findings_artifact_paths(
            backup_dir,
            state["head_branch"],
            runtime_model,
            pr_number=state["pr_number"],
            commit=state["commit"],
            repository=repository,
            review_commit=state.get("review_commit"),
        )
        findings_destination = safe_findings_destination(repo, findings_value, repository=repository)
        check_findings_draft(findings_draft, draft, state, runtime_model)
        previous = destination_fingerprint(findings_destination)
        if previous["kind"] not in {"missing", "file"}:
            raise WorkspaceError("findings destination must be a regular file")
        previous_bytes = findings_destination.read_bytes() if previous["kind"] == "file" else None
        atomic_copy(findings_draft, findings_destination)
        try:
            atomic_copy(draft, destination)
        except BaseException:
            # レビューを保存できなければ、候補一覧も保存前の状態に戻す（片方だけ新しい状態を残さない）
            if previous_bytes is None:
                findings_destination.unlink(missing_ok=True)
            else:
                findings_destination.write_bytes(previous_bytes)
            raise
    return {
        "backup": str(draft),
        "destination": str(destination),
        "findings_destination": str(findings_destination),
    }


def require_session_worktree(worktree: Path, repo: Path) -> None:
    """Refuse to delete anything but this session's linked worktree of repo."""
    try:
        top_level = Path(str(git(worktree, "rev-parse", "--show-toplevel"))).resolve(strict=True)
    except (WorkspaceError, OSError) as error:
        raise WorkspaceError(f"session path is not a linked worktree: {worktree}") from error
    if top_level != worktree:
        raise WorkspaceError(f"session path is not a linked worktree: {worktree}")
    if git_path(worktree, "--git-dir") == git_path(worktree, "--git-common-dir"):
        raise WorkspaceError(f"session path is not a linked worktree: {worktree}")
    if git_path(worktree, "--git-common-dir") != git_path(repo, "--git-common-dir"):
        raise WorkspaceError(f"session path is not a linked worktree of the pinned repository: {worktree}")


def require_saved_review(draft: Path, destination: Path) -> None:
    """Keep the session when the destination does not hold the finished draft."""
    if not draft.is_file():
        raise WorkspaceError("review draft is missing; the review is not saved")
    fingerprint = destination_fingerprint(destination)
    if fingerprint["kind"] != "file" or fingerprint.get("sha256") != file_digest(draft):
        raise WorkspaceError(
            "review destination does not hold the pinned draft; the review is not saved, so the session is kept"
        )


def read_only_git(repo: Path, *args: str) -> str:
    """Run an inspection-only Git command without optional index writes."""
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        env=environment,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(f"git {' '.join(args)} failed: {message}")
    return result.stdout.decode("utf-8", errors="strict")


def _has_hidden_tracked_index_entries(listed_files: str) -> bool:
    """Detect index flags that can hide modified tracked files from status."""
    return any(entry and (entry[0].islower() or entry[0] == "S") for entry in listed_files.split("\0"))


def _require_empty_uninitialized_submodules(repo_dir: Path) -> None:
    """Reject files inside uninitialized submodules, which status and foreach never scan."""
    for entry in read_only_git(repo_dir, "ls-files", "--stage", "-z").split("\0"):
        metadata, _, relative_path = entry.partition("\t")
        if not metadata.startswith("160000 "):
            continue
        submodule = repo_dir / relative_path
        if (submodule / ".git").exists():
            _require_empty_uninitialized_submodules(submodule)
        elif submodule.is_symlink() or (submodule.exists() and not submodule.is_dir()):
            raise WorkspaceError(f"uninitialized submodule path is not a directory: {submodule}")
        elif submodule.is_dir() and next(submodule.iterdir(), None) is not None:
            raise WorkspaceError(f"session worktree contains files inside an uninitialized submodule: {submodule}")


def _require_regular_file(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WorkspaceError(f"{description} is missing: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise WorkspaceError(f"{description} must be a regular file: {path}")


def _require_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WorkspaceError(f"{description} is missing: {path}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceError(f"{description} must be a real directory: {path}")


def _validate_reapable_session(state_value: str) -> tuple[Path, dict[str, Any]]:
    """Validate all session data that strict periodic cleanup would remove."""
    state_path = Path(os.path.abspath(state_value))
    session_dir = state_path.parent
    try:
        _require_directory(session_dir, "session directory")
        _require_regular_file(state_path, "session state")
        repo, state = load_state(str(state_path))

        pr_number = state["pr_number"]
        pinned_review_ref(pr_number)
        if re.fullmatch(rf"pr-review-{pr_number}-[a-z0-9_]{{8}}", session_dir.name) is None:
            raise WorkspaceError("session directory name does not match its pinned PR number and mkdtemp suffix")

        backup_dir = session_dir / "review-backup"
        worktree = session_dir / "worktree"
        _require_directory(backup_dir, "session backup directory")
        _require_directory(worktree, "session worktree")
        if state["backup_dir"] != str(backup_dir) or state["worktree"] != str(worktree):
            raise WorkspaceError("session paths do not match the pinned state")
        repository = session_repository(state)
        expected_entries = {"session.json", "review-backup", "worktree"}
        if state["target_kind"] == "clone":
            expected_entries.add("repo")
            clone_dir = session_dir / "repo"
            _require_directory(clone_dir, "session clone")
            if {entry.name for entry in clone_dir.iterdir()} != {".git"}:
                raise WorkspaceError("session clone contains files other than .git")
        if {entry.name for entry in session_dir.iterdir()} != expected_entries:
            raise WorkspaceError("session contains unaccounted-for files")

        expected_draft, expected_destination = review_artifact_paths(
            backup_dir,
            state["head_branch"],
            state["model_name"],
            pr_number=pr_number,
            repository=repository,
            review_commit=state.get("review_commit"),
        )
        if state["draft"] != str(expected_draft) or state["destination"] != expected_destination:
            raise WorkspaceError("session artifact paths do not match the pinned review")
        if state["previous_review"] is not None and state["previous_review"] != str(backup_dir / "previous-review.md"):
            raise WorkspaceError("previous review snapshot does not match its session")

        allowed_backup = {expected_draft.name}
        if state["previous_review"]:
            allowed_backup.add("previous-review.md")
        # 候補一覧の下書きは無くてもよい（#119 の前に --findings なしで保存したセッションには無い）
        findings_draft, _findings_destination = findings_artifact_paths(
            backup_dir,
            state["head_branch"],
            state["model_name"],
            pr_number=pr_number,
            commit=state["commit"],
            repository=repository,
            review_commit=state.get("review_commit"),
        )
        if findings_draft.name in {entry.name for entry in backup_dir.iterdir()}:
            _require_regular_file(findings_draft, "findings draft")
            allowed_backup.add(findings_draft.name)
        if {entry.name for entry in backup_dir.iterdir()} != allowed_backup:
            raise WorkspaceError("review backup contains unaccounted-for files")
        _require_regular_file(expected_draft, "review draft")
        if state["previous_review"]:
            previous = backup_dir / "previous-review.md"
            _require_regular_file(previous, "previous review snapshot")
            original_fingerprint = _json_object(state["destination_fingerprint"])
            if (
                original_fingerprint is None
                or original_fingerprint.get("kind") != "file"
                or destination_fingerprint(previous).get("sha256") != original_fingerprint.get("sha256")
            ):
                raise WorkspaceError("previous review snapshot does not match its pinned contents")

        destination = safe_destination(repo, state["destination"], repository=repository)
        require_saved_review(expected_draft, destination)
        require_session_worktree(worktree, session_target(state))
        if read_only_git(worktree, "rev-parse", "HEAD").strip() != state["commit"]:
            raise WorkspaceError("session worktree does not match the pinned commit")
        if read_only_git(worktree, "branch", "--show-current").strip():
            raise WorkspaceError("session worktree is not detached")

        status = read_only_git(worktree, "status", "--porcelain", "--untracked-files=all")
        ignored = read_only_git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard")
        index_entries = read_only_git(worktree, "ls-files", "-v", "-z")
        submodule_artifacts = read_only_git(
            worktree,
            "submodule",
            "foreach",
            "--quiet",
            "--recursive",
            "git status --porcelain --untracked-files=all && git ls-files --others --ignored --exclude-standard",
        )
        submodule_index_entries = read_only_git(
            worktree,
            "submodule",
            "foreach",
            "--quiet",
            "--recursive",
            "git ls-files -v -z",
        )
        if status or ignored or submodule_artifacts:
            raise WorkspaceError("session worktree contains dirty, untracked, or ignored files")
        _require_empty_uninitialized_submodules(worktree)
        if _has_hidden_tracked_index_entries(index_entries) or _has_hidden_tracked_index_entries(
            submodule_index_entries
        ):
            raise WorkspaceError(
                "session worktree contains tracked files with skip-worktree or assume-unchanged flags"
            )
        return repo, state
    except WorkspaceError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        raise WorkspaceError(f"invalid or incomplete PR-review session: {error}") from error


def require_reapable_session(state_file: str) -> None:
    """Refuse periodic removal unless the saved session is fully accounted for."""
    _validate_reapable_session(state_file)


def check_reap_candidate(args: argparse.Namespace) -> dict[str, str]:
    """Read-only eligibility check used by periodic workspace cleanup."""
    require_reapable_session(args.state)
    return {"status": "eligible"}


def pinned_review_ref(pr_number: int) -> str:
    """Derive the session's pinned fetch ref; it can never leave refs/pr-review/."""
    if type(pr_number) is not int or pr_number < 1:
        raise WorkspaceError("invalid PR number in session state")
    return f"refs/pr-review/{pr_number}"


def pinned_ref_commit(repo: Path, pr_ref: str) -> str | None:
    """Return the exact pinned ref's commit, not a similarly named ref."""
    existing = str(git(repo, "for-each-ref", "--format=%(refname) %(objectname)", pr_ref))
    for line in existing.splitlines():
        name, _, commit = line.partition(" ")
        if name == pr_ref:
            return commit
    return None


def delete_pinned_ref(repo: Path, pr_ref: str, expected_commit: str) -> str:
    """Only delete the ref if it still names this session's commit."""
    current = pinned_ref_commit(repo, pr_ref)
    if current is None:
        return "missing"
    if current != expected_commit:
        return "repinned"
    try:
        git(repo, "update-ref", "-d", pr_ref, expected_commit)
    except WorkspaceError:
        # A concurrent fetch can change the ref between the read and delete.
        current = pinned_ref_commit(repo, pr_ref)
        if current is None:
            return "missing"
        if current != expected_commit:
            return "repinned"
        raise
    return "deleted"


def cleanup(args: argparse.Namespace) -> dict[str, str]:
    strict_reap = bool(getattr(args, "strict_reap", False))
    try:
        repo, state = load_state(args.state)
        session_dir = Path(args.state).resolve(strict=True).parent
        worktree = Path(state["worktree"]).resolve(strict=True)
        draft = Path(state["draft"]).resolve(strict=False)
        destination = safe_destination(repo, state["destination"], repository=session_repository(state))
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        if not strict_reap:
            raise
        raise WorkspaceError(f"invalid or incomplete PR-review session: {error}") from error

    with review_save_lock(repo):
        if strict_reap:
            validated_repo, state = _validate_reapable_session(args.state)
            if validated_repo != repo:
                raise WorkspaceError("session repository changed while waiting for the cleanup lock")
            session_dir = Path(args.state).resolve(strict=True).parent
            worktree = Path(state["worktree"]).resolve(strict=True)
            draft = Path(state["draft"]).resolve(strict=False)
            destination = safe_destination(repo, state["destination"], repository=session_repository(state))
        require_saved_review(draft, destination)
        target = session_target(state)
        require_session_worktree(worktree, target)
        pr_ref = pinned_review_ref(state["pr_number"])
        pr_ref_status = delete_pinned_ref(target, pr_ref, state["pr_head"])
        git(target, "worktree", "remove", "--force", str(worktree))
        if worktree.exists():
            raise WorkspaceError(f"git did not remove the review worktree: {worktree}")
        shutil.rmtree(session_dir)
    return {
        "destination": str(destination),
        "pr_ref": pr_ref,
        "pr_ref_status": pr_ref_status,
        "session_dir": str(session_dir),
        "worktree": str(worktree),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    reap_check_parser = commands.add_parser("reap-check", help="check whether a saved session is safe to reap")
    reap_check_parser.add_argument("--state", required=True)
    reap_check_parser.set_defaults(handler=check_reap_candidate)

    cleanup_parser = commands.add_parser("cleanup", help="remove the session worktree once the review is saved")
    cleanup_parser.add_argument("--state", required=True)
    cleanup_parser.add_argument("--strict-reap", action="store_true")
    cleanup_parser.set_defaults(handler=cleanup)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (WorkspaceError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
