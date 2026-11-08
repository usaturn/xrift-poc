"""保存先に置く記録（observation・failure）のモデル。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tools.review_eval.canonical import hjson
from tools.review_eval.config import Route

Body = Literal["stored", "withheld_secret"]
Trigger = Literal["save", "sweep", "manual"]
FailureKind = Literal[
    "store_not_initialized",
    "identity_mismatch",
    "legacy_layout",
    "out_of_period",
    "secret_withheld",
    "too_large",
    "scan_failed",
    "capacity_held",
    "sweep_limit",
    "lock_timeout",
    "lost_revision",
    "unexpected_error",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FormatInfo(_Model):
    schema_: str | None = Field(alias="schema")
    kind: str | None
    known: bool


class TargetInfo(_Model):
    reviewed_head: str | None
    base: str | None
    source_key: str | None
    missing_reason: str | None


class InputRef(_Model):
    path: str
    sha256: str


class Observation(_Model):
    schema_: Literal["review-eval-observation/v1"] = Field(alias="schema")
    observation_id: str
    repository: str
    pr: int
    review_dir: str | None
    path: str
    sha256: str
    bytes: int
    body: Body
    format: FormatInfo
    target: TargetInfo
    observed_at: str
    file_mtime: str | None
    trigger: Trigger
    route: Route
    event: str
    context: dict[str, JsonValue]
    inputs: list[InputRef]
    collector: dict[str, str]


def observation_identity(repository: str, pr: int, review_dir: str | None, path: str, sha256: str) -> str:
    """同じ版を同じ id にする。`@` 始まりの仮想記録は review_dir を含めない。"""
    directory = None if path.startswith("@") else review_dir
    return hjson(
        {"repository": repository.casefold(), "pr": pr, "review_dir": directory, "path": path, "sha256": sha256}
    )


class Failure(_Model):
    schema_: Literal["review-eval-failure/v1"] = Field(alias="schema")
    kind: FailureKind
    at: str
    repository: str | None
    pr: int | None
    review_dir: str | None
    path: str | None
    sha256: str | None
    route: Route | None
    event: str | None
    reason: str
