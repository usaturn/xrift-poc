#!/usr/bin/env python3
"""Prepare and complete one noninteractive PR review using the existing workspace helper."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, cast

# This CLI can be invoked from a different repository's review worktree.
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from tools.agent_runner.dsh_identity import read_session_identity  # noqa: E402
from tools.agent_runner.models import BatchResult, Identity, Request, Runtime  # noqa: E402

BINDING_KEYS = ("run_id", "request_id", "agent_id", "role")
SKILL_PATH = ".agents/skills/pr-review/SKILL.md"
EXECUTION_SECTION = "\n## Review timing\n\n<!-- pr-review:execution -->\n"


def _workspace() -> ModuleType:
    path = Path(__file__).with_name("pr_review_workspace.py")
    spec = importlib.util.spec_from_file_location("review_workspace", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = _workspace()
parent_data = workspace._load_script("pr_review_parent", Path(__file__).with_name("pr_review_parent.py"))
records = workspace._load_script("review_records", ROOT / ".agents/scripts/review_records.py")
record = workspace._load_script("review_run_record", Path(__file__).with_name("review_run_record.py"))
FAILURES = (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise workspace.WorkspaceError(f"cannot read JSON {path}: {error}") from error


def command(argv: list[str]) -> str:
    result = subprocess.run(argv, check=False, text=True, capture_output=True)
    if result.returncode:
        raise workspace.WorkspaceError(f"{argv[0]} failed: {result.stderr.strip()}")
    return result.stdout


def request_binding(path: str | None, *, work_dir: Path | None = None) -> dict[str, str]:
    if path is None:
        return dict.fromkeys(BINDING_KEYS, "unknown")
    request = Request.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if request.work_dir.resolve(strict=True) != (work_dir or Path.cwd()).resolve(strict=True):
        raise workspace.WorkspaceError("request work_dir must match the invocation cwd, not the review worktree")
    if request.result_shape != "json":
        raise workspace.WorkspaceError("noninteractive review requires request result_shape=json")
    return {key: str(getattr(request, key)) for key in BINDING_KEYS}


def request_deadline(path: str) -> float:
    return Request.model_validate_json(Path(path).read_text(encoding="utf-8")).deadline_seconds


def observed_dsh_identity() -> Identity:
    if os.environ.get("DSH_SHELL") != "1" or os.environ.get("DSH_PROFILE") != "headless":
        raise workspace.WorkspaceError("DSH headless identity must be read inside its own bash tool")
    home, session = os.environ.get("DSH_HOME"), os.environ.get("DSH_SESSION_ID")
    if not home or not session:
        raise workspace.WorkspaceError("DSH_HOME and DSH_SESSION_ID must both be observed")
    return read_session_identity(Path(home), Path.cwd(), session, live=True)


def observed_model(runtime: Runtime, supplied: str | None) -> tuple[str | None, str]:
    if runtime == "dsh":
        identity = observed_dsh_identity()
        if supplied is not None and supplied != identity.model:
            raise workspace.WorkspaceError("--model-name does not match the observed DSH model")
        return identity.model, identity.provider or "unknown"
    if runtime == "codex":
        if supplied is not None:
            raise workspace.WorkspaceError("Codex model must come from the current rollout, not --model-name")
        return None, "unknown"
    if runtime == "pi":
        provider, model = os.environ.get("PI_PROVIDER"), os.environ.get("PI_MODEL")
        if not provider or not model:
            raise workspace.WorkspaceError("PI_PROVIDER and PI_MODEL must both be observed in this runtime")
        if supplied is not None and supplied != model:
            raise workspace.WorkspaceError("--model-name does not match this runtime's PI_MODEL")
        return model, provider
    if not supplied:
        raise workspace.WorkspaceError("exact runtime model is unavailable; fail without asking a question")
    return supplied, "unknown"


def read_records(target: dict[str, Any], comments_path: str | None) -> dict[str, Any]:
    if comments_path is not None:
        payload = read_json(Path(comments_path))
    else:
        endpoint = f"repos/{target['repository']}/issues/{target['number']}/comments"
        text = command(["gh", "api", "--paginate", "--slurp", endpoint])
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise workspace.WorkspaceError("PR comments API did not return JSON") from error
    comments = records.comments_from_api(payload)
    return dict(
        records.read_state(comments, expected_pr={"repository": target["repository"], "number": target["number"]})
    )


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_record_path(parent: Path, state: Mapping[str, Any] | None) -> Path | None:
    """state に書いた実行記録のパス。記録を持たない state（この変更より前のセッション）なら None。"""
    context = cast(dict[str, Any], (state or {}).get("run_context") or {})
    relative = context.get("run_record")
    if not relative or state is None:
        return None
    return record.safe_record_path(parent, str(relative), str(state["pr_repo"]))


def locate_record(parent: Path, state: Mapping[str, Any]) -> tuple[Path | None, str | None]:
    """run_record_path の失敗も、レビューの結果を変えない記録の失敗として返す。"""
    try:
        return run_record_path(parent, state), None
    except FAILURES as error:
        return None, f"run record not updated: {error}"


def record_update(parent: Path, path: Path | None, change: Any) -> str | None:
    """実行記録を更新する。失敗してもレビューの結果は変えず、理由を返す（4.1 節）。"""
    if path is None:
        return None
    try:
        record.update(parent, path, change)
    except FAILURES as error:
        return f"run record not updated: {error}"
    return None


def record_fields(path: Path | None, error: str | None) -> dict[str, Any]:
    fields: dict[str, Any] = {"run_record": str(path) if path is not None else None}
    if error is not None:
        fields["run_record_error"] = error
    return fields


def helper_write_exceptions(
    *,
    tool_root: Path,
    parent: Path,
    parent_git: Path,
    review_dir: str,
    number: int,
    source_git: Path | None = None,
) -> tuple[str, ...]:
    """この helper が管理する書き込み先。呼び出し側が runner の作業範囲の指示に、許可する例外として載せる。

    writes=False は、レビュー対象・どの checkout の追跡ファイル・他の担当の成果を変えないという意味で、
    ここに挙げる helper の書き込みはそれに当たらない（references/noninteractive.md）。loop の inspect と
    review-fix-planning の self-review が使う。パスを事前に決められるものは、パスを行の先頭に置く。

    review_dir は保存先のディレクトリで、ブランチ名を知らない呼び出し側は ``#<PR>-*`` のパターンを渡す。
    source_git は ``--source-root`` の checkout の Git の共有ディレクトリ。親（parent_git）と違えば、
    helper は review 用の worktree・ref・オブジェクトをそちらに書き、親には保存ロックだけを書く
    （DSH は専用 clone に取得し checkout の Git には書かないため、Git の許可行は non-DSH に限定する）。
    """
    worktree_data = (
        "the Git objects, refs and worktree registrations that the helper adds and removes for its review worktree"
    )
    git_scope = "non-DSH helper only; DSH uses a dedicated clone and does not write checkout Git data"
    if source_git is None or source_git == parent_git:
        git_data = (
            f"{parent_git}: the helper's save lock (pr-review-save.lock) and {worktree_data} (a submodule's Git data "
            "is here too; a repository without a checkout here is cloned inside the helper's session instead; "
            f"{git_scope})",
        )
    else:
        git_data = (
            f"{parent_git / 'pr-review-save.lock'}: the helper's save lock ({git_scope})",
            f"{source_git}: {worktree_data} in the --source-root checkout ({git_scope})",
        )
    return (
        f"{tool_root}: the source checkout (tool_root) that holds the pr-review skill, its references and its "
        "helper. Read them and run the helper from there; do not edit it",
        f"{review_dir}: the run records and the saved review and candidates that the helper writes; "
        "do not edit them by hand",
        *git_data,
        f"{parent / 'tmp/agents'}/pr-review-scratch-{number}.*: one scratch directory created with "
        "agent_tmp.py mkdir as pr-review allows; remove it when it is no longer needed",
        f"the session that the helper's prepare creates (pr-review-{number}-* in the temporary directory, or "
        "the DSH staging area): its review worktree, backup and state belong to the helper. Write only the "
        "draft and findings_draft that prepare returns, and leave no files in the review worktree",
        f"{tool_root / 'tools/devenv/.venv'} and uv's cache: the environment that "
        "`uv run --project <tool_root>/tools/devenv --locked` maintains",
    )


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    started_at, started_monotonic = utc_now(), time.monotonic()
    binding = request_binding(args.request)
    model, provider = observed_model(args.runtime, args.model_name)
    artifact_identity = f"{provider}/{model}" if args.runtime == "dsh" else model
    model_name, _source = workspace.resolve_runtime_model_name(artifact_identity)
    snapshot = None
    request: Request | None = None
    if args.runtime == "dsh":
        if not args.request:
            raise workspace.WorkspaceError("DSH review requires an authenticated parent's Request")
        request = Request.model_validate_json(Path(args.request).read_text(encoding="utf-8"))
        snapshot = parent_data.load(workspace, parent_data.descriptor_from_prompt(request.prompt), args.pr)
        if snapshot["review_commit"] != args.review_commit:
            raise workspace.WorkspaceError("review commit does not match the parent snapshot")
        target = snapshot["target"]
        requested_head = parent_data.parameters_from_prompt(request.prompt).get("pr_head")
        if requested_head is not None and requested_head != target["head_sha"]:
            raise workspace.WorkspaceError("parent snapshot does not match Request PR HEAD")
        parent = Path(target["parent_root"])
    else:
        parent = workspace._target_call("default_parent_root")
        target = workspace._target_call("resolve", args.pr, parent_root=parent)
    skill = {"path": SKILL_PATH, "sha256": hashlib.sha256((ROOT / SKILL_PATH).read_bytes()).hexdigest()}
    provenance = {
        **binding,
        "provider": provider,
        "effort": "unknown",
        "session_id": "unknown",
        "cli_version": "unknown",
        "skills": [skill],
    }
    if args.runtime == "dsh":
        observed = observed_dsh_identity()
        provenance.update(effort=observed.effort or "unknown", session_id=observed.session_id or "unknown")
    identity = {"model": model or model_name, "runtime": args.runtime, "provenance": provenance}
    review_kind = "commit" if args.review_commit else "pr_head"
    # 記録の reviewed_head は完全なハッシュなので、create と同じ検査を記録より先に行う
    workspace.artifact_model(model_name, args.review_commit)
    # 記録の名前にセッションの接尾辞を使うので、セッションの名前を先に決めて create に渡す
    session_name, suffix = record.new_session_name(int(target["number"]))
    temp_root = (
        parent_data.staging_root(workspace, target, request)
        if snapshot is not None and request is not None
        else Path(args.temp_root).resolve()
        if args.temp_root
        else Path(tempfile.gettempdir()).resolve()
    )
    try:
        record_path = record.safe_record_path(
            parent, record.record_relative(target, started_at, model_name, suffix), str(target["repository"])
        )
        record.create(
            parent,
            record_path,
            record.started_document(
                target=target,
                review_kind=review_kind,
                reviewed_head=args.review_commit or target["head_sha"],
                reviewer=identity,
                started_at=started_at,
            ),
        )
    except FAILURES as error:
        raise workspace.WorkspaceError(f"cannot write run record: {error}") from error

    def failed_prepare(reason: str) -> dict[str, Any]:
        elapsed = time.monotonic() - started_monotonic
        seconds = {"prepare_seconds": elapsed, "total_seconds": elapsed}
        error = record_update(
            parent, record_path, record.mark_failed("prepare", reason, finished_at=utc_now(), seconds=seconds)
        )
        return record_fields(record_path, error)

    try:
        decision_state = (
            dict(
                records.read_state(
                    records.comments_from_api(snapshot["comments"]),
                    expected_pr={"repository": target["repository"], "number": target["number"]},
                )
            )
            if snapshot is not None
            else read_records(target, args.records_comments)
        )
        session = workspace.create(
            argparse.Namespace(
                target=json.dumps(target),
                model_name=artifact_identity,
                review_commit=args.review_commit,
                source_root=args.source_root,
                private_clone=args.runtime == "dsh",
                parent_input=snapshot,
                temp_root=str(temp_root),
                session_dir=str(temp_root / session_name),
            )
        )
    except FAILURES as error:
        return {
            **result_fields(None),
            **binding,
            "status": "failed",
            "reason": str(error),
            "repository": target["repository"],
            "pr_number": target["number"],
            **failed_prepare(str(error)),
        }
    state_file = Path(session["state_file"])
    state: dict[str, Any] = {}
    try:
        _parent, state = workspace.load_state(str(state_file))
        reviewer = {**identity, "review_file": session["review_file"]}
        context = {
            "binding": binding,
            "request_path": str(Path(args.request).resolve(strict=True)) if args.request else None,
            "request_sha256": workspace.file_digest(Path(args.request)) if args.request else None,
            "started_at": started_at,
            "started_monotonic": started_monotonic,
            "reviewer": reviewer,
            "skill": skill,
            "run_record": PurePosixPath(record_path.relative_to(parent)).as_posix(),
            "parent_pr_data": snapshot["descriptor"] if snapshot is not None else None,
        }
        state["run_context"] = context
        workspace.write_json(state_file, state)
        base_ref = target["base_ref"]
        # Keep the base private to this owned worktree, including during concurrent reviews.
        worktree = Path(session["worktree"])
        ref = "refs/worktree/pr-review-base"
        if snapshot is None:
            workspace.git(
                worktree,
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                "--no-tags",
                "--refmap=",
                "origin",
                f"+refs/heads/{base_ref}:{ref}",
            )
        else:
            workspace.git(worktree, "update-ref", ref, snapshot["base"])
        base = workspace.git(worktree, "merge-base", ref, state["commit"])
        if snapshot is not None and base != snapshot["base"]:
            raise workspace.WorkspaceError("bundle does not match the prepared base")
        document: dict[str, Any] = {
            "schema": "review-findings/v2",
            "kind": "candidates",
            "source": "reviewer",
            "pr": {"repository": target["repository"], "number": target["number"]},
            "reviewed_head": state["commit"],
            "base": base,
            "reviewer": reviewer,
            "skill": skill,
            "timing": {"started_at": started_at, "finished_at": started_at, "seconds": None, "waiting_seconds": None},
            "coverage": {"read": [], "checks": [], "declined": []},
            "findings": [],
            "unverified_candidates": [],
        }
        context["immutable"] = {
            key: document[key]
            for key in (
                "schema",
                "kind",
                "source",
                "pr",
                "reviewed_head",
                "base",
                "reviewer",
                "skill",
            )
        }
        fingerprint = state["destination_fingerprint"]
        inputs = {
            "request": (
                {"sha256": context["request_sha256"], "deadline_seconds": request_deadline(args.request)}
                if args.request
                else None
            ),
            "previous_review": {"sha256": fingerprint["sha256"]} if fingerprint["kind"] == "file" else None,
            "records": record.records_input(decision_state),
            "diff": record.diff_input(worktree, str(base), state["commit"]),
        }
        context["prepared_monotonic"] = time.monotonic()
        workspace.write_json(state_file, state)
        workspace.write_json(Path(session["findings_draft"]), document)
    except (OSError, ValueError, RuntimeError) as error:
        return {
            **result_fields(state),
            **binding,
            "status": "failed",
            "reason": str(error),
            "repository": target["repository"],
            "pr_number": target["number"],
            "pr_head": session["pr_head"],
            "reviewed_head": session["reviewed_head"],
            "state_file": str(state_file),
            "retained_paths": retained_paths({**session, "pr_number": target["number"]}),
            **failed_prepare(str(error)),
        }
    record_error = record_update(
        parent,
        record_path,
        record.mark_prepared(
            mode="Update" if state["previous_review"] is not None else "Initial",
            base=str(base),
            inputs=inputs,
            prepare_seconds=context["prepared_monotonic"] - started_monotonic,
        ),
    )
    return {
        **session,
        **binding,
        "status": "prepared",
        "repository": target["repository"],
        "pr_number": target["number"],
        "pr_url": target["url"],
        "base": base,
        "started_at": started_at,
        "reviewer": reviewer,
        "records": decision_state,
        "requirements": snapshot["requirements"] if snapshot is not None else None,
        "diff_command": ["git", "-C", session["worktree"], "diff", "--find-renames", f"{base}..{state['commit']}"],
        **record_fields(record_path, record_error),
    }


def retained_paths(state: dict[str, Any]) -> dict[str, str]:
    return {
        "worktree": state["worktree"],
        "draft": state["draft"],
        "session_dir": str(Path(state["worktree"]).parent),
        "pr_ref": workspace.pinned_review_ref(state["pr_number"]),
    }


def result_fields(state: dict[str, Any] | None = None) -> dict[str, Any]:
    context = (state or {}).get("run_context", {})
    return {
        "schema_version": 1,
        **context.get("binding", dict.fromkeys(BINDING_KEYS, "unknown")),
        "repository": (state or {}).get("pr_repo"),
        "pr_number": (state or {}).get("pr_number"),
        "pr_head": (state or {}).get("pr_head"),
        "reviewed_head": (state or {}).get("commit"),
        "review_file": None,
        "findings_file": None,
        "active_findings": None,
        "cleanup": "not_attempted",
        "cleanup_details": None,
        "retained_paths": None,
        "reason": None,
    }


def complete(
    args: argparse.Namespace, *, parent_request: Request | None = None, parent_identity: Identity | None = None
) -> dict[str, Any]:
    parent, state = workspace.load_state(args.state)
    context_value = state.get("run_context")
    if not isinstance(context_value, dict) or "immutable" not in context_value:
        raise workspace.WorkspaceError("state is missing the prepared request context")
    context = cast(dict[str, Any], context_value)
    record_path, located = locate_record(parent, state)
    complete_started = time.monotonic()
    prepared = context.get("prepared_monotonic")
    review_seconds = complete_started - prepared if isinstance(prepared, (int, float)) else None
    stage = "complete"
    save_started: float | None = None
    try:
        if context["reviewer"]["runtime"] == "dsh":
            observed = parent_identity if parent_identity is not None else observed_dsh_identity()
            previous = context["reviewer"]
            metadata = previous["provenance"]
            if (observed.model, observed.provider, observed.effort or "unknown", observed.session_id) != (
                previous["model"],
                metadata["provider"],
                metadata["effort"],
                metadata["session_id"],
            ):
                raise workspace.WorkspaceError("observed DSH route changed during review")
        request_path = context["request_path"]
        if request_path is not None and (
            workspace.file_digest(Path(request_path)) != context["request_sha256"]
            or request_binding(request_path, work_dir=parent_request.work_dir if parent_request else None)
            != context["binding"]
        ):
            raise workspace.WorkspaceError("request binding changed during review")
        if hashlib.sha256((ROOT / SKILL_PATH).read_bytes()).hexdigest() != context["skill"]["sha256"]:
            raise workspace.WorkspaceError("loaded review skill changed during review")
        if context.get("parent_pr_data") is not None:
            snapshot = parent_data.load(
                workspace,
                context["parent_pr_data"],
                f"https://github.com/{state['pr_repo']}/pull/{state['pr_number']}",
            )
            if (
                snapshot["target"]["head_sha"],
                snapshot["base"],
                snapshot["review_commit"] or snapshot["target"]["head_sha"],
            ) != (state["pr_head"], context["immutable"]["base"], state["commit"]):
                raise workspace.WorkspaceError("parent snapshot target changed")
        draft = Path(state["draft"])
        markdown = draft.read_text(encoding="utf-8")
        findings, _destination = workspace.findings_artifact_paths(
            Path(state["backup_dir"]),
            state["head_branch"],
            state["model_name"],
            pr_number=state["pr_number"],
            commit=state["commit"],
            repository=state["pr_repo"],
            review_commit=state.get("review_commit"),
        )
        document_value = read_json(findings)
        if not isinstance(document_value, dict):
            raise workspace.WorkspaceError("candidates must be a JSON object")
        document = cast(dict[str, Any], document_value)
        if any(document.get(key) != value for key, value in context["immutable"].items()):
            raise workspace.WorkspaceError("candidates do not match the prepared target or reviewer")
        coverage = document.get("coverage")
        if not isinstance(coverage, dict) or not cast(dict[str, Any], coverage).get("read"):
            raise workspace.WorkspaceError("review coverage is missing; an unreviewed scaffold is not zero findings")
        staged = context.get("staged")
        if parent_request is not None:
            if (
                staged is None
                or staged["draft_sha256"] != workspace.file_digest(draft)
                or staged["findings_sha256"] != workspace.file_digest(findings)
            ):
                raise workspace.WorkspaceError("review drafts changed after staging")
        timing = (
            staged["timing"]
            if staged is not None
            else {
                "started_at": context["started_at"],
                "finished_at": utc_now(),
                "seconds": round(time.monotonic() - context["started_monotonic"], 6),
                "waiting_seconds": None,
            }
        )
        document["timing"] = timing
        execution = {
            "reviewer": context["reviewer"],
            "timing": timing,
            "pr_head": state["pr_head"],
            "reviewed_head": state["commit"],
            "review_kind": "commit" if state.get("review_commit") else "pr_head",
        }
        # Repeating a refused completion must not accumulate misleading timing blocks.
        markdown = markdown.split(EXECUTION_SECTION, 1)[0].rstrip() + EXECUTION_SECTION
        markdown += "```json\n" + json.dumps(execution, ensure_ascii=False, indent=2) + "\n```\n"
        workspace.write_json(findings, document)
        draft.write_text(markdown, encoding="utf-8")
        if context.get("parent_pr_data") is not None and parent_request is None:
            # Only the authenticated parent can recheck the live HEAD and publish.
            context["staged"] = {
                "timing": timing,
                "draft_sha256": workspace.file_digest(draft),
                "findings_sha256": workspace.file_digest(findings),
            }
            workspace.write_json(Path(args.state), state)
            return {
                **result_fields(state),
                "status": "staged",
                "staged": context["staged"],
                "prepared": context["immutable"],
                "state_file": args.state,
                "reviewer": context["reviewer"],
                "timing": timing,
                "retained_paths": retained_paths(state),
            }
        stage = "save"
        save_started = time.monotonic()
        saved = workspace.save(argparse.Namespace(state=args.state, findings=True))
    except FAILURES as error:
        now = time.monotonic()
        seconds = {
            "review_seconds": review_seconds,
            "save_seconds": now - save_started if save_started is not None else None,
            "total_seconds": now - context["started_monotonic"],
        }
        record_error = record_update(
            parent, record_path, record.mark_failed(stage, str(error), finished_at=utc_now(), seconds=seconds)
        )
        return {
            **result_fields(state),
            "status": "failed",
            "reason": str(error),
            "state_file": args.state,
            "retained_paths": retained_paths(state),
            **record_fields(record_path, record_error or located),
        }
    save_seconds = time.monotonic() - save_started
    # 保存は済んでいる。記録の参照を作れなくても、レビューの結果と後片付けは変えない
    outputs: dict[str, Any] | None = None
    try:
        outputs = record.outputs_document(
            review_path=Path(saved["destination"]),
            findings_path=Path(saved["findings_destination"]),
            document=document,
        )
    except FAILURES as error:
        located = located or f"run record not updated: {error}"
    cleanup_started = time.monotonic()
    result = {
        **result_fields(state),
        "status": "success",
        "review_file": saved["destination"],
        "findings_file": saved["findings_destination"],
        "active_findings": len(document["findings"]),
        "cleanup": "success",
        "reviewer": context["reviewer"],
        "timing": timing,
        "save_seconds": save_seconds,
    }
    try:
        result["cleanup_details"] = workspace.cleanup(argparse.Namespace(state=args.state))
    except (OSError, ValueError, RuntimeError) as error:
        result.update(
            status="failed",
            cleanup="failed",
            reason=str(error),
            state_file=args.state,
            retained_paths=retained_paths(state),
        )
    result["cleanup_seconds"] = time.monotonic() - cleanup_started
    result["total_seconds"] = time.monotonic() - context["started_monotonic"]
    seconds = {
        "review_seconds": review_seconds,
        "save_seconds": save_seconds,
        "cleanup_seconds": result["cleanup_seconds"],
        "total_seconds": result["total_seconds"],
    }
    if outputs is None:
        result.update(record_fields(record_path, located))
        return result
    finished_at = utc_now()
    change = (
        record.mark_success(outputs, finished_at=finished_at, seconds=seconds)
        if result["status"] == "success"
        else record.mark_failed("cleanup", result["reason"], finished_at=finished_at, seconds=seconds, outputs=outputs)
    )
    result.update(record_fields(record_path, record_update(parent, record_path, change) or located))
    return result


def with_text_body(value: Any) -> dict[str, Any] | None:
    """GitHub は空の本文を null で返す。空文字にした object、形が違えば None。"""
    if not isinstance(value, dict):
        return None
    document = cast(dict[str, Any], value)
    if "body" not in document:
        return None
    if document["body"] is None:
        document["body"] = ""
    return document if isinstance(document["body"], str) else None


def referenced_issue(repository: str, number: int) -> dict[str, Any]:
    """PR 本文が参照する Issue。失敗の理由には gh の出力を写さない（秘密値が混ざりうる）。"""
    try:
        workspace._target_call("validate_repository", repository)
        text = command(["gh", "issue", "view", str(number), "--repo", repository, "--json", "body,comments"])
    except FAILURES as error:
        raise workspace.WorkspaceError("Issue is not readable with gh issue view") from error
    try:
        issue = with_text_body(json.loads(text))
    except ValueError as error:
        raise workspace.WorkspaceError("Issue response is not JSON") from error
    if issue is None or not isinstance(issue.get("comments"), list):
        raise workspace.WorkspaceError("Issue requirements metadata is incomplete")
    return issue


def parent_requirements(pr: str, repository: str, number: int) -> dict[str, Any]:
    """対象 PR の要件は必須。参照 Issue を読めなければ理由付きの未取得として渡し、レビューは止めない。"""
    document = with_text_body(
        json.loads(command(["gh", "pr", "view", pr, "--repo", repository, "--json", "body,closingIssuesReferences"]))
    )
    if document is None or not isinstance(document.get("closingIssuesReferences"), list):
        raise workspace.WorkspaceError("PR requirements metadata is incomplete")
    references: set[tuple[str, int]] = {
        (repository, int(n)) for n in re.findall(r"(?<![\w/])#([1-9][0-9]*)", document["body"])
    }
    for entry in document["closingIssuesReferences"]:
        url = cast(dict[str, Any], entry).get("url") if isinstance(entry, dict) else None
        match = re.fullmatch(
            r"https://github.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)", url if isinstance(url, str) else ""
        )
        if match is None:
            raise workspace.WorkspaceError("closing Issue URL is invalid")
        references.add((match[1], int(match[2])))
    for match in re.finditer(
        r"https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)", document["body"]
    ):
        references.add((match[1], int(match[2])))
    issues: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for repo, issue_number in sorted(references - {(repository, number)}):
        try:
            issues.append({"repository": repo, "number": issue_number, **referenced_issue(repo, issue_number)})
        except workspace.WorkspaceError as error:
            unavailable.append({"repository": repo, "number": issue_number, "reason": str(error)})
    return {"pr": document, "issues": issues, "unavailable": unavailable}


def prepare_parent(args: argparse.Namespace) -> dict[str, str]:
    """Shared parent preparation for ordinary runner calls and the review loop."""
    if args.records_comments:
        comments = read_json(Path(args.records_comments))
    else:
        target = workspace._target_call("resolve", args.pr, parent_root=workspace._target_call("default_parent_root"))
        comments = json.loads(
            command(
                [
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/{target['repository']}/issues/{target['number']}/comments",
                ]
            )
        )
    records.comments_from_api(comments)
    requirements = getattr(args, "requirements", None)
    if requirements is None:
        parsed = workspace._target_call("parse_pull_request_argument", args.pr)
        repository = parsed.repository or workspace._target_call(
            "parent_repository", workspace._target_call("default_parent_root")
        )
        requirements = parent_requirements(args.pr, repository, parsed.number)
    return parent_data.prepare(workspace, args, comments, requirements)


def finalize_parent_batch(batch: BatchResult) -> None:
    """Revalidate stopped staged attempts; never trust payload paths by themselves."""
    for result in batch.results:
        if not result.attempts:
            continue
        attempt = result.attempts[-1]
        payload = attempt.payload
        if not isinstance(payload, dict) or payload.get("status") != "staged":
            continue
        try:
            if (
                result.state != "success"
                or attempt.state != "success"
                or attempt.exit_code != 0
                or not attempt.stop_confirmed
                or attempt.sensitive_paths
            ):
                raise workspace.WorkspaceError("staged reviewer did not stop successfully with sensitive cleanup")
            if attempt.request_path is None or attempt.work_dir is None:
                raise workspace.WorkspaceError("staged reviewer has no actual Request")
            request = Request.model_validate_json(parent_data.regular_bytes(attempt.request_path))
            binding = {key: str(getattr(request, key)) for key in BINDING_KEYS}
            if (
                (request.run_id, request.request_id, request.agent_id, request.role, request.work_dir)
                != (batch.run_id, result.request_id, result.actual_agent, result.role, attempt.work_dir)
                or attempt.agent_id != request.agent_id
                or any(payload.get(k) != v for k, v in binding.items())
            ):
                raise workspace.WorkspaceError("staged result does not match actual Request")
            descriptor = parent_data.descriptor_from_prompt(request.prompt)
            state_path = Path(str(payload["state_file"]))
            parent_data.regular_bytes(state_path)
            snapshot = parent_data.load(
                workspace, descriptor, f"https://github.com/{payload['repository']}/pull/{payload['pr_number']}"
            )
            staging = parent_data.staging_root(workspace, snapshot["target"], request)
            if not state_path.is_relative_to(staging):
                raise workspace.WorkspaceError("staged session is outside this Request's PR staging directory")
            _parent, state = workspace.load_state(str(state_path))
            context = state.get("run_context", {})
            if (
                context.get("binding") != binding
                or context.get("parent_pr_data") != descriptor
                or payload.get("reviewer") != context.get("reviewer")
                or payload.get("staged") != context.get("staged")
                or payload.get("prepared") != context.get("immutable")
                or context.get("request_path") != str(attempt.request_path)
            ):
                raise workspace.WorkspaceError("staged session does not match its prepared context")
            if any(
                payload.get(key) != value
                for key, value in {
                    "repository": state["pr_repo"],
                    "pr_number": state["pr_number"],
                    "pr_head": state["pr_head"],
                    "reviewed_head": state["commit"],
                }.items()
            ):
                raise workspace.WorkspaceError("staged result target changed")
            if context["reviewer"]["runtime"] != "dsh":
                raise workspace.WorkspaceError("parent staging is only supported for DSH")
            parent_data.regular_bytes(Path(state["draft"]))
            findings, _destination = workspace.findings_artifact_paths(
                Path(state["backup_dir"]),
                state["head_branch"],
                state["model_name"],
                pr_number=state["pr_number"],
                commit=state["commit"],
                repository=state["pr_repo"],
                review_commit=state.get("review_commit"),
            )
            parent_data.regular_bytes(findings)
            attempt.payload = complete(
                argparse.Namespace(state=str(state_path)), parent_request=request, parent_identity=attempt.identity
            )
            if attempt.payload.get("status") == "success":
                # Remove only empty Request-owned staging directories, never other sessions.
                for empty in (staging, staging.parent, staging.parent.parent, staging.parent.parent.parent):
                    try:
                        empty.rmdir()
                    except OSError:
                        break
        except FAILURES as error:
            attempt.payload = {**payload, "status": "failed", "reason": str(error), "cleanup": "not_attempted"}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    parent = commands.add_parser("prepare-parent", help="認証済み親で固定PRデータを取得する")
    parent.add_argument("--pr", required=True)
    parent.add_argument("--output-dir", required=True)
    parent.add_argument("--source-root")
    parent.add_argument("--review-commit")
    parent.add_argument("--records-comments")
    parent.set_defaults(handler=prepare_parent)
    finalize = commands.add_parser("finalize-parent", help="停止済みDSHレビューを親で保存する")
    finalize.add_argument("--result", required=True, type=Path)
    start = commands.add_parser("prepare")
    start.add_argument("--pr", required=True)
    start.add_argument(
        "--runtime", required=True, choices=("claude-code", "devin", "pi", "dsh", "codex", "cursor", "grok-build")
    )
    start.add_argument("--model-name")
    start.add_argument("--request")
    start.add_argument("--review-commit")
    start.add_argument("--source-root")
    start.add_argument("--records-comments")
    start.add_argument("--temp-root")
    start.set_defaults(handler=prepare)
    finish = commands.add_parser("complete")
    finish.add_argument("--state", required=True)
    finish.set_defaults(handler=complete)
    failure = commands.add_parser("fail")
    failure.add_argument("--state")
    failure.add_argument("--request")
    failure.add_argument("--reason", required=True)
    runner = commands.add_parser("attach-runner", help="ランナーの result.json の観測値を実行記録に結び付ける")
    runner.add_argument("--result", required=True, type=Path)
    return root


def fail_review(parent: Path, state: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """モデルが prepare の後に止めた（failed_at: review）。終わった記録は書き換えない。"""
    path, located = locate_record(parent, state)
    context = cast(dict[str, Any], state.get("run_context") or {})
    now = time.monotonic()
    prepared, started = context.get("prepared_monotonic"), context.get("started_monotonic")
    seconds = {
        "review_seconds": now - prepared if isinstance(prepared, (int, float)) else None,
        "total_seconds": now - started if isinstance(started, (int, float)) else None,
    }
    error = record_update(parent, path, record.mark_failed("review", reason, finished_at=utc_now(), seconds=seconds))
    return record_fields(path, error or located)


def attach_runner(result_path: Path) -> dict[str, Any]:
    """書き先は親の main worktree の reviews/ の実行記録だけ。worker が返したパスは使わない。"""
    try:
        parent = workspace._target_call("default_parent_root")
        return record.attach_runner(result_path, parent)
    except FAILURES as error:
        return {"schema_version": 1, "status": "failed", "result_file": str(result_path), "reason": str(error)}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "finalize-parent":
        batch = BatchResult.model_validate_json(args.result.read_text(encoding="utf-8"))
        finalize_parent_batch(batch)
        workspace.write_json(args.result, batch.model_dump(mode="json"))
        # A refused save is a failed review payload, not a failed after_run hook.
        print(json.dumps({"status": "success", "result_file": str(args.result)}))
        return 0
    if args.command == "prepare-parent":
        try:
            print(json.dumps(prepare_parent(args), ensure_ascii=False))
            return 0
        except FAILURES as error:
            print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
            return 1
    if args.command == "attach-runner":
        result = attach_runner(args.result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["status"] == "failed" else 0
    parent: Path | None = None
    state: dict[str, Any] | None = None
    try:
        if getattr(args, "state", None):
            parent, state = workspace.load_state(args.state)
        if args.command == "fail":
            result = {**result_fields(state), "status": "failed", "reason": args.reason}
            if args.request:
                result.update(request_binding(args.request))
            if parent is not None and state is not None:
                result.update(fail_review(parent, state, args.reason))
        else:
            result = args.handler(args)
    except FAILURES as error:
        result = {**result_fields(state), "status": "failed", "reason": str(error)}
        if getattr(args, "request", None):
            try:
                result.update(request_binding(args.request))
            except OSError, ValueError, RuntimeError:
                pass
        if getattr(args, "state", None):
            result["state_file"] = args.state
        if parent is not None and state is not None:
            result.update(record_fields(*locate_record(parent, state)))
    if result["status"] == "failed" and state is not None:
        result["retained_paths"] = retained_paths(state)
    result.setdefault("run_record", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
