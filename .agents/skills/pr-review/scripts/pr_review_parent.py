"""Immutable, credential-free PR inputs prepared by the authenticated controller."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from pathlib import Path
from types import ModuleType
from typing import Any, cast

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def regular_bytes(path: Path) -> bytes:
    if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("parent review input must be a canonical regular file")
    return path.read_bytes()


def digest(path: Path) -> str:
    return hashlib.sha256(regular_bytes(path)).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate parent review input key")
        result[key] = value
    return result


def parameters_from_prompt(prompt: str) -> dict[str, Any]:
    try:
        parameters = json.loads(prompt.rsplit("```json\n", 1)[1].split("\n```", 1)[0], object_pairs_hook=unique_object)
        if not isinstance(parameters, dict):
            raise ValueError("Request parameters must be a JSON object")
        return cast(dict[str, Any], parameters)
    except IndexError as error:
        raise ValueError("DSH review requires parent parameters in Request.prompt") from error


def descriptor_from_prompt(prompt: str) -> dict[str, str]:
    try:
        descriptor = parameters_from_prompt(prompt)["parent_pr_data"]
        if set(descriptor) != {"path", "sha256"} or not all(isinstance(v, str) for v in descriptor.values()):
            raise ValueError("invalid parent review descriptor")
        if DIGEST.fullmatch(descriptor["sha256"]) is None:
            raise ValueError("invalid parent review digest")
        return descriptor
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("DSH review requires trusted parent_pr_data in Request.prompt") from error


def load(workspace: ModuleType, descriptor: dict[str, str], pr: str) -> dict[str, Any]:
    path = Path(descriptor["path"])
    raw = regular_bytes(path)
    if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
        raise ValueError("parent PR snapshot changed")
    loaded = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(loaded, dict):
        raise ValueError("invalid parent PR snapshot")
    document = cast(dict[str, Any], loaded)
    if (
        set(document) != {"schema", "target", "base", "review_commit", "comments", "bundle_sha256", "requirements"}
        or document["schema"] != "pr-review-parent/v1"
    ):
        raise ValueError("invalid parent PR snapshot")
    target = workspace.parse_target(json.dumps(document["target"]))
    with workspace.target_errors() as helper:
        clone = helper.CloneTarget.from_json(target)
        parsed = helper.parse_pull_request_argument(pr)
        if parsed.number != clone.number or (parsed.repository is not None and parsed.repository != clone.repository):
            raise ValueError("parent snapshot belongs to another PR")
        helper.require_parent_root(Path(target["parent_root"]))
    if SHA.fullmatch(str(document["base"])) is None or not target.get("base_ref"):
        raise ValueError("invalid parent PR base")
    commit = document["review_commit"]
    if commit is not None and (not isinstance(commit, str) or SHA.fullmatch(commit) is None):
        raise ValueError("invalid parent review commit")
    bundle = path.with_name("objects.bundle")
    if digest(bundle) != document["bundle_sha256"]:
        raise ValueError("parent PR bundle changed")
    return {**document, "target": target, "bundle": str(bundle), "descriptor": descriptor}


def staging_root(workspace: ModuleType, target: dict[str, Any], request: Any) -> Path:
    directory = workspace._target_call(
        "review_directory", Path(target["parent_root"]), target["repository"], target["number"], target["head_ref"]
    )
    path = directory / ".staging" / request.run_id / request.request_id / request.agent_id
    if path.resolve(strict=False) != path:
        raise ValueError("staging directory must not contain symlinks")
    return path


def prepare(workspace: ModuleType, args: Any, comments: Any, requirements: Any) -> dict[str, str]:
    parent_value = getattr(args, "parent_root", None)
    parent = Path(parent_value) if parent_value else workspace._target_call("default_parent_root")
    api_runner = getattr(args, "api_runner", None)
    api_args = {"runner": api_runner} if api_runner is not None else {}
    target = workspace._target_call("resolve", args.pr, parent_root=parent, **api_args)
    workspace.artifact_model("parent-input", args.review_commit)
    directory = Path(args.output_dir).absolute()
    if directory.resolve(strict=False) != directory:
        raise ValueError("parent input directory must be canonical")
    # An existing directory is never ours to replace; refuse it before creating anything.
    directory.mkdir(mode=0o700)
    try:
        return _populate(workspace, args, target, directory, comments, requirements, api_args)
    except BaseException:
        # Only this call created the directory; leave nothing that blocks a retry of the same cycle.
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _populate(
    workspace: ModuleType,
    args: Any,
    target: dict[str, Any],
    directory: Path,
    comments: Any,
    requirements: Any,
    api_args: dict[str, Any],
) -> dict[str, str]:
    source = directory / "repo"
    checkout_value = args.source_root or target["target_root"]
    with workspace.target_errors() as helper:
        clone = helper.CloneTarget.from_json(target)
        if checkout_value:
            workspace.require_target_checkout(Path(checkout_value), clone.repository)
        # A local clone can inherit shallow boundaries and create an unusable bundle.
        # Fetch complete remote history only into this owned clone, never unshallow the parent.
        helper.clone_repository(clone, source, checkout_branch=False)
        head_ref = helper.fetch_pull_head(source, clone.number, clone.head_sha)
    base_ref = "refs/pr-review-parent/base"
    workspace.git(
        source,
        "fetch",
        "--quiet",
        "--no-write-fetch-head",
        "--no-tags",
        "--refmap=",
        "origin",
        f"+refs/heads/{target['base_ref']}:{base_ref}",
    )
    commit = args.review_commit or target["head_sha"]
    if args.review_commit and checkout_value:
        workspace.git(source, "fetch", "--quiet", "--no-write-fetch-head", str(checkout_value), commit)
    workspace.require_review_commit(source, commit)
    commit_ref = "refs/pr-review-parent/commit"
    workspace.git(source, "update-ref", commit_ref, commit)
    base = workspace.git(source, "merge-base", base_ref, commit)
    current = workspace._target_call("fetch_pull_request", clone.repository, clone.number, **api_args)
    if current.head_sha != clone.head_sha:
        raise ValueError("PR head changed during parent preparation")
    bundle = directory / "objects.bundle"
    workspace.git(source, "bundle", "create", str(bundle), head_ref, base_ref, commit_ref)
    validation = directory / "bundle-check"
    workspace.init_temporary_repository(source, validation)
    workspace.git(
        validation,
        "fetch",
        "--quiet",
        "--no-write-fetch-head",
        str(bundle),
        f"+{head_ref}:{head_ref}",
        f"+{base_ref}:{base_ref}",
        f"+{commit_ref}:{commit_ref}",
    )
    shutil.rmtree(validation)
    document = {
        "schema": "pr-review-parent/v1",
        "target": target,
        "base": base,
        "review_commit": args.review_commit,
        "comments": comments,
        "bundle_sha256": digest(bundle),
        "requirements": requirements,
    }
    snapshot = directory / "snapshot.json"
    snapshot.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(source)
    bundle.chmod(0o444)
    snapshot.chmod(0o444)
    return {"path": str(snapshot), "sha256": digest(snapshot)}
