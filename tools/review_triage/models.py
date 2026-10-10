"""共通型と API 定数（v1）。

TypeSafe System One（Jev）を使うレビュートリアージ PoC の全レーンが共有する型。
純粋なデータ定義だけを持ち、ネットワーク・Git・ファイル探索は行わない。
出典: https://docs.typesafe.ai/api.md, /models.md, /confidence, /primitives/noul（2026-09-26 時点）。
"""

import enum
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Annotated, Literal, Protocol, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL_ID = "jev-latest"  # 常に最新を使う（人間の指示 2026-09-26）。公式の版固定推奨は採用しない
STATE_CHAR_LIMIT = 24_000  # 公式上限「state + 最長質問 32k tokens」に対する保守的な文字数上限
REQUEST_BYTE_LIMIT = 200_000  # 公式上限「1 リクエスト 64k tokens」に対する保守的なバイト上限
CHOICE_MAX_OPTIONS = 255
SCORE_MIN_LEVELS = 2
SCORE_MAX_LEVELS = 10
QUESTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SECTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_HEX12 = r"^[0-9a-f]{12}$"
_SHA256 = r"^[0-9a-f]{64}$"


class _Strict(BaseModel):
    """本ツールが生成する型。未知フィールド・NaN・inf を拒否し、生成後は変更できない。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class _FromApi(BaseModel):
    """TypeSafe 応答由来の型。公式仕様に無いフィールドを ``model_extra`` に保持する。"""

    model_config = ConfigDict(extra="allow", frozen=True, allow_inf_nan=False)


class Repository(enum.StrEnum):
    ENV = "usaturn/xrift-poc"


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    UNKNOWN = "unknown"


class FindingStatus(enum.StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    PENDING = "pending"
    UNVERIFIED = "unverified"
    DECIDED = "decided"


class Marker(enum.StrEnum):
    VERIFIED = "verified"
    DESIGN_JUDGMENT = "design_judgment"
    CONFIRM_RECOMMENDED = "confirm_recommended"
    NONE = "none"


class Proposal(enum.StrEnum):
    FIX_NOW = "fix_now"
    SEPARATE_ISSUE = "separate_issue"
    REJECT = "reject"
    HUMAN_REVIEW = "human_review"


class ErrorKind(enum.StrEnum):
    NO_KEY = "no_key"
    NOT_ALLOWED = "not_allowed"
    TOO_LARGE = "too_large"
    CALL_BUDGET = "call_budget"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    TLS = "tls"
    REDIRECT = "redirect"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMIT = "rate_limit"
    SERVICE = "service"
    MALFORMED_RESPONSE = "malformed_response"
    UNEXPECTED_STATUS = "unexpected_status"


class ReportStatus(enum.StrEnum):
    ADVISORY = "advisory"
    FALLBACK = "fallback"
    UNCALIBRATED = "uncalibrated"


class NoulQuestion(_Strict):
    type: Literal["noul"] = "noul"
    instructions: str = Field(min_length=1)
    criteria: dict[Literal["true", "false"], str] | None = None


class ChoiceQuestion(_Strict):
    type: Literal["choice"] = "choice"
    instructions: str = Field(min_length=1)
    criteria: dict[str, str | None]

    @model_validator(mode="after")
    def _check_options(self) -> Self:
        if not 2 <= len(self.criteria) <= CHOICE_MAX_OPTIONS:
            raise ValueError(f"choice criteria must have 2..{CHOICE_MAX_OPTIONS} options, got {len(self.criteria)}")
        return self


class ScoreQuestion(_Strict):
    type: Literal["score"] = "score"
    instructions: str = Field(min_length=1)
    criteria: tuple[str, ...]

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        if not SCORE_MIN_LEVELS <= len(self.criteria) <= SCORE_MAX_LEVELS:
            raise ValueError(
                f"score criteria must have {SCORE_MIN_LEVELS}..{SCORE_MAX_LEVELS} levels, got {len(self.criteria)}"
            )
        return self


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class ApiPayload(TypedDict):
    """公式 API の request body。"""

    state: dict[str, str]
    model: str
    questions: dict[str, dict[str, object]]


def _check_purpose(purpose: str, finding_ids: tuple[str, ...]) -> None:
    """finding は 1 件、pair は異なる 2 件の finding_ids を持つことを検査する。"""
    expected = 1 if purpose == "finding" else 2
    if len(finding_ids) != expected or len(set(finding_ids)) != expected:
        raise ValueError(f"a {purpose} needs exactly {expected} distinct finding id(s)")


class SystemOneRequest(_Strict):
    version: Literal[1] = 1
    model: str = Field(min_length=1)
    state: dict[str, str]
    questions: dict[str, Question]
    state_sha256: str = Field(pattern=_SHA256)
    question_set_version: str = Field(min_length=1)
    purpose: Literal["finding", "pair"]
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.state:
            raise ValueError("state must not be empty")
        for key in self.state:
            if not SECTION_KEY_PATTERN.fullmatch(key):
                raise ValueError(f"invalid state section key: {key!r}")
        if not self.questions:
            raise ValueError("questions must not be empty")
        for question_id in self.questions:
            if not QUESTION_ID_PATTERN.fullmatch(question_id):
                raise ValueError(f"invalid question id: {question_id!r}")
        _check_purpose(self.purpose, self.finding_ids)
        return self

    def to_api_payload(self) -> ApiPayload:
        """公式 API の request body（state / model / questions だけ）を返す。``criteria=None`` は省く。"""
        questions: dict[str, dict[str, object]] = {
            question_id: question.model_dump(mode="json", exclude_none=True)
            for question_id, question in self.questions.items()
        }
        return {"state": dict(self.state), "model": self.model, "questions": questions}

    def payload_bytes(self) -> int:
        return len(json.dumps(self.to_api_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class NoulAnswer(_FromApi):
    type: Literal["noul"]
    noul: float = Field(ge=0.0, le=1.0)


class ChoiceAnswer(_FromApi):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float] | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class ScoreAnswer(_FromApi):
    type: Literal["score"]
    score: float
    legend: dict[str, str] | None = None
    probabilities: dict[str, float] | None = None
    confidence: float = Field(ge=0.0, le=1.0)


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class Usage(_FromApi):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class SystemOneResponse(_FromApi):
    model: str = Field(min_length=1)
    answers: dict[str, Answer]
    usage: Usage
    request_id: str | None = None

    def check_against(self, request: SystemOneRequest) -> tuple[str, ...]:
        """request の質問と answers の整合を検査し、問題を列挙する（空なら整合）。"""
        problems: list[str] = []
        for question_id, question in request.questions.items():
            answer = self.answers.get(question_id)
            if answer is None:
                problems.append(f"missing answer: {question_id}")
                continue
            if answer.type != question.type:
                problems.append(f"type mismatch: {question_id} expected {question.type} got {answer.type}")
                continue
            if isinstance(question, ChoiceQuestion) and isinstance(answer, ChoiceAnswer):
                if answer.choice not in question.criteria:
                    problems.append(f"choice outside criteria: {question_id} -> {answer.choice}")
        for question_id in self.answers:
            if question_id not in request.questions:
                problems.append(f"unknown answer: {question_id}")
        return tuple(problems)


class ClientError(_Strict):
    kind: ErrorKind
    http_status: int | None = None
    message: str = Field(max_length=200)  # サニタイズ済みの短い理由。キー・state・応答本文を含めない
    retryable: bool


class ClientOutcome(_Strict):
    version: Literal[1] = 1
    ok: bool
    response: SystemOneResponse | None = None
    error: ClientError | None = None
    model_requested: str = Field(min_length=1)
    request_sha256: str = Field(pattern=_SHA256)
    purpose: Literal["finding", "pair"]
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    elapsed_ms: int | None = Field(default=None, ge=0)
    calls_made: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.ok != (self.response is not None) or self.ok != (self.error is None):
            raise ValueError("ok must be True exactly when response is present and error is absent")
        _check_purpose(self.purpose, self.finding_ids)
        return self


class HttpRequest(_Strict):
    method: Literal["POST"] = "POST"
    url: str = Field(min_length=1)
    headers: dict[str, str]
    body: bytes
    timeout_s: float = Field(gt=0)


class HttpResponse(_Strict):
    status: int = Field(ge=100, le=599)
    headers: dict[str, str]
    body: bytes
    final_url: str = Field(min_length=1)


class Transport(Protocol):
    """client が差し替える HTTP 層。redirect を追わず 3xx をそのまま返す。"""

    def send(self, request: HttpRequest) -> HttpResponse: ...


_HEAD = r"^[0-9a-f]{7,40}$"


class ReviewHistoryRow(_Strict):
    reviewed_at: str = Field(min_length=1)
    head: str = Field(pattern=_HEAD)
    mode: str = Field(min_length=1)


class ReviewFile(_Strict):
    source_file: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    target_pr: int | None = Field(default=None, ge=1)
    header_head: str | None = Field(default=None, pattern=_HEAD)
    header_date: str | None = None
    history: tuple[ReviewHistoryRow, ...] = ()


class ReviewFinding(_Strict):
    finding_id: str = Field(min_length=1)
    finding_key: str = Field(pattern=_HEX12)
    repository: Repository
    review_dir: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    reviewed_head: str | None = Field(default=None, pattern=_HEAD)
    detected_head: str | None = Field(default=None, pattern=_HEAD)
    severity: Severity
    status: FindingStatus
    code: str = Field(pattern=r"^[A-Z]{1,2}-[0-9]+$")
    title: str = Field(min_length=1)
    marker: Marker
    body: str = Field(min_length=1)
    location: str | None = None
    content: str | None = None
    evidence: str | None = None
    recommendation: str | None = None
    missing: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must not precede line_start")
        return self


def finding_key(repository: Repository, review_dir: str, finding_id: str) -> str:
    """質問 ID に使う 12 桁 hex。repository / review_dir / finding_id から決定論的に作る。"""
    digest = hashlib.sha256(f"{repository.value}\n{review_dir}\n{finding_id}".encode()).hexdigest()
    return digest[:12]


class UnparsedBlock(_Strict):
    source_file: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    text_sha256: str = Field(pattern=_SHA256)
    reason: str = Field(min_length=1)


class ReviewBatch(_Strict):
    version: Literal[1] = 1
    repository: Repository
    review_dir: str = Field(min_length=1)
    reviewed_head: str = Field(pattern=_HEAD)
    files: tuple[ReviewFile, ...]
    findings: tuple[ReviewFinding, ...] = ()
    unparsed: tuple[UnparsedBlock, ...] = ()
    anomalies: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        file_names = {review_file.source_file for review_file in self.files}
        ids: set[str] = set()
        keys: set[str] = set()
        for finding in self.findings:
            if finding.finding_id in ids:
                raise ValueError(f"duplicate finding_id: {finding.finding_id}")
            if finding.finding_key in keys:
                raise ValueError(f"duplicate finding_key: {finding.finding_key}")
            if finding.source_file not in file_names:
                raise ValueError(f"finding {finding.finding_id} refers to unknown file {finding.source_file}")
            ids.add(finding.finding_id)
            keys.add(finding.finding_key)
        return self

    def active_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.status is FindingStatus.ACTIVE)

    def finding(self, finding_id: str) -> ReviewFinding:
        for finding in self.findings:
            if finding.finding_id == finding_id:
                return finding
        raise KeyError(finding_id)


class StateSource(_Strict):
    kind: Literal["review", "diff", "blob", "context"]
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    byte_count: int = Field(ge=0)


def canonical_sections(sections: Mapping[str, str]) -> bytes:
    """節の順序を保った compact JSON（UTF-8）。ハッシュとバイト数の基準。"""
    return json.dumps(dict(sections), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def state_digest(sections: Mapping[str, str]) -> tuple[str, int, int]:
    """(content_sha256, char_count, byte_count) を返す。#383 が PreparedState を作るときに使う。"""
    canonical = canonical_sections(sections)
    return hashlib.sha256(canonical).hexdigest(), sum(len(value) for value in sections.values()), len(canonical)


class PreparedState(_Strict):
    version: Literal[1] = 1
    purpose: Literal["finding", "pair"]
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    sections: dict[str, str]
    sources: tuple[StateSource, ...] = ()
    content_sha256: str = Field(pattern=_SHA256)
    char_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    allowed: bool
    allowed_by: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.sections:
            raise ValueError("sections must not be empty")
        for key in self.sections:
            if not SECTION_KEY_PATTERN.fullmatch(key):
                raise ValueError(f"invalid section key: {key!r}")
        if state_digest(self.sections) != (self.content_sha256, self.char_count, self.byte_count):
            raise ValueError("content_sha256 / char_count / byte_count do not match sections")
        _check_purpose(self.purpose, self.finding_ids)
        if self.allowed:
            if self.blocked_reasons:
                raise ValueError("an allowed state cannot carry blocked_reasons")
            if self.content_sha256 not in self.allowed_by:
                raise ValueError("an allowed state must list its own content_sha256 in allowed_by")
        elif not self.blocked_reasons:
            raise ValueError("a blocked state must give at least one reason")
        return self


class Thresholds(_Strict):
    version: Literal[1] = 1
    model_id: str = Field(min_length=1)
    choice_min_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    score_min_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    noul_yes_min: float = Field(default=0.95, ge=0.0, le=1.0)
    noul_no_max: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.noul_no_max >= self.noul_yes_min:
            raise ValueError("noul_no_max must be below noul_yes_min")
        return self


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 5,
    Severity.HIGH: 4,
    Severity.MEDIUM: 3,
    Severity.LOW: 2,
    Severity.INFO: 1,
    Severity.UNKNOWN: 0,
}


class SeveritySummary(_Strict):
    by_model: dict[str, Severity]
    highest: Severity
    lowest: Severity
    agreement: bool

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.by_model:
            raise ValueError("by_model must not be empty")
        ranked = sorted(self.by_model.values(), key=lambda severity: _SEVERITY_RANK[severity])
        if (ranked[-1], ranked[0]) != (self.highest, self.lowest):
            raise ValueError("highest / lowest do not match by_model")
        if self.agreement != (len(set(self.by_model.values())) == 1):
            raise ValueError("agreement does not match by_model")
        return self


def summarize_severity(by_model: Mapping[str, Severity]) -> SeveritySummary:
    """元レビューの severity から決定論的に要約する。Jev の Score は使わない。"""
    if not by_model:
        raise ValueError("by_model must not be empty")
    ranked = sorted(by_model.values(), key=lambda severity: _SEVERITY_RANK[severity])
    return SeveritySummary(
        by_model=dict(by_model),
        highest=ranked[-1],
        lowest=ranked[0],
        agreement=len(set(by_model.values())) == 1,
    )


class TriageDecision(_Strict):
    finding_id: str = Field(min_length=1)
    proposal: Proposal
    duplicate_candidates: tuple[str, ...] = ()
    severity_summary: SeveritySummary
    evidence_refs: tuple[str, ...] = ()
    escalation_reasons: tuple[str, ...] = ()
    answers: dict[str, Answer] = Field(default_factory=dict)
    advisory: Literal[True] = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.proposal is Proposal.HUMAN_REVIEW and not self.escalation_reasons:
            raise ValueError("human_review needs at least one escalation reason")
        return self


class TriageReport(_Strict):
    version: Literal[1] = 1
    repository: Repository
    review_dir: str = Field(min_length=1)
    reviewed_head: str = Field(pattern=_HEAD)
    model_id_requested: str = Field(min_length=1)
    model_id_responded: str | None = None
    question_set_version: str = Field(min_length=1)
    thresholds: Thresholds
    status: ReportStatus
    reason: str = Field(min_length=1)
    decisions: tuple[TriageDecision, ...] = ()
    unparsed_count: int = Field(ge=0)
    decided_count: int = Field(default=0, ge=0)
    unverified_count: int = Field(default=0, ge=0)
    generated_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        finding_ids = [decision.finding_id for decision in self.decisions]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("decisions must have unique finding ids")
        if self.status is not ReportStatus.ADVISORY:
            if any(decision.proposal is not Proposal.HUMAN_REVIEW for decision in self.decisions):
                raise ValueError(f"status {self.status.value} requires every decision to be human_review")
        return self
