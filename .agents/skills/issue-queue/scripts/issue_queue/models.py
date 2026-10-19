"""Issue queue の保存形式。機械状態の書き手は queue.py に限定する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Phase = Literal["implement", "followup", "cleanup"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(Record):
    pane_id: str
    terminal_id: str
    session: str


class Policy(Record):
    agent_kind: Literal["claude", "codex", "pi"]
    agent_args: list[str]
    authorization: str = Field(min_length=1)
    exclusions: list[str]
    remote_branches: Literal["keep"] = "keep"
    additional_issues: Literal["propose", "enqueue"] = "propose"
    trial: bool = False
    monitor_seconds: int = Field(default=28800, ge=60, le=28800)


class Init(Record):
    issues: list[int] = Field(min_length=1)
    policy: Policy


class Retained(Record):
    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    blocks_next: bool


class IgnoredWorktree(Record):
    """起動の前の検査で、問題が無いと判定して無視したworktree。runのイベントに残す。"""

    path: str
    head: str | None
    branch: str | None
    basis: str


class Result(Record):
    outcome: Literal["success", "stopped", "failed"]
    summary: str = Field(min_length=1)
    finalized: bool
    background_stopped: bool
    retained: list[Retained]
    urls: list[str]
    issues: list[str]
    loop_run: str | None = None


class Effect(Record):
    key: str
    kind: Literal["issue", "comment"]
    target: int | None
    title: str | None
    body: str
    marker: str
    state: Literal["intent", "confirmed"] = "intent"
    url: str | None = None
    created_at: str = Field(default_factory=now)


class Attempt(Record):
    id: str
    issue: int
    phase: Phase
    agent_name: str
    marker: str
    created_at: str = Field(default_factory=now)
    version: dict[str, str]
    # agent startへ渡す引数。旧helperのattemptには無い。
    launch_args: list[str] | None = None
    launch: Literal["prepared", "splitting", "pane_created", "starting", "started", "submitting", "submitted"]
    pane_id: str | None = None
    pane_terminal_id: str | None = None
    identity: Identity | None = None
    acknowledged: bool = False
    prompt: str = ""
    result: Result | None = None
    effects: list[Effect] = Field(default_factory=list[Effect])
    closing: bool = False
    closed: bool = False
    # 受領前に子が消えたattemptをabandonで閉じた理由。
    abandon_reason: str | None = None
    merged_at: str | None = None
    pr_url: str | None = None
    finished_at: str | None = None


class Decision(Record):
    id: str
    category: Literal["blocking", "retain", "final"]
    reason: str = Field(min_length=1)
    resolution: str | None = None
    path: str | None = None
    pr: int | None = None
    pr_head: str | None = None


class Run(Record):
    schema_version: Literal[1] = 1
    id: str
    parent: str
    repository: Literal["usaturn/xrift-poc"] = "usaturn/xrift-poc"
    owner: Identity
    queue: list[int]
    index: int = 0
    phase: Phase = "implement"
    policy: Policy
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    attempts: list[Attempt] = Field(default_factory=list[Attempt])
    decisions: list[Decision] = Field(default_factory=list[Decision])
    stop_after: Literal["followup", "cleanup"] | None = None
    paused: bool = False
    complete: bool = False
    stopped_reason: str | None = None
    observation: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])

    def current(self) -> Attempt | None:
        if self.complete:
            return None
        return next(
            (
                a
                for a in reversed(self.attempts)
                if a.issue == self.queue[self.index] and a.phase == self.phase and not a.closed
            ),
            None,
        )

    def event(self, action: str, **values: Any) -> None:
        self.events.append({"at": now(), "action": action, **values})
