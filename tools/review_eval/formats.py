"""収集した記録の形式の判別と、対象 SHA・文書の `pr` の取り出し（推測で埋めず、無ければ理由を残す）。"""

import json
import re
from dataclasses import dataclass
from typing import cast

from tools.review_eval.models import FormatInfo, TargetInfo

FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_JSON_BLOCK = re.compile(r"```json\n(?P<body>.*?)\n```", re.DOTALL)
_TIMING_HEADING = "\n## Review timing\n"
_LOOP_SCHEMA = "pr-review-fix-loop/v1"
_FINDINGS_KINDS: frozenset[str | None] = frozenset(
    {"candidates", "verified", "planning", "statement_candidates", "review_run"}
)
_KNOWN_KINDS: dict[str, frozenset[str | None]] = {
    "review-findings/v1": _FINDINGS_KINDS,
    "review-findings/v2": _FINDINGS_KINDS,
    _LOOP_SCHEMA: frozenset({"run", "cycle", "diagnostic"}),
    "review-records/v1": frozenset({None}),
    "review-records/v2": frozenset({None}),
}


UNKNOWN_FORMAT = FormatInfo(schema=None, kind=None, known=False)


@dataclass(frozen=True)
class Described:
    format: FormatInfo
    target: TargetInfo
    document: dict[str, object] | None


def _parse_json(data: bytes) -> dict[str, object] | None:
    try:
        value: object = json.loads(data)
    except ValueError, RecursionError:
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _timing_block(data: bytes) -> dict[str, object] | None:
    """レビューファイルの `## Review timing` の JSON ブロック（pr-review の helper が書く）。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    index = text.rfind(_TIMING_HEADING)
    match = _JSON_BLOCK.search(text, index) if index >= 0 else None
    return _parse_json(match["body"].encode("utf-8")) if match else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _missing(reason: str) -> TargetInfo:
    return TargetInfo(reviewed_head=None, base=None, source_key=None, missing_reason=reason)


def _target(head: object, base: object, key: str) -> TargetInfo:
    reviewed = head if isinstance(head, str) and FULL_SHA.fullmatch(head) else None
    merge_base = base if isinstance(base, str) and FULL_SHA.fullmatch(base) else None
    if reviewed is None:
        reason = "not_recorded" if head is None else "not_full_sha"
        return TargetInfo(reviewed_head=None, base=merge_base, source_key=None, missing_reason=reason)
    return TargetInfo(reviewed_head=reviewed, base=merge_base, source_key=key, missing_reason=None)


def describe(path: str, data: bytes | None) -> Described:
    """`path` は review_dir からの相対か `@` 始まりの仮想記録。`data` が None（大きすぎて読まない）なら不明。"""
    if data is None:
        return Described(UNKNOWN_FORMAT, _missing("not_read"), None)
    document = _parse_json(data) if path.startswith("@") or path.endswith(".json") else None
    if document is not None:
        schema, kind = _text(document.get("schema")), _text(document.get("kind"))
        known = schema is not None and kind in _KNOWN_KINDS.get(schema, frozenset())
        info = FormatInfo(schema=schema, kind=kind, known=known)
        if schema == _LOOP_SCHEMA and kind == "run":
            return Described(info, _missing("not_applicable"), document)
        key = "head" if schema == _LOOP_SCHEMA and kind == "cycle" else "reviewed_head"
        return Described(info, _target(document.get(key), document.get("base"), key), document)
    if "/" not in path and not path.startswith("@") and path.endswith(".md"):
        timing = _timing_block(data)
        info = FormatInfo(schema=None, kind="review_markdown", known=timing is not None)
        if timing is None:
            return Described(info, _missing("not_recorded"), None)
        return Described(info, _target(timing.get("reviewed_head"), None, "review_timing.reviewed_head"), None)
    return Described(UNKNOWN_FORMAT, _missing("not_recorded"), None)


def pr_mismatch(document: dict[str, object] | None, repository: str, number: int) -> bool:
    """文書の `pr` が場所と食い違うか。`pr.repository`・`pr.number` のあるものを照合し、型の誤りも食い違いとする。"""
    pr = document.get("pr") if document is not None else None
    if pr is None:
        return False
    if not isinstance(pr, dict):
        return True
    fields = cast(dict[str, object], pr)
    if "repository" in fields:
        claimed = fields["repository"]
        if not isinstance(claimed, str) or claimed.casefold() != repository.casefold():
            return True
    if "number" in fields:
        claimed = fields["number"]
        if not isinstance(claimed, int) or isinstance(claimed, bool) or claimed != number:
            return True
    return False


def withheld(described: Described) -> Described:
    """本文を保存しない observation に残す形。

    検査に通らなかった本文から取った任意の文字列（未知の形式名・SHA）を残さず、既知の形式名だけを残す。
    """
    info = described.format if described.format.known else UNKNOWN_FORMAT
    target = described.target if described.target.missing_reason == "not_read" else _missing("withheld")
    return Described(info, target, None)
