import json

import pytest

from tools.review_triage.models import (
    DEFAULT_MODEL_ID,
    QUESTION_ID_PATTERN,
    STATE_CHAR_LIMIT,
    ChoiceQuestion,
    NoulQuestion,
    PreparedState,
    Proposal,
    state_digest,
)
from tools.review_triage.questions import (
    QUESTION_SET_VERSION,
    NotAllowedStateError,
    StateTooLargeError,
    applies_question_id,
    build_request,
    proposal_question_id,
    question_set_for_finding,
    question_set_for_pair,
    referenced_sections,
    same_question_id,
)

FINDING_A = "0123456789ab"
FINDING_B = "ba9876543210"


def _state(sections: dict[str, str], *, allowed: bool = True, purpose: str = "finding") -> PreparedState:
    digest, chars, size = state_digest(sections)
    return PreparedState.model_validate(
        {
            "purpose": purpose,
            "finding_ids": ["a/active/L-1"] if purpose == "finding" else ["a/active/L-1", "b/active/L-2"],
            "sections": sections,
            "content_sha256": digest,
            "char_count": chars,
            "byte_count": size,
            "allowed": allowed,
            "allowed_by": [digest] if allowed else [],
            "blocked_reasons": [] if allowed else ["hash not allowed"],
        }
    )


def test_question_ids_are_deterministic_and_valid() -> None:
    assert applies_question_id(FINDING_A) == f"applies_{FINDING_A}" == applies_question_id(FINDING_A)
    assert proposal_question_id(FINDING_A) == f"proposal_{FINDING_A}"
    for question_id in (
        applies_question_id(FINDING_A),
        proposal_question_id(FINDING_A),
        same_question_id(FINDING_A, FINDING_B),
    ):
        assert QUESTION_ID_PATTERN.match(question_id)
    assert applies_question_id(FINDING_A) != applies_question_id(FINDING_B)
    with pytest.raises(ValueError):
        applies_question_id("not-hex")
    with pytest.raises(ValueError):
        applies_question_id(f"{FINDING_A}\n")
    with pytest.raises(ValueError):
        same_question_id(f"{FINDING_A}\n", FINDING_B)


def test_question_sets_have_fixed_shape_and_pair_order_is_normalized() -> None:
    finding_set = question_set_for_finding(FINDING_A)
    assert list(finding_set) == [applies_question_id(FINDING_A), proposal_question_id(FINDING_A)]
    applies = finding_set[applies_question_id(FINDING_A)]
    assert isinstance(applies, NoulQuestion) and applies.criteria is not None
    assert list(applies.criteria) == ["true", "false"]
    proposal = finding_set[proposal_question_id(FINDING_A)]
    assert isinstance(proposal, ChoiceQuestion)
    # policy は choice を Proposal として読むため、選択肢は Proposal の値と一致させる
    assert set(proposal.criteria) == {member.value for member in Proposal}
    assert same_question_id(FINDING_A, FINDING_B) == same_question_id(FINDING_B, FINDING_A)
    assert list(question_set_for_pair(FINDING_B, FINDING_A)) == [same_question_id(FINDING_A, FINDING_B)]
    with pytest.raises(ValueError):
        same_question_id(FINDING_A, FINDING_A)


def test_build_request_refuses_blocked_or_oversized_state() -> None:
    questions = question_set_for_finding(FINDING_A)
    blocked = _state({"finding": "x", "evidence": "y"}, allowed=False)
    with pytest.raises(NotAllowedStateError):
        build_request(blocked, questions=questions, model=DEFAULT_MODEL_ID)
    oversized = _state({"finding": "あ" * (STATE_CHAR_LIMIT + 1), "evidence": "y"})
    with pytest.raises(StateTooLargeError):
        build_request(oversized, questions=questions, model=DEFAULT_MODEL_ID)


def test_build_request_payload_matches_api_shape() -> None:
    state = _state({"finding": "指摘", "evidence": "抜粋"})
    request = build_request(state, questions=question_set_for_finding(FINDING_A), model=DEFAULT_MODEL_ID)
    assert request.state_sha256 == state.content_sha256
    assert request.question_set_version == QUESTION_SET_VERSION
    assert (request.purpose, request.finding_ids) == ("finding", ("a/active/L-1",))
    payload = request.to_api_payload()
    assert set(payload) == {"state", "model", "questions"}
    assert payload["model"] == "jev-latest"
    applies = payload["questions"][applies_question_id(FINDING_A)]
    assert set(applies) == {"type", "instructions", "criteria"}
    pair_request = build_request(
        _state({"finding_a": "a", "finding_b": "b"}, purpose="pair"),
        questions=question_set_for_pair(FINDING_A, FINDING_B),
        model=DEFAULT_MODEL_ID,
    )
    same_id = same_question_id(FINDING_A, FINDING_B)
    same = pair_request.to_api_payload()["questions"]
    assert same[same_id] == {"type": "noul", "instructions": pair_request.questions[same_id].instructions}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert request.payload_bytes() == len(encoded)


def test_build_request_requires_referenced_sections() -> None:
    synthetic = NoulQuestion(instructions="Do `finding_a` and `diff2` match `finding_a`, unlike `Bad` or ` x `?")
    assert referenced_sections(synthetic) == frozenset({"finding_a", "diff2"})
    questions = question_set_for_finding(FINDING_A)
    with pytest.raises(ValueError, match="missing state sections"):
        build_request(_state({"finding": "x"}), questions=questions, model=DEFAULT_MODEL_ID)
    request = build_request(_state({"finding": "x", "evidence": "y"}), questions=questions, model=DEFAULT_MODEL_ID)
    assert "diff" not in request.state


def test_build_request_rejects_question_id_with_trailing_newline() -> None:
    state = _state({"finding": "x", "evidence": "y"})
    questions = {f"{applies_question_id(FINDING_A)}\n": NoulQuestion(instructions="Is `finding` true?")}
    with pytest.raises(ValueError, match="invalid question id"):
        build_request(state, questions=questions, model=DEFAULT_MODEL_ID)


def test_instructions_are_single_propositions() -> None:
    # 「1 質問 1 命題」は設計上の規則（interfaces.rst・questions.py）。全質問の文が ? をちょうど 1 つ持つことを確かめる
    all_questions = {**question_set_for_finding(FINDING_A), **question_set_for_pair(FINDING_A, FINDING_B)}
    for question in all_questions.values():
        assert question.instructions.count("?") == 1
