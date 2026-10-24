"""issue-to-pr の Plan の Task を、agent-runner の workflow issue の implement に割り当てた担当に依頼する。

check は設定（雛形 < 個人設定 < --implement）だけを検査し、worktree・runner・GitHub に触れない。
run は Task 1 つを runner で依頼し、停止の確認の後に取り込めるかを git で確かめて判定する（取り込みはしない）。
accept は判定を今の状態でやり直してから --ff-only で取り込み、cleanup は runner の worktree と出力先を片付ける。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.agent_runner import (  # noqa: E402
    Config,
    ConfigError,
    RunnerError,
    Workflow,
    load_config,
    resolve_config_path,
    run_workflow,
)
from tools.agent_runner.adapters import Adapter, load_adapter  # noqa: E402
from tools.agent_runner.config import validate_config  # noqa: E402
from tools.agent_runner.environ import literal_environ  # noqa: E402
from tools.agent_runner.models import Assignment, BatchResult  # noqa: E402
from tools.agent_runner.workspace import (  # noqa: E402
    CheckoutAuditError,
    blocking_checkout_audit,
    require_auditable_checkouts,
)


def _load_agent_tmp() -> Any:
    spec = importlib.util.spec_from_file_location("implement_agent_tmp", ROOT / ".agents/scripts/agent_tmp.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_tmp = _load_agent_tmp()

DEFAULT_TEMPLATE = ROOT / ".devcontainer/agent-runner.toml"
WORKFLOW = "issue"
ROLE = "implement"
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
RETRY_STATUSES = frozenset({"BLOCKED", "NEEDS_CONTEXT"})

Status = Literal["DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT"]
Verdict = Literal["importable", "no_changes", "retry", "rejected", "failed"]


class TaskInputError(ValueError):
    """implement_task 自身の入力の誤り。文は値・パスを含まない固定の文で、そのまま出す。"""


# --- 設定 ---------------------------------------------------------------------------------------------------------


def project(config: Config) -> Config:
    """workflow issue の implement だけを残す。投影の外への依存（同モデルの除外）を黙って捨てない。"""
    config = validate_config(config, WORKFLOW)
    workflow = config.workflows[WORKFLOW]
    role = config.roles.get(ROLE)
    assignment = workflow.assignments.get(ROLE)
    if role is None or assignment is None:
        raise ConfigError("invalid_reference")
    if role.writes is not True or role.result_shape != "json" or len(assignment.agents) != 1:
        raise ConfigError("invalid_config")
    constraints = {name: other for name, other in workflow.exclude_same_model_as.items() if name == ROLE}
    if any(other != ROLE for other in constraints.values()):
        raise ConfigError("invalid_reference")
    selected = config.model_copy(deep=True)
    selected.workflows = {WORKFLOW: Workflow(assignments={ROLE: assignment}, exclude_same_model_as=constraints)}
    return validate_config(selected, WORKFLOW)


def personal_config(config: Path | None) -> Path | None:
    if config is not None:
        return config
    candidate = resolve_config_path()
    return candidate if candidate.exists() else None


def load(template: Path, config: Path | None, implement: str | None) -> Config:
    """雛形 < 個人設定 < --implement で読み込み、投影する。--implement は agents を置き換え、fallback を保つ。"""
    assignments = {ROLE: [implement]} if implement is not None else None
    return project(load_config(template, personal_config(config), assignments=assignments, workflow=WORKFLOW))


def assignment_of(config: Config) -> Assignment:
    return config.workflows[WORKFLOW].assignments[ROLE]


def check(template: Path, config: Path | None, implement: str | None) -> dict[str, Any]:
    assignment = assignment_of(load(template, config, implement))
    personal = personal_config(config)
    return {
        "status": "ok",
        "assignment": assignment.model_dump(mode="json"),
        "self": assignment.agents == ["self"],
        "template": str(template),
        "config": str(personal) if personal is not None else None,
    }


# --- 依頼と結果の形 -----------------------------------------------------------------------------------------------


def _single_line(value: str) -> bool:
    """runner の write_exceptions と同じ条件: 空でない・前後に空白が無い・1 行・制御文字が無い。"""
    return (
        value != ""
        and value == value.strip()
        and value.splitlines() == [value]
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def _nonblank(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("must be nonblank text without NUL")
    return value


def _sha(value: str) -> str:
    if SHA.fullmatch(value) is None:
        raise ValueError("must be a full lowercase commit hash")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskRequest(_Strict):
    """司令塔が作る依頼の JSON。prompt は Plan のその Task の全文などを入れた、それだけで作業できる依頼。"""

    repository: str
    revision: str
    task: str
    allowed_files: list[str] = Field(min_length=1)
    commit_message: str
    prompt: str

    @field_validator("repository")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("repository must be absolute")
        return value

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return _sha(value)

    @field_validator("task")
    @classmethod
    def _task(cls, value: str) -> str:
        if not _single_line(value):
            raise ValueError("task must be one trimmed line")
        return value

    @field_validator("allowed_files")
    @classmethod
    def _allowed_files(cls, value: list[str]) -> list[str]:
        for entry in value:
            parts = entry.removesuffix("/").split("/")
            if not _single_line(entry) or entry.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("allowed_files entries must be normalized relative paths")
        return value

    @field_validator("commit_message", "prompt")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)


class Command(_Strict):
    argv: list[str] = Field(min_length=1)
    exit_code: int


class TaskResult(_Strict):
    """担当が返す JSON（runner の receipt の payload）。"""

    status: Status
    commit: str | None
    changed_files: list[str]
    commands: list[Command]
    concerns: list[str]
    incomplete: list[str]
    retained_paths: list[str]

    @field_validator("commit")
    @classmethod
    def _commit(cls, value: str | None) -> str | None:
        return value if value is None else _sha(value)


def is_task_result(payload: object) -> bool:
    """runner の payload_check。TaskResult に合わない結果は invalid_result になり、fallback の次の候補を試す。"""
    try:
        TaskResult.model_validate(payload)
    except ValidationError:
        return False
    return True


class Assessment(_Strict):
    verdict: Verdict
    reasons: list[str]
    work_dir: str | None = None
    commit: str | None = None
    helper_commit: bool = False
    changed_files: list[str] = Field(default_factory=list[str])
    result: TaskResult | None = None


def allowed(path: str, entries: Sequence[str]) -> bool:
    """完全一致、または / で終わる要素（ディレクトリ）の下。"""
    return any(path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in entries)


def exit_code_for(verdict: str) -> int:
    return 0 if verdict in {"importable", "no_changes"} else 1


RESULT_SHAPE = json.dumps(
    {
        "status": "DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT",
        "commit": "<full SHA of HEAD after your last commit> | null",
        "changed_files": ["<path>"],
        "commands": [{"argv": ["<program>", "<argument>"], "exit_code": 0}],
        "concerns": ["<text>"],
        "incomplete": ["<text>"],
        "retained_paths": ["<absolute path you left behind>"],
    },
    ensure_ascii=False,
)


def compose_prompt(request: TaskRequest) -> str:
    """担当への依頼文。runner はこの前に作業範囲の指示（Work in this worktree only・writes=True）を付ける。"""
    return "\n".join(
        [
            "Implement exactly ONE Task of an implementation plan in this runner worktree. Do not rely on any skill "
            "or memory being available: everything you need is in this request.",
            "",
            "Files you may change (paths relative to the worktree root; an entry ending with '/' is a directory and "
            "everything below it). Do not create, modify, delete or rename anything else:",
            json.dumps(request.allowed_files, ensure_ascii=False),
            "",
            "Commits: you may commit in this worktree (detached HEAD). If you commit, make only linear commits on top "
            "of the current HEAD and never create a merge commit; every commit is checked against the list above, so "
            "do not touch other files even in a commit you later revert. If git refuses to stage or commit (for "
            'example because a sandbox makes .git read-only), leave your changes uncommitted and return "commit": '
            "null; the caller commits them after checking. If you commit, leave nothing uncommitted.",
            "",
            "Never: push, create or edit pull requests or issues, write to GitHub, start another agent (runner or "
            "subagent), or use any other worktree or checkout.",
            "",
            "Run the related tests and static checks the Task names, and report each command's argv and exit code.",
            "",
            "Return exactly one JSON object with exactly these keys:",
            RESULT_SHAPE,
            "status: DONE (finished), DONE_WITH_CONCERNS (finished, but list your doubts in concerns), BLOCKED (you "
            "cannot finish; say why in concerns), NEEDS_CONTEXT (information is missing; say what in incomplete). "
            "changed_files: every path you added, changed or deleted, with both names for a rename, relative to the "
            "worktree root.",
            "",
            f"Task: {request.task}",
            "",
            request.prompt,
        ]
    )


# --- git ----------------------------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """GIT_* の経路指定を除いた環境で git を呼ぶ（tools.agent_runner.workspace と同じ）。"""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=check,
        timeout=60,
        env={key: value for key, value in literal_environ().items() if not key.startswith("GIT_")},
    )


def _text(output: bytes) -> str:
    return os.fsdecode(output.rstrip(b"\n"))


def head(cwd: Path) -> str:
    return _text(_git(cwd, "rev-parse", "--verify", "HEAD").stdout)


def status_paths(cwd: Path) -> list[str]:
    """未 commit の変更（untracked を含み、ignored を除く）。リネームは元と先の 2 件に分ける。"""
    output = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").stdout
    return sorted(os.fsdecode(entry[3:]) for entry in output.split(b"\x00") if entry)


def diff_paths(cwd: Path, base: str, commit: str) -> list[str]:
    output = _git(cwd, "diff", "--name-only", "-z", "--no-renames", base, commit, "--").stdout
    return sorted(os.fsdecode(entry) for entry in output.split(b"\x00") if entry)


def range_paths(cwd: Path, base: str, commit: str) -> list[str]:
    """base..commit の各 commit が追加・変更・削除したパスの和集合。後の commit で戻した変更も残る。"""
    output = _git(
        cwd, "log", "--format=", "--no-show-signature", "--name-only", "--no-renames", "-z", f"{base}..{commit}", "--"
    ).stdout
    return sorted({os.fsdecode(entry) for entry in output.split(b"\x00") if entry})


def merge_commits(cwd: Path, base: str, commit: str) -> list[str]:
    return _text(_git(cwd, "rev-list", "--merges", f"{base}..{commit}", "--").stdout).split()


def is_ancestor(cwd: Path, ancestor: str, commit: str) -> bool:
    result = _git(cwd, "merge-base", "--is-ancestor", ancestor, commit, check=False)
    if result.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(result.returncode, "git merge-base")
    return result.returncode == 0


def check_feature(repository: Path, revision: str) -> None:
    """取り込み先の feature worktree: linked worktree の root で、HEAD が revision、未 commit の変更が無い。"""
    try:
        top = Path(_text(_git(repository, "rev-parse", "--show-toplevel").stdout)).resolve()
        git_dir = _text(_git(repository, "rev-parse", "--path-format=absolute", "--git-dir").stdout)
        common = _text(_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout)
        current = head(repository)
        dirty = status_paths(repository)
    except subprocess.SubprocessError, OSError:
        raise TaskInputError("repository is not a readable git checkout") from None
    if top != repository.resolve() or Path(git_dir).resolve() == Path(common).resolve():
        raise TaskInputError("repository must be the root of a linked feature worktree")
    if current != revision:
        raise TaskInputError("revision must be the current HEAD of the feature worktree")
    if dirty:
        raise TaskInputError("the feature worktree has uncommitted changes")


# --- 判定 ---------------------------------------------------------------------------------------------------------


def _inspect(request: TaskRequest, result: TaskResult, work: Path) -> Assessment:
    """取り込みの前の確認。runner の worktree を git で読み、報告を信用しない。

    担当の commit は --ff-only で範囲ごと履歴に入るので、最終 tree の差分ではなく範囲のどの commit も照合する。
    merge commit の差分は親の取り方で担当の変更と base 側の変更が混ざるので、直線の範囲だけを受け付ける。
    """
    reasons: list[str] = []
    current = head(work)
    leftovers = status_paths(work)
    changed: list[str] = []
    touched: list[str] = []
    if result.commit is not None:
        if current != result.commit:
            reasons.append(f"commit {result.commit} is not the runner worktree HEAD {current}")
        elif result.commit == request.revision:
            reasons.append("commit equals revision: nothing was committed")
        elif not is_ancestor(work, request.revision, result.commit):
            reasons.append(f"commit does not descend from revision {request.revision}")
        else:
            changed = diff_paths(work, request.revision, result.commit)
            merges = merge_commits(work, request.revision, result.commit)
            if merges:
                reasons.append(f"merge commits between revision and commit (only linear commits): {', '.join(merges)}")
            else:
                touched = range_paths(work, request.revision, result.commit)
        if leftovers:
            reasons.append(f"uncommitted changes remain after the commit: {', '.join(leftovers)}")
    elif current != request.revision:
        reasons.append(f"HEAD moved to {current} but commit is null")
    elif not leftovers:
        return Assessment(verdict="no_changes", reasons=[], work_dir=str(work), result=result)
    else:
        changed = touched = leftovers
        unreported = sorted(set(changed) - set(result.changed_files))
        missing = sorted(set(result.changed_files) - set(changed))
        if unreported or missing:
            reasons.append(
                f"changed_files does not match the uncommitted changes (unreported: {unreported}, missing: {missing})"
            )
    outside = [path for path in touched if not allowed(path, request.allowed_files)]
    if outside:
        reasons.append(f"changes outside allowed_files: {', '.join(outside)}")
    return Assessment(
        verdict="rejected" if reasons else "importable",
        reasons=reasons,
        work_dir=str(work),
        commit=result.commit,
        helper_commit=result.commit is None,
        changed_files=changed,
        result=result,
    )


def assess(request: TaskRequest, batch: BatchResult) -> Assessment:
    """runner の結果と、成功した試行の worktree の今の状態から、取り込めるかを決める。"""
    attempts = [attempt for outcome in batch.results for attempt in outcome.attempts]
    unstopped = [attempt.agent_id for attempt in attempts if not attempt.stop_confirmed]
    if unstopped:
        return Assessment(
            verdict="failed",
            reasons=[f"stop_unconfirmed: attempts of {', '.join(unstopped)}; their areas are retained"],
        )
    if len(batch.results) != 1:
        return Assessment(verdict="failed", reasons=["runner returned an unexpected number of results"])
    outcome = batch.results[0]
    if outcome.state != "success" or not outcome.attempts:
        exhausted = "; fallback exhausted" if outcome.fallback_exhausted else ""
        return Assessment(verdict="failed", reasons=[f"runner: {outcome.state}{exhausted}"])
    attempt = outcome.attempts[-1]
    work_dir = str(attempt.work_dir) if attempt.work_dir is not None else None
    # run では runner が is_task_result で検査済み。accept が記録を読み直してやり直すときの防御として残す
    try:
        result = TaskResult.model_validate(attempt.payload)
    except ValidationError as error:
        return Assessment(verdict="rejected", reasons=[f"invalid_result: {_fields(error)}"], work_dir=work_dir)
    if not batch.checkout_isolated:
        return Assessment(
            verdict="rejected", reasons=["checkout_isolation: incomplete"], work_dir=work_dir, result=result
        )
    if blocking_checkout_audit(batch.outside_changes, batch.checkout_audit, isolated=batch.checkout_isolated):
        return Assessment(
            verdict="rejected",
            reasons=[f"outside_changes: {', '.join(batch.outside_changes)}"],
            work_dir=work_dir,
            result=result,
        )
    if result.status in RETRY_STATUSES:
        return Assessment(verdict="retry", reasons=[f"status: {result.status}"], work_dir=work_dir, result=result)
    if attempt.work_dir is None:
        return Assessment(verdict="rejected", reasons=["the successful attempt has no worktree"], result=result)
    try:
        return _inspect(request, result, attempt.work_dir)
    except subprocess.SubprocessError, OSError:
        return Assessment(
            verdict="rejected",
            reasons=["the runner worktree cannot be read with git"],
            work_dir=work_dir,
            result=result,
        )


def summarize(assignment: Assignment, batch: BatchResult, assessment: Assessment) -> dict[str, Any]:
    outcome = batch.results[0] if len(batch.results) == 1 else None
    attempts = outcome.attempts if outcome is not None else []
    return {
        "verdict": assessment.verdict,
        "reasons": assessment.reasons,
        "assignment": assignment.model_dump(mode="json"),
        "requested_agent": outcome.requested_agent if outcome is not None else None,
        "actual_agent": outcome.actual_agent if outcome is not None else None,
        "identity": attempts[-1].identity.model_dump(mode="json") if attempts else None,
        "attempts": [
            {
                "agent_id": attempt.agent_id,
                "state": attempt.state,
                "elapsed_seconds": attempt.elapsed_seconds,
                "stop_confirmed": attempt.stop_confirmed,
                "exit_code": attempt.exit_code,
                "work_dir": str(attempt.work_dir) if attempt.work_dir is not None else None,
            }
            for attempt in attempts
        ],
        "work_dir": assessment.work_dir,
        "commit": assessment.commit,
        "helper_commit": assessment.helper_commit,
        "changed_files": assessment.changed_files,
        "result": assessment.result.model_dump(mode="json") if assessment.result is not None else None,
        "outside_changes": batch.outside_changes,
        "elapsed_seconds": batch.elapsed_seconds,
        "artifact_dir": str(batch.artifact_dir),
    }


def run(
    config: Config,
    request: TaskRequest,
    output_dir: Path,
    *,
    adapter_loader: Callable[[str], Adapter] = load_adapter,
) -> dict[str, Any]:
    """Task 1 つを runner で依頼し（concurrency 1）、判定と記録を output_dir/task.json に残す。"""
    selected = project(config)
    assignment = assignment_of(selected)
    if assignment.agents == ["self"]:
        raise TaskInputError("issue.implement is self: the controller implements the Task itself (no runner launch)")
    repository = Path(request.repository)
    check_feature(repository, request.revision)
    if output_dir.exists() or output_dir.is_symlink():
        raise TaskInputError("output directory must be new")
    require_auditable_checkouts(repository)
    batch = run_workflow(
        selected,
        WORKFLOW,
        compose_prompt(request),
        repository,
        revision=request.revision,
        concurrency=1,
        output_dir=output_dir,
        adapter_loader=adapter_loader,
        # AGENTS.md の置き場所の規則どおり、担当が agent_tmp.py mkdir でスクラッチを作れるようにする
        writable_paths=agent_tmp.runner_scratch(repository),
        payload_check=is_task_result,
    )
    assessment = assess(request, batch)
    record = {
        "request": request.model_dump(mode="json"),
        "assignment": assignment.model_dump(mode="json"),
        "batch": batch.model_dump(mode="json"),
        "assessment": assessment.model_dump(mode="json"),
    }
    (batch.artifact_dir / "task.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summarize(assignment, batch, assessment)


def _fields(error: ValidationError) -> str:
    """検査に失敗した項目の位置だけ（値は出さない）。"""
    locations = {
        ".".join(str(part) for part in detail["loc"]) or "(root)"
        for detail in error.errors(include_url=False, include_input=False)
    }
    return ", ".join(sorted(locations))


def read_request(path: Path) -> TaskRequest:
    try:
        return TaskRequest.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise TaskInputError(f"invalid request: {_fields(error)}") from None


# --- 取り込みと後片付け -------------------------------------------------------------------------------------------


class AcceptRefused(TaskInputError):
    """取り込みの前の確認に反したので取り込まない（終了コード 1）。feature worktree は動かしていない。"""


def load_record(run_dir: Path) -> tuple[TaskRequest, BatchResult]:
    path = run_dir / "task.json"
    if run_dir.is_symlink() or not run_dir.is_dir() or path.is_symlink() or not path.is_file():
        raise TaskInputError("run directory must be a real directory with task.json written by run")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        request = TaskRequest.model_validate_json(json.dumps(record["request"]))
        batch = BatchResult.model_validate_json(json.dumps(record["batch"]))
    except ValueError, KeyError, TypeError:
        raise TaskInputError("task.json is not a record written by run") from None
    return request, batch


def accept(run_dir: Path) -> dict[str, Any]:
    """今の状態で判定をやり直し、importable なら（必要なら helper が commit して）--ff-only で取り込む。"""
    request, batch = load_record(run_dir)
    assessment = assess(request, batch)
    if assessment.verdict != "importable" or assessment.work_dir is None:
        raise AcceptRefused(f"not importable ({assessment.verdict}): {'; '.join(assessment.reasons)}")
    repository = Path(request.repository)
    try:
        check_feature(repository, request.revision)
    except TaskInputError as error:
        raise AcceptRefused(str(error)) from None
    work = Path(assessment.work_dir)
    commit = assessment.commit
    try:
        if assessment.helper_commit:
            _git(work, "add", "-A")
            staged = _git(work, "diff", "--cached", "--name-only", "-z", "--no-renames").stdout
            if sorted(os.fsdecode(entry) for entry in staged.split(b"\x00") if entry) != assessment.changed_files:
                raise AcceptRefused("the staged paths differ from the checked changes")
            # hook は迂回しない。行頭の # を残すため、メッセージは空白の整理だけにする
            _git(work, "commit", "-q", "--cleanup=whitespace", "-m", request.commit_message)
            commit = head(work)
            if diff_paths(work, request.revision, commit) != assessment.changed_files:
                raise AcceptRefused("the helper commit differs from the checked changes")
        assert commit is not None
        _git(repository, "merge", "--ff-only", "-q", commit)
        if head(repository) != commit:
            raise AcceptRefused("the feature worktree HEAD is not the imported commit after the merge")
    except subprocess.CalledProcessError as error:
        raise AcceptRefused(f"git {error.cmd[3] if len(error.cmd) > 3 else ''} failed".strip()) from None
    accepted = {
        "state": "accepted",
        "commit": commit,
        "helper_commit": assessment.helper_commit,
        "changed_files": assessment.changed_files,
    }
    (run_dir / "accept.json").write_text(json.dumps(accepted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return accepted


def _registered(repository: Path) -> set[str]:
    output = _git(repository, "worktree", "list", "--porcelain", "-z").stdout
    return {
        os.fsdecode(field[len(b"worktree ") :]) for field in output.split(b"\x00") if field.startswith(b"worktree ")
    }


def cleanup(run_dir: Path) -> dict[str, Any]:
    """停止を確認した試行の worktree の登録を外し（force しない）、何も残らなければ run_dir を消す。"""
    request, batch = load_record(run_dir)
    repository = Path(request.repository)
    removed: list[str] = []
    retained: list[dict[str, str]] = []
    for attempt in (attempt for outcome in batch.results for attempt in outcome.attempts):
        if attempt.work_dir is None:
            continue
        work = str(attempt.work_dir)
        if not attempt.stop_confirmed:
            retained.append({"path": str(attempt.work_dir.parent), "reason": "stop not confirmed"})
        elif attempt.sensitive_paths:
            retained.append({"path": str(attempt.work_dir.parent), "reason": "sensitive paths may hold secrets"})
        elif not attempt.work_dir.exists():
            if work in _registered(repository):
                retained.append({"path": work, "reason": "registered but missing; remove the registration by hand"})
        elif _git(repository, "worktree", "remove", "--", work, check=False).returncode == 0:
            removed.append(work)
        else:
            retained.append({"path": work, "reason": "git worktree remove refused (uncommitted or untracked files)"})
    removed_run_dir = not retained
    if removed_run_dir:
        shutil.rmtree(run_dir)
    return {"removed_worktrees": removed, "retained": retained, "removed_run_dir": removed_run_dir}


# --- CLI ----------------------------------------------------------------------------------------------------------


def failure_reason(error: Exception) -> str:
    if isinstance(error, TaskInputError):
        return str(error)
    if isinstance(error, ConfigError):
        return f"config: {error.category}"
    if isinstance(error, RunnerError):
        return f"runner: {error.category}"
    return type(error).__name__


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """check と run が同じ定義で受け付ける設定の引数。"""
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--config", type=Path, help="個人設定（既定は AGENT_RUNNER_CONFIG・XDG_CONFIG_HOME・~/.config）"
    )
    parser.add_argument("--implement", metavar="AGENT", help="その回だけ issue.implement の一次担当を置き換える")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="implement_task.py", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    _add_config_arguments(commands.add_parser("check", help="設定だけを検査する（worktree・runner に触れない）"))
    launch = commands.add_parser("run", help="Task 1 つを runner で依頼し、取り込めるかを判定する")
    launch.add_argument("--request", type=Path, required=True, help="TaskRequest の JSON")
    launch.add_argument("--output-dir", type=Path, required=True, help="runner の出力先（存在しないパス）")
    _add_config_arguments(launch)
    for name, help_text in (
        ("accept", "判定をやり直し、importable なら --ff-only で feature worktree に取り込む"),
        ("cleanup", "runner の worktree の登録を外し、何も残らなければ出力先を消す"),
    ):
        commands.add_parser(name, help=help_text).add_argument("--run-dir", type=Path, required=True)
    return root


def _error(reason: str, **extra: object) -> int:
    print(json.dumps({"status": "error", "reason": reason, **extra}, ensure_ascii=False))
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"accept", "cleanup"}:
            run_dir = args.run_dir.absolute()
            payload = accept(run_dir) if args.command == "accept" else cleanup(run_dir)
            code = 1 if payload.get("retained") else 0
        else:
            template = args.template.resolve()
            config = args.config.resolve() if args.config else None
            if args.command == "check":
                payload = check(template, config, args.implement)
                code = 0
            else:
                request = read_request(args.request)
                payload = run(load(template, config, args.implement), request, args.output_dir.absolute())
                code = exit_code_for(payload["verdict"])
    except AcceptRefused as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False))
        return 1
    except CheckoutAuditError as error:
        return _error(
            "checkout_audit: registered checkouts cannot be audited before launch",
            checkout_audit=[finding.model_dump(mode="json") for finding in error.findings],
        )
    except (ConfigError, TaskInputError, RunnerError, OSError, subprocess.SubprocessError) as error:
        return _error(failure_reason(error))
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
