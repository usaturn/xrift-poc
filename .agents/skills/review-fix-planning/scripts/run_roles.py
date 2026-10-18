"""設定の修正役割、または一度だけのPR全体レビューと修正確認を実行する。

successは応答の回収であり修正の合格ではない。selfは返された依頼を司令塔が実行する。
fix-checkの成果は既存review-verify dispatcherのacceptで別途検査して受け取る。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

ROOT = Path(__file__).resolve().parents[4]
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
from tools.agent_runner.models import BatchResult  # noqa: E402
from tools.agent_runner.workspace import (  # noqa: E402
    CheckoutAuditError,
    WorkspaceError,
    git_common_dir,
    require_auditable_checkouts,
    resolve_revision,
)


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = load_module("planning_fix_dispatch", ROOT / ".agents/skills/review-verify/scripts/dispatch.py")
findings = verifier.load_review_findings()
review_target = load_module("planning_review_target", ROOT / ".agents/scripts/review_target.py")
review_entry = load_module("planning_review_run", ROOT / ".agents/skills/pr-review/scripts/review_run.py")


class RoleInputError(ValueError):
    """run_roles自身の入力の誤り。文は値・パスを含まない固定の文で、stderrにそのまま出す。"""


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository: str
    number: int

    @field_validator("repository")
    @classmethod
    def slug(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("invalid repository slug")
        return value

    @field_validator("number")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("PR number must be positive")
        return value


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository: str
    source_head: str
    revision: str
    pr: PullRequest
    prompt: dict[str, str]

    @field_validator("repository")
    @classmethod
    def absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("repository must be absolute")
        return value

    @field_validator("source_head", "revision")
    @classmethod
    def commit(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
            raise ValueError("source_head/revision must be full hashes")
        return value


def project(config: Config, phase: str) -> Config:
    """fallbackと投影内の制約を維持する。投影外の依存を黙って捨てない。"""
    if phase not in {"implement", "self-review"}:
        raise RoleInputError("unknown phase")
    config = validate_config(config, "fix")
    roles = ("implement",) if phase == "implement" else ("self-review", "fix-check")
    workflow = config.workflows["fix"]
    for name in roles:
        role = config.roles.get(name)
        assignment = workflow.assignments.get(name)
        if role is None or assignment is None:
            raise ConfigError("invalid_reference")
        if role.writes != (name == "implement") or role.result_shape != "json" or len(assignment.agents) != 1:
            raise ConfigError("invalid_config")
    constraints = {name: other for name, other in workflow.exclude_same_model_as.items() if name in roles}
    if any(other not in roles for other in constraints.values()):
        raise ConfigError("invalid_reference")
    selected = config.model_copy(deep=True)
    selected.workflows = {
        "fix": Workflow(
            assignments={name: workflow.assignments[name] for name in roles}, exclude_same_model_as=constraints
        )
    }
    if phase == "self-review":
        selected.roles["fix-check"].deadline_seconds = min(selected.roles["fix-check"].deadline_seconds, 1800)
    return validate_config(selected, "fix")


def reject_dsh_review_agents(config: Config) -> None:
    """self-review・fix-check を DSH の担当に割り当てた設定を、1 回枠を予約する前に拒否する（#671）。

    loop が DSH のレビュアーに行う親側の準備（``prepare_parent``・保存先への書き込みの許可・``staged`` の保存）を
    run_roles は行わないので、DSH の self-review は完了できず、戻せない枠だけを消費する。runner は一次担当が
    self のとき fallback を試さないが、「割り当てられない」という 1 つの規則にするため fallback も見る。
    """
    for role in ("self-review", "fix-check"):
        assignment = config.workflows["fix"].assignments[role]
        if any(
            config.agents[name].runtime == "dsh" for name in assignment.agents + assignment.fallback if name != "self"
        ):
            raise RoleInputError(
                "self-review/fix-check cannot be assigned to a dsh runtime agent (primary or fallback): run_roles "
                "lacks the parent-side preparation DSH needs (prepare_parent, sandbox write paths, saving staged "
                "results)"
            )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise RoleInputError("invalid JSON input") from None


def reserve_review(path: Path, data: RoleInput) -> None:
    """失敗も一度として残す。slotを戻す・別HEADで上限をリセットする操作はない。"""
    if not path.is_absolute() or path.is_symlink():
        raise RoleInputError("planning_file must be absolute and not a symlink")
    if not path.is_file():
        raise RoleInputError(
            "planning_file must be an existing file; create the provisional v2 planning before launching"
        )
    lock = path.with_suffix(path.suffix + ".review-lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RoleInputError("planning review is locked") from None
    identity = os.fstat(fd)
    temporary: Path | None = None
    try:
        try:
            planning = findings.validate_document(read_json(path))
        except findings.FindingsError:
            planning = None
        if planning is None or planning.get("schema") != "review-findings/v2" or planning.get("kind") != "planning":
            raise RoleInputError("planning_file must be a complete v2 planning document")
        if planning["pr"] != data.pr.model_dump() or planning["reviewed_head"] != data.source_head:
            raise RoleInputError("planning PR/source_head mismatch")
        if any(activity["stage"] == "pre_push_review" for activity in planning["activities"]):
            raise RoleInputError(
                "self-review already reserved; only run_roles.py reserves it and a reservation is never released"
            )
        planning["activities"].append(
            {"stage": "pre_push_review", "actor": planning["planner"], "seconds": None, "waiting_seconds": None}
        )
        findings.validate_document(planning)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".review-reserve-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(planning, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        try:
            current = lock.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            lock.unlink()


def write_exceptions(config: Config, data: RoleInput) -> tuple[str, ...]:
    """外部の担当に割り当てた self-review・fix-check の書き込み。runner の作業範囲の指示への例外として渡す。

    self は起動しないので求めない（既定の流れを変えない）。runner は workflow のすべての役割に同じ一覧を付けるので、
    各行の末尾にその役割を書く。self-review は未 push の commit を ``--source-root`` で pr-review に渡すので、
    helper の Git の書き込み先は RoleInput.repository の共有ディレクトリになる。保存先はブランチ名を知らないので
    ``#<PR>-*`` のパターンにする。
    """
    lines: list[str] = []
    if verifier.assigned_externally(config, "self-review"):
        try:
            parent = review_target.default_parent_root()
            reviews = review_target.repository_reviews_root(parent, data.pr.repository)
            helper = review_entry.helper_write_exceptions(
                tool_root=ROOT,
                parent=parent,
                parent_git=git_common_dir(parent),
                review_dir=f"{reviews}/#{data.pr.number}-*",
                number=data.pr.number,
                source_git=git_common_dir(Path(data.repository)),
            )
        except review_target.TargetError, WorkspaceError:
            raise RoleInputError(
                "self-review write exceptions need the parent main worktree and its Git data"
            ) from None
        lines += [f"{line} (self-review role only)" for line in helper]
    if verifier.assigned_externally(config, "fix-check"):
        lines += [
            f"{line} (fix-check role only)" for line in verifier.fix_check_write_exceptions(Path(data.repository))
        ]
    return tuple(lines)


def fix_context(request: Mapping[str, Any], data: RoleInput, config: Config) -> dict[str, Any]:
    fixed = verifier.FixInput.model_validate(request)
    if (
        Path(fixed.repository).resolve() != Path(data.repository).resolve()
        or fixed.from_ != data.source_head
        or fixed.to != data.revision
    ):
        raise RoleInputError("fix_input repository/from/to mismatch")
    try:
        verified: dict[str, Any] = findings.validate_document(read_json(Path(fixed.verified_file)))
    except findings.FindingsError:
        raise RoleInputError("verified_file must be a valid review-findings document") from None
    if verified["kind"] != "verified":
        raise RoleInputError("verified_file must contain a verified document")
    if verified["pr"] != data.pr.model_dump() or verified["reviewed_head"] != data.source_head:
        raise RoleInputError("verified_file PR/source_head mismatch")
    review_dir = Path(fixed.review_dir)
    verifier.check_review_directories(review_dir)
    skill_bytes = verifier.SKILL.read_bytes()
    return {
        "request": fixed.model_dump(by_alias=True),
        "verified": verified,
        "skill": {"path": ".agents/skills/review-verify/SKILL.md", "sha256": hashlib.sha256(skill_bytes).hexdigest()},
        "time_limits": verifier.time_limits(datetime.now(UTC), int(config.roles["fix-check"].deadline_seconds)),
        "tool_root": str(ROOT),
        "skill_dir": str(verifier.SKILL.parent),
        "review_snapshot": verifier.snapshot_review(review_dir),
        "skill_body": skill_bytes.decode("utf-8"),
    }


def run(
    config: Config,
    phase: str,
    request: Mapping[str, Any],
    output_dir: Path,
    *,
    planning_file: Path | None = None,
    fix_input: Mapping[str, Any] | None = None,
    adapter_loader: Callable[[str], Adapter] = load_adapter,
) -> BatchResult:
    selected = project(config, phase)
    if phase == "self-review":
        reject_dsh_review_agents(selected)
    data = RoleInput.model_validate(request)
    repository = Path(data.repository)
    try:
        for revision in (data.source_head, data.revision):
            if resolve_revision(repository, revision) != revision:
                raise RoleInputError("revision is not a commit")
    except WorkspaceError:
        raise RoleInputError("repository/revisions cannot be resolved") from None
    if output_dir.exists() or output_dir.is_symlink():
        raise RoleInputError("output directory must be new")
    role_prompts: dict[str, str] = {}
    for role in selected.workflows["fix"].assignments:
        if not data.prompt.get(role, "").strip():
            raise RoleInputError("each selected role needs a complete prompt")
        role_prompts[role] = data.prompt[role]
    require_auditable_checkouts(repository)
    # 1 回枠の予約より前に求める。求められなければ枠を消費せずに止まる
    exceptions = write_exceptions(selected, data) if phase == "self-review" else ()
    context = None
    if phase == "self-review":
        if planning_file is None or fix_input is None:
            raise RoleInputError("self-review requires planning_file and fix_input")
        context = fix_context(fix_input, data, selected)
        attached = {key: value for key, value in context.items() if key not in {"review_snapshot", "skill_body"}}
        role_prompts["fix-check"] += (
            "\nUse review-verify mode fix_check with exactly the supplied seven input fields. "
            "Use only committed from/to in the runner worktree, "
            "never target code from the original checkout/tool_root. "
            "No nested agent, GitHub writes, push, commit, repair, network probes, or caller review_dir writes. "
            "Use Python 3.14+ and fake commands with one-shot HOME. "
            "Observe time_limits between checks; record remaining unchecked checks and return before deadline_at. "
            "Run statement-candidates into unique temporary storage. Preserve all tool candidates/claims/terms; "
            "only append discoveries. "
            "Return exactly {verified: fix_check document, inventory: unchanged tool sidecar}. "
            "The caller attaches observed identity and validates/publishes; do not invent it. "
            "Use statement_candidates.file=<to first 12>.statement-candidates.json.\n"
            + json.dumps(attached, ensure_ascii=False)
            + "\nApplied skill:\n"
            + context["skill_body"]
        )
        reserve_review(planning_file, data)
    prompt = (
        "Perform ONLY the instruction for your actual Request.role from role_prompts. "
        "Never execute another role or invoke another agent. These prompts are independent complete inputs. "
        "Return the JSON required by your role; success of collection is not approval.\n"
        + json.dumps(
            {
                "pr": data.pr.model_dump(),
                "source_head": data.source_head,
                "revision": data.revision,
                "role_prompts": role_prompts,
            },
            ensure_ascii=False,
        )
    )
    batch = run_workflow(
        selected,
        "fix",
        prompt,
        repository,
        revision=data.revision,
        concurrency=1 if phase == "implement" else 2,
        output_dir=output_dir,
        adapter_loader=adapter_loader,
        write_exceptions=exceptions,
    )
    stopped = all(attempt.stop_confirmed for result in batch.results for attempt in result.attempts)
    changes = (
        verifier.review_changes(Path(context["request"]["review_dir"]), context["review_snapshot"])
        if context is not None and stopped
        else None
    )
    self_prompts = {result.role: role_prompts[result.role] for result in batch.results if result.state == "self"}
    summary = {
        "request": data.model_dump(),
        "batch": batch.model_dump(mode="json"),
        "self_prompts": self_prompts,
        "review_changes": changes,
    }
    (batch.artifact_dir / "roles.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if context is not None:
        fixed_results = [result for result in batch.results if result.role == "fix-check"]
        state = (
            "failed"
            if not stopped or not fixed_results
            else "self"
            if fixed_results[0].state == "self"
            else "success"
            if fixed_results[0].state == "success"
            else "failed"
        )
        fixed_batch = batch.model_copy(
            update={
                "state": state,
                "results": fixed_results,
                "assignments": {"fix-check": batch.assignments["fix-check"]},
            }
        )
        receipt_dir = batch.artifact_dir / "fix-check-receipt"
        receipt_dir.mkdir()
        receipt = {
            key: value
            for key, value in context.items()
            if key in {"request", "verified", "skill", "time_limits", "review_snapshot"}
        }
        receipt.update(
            batch=fixed_batch.model_dump(mode="json"),
            verifier=verifier.observed_verifier(fixed_batch, selected, context["skill"]),
            review_changes=changes,
        )
        if state == "self":
            receipt["prompt"] = role_prompts["fix-check"]
        (receipt_dir / "dispatch.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return batch


def failure_reason(error: Exception) -> str:
    """stderrに出す失敗の理由。RoleInputErrorは固定の文、それ以外は分類だけ。

    loopのstartも、ここで拒否する設定を同じ理由で止める（#675）。
    """
    return str(error) if isinstance(error, RoleInputError) else verifier.failure_category(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("implement", "self-review"), required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--planning-file", type=Path)
    parser.add_argument("--fix-input", type=Path)
    parser.add_argument("--template", type=Path, default=ROOT / ".devcontainer/agent-runner.toml")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--assign", action="append", default=[], metavar="ROLE=AGENT")
    args = parser.parse_args(argv)
    try:
        assignments: dict[str, list[str]] = {}
        for entry in args.assign:
            role, separator, agent = entry.partition("=")
            if (
                not separator
                or role not in {"implement", "self-review", "fix-check"}
                or not agent
                or role in assignments
            ):
                raise RoleInputError("invalid role assignment")
            assignments[role] = [agent]
        personal = args.config
        if personal is None:
            candidate = resolve_config_path()
            personal = candidate if candidate.exists() else None
        config = load_config(args.template, personal, assignments=assignments or None, workflow="fix")
        batch = run(
            config,
            args.phase,
            read_json(args.request),
            args.output_dir,
            planning_file=args.planning_file,
            fix_input=read_json(args.fix_input) if args.fix_input else None,
        )
        saved = read_json(batch.artifact_dir / "roles.json")
    except CheckoutAuditError as error:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "category": "environment",
                    "checkout_audit": [finding.model_dump() for finding in error.findings],
                },
                ensure_ascii=False,
            )
        )
        return 1
    except (ConfigError, RunnerError, OSError, ValueError, findings.FindingsError) as error:
        reason = failure_reason(error)
        print(
            f"planning roles: input/config/run failed ({reason}); no verification success implied",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "state": batch.state,
                "artifact_dir": str(batch.artifact_dir),
                "pending_self": list(saved["self_prompts"]),
                "outside_changes": batch.outside_changes,
                "checkout_audit": [finding.model_dump() for finding in batch.checkout_audit],
                "review_changes": saved["review_changes"],
            },
            ensure_ascii=False,
        )
    )
    collected = all(
        result.state in {"self", "success"} and all(attempt.stop_confirmed for attempt in result.attempts)
        for result in batch.results
    )
    return (
        0
        if collected and not batch.outside_changes and (args.phase == "implement" or saved["review_changes"] == [])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
