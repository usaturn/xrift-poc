"""Strict configuration and wire models for the noninteractive runner."""

import math
import re
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

Runtime = Literal["claude-code", "devin", "pi", "codex", "cursor"]
ResultShape = Literal["json", "text"]
State = Literal[
    "success",
    "self",
    "unsupported",
    "launch_failed",
    "awaiting_authentication",
    "awaiting_approval",
    "response_failed",
    "missing_result",
    "invalid_result",
    "timed_out",
    "interrupted",
]
ReceiptStatus = Literal["success", "awaiting_authentication", "awaiting_approval", "response_failed", "interrupted"]
BatchState = Literal["success", "self", "partial_success", "failed"]
Payload = dict[str, object] | str | None


def _identifier(value: object) -> object:
    if isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("invalid identifier")
    return value


def _nonblank(value: object) -> object:
    if isinstance(value, str) and (not value.strip() or "\x00" in value):
        raise ValueError("text must be nonblank and contain no NUL")
    return value


def _argument(value: object) -> object:
    if isinstance(value, str) and "\x00" in value:
        raise ValueError("argument must contain no NUL")
    return value


Identifier = Annotated[str, BeforeValidator(_identifier)]
Nonblank = Annotated[str, BeforeValidator(_nonblank)]
Argument = Annotated[str, BeforeValidator(_argument)]
PositiveSeconds = Annotated[float, Field(gt=0)]
Command = Annotated[list[Argument], Field(min_length=1)]
ElapsedSeconds = Annotated[float, Field(ge=0)]


class StrictModel(BaseModel):
    """Reject extra keys, coercion, and nonfinite numbers at every boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def _schema_version(cls, value: object) -> object:
        # Literal[1] alone accepts True because bool is an int subclass.
        if type(value) is not int or value != 1:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("payload", mode="before", check_fields=False)
    @classmethod
    def _payload(cls, value: object) -> object:
        """Accept only JSON values, including within an otherwise arbitrary object map."""
        active: set[int] = set()

        def visit(item: object) -> None:
            if item is None or type(item) in (str, bool, int):
                return
            if type(item) is float and math.isfinite(item):
                return
            item_id = id(item)
            if isinstance(item, (dict, list)):
                if item_id in active:
                    raise ValueError("payload must not contain cycles")
                active.add(item_id)
                if isinstance(item, dict):
                    for key, child in cast(dict[object, object], item).items():
                        if type(key) is not str:
                            raise ValueError("payload keys must be strings")
                        visit(child)
                else:
                    for child in cast(list[object], item):
                        visit(child)
                active.remove(item_id)
                return
            raise ValueError("payload must contain only JSON values")

        visit(value)
        return value


class Agent(StrictModel):
    runtime: Runtime
    command: list[Argument] = Field(min_length=1)
    model: Nonblank
    model_key: Nonblank | None = None
    provider: Nonblank | None = None
    effort: Nonblank | None = None
    approval: Literal["noninteractive"] = "noninteractive"
    skill_sources: list[Nonblank] = Field(default_factory=list)
    args: list[Argument] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def _executable(cls, value: list[str]) -> list[str]:
        _nonblank(value[0])
        return value

    @field_validator("env")
    @classmethod
    def _environment_names(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in value):
            raise ValueError("environment entries must be variable names")
        return value


class Role(StrictModel):
    skills: list[Nonblank] = Field(default_factory=list)
    writes: bool
    deadline_seconds: PositiveSeconds
    result_shape: ResultShape


class Assignment(StrictModel):
    agents: list[Identifier] = Field(min_length=1)
    fallback: list[Identifier] = Field(default_factory=list)

    @field_validator("agents")
    @classmethod
    def _self_alone(cls, value: list[str]) -> list[str]:
        if "self" in value and value != ["self"]:
            raise ValueError("self must be the only assigned agent")
        return value

    @field_validator("fallback")
    @classmethod
    def _no_self_fallback(cls, value: list[str]) -> list[str]:
        if "self" in value:
            raise ValueError("self is not a fallback agent")
        return value


class Workflow(StrictModel):
    assignments: dict[Identifier, Assignment] = Field(min_length=1)
    exclude_same_model_as: dict[Identifier, Identifier] = Field(default_factory=dict)
    # CLI が result.json を保存した後に順に起動する argv（hooks.py）。結果の状態は変えない
    after_run: list[Command] = Field(default_factory=list[list[str]])

    @field_validator("after_run")
    @classmethod
    def _executables(cls, value: list[list[str]]) -> list[list[str]]:
        for command in value:
            _nonblank(command[0])
        return value


class Config(StrictModel):
    schema_version: Literal[1] = 1
    agents: dict[Identifier, Agent] = Field(default_factory=dict)
    roles: dict[Identifier, Role] = Field(default_factory=dict)
    workflows: dict[Identifier, Workflow] = Field(default_factory=dict)

    @field_validator("agents")
    @classmethod
    def _reserve_self(cls, value: dict[str, Agent]) -> dict[str, Agent]:
        if "self" in value:
            raise ValueError("self is reserved")
        return value


class Request(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    request_id: Identifier
    agent_id: Identifier
    role: Identifier
    work_dir: Path
    prompt: str
    deadline_seconds: PositiveSeconds
    result_shape: ResultShape


class Receipt(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    request_id: Identifier
    agent_id: Identifier
    role: Identifier
    status: ReceiptStatus
    payload: Payload = None


class Identity(StrictModel):
    model: str | None = None
    provider: str | None = None
    effort: str | None = None
    session_id: str | None = None
    cli_version: str | None = None
    skill_versions: dict[str, str] = Field(default_factory=dict)


DiagnosisEnding = Literal["completed", "error_event", "turn_end", "incomplete", "unreadable"]
NonNegativeInt = Annotated[int, Field(ge=0)]
# The runner cuts every Diagnosis string to this many characters after redaction (#96).
DIAGNOSIS_TEXT_CHARS = 200


class Diagnosis(StrictModel):
    """How an attempt ended, for failures nobody watched (#96).

    Counts, fixed labels and the provider's code/status/message head only: never
    answer text, tool input or results, stdout/stderr bodies or environment values.
    """

    ending: DiagnosisEnding
    reason: str | None = None
    code: str | None = None
    status: int | None = None
    message: str | None = None
    steps: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    last_event: str | None = None
    last_input_tokens: NonNegativeInt | None = None
    last_record: str | None = None
    last_record_at: str | None = None
    stderr_authentication: bool = False
    stderr_approval: bool = False


class Attempt(StrictModel):
    agent_id: Identifier
    state: State
    identity: Identity = Field(default_factory=Identity)
    diagnosis: Diagnosis | None = None
    exit_code: int | None = None
    elapsed_seconds: ElapsedSeconds
    stop_confirmed: bool
    checkout_isolated: bool = False
    work_dir: Path | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    request_path: Path | None = None
    sensitive_paths: list[Path] = Field(default_factory=list[Path])
    payload: Payload = None


class Result(StrictModel):
    request_id: Identifier
    role: Identifier
    requested_agent: Identifier
    actual_agent: Identifier
    state: State
    attempts: list[Attempt]
    fallback_exhausted: bool = False


AuditReason = Literal[
    "checkout_missing",
    "not_directory",
    "git_unavailable",
    "filesystem_unavailable",
    "audit_unavailable",
    "content_changed",
    "checkout_replaced",
]


class CheckoutAudit(StrictModel):
    """Observations only: fixed reasons, no Git stderr or attribution to a worker."""

    path: str
    kind: Literal["unavailable_before", "unavailable_after", "content_changed", "replaced"]
    reason: AuditReason
    locked: bool = False
    prunable: bool = False


class BatchResult(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    state: BatchState
    assignments: dict[Identifier, list[Identifier]]
    results: list[Result]
    outside_changes: list[str]
    checkout_audit: list[CheckoutAudit] = Field(default_factory=list[CheckoutAudit])
    checkout_isolated: bool = False
    elapsed_seconds: ElapsedSeconds
    artifact_dir: Path
