"""質問セット v1 と SystemOneRequest の組み立て。

質問文は英語で固定し、state の節キーをバッククォートで参照する。1 質問 1 命題。
``applies`` の回答は「引用された committed 内容に照らして主張が事実か」の意味に限り、再現検証の代わりにしない。
"""

import re
from collections.abc import Mapping
from typing import Literal

from tools.review_triage.models import (
    QUESTION_ID_PATTERN,
    STATE_CHAR_LIMIT,
    ChoiceQuestion,
    NoulQuestion,
    PreparedState,
    Question,
    SystemOneRequest,
)

QUESTION_SET_VERSION = "1"
OPTIONAL_SECTION_KEYS = frozenset({"diff", "evidence_a", "evidence_b"})
_KEY = re.compile(r"^[0-9a-f]{12}$")
_BACKTICKED = re.compile(r"`([a-z][a-z0-9_]*)`")

APPLIES_INSTRUCTIONS = (
    "Using only `evidence` (committed content at the target HEAD) and `diff` when present, "
    "is the claim made in `finding` factually true for that content?"
)
APPLIES_CRITERIA: dict[Literal["true", "false"], str] = {
    "true": "The cited code or behaviour exists at the target HEAD as the finding describes.",
    "false": "The evidence does not show it, or the evidence contradicts the finding.",
}
PROPOSAL_INSTRUCTIONS = "Judged only from `finding` and `evidence`, which handling does this finding deserve?"
PROPOSAL_CRITERIA: dict[str, str | None] = {
    "fix_now": "A concrete defect or factual error at the target HEAD that the same change should fix.",
    "separate_issue": "A real but separable improvement that deserves its own issue.",
    "reject": "Not a defect: the evidence contradicts it, or it asks for behaviour the project does not want.",
    "human_review": "Cannot be settled from the evidence alone; a person must look at it.",
}
SAME_INSTRUCTIONS = "Do `finding_a` and `finding_b` describe the same underlying problem at the same location?"


class NotAllowedStateError(ValueError):
    """未許可の state を送信用 request にしようとした。"""


class StateTooLargeError(ValueError):
    """state が STATE_CHAR_LIMIT を超えている。"""


def _check_key(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise ValueError(f"finding key must be 12 hex chars: {key!r}")
    return key


def applies_question_id(key: str) -> str:
    return f"applies_{_check_key(key)}"


def proposal_question_id(key: str) -> str:
    return f"proposal_{_check_key(key)}"


def same_question_id(key_a: str, key_b: str) -> str:
    first, second = sorted((_check_key(key_a), _check_key(key_b)))
    if first == second:
        raise ValueError("a pair needs two different finding keys")
    return f"same_{first}_{second}"


def question_set_for_finding(key: str) -> dict[str, Question]:
    return {
        applies_question_id(key): NoulQuestion(instructions=APPLIES_INSTRUCTIONS, criteria=dict(APPLIES_CRITERIA)),
        proposal_question_id(key): ChoiceQuestion(
            instructions=PROPOSAL_INSTRUCTIONS, criteria=dict(PROPOSAL_CRITERIA)
        ),
    }


def question_set_for_pair(key_a: str, key_b: str) -> dict[str, Question]:
    return {same_question_id(key_a, key_b): NoulQuestion(instructions=SAME_INSTRUCTIONS)}


def referenced_sections(question: Question) -> frozenset[str]:
    """指示文がバッククォートで参照する state の節キー。"""
    return frozenset(_BACKTICKED.findall(question.instructions))


def build_request(state: PreparedState, *, questions: Mapping[str, Question], model: str) -> SystemOneRequest:
    """許可済みで上限内の state と質問から送信用 request を組み立てる純粋関数。"""
    if not state.allowed:
        reasons = ", ".join(state.blocked_reasons)
        raise NotAllowedStateError(f"state {state.content_sha256[:12]} is not allowed to be sent: {reasons}")
    if state.char_count > STATE_CHAR_LIMIT:
        raise StateTooLargeError(f"state has {state.char_count} chars, limit is {STATE_CHAR_LIMIT}")
    if not questions:
        raise ValueError("questions must not be empty")
    for question_id, question in questions.items():
        if not QUESTION_ID_PATTERN.fullmatch(question_id):
            raise ValueError(f"invalid question id: {question_id!r}")
        missing = referenced_sections(question) - OPTIONAL_SECTION_KEYS - set(state.sections)
        if missing:
            raise ValueError(f"question {question_id} references missing state sections: {sorted(missing)}")
    return SystemOneRequest(
        model=model,
        state=dict(state.sections),
        questions=dict(questions),
        state_sha256=state.content_sha256,
        question_set_version=QUESTION_SET_VERSION,
        purpose=state.purpose,
        finding_ids=state.finding_ids,
    )
