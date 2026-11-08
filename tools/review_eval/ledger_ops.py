"""確認待ち台帳（`review-eval-ledger/v1`）の op のモデルと値の一覧（Spec 5 章）。

journal の 1 行が 1 つの op である。`schema`・`seq`・`at`・`by` は台帳が付ける。今の状態に対する検査と fold は
`ledger.py` が行う。主張の id・packet の id・item の id は `new_id()` で作る（内容やラベルを符号化しない）。
"""

import secrets
from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from tools.review_eval.canonical import hjson

LEDGER_SCHEMA = "review-eval-ledger/v1"
MAX_STATE_BYTES = 16384

Label = Literal["supports", "contradicts", "not_addressed", "insufficient_context"]
LABELS: tuple[Label, ...] = get_args(Label)
CandidateStatus = Literal["pending", "selected", "not_selected", "no_permission", "missing", "failed", "held"]
CANDIDATE_STATUSES: tuple[CandidateStatus, ...] = get_args(CandidateStatus)
CandidateKind = Literal["finding", "unverified_candidate", "new_problem", "markdown_only"]
CANDIDATE_KINDS: tuple[CandidateKind, ...] = get_args(CandidateKind)
Relation = Literal["same_defect", "before_after", "near_duplicate", "same_pr_issue", "template_derived"]
RELATIONS: tuple[Relation, ...] = get_args(Relation)
Scope = Literal["local_use", "runtime_presentation", "publication", "external_send"]
SCOPES: tuple[Scope, ...] = get_args(Scope)
ExposureKind = Literal["pilot", "tuning", "known", "seen"]
EXPOSURE_KINDS: tuple[ExposureKind, ...] = get_args(ExposureKind)
Split = Literal["calibration", "held_out", "excluded"]
SPLITS: tuple[Split, ...] = get_args(Split)
Role = Literal["g1", "g2", "adjudicator", "evaluator"]
ROLES: tuple[Role, ...] = get_args(Role)
GOLD_ROLES: frozenset[Role] = frozenset({"g1", "g2", "adjudicator"})
Stage = Literal["selection", "anonymization", "annotation", "adjudication", "storage", "other"]
STAGES: tuple[Stage, ...] = get_args(Stage)

# 空白だけでない文字列（人・担当の識別、理由など）
Text = Annotated[str, StringConstraints(pattern=r"\S")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Timestamp = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]


class LedgerError(Exception):
    """台帳の op の拒否・journal の破損。メッセージに主張の本文を含めない。"""


def new_id() -> str:
    """主張・packet・item の id（乱数。内容・正解・担当を含めない）。"""
    return secrets.token_hex(16)


def identity_key(value: str) -> str:
    """人・担当の識別子の照合に使う形（空白の違いと大文字・小文字を区別しない）。worker と by は同じ空間で照合する。"""
    return " ".join(value.split()).casefold()


def candidate_identity(repository: str, pr: int, review_dir: str, ref: str, item_sha256: str) -> str:
    """同じ ref でも項目の内容が違えば別の候補にする（同じ HEAD の再レビューで同じ ID が別の内容を指す場合）。"""
    return hjson(
        {
            "repository": repository.casefold(),
            "pr": pr,
            "review_dir": review_dir,
            "ref": ref,
            "item_sha256": item_sha256,
        }
    )


def members_identity(members: list[str]) -> str:
    return hjson(sorted(members))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class Evidence(_Model):
    """根拠 1 件。SHA・repo を持たず、主張の target の 2 つの時点（before・after）だけを指す。"""

    at: Literal["before", "after"]
    path: Text
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line は start_line 以上にする")
        return self


class ClaimState(_Model):
    """単一主張の leaf の内容。匿名化（パス・名前の置き換え）は人が書くときに行う。"""

    claim: Text
    diff: str
    evidence: list[Evidence]


class Target(_Model):
    """主張の対象（管理用。state に入れない）。repository は親の候補から決める。"""

    repository: Text
    before_sha: CommitSha | None = None
    after_sha: CommitSha


class _Op(_Model):
    schema_: Literal["review-eval-ledger/v1"] = Field(alias="schema")
    seq: int = Field(ge=1)
    at: Timestamp
    by: Text


class CandidateSync(_Op):
    op: Literal["candidate.sync"]
    candidate_id: Sha256
    repository: Text
    pr: int = Field(ge=1)
    review_dir: Text
    ref: Text
    item_sha256: Sha256
    kind: CandidateKind
    observation_id: Sha256
    path: Text


class CandidateStatusSet(_Op):
    op: Literal["candidate.status"]
    candidate_id: Sha256
    status: CandidateStatus
    reason: str | None = None


class ClaimCreate(_Op):
    op: Literal["claim.create"]
    claim_id: Identifier
    candidates: list[Sha256] = Field(min_length=1)
    pr: int = Field(ge=1)
    target: Target
    issue: int | None = Field(default=None, ge=1)
    language: Text
    state: ClaimState
    state_sha256: Sha256


class ClaimRevise(_Op):
    op: Literal["claim.revise"]
    claim_id: Identifier
    revision: int = Field(ge=2)
    state: ClaimState
    state_sha256: Sha256


class ClaimConfirm(_Op):
    op: Literal["claim.confirm"]
    claim_id: Identifier
    revision: int = Field(ge=1)
    state_sha256: Sha256


class RelationAdd(_Op):
    op: Literal["relation.add"]
    left: Identifier
    right: Identifier
    relation: Relation
    note: str | None = None


class FamilyConfirm(_Op):
    op: Literal["family.confirm"]
    claim_id: Identifier
    members: list[Identifier] = Field(min_length=1)
    members_sha256: Sha256


class PermissionSet(_Op):
    """対象は主張 id か `repo:<owner>/<repo>`。runtime は runtime_presentation のときだけ（必須）。"""

    op: Literal["permission.set"]
    subject: Text
    scope: Scope
    runtime: Text | None = None
    grant: bool
    note: str | None = None


class ExposureAdd(_Op):
    op: Literal["exposure.add"]
    claim_id: Identifier
    who: Text
    kind: ExposureKind
    note: str | None = None


class SplitAssign(_Op):
    op: Literal["split.assign"]
    claim_id: Identifier
    split: Split
    reason: Text
    members: list[Identifier] = Field(min_length=1)
    members_sha256: Sha256


class PacketItem(_Model):
    """packet の item と主張の対応（管理用。journal にだけ残し、packet.json には書かない）。"""

    item_id: Identifier
    claim_id: Identifier
    revision: int = Field(ge=1)
    state_sha256: Sha256


class PacketExport(_Op):
    """packet の書き出し。各 item の主張に、worker の `seen` の露出を記録したことになる。"""

    op: Literal["packet.export"]
    packet_id: Identifier
    role: Role
    worker: Text
    runtime: Text
    items: list[PacketItem] = Field(min_length=1)


class Answer(_Model):
    item_id: Identifier
    label: Label
    rationale: str
    evidence_refs: list[str]
    major_counterexample: str | None = None


class AnswerImport(_Op):
    """回答（`review-eval-answers/v1`）の取り込み。answers_sha256 は取り込んだファイルの Hfile（任意）。"""

    op: Literal["answer.import"]
    packet_id: Identifier
    worker: Text
    answers: list[Answer] = Field(min_length=1)
    answers_sha256: Sha256 | None = None


class TimeAdd(_Op):
    op: Literal["time.add"]
    stage: Stage
    seconds: int = Field(ge=0)
    claims: list[Identifier] = Field(default_factory=list[str])
    note: str | None = None


type Op = Annotated[
    CandidateSync
    | CandidateStatusSet
    | ClaimCreate
    | ClaimRevise
    | ClaimConfirm
    | RelationAdd
    | FamilyConfirm
    | PermissionSet
    | ExposureAdd
    | SplitAssign
    | PacketExport
    | AnswerImport
    | TimeAdd,
    Field(discriminator="op"),
]
OP_ADAPTER: TypeAdapter[Op] = TypeAdapter(Op)
