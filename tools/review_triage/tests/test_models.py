import json

import pytest
from pydantic import ValidationError

from tools.review_triage.models import (
    CHOICE_MAX_OPTIONS,
    ChoiceAnswer,
    ChoiceQuestion,
    ClientError,
    ClientOutcome,
    ErrorKind,
    FindingStatus,
    Marker,
    NoulAnswer,
    NoulQuestion,
    PreparedState,
    Proposal,
    ReportStatus,
    Repository,
    ReviewBatch,
    ReviewFile,
    ReviewFinding,
    ScoreAnswer,
    ScoreQuestion,
    Severity,
    SeveritySummary,
    SystemOneRequest,
    SystemOneResponse,
    Thresholds,
    TriageDecision,
    TriageReport,
    Usage,
    finding_key,
    state_digest,
    summarize_severity,
)

SHA64 = "0" * 64


def _finding_request() -> SystemOneRequest:
    return SystemOneRequest(
        model="jev-latest",
        state={"finding": "指摘本文", "evidence": "抜粋"},
        questions={
            "applies_0123456789ab": NoulQuestion(instructions="Is the claim in `finding` true?"),
            "proposal_0123456789ab": ChoiceQuestion(
                instructions="Which handling does `finding` deserve?",
                criteria={"fix_now": "fix", "reject": "reject"},
            ),
        },
        state_sha256=SHA64,
        question_set_version="1",
        purpose="finding",
        finding_ids=("claude-opus-5/active/L-1",),
    )


def test_choice_question_rejects_one_or_too_many_options() -> None:
    with pytest.raises(ValidationError):
        ChoiceQuestion(instructions="x", criteria={"only": None})
    too_many: dict[str, str | None] = {f"o{i}": None for i in range(CHOICE_MAX_OPTIONS + 1)}
    with pytest.raises(ValidationError):
        ChoiceQuestion(instructions="x", criteria=too_many)
    assert len(ChoiceQuestion(instructions="x", criteria={"a": None, "b": "B"}).criteria) == 2


def test_score_question_rejects_one_or_eleven_levels() -> None:
    with pytest.raises(ValidationError):
        ScoreQuestion(instructions="x", criteria=("one",))
    with pytest.raises(ValidationError):
        ScoreQuestion(instructions="x", criteria=tuple(f"l{i}" for i in range(11)))
    assert len(ScoreQuestion(instructions="x", criteria=("a", "b")).criteria) == 2


def test_request_rejects_bad_question_id_empty_state_and_version_2() -> None:
    good = _finding_request()
    bad_id = {"Bad-Id": {"type": "noul", "instructions": "q"}}
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "questions": bad_id})
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "state": {}})
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "state": {"Bad Key": "v"}})
    newline_id = {"applies_0123456789ab\n": {"type": "noul", "instructions": "q"}}
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "questions": newline_id})
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "state": {"finding\n": "v"}})
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "version": 2})
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**good.model_dump(), "purpose": "pair"})


def test_request_payload_shape_and_bytes() -> None:
    request = _finding_request()
    payload = request.to_api_payload()
    assert set(payload) == {"state", "model", "questions"}
    assert payload["model"] == "jev-latest"
    assert payload["state"] == {"finding": "指摘本文", "evidence": "抜粋"}
    questions = payload["questions"]
    assert questions["applies_0123456789ab"] == {"type": "noul", "instructions": "Is the claim in `finding` true?"}
    assert questions["proposal_0123456789ab"] == {
        "type": "choice",
        "instructions": "Which handling does `finding` deserve?",
        "criteria": {"fix_now": "fix", "reject": "reject"},
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert request.payload_bytes() == len(encoded)


def test_models_are_frozen() -> None:
    request = _finding_request()
    with pytest.raises(ValidationError):
        request.model = "other"  # type: ignore[misc]


def _usage() -> dict[str, int]:
    return {"input_tokens": 1, "output_tokens": 1}


def test_answer_ranges_reject_out_of_range_nan_and_negative_usage() -> None:
    for bad in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            NoulAnswer.model_validate({"type": "noul", "noul": bad})
    with pytest.raises(ValidationError):
        ChoiceAnswer.model_validate({"type": "choice", "choice": "a", "confidence": 1.5})
    with pytest.raises(ValidationError):
        Usage.model_validate({"input_tokens": -1, "output_tokens": 0})
    assert ScoreAnswer.model_validate({"type": "score", "score": 0.5, "confidence": 0.5}).legend is None


def test_check_against_reports_missing_unknown_mismatch_and_choice_outside_criteria() -> None:
    request = _finding_request()
    response = SystemOneResponse.model_validate(
        {
            "model": "jev-latest",
            "answers": {
                "applies_0123456789ab": {"type": "choice", "choice": "x", "confidence": 0.5},
                "proposal_0123456789ab": {"type": "choice", "choice": "not_an_option", "confidence": 0.9},
                "extra_q": {"type": "noul", "noul": 0.1},
            },
            "usage": _usage(),
        }
    )
    problems = response.check_against(request)
    assert any(p.startswith("type mismatch: applies_0123456789ab") for p in problems)
    assert any(p.startswith("choice outside criteria: proposal_0123456789ab") for p in problems)
    assert "unknown answer: extra_q" in problems
    empty = SystemOneResponse.model_validate({"model": "jev-latest", "answers": {}, "usage": _usage()})
    assert set(empty.check_against(request)) == {
        "missing answer: applies_0123456789ab",
        "missing answer: proposal_0123456789ab",
    }
    good = SystemOneResponse.model_validate(
        {
            "model": "jev-latest",
            "answers": {
                "applies_0123456789ab": {"type": "noul", "noul": 0.9},
                "proposal_0123456789ab": {"type": "choice", "choice": "reject", "confidence": 0.9},
            },
            "usage": _usage(),
        }
    )
    assert good.check_against(request) == ()


def test_client_outcome_invariants() -> None:
    error = ClientError(kind=ErrorKind.TIMEOUT, http_status=None, message="timed out", retryable=True)
    response = SystemOneResponse.model_validate({"model": "jev-latest", "answers": {}, "usage": _usage()})
    base: dict[str, object] = {
        "model_requested": "jev-latest",
        "request_sha256": SHA64,
        "purpose": "finding",
        "finding_ids": ["claude-opus-5/active/L-1"],
        "calls_made": 1,
    }
    failed = ClientOutcome.model_validate({**base, "ok": False, "error": error.model_dump()})
    assert failed.error is not None and failed.error.kind is ErrorKind.TIMEOUT
    succeeded = ClientOutcome.model_validate({**base, "ok": True, "response": response.model_dump()})
    assert succeeded.response is not None
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate({**base, "ok": True})
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate({**base, "ok": False})
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate(
            {**base, "ok": True, "response": response.model_dump(), "error": error.model_dump()}
        )
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate({**base, "ok": False, "error": error.model_dump(), "version": 2})
    with pytest.raises(ValidationError):
        ClientError(kind=ErrorKind.SERVICE, http_status=500, message="x" * 201, retryable=True)


def _finding(finding_id: str = "claude-opus-5/active/L-1", **overrides: object) -> ReviewFinding:
    fields: dict[str, object] = {
        "finding_id": finding_id,
        "finding_key": finding_key(Repository.PARENT, "#406-feature-issue-382-rst-writer-foundation", finding_id),
        "repository": Repository.PARENT,
        "review_dir": "#406-feature-issue-382-rst-writer-foundation",
        "source_file": "claude-opus-5.md",
        "model_name": "claude-opus-5",
        "line_start": 40,
        "line_end": 48,
        "reviewed_head": "e0bff6690762",
        "detected_head": "e0bff6690762",
        "severity": Severity.LOW,
        "status": FindingStatus.ACTIVE,
        "code": "L-1",
        "title": "見出し",
        "marker": Marker.VERIFIED,
        "body": "### L-1: 見出し【検証済み】\n- 位置: a.py:1\n",
        "location": "a.py:1",
        "missing": ["根拠"],
    }
    fields.update(overrides)
    return ReviewFinding.model_validate(fields)


def _batch(*findings: ReviewFinding) -> ReviewBatch:
    return ReviewBatch(
        repository=Repository.PARENT,
        review_dir="#406-feature-issue-382-rst-writer-foundation",
        reviewed_head="e0bff6690762",
        files=(ReviewFile(source_file="claude-opus-5.md", model_name="claude-opus-5"),),
        findings=findings,
    )


def _prepared(sections: dict[str, str], *, allowed: bool = True, **overrides: object) -> PreparedState:
    digest, chars, size = state_digest(sections)
    fields: dict[str, object] = {
        "purpose": "finding",
        "finding_ids": ["claude-opus-5/active/L-1"],
        "sections": sections,
        "content_sha256": digest,
        "char_count": chars,
        "byte_count": size,
        "allowed": allowed,
        "allowed_by": [digest] if allowed else [],
        "blocked_reasons": [] if allowed else ["hash not allowed"],
    }
    fields.update(overrides)
    return PreparedState.model_validate(fields)


def test_finding_key_is_deterministic_12_hex() -> None:
    key = finding_key(Repository.PARENT, "#1-dir", "m/active/L-1")
    assert len(key) == 12 and int(key, 16) >= 0
    assert key == finding_key(Repository.PARENT, "#1-dir", "m/active/L-1")
    assert key != finding_key(Repository.PUBLIC, "#1-dir", "m/active/L-1")


def test_review_finding_allows_unknown_severity_and_rejects_bad_lines_code_body() -> None:
    assert _finding(severity=Severity.UNKNOWN).severity is Severity.UNKNOWN
    with pytest.raises(ValidationError):
        _finding(line_end=39)
    with pytest.raises(ValidationError):
        _finding(code="l1")
    with pytest.raises(ValidationError):
        _finding(body="")


def test_review_batch_rejects_duplicates_and_unknown_files() -> None:
    first = _finding()
    resolved = _finding("claude-opus-5/resolved/L-1", status=FindingStatus.RESOLVED)
    batch = _batch(first, resolved)
    assert batch.active_findings() == (first,)
    assert batch.finding("claude-opus-5/resolved/L-1") is resolved
    with pytest.raises(KeyError):
        batch.finding("nope")
    with pytest.raises(ValidationError):
        _batch(first, _finding())
    with pytest.raises(ValidationError):
        _batch(first, _finding("other/active/L-2", finding_key=first.finding_key))
    with pytest.raises(ValidationError):
        _batch(_finding(source_file="ghost.md"))
    with pytest.raises(ValidationError):
        ReviewBatch.model_validate({**_batch(first).model_dump(), "version": 2})


def test_prepared_state_invariants() -> None:
    state = _prepared({"finding": "本文", "evidence": "抜粋"})
    assert state.allowed and state.char_count == 4
    with pytest.raises(ValidationError):
        _prepared({"finding": "本文"}, blocked_reasons=["x"])
    with pytest.raises(ValidationError):
        _prepared({"finding": "本文"}, allowed_by=[])
    with pytest.raises(ValidationError):
        _prepared({"finding": "本文"}, allowed=False, blocked_reasons=[])
    with pytest.raises(ValidationError):
        _prepared({"finding": "本文"}, content_sha256=SHA64)
    with pytest.raises(ValidationError):
        _prepared({"finding": "本文"}, char_count=99)
    with pytest.raises(ValidationError):
        _prepared({"Finding": "本文"})
    with pytest.raises(ValidationError):
        _prepared({"finding\n": "本文"})
    with pytest.raises(ValidationError):
        _prepared({})
    with pytest.raises(ValidationError):
        _prepared({"finding_a": "a", "finding_b": "b"}, purpose="pair")
    pair = _prepared(
        {"finding_a": "a", "finding_b": "b"}, purpose="pair", finding_ids=["x/active/L-1", "y/active/L-2"]
    )
    assert pair.purpose == "pair"
    with pytest.raises(ValidationError):
        PreparedState.model_validate({**state.model_dump(), "version": 2})


def _decision(proposal: Proposal = Proposal.REJECT, **overrides: object) -> TriageDecision:
    fields: dict[str, object] = {
        "finding_id": "claude-opus-5/active/L-1",
        "proposal": proposal,
        "severity_summary": summarize_severity({"claude-opus-5": Severity.LOW, "grok-4.7": Severity.LOW}).model_dump(),
        "evidence_refs": ["claude-opus-5.md:40-48"],
        "escalation_reasons": ["needs a person"] if proposal is Proposal.HUMAN_REVIEW else [],
    }
    fields.update(overrides)
    return TriageDecision.model_validate(fields)


def _report(status: ReportStatus, *decisions: TriageDecision) -> TriageReport:
    return TriageReport(
        repository=Repository.PARENT,
        review_dir="#406-feature-issue-382-rst-writer-foundation",
        reviewed_head="e0bff6690762",
        model_id_requested="jev-latest",
        model_id_responded="jev-latest",
        question_set_version="1",
        thresholds=Thresholds(model_id="jev-latest"),
        status=status,
        reason="offline replay",
        decisions=decisions,
        unparsed_count=0,
        generated_at="2026-09-26T00:00:00Z",
    )


def test_thresholds_defaults_ranges_and_ordering() -> None:
    # 既定値の具体的な値は test_policy.py の閾値の境界のテストと golden report が確かめる
    Thresholds(model_id="jev-latest")
    with pytest.raises(ValidationError):
        Thresholds(model_id="jev-latest", noul_yes_min=1.5)
    with pytest.raises(ValidationError):
        Thresholds(model_id="jev-latest", noul_yes_min=0.5, noul_no_max=0.5)
    with pytest.raises(ValidationError):
        Thresholds.model_validate({"model_id": "jev-latest", "version": 2})


def test_severity_summary_is_deterministic_and_self_consistent() -> None:
    summary = summarize_severity({"a": Severity.HIGH, "b": Severity.INFO, "c": Severity.UNKNOWN})
    assert (summary.highest, summary.lowest, summary.agreement) == (Severity.HIGH, Severity.UNKNOWN, False)
    assert summarize_severity({"a": Severity.LOW}).agreement is True
    with pytest.raises(ValueError):
        summarize_severity({})
    with pytest.raises(ValidationError):
        SeveritySummary(by_model={"a": Severity.LOW}, highest=Severity.HIGH, lowest=Severity.LOW, agreement=True)
    with pytest.raises(ValidationError):
        SeveritySummary(
            by_model={"a": Severity.LOW, "b": Severity.HIGH},
            highest=Severity.HIGH,
            lowest=Severity.LOW,
            agreement=True,
        )


def test_triage_decision_is_advisory_only() -> None:
    assert _decision().advisory is True
    with pytest.raises(ValidationError):
        _decision(advisory=False)
    with pytest.raises(ValidationError):
        TriageDecision.model_validate({**_decision().model_dump(), "proposal": "merge_now"})
    with pytest.raises(ValidationError):
        _decision(Proposal.HUMAN_REVIEW, escalation_reasons=[])
    assert _decision(Proposal.HUMAN_REVIEW).escalation_reasons == ("needs a person",)


def test_triage_report_invariants() -> None:
    advisory = _report(
        ReportStatus.ADVISORY, _decision(Proposal.FIX_NOW), _decision(Proposal.HUMAN_REVIEW, finding_id="x/active/L-2")
    )
    assert len(advisory.decisions) == 2
    with pytest.raises(ValidationError):
        _report(ReportStatus.ADVISORY, _decision(), _decision())
    with pytest.raises(ValidationError):
        _report(ReportStatus.FALLBACK, _decision(Proposal.FIX_NOW))
    fallback = _report(ReportStatus.FALLBACK, _decision(Proposal.HUMAN_REVIEW))
    assert fallback.status is ReportStatus.FALLBACK
    with pytest.raises(ValidationError):
        TriageReport.model_validate({**fallback.model_dump(), "version": 2})


def test_purpose_and_finding_ids_must_agree() -> None:
    two = ["claude-opus-5/active/L-1", "grok-4.7/active/L-2"]
    with pytest.raises(ValidationError):
        _prepared({"finding": "本文"}, finding_ids=two)
    with pytest.raises(ValidationError):
        _prepared({"finding_a": "a", "finding_b": "b"}, purpose="pair", finding_ids=[two[0], two[0]])
    request = _finding_request().model_dump()
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**request, "finding_ids": two})
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate({**request, "purpose": "pair", "finding_ids": [two[0], two[0]]})
    error = ClientError(kind=ErrorKind.TIMEOUT, message="timed out", retryable=True).model_dump()
    outcome: dict[str, object] = {
        "ok": False,
        "error": error,
        "model_requested": "jev-latest",
        "request_sha256": SHA64,
        "calls_made": 0,
    }
    assert ClientOutcome.model_validate({**outcome, "purpose": "pair", "finding_ids": two}).purpose == "pair"
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate({**outcome, "purpose": "finding", "finding_ids": two})
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate({**outcome, "purpose": "pair", "finding_ids": two[:1]})
    with pytest.raises(ValidationError):
        ClientOutcome.model_validate({**outcome, "purpose": "pair", "finding_ids": [two[0], two[0]]})
