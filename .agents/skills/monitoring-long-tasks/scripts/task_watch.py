"""事前宣言した長時間 worker を監視し、継続・保存への移行・停止を判定する（Issue #598）。

判定は宣言（Policy）と観測（Observation）だけから決める。実質進捗は worker が追記する
JSONL の新しい項目で数え、heartbeat・同じ内容の繰り返し・ファイルの更新時刻は数えない。

Linux の /proc を前提にする。sandbox ではない。setsid で process group を抜けた子孫は観測しない。

- ``run``: 対象を独立した session で起動し、state dir に宣言・状態・最終結果を書く
- ``status``: 状態を短い JSON で返す。watcher と対象の生存は識別情報（PID・starttime）で照合する
- ``stop``: 識別情報が一致する watcher にだけ SIGINT を送り、宣言どおりの停止を待つ
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

SCHEMA_VERSION = 1
MAX_STOP_GRACE_SECONDS = 30.0
# 進捗を一度に読む大きさ。1 行がこれを超えたら不正な記録として次の改行まで読み飛ばす。
READ_LIMIT_BYTES = 1024 * 1024
# 保存への移行を求めた後、この秒数（ポーリング間隔が長ければその間隔）に書かれた新しい項目は、
# 予約枠の新規調査に数えない。worker が確認してから書くまでの間と、watcher が読んでから
# finalize.requested を置くまでの間の競合で、協力的な worker を止めないための猶予。
FINALIZE_GRACE_SECONDS = 1.0
# 対象の終了後、同じ group の子孫が終わるのを待つ上限。終了処理の短い遅れを残存と誤認しない。
EXIT_SETTLE_SECONDS = 2.0

Decision = Literal["continue", "finalize", "stop", "hold", "stopped", "completed", "exited_incomplete"]
Reason = Literal[
    "",
    "safety",
    "deadline",
    "repeated_failure",
    "investigation_in_reserve",
    "no_progress",
    "operator",
    "launch_failed",
    "residual_processes",
]
FailureClass = Literal["reliability", "safety"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Policy(_Strict):
    """起動前に宣言する監視と停止の方針。"""

    deadline_seconds: float = Field(gt=0)
    reserve_seconds: float = Field(default=0.0, ge=0)
    no_progress_seconds: float | None = Field(default=None, gt=0)
    progress_file: str | None = None
    result_file: str | None = None
    partial: tuple[str, ...] = ()
    stop: Literal["none", "INT", "TERM"]
    stop_grace_seconds: float = Field(default=MAX_STOP_GRACE_SECONDS, gt=0, le=MAX_STOP_GRACE_SECONDS)
    escalate_kill: bool = False
    max_repeated_failures: int = Field(default=2, ge=1)
    poll_seconds: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.reserve_seconds >= self.deadline_seconds:
            raise ValueError("reserve_seconds must be shorter than deadline_seconds")
        if self.no_progress_seconds is not None and self.progress_file is None:
            raise ValueError("no_progress_seconds requires progress_file")
        return self

    @property
    def research_deadline_seconds(self) -> float:
        """新規調査を終える時刻（開始からの秒数）。予約枠はここから絶対期限まで。"""
        return self.deadline_seconds - self.reserve_seconds


@dataclass(frozen=True, slots=True)
class Observation:
    elapsed: float
    exited: bool
    exit_code: int | None
    result_present: bool
    since_substantive: float
    safety_signature: str | None
    max_failure_count: int
    investigation_in_reserve: bool


def decide(policy: Policy, obs: Observation) -> tuple[Decision, Reason]:
    """宣言と観測から次の判定を返す。停止の理由は安全 → 期限 → 失敗 → 予約枠 → 無進捗の順。"""
    if obs.exited:
        # 安全の失敗を記録したまま終了した対象を、完了として通さない。
        if obs.safety_signature is not None:
            return "exited_incomplete", "safety"
        done = obs.exit_code == 0 and (policy.result_file is None or obs.result_present)
        return ("completed" if done else "exited_incomplete"), ""
    reason = _stop_reason(policy, obs)
    if reason:
        # 停止を宣言していない対象には決してシグナルを送らない。理由を付けて保留にする。
        return ("hold" if policy.stop == "none" else "stop"), reason
    if policy.reserve_seconds > 0 and obs.elapsed >= policy.research_deadline_seconds:
        return "finalize", ""
    return "continue", ""


def _stop_reason(policy: Policy, obs: Observation) -> Reason:
    if obs.safety_signature is not None:
        return "safety"
    if obs.elapsed >= policy.deadline_seconds:
        return "deadline"
    if obs.max_failure_count >= policy.max_repeated_failures:
        return "repeated_failure"
    if obs.investigation_in_reserve:
        return "investigation_in_reserve"
    if (
        policy.no_progress_seconds is not None
        and obs.elapsed < policy.research_deadline_seconds
        and obs.since_substantive >= policy.no_progress_seconds
    ):
        return "no_progress"
    return ""


class _ProgressRecord(_Strict):
    kind: Literal["progress"]
    items: list[str]


class _HeartbeatRecord(_Strict):
    kind: Literal["heartbeat"]


class _FailureRecord(_Strict):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)

    kind: Literal["failure"]
    failure_class: FailureClass = Field(alias="class")
    signature: str = Field(min_length=1)


class _SavedRecord(_Strict):
    kind: Literal["saved"]
    path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if not os.path.isabs(self.path):
            raise ValueError("saved path must be absolute")
        return self


_RECORD = TypeAdapter[_ProgressRecord | _HeartbeatRecord | _FailureRecord | _SavedRecord](
    Annotated[_ProgressRecord | _HeartbeatRecord | _FailureRecord | _SavedRecord, Field(discriminator="kind")]
)


@dataclass(slots=True)
class FailureCount:
    failure_class: FailureClass
    count: int = 0


@dataclass(slots=True)
class ProgressState:
    """進捗ファイルから読み取った累積の状態。offset は処理済みの完全な行の末尾。"""

    offset: int = 0
    items: set[str] = field(default_factory=set[str])
    last_substantive: float = 0.0
    heartbeats: int = 0
    invalid_records: int = 0
    failures: dict[str, FailureCount] = field(default_factory=dict[str, FailureCount])
    safety_signature: str | None = None
    saved: list[str] = field(default_factory=list[str])
    investigation_in_reserve: bool = False
    skipping_long_line: bool = False

    @property
    def max_failure_count(self) -> int:
        return max((item.count for item in self.failures.values()), default=0)


def read_progress(path: Path | None, state: ProgressState, *, elapsed: float, reserve_from: int | None) -> None:
    """改行で終わった新しい行を、呼び出した時点のファイルの末尾まで読み、state を更新する。

    通常ファイルでなければ読まない（FIFO で watcher を止めない）。reserve_from はファイル内の位置で、
    そこから始まる行に新しい項目があれば予約枠内の新規調査とみなす。None なら予約枠はまだ始まっていない。
    """
    if path is None:
        return
    try:
        info = os.stat(path)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_size <= state.offset:
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return
    with os.fdopen(descriptor, "rb") as stream:
        while state.offset < info.st_size:
            stream.seek(state.offset)
            chunk = stream.read(min(READ_LIMIT_BYTES, info.st_size - state.offset))
            end = chunk.rfind(b"\n")
            if end < 0:
                if len(chunk) < READ_LIMIT_BYTES:
                    return  # 書きかけの末尾行。次の呼び出しで読む
                # 1 行が上限を超えた。後続の記録（安全の失敗・saved）を読めなくしないよう、読み飛ばす。
                if not state.skipping_long_line:
                    state.invalid_records += 1
                    state.skipping_long_line = True
                state.offset += len(chunk)
                continue
            position = state.offset
            for line in chunk[:end].split(b"\n"):
                start, position = position, position + len(line) + 1
                if state.skipping_long_line:
                    state.skipping_long_line = False
                    continue
                in_reserve = reserve_from is not None and start >= reserve_from
                _apply(line, state, elapsed=elapsed, in_reserve=in_reserve)
            state.offset = position


def _apply(line: bytes, state: ProgressState, *, elapsed: float, in_reserve: bool) -> None:
    try:
        record = _RECORD.validate_json(line)
    except ValidationError:
        state.invalid_records += 1
        return
    match record:
        case _ProgressRecord(items=items):
            new = {item.strip() for item in items if item.strip()} - state.items
            if not new:
                state.heartbeats += 1
                return
            state.items |= new
            state.last_substantive = elapsed
            if in_reserve:
                state.investigation_in_reserve = True
        case _HeartbeatRecord():
            state.heartbeats += 1
        case _FailureRecord(failure_class=failure_class, signature=signature):
            counted = state.failures.setdefault(signature, FailureCount(failure_class))
            counted.count += 1
            if failure_class == "safety" and state.safety_signature is None:
                state.safety_signature = signature
        case _SavedRecord(path=saved):
            state.saved.append(saved)


class Identity(_Strict):
    """PID だけでは再利用を区別できないため、/proc の starttime と組で識別する。"""

    pid: int = Field(gt=0)
    starttime: int = Field(ge=0)


class Declaration(_Strict):
    schema_version: Literal[1]
    created_at: str
    state_dir: str
    cwd: str
    argv: tuple[str, ...]
    policy: Policy
    watcher: Identity
    target: Identity | None


class TargetState(_Strict):
    pid: int | None
    alive: bool
    exit_code: int | None


class FailureSummary(_Strict):
    failure_class: FailureClass
    count: int


class StopOutcome(_Strict):
    signal: str
    requested_at: str
    confirmed: bool


class Status(_Strict):
    schema_version: Literal[1] = 1
    decision: Decision
    reason: Reason
    updated_at: str
    elapsed_seconds: float
    deadline_seconds: float
    research_deadline_seconds: float
    remaining_seconds: float
    deadline_exceeded: bool
    seconds_since_substantive_progress: float
    substantive_items: int
    heartbeats: int
    invalid_records: int
    failures: dict[str, FailureSummary]
    finalize_requested: bool
    target: TargetState


class Final(Status):
    stop: StopOutcome | None
    residual_pids: tuple[int, ...]
    partial_results: tuple[str, ...]
    paths: dict[str, str | None]
    next_step: str


@dataclass(frozen=True, slots=True)
class _Stat:
    state: str
    ppid: int
    pgrp: int
    starttime: int


def _read_stat(pid: int) -> _Stat | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        return _Stat(fields[0].decode("ascii"), int(fields[1]), int(fields[2]), int(fields[19]))
    except OSError, ValueError, IndexError, UnicodeDecodeError:
        return None


def _matches(identity: Identity, *, watcher: bool = False) -> bool:
    """識別情報が一致し、実行中（ゾンビでない）かを返す。watcher はコマンドラインも照合する。"""
    current = _read_stat(identity.pid)
    if current is None or current.starttime != identity.starttime or current.state in ("Z", "X"):
        return False
    if not watcher:
        return True
    try:
        arguments = Path(f"/proc/{identity.pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(Path(os.fsdecode(argument)).name == "task_watch.py" for argument in arguments)


def _caller_and_ancestors() -> set[int]:
    pid, seen = os.getpid(), set[int]()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        current = _read_stat(pid)
        if current is None:
            break
        pid = current.ppid
    return seen


def _live_group_members(pgid: int) -> tuple[int, ...]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal():
            current = _read_stat(int(entry.name))
            if current is not None and current.pgrp == pgid and current.state not in ("Z", "X"):
                members.append(int(entry.name))
    return tuple(sorted(members))


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_json(path: Path, model: BaseModel) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


_EXIT_CODES: dict[str, int] = {"completed": 0, "exited_incomplete": 1, "stopped": 1, "hold": 3}


def _next_step(decision: Decision, reason: Reason, stop: StopOutcome | None) -> str:
    if decision == "hold":
        if reason == "residual_processes":
            return (
                "対象は終了したが、同じ process group にプロセスが残っている。"
                "残存 PID と各パスを報告し、依存作業を始めず、作業領域を消さない。"
            )
        if stop is not None:
            return "停止を確認できない。対象・停止状態・残存パスを報告し、依存作業を始めず、作業領域を消さない。"
        if reason == "safety":
            return (
                "安全の失敗が記録されたが、停止を宣言していないので止めていない。"
                "全体停止として利用者に報告し、対象は残す。"
            )
        return "停止を宣言していないので止めていない。stall・期限超過として報告し、対象は残す。"
    if reason == "safety":
        return "安全の失敗が記録された。全体停止して利用者に報告する。効率のために再試行しない。"
    if decision == "completed":
        return "結果を読んで次へ進む。"
    if decision == "exited_incomplete":
        return "結果が無いまま終了した。結果を 1 回だけ再確認し、失敗として扱う。ログは末尾だけ読む。"
    if reason == "repeated_failure":
        return "同じ失敗が上限に達した。同じ手段で再試行せず、方法の変更か範囲の縮小を選ぶ。"
    return "部分成果から保存し、停止理由・残作業・次の対策と理由を短く記録する。"


class _Watch:
    """1 回の run の状態。ポーリングごとに観測し、状態ファイルを書き換える。"""

    def __init__(self, policy: Policy, argv: list[str], state_dir: Path) -> None:
        self.policy = policy
        self.argv = argv
        self.state_dir = state_dir
        self.started = time.monotonic()
        self.progress = ProgressState()
        self.finalize_requested = False
        self.finalize_requested_at: float | None = None
        # 予約枠の新規調査として数え始める進捗ファイル内の位置。猶予が過ぎた時点のファイルの末尾。
        self.reserve_from: int | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.last_event: tuple[str, str] | None = None

    def path(self, name: str) -> Path:
        return self.state_dir / name

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def status(self, decision: Decision, reason: Reason) -> Status:
        elapsed = self.elapsed()
        exit_code = self.proc.returncode if self.proc is not None else None
        return Status(
            decision=decision,
            reason=reason,
            updated_at=_utc_now(),
            elapsed_seconds=elapsed,
            deadline_seconds=self.policy.deadline_seconds,
            research_deadline_seconds=self.policy.research_deadline_seconds,
            remaining_seconds=max(0.0, self.policy.deadline_seconds - elapsed),
            deadline_exceeded=elapsed >= self.policy.deadline_seconds,
            seconds_since_substantive_progress=elapsed - self.progress.last_substantive,
            substantive_items=len(self.progress.items),
            heartbeats=self.progress.heartbeats,
            invalid_records=self.progress.invalid_records,
            failures={
                name: FailureSummary(failure_class=item.failure_class, count=item.count)
                for name, item in self.progress.failures.items()
            },
            finalize_requested=self.finalize_requested,
            target=TargetState(
                pid=self.proc.pid if self.proc is not None else None,
                alive=self.proc is not None and exit_code is None,
                exit_code=exit_code,
            ),
        )

    def record(self, decision: Decision, reason: Reason) -> None:
        status = self.status(decision, reason)
        _write_json(self.path("status.json"), status)
        if (decision, reason) != self.last_event:
            self.last_event = (decision, reason)
            event = {"at": status.updated_at, "elapsed_seconds": status.elapsed_seconds}
            with self.path("events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({**event, "decision": decision, "reason": reason}) + "\n")

    def finish(
        self, decision: Decision, reason: Reason, stop: StopOutcome | None = None, residual: tuple[int, ...] = ()
    ) -> int:
        self.record(decision, reason)
        saved = [*self.progress.saved, *self.policy.partial]
        partial = tuple(dict.fromkeys(path for path in saved if os.path.exists(path)))
        names = ("declaration.json", "status.json", "final.json", "events.jsonl", "finalize.requested")
        paths: dict[str, str | None] = {"state_dir": str(self.state_dir)}
        paths.update({name: str(self.path(name)) for name in names})
        paths.update(
            stdout=str(self.path("target.stdout.log")),
            stderr=str(self.path("target.stderr.log")),
            progress_file=self.policy.progress_file,
            result_file=self.policy.result_file,
        )
        final = Final(
            **self.status(decision, reason).model_dump(),
            stop=stop,
            residual_pids=residual,
            partial_results=partial,
            paths=paths,
            next_step=_next_step(decision, reason, stop),
        )
        _write_json(self.path("final.json"), final)
        _print(final.model_dump(mode="json"))
        return _EXIT_CODES[decision]

    def request_finalize(self) -> None:
        self.path("finalize.requested").write_text(_utc_now() + "\n", encoding="utf-8")
        self.finalize_requested = True
        self.finalize_requested_at = self.elapsed()

    def observe(self) -> Observation:
        assert self.proc is not None
        exit_code = self.proc.poll()
        elapsed = self.elapsed()
        progress_path = Path(self.policy.progress_file) if self.policy.progress_file else None
        grace = max(self.policy.poll_seconds, FINALIZE_GRACE_SECONDS)
        if (
            self.reserve_from is None
            and self.finalize_requested_at is not None
            and elapsed >= self.finalize_requested_at + grace
        ):
            # 猶予の間に書かれた行（この位置より前）は、予約枠の新規調査に数えない。
            self.reserve_from = _file_size(progress_path)
        read_progress(progress_path, self.progress, elapsed=elapsed, reserve_from=self.reserve_from)
        result = self.policy.result_file
        return Observation(
            elapsed=elapsed,
            exited=exit_code is not None,
            exit_code=exit_code,
            result_present=result is not None and os.path.exists(result),
            since_substantive=elapsed - self.progress.last_substantive,
            safety_signature=self.progress.safety_signature,
            max_failure_count=self.progress.max_failure_count,
            investigation_in_reserve=self.progress.investigation_in_reserve,
        )


def _file_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _wait_group_empty(pgid: int, until: float) -> tuple[int, ...]:
    while (members := _live_group_members(pgid)) and time.monotonic() < until:
        time.sleep(0.05)
    return members


def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    # 回収前の leader、または生存メンバーが残る間だけ、その group 番号は watcher が起動した group を指す。
    if proc.returncode is None or _live_group_members(proc.pid):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError, PermissionError:
            pass


def _wait_stopped(proc: subprocess.Popen[bytes], until: float) -> bool:
    while True:
        if proc.poll() is not None and not _live_group_members(proc.pid):
            return True
        if time.monotonic() >= until:
            return False
        time.sleep(0.05)


def _stop_target(proc: subprocess.Popen[bytes], policy: Policy) -> StopOutcome:
    sig = signal.SIGINT if policy.stop == "INT" else signal.SIGTERM
    requested_at = _utc_now()
    _signal_group(proc, sig)
    confirmed = _wait_stopped(proc, time.monotonic() + policy.stop_grace_seconds)
    if not confirmed and policy.escalate_kill:
        _signal_group(proc, signal.SIGKILL)
        confirmed = _wait_stopped(proc, time.monotonic() + policy.stop_grace_seconds)
    return StopOutcome(signal=sig.name, requested_at=requested_at, confirmed=confirmed)


def _run(policy: Policy, argv: list[str], state_dir: Path) -> int:
    try:
        # 既存の directory や dangling symlink を拒否し、他の run の状態を上書きしない。
        state_dir.mkdir(mode=0o700)
    except OSError as error:
        print(json.dumps({"error": f"state dir: {error.strerror}"}), file=sys.stderr)
        return 2
    run = _Watch(policy, argv, state_dir)
    operator = {"requested": False}
    wake_read, wake_write = socket.socketpair()
    wake_read.setblocking(False)
    wake_write.setblocking(False)
    signal.set_wakeup_fd(wake_write.fileno(), warn_on_full_buffer=False)

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        if signum != signal.SIGCHLD:
            operator["requested"] = True

    for handled in (signal.SIGINT, signal.SIGTERM, signal.SIGCHLD):
        signal.signal(handled, on_signal)
    with run.path("target.stdout.log").open("wb") as stdout, run.path("target.stderr.log").open("wb") as stderr:
        try:
            run.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True, close_fds=True
            )
        except OSError:
            return run.finish("exited_incomplete", "launch_failed")
    own = _read_stat(os.getpid())
    target = _read_stat(run.proc.pid)
    declaration = Declaration(
        schema_version=SCHEMA_VERSION,
        created_at=_utc_now(),
        state_dir=str(state_dir),
        cwd=os.getcwd(),
        argv=tuple(argv),
        policy=policy,
        watcher=Identity(pid=os.getpid(), starttime=own.starttime if own else 0),
        target=Identity(pid=run.proc.pid, starttime=target.starttime) if target else None,
    )
    _write_json(run.path("declaration.json"), declaration)
    while True:
        decision, reason = decide(policy, run.observe())
        if operator["requested"] and decision not in ("completed", "exited_incomplete"):
            decision, reason = ("hold" if policy.stop == "none" else "stop"), "operator"
        if decision == "finalize" and not run.finalize_requested:
            run.request_finalize()
        if decision in ("completed", "exited_incomplete"):
            # 対象が終わっても、同じ group に子孫が残っていれば片付けや依存作業を始めさせない。
            settle = time.monotonic() + min(policy.stop_grace_seconds, EXIT_SETTLE_SECONDS)
            residual = _wait_group_empty(run.proc.pid, settle)
            if residual:
                return run.finish("hold", "residual_processes", residual=residual)
            return run.finish(decision, reason)
        if decision == "stop":
            run.record(decision, reason)
            outcome = _stop_target(run.proc, policy)
            residual = _live_group_members(run.proc.pid)
            return run.finish("stopped" if outcome.confirmed else "hold", reason, outcome, residual)
        if decision == "hold" and reason == "operator":
            return run.finish(decision, reason, residual=_live_group_members(run.proc.pid))
        run.record(decision, reason)
        select.select([wake_read], [], [], policy.poll_seconds)
        try:
            while wake_read.recv(256):
                pass
        except BlockingIOError:
            pass


def _load_declaration(state_dir: Path) -> Declaration | None:
    try:
        return Declaration.model_validate_json(state_dir.joinpath("declaration.json").read_bytes())
    except OSError, ValidationError:
        return None


def _read_object(path: Path) -> dict[str, object] | None:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(loaded, dict):
        return None
    # JSON の object のキーは常に文字列である。
    return cast(dict[str, object], loaded)


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _final_exit_code(final: dict[str, object]) -> int:
    decision = final.get("decision")
    return _EXIT_CODES.get(decision, 3) if isinstance(decision, str) else 3


def _status(state_dir: Path) -> int:
    declaration = _load_declaration(state_dir)
    if declaration is None:
        print(json.dumps({"error": "declaration.json is missing or invalid"}), file=sys.stderr)
        return 2
    output: dict[str, object] = {"source": "declaration"}
    for source in ("final", "status"):
        loaded = _read_object(state_dir / f"{source}.json")
        if loaded is not None:
            output = {"source": source, **loaded}
            break
    output["watcher_alive"] = _matches(declaration.watcher, watcher=True)
    output["target_alive"] = declaration.target is not None and _matches(declaration.target)
    _print(output)
    return 0


def _stop(state_dir: Path) -> int:
    declaration = _load_declaration(state_dir)
    if declaration is None:
        print(json.dumps({"error": "declaration.json is missing or invalid"}), file=sys.stderr)
        return 2
    final_path = state_dir / "final.json"
    if (final := _read_object(final_path)) is not None:
        _print(final)
        return _final_exit_code(final)
    watcher = declaration.watcher
    if watcher.pid in _caller_and_ancestors():
        _print({"refused": True, "reason": "watcher PID is this caller or its ancestor", "pid": watcher.pid})
        return 2
    if not _matches(watcher, watcher=True):
        _print({"refused": True, "reason": "watcher identity does not match", "pid": watcher.pid})
        return 2
    os.kill(watcher.pid, signal.SIGINT)
    grace = declaration.policy.stop_grace_seconds * (2 if declaration.policy.escalate_kill else 1)
    until = time.monotonic() + grace + 5
    while time.monotonic() < until:
        if (final := _read_object(final_path)) is not None:
            _print(final)
            return _final_exit_code(final)
        time.sleep(0.1)
    _print({"stop_requested": True, "final": None, "watcher_alive": _matches(watcher, watcher=True)})
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="対象を起動し、宣言どおりに判定・停止する（対象の argv は -- の後）")
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--deadline-seconds", type=float, required=True)
    run.add_argument("--reserve-seconds", type=float, default=0.0)
    run.add_argument("--no-progress-seconds", type=float)
    run.add_argument("--progress-file")
    run.add_argument("--result-file")
    run.add_argument("--partial", action="append", default=[])
    run.add_argument("--stop", choices=("none", "INT", "TERM"), required=True)
    run.add_argument("--stop-grace-seconds", type=float, default=MAX_STOP_GRACE_SECONDS)
    run.add_argument("--escalate-kill", action="store_true")
    run.add_argument("--max-repeated-failures", type=int, default=2)
    run.add_argument("--poll-seconds", type=float, default=5.0)
    for name, text in (("status", "状態を短い JSON で返す"), ("stop", "watcher に宣言どおりの停止を求める")):
        commands.add_parser(name, help=text).add_argument("--state-dir", type=Path, required=True)
    return parser


def _absolute(path: str | None) -> str | None:
    return os.path.abspath(path) if path is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in arguments:
        split = arguments.index("--")
        arguments, command = arguments[:split], arguments[split + 1 :]
    args = _parser().parse_args(arguments)
    state_dir = Path(os.path.abspath(args.state_dir))
    if args.command == "status":
        return _status(state_dir)
    if args.command == "stop":
        return _stop(state_dir)
    if not command:
        print(json.dumps({"error": "target argv is required after --"}), file=sys.stderr)
        return 2
    try:
        policy = Policy(
            deadline_seconds=args.deadline_seconds,
            reserve_seconds=args.reserve_seconds,
            no_progress_seconds=args.no_progress_seconds,
            progress_file=_absolute(args.progress_file),
            result_file=_absolute(args.result_file),
            partial=tuple(os.path.abspath(path) for path in args.partial),
            stop=args.stop,
            stop_grace_seconds=args.stop_grace_seconds,
            escalate_kill=args.escalate_kill,
            max_repeated_failures=args.max_repeated_failures,
            poll_seconds=args.poll_seconds,
        )
    except ValidationError as error:
        problems = [f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors()]
        print(json.dumps({"error": "invalid policy", "problems": problems}, ensure_ascii=False), file=sys.stderr)
        return 2
    return _run(policy, command, state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
