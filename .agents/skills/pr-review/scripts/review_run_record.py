#!/usr/bin/env python3
"""pr-review の実行記録（review-findings v2 の kind: review_run）を書く。

形式と規則の定義は ``.agents/review-findings.md`` 4.1 節にある。書くのは review_run.py だけで、モデルは書かない。
記録の読み書きは保存と同じロックの中で行い、ランナーの観測値の書き込み（``attach_runner``）と取りこぼしなく並べる。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import secrets
import stat
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

# このモジュールは、別のリポジトリのレビュー worktree から呼ばれることもある。
ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.agent_runner.models import Attempt, BatchResult  # noqa: E402

Change = Callable[[dict[str, Any]], dict[str, Any] | None]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = _load("review_run_record_workspace", Path(__file__).with_name("pr_review_workspace.py"))
findings = workspace.load_review_findings()
BINDING_KEYS = ("run_id", "request_id", "agent_id", "role")
# complete の拒否（failed_at: complete）の後に complete を呼び直したときだけ、終わった失敗を書き換えてよい
FINISHABLE_AFTER_REFUSAL = ("complete", "save", "cleanup")


def new_session_name(pr_number: int) -> tuple[str, str]:
    """helper のセッションディレクトリの名前と、その 8 文字の接尾辞（記録の名前に使う）。"""
    suffix = secrets.token_hex(4)
    return f"pr-review-{pr_number}-{suffix}", suffix


def record_relative(target: Mapping[str, Any], started_at: str, model: str, suffix: str) -> str:
    directory = workspace.review_directory_name(int(target["number"]), str(target["head_ref"]))
    stamp = started_at.replace("-", "").replace(":", "")
    return f"{workspace.reviews_prefix(str(target['repository']))}/{directory}/runs/{stamp}-{model}-{suffix}.json"


def safe_record_path(parent: Path, relative: str, repository: str) -> Path:
    """記録の保存先。reviews/<owner>/<repo>/<directory>/runs/<name>.json だけを許し、途中の symlink を拒否する。"""
    value = PurePosixPath(relative)
    if (
        value.is_absolute()
        or len(value.parts) != 6
        or value.parts[4] != "runs"
        or value.suffix != ".json"
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise workspace.WorkspaceError("run record must be reviews/<owner>/<repo>/<directory>/runs/<name>.json")
    workspace.require_repository_layout(parent, value, repository, length=6)
    path = parent / Path(*value.parts)
    if not workspace.is_within(path.resolve(strict=False), parent / "reviews"):
        raise workspace.WorkspaceError("run record escapes the main repository reviews/ directory")
    return path


def started_document(
    *,
    target: Mapping[str, Any],
    review_kind: str,
    reviewed_head: str,
    reviewer: Mapping[str, Any],
    started_at: str,
) -> dict[str, Any]:
    return {
        "schema": findings.PROVENANCE_SCHEMA_ID,
        "kind": "review_run",
        "pr": {"repository": target["repository"], "number": target["number"]},
        "review_kind": review_kind,
        "mode": None,
        "pr_head": target["head_sha"],
        "reviewed_head": reviewed_head,
        "base": None,
        "reviewer": dict(reviewer),
        "inputs": None,
        "status": "started",
        "failed_at": None,
        "reason": None,
        "outputs": None,
        "timing": {"started_at": started_at, "finished_at": None, **dict.fromkeys(findings.RUN_STAGE_SECONDS)},
        "runner": None,
    }


def read(path: Path) -> dict[str, Any]:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise workspace.WorkspaceError(f"run record is not a regular file: {path}")
    document: dict[str, Any] = findings.validate_document(json.loads(path.read_text(encoding="utf-8")))
    if document["kind"] != "review_run":
        raise workspace.WorkspaceError(f"not a run record: {path}")
    return document


def create(parent: Path, path: Path, document: Mapping[str, Any]) -> None:
    findings.validate_document(document)
    with workspace.review_save_lock(parent):
        if path.exists() or path.is_symlink():
            raise workspace.WorkspaceError(f"run record already exists: {path}")
        workspace.write_json(path, dict(document))


def update(parent: Path, path: Path, change: Change) -> bool:
    """読む → 規則を適用 → 検査 → 置き換え。規則が書き換えを許さなければ何もしない。"""
    with workspace.review_save_lock(parent):
        current = read(path)
        changed = change(copy.deepcopy(current))
        if changed is None or changed == current:
            return False
        findings.validate_document(changed)
        workspace.write_json(path, changed)
    return True


def mark_prepared(*, mode: str, base: str, inputs: Mapping[str, Any], prepare_seconds: float) -> Change:
    def change(document: dict[str, Any]) -> dict[str, Any] | None:
        if document["status"] != "started":
            return None
        document.update(mode=mode, base=base, inputs=dict(inputs))
        document["timing"]["prepare_seconds"] = prepare_seconds
        return document

    return change


def _may_finish(document: Mapping[str, Any], stage: str) -> bool:
    return document["status"] == "started" or (
        document["status"] == "failed" and document["failed_at"] == "complete" and stage in FINISHABLE_AFTER_REFUSAL
    )


def mark_failed(
    stage: str,
    reason: str,
    *,
    finished_at: str,
    seconds: Mapping[str, float | None],
    outputs: Mapping[str, Any] | None = None,
) -> Change:
    def change(document: dict[str, Any]) -> dict[str, Any] | None:
        if not _may_finish(document, stage):
            return None
        document.update(status="failed", failed_at=stage, reason=reason, outputs=dict(outputs) if outputs else None)
        document["timing"].update(seconds, finished_at=finished_at)
        return document

    return change


def mark_success(outputs: Mapping[str, Any], *, finished_at: str, seconds: Mapping[str, float | None]) -> Change:
    def change(document: dict[str, Any]) -> dict[str, Any] | None:
        if not _may_finish(document, "complete"):
            return None
        document.update(status="success", failed_at=None, reason=None, outputs=dict(outputs))
        document["timing"].update(seconds, finished_at=finished_at)
        return document

    return change


def records_input(decision_state: Mapping[str, Any]) -> dict[str, Any]:
    """prepare が読んだ判断の記録の指紋。本文は写さない。"""
    text = json.dumps(decision_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    counts = Counter(str(item["state"]) for item in decision_state.get("decisions", []))
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "decisions": dict(sorted(counts.items()))}


def diff_input(worktree: Path, base: str, head: str) -> dict[str, int]:
    """``git diff --numstat`` の件数。バイナリは追加・削除の行数を持たないので別に数える。"""
    output = str(workspace.git(worktree, "diff", "--numstat", "--find-renames", f"{base}..{head}"))
    files = added = deleted = binary = 0
    for line in output.splitlines():
        if not line:
            continue
        add, remove, _path = line.split("\t", 2)
        files += 1
        if add == "-" and remove == "-":
            binary += 1
        else:
            added += int(add)
            deleted += int(remove)
    return {"files": files, "added": added, "deleted": deleted, "binary": binary}


def outputs_document(*, review_path: Path, findings_path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """保存した 2 つの成果への参照。パスはレビューディレクトリからの相対。"""
    by_severity = dict.fromkeys(findings.RUN_SEVERITY_KEYS, 0)
    for finding in document["findings"]:
        by_severity[str(finding["severity"]).lower()] += 1
    return {
        "review_file": review_path.name,
        "review_sha256": workspace.file_digest(review_path),
        "findings_file": f"findings/{findings_path.name}",
        "findings_sha256": workspace.file_digest(findings_path),
        "active_findings": len(document["findings"]),
        "by_severity": by_severity,
        "unverified": len(document["unverified_candidates"]),
    }


def runner_block(attempt: Attempt) -> dict[str, Any]:
    """ランナーが観測した値。Identity の null は 3.1 節の規則どおり unknown にする。"""
    return {
        "identity": {key: getattr(attempt.identity, key) or "unknown" for key in findings.RUNNER_IDENTITY_KEYS},
        "attempt": {
            "state": attempt.state,
            "exit_code": attempt.exit_code,
            "elapsed_seconds": attempt.elapsed_seconds,
            "stop_confirmed": attempt.stop_confirmed,
        },
    }


def _indexed_records(parent: Path) -> tuple[dict[tuple[str, ...], list[tuple[Path, dict[str, Any]]]], int]:
    """親の reviews/ にある実行記録を、request binding の 4 つの値で引けるようにする。

    形式を満たさないファイル、途中の階層が symlink のファイル、置き場所と PR が合わないファイルは書き先にしない。
    """
    index: dict[tuple[str, ...], list[tuple[Path, dict[str, Any]]]] = {}
    skipped = 0
    for path in sorted((parent / "reviews").glob("*/*/#*/runs/*.json")):
        try:
            parts = path.relative_to(parent).parts
            repository = f"{parts[1]}/{parts[2]}"
            safe = safe_record_path(parent, "/".join(parts), repository)
            document = read(safe)
            pr = document["pr"]
            if pr["repository"] != repository or not parts[3].startswith(f"#{pr['number']}-"):
                raise workspace.WorkspaceError("run record does not match its directory")
        except OSError, ValueError, RuntimeError:
            skipped += 1
            continue
        key = tuple(str(document["reviewer"]["provenance"][name]) for name in BINDING_KEYS)
        index.setdefault(key, []).append((safe, document))
    return index, skipped


def attach_runner(result_path: Path, parent: Path) -> dict[str, Any]:
    """ランナーの result.json の各試行を実行記録と突き合わせ、runner 欄だけを書く（4.1 節）。

    同じ値なら書かない。違う値が既にあれば、どのファイルにも書かずに失敗を返す。
    """
    batch = BatchResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    attached: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    unmatched: list[dict[str, str]] = []
    planned: list[tuple[Path, dict[str, Any]]] = []
    with workspace.review_save_lock(parent):
        index, skipped = _indexed_records(parent)
        for result in batch.results:
            for attempt in result.attempts:
                key = (batch.run_id, result.request_id, attempt.agent_id, result.role)
                matches = index.get(key, [])
                if not matches:
                    unmatched.append({**dict(zip(BINDING_KEYS, key, strict=True)), "state": attempt.state})
                block = runner_block(attempt)
                for path, document in matches:
                    if document["runner"] == block:
                        unchanged.append(str(path))
                    elif document["runner"] is not None:
                        conflicts.append(str(path))
                    else:
                        changed = {**document, "runner": block}
                        findings.validate_document(changed)
                        planned.append((path, changed))
        if not conflicts:
            for path, document in planned:
                workspace.write_json(path, document)
                attached.append(str(path))
    return {
        "schema_version": 1,
        "status": "failed" if conflicts else "success",
        "result_file": str(result_path),
        "run_id": batch.run_id,
        "attached": attached,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "unmatched_count": len(unmatched),
        "skipped": skipped,
        "conflicts": conflicts,
        "reason": "runner values differ from existing run records; nothing was written" if conflicts else None,
    }
