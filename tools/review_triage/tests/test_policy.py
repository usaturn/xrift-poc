import builtins
import io
import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import ValidationError

from tools.review_triage.models import (
    ClientError,
    ClientOutcome,
    ErrorKind,
    FindingStatus,
    Marker,
    Proposal,
    ReportStatus,
    Repository,
    ReviewBatch,
    ReviewFile,
    ReviewFinding,
    Severity,
    SystemOneResponse,
    Thresholds,
    TriageDecision,
    TriageReport,
    UnparsedBlock,
    finding_key,
)
from tools.review_triage.policy import EscalationCode, triage
from tools.review_triage.questions import (
    QUESTION_SET_VERSION,
    applies_question_id,
    proposal_question_id,
    same_question_id,
)
from tools.review_triage.report import render_report

TARGET = "fdfc294f57fb773d9024d041d834e7ede1c7cd78"
OTHER_HEAD = "4f5ac57f0b22"
MODEL = "jev-latest"
REVIEW_DIR = "reviews/#999-feature-example"
THRESHOLDS = Thresholds(model_id=MODEL)
GENERATED_AT = "2026-09-26T00:00:00Z"
FIXTURES = Path(__file__).parent / "fixtures" / "policy"


def _finding(
    model_name: str = "alpha",
    code: str = "H-1",
    *,
    severity: Severity = Severity.LOW,
    status: FindingStatus = FindingStatus.ACTIVE,
    marker: Marker = Marker.NONE,
    reviewed_head: str | None = TARGET[:12],
    detected_head: str | None = TARGET[:12],
    location: str | None = "tools/example.py:10",
    evidence: str | None = "根拠の抜粋",
    line: int = 10,
) -> ReviewFinding:
    finding_id = f"{model_name}/{status.value}/{code}"
    return ReviewFinding(
        finding_id=finding_id,
        finding_key=finding_key(Repository.ENV, REVIEW_DIR, finding_id),
        repository=Repository.ENV,
        review_dir=REVIEW_DIR,
        source_file=f"{model_name}.md",
        model_name=model_name,
        line_start=line,
        line_end=line + 6,
        reviewed_head=reviewed_head,
        detected_head=detected_head,
        severity=severity,
        status=status,
        code=code,
        title=f"{code} の見出し",
        marker=marker,
        body=f"### {code}: 見出し",
        location=location,
        content="内容",
        evidence=evidence,
        recommendation="推奨対応",
    )


def _batch(*findings: ReviewFinding, unparsed: int = 0, anomalies: tuple[str, ...] = ()) -> ReviewBatch:
    files = {finding.source_file: finding.model_name for finding in findings}
    blocks = tuple(
        UnparsedBlock(
            source_file="alpha.md", line_start=index + 1, line_end=index + 1, text_sha256="e" * 64, reason="x"
        )
        for index in range(unparsed)
    )
    if unparsed:
        files.setdefault("alpha.md", "alpha")
    return ReviewBatch(
        repository=Repository.ENV,
        review_dir=REVIEW_DIR,
        reviewed_head=TARGET,
        files=tuple(ReviewFile(source_file=name, model_name=model) for name, model in sorted(files.items())),
        findings=findings,
        unparsed=blocks,
        anomalies=anomalies,
    )


def _noul(value: float) -> dict[str, object]:
    return {"type": "noul", "noul": value}


def _choice(choice: str, confidence: float) -> dict[str, object]:
    return {"type": "choice", "choice": choice, "confidence": confidence}


def _response(answers: dict[str, dict[str, object]], *, model: str, request_id: str | None) -> SystemOneResponse:
    return SystemOneResponse.model_validate(
        {
            "model": model,
            "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 0},
            "request_id": request_id,
        }
    )


def _finding_outcome(
    finding: ReviewFinding,
    *,
    applies: float = 1.0,
    proposal: str = "fix_now",
    confidence: float = 0.95,
    answers: dict[str, dict[str, object]] | None = None,
    model_requested: str = MODEL,
    model_responded: str = "jev-1.13.0",
    request_sha: str = "1" * 64,
    request_id: str | None = None,
) -> ClientOutcome:
    key = finding.finding_key
    if answers is None:
        answers = {applies_question_id(key): _noul(applies), proposal_question_id(key): _choice(proposal, confidence)}
    return ClientOutcome(
        ok=True,
        response=_response(answers, model=model_responded, request_id=request_id),
        model_requested=model_requested,
        request_sha256=request_sha,
        purpose="finding",
        finding_ids=(finding.finding_id,),
        calls_made=1,
    )


def _pair_outcome(
    first: ReviewFinding,
    second: ReviewFinding,
    noul: float,
    *,
    answers: dict[str, dict[str, object]] | None = None,
    request_sha: str = "2" * 64,
) -> ClientOutcome:
    if answers is None:
        answers = {same_question_id(first.finding_key, second.finding_key): _noul(noul)}
    return ClientOutcome(
        ok=True,
        response=_response(answers, model="jev-1.13.0", request_id=None),
        model_requested=MODEL,
        request_sha256=request_sha,
        purpose="pair",
        finding_ids=(first.finding_id, second.finding_id),
        calls_made=1,
    )


def _failed(
    finding_ids: tuple[str, ...], kind: ErrorKind, *, model_requested: str = MODEL, request_sha: str = "3" * 64
) -> ClientOutcome:
    return ClientOutcome(
        ok=False,
        error=ClientError(kind=kind, message="sanitized reason", retryable=False),
        model_requested=model_requested,
        request_sha256=request_sha,
        purpose="finding" if len(finding_ids) == 1 else "pair",
        finding_ids=finding_ids,
        calls_made=0,
    )


def _run(batch: ReviewBatch, outcomes: list[ClientOutcome], thresholds: Thresholds = THRESHOLDS) -> TriageReport:
    return triage(batch, outcomes, thresholds=thresholds, generated_at=GENERATED_AT)


def _decision(report: TriageReport, finding: ReviewFinding) -> TriageDecision:
    matches = [decision for decision in report.decisions if decision.finding_id == finding.finding_id]
    assert len(matches) == 1
    return matches[0]


def _codes(decision: TriageDecision) -> list[str]:
    return [reason.split(":", 1)[0] for reason in decision.escalation_reasons]


def _assert_every_finding_kept(batch: ReviewBatch, report: TriageReport) -> None:
    assert [decision.finding_id for decision in report.decisions] == [f.finding_id for f in batch.findings]
    assert all(decision.advisory is True for decision in report.decisions)
    assert all(any(ref.startswith("review:") for ref in decision.evidence_refs) for decision in report.decisions)


# --- 高 confidence の助言 -------------------------------------------------------


def test_confident_fix_now_becomes_an_advisory_draft() -> None:
    finding = _finding()
    report = _run(_batch(finding), [_finding_outcome(finding)])
    decision = _decision(report, finding)
    assert report.status is ReportStatus.ADVISORY
    assert report.reason == "advisory: 1 of 1 active finding(s) have an advisory proposal"
    assert decision.proposal is Proposal.FIX_NOW
    assert decision.advisory is True
    assert decision.escalation_reasons == ()
    assert sorted(decision.answers) == sorted(
        [applies_question_id(finding.finding_key), proposal_question_id(finding.finding_key)]
    )
    assert report.model_id_requested == MODEL
    assert report.model_id_responded == "jev-1.13.0"
    assert report.question_set_version == QUESTION_SET_VERSION
    assert report.generated_at == GENERATED_AT


@pytest.mark.parametrize(
    ("confidence", "expected", "codes"),
    [
        (0.9, Proposal.FIX_NOW, []),
        (0.8999999999, Proposal.HUMAN_REVIEW, ["proposal_low_confidence"]),
        (0.9000000001, Proposal.FIX_NOW, []),
    ],
)
def test_choice_confidence_threshold_boundary(confidence: float, expected: Proposal, codes: list[str]) -> None:
    finding = _finding()
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding, confidence=confidence)]), finding)
    assert decision.proposal is expected
    assert _codes(decision) == codes


def test_low_confidence_reason_keeps_the_exact_numbers() -> None:
    finding = _finding()
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding, confidence=0.8999999999)]), finding)
    question_id = proposal_question_id(finding.finding_key)
    assert decision.escalation_reasons == (f"proposal_low_confidence: {question_id} confidence=0.8999999999 < 0.9",)


@pytest.mark.parametrize(
    ("noul", "proposal", "expected", "codes"),
    [
        (1.0, "fix_now", Proposal.FIX_NOW, []),
        (0.95, "fix_now", Proposal.FIX_NOW, []),
        (0.9499999999, "fix_now", Proposal.HUMAN_REVIEW, ["applies_uncertain"]),
        (0.5, "fix_now", Proposal.HUMAN_REVIEW, ["applies_uncertain"]),
        (0.0500000001, "reject", Proposal.HUMAN_REVIEW, ["applies_uncertain"]),
        (0.05, "reject", Proposal.REJECT, []),
        (0.0, "reject", Proposal.REJECT, []),
        (1.0, "separate_issue", Proposal.SEPARATE_ISSUE, []),
        (0.0, "fix_now", Proposal.HUMAN_REVIEW, ["applies_proposal_conflict"]),
        (0.0, "separate_issue", Proposal.HUMAN_REVIEW, ["applies_proposal_conflict"]),
        (1.0, "reject", Proposal.HUMAN_REVIEW, ["applies_proposal_conflict"]),
    ],
)
def test_noul_is_read_with_separate_yes_and_no_thresholds(
    noul: float, proposal: str, expected: Proposal, codes: list[str]
) -> None:
    finding = _finding(severity=Severity.LOW)
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding, applies=noul, proposal=proposal)]), finding)
    assert decision.proposal is expected
    assert _codes(decision) == codes


def test_model_choosing_human_review_is_an_escalation() -> None:
    finding = _finding()
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding, proposal="human_review")]), finding)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert _codes(decision) == ["model_chose_human_review"]


# --- 応答欠損・解析不能 ---------------------------------------------------------


def test_finding_without_outcome_is_returned_to_human_review() -> None:
    sent, missing = _finding("alpha"), _finding("beta")
    report = _run(_batch(sent, missing), [_finding_outcome(sent)])
    assert report.status is ReportStatus.ADVISORY
    assert _decision(report, sent).proposal is Proposal.FIX_NOW
    assert _decision(report, missing).proposal is Proposal.HUMAN_REVIEW
    assert _codes(_decision(report, missing)) == ["missing_outcome"]


def _answers_for(finding: ReviewFinding, **overrides: dict[str, object] | None) -> dict[str, dict[str, object]]:
    names = {
        "applies": applies_question_id(finding.finding_key),
        "proposal": proposal_question_id(finding.finding_key),
    }
    answers: dict[str, dict[str, object]] = {names["applies"]: _noul(1.0), names["proposal"]: _choice("fix_now", 0.95)}
    for name, value in overrides.items():
        question_id = names.get(name, name)
        if value is None:
            answers.pop(question_id)
        else:
            answers[question_id] = value
    return answers


@pytest.mark.parametrize(
    ("overrides", "codes"),
    [
        ({"applies": None}, ["answer_missing"]),
        ({"proposal": None}, ["answer_missing"]),
        ({"applies": _choice("fix_now", 0.99)}, ["answer_type_mismatch"]),
        ({"proposal": _noul(0.99)}, ["answer_type_mismatch"]),
        ({"proposal": _choice("maybe", 0.99)}, ["choice_outside_criteria"]),
        ({"applies_ffffffffffff": _noul(0.5)}, ["answer_unexpected"]),
    ],
)
def test_unusable_answers_are_escalated(overrides: dict[str, dict[str, object] | None], codes: list[str]) -> None:
    finding = _finding()
    outcome = _finding_outcome(finding, answers=_answers_for(finding, **overrides))
    decision = _decision(_run(_batch(finding), [outcome]), finding)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert _codes(decision) == codes


def test_long_choice_text_is_truncated_in_the_reason() -> None:
    finding = _finding()
    answers = _answers_for(finding, proposal=_choice("x" * 500, 0.99))
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding, answers=answers)]), finding)
    question_id = proposal_question_id(finding.finding_key)
    assert decision.escalation_reasons == (f"choice_outside_criteria: {question_id} choice={'x' * 64!r}",)


def test_empty_generated_at_is_rejected_instead_of_reading_the_clock() -> None:
    finding = _finding()
    with pytest.raises(ValidationError):
        triage(_batch(finding), [_finding_outcome(finding)], thresholds=THRESHOLDS, generated_at="")


def test_duplicate_outcomes_for_one_finding_are_not_used() -> None:
    finding = _finding()
    outcomes = [_finding_outcome(finding, request_sha="a" * 64), _finding_outcome(finding, request_sha="b" * 64)]
    decision = _decision(_run(_batch(finding), outcomes), finding)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert decision.escalation_reasons == ("duplicate_outcome: count=2",)
    assert decision.answers == {}
    assert {f"request:{'a' * 64}", f"request:{'b' * 64}"} <= set(decision.evidence_refs)


def test_invalid_request_is_reported_apart_from_service_failures() -> None:
    broken, fine = _finding("alpha"), _finding("beta")
    report = _run(
        _batch(broken, fine), [_failed((broken.finding_id,), ErrorKind.INVALID_REQUEST), _finding_outcome(fine)]
    )
    assert report.status is ReportStatus.ADVISORY
    assert "invalid_request outcomes: 1 (sender-side error, not a service failure)" in report.reason
    assert _codes(_decision(report, broken)) == ["invalid_request"]
    assert _decision(report, fine).proposal is Proposal.FIX_NOW


# --- 障害・未校正・未送信 -------------------------------------------------------


@pytest.mark.parametrize("kind", [kind for kind in ErrorKind if kind is not ErrorKind.INVALID_REQUEST])
def test_client_failure_returns_every_finding_to_human_review(kind: ErrorKind) -> None:
    first, second, resolved = _finding("alpha"), _finding("beta"), _finding("gamma", status=FindingStatus.RESOLVED)
    batch = _batch(first, second, resolved)
    report = _run(batch, [_finding_outcome(first), _failed((second.finding_id,), kind)])
    assert report.status is ReportStatus.FALLBACK
    assert report.reason == f"fallback: client failures: {kind.value} x1"
    _assert_every_finding_kept(batch, report)
    for decision in report.decisions:
        assert decision.proposal is Proposal.HUMAN_REVIEW
        assert decision.escalation_reasons[0] == "report_fallback"
        assert decision.answers == {}
        assert decision.duplicate_candidates == ()
    assert _codes(_decision(report, resolved)) == ["report_fallback", "status_resolved"]


def test_uncalibrated_thresholds_return_every_finding_to_human_review() -> None:
    finding = _finding()
    batch = _batch(finding)
    report = _run(batch, [_finding_outcome(finding)], Thresholds(model_id="jev-1.13.0"))
    assert report.status is ReportStatus.UNCALIBRATED
    assert report.reason == "uncalibrated: requested model jev-latest does not match thresholds.model_id jev-1.13.0"
    _assert_every_finding_kept(batch, report)
    decision = _decision(report, finding)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert decision.escalation_reasons == ("report_uncalibrated",)
    assert decision.answers == {}


def test_responded_model_is_recorded_but_not_a_gate() -> None:
    finding = _finding()
    report = _run(_batch(finding), [_finding_outcome(finding, model_responded="jev-2.0.0")])
    assert report.status is ReportStatus.ADVISORY
    assert report.model_id_requested == MODEL
    assert report.model_id_responded == "jev-2.0.0"


def test_mixed_requested_models_are_joined_and_uncalibrated() -> None:
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [_finding_outcome(first), _finding_outcome(second, model_requested="jev-1.13.0")]
    report = _run(_batch(first, second), outcomes)
    assert report.status is ReportStatus.UNCALIBRATED
    assert report.model_id_requested == "jev-1.13.0,jev-latest"
    assert report.reason == "uncalibrated: requested model jev-1.13.0 does not match thresholds.model_id jev-latest"


def test_failure_and_uncalibrated_model_are_both_written_in_the_reason() -> None:
    first, second = _finding("alpha"), _finding("beta")
    batch = _batch(first, second)
    outcomes = [
        _finding_outcome(first),
        _failed((second.finding_id,), ErrorKind.SERVICE, model_requested="jev-1.13.0"),
    ]
    report = _run(batch, outcomes)
    assert report.status is ReportStatus.FALLBACK
    assert report.reason == (
        "fallback: client failures: service x1; "
        "requested model jev-1.13.0 does not match thresholds.model_id jev-latest"
    )
    _assert_every_finding_kept(batch, report)
    assert all(decision.escalation_reasons[0] == "report_fallback" for decision in report.decisions)


def test_no_outcomes_means_nothing_was_sent() -> None:
    finding = _finding()
    batch = _batch(finding)
    report = _run(batch, [])
    assert report.status is ReportStatus.FALLBACK
    assert report.reason == "fallback: no outcomes for 1 active finding(s)"
    assert report.model_id_requested == MODEL
    assert report.model_id_responded is None
    _assert_every_finding_kept(batch, report)
    assert _codes(_decision(report, finding)) == ["report_fallback"]


def test_batch_without_active_findings_is_advisory_without_outcomes() -> None:
    resolved = _finding(status=FindingStatus.RESOLVED)
    report = _run(_batch(resolved), [])
    assert report.status is ReportStatus.ADVISORY
    assert _codes(_decision(report, resolved)) == ["status_resolved"]


def test_outcome_for_a_finding_outside_the_batch_falls_back() -> None:
    finding, stranger = _finding("alpha"), _finding("zeta")
    batch = _batch(finding)
    report = _run(batch, [_finding_outcome(finding), _finding_outcome(stranger)])
    assert report.status is ReportStatus.FALLBACK
    assert report.reason == "fallback: outcomes reference 1 finding id(s) outside the batch"
    _assert_every_finding_kept(batch, report)


def test_structural_reasons_survive_a_fallback() -> None:
    finding = _finding(location=None)
    report = _run(_batch(finding), [])
    assert _codes(_decision(report, finding)) == ["report_fallback", "insufficient_evidence"]


# --- 構造検査（HEAD・根拠・マーカー・状態）--------------------------------------


@pytest.mark.parametrize(
    ("reviewed_head", "detected_head", "codes"),
    [
        (TARGET[:12], OTHER_HEAD, []),
        (TARGET, None, []),
        (None, TARGET[:7], []),
        (OTHER_HEAD, TARGET[:12], ["head_mismatch"]),
        (None, OTHER_HEAD, ["head_mismatch"]),
        (None, None, ["head_unknown"]),
    ],
)
def test_head_is_compared_with_the_target(
    reviewed_head: str | None, detected_head: str | None, codes: list[str]
) -> None:
    finding = _finding(reviewed_head=reviewed_head, detected_head=detected_head)
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding)]), finding)
    assert _codes(decision) == codes
    assert decision.proposal is (Proposal.FIX_NOW if not codes else Proposal.HUMAN_REVIEW)


def test_head_mismatch_names_both_heads() -> None:
    finding = _finding(reviewed_head=OTHER_HEAD)
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding)]), finding)
    assert decision.escalation_reasons == (f"head_mismatch: finding={OTHER_HEAD} target={TARGET}",)


@pytest.mark.parametrize(
    ("location", "evidence", "detail"),
    [
        (None, "根拠", "missing=location"),
        ("x.py:1", None, "missing=evidence"),
        ("  ", None, "missing=location,evidence"),
    ],
)
def test_insufficient_evidence_is_escalated(location: str | None, evidence: str | None, detail: str) -> None:
    finding = _finding(location=location, evidence=evidence)
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding)]), finding)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert decision.escalation_reasons == (f"insufficient_evidence: {detail}",)


def test_blank_location_is_not_listed_as_a_reference() -> None:
    finding = _finding(location="  ")
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding)]), finding)
    assert decision.escalation_reasons == ("insufficient_evidence: missing=location",)
    assert [ref for ref in decision.evidence_refs if ref.startswith("location:")] == []


def test_design_judgment_marker_is_always_escalated() -> None:
    finding = _finding(marker=Marker.DESIGN_JUDGMENT)
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding)]), finding)
    assert _codes(decision) == ["design_judgment_marker"]


@pytest.mark.parametrize(
    ("applies", "proposal", "expected", "codes"),
    [
        (0.0, "reject", Proposal.HUMAN_REVIEW, ["verified_finding_rejected"]),
        (0.5, "reject", Proposal.HUMAN_REVIEW, ["applies_uncertain"]),
        (1.0, "fix_now", Proposal.FIX_NOW, []),
    ],
)
def test_verified_finding_is_not_rejected_by_the_model(
    applies: float, proposal: str, expected: Proposal, codes: list[str]
) -> None:
    finding = _finding(marker=Marker.VERIFIED)
    outcome = _finding_outcome(finding, applies=applies, proposal=proposal)
    decision = _decision(_run(_batch(finding), [outcome]), finding)
    assert decision.proposal is expected
    assert _codes(decision) == codes


@pytest.mark.parametrize(
    ("status", "code"), [(FindingStatus.RESOLVED, "status_resolved"), (FindingStatus.PENDING, "status_pending")]
)
def test_non_active_findings_are_kept_but_not_triaged(status: FindingStatus, code: str) -> None:
    finding = _finding(status=status, location=None)
    decision = _decision(_run(_batch(finding), [_finding_outcome(finding)]), finding)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert decision.escalation_reasons == (code,)
    assert decision.answers == {}


@pytest.mark.parametrize("status", ["unverified", "decided"])
def test_recorded_findings_do_not_affect_active_advice(status: str) -> None:
    active = _finding("active")
    recorded = _finding("recorded", status=FindingStatus(status), severity=Severity.UNKNOWN)
    outcomes = [
        _finding_outcome(active),
        _finding_outcome(recorded, model_requested="other-model"),
        _pair_outcome(active, recorded, 1.0),
        _failed((recorded.finding_id,), ErrorKind.CONNECTION),
    ]
    report = _run(_batch(active, recorded), outcomes)
    decision = _decision(report, active)
    assert report.status is ReportStatus.ADVISORY
    assert report.model_id_requested == MODEL
    assert decision.proposal is Proposal.FIX_NOW
    assert decision.duplicate_candidates == ()
    assert decision.severity_summary.by_model == {active.finding_id: Severity.LOW}
    assert report.reason == "advisory: 1 of 1 active finding(s) have an advisory proposal"
    data = report.model_dump(mode="json")
    assert data["unverified_count"] == (1 if status == "unverified" else 0)
    assert data["decided_count"] == (1 if status == "decided" else 0)
    if status == "unverified":
        candidate = _decision(report, recorded)
        assert candidate.proposal is Proposal.HUMAN_REVIEW
        assert candidate.escalation_reasons == ("status_unverified",)
        assert candidate.answers == {}
    else:
        assert [d.finding_id for d in report.decisions] == [active.finding_id]


def test_only_decided_findings_produce_counts_without_new_decisions() -> None:
    findings = [_finding("alpha", status=FindingStatus("decided")), _finding("beta", status=FindingStatus("decided"))]
    report = _run(_batch(*findings), [])
    assert report.status is ReportStatus.ADVISORY
    assert report.decisions == ()
    assert report.model_dump(mode="json")["decided_count"] == 2
    assert report.reason == "advisory: 0 of 0 active finding(s) have an advisory proposal"


# --- severity 保護 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "applies", "proposal", "expected", "codes"),
    [
        (Severity.CRITICAL, 0.0, "reject", Proposal.HUMAN_REVIEW, ["severe_not_fix_now"]),
        (Severity.HIGH, 1.0, "separate_issue", Proposal.HUMAN_REVIEW, ["severe_not_fix_now"]),
        (Severity.UNKNOWN, 0.0, "reject", Proposal.HUMAN_REVIEW, ["severe_not_fix_now"]),
        (Severity.HIGH, 1.0, "fix_now", Proposal.FIX_NOW, []),
        (Severity.MEDIUM, 0.0, "reject", Proposal.REJECT, []),
        (Severity.INFO, 1.0, "separate_issue", Proposal.SEPARATE_ISSUE, []),
    ],
)
def test_severe_findings_are_never_rejected_or_deferred_by_the_model(
    severity: Severity, applies: float, proposal: str, expected: Proposal, codes: list[str]
) -> None:
    finding = _finding(severity=severity)
    outcome = _finding_outcome(finding, applies=applies, proposal=proposal)
    decision = _decision(_run(_batch(finding), [outcome]), finding)
    assert decision.proposal is expected
    assert _codes(decision) == codes


# --- 追跡情報・決定論 -----------------------------------------------------------


def test_evidence_refs_trace_back_to_the_review_and_the_request() -> None:
    finding = _finding(detected_head=OTHER_HEAD)
    outcome = _finding_outcome(finding, request_sha="f" * 64, request_id="req-123")
    decision = _decision(_run(_batch(finding), [outcome]), finding)
    assert decision.evidence_refs == (
        "review:alpha.md#L10-L16",
        "location:tools/example.py:10",
        f"reviewed_head:{TARGET[:12]}",
        f"detected_head:{OTHER_HEAD}",
        f"request:{'f' * 64}",
        "response:req-123",
    )


def test_generated_at_defaults_to_the_utc_clock() -> None:
    finding = _finding()
    report = triage(_batch(finding), [_finding_outcome(finding)], thresholds=THRESHOLDS)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", report.generated_at)


def test_escalation_codes_can_be_split_from_the_detail() -> None:
    # 理由は `<code>: <detail>` の形で、集計は先頭のコードで行う。値の一意性は @enum.unique が保証する
    assert all(re.fullmatch(r"[a-z_]+", code.value) for code in EscalationCode)


# --- 重複候補と横断検査 -------------------------------------------------------


def test_severity_protection_follows_the_duplicate_group() -> None:
    low, high = _finding("alpha", "L-1", severity=Severity.LOW), _finding("beta", "H-1", severity=Severity.HIGH)
    outcomes = [
        _finding_outcome(low, applies=0.0, proposal="reject", request_sha="a" * 64),
        _finding_outcome(high, applies=0.0, proposal="reject", request_sha="b" * 64),
        _pair_outcome(low, high, 0.99),
    ]
    decision = _decision(_run(_batch(low, high), outcomes), low)
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert decision.escalation_reasons == ("severe_not_fix_now: proposal=reject severe=high",)
    summary = decision.severity_summary
    assert summary.by_model == {low.finding_id: Severity.LOW, high.finding_id: Severity.HIGH}
    assert (summary.highest, summary.lowest, summary.agreement) == (Severity.HIGH, Severity.LOW, False)


@pytest.mark.parametrize(
    ("noul", "listed", "codes"),
    [
        (0.99, True, []),
        (0.95, True, []),
        (0.5, True, ["duplicate_uncertain"]),
        (0.05, False, []),
        (0.0, False, []),
    ],
)
def test_duplicate_pairs_are_shown_as_candidates(noul: float, listed: bool, codes: list[str]) -> None:
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64),
        _finding_outcome(second, request_sha="b" * 64),
        _pair_outcome(first, second, noul),
    ]
    report = _run(_batch(first, second), outcomes)
    question_id = same_question_id(first.finding_key, second.finding_key)
    for mine, partner in ((first, second), (second, first)):
        decision = _decision(report, mine)
        assert decision.duplicate_candidates == ((partner.finding_id,) if listed else ())
        assert _codes(decision) == codes
        assert question_id in decision.answers
        assert f"request:{'2' * 64}" in decision.evidence_refs


def test_duplicates_are_not_merged_transitively() -> None:
    first, second, third = _finding("alpha"), _finding("beta"), _finding("gamma")
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64),
        _finding_outcome(second, request_sha="b" * 64),
        _finding_outcome(third, request_sha="c" * 64),
        _pair_outcome(first, second, 0.99, request_sha="d" * 64),
        _pair_outcome(second, third, 0.99, request_sha="e" * 64),
    ]
    batch = _batch(first, second, third)
    report = _run(batch, outcomes)
    _assert_every_finding_kept(batch, report)
    assert _decision(report, first).duplicate_candidates == (second.finding_id,)
    assert _decision(report, second).duplicate_candidates == (first.finding_id, third.finding_id)
    assert _decision(report, third).duplicate_candidates == (second.finding_id,)


def test_confident_duplicates_with_different_proposals_are_escalated() -> None:
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [
        _finding_outcome(first, applies=1.0, proposal="fix_now", request_sha="a" * 64),
        _finding_outcome(second, applies=1.0, proposal="separate_issue", request_sha="b" * 64),
        _pair_outcome(first, second, 0.99),
    ]
    report = _run(_batch(first, second), outcomes)
    assert _decision(report, first).escalation_reasons == (
        f"duplicate_proposal_conflict: partner={second.finding_id} fix_now!=separate_issue",
    )
    assert _decision(report, second).escalation_reasons == (
        f"duplicate_proposal_conflict: partner={first.finding_id} separate_issue!=fix_now",
    )


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        ({}, "answer_missing"),
        ({"same": _choice("fix_now", 0.99)}, "answer_type_mismatch"),
        ({"same": _noul(0.99), "same_ffffffffffff_ffffffffffff": _noul(0.99)}, "answer_unexpected"),
    ],
)
def test_unusable_pair_answers_escalate_both_findings(answers: dict[str, dict[str, object]], problem: str) -> None:
    first, second = _finding("alpha"), _finding("beta")
    question_id = same_question_id(first.finding_key, second.finding_key)
    pair_answers = {question_id if name == "same" else name: value for name, value in answers.items()}
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64),
        _finding_outcome(second, request_sha="b" * 64),
        _pair_outcome(first, second, 0.0, answers=pair_answers),
    ]
    report = _run(_batch(first, second), outcomes)
    for mine, partner in ((first, second), (second, first)):
        decision = _decision(report, mine)
        assert decision.proposal is Proposal.HUMAN_REVIEW
        assert decision.escalation_reasons[0].startswith(f"pair_invalid: partner={partner.finding_id} {problem}")
        assert decision.duplicate_candidates == ()


def test_invalid_request_on_a_pair_escalates_both_findings() -> None:
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64),
        _finding_outcome(second, request_sha="b" * 64),
        _failed((first.finding_id, second.finding_id), ErrorKind.INVALID_REQUEST),
    ]
    report = _run(_batch(first, second), outcomes)
    assert report.status is ReportStatus.ADVISORY
    assert _decision(report, first).escalation_reasons == (
        f"pair_invalid: partner={second.finding_id} invalid_request",
    )


def test_the_same_pair_sent_twice_is_not_used() -> None:
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64),
        _finding_outcome(second, request_sha="b" * 64),
        _pair_outcome(first, second, 0.99, request_sha="c" * 64),
        _pair_outcome(second, first, 0.99, request_sha="d" * 64),
    ]
    report = _run(_batch(first, second), outcomes)
    assert _decision(report, first).escalation_reasons == (
        f"pair_invalid: partner={second.finding_id} duplicate_outcome count=2",
    )
    assert _decision(report, first).duplicate_candidates == ()


def test_pair_with_a_finding_outside_the_batch_falls_back() -> None:
    finding, stranger = _finding("alpha"), _finding("zeta")
    batch = _batch(finding)
    report = _run(batch, [_finding_outcome(finding), _pair_outcome(finding, stranger, 0.99)])
    assert report.status is ReportStatus.FALLBACK
    _assert_every_finding_kept(batch, report)


def test_resolved_partner_is_listed_but_not_escalated() -> None:
    active, resolved = _finding("alpha"), _finding("beta", status=FindingStatus.RESOLVED)
    outcomes = [_finding_outcome(active, request_sha="a" * 64), _pair_outcome(active, resolved, 0.99)]
    report = _run(_batch(active, resolved), outcomes)
    assert _decision(report, active).duplicate_candidates == (resolved.finding_id,)
    assert _decision(report, active).proposal is Proposal.FIX_NOW
    assert _decision(report, resolved).escalation_reasons == ("status_resolved",)
    assert _decision(report, resolved).duplicate_candidates == ()


def _mixed_case() -> tuple[ReviewBatch, list[ClientOutcome]]:
    first = _finding("alpha", "H-1", severity=Severity.HIGH)
    second = _finding("beta", "M-1", severity=Severity.MEDIUM)
    third = _finding("gamma", "L-1")
    fourth = _finding("delta", "I-1", severity=Severity.INFO)
    resolved = _finding("alpha", "L-2", status=FindingStatus.RESOLVED, line=40)
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64, request_id="req-a"),
        _finding_outcome(second, request_sha="b" * 64),
        _finding_outcome(third, confidence=0.5, request_sha="c" * 64),
        _finding_outcome(fourth, proposal="separate_issue", confidence=0.93, request_sha="d" * 64),
        _pair_outcome(first, second, 0.97, request_sha="e" * 64),
        _pair_outcome(second, third, 0.5, request_sha="f" * 64),
    ]
    return _batch(first, second, third, fourth, resolved, unparsed=1, anomalies=("x",)), outcomes


def test_same_inputs_give_the_same_report_in_any_outcome_order() -> None:
    batch, outcomes = _mixed_case()
    report = _run(batch, outcomes)
    assert _run(batch, outcomes) == report
    assert _run(batch, list(reversed(outcomes))).model_dump_json() == report.model_dump_json()
    _assert_every_finding_kept(batch, report)


def test_report_round_trips_through_json() -> None:
    batch, outcomes = _mixed_case()
    report = _run(batch, outcomes)
    assert TriageReport.model_validate_json(report.model_dump_json()) == report


def test_reason_notes_unparsed_blocks_and_anomalies() -> None:
    batch, outcomes = _mixed_case()
    report = _run(batch, outcomes)
    assert report.unparsed_count == 1
    assert "unparsed blocks: 1 (not triaged; check the review files)" in report.reason
    assert "input anomalies: 1" in report.reason


def test_golden_report_json() -> None:
    """golden_report.json の再生成手順と更新の判断基準。

    `_run(*_mixed_case())` の `TriageReport.model_dump_json(indent=2)` に改行 1 つを足した文字列で置き換える。
    理由コード・提案・候補・件数が変わる差分は fixture を置き換えず規則の変更として扱い、表記だけの差分なら置き換える。
    """
    batch, outcomes = _mixed_case()
    expected = json.loads((FIXTURES / "golden_report.json").read_text(encoding="utf-8"))
    assert json.loads(_run(batch, outcomes).model_dump_json()) == expected


# --- 禁止事項 -------------------------------------------------------------------


def _refuse(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("policy and report must not open files, use the network or start processes")


def test_policy_and_report_do_no_io(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    batch, outcomes = _mixed_case()
    for owner, name in (
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, _refuse)
    report = _run(batch, outcomes)
    markdown = render_report(report)
    monkeypatch.undo()
    assert report.decisions and markdown
    assert capsys.readouterr() == ("", "")
