"""PR 修正ループ（pr-review-fix-loop）の helper。

司令塔が start → cycle-begin → review → （review-fix-planning）→ cycle-end → … → finish を順に呼ぶ。
状態は ``<review_dir>/loop/`` に保存し、どのサブコマンドも標準出力に 1 つの JSON を書く。JSON の ``next`` は、
次に実行する完全なコマンド（argv の配列）である。レビュアーは agent-runner で起動し、判断の記録は
``.agents/scripts/review_records.py`` で照合する。形式を足さず、既存の成果をパスと SHA-256 で参照する。

終了コード: 0 成功、1 想定外の内部エラー（stderr に traceback）、2 引数・入力の誤り、3 排他・状態の順序の違反、
4 対象の食い違い、5 外部（GitHub・git・runner）の失敗、6 全レビュアーの失敗、7 資源の停止を確認できない。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from tools.agent_runner import (  # noqa: E402
    Assignment,
    BatchResult,
    Config,
    ConfigError,
    Request,
    Result,
    RunnerError,
    load_config,
    resolve_config_path,
    run_workflow,
)
from tools.agent_runner.adapters import Adapter, load_adapter  # noqa: E402
from tools.agent_runner.hooks import run_after_run  # noqa: E402

SCHEMA = "pr-review-fix-loop/v1"
HELPER = Path(__file__).resolve()
DEFAULT_TEMPLATE = ROOT / ".devcontainer/agent-runner.toml"
RUNTIMES = ("claude-code", "devin", "pi", "codex", "cursor", "grok-build")
FIX_ROLES = ("implement", "self-review", "fix-check")
ROLES = ("inspect", *FIX_ROLES)
OUTCOMES = ("fixes_pushed", "no_fix_needed", "stopped")
STOP_REASON = re.compile(r"(?:permission|environment|external): \S.*", re.DOTALL)
MAX_CYCLES_LIMIT = 10
# 使い捨ての runner の出力を置く /tmp のディレクトリ名（AGENTS.md の「PR 修正ループの所有 worktree」と同じ接頭辞）
TMP_PREFIX = "xrift-poc-pr-loop-"
# start が現在の Herdr ペインの名前を変えるときの herdr の期限（秒）。応答が無くても start を待たせ続けない
HERDR_TIMEOUT_SECONDS: float = 10
_HISTORY_ROW = re.compile(
    r"^\|[^|\n]*\|\s*`?(?P<head>[0-9a-f]{12})`?\s*\|\s*(?:Initial|Update)\s*\|\s*$", re.MULTILINE
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


target = _load("pr_review_fix_loop_review_target", ROOT / ".agents/scripts/review_target.py")
records = _load("pr_review_fix_loop_review_records", ROOT / ".agents/scripts/review_records.py")
findings = _load("pr_review_fix_loop_review_findings", ROOT / ".agents/scripts/review_findings.py")
review_entry = _load("pr_review_fix_loop_review_run", ROOT / ".agents/skills/pr-review/scripts/review_run.py")
run_roles = _load("pr_review_fix_loop_run_roles", ROOT / ".agents/skills/review-fix-planning/scripts/run_roles.py")


class LoopError(Exception):
    """利用者・司令塔に返す失敗。``code`` は終了コード、``stage`` は拒否した段階。"""

    def __init__(
        self,
        code: int,
        stage: str,
        reason: str,
        *,
        next_command: list[str] | None = None,
        requires_input: dict[str, str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.stage = stage
        self.reason = reason
        self.next_command = next_command
        # next_command に足す引数のうち、司令塔が事実を確かめて書く値（{オプション: 書く内容}）。
        # 書きかけの値を実行させないよう、next_command には含めない
        self.requires_input = requires_input


_PLACEHOLDER = re.compile(r"<[^<>]*>")
# recover が求める確認の事実（LoopError.requires_input）
CONFIRM_STOPPED = {
    "--operator-confirmed-stopped": (
        "runner とレビュアーのプロセスが残っていないことを確かめた事実（例: 確かめたコマンドと結果）"
    )
}


def require_fact(option: str, value: str) -> str:
    """司令塔が書く値を検査する。空や、<...> のような書きかけの値を受け付けない。"""
    text = value.strip()
    if not text or _PLACEHOLDER.search(text):
        raise LoopError(2, "arguments", f"{option} には確かめた事実を具体的に書く（空や <...> の形は受け付けない）")
    return text


# --- 記録のモデル -------------------------------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)


class PrRef(StrictModel):
    repository: str
    number: int
    url: str

    def key(self) -> dict[str, Any]:
        """判断の記録・候補一覧の ``pr`` と同じ形。"""
        return {"repository": self.repository, "number": self.number}


class LockRecord(StrictModel):
    schema_: Literal["pr-review-fix-loop/v1"] = Field(default="pr-review-fix-loop/v1", alias="schema")
    kind: Literal["lock"] = "lock"
    run_id: str
    pr: PrRef
    run_dir: str
    created_at: str


class Controller(StrictModel):
    model: str
    runtime: str


class Options(StrictModel):
    max_cycles: int
    hold_action: Literal["skip", "fix"] | None
    assign: dict[str, list[str]]
    template: str
    config: str | None
    fresh_review: bool


class Resource(StrictModel):
    path: str
    kind: Literal["runner_output"] = "runner_output"
    cycle: int
    state: Literal["created", "removed", "retained"]
    reason: str | None = None


class RunResult(StrictModel):
    outcome: Literal["fixes_pushed", "no_fix_needed", "stopped"]
    ended_by: Literal["outcome", "cycle_limit", "abort"]
    stop_reason: str | None
    merged: bool
    cycles: int


class HerdrPane(StrictModel):
    """start が現在の Herdr ペインの名前を変えた結果。"""

    state: Literal["renamed", "skipped", "failed"]
    pane_id: str | None
    label: str
    reason: str | None = None


class RunRecord(StrictModel):
    schema_: Literal["pr-review-fix-loop/v1"] = Field(default="pr-review-fix-loop/v1", alias="schema")
    kind: Literal["run"] = "run"
    run_id: str
    pr: PrRef
    review_dir: str
    tool_root: str
    tool_commit: str
    controller: Controller
    options: Options
    assignments: dict[str, dict[str, Any]]
    started_at: str
    state: Literal["running", "ended", "held", "abandoned"]
    ended_at: str | None = None
    result: RunResult | None = None
    resources: list[Resource] = Field(default_factory=list[Resource])
    notes: list[str] = Field(default_factory=list[str])
    # この項目より前に作った run には無い
    herdr_pane: HerdrPane | None = None


RejectedStage = Literal["runner", "payload", "binding", "head", "artifacts"]


class ReviewerEntry(StrictModel):
    requested_agent: str
    actual_agent: str
    runner_state: str
    verdict: Literal["accepted", "rejected"]
    reason: str | None = None
    identity: dict[str, Any] = Field(default_factory=dict[str, Any])
    reviewer: dict[str, Any] | None = None
    timing: dict[str, Any] | None = None
    review_file: str | None = None
    review_sha256: str | None = None
    findings_file: str | None = None
    findings_sha256: str | None = None
    active_findings: int | None = None
    cleanup: str | None = None
    warnings: list[str] = Field(default_factory=list[str])
    retained_paths: list[str] = Field(default_factory=list[str])
    # 却下したときの段階と、却下した試行に実際に渡した指示（request.json の写し）。stdout・stderr は写さない
    rejected_stage: RejectedStage | None = None
    request_file: str | None = None
    request_sha256: str | None = None


class ReusedReview(StrictModel):
    model: str
    runtime: str
    review_file: str
    review_sha256: str
    findings_file: str
    findings_sha256: str
    reviewed_head: str
    active_findings: int


class BatchInfo(StrictModel):
    run_id: str
    state: str
    assignments: dict[str, list[str]]
    result_file: str
    outside_changes: list[str]


class CleanupInfo(StrictModel):
    state: Literal["removed", "retained"]
    retained: list[str]


class RerunPart(StrictModel):
    """runner は成功したが loop が受け付けなかったレビュアーの、同じサイクルでの 1 回だけの再実行。"""

    agents: list[str]
    # 再実行で置き換えた 1 回目の記録
    replaced: list[ReviewerEntry]
    output_dir: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    seconds: float | None = None
    batch: BatchInfo | None = None
    after_run_started: bool = False
    after_run: list[dict[str, Any]] | None = None
    cleanup: CleanupInfo | None = None


class ReviewPart(StrictModel):
    mode: Literal["run", "reuse"]
    output_dir: str | None = None
    comments_file: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    seconds: float | None = None
    batch: BatchInfo | None = None
    after_run_started: bool = False
    after_run: list[dict[str, Any]] | None = None
    reviewers: list[ReviewerEntry] = Field(default_factory=list[ReviewerEntry])
    reused: list[ReusedReview] = Field(default_factory=list[ReusedReview])
    cleanup: CleanupInfo | None = None
    rerun: RerunPart | None = None


class RoleRun(StrictModel):
    dir: str
    state: str
    assignments: dict[str, list[str]]
    results: list[dict[str, Any]]


class FinalizePart(StrictModel):
    result_file: str
    result: dict[str, Any]
    round_dir: str | None


class PlanningPart(StrictModel):
    round: int
    comment_id: int | None
    posted: bool
    outcome: Literal["fixes_pushed", "no_fix_needed", "stopped"]
    commit: str | None
    stop_reason: str | None
    merged: bool
    decisions: dict[str, dict[str, int]]
    planning_file: str | None
    planning_sha256: str | None
    verified_file: str | None
    verified_sha256: str | None
    finalize: FinalizePart | None
    role_runs: list[RoleRun]
    ended_at: str


class CycleRecord(StrictModel):
    schema_: Literal["pr-review-fix-loop/v1"] = Field(default="pr-review-fix-loop/v1", alias="schema")
    kind: Literal["cycle"] = "cycle"
    run_id: str
    cycle: int
    head: str
    started_at: str
    ended_at: str | None = None
    state: Literal["reviewing", "planning", "ended"]
    # cycle-begin の時点で PR にあった判断の記録の最大の round。cycle-end はこれより後の記録だけを受け付ける
    prior_round: int = 0
    review: ReviewPart
    planning: PlanningPart | None = None


# --- 入出力 -------------------------------------------------------------------------------------------------------


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: BaseModel | Mapping[str, Any] | Sequence[Any]) -> None:
    """同じディレクトリの一時ファイルに書いてから置き換える（途中の状態を残さない）。"""
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path: Path, *, stage: str = "arguments") -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LoopError(2, stage, f"{path} を JSON として読めない: {error}") from error


def read_model[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LoopError(2, "arguments", f"{path} を読めない: {error}") from error
    except ValidationError as error:
        raise LoopError(
            2, "arguments", f"{path} は {SCHEMA} の記録として読めない: {error.error_count()} 件の問題"
        ) from error


def loop_dir(review_dir: Path) -> Path:
    return review_dir / "loop"


def lock_path(review_dir: Path) -> Path:
    return loop_dir(review_dir) / "lock.json"


def cycle_path(run_dir: Path, number: int) -> Path:
    return run_dir / f"cycle-{number}.json"


def cycle_dir(run_dir: Path, number: int) -> Path:
    return run_dir / f"cycle-{number}"


def load_run(run_dir: Path) -> RunRecord:
    return read_model(run_dir / "run.json", RunRecord)


def save_run(run_dir: Path, run: RunRecord) -> None:
    write_json(run_dir / "run.json", run)


def load_cycles(run_dir: Path) -> list[CycleRecord]:
    found: list[CycleRecord] = []
    number = 1
    while cycle_path(run_dir, number).exists():
        found.append(read_model(cycle_path(run_dir, number), CycleRecord))
        number += 1
    return found


def save_cycle(run_dir: Path, cycle: CycleRecord) -> None:
    write_json(cycle_path(run_dir, cycle.cycle), cycle)


def command(subcommand: str, *args: str) -> list[str]:
    """この helper を同じ版で呼び直す完全なコマンド。"""
    return [
        "uv",
        "run",
        "--project",
        str(ROOT / "tools/devenv"),
        "--locked",
        "python",
        str(HELPER),
        subcommand,
        *args,
    ]


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise LoopError(5, "git", f"git {' '.join(args[:2])} が失敗した: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


# --- ロック -------------------------------------------------------------------------------------------------------


def read_lock(review_dir: Path) -> LockRecord | None:
    path = lock_path(review_dir)
    if not path.exists():
        return None
    return read_model(path, LockRecord)


def acquire_lock(review_dir: Path, lock: LockRecord) -> None:
    """PR ごとのロックを新規作成で取る。既にあれば、古くても上書きしない。"""
    path = lock_path(review_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 中身を書いた一時ファイルを link で置く（新規作成だけで、空や書きかけのロックを残さない）
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".lock.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(lock.model_dump_json(indent=2) + "\n")
        os.link(temporary, path)
    except FileExistsError:
        try:
            holder = read_lock(review_dir)
        except LoopError:
            holder = None
        if holder is None:
            raise LoopError(3, "lock_held", f"読めないロック {path} がある。中身を確かめてから扱う") from None
        raise LoopError(
            3,
            "lock_held",
            f"この PR は run {holder.run_id}（{holder.run_dir}、{holder.created_at} 開始）が使用中",
            next_command=command("status", "--run", holder.run_dir),
        ) from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def release_lock(review_dir: Path, run_id: str) -> bool:
    """自分の run のロックだけを消す。"""
    lock = read_lock(review_dir)
    if lock is None or lock.run_id != run_id:
        return False
    lock_path(review_dir).unlink()
    return True


def require_owner(run: RunRecord) -> None:
    lock = read_lock(Path(run.review_dir))
    if lock is None or lock.run_id != run.run_id:
        holder = "無い" if lock is None else f"run {lock.run_id} のもの"
        raise LoopError(3, "not_owner", f"この PR のロックは {holder}。run {run.run_id} は操作できない")


# --- GitHub -------------------------------------------------------------------------------------------------------

GhRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run_gh(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


class PullState(StrictModel):
    repository: str
    number: int
    url: str
    state: str
    merged: bool
    head_sha: str
    head_ref: str

    @property
    def open(self) -> bool:
        return self.state == "open" and not self.merged


class GitHub:
    """gh の読み取りだけを行う。実行関数は差し替えられる（テストでは偽の gh）。"""

    def __init__(self, runner: GhRunner = _run_gh) -> None:
        self.runner = runner

    def _api(self, *args: str) -> Any:
        result = self.runner(["gh", "api", *args])
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[:300]
            raise LoopError(5, "github", f"gh api {args[-1]} が失敗した（終了コード {result.returncode}）: {detail}")
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise LoopError(5, "github", f"gh api {args[-1]} の出力が JSON ではない") from error

    def pull(self, repository: str, number: int) -> PullState:
        data = self._api(f"repos/{repository}/pulls/{number}")
        try:
            value = cast(dict[str, Any], data)
            head = cast(dict[str, Any], value["head"])
            # 正式な名前（base 側の full_name）を使う。大文字小文字の違う指定で、ロックや保存先が分かれないようにする
            canonical = cast(str, cast(dict[str, Any], cast(dict[str, Any], value["base"])["repo"])["full_name"])
            state = PullState(
                repository=canonical,
                number=value["number"],
                url=value["html_url"],
                state=value["state"],
                merged=value["merged"],
                head_sha=head["sha"],
                head_ref=head["ref"],
            )
        except (KeyError, TypeError, ValidationError) as error:
            raise LoopError(5, "github", f"{repository}#{number} の PR の応答を読めない") from error
        if (
            state.number != number
            or state.repository.lower() != repository.lower()
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", state.head_sha) is None
        ):
            raise LoopError(5, "github", f"{repository}#{number} の PR の応答が依頼と食い違う")
        return state

    def comments(self, repository: str, number: int) -> Any:
        """``gh api --paginate --slurp`` の出力（ページの配列）をそのまま返す。"""
        return self._api("--paginate", "--slurp", f"repos/{repository}/issues/{number}/comments")


# --- Herdr --------------------------------------------------------------------------------------------------------


def rename_pane(number: int) -> HerdrPane:
    """Herdr のペインの中なら、現在のペイン（HERDR_PANE_ID）の名前を ``PR#<番号>`` に変える。

    ほかのペインやフォーカスは変えない。ペインの名前は表示だけの情報なので、失敗しても例外にせず結果で返す。
    """
    label = f"PR#{number}"
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    if os.environ.get("HERDR_ENV") != "1":
        return HerdrPane(state="skipped", pane_id=None, label=label, reason="HERDR_ENV が 1 ではない（Herdr の外）")
    if not pane_id.strip():
        return HerdrPane(state="skipped", pane_id=None, label=label, reason="HERDR_PANE_ID が空")
    try:
        result = subprocess.run(
            ["herdr", "pane", "rename", pane_id, label],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=HERDR_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        reason = "herdr が PATH に無い"
    except subprocess.TimeoutExpired:
        reason = f"herdr pane rename が {HERDR_TIMEOUT_SECONDS:g} 秒以内に終わらなかった"
    except OSError as error:
        reason = f"herdr を起動できない: {error}"
    else:
        if result.returncode == 0:
            return HerdrPane(state="renamed", pane_id=pane_id, label=label)
        detail = (result.stderr or "").strip()[:300]
        reason = f"herdr pane rename が終了コード {result.returncode} で失敗した: {detail}"
    return HerdrPane(state="failed", pane_id=pane_id, label=label, reason=reason)


# --- 設定と割り当て -----------------------------------------------------------------------------------------------


def parse_assign(entries: Sequence[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in entries:
        role, separator, value = entry.partition("=")
        names = [name.strip() for name in value.split(",")]
        if not separator or role not in ROLES or role in result or not value or any(not name for name in names):
            raise LoopError(
                2, "arguments", f"--assign は ROLE=AGENT[,AGENT]（ROLE は {', '.join(ROLES)}、各 1 回）: {entry}"
            )
        if role == "inspect" and "self" in names:
            raise LoopError(
                2, "arguments", "レビュアー（inspect）に self は使えない。runner で起動する agent を指定する"
            )
        if role in FIX_ROLES and len(names) != 1:
            raise LoopError(2, "arguments", f"{role} の担当は 1 つだけ指定する: {entry}")
        result[role] = names
    return result


def personal_config(config: Path | None) -> Path | None:
    """明示した設定、無ければ既存の個人設定（無ければ雛形だけ）。"""
    if config is not None:
        return config
    candidate = resolve_config_path()
    return candidate if candidate.exists() else None


def load_review_config(run: RunRecord) -> Config:
    inspect = {"inspect": run.options.assign["inspect"]} if "inspect" in run.options.assign else None
    config = Path(run.options.config) if run.options.config is not None else None
    try:
        return load_config(Path(run.options.template), personal_config(config), assignments=inspect, workflow="review")
    except ConfigError as error:
        raise LoopError(2, "config", f"agent-runner の設定を使えない（{error.category}）") from error


def load_assignments(
    template: Path, config: Path | None, assign: Mapping[str, list[str]]
) -> dict[str, dict[str, Any]]:
    """雛形 < 個人設定 < --assign で読み込み、review の inspect と fix の各役割の有効な割り当てを返す。

    run_roles.py が設定だけで拒否する fix の割り当て（DSH の self-review・fix-check など）は、レビューや implement の
    後の run_roles の呼び出しを待たずにここで止める（#675）。判定と理由は run_roles の関数を使い、規則を写さない。
    """
    personal = personal_config(config)
    inspect = {role: agents for role, agents in assign.items() if role == "inspect"}
    fix = {role: agents for role, agents in assign.items() if role in FIX_ROLES}
    try:
        review_config = load_config(template, personal, assignments=inspect or None, workflow="review")
        fix_config = load_config(template, personal, assignments=fix or None, workflow="fix")
    except ConfigError as error:
        raise LoopError(2, "config", f"agent-runner の設定を使えない（{error.category}）") from error
    review_assignments = review_config.workflows["review"].assignments
    fix_assignments = fix_config.workflows["fix"].assignments
    if "inspect" not in review_assignments or review_assignments["inspect"].agents == ["self"]:
        raise LoopError(2, "config", "workflows.review の inspect に runner で起動する agent が割り当てられていない")
    missing = [role for role in FIX_ROLES if role not in fix_assignments]
    if missing:
        raise LoopError(2, "config", f"workflows.fix に {', '.join(missing)} の割り当てが無い")
    try:
        run_roles.project(fix_config, "implement")
        run_roles.reject_dsh_review_agents(run_roles.project(fix_config, "self-review"))
    except (ConfigError, run_roles.RoleInputError) as error:
        raise LoopError(
            2,
            "config",
            f"run_roles.py が拒否する fix の割り当て（{run_roles.failure_reason(error)}）。"
            "--assign か、個人設定の workflows.fix・roles を直す",
        ) from error
    return {
        "review": {"inspect": review_assignments["inspect"].model_dump(mode="json")},
        "fix": {role: fix_assignments[role].model_dump(mode="json") for role in FIX_ROLES},
    }


def planning_guidance(run: RunRecord) -> dict[str, Any]:
    """司令塔が review-fix-planning と run_roles.py に渡す値（組み立て直させない）。"""
    options: list[str] = ["--template", run.options.template]
    if run.options.config is not None:
        options += ["--config", run.options.config]
    for role in FIX_ROLES:
        if role in run.options.assign:
            options += ["--assign", f"{role}={run.options.assign[role][0]}"]
    return {
        "review_dir": run.review_dir,
        "pr_url": run.pr.url,
        "hold_action": run.options.hold_action,
        "run_roles_options": options,
    }


# --- PR の解決 ----------------------------------------------------------------------------------------------------


def _target_error(error: Exception) -> LoopError:
    code = cast(int, getattr(error, "code", 0))
    if code in {2, 10, 11}:
        return LoopError(2, "arguments", str(error))
    if code == 7:
        return LoopError(5, "parent", str(error))
    return LoopError(5, "github", str(error))


def resolve_pr(argument: str, parent_root: Path | None) -> tuple[Path, str, int]:
    try:
        root = cast(Path, target.require_parent_root(parent_root) if parent_root else target.default_parent_root())
        parsed = target.parse_pull_request_argument(argument)
        repository = cast(str, parsed.repository or target.parent_repository(root))
        target.require_planning_owner(repository)
    except target.TargetError as error:
        raise _target_error(error) from error
    return root, repository, cast(int, parsed.number)


def review_dir_for(root: Path, pull: PullState) -> Path:
    try:
        return cast(Path, target.review_directory(root, pull.repository, pull.number, pull.head_ref))
    except target.TargetError as error:
        raise _target_error(error) from error


# --- 再利用できる成果 ---------------------------------------------------------------------------------------------


def last_history_head(markdown: str) -> str | None:
    rows = _HISTORY_ROW.findall(markdown)
    return cast(str, rows[-1]) if rows else None


def find_reusable(review_dir: Path, pr: PrRef, head: str) -> list[ReusedReview]:
    """現在の HEAD を対象にした、検査を通る成果 2 件（候補一覧と、対になる本文）を探す。"""
    folder = review_dir / "findings"
    if not folder.is_dir() or folder.is_symlink():
        return []
    suffix = f"-{head[:12]}.json"
    found: list[ReusedReview] = []
    for path in sorted(folder.glob(f"*{suffix}")):
        review = review_dir / (path.name.removesuffix(suffix) + ".md")
        if path.is_symlink() or not path.is_file() or review.is_symlink() or not review.is_file():
            continue
        try:
            document = cast(dict[str, Any], findings.validate_document(json.loads(path.read_text(encoding="utf-8"))))
            markdown = review.read_text(encoding="utf-8")
            if (
                document["kind"] != "candidates"
                or document["reviewed_head"] != head
                or document["pr"] != pr.key()
                or last_history_head(markdown) != head[:12]
            ):
                continue
            findings.check_pair(markdown, document)
        except OSError, ValueError:
            continue
        reviewer = cast(dict[str, Any], document["reviewer"])
        found.append(
            ReusedReview(
                model=reviewer["model"],
                runtime=reviewer["runtime"],
                review_file=str(review),
                review_sha256=sha256_file(review),
                findings_file=str(path),
                findings_sha256=sha256_file(path),
                reviewed_head=head,
                active_findings=len(cast(list[Any], document["findings"])),
            )
        )
    return found


# --- レビュアー ---------------------------------------------------------------------------------------------------


def review_config_without_github_tokens(config: Config) -> Config:
    """PR APIs run in the parent; keep LLM credentials and other runtimes unchanged."""
    safe = config.model_copy(deep=True)
    for agent in safe.agents.values():
        if agent.runtime == "dsh":
            agent.env = [name for name in agent.env if name not in {"GH_TOKEN", "GITHUB_TOKEN"}]
    return safe


def review_write_paths(run: RunRecord, runtimes: Mapping[str, str]) -> tuple[Path, ...]:
    """Controller-owned grants, never roots inferred from the agent's prompt/config."""
    if "dsh" not in runtimes.values():
        return ()
    directory = Path(run.review_dir)
    try:
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise ValueError("review directory must be canonical")
        parent = directory.parents[3]
        common = Path(git_output(parent, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        lock = common / "pr-review-save.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("save lock must be a regular file")
        finally:
            os.close(descriptor)
        return directory, lock
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise LoopError(5, "sandbox", "DSH の必要保存先を安全に準備できない") from error


def review_write_exceptions(run: RunRecord) -> tuple[str, ...]:
    """pr-review の helper が管理する書き込み先。runner の作業範囲の指示に、呼び出し側が許可する例外として載せる。

    inspect の writes=False は、レビュー対象・どの checkout の追跡ファイル・他のレビュアーの成果を変えないという
    意味で、ここに挙げる helper の書き込みはそれに当たらない。パスを事前に決められるものは、パスを行の先頭に置く。
    """
    tool_root = Path(run.tool_root)
    review_dir = Path(run.review_dir)
    parent = review_dir.parents[3]
    common = git_output(parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
    number = run.pr.number
    return (
        f"{tool_root}: the source checkout named tool_root in the request. Read the pr-review skill and its "
        "references there and run its helper from there; do not edit it",
        f"{review_dir}: the run records and the saved review and candidates that the helper writes; "
        "do not edit them by hand",
        f"{common}: the helper's save lock (pr-review-save.lock) and the Git objects, refs and worktree "
        "registrations that the helper adds and removes for its review worktree (a submodule's Git data is "
        "here too; a repository without a checkout here is cloned inside the helper's session instead)",
        f"{parent / 'tmp/agents'}/pr-review-scratch-{number}.*: one scratch directory created with "
        "agent_tmp.py mkdir as pr-review allows; remove it when it is no longer needed",
        f"the session that the helper's prepare creates (pr-review-{number}-* in the temporary directory, or "
        "the DSH staging area): its review worktree, backup and state belong to the helper. Write only the "
        "draft and findings_draft that prepare returns, and leave no files in the review worktree",
        f"{tool_root / 'tools/devenv/.venv'} and uv's cache: the environment that "
        "`uv run --project <tool_root>/tools/devenv --locked` maintains",
        "files that the agent runtime or its extensions create by themselves in the directory you were launched in "
        "(for example .pi-lens-probe-home/ from pi-lens): they belong to the runtime, not to you. Do not edit or "
        "delete them, and do not count them as your modifications; the caller discards that directory",
    )


def discard_parent_input(path: Path) -> None:
    """この review が作り、まだどのレビュアーにも渡していない親入力だけを消す（同じサイクルで作り直せるように）。

    消せなければ残る。やり直しの親準備は既存のディレクトリを拒否するので、黙って上書きはしない。
    """
    shutil.rmtree(path, ignore_errors=True)


def reviewer_prompt(
    *,
    tool_root: Path,
    pr: PrRef,
    head: str,
    comments_file: Path,
    runtimes: Mapping[str, str],
    parent_pr_data: dict[str, str] | None = None,
) -> str:
    """全レビュアーに同じものを渡す依頼文。値は末尾の JSON ブロックに置く。"""
    parameters = {
        "pr_url": pr.url,
        "pr_head": head,
        "records_comments": str(comments_file),
        "tool_root": str(tool_root),
        "skill": str(tool_root / ".agents/skills/pr-review/SKILL.md"),
        "helper": str(tool_root / ".agents/skills/pr-review/scripts/review_run.py"),
        "runtimes": dict(sorted(runtimes.items())),
        "parent_pr_data": parent_pr_data,
    }
    return (
        "あなたは PR 修正ループのレビュアーとして、pr-review を非対話で 1 回だけ実行する。"
        "値は末尾の JSON ブロックを使う。\n\n"
        "1. 最初に `skill`（pr-review の SKILL.md）と、その参照文書（references/time-budget.md・evidence.md・"
        "noninteractive.md）を全文読み、その手順に従う。"
        "`tool_root` は skill と helper の置き場所（source checkout）で、起動した cwd（runner の worktree）とは"
        "別の checkout である。skill・参照文書・helper は `tool_root` の版だけを使い、起動した cwd や他の checkout に"
        "ある同名のものは使わない（版の混用を防ぐため。`tool_root` の版を使うことはこの禁止に当たらない）。"
        "helper は `helper` を `uv run --project <tool_root>/tools/devenv --locked python` で実行する。\n"
        "2. prepare には `--pr <pr_url>`、`--runtime <runtimes の、自分の Request.agent_id の値>`、"
        "`--request <起動した cwd の親にある request.json>`、`--records-comments <records_comments>` を渡す。"
        "モデル名は pr-review のランタイムごとの規則に従う"
        "（Codex/DSH headless は省略、Pi は PI_PROVIDER と PI_MODEL を確かめる）。"
        "`--review-commit` は付けない。complete も起動した cwd から呼ぶ。\n"
        "3. prepare が返した reviewed_head が pr_head と違うときは、レビューせずに fail で終える"
        "（PR の HEAD が動いた）。\n"
        "4. してはいけないこと: 別のエージェントの起動・再委譲、追跡ファイルの変更、commit・push、"
        "GitHub への書き込み（コメントを含む）、他のレビュアーの成果を読むこと、"
        "`./scripts/ci/verify.sh`・`./scripts/ci/verify-devenv.sh` の実行。"
        "冒頭の runner の writes=False と「この worktree だけ」の指示は、"
        "レビュー対象（prepare が作る review worktree の追跡ファイルを含む）・どの checkout の追跡ファイル・"
        "他のレビュアーの成果を変えないという意味で、"
        "冒頭に列挙された例外（helper が管理する実行記録・セッション・保存・ロックなど）はこれに当たらない。\n"
        "DSH は隔離 HOME/cache/temp と controller が許可した正式保存先・保存ロックだけが追加の書き込み対象。"
        "DSH prepare は Request 内の parent_pr_data を使い、GitHub API/token/remote fetch を呼ばない。"
        "DSH complete の staged は親が停止確認後に HEAD を再照合して保存・cleanup する。"
        "保存は通常の helper を bash から呼び、sandbox の拒否は設定変更や別経路で再試行しない。\n"
        "5. 最後に complete か fail の JSON を 1 回だけ返す。途中に説明文を出さない。"
        "ランタイムが devin のときだけ、その JSON を 1 つの json コードフェンスで囲む。\n\n"
        "```json\n" + json.dumps(parameters, ensure_ascii=False, indent=2) + "\n```\n"
    )


def _artifact(review_dir: Path, value: object) -> Path | None:
    """レビューディレクトリの中の通常のファイル（symlink でない絶対パス）だけを受け付ける。"""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        return None
    resolved = path.resolve()
    if not resolved.is_relative_to(review_dir.resolve()) or not resolved.is_file():
        return None
    return path


def verify_reviewer(
    result: Result, *, pr: PrRef, head: str, review_dir: Path, expected_runtime: str | None = None
) -> ReviewerEntry:
    """runner の外側の結果と pr-review の payload・成果 2 件を照合する。失敗を指摘 0 件にしない。"""
    attempt = result.attempts[-1] if result.attempts else None
    entry = ReviewerEntry(
        requested_agent=result.requested_agent,
        actual_agent=result.actual_agent,
        runner_state=result.state,
        verdict="rejected",
        identity=attempt.identity.model_dump(mode="json") if attempt is not None else {},
    )

    def reject(stage: RejectedStage, reason: str) -> ReviewerEntry:
        entry.rejected_stage = stage
        entry.reason = reason
        return entry

    if attempt is None or not all(item.stop_confirmed for item in result.attempts):
        return reject("runner", "runner が試行の停止を確認できていない")
    if result.state != "success" or attempt.state != "success" or attempt.exit_code != 0:
        return reject("runner", f"runner の結果が success ではない（{result.state}、終了コード {attempt.exit_code}）")
    if not isinstance(attempt.payload, dict):
        return reject("payload", "payload が JSON object ではない")
    payload = attempt.payload
    cleanup = payload.get("cleanup")
    entry.cleanup = cleanup if isinstance(cleanup, str) else None
    # 失敗した結果でも、pr-review が残したパス（worktree・draft・session など）は報告する
    retained = payload.get("retained_paths")
    if isinstance(retained, dict):
        retained = list(cast(dict[str, object], retained).values())
    if isinstance(retained, list):
        entry.retained_paths = sorted({str(item) for item in cast(list[object], retained) if isinstance(item, str)})
    if payload.get("status") != "success":
        return reject(
            "payload", f"pr-review の結果が success ではない（{payload.get('status')}: {payload.get('reason')}）"
        )
    if attempt.request_path is None:
        return reject("binding", "試行の request.json が無い")
    try:
        request = Request.model_validate_json(attempt.request_path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return reject("binding", "試行の request.json を読めない")
    for key in ("run_id", "request_id", "agent_id", "role"):
        if payload.get(key) != getattr(request, key):
            return reject("binding", f"payload の {key} が依頼と一致しない")
    if payload.get("agent_id") != result.actual_agent:
        return reject("binding", "payload の agent_id が実際の担当と一致しない")
    if payload.get("repository") != pr.repository or payload.get("pr_number") != pr.number:
        return reject("binding", "payload が別の PR を指す")
    if payload.get("pr_head") != head or payload.get("reviewed_head") != head:
        return reject("head", "レビューした HEAD がサイクルの HEAD と一致しない")
    review = _artifact(review_dir, payload.get("review_file"))
    candidates = _artifact(review_dir, payload.get("findings_file"))
    if review is None or candidates is None:
        return reject("artifacts", "成果のパスが、レビューディレクトリの中の通常のファイルではない")
    if (
        review.resolve().parent != review_dir.resolve()
        or candidates.resolve().parent != (review_dir / "findings").resolve()
        or candidates.name != f"{review.stem}-{head[:12]}.json"
    ):
        return reject("artifacts", "成果 2 件の置き場所・名前が pr-review の保存の規則と合わない")
    try:
        document = cast(dict[str, Any], findings.validate_document(json.loads(candidates.read_text(encoding="utf-8"))))
        markdown = review.read_text(encoding="utf-8")
        if document["kind"] != "candidates" or document["reviewed_head"] != head or document["pr"] != pr.key():
            return reject("artifacts", "候補一覧が別の対象を指す")
        if last_history_head(markdown) != head[:12]:
            return reject("artifacts", "レビューファイルの最後の Review history の HEAD がサイクルの HEAD と違う")
        findings.check_pair(markdown, document)
    except (OSError, ValueError) as error:
        return reject("artifacts", f"成果 2 件の検査に失敗した: {str(error)[:200]}")
    if expected_runtime == "dsh" or document["reviewer"]["runtime"] == "dsh":
        reviewer = document["reviewer"]
        metadata = reviewer["provenance"]
        identity = attempt.identity
        if (
            reviewer["runtime"] != "dsh"
            or reviewer != payload.get("reviewer")
            or payload.get("timing") != document["timing"]
        ):
            return reject("artifacts", "DSH の成果と payload の reviewer/timing が一致しない")
        if (
            not identity.model
            or not identity.provider
            or not identity.session_id
            or (reviewer["model"], metadata["provider"], metadata["effort"], metadata["session_id"])
            != (identity.model, identity.provider, identity.effort or "unknown", identity.session_id)
        ):
            return reject("artifacts", "DSH のモデル識別が runner の実測値と一致しない")
        if any(metadata[key] != getattr(request, key) for key in ("run_id", "request_id", "agent_id", "role")):
            return reject("artifacts", "DSH reviewer の実行情報が request と一致しない")
        # 識別行は最初の見出しより前の冒頭だけで数える。本文での引用は冒頭の識別行ではない
        preamble = itertools.takewhile(lambda line: not line.startswith("## "), markdown.splitlines())
        headers = [line for line in preamble if line.startswith("- レビュアー:")]
        if headers != [f"- レビュアー: {identity.model}（ランタイム: dsh）"]:
            return reject("artifacts", "DSH の本文冒頭のモデル識別が実測値と一致しない")
        blocks = re.findall(r"<!-- pr-review:execution -->\s*```json\s*\n(.*?)\n```", markdown, re.DOTALL)
        try:
            execution = json.loads(blocks[0]) if len(blocks) == 1 else None
        except ValueError:
            execution = None
        if (
            not isinstance(execution, dict)
            or cast(dict[str, Any], execution).get("reviewer") != reviewer
            or cast(dict[str, Any], execution).get("timing") != document["timing"]
        ):
            return reject("artifacts", "DSH の本文の実行情報が候補一覧と一致しない")
        if cleanup != "success":
            return reject("artifacts", "DSH review の cleanup が成功していない")
    active = len(cast(list[Any], document["findings"]))
    if payload.get("active_findings") != active:
        return reject("artifacts", "payload の active_findings が候補一覧と一致しない")
    reviewer = payload.get("reviewer")
    timing = payload.get("timing")
    entry.verdict = "accepted"
    entry.reason = None
    entry.reviewer = cast(dict[str, Any], reviewer) if isinstance(reviewer, dict) else None
    entry.timing = cast(dict[str, Any], timing) if isinstance(timing, dict) else None
    entry.review_file = str(review)
    entry.review_sha256 = sha256_file(review)
    entry.findings_file = str(candidates)
    entry.findings_sha256 = sha256_file(candidates)
    entry.active_findings = active
    if entry.cleanup != "success":
        entry.warnings.append(f"pr-review の後片付けが {entry.cleanup}。残ったパスを確かめる")
    return entry


# --- 資源 ---------------------------------------------------------------------------------------------------------


def _output_dir(resource: Resource) -> Path:
    return Path(resource.path) / f"cycle-{resource.cycle}-review"


def _read_batch(path: Path) -> BatchResult | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return BatchResult.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return None


def _stopped(batch: BatchResult) -> bool:
    return all(attempt.stop_confirmed for result in batch.results for attempt in result.attempts)


def _registered_worktrees(root: Path) -> list[Path]:
    listing = git_output(root, "worktree", "list", "--porcelain", "-z")
    return [Path(field.removeprefix("worktree ")) for field in listing.split("\x00") if field.startswith("worktree ")]


def cleanup_output(
    tool_root: Path, resource: Resource, batch: BatchResult | None, *, force: bool = False, reason: str | None = None
) -> list[str]:
    """記録した出力ディレクトリの資源だけを回収する。残したパスを返し、resource.state を更新する。

    停止を確認できない（force でない）ときは何も消さない。worktree は対象の checkout から外す
    （force のときだけ --force）。秘密が残りうる sensitive_paths は消さずに残して報告する。
    """
    parent = Path(resource.path)
    if not parent.name.startswith(TMP_PREFIX) or parent.is_symlink():
        resource.state = "retained"
        resource.reason = "記録した出力ディレクトリの名前・種類が想定と違うので消さない"
        return [str(parent)]
    try:
        parent.lstat()
    except FileNotFoundError:
        try:
            registrations = [
                str(path)
                for path in _registered_worktrees(tool_root)
                if path.absolute().is_relative_to(parent.absolute())
            ]
        except LoopError:
            resource.state = "retained"
            resource.reason = "Git登録を照合できないので回収完了としない"
            return [str(parent)]
        if registrations:
            resource.state = "retained"
            resource.reason = "親ディレクトリは不在だがGit登録が残る。移動・所有・停止の確認が必要"
            return registrations
        resource.state = "removed"
        resource.reason = reason
        return []
    except OSError:
        resource.state = "retained"
        resource.reason = "親ディレクトリを調べられないので回収完了としない"
        return [str(parent)]
    if not force and (batch is None or not _stopped(batch)):
        return [str(parent)]
    retained: list[str] = []
    owned = parent.resolve()
    for worktree in _registered_worktrees(tool_root):
        if not worktree.resolve().is_relative_to(owned):
            continue
        try:
            git_output(tool_root, "worktree", "remove", *(["--force"] if force else []), str(worktree))
        except LoopError:
            retained.append(str(worktree))
    if batch is not None:
        retained += [
            str(path) for item in batch.results for attempt in item.attempts for path in attempt.sensitive_paths
        ]
    else:
        # runner の結果が無いと、秘密が残りうるファイル（receipt など）を判別できないので、ディレクトリごと残す
        retained.append(str(parent))
        reason = reason or "runner の結果が無く、秘密が残りうるファイルを判別できない"
    if retained:
        resource.state = "retained"
        resource.reason = reason or "外せない worktree か、秘密が残りうるファイルがある"
        return retained
    shutil.rmtree(parent)
    resource.state = "removed"
    resource.reason = reason
    return []


def audit_resources(run: RunRecord) -> list[str]:
    """停止を確認できない資源の完全パス。停止を確認できた資源は、ここで回収し直す。"""
    unconfirmed: list[str] = []
    for resource in run.resources:
        if resource.state == "removed":
            continue
        if not Path(resource.path).exists():
            if cleanup_output(Path(run.tool_root), resource, None):
                unconfirmed.append(resource.path)
            continue
        if resource.state != "created":
            continue
        batch = _read_batch(_output_dir(resource) / "result.json")
        if batch is None or not _stopped(batch):
            unconfirmed.append(resource.path)
            continue
        cleanup_output(Path(run.tool_root), resource, batch)
    return unconfirmed


# --- サブコマンド -------------------------------------------------------------------------------------------------


def start(
    argument: str,
    *,
    controller_model: str,
    controller_runtime: str,
    max_cycles: int = 3,
    hold_action: str | None = None,
    assign: Sequence[str] = (),
    template: Path = DEFAULT_TEMPLATE,
    config: Path | None = None,
    fresh_review: bool = False,
    github: GitHub | None = None,
    parent_root: Path | None = None,
    tool_root: Path = ROOT,
) -> dict[str, Any]:
    if controller_runtime not in RUNTIMES or not controller_model.strip():
        raise LoopError(
            2, "arguments", f"--controller-runtime は {', '.join(RUNTIMES)}、--controller-model は空にしない"
        )
    if type(max_cycles) is not int or not 1 <= max_cycles <= MAX_CYCLES_LIMIT:
        raise LoopError(2, "arguments", f"--max-cycles は 1〜{MAX_CYCLES_LIMIT}")
    if hold_action not in (None, "skip", "fix"):
        raise LoopError(2, "arguments", "--hold-action は skip か fix")
    requested = parse_assign(assign)
    github = github or GitHub()
    root, repository, number = resolve_pr(argument, parent_root)
    assignments = load_assignments(template, config, requested)
    pull = github.pull(repository, number)
    if not pull.open:
        raise LoopError(
            4, "pr_not_open", f"{repository}#{number} は open ではない（state={pull.state}、merged={pull.merged}）"
        )
    review_dir = review_dir_for(root, pull)
    tool_commit = git_output(tool_root, "rev-parse", "HEAD")
    warnings: list[str] = []
    if git_output(tool_root, "status", "--porcelain", "--untracked-files=no"):
        warnings.append(
            f"{tool_root} に未 commit の変更がある。レビュアーは commit {tool_commit[:12]} の worktree で動くが、"
            f"スキルと helper は {tool_root} の作業ツリーのもの（未 commit の変更を含む）を読む"
        )
    run_id = uuid.uuid4().hex
    run_dir = loop_dir(review_dir) / run_id
    pr = PrRef(repository=pull.repository, number=pull.number, url=pull.url)
    acquire_lock(review_dir, LockRecord(run_id=run_id, pr=pr, run_dir=str(run_dir), created_at=now()))
    try:
        run_dir.mkdir()
        run = RunRecord(
            run_id=run_id,
            pr=pr,
            review_dir=str(review_dir),
            tool_root=str(tool_root),
            tool_commit=tool_commit,
            controller=Controller(model=controller_model, runtime=controller_runtime),
            options=Options(
                max_cycles=max_cycles,
                hold_action="skip" if hold_action is None else hold_action,
                assign=requested,
                template=str(template),
                config=str(config) if config is not None else None,
                fresh_review=fresh_review,
            ),
            assignments=assignments,
            started_at=now(),
            state="running",
        )
        save_run(run_dir, run)
        # run を始めた後だけ変える（失敗する start や、別の run が使っている PR では変えない）
        pane = rename_pane(pr.number)
        run.herdr_pane = pane
        save_run(run_dir, run)
    except BaseException:
        release_lock(review_dir, run_id)
        raise
    if pane.state == "failed":
        warnings.append(
            f"現在の Herdr ペイン（{pane.pane_id}）の名前を {pane.label} に変えられなかった: {pane.reason}。"
            "run は続ける"
        )
    return {
        "status": "ok",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "review_dir": str(review_dir),
        "pr": pr.model_dump(mode="json"),
        "head": pull.head_sha,
        "assignments": assignments,
        "herdr_pane": pane.model_dump(mode="json"),
        "warnings": warnings,
        "next": command("cycle-begin", "--run", str(run_dir)),
    }


def cycle_begin(run_dir: Path, *, github: GitHub | None = None) -> dict[str, Any]:
    run = load_run(run_dir)
    require_owner(run)
    if run.state != "running" or run.result is not None:
        raise LoopError(3, "state", f"run {run.run_id} は {run.state} で終わっている。新しいサイクルを始めない")
    existing = load_cycles(run_dir)
    if existing and existing[-1].state != "ended":
        raise LoopError(3, "state", f"サイクル {existing[-1].cycle} が {existing[-1].state} のまま")
    number = len(existing) + 1
    if number > run.options.max_cycles:
        raise LoopError(3, "state", f"サイクルの上限 {run.options.max_cycles} に達している")
    github = github or GitHub()
    pull = github.pull(run.pr.repository, run.pr.number)
    if not pull.open:
        raise LoopError(4, "pr_not_open", f"PR は open ではない（state={pull.state}、merged={pull.merged}）")
    notes: list[str] = []
    if existing and existing[-1].planning is not None and existing[-1].planning.commit not in (None, pull.head_sha):
        notes.append(f"PR の HEAD が前のサイクルで push した commit {existing[-1].planning.commit} から動いている")
    reused = [] if run.options.fresh_review else find_reusable(Path(run.review_dir), run.pr, pull.head_sha)
    prior = _read_records(github, run)[1]
    cycle = CycleRecord(
        run_id=run.run_id,
        cycle=number,
        head=pull.head_sha,
        started_at=now(),
        state="planning" if reused else "reviewing",
        prior_round=max((cast(int, entry["round"]) for entry in prior["rounds"]), default=0),
        review=ReviewPart(mode="reuse" if reused else "run", reused=reused),
    )
    cycle_dir(run_dir, number).mkdir(exist_ok=True)
    save_cycle(run_dir, cycle)
    output: dict[str, Any] = {
        "status": "ok",
        "cycle": number,
        "head": pull.head_sha,
        "review": {"mode": cycle.review.mode, "reused": [item.model_dump(mode="json") for item in reused]},
        "notes": notes,
    }
    if reused:
        output["planning"] = planning_guidance(run)
        output["next"] = command("cycle-end", "--run", str(run_dir))
    else:
        output["next"] = command("review", "--run", str(run_dir))
    return output


def _read_records(github: GitHub, run: RunRecord) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """PR の全コメントと、判断の記録の現在の状態。壊れた記録は終了コード 4。"""
    try:
        comments = cast(
            list[dict[str, Any]], records.comments_from_api(github.comments(run.pr.repository, run.pr.number))
        )
        state = cast(dict[str, Any], records.read_state(comments, expected_pr=run.pr.key()))
    except records.RecordError as error:
        raise LoopError(4, "record_inconsistent", "; ".join(error.problems)[:600]) from error
    return comments, state


def _decision_counts(record: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {"state": {}, "change": {}}
    for item in cast(list[dict[str, Any]], record["decisions"]):
        for key in ("state", "change"):
            value = cast(str, item[key])
            counts[key][value] = counts[key].get(value, 0) + 1
    return counts


def _read_finalize(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or cast(dict[str, Any], value).get("outcome") not in ("no_fix_needed", "stopped")
        or not isinstance(cast(dict[str, Any], value).get("merged"), bool)
        or not isinstance(cast(dict[str, Any], value).get("issues"), list)
    ):
        raise LoopError(2, "arguments", f"{path} は finalize.py の結果（outcome・merged・issues）ではない")
    return cast(dict[str, Any], value)


def _read_role_run(path: Path) -> RoleRun:
    roles = read_json(path / "roles.json")
    try:
        # roles.json は JSON なので、strict な runner のモデルも JSON として検証する（パスは文字列で入っている）
        batch = BatchResult.model_validate_json(json.dumps(cast(dict[str, Any], roles)["batch"]))
    except (KeyError, TypeError, ValidationError) as error:
        raise LoopError(2, "arguments", f"{path}/roles.json は run_roles.py の出力ではない") from error
    results = [
        {
            "role": item.role,
            "requested_agent": item.requested_agent,
            "actual_agent": item.actual_agent,
            "state": item.state,
            "identity": item.attempts[-1].identity.model_dump(mode="json") if item.attempts else None,
        }
        for item in batch.results
    ]
    return RoleRun(dir=str(path.resolve()), state=batch.state, assignments=batch.assignments, results=results)


def _hash_if_file(path: Path) -> tuple[str | None, str | None]:
    if path.is_file() and not path.is_symlink():
        return str(path), sha256_file(path)
    return None, None


def cycle_end(
    run_dir: Path,
    *,
    finalize_result: Path | None = None,
    round_dir: Path | None = None,
    role_runs: Sequence[Path] = (),
    record_file: Path | None = None,
    github: GitHub | None = None,
) -> dict[str, Any]:
    run = load_run(run_dir)
    require_owner(run)
    existing = load_cycles(run_dir)
    if not existing or existing[-1].state != "planning" or run.result is not None:
        state = existing[-1].state if existing else "未開始"
        raise LoopError(3, "state", f"計画の段階のサイクルが無い（最後のサイクル: {state}）")
    cycle = existing[-1]
    github = github or GitHub()
    pr_key = run.pr.key()
    comments, state = _read_records(github, run)
    # このサイクルの記録は、cycle-begin の後に投稿された（round が増えた）ものだけ。
    # 前の run の同じ HEAD の記録を拾わない
    matching = [
        entry
        for entry in cast(list[dict[str, Any]], state["rounds"])
        if entry["reviewed_head"] == cycle.head and entry["round"] > cycle.prior_round
    ]
    comment_id: int | None = None
    if matching:
        latest = max(matching, key=lambda entry: (entry["round"], entry["comment_id"]))
        comment_id = cast(int, latest["comment_id"])
        body = next(cast(str, comment["body"]) for comment in comments if comment["id"] == comment_id)
        record = cast(dict[str, Any], records.extract_record(body))
        prior = records.read_state(comments, expected_pr=pr_key, exclude_comment_ids=[comment_id])
    elif record_file is not None:
        try:
            record = cast(dict[str, Any], records.validate_record(read_json(record_file)))
        except records.RecordError as error:
            raise LoopError(4, "record_inconsistent", "; ".join(error.problems)[:600]) from error
        if record["reviewed_head"] != cycle.head or record["pr"] != pr_key or record["round"] <= cycle.prior_round:
            raise LoopError(
                4, "record_inconsistent", "--record-file の記録は、このサイクルの PR・HEAD・round のものではない"
            )
        prior = state
    else:
        raise LoopError(
            4,
            "record_missing",
            f"サイクル {cycle.cycle} の HEAD {cycle.head[:12]} の判断の記録が PR に無い。"
            "計画を続け、状況コメントを投稿してから cycle-end を呼ぶ",
            next_command=command("cycle-end", "--run", str(run_dir)),
        )
    try:
        records.check_against(record, prior)
    except records.RecordError as error:
        raise LoopError(4, "record_inconsistent", "; ".join(error.problems)[:600]) from error
    outcome = cast(str, record["outcome"])
    if outcome not in OUTCOMES:
        raise LoopError(
            4, "record_inconsistent", f"新しい loop は結果 {outcome} を受け付けない（{', '.join(OUTCOMES)}）"
        )

    pull = github.pull(run.pr.repository, run.pr.number)
    final_outcome = outcome
    stop_reason = cast(str | None, record.get("stop_reason"))
    merged = False
    finalize: FinalizePart | None = None
    if outcome == "fixes_pushed":
        commit = cast(str, record["commit"])
        if commit == cycle.head or not pull.open or pull.head_sha != commit:
            raise LoopError(
                4,
                "pushed_head_mismatch",
                f"記録の commit {commit[:12]} と PR の HEAD {pull.head_sha[:12]}（state={pull.state}）が一致しない",
            )
    elif outcome == "no_fix_needed":
        if finalize_result is None:
            raise LoopError(
                2, "arguments", "no_fix_needed の記録には --finalize-result（finalize.py の結果ファイル）が要る"
            )
        result = _read_finalize(finalize_result)
        finalize = FinalizePart(
            result_file=str(finalize_result.resolve()),
            result=result,
            round_dir=str(round_dir.resolve()) if round_dir is not None else None,
        )
        merged = cast(bool, result["merged"])
        if merged and (not pull.merged or pull.head_sha != cycle.head):
            raise LoopError(
                4,
                "merge_mismatch",
                f"finalize はマージ済みと返したが、PR は merged={pull.merged}・HEAD {pull.head_sha[:12]}",
            )
        if result["outcome"] == "no_fix_needed":
            if not merged:
                raise LoopError(4, "merge_mismatch", "finalize の結果が no_fix_needed なのにマージされていない")
        else:
            final_outcome = "stopped"
            stop_reason = cast(str | None, result.get("stop_reason")) or "external: finalize が停止理由を返さなかった"

    planning_file, planning_sha = _hash_if_file(Path(run.review_dir) / "verified" / f"{cycle.head[:12]}.planning.json")
    verified_file, verified_sha = _hash_if_file(Path(run.review_dir) / "verified" / f"{cycle.head[:12]}.json")
    cycle.planning = PlanningPart(
        round=cast(int, record["round"]),
        comment_id=comment_id,
        posted=comment_id is not None,
        outcome=final_outcome,
        commit=cast(str | None, record.get("commit")),
        stop_reason=stop_reason if final_outcome == "stopped" else None,
        merged=merged,
        decisions=_decision_counts(record),
        planning_file=planning_file,
        planning_sha256=planning_sha,
        verified_file=verified_file,
        verified_sha256=verified_sha,
        finalize=finalize,
        role_runs=[_read_role_run(path) for path in role_runs],
        ended_at=now(),
    )
    cycle.state = "ended"
    cycle.ended_at = cycle.planning.ended_at
    if final_outcome == "fixes_pushed" and cycle.cycle < run.options.max_cycles:
        next_command = command("cycle-begin", "--run", str(run_dir))
    else:
        run.result = RunResult(
            outcome=final_outcome,
            ended_by="cycle_limit" if final_outcome == "fixes_pushed" else "outcome",
            stop_reason=stop_reason if final_outcome == "stopped" else None,
            merged=merged,
            cycles=cycle.cycle,
        )
        next_command = command("finish", "--run", str(run_dir))
    save_cycle(run_dir, cycle)
    save_run(run_dir, run)
    return {
        "status": "ok",
        "cycle": cycle.cycle,
        "round": record["round"],
        "comment_id": comment_id,
        "outcome": final_outcome,
        "merged": merged,
        "stop_reason": cycle.planning.stop_reason,
        "next": next_command,
    }


def _reviewer_summary(entry: ReviewerEntry) -> dict[str, Any]:
    return {
        "agent": entry.actual_agent,
        "requested": entry.requested_agent,
        "verdict": entry.verdict,
        "reason": entry.reason,
        "rejected_stage": entry.rejected_stage,
        "model": entry.identity.get("model"),
        "active_findings": entry.active_findings,
        "review_file": entry.review_file,
        "warnings": entry.warnings,
    }


def _failed_review_reason(entries: Sequence[ReviewerEntry]) -> str:
    reasons = "; ".join(f"{entry.actual_agent}: {entry.reason}" for entry in entries) or "担当が無い"
    # 書きかけの値（<...>）と取り違えないよう、理由の中の山括弧を全角にする
    reasons = reasons.replace("<", "＜").replace(">", "＞")[:400]
    return f"external: 全レビュアーの結果を受け付けられなかった（{reasons}）"


def _all_failed(run_dir: Path, entries: Sequence[ReviewerEntry]) -> LoopError:
    reason = _failed_review_reason(entries)
    return LoopError(
        6,
        "review",
        reason.removeprefix("external: ") + "。指摘 0 件としては扱わない。"
        "停止の理由が外部ではなく環境・権限なら、--abort の分類を書き換える",
        next_command=command("finish", "--run", str(run_dir), f"--abort={reason}"),
    )


def keep_rejected_request(result: Result, entry: ReviewerEntry, directory: Path) -> None:
    """却下した試行に実際に渡した指示（runner が書いた request.json）を ``<request_id>.json`` に写す。

    runner の request.json は環境の値を含まない（tools/agent_runner/interfaces.rst）。通常のファイル（symlink でない）
    だけを写し、秘密が残りうる stdout・stderr は写さない。
    """
    attempt = result.attempts[-1] if result.attempts else None
    # 停止を確かめられない試行のファイルは、生きた書き手が差し替えうるので読まない（runner と同じ方針）
    if entry.verdict != "rejected" or attempt is None or attempt.request_path is None or not attempt.stop_confirmed:
        return
    source = attempt.request_path
    if source.is_symlink() or not source.is_file() or Path(result.request_id).name != result.request_id:
        return
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{result.request_id}.json"
    shutil.copyfile(source, destination)
    entry.request_file = str(destination)
    entry.request_sha256 = sha256_file(destination)


def _review_batch(
    run_dir: Path,
    run: RunRecord,
    cycle: CycleRecord,
    part: ReviewPart | RerunPart,
    *,
    github: GitHub | None,
    adapter_loader: Callable[[str], Adapter],
    tmp_root: Path | None,
) -> tuple[BatchResult, list[ReviewerEntry], list[str]]:
    """runner の 1 回の実行（起動 → 親の確定・後処理 → 検査 → 回収）。part は 1 回目か再実行の記録。

    途中で止まっても part の記録から同じ段を再開し、レビュアーを起動し直さない。戻り値は runner の結果、
    検査した記録、回収できずに残ったパス。
    """
    rerun = part if isinstance(part, RerunPart) else None
    prefix = "rerun-" if rerun is not None else ""
    review_dir = Path(run.review_dir)
    work = cycle_dir(run_dir, cycle.cycle)
    if part.output_dir is not None:
        output_dir = Path(part.output_dir)
        batch = _read_batch(output_dir / "result.json")
        if batch is None:
            raise LoopError(
                3,
                "interrupted_review",
                f"前回のレビューの出力 {output_dir} に結果が無い（中断の跡）。レビュアーを起動し直さない。"
                "runner とレビュアーのプロセスが残っていないことを確かめてから recover で回収する",
                next_command=command("recover", "--run", str(run_dir)),
                requires_input=CONFIRM_STOPPED,
            )
    else:
        github = github or GitHub()
        pull = github.pull(run.pr.repository, run.pr.number)
        if not pull.open:
            raise LoopError(4, "pr_not_open", f"PR は open ではない（state={pull.state}、merged={pull.merged}）")
        if pull.head_sha != cycle.head:
            raise LoopError(
                4,
                "head_moved",
                f"cycle-begin の後に PR の HEAD が {cycle.head[:12]} から {pull.head_sha[:12]} に動いた。"
                "この run を止め、新しい run で始め直す",
                next_command=command(
                    "finish", "--run", str(run_dir), "--abort=external: レビュー前に PR の HEAD が動いた"
                ),
            )
        config = review_config_without_github_tokens(load_review_config(run))
        if rerun is not None:
            # 受け付けなかった実担当だけを、同じ role の期限で起動し直す。fallback や別モデルへは切り替えない
            config.workflows["review"].assignments["inspect"] = Assignment(agents=list(rerun.agents), fallback=[])
        inspect = config.workflows["review"].assignments["inspect"]
        runtimes = {name: config.agents[name].runtime for name in [*inspect.agents, *inspect.fallback]}
        write_paths = review_write_paths(run, runtimes)
        work.mkdir(exist_ok=True)
        if rerun is None:
            comments_file = work / "comments.json"
            write_json(comments_file, cast(Sequence[Any], github.comments(run.pr.repository, run.pr.number)))
        else:
            # 同じサイクルの計画の前なので、判断の記録は 1 回目と同じ。同じ入力を渡す
            comments_file = Path(cast(str, cycle.review.comments_file))
        parent_pr_data = None
        parent_input = work / f"{prefix}parent-pr-data"
        if "dsh" in runtimes.values():
            try:
                # 既存のディレクトリは拒否する。作りかけの領域は prepare_parent が自分で片付ける
                parent_pr_data = review_entry.prepare_parent(
                    argparse.Namespace(
                        pr=run.pr.url,
                        output_dir=str(parent_input),
                        source_root=None,
                        review_commit=None,
                        records_comments=str(comments_file),
                        parent_root=str(Path(run.review_dir).parents[3]),
                        api_runner=github.runner,
                    )
                )
                snapshot = review_entry.parent_data.load(review_entry.workspace, parent_pr_data, run.pr.url)
                if snapshot["target"]["head_sha"] != cycle.head:
                    raise ValueError("PR head changed during parent preparation")
            except review_entry.FAILURES as error:
                if parent_pr_data is not None:
                    discard_parent_input(parent_input)
                raise LoopError(5, "parent_preparation", str(error)) from error
        parent = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=tmp_root))
        resource = Resource(path=str(parent), cycle=cycle.cycle, state="created")
        output_dir = _output_dir(resource)
        run.resources.append(resource)
        save_run(run_dir, run)
        part.output_dir = str(output_dir)
        cycle.review.comments_file = str(comments_file)
        part.started_at = now()
        save_cycle(run_dir, cycle)
        prompt = reviewer_prompt(
            tool_root=Path(run.tool_root),
            pr=run.pr,
            head=cycle.head,
            comments_file=comments_file,
            runtimes=runtimes,
            parent_pr_data=parent_pr_data,
        )
        try:
            batch = run_workflow(
                config,
                "review",
                prompt,
                Path(run.tool_root),
                revision=run.tool_commit,
                concurrency=len(inspect.agents),
                output_dir=output_dir,
                adapter_loader=adapter_loader,
                sandbox_write_paths=write_paths,
                write_exceptions=review_write_exceptions(run),
            )
        except (ConfigError, RunnerError) as error:
            if output_dir.exists():
                raise LoopError(
                    5,
                    "runner",
                    f"runner でレビュアーを起動できない（{type(error).__name__}）。出力 {output_dir} が残っている",
                    next_command=command("status", "--run", str(run_dir)),
                ) from error
            # runner が何も作らずに拒否した（設定・入れ子の起動など）。何も起動していないので、やり直せる状態に戻す
            parent.rmdir()
            if parent_pr_data is not None:
                discard_parent_input(parent_input)
            resource.state = "removed"
            resource.reason = f"runner が起動の前に拒否した（{type(error).__name__}）"
            part.output_dir = None
            part.started_at = None
            save_run(run_dir, run)
            save_cycle(run_dir, cycle)
            raise LoopError(
                5,
                "runner",
                f"runner でレビュアーを起動できない（{type(error).__name__}）。何も起動していない。"
                "原因（設定・入れ子の起動など）を直してから review をやり直す",
                next_command=command("review", "--run", str(run_dir)),
            ) from error
        # 親の確定より前に runner の結果を残す。途中で止まっても、再開が同じ親の確定から続ける
        write_json(output_dir / "result.json", batch)
    config = load_review_config(run)
    if part.after_run is None:
        if part.after_run_started:
            raise LoopError(7, "after_run", "前回の後処理の停止・結果が未確認。再実行せず recover で回収する")
        # 新規・再開とも、停止済みの staged だけを親が同じ検査で正式成果に確定する（確定済みは触らない）
        review_entry.finalize_parent_batch(batch)
        write_json(output_dir / "result.json", batch)
        part.after_run_started = True
        save_cycle(run_dir, cycle)
        reports = run_after_run(
            config.workflows["review"].after_run,
            result_path=output_dir / "result.json",
            cwd=Path(run.tool_root),
            cancel=threading.Event(),
        )
        # Keep evidence outside the disposable output before cleanup removes it.
        for report in reports:
            for key in ("stdout_path", "stderr_path"):
                source = Path(str(report[key]))
                destination = work / f"{prefix}{source.name}"
                if source.is_file():
                    shutil.copyfile(source, destination)
                    report[key] = str(destination)
        part.after_run = cast(list[dict[str, Any]], reports)
        save_cycle(run_dir, cycle)
    if any(report.get("stop_confirmed") is not True for report in part.after_run):
        raise LoopError(7, "after_run", "後処理の停止を確認できない。出力を保持して recover で回収する")
    if any(report.get("state") != "success" for report in part.after_run):
        raise LoopError(5, "after_run", "レビュー後処理が失敗した。結果とログを保持し、再実行しない")
    # 後処理は result.json を更新しうる。新規・再開とも、保存された結果をレビュアーの検査に使う
    saved = _read_batch(output_dir / "result.json")
    if saved is None:
        raise LoopError(5, "after_run", f"後処理の後の結果 {output_dir / 'result.json'} を読めない。再実行しない")
    batch = saved
    entries = [
        verify_reviewer(
            item,
            pr=run.pr,
            head=cycle.head,
            review_dir=review_dir,
            expected_runtime=config.agents[item.actual_agent].runtime if item.actual_agent in config.agents else None,
        )
        for item in batch.results
    ]
    # 回収で出力が消える前に、却下した試行に渡した指示を残す
    for item, entry in zip(batch.results, entries, strict=True):
        keep_rejected_request(item, entry, work / "requests")
    result_copy = work / f"{prefix or 'review-'}result.json"
    write_json(result_copy, batch)
    resource = next((item for item in run.resources if _output_dir(item) == output_dir), None)
    retained = cleanup_output(Path(run.tool_root), resource, batch) if resource is not None else [str(output_dir)]
    part.batch = BatchInfo(
        run_id=batch.run_id,
        state=batch.state,
        assignments=batch.assignments,
        result_file=str(result_copy),
        outside_changes=batch.outside_changes,
    )
    part.ended_at = now()
    part.seconds = batch.elapsed_seconds
    part.cleanup = CleanupInfo(
        state="removed" if resource is not None and resource.state == "removed" else "retained", retained=retained
    )
    return batch, entries, retained


def review(
    run_dir: Path,
    *,
    github: GitHub | None = None,
    adapter_loader: Callable[[str], Adapter] = load_adapter,
    tmp_root: Path | None = None,
) -> dict[str, Any]:
    run = load_run(run_dir)
    require_owner(run)
    existing = load_cycles(run_dir)
    if not existing or existing[-1].state != "reviewing" or run.result is not None:
        raise LoopError(3, "state", "レビューの段階のサイクルが無い（reuse のサイクルではレビュアーを起動しない）")
    cycle = existing[-1]
    if cycle.review.batch is None:
        batch, entries, retained = _review_batch(
            run_dir, run, cycle, cycle.review, github=github, adapter_loader=adapter_loader, tmp_root=tmp_root
        )
        cycle.review.reviewers = entries
        again = [entry for entry in entries if entry.verdict == "rejected" and entry.rejected_stage != "runner"]
        # runner は成功したのに受け付けなかったレビュアー（レビューの未実施・成果の欠落・HEAD の不一致など）は、
        # 同じサイクルで 1 回だけ起動し直す。runner の失敗（期限切れ・認証待ち・起動失敗）は再実行しない。
        # 再実行は依存する仕事なので、別の担当でも停止を確かめられない試行があれば始めない（recover の経路に残す）
        if again and _stopped(batch):
            cycle.review.rerun = RerunPart(agents=[entry.actual_agent for entry in again], replaced=again)
    elif cycle.review.rerun is not None and cycle.review.rerun.batch is None:
        batch, entries, retained = _review_batch(
            run_dir, run, cycle, cycle.review.rerun, github=github, adapter_loader=adapter_loader, tmp_root=tmp_root
        )
        rerun_entries = {entry.actual_agent: entry for entry in entries}
        cycle.review.reviewers = [rerun_entries.get(entry.actual_agent, entry) for entry in cycle.review.reviewers]
    else:
        # 前の review（と再実行）が全レビュアーの失敗で終わった。レビュアーを起動し直さず、同じ結果を返す
        raise _all_failed(run_dir, cycle.review.reviewers)
    entries = cycle.review.reviewers
    accepted = [entry for entry in entries if entry.verdict == "accepted"]
    pending = cycle.review.rerun is not None and cycle.review.rerun.batch is None
    if accepted and not pending:
        cycle.state = "planning"
    save_cycle(run_dir, cycle)
    save_run(run_dir, run)
    if not accepted and not pending:
        raise _all_failed(run_dir, entries)
    reviewer_retained = [path for entry in entries for path in entry.retained_paths]
    output: dict[str, Any] = {
        "status": "ok",
        "cycle": cycle.cycle,
        "head": cycle.head,
        "accepted": [_reviewer_summary(entry) for entry in accepted],
        "rejected": [_reviewer_summary(entry) for entry in entries if entry.verdict == "rejected"],
        "outside_changes": batch.outside_changes,
        "retained": retained + reviewer_retained,
    }
    if cycle.review.rerun is not None:
        output["rerun"] = {"agents": list(cycle.review.rerun.agents)}
    if pending:
        output["next"] = command("review", "--run", str(run_dir))
        return output
    output["planning"] = planning_guidance(run)
    output["next"] = command("cycle-end", "--run", str(run_dir))
    return output


def recover(run_dir: Path, *, operator_confirmed_stopped: str | None = None) -> dict[str, Any]:
    """中断・保持した run の、記録した資源だけを回収してロックを解放する。"""
    run = load_run(run_dir)
    require_owner(run)
    if run.state in ("ended", "abandoned"):
        raise LoopError(3, "state", f"run {run.run_id} は {run.state}")
    confirmation = (
        require_fact("--operator-confirmed-stopped", operator_confirmed_stopped)
        if operator_confirmed_stopped is not None
        else None
    )
    unconfirmed = audit_resources(run)
    retained: list[str] = []
    if unconfirmed:
        if confirmation is None:
            save_run(run_dir, run)
            raise LoopError(
                7,
                "resources",
                "停止を確認できない資源がある: " + ", ".join(unconfirmed) + "。runner とレビュアーのプロセスが"
                "残っていないことを確かめ、その事実を --operator-confirmed-stopped で渡す",
                next_command=command("recover", "--run", str(run_dir)),
                requires_input=CONFIRM_STOPPED,
            )
        for resource in run.resources:
            if resource.path in unconfirmed:
                batch = _read_batch(_output_dir(resource) / "result.json")
                retained += cleanup_output(
                    Path(run.tool_root), resource, batch, force=True, reason=f"operator: {confirmation}"
                )
        run.notes.append(f"{now()} recover: {confirmation}")
    run.state = "ended" if run.result is not None else "abandoned"
    run.ended_at = now()
    save_run(run_dir, run)
    released = release_lock(Path(run.review_dir), run.run_id)
    return {
        "status": "ok",
        "run_id": run.run_id,
        "state": run.state,
        "result": run.result.model_dump(mode="json") if run.result else None,
        "lock_released": released,
        "retained": sorted({*retained, *(resource.path for resource in run.resources if resource.state != "removed")}),
    }


def _cycle_summary(cycle: CycleRecord) -> dict[str, Any]:
    return {
        "cycle": cycle.cycle,
        "head": cycle.head,
        "state": cycle.state,
        "review_mode": cycle.review.mode,
        "reviewers": [
            {"agent": item.actual_agent, "verdict": item.verdict, "reason": item.reason}
            for item in cycle.review.reviewers
        ],
        "outcome": cycle.planning.outcome if cycle.planning else None,
        "round": cycle.planning.round if cycle.planning else None,
        "commit": cycle.planning.commit if cycle.planning else None,
    }


def finish(run_dir: Path, *, abort: str | None = None) -> dict[str, Any]:
    run = load_run(run_dir)
    require_owner(run)
    if run.state not in ("running", "held"):
        raise LoopError(3, "state", f"run {run.run_id} は {run.state}")
    existing = load_cycles(run_dir)
    if abort is not None:
        if STOP_REASON.fullmatch(abort) is None or _PLACEHOLDER.search(abort):
            raise LoopError(2, "arguments", "--abort は「permission|environment|external: 具体的な障害」の形で書く")
        if run.result is not None:
            raise LoopError(3, "state", "run は既に結果が決まっている。--abort なしで finish する")
        run.result = RunResult(
            outcome="stopped", ended_by="abort", stop_reason=abort, merged=False, cycles=len(existing)
        )
    elif run.result is None:
        raise LoopError(3, "state", "run はまだ終わっていない。サイクルの途中で止めるときは --abort を付ける")
    unconfirmed = audit_resources(run)
    if unconfirmed:
        run.state = "held"
        save_run(run_dir, run)
        raise LoopError(
            7,
            "resources",
            "停止を確認できない資源があるので、ロックを保持する: " + ", ".join(unconfirmed),
            next_command=command("recover", "--run", str(run_dir)),
            requires_input=CONFIRM_STOPPED,
        )
    run.state = "ended"
    run.ended_at = now()
    save_run(run_dir, run)
    released = release_lock(Path(run.review_dir), run.run_id)
    return {
        "status": "ok",
        "run_id": run.run_id,
        "pr": run.pr.model_dump(mode="json"),
        "result": run.result.model_dump(mode="json"),
        "cycles": [_cycle_summary(cycle) for cycle in existing],
        "lock_released": released,
        "retained": [resource.path for resource in run.resources if resource.state == "retained"],
    }


def _next_step(run: RunRecord, existing: Sequence[CycleRecord], run_dir: Path) -> list[str] | None:
    if run.state in ("ended", "abandoned"):
        return None
    if run.state == "held":
        return command("recover", "--run", str(run_dir))
    if run.result is not None:
        return command("finish", "--run", str(run_dir))
    if not existing or existing[-1].state == "ended":
        return command("cycle-begin", "--run", str(run_dir))
    if existing[-1].state == "reviewing":
        rerun = existing[-1].review.rerun
        if existing[-1].review.batch is not None and (rerun is None or rerun.batch is not None):
            # 全レビュアーが失敗したサイクル（再実行の後を含む）。review をやり直さずに止める
            return command(
                "finish", "--run", str(run_dir), f"--abort={_failed_review_reason(existing[-1].review.reviewers)}"
            )
        return command("review", "--run", str(run_dir))
    return command("cycle-end", "--run", str(run_dir))


def status(
    run_dir: Path | None = None,
    *,
    argument: str | None = None,
    github: GitHub | None = None,
    parent_root: Path | None = None,
) -> dict[str, Any]:
    """読み取りだけ。ロックの持ち主、run の状態、次の操作を返す。"""
    if run_dir is None:
        if argument is None:
            raise LoopError(2, "arguments", "status には PR か --run を指定する")
        root, repository, number = resolve_pr(argument, parent_root)
        pull = (github or GitHub()).pull(repository, number)
        review_dir = review_dir_for(root, pull)
        lock = read_lock(review_dir)
        if lock is None:
            return {"status": "ok", "review_dir": str(review_dir), "lock": None, "next": None}
        run_dir = Path(lock.run_dir)
    run = load_run(run_dir)
    lock = read_lock(Path(run.review_dir))
    existing = load_cycles(run_dir)
    return {
        "status": "ok",
        "run_id": run.run_id,
        "run_dir": str(run_dir),
        "pr": run.pr.model_dump(mode="json"),
        "state": run.state,
        "lock_owner": lock.run_id if lock else None,
        "owns_lock": lock is not None and lock.run_id == run.run_id,
        "result": run.result.model_dump(mode="json") if run.result else None,
        "cycles": [_cycle_summary(cycle) for cycle in existing],
        "next": _next_step(run, existing, run_dir),
    }


# --- CLI ----------------------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="loop.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="ロックを取り、run を作る")
    begin.add_argument("pr", help="PR の番号（#N・N。親の origin）か URL")
    begin.add_argument("--controller-model", required=True, help="司令塔の正確なモデル ID")
    begin.add_argument("--controller-runtime", required=True, choices=RUNTIMES)
    begin.add_argument("--max-cycles", type=int, default=3)
    begin.add_argument("--hold-action", choices=("skip", "fix"), default="skip", help="Hold の方針（既定: skip）")
    begin.add_argument("--assign", action="append", default=[], metavar="ROLE=AGENT[,AGENT]")
    begin.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    begin.add_argument("--config", type=Path)
    begin.add_argument("--fresh-review", action="store_true", help="現在の HEAD の成果があってもレビュアーを起動する")
    for name in ("cycle-begin", "review"):
        sub = commands.add_parser(name)
        sub.add_argument("--run", type=Path, required=True)
    end = commands.add_parser("cycle-end")
    end.add_argument("--run", type=Path, required=True)
    end.add_argument("--finalize-result", type=Path)
    end.add_argument("--round-dir", type=Path)
    end.add_argument("--role-run", type=Path, action="append", default=[])
    end.add_argument("--record-file", type=Path)
    done = commands.add_parser("finish")
    done.add_argument("--run", type=Path, required=True)
    done.add_argument("--abort", help="permission|environment|external: <具体的な障害>")
    show = commands.add_parser("status")
    show.add_argument("pr", nargs="?")
    show.add_argument("--run", type=Path)
    rescue = commands.add_parser("recover")
    rescue.add_argument("--run", type=Path, required=True)
    rescue.add_argument("--operator-confirmed-stopped", help="停止を確かめた事実（記録に残す）")
    return root


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    name = cast(str, args.command)
    if name == "start":
        return start(
            args.pr,
            controller_model=args.controller_model,
            controller_runtime=args.controller_runtime,
            max_cycles=args.max_cycles,
            hold_action=args.hold_action,
            assign=args.assign,
            template=args.template.resolve(),
            config=args.config.resolve() if args.config else None,
            fresh_review=args.fresh_review,
        )
    if name == "cycle-begin":
        return cycle_begin(args.run.resolve())
    if name == "cycle-end":
        return cycle_end(
            args.run.resolve(),
            finalize_result=args.finalize_result,
            round_dir=args.round_dir,
            role_runs=args.role_run,
            record_file=args.record_file,
        )
    if name == "finish":
        return finish(args.run.resolve(), abort=args.abort)
    if name == "status":
        return status(args.run.resolve() if args.run else None, argument=args.pr)
    if name == "review":
        return review(args.run.resolve())
    if name == "recover":
        return recover(args.run.resolve(), operator_confirmed_stopped=args.operator_confirmed_stopped)
    raise LoopError(2, "arguments", f"未対応のサブコマンド: {name}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output = dispatch(args)
    except LoopError as error:
        run = getattr(args, "run", None)
        print(
            json.dumps(
                {
                    "status": "error",
                    "stage": error.stage,
                    "reason": error.reason,
                    "run_dir": str(run) if run is not None else None,
                    "next": error.next_command,
                    "requires_input": error.requires_input,
                },
                ensure_ascii=False,
            )
        )
        return error.code
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
