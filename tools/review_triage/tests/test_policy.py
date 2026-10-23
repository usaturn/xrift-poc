import pytest

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
    finding_key,
)
from tools.review_triage.policy import EscalationCode, triage
from tools.review_triage.questions import applies_question_id, proposal_question_id, same_question_id

TARGET = "fdfc294f57fb773d9024d041d834e7ede1c7cd78"
OTHER_HEAD = "4f5ac57f0b22"
MODEL = "jev-latest"
REVIEW_DIR = "reviews/#999-feature-example"
# 既定値に依らないよう閾値を明示し、回答の値は閾値から十分に離す
THRESHOLDS = Thresholds(model_id=MODEL, choice_min_confidence=0.8, noul_yes_min=0.9, noul_no_max=0.1)
YES, NO = 1.0, 0.0
UNCERTAIN = (THRESHOLDS.noul_yes_min + THRESHOLDS.noul_no_max) / 2
CONFIDENT = 1.0
GENERATED_AT = "2026-09-26T00:00:00Z"


def _finding(
    model_name: str = "alpha",
    code: str = "H-1",
    *,
    severity: Severity = Severity.LOW,
    marker: Marker = Marker.NONE,
    reviewed_head: str | None = TARGET[:12],
    evidence: str | None = "根拠の抜粋",
) -> ReviewFinding:
    finding_id = f"{model_name}/{FindingStatus.ACTIVE.value}/{code}"
    return ReviewFinding(
        finding_id=finding_id,
        finding_key=finding_key(Repository.ENV, REVIEW_DIR, finding_id),
        repository=Repository.ENV,
        review_dir=REVIEW_DIR,
        source_file=f"{model_name}.md",
        model_name=model_name,
        line_start=10,
        line_end=16,
        reviewed_head=reviewed_head,
        detected_head=TARGET[:12],
        severity=severity,
        status=FindingStatus.ACTIVE,
        code=code,
        title=f"{code} の見出し",
        marker=marker,
        body=f"### {code}: 見出し",
        location="tools/example.py:10",
        content="内容",
        evidence=evidence,
        recommendation="推奨対応",
    )


def _batch(*findings: ReviewFinding) -> ReviewBatch:
    files = {finding.source_file: finding.model_name for finding in findings}
    return ReviewBatch(
        repository=Repository.ENV,
        review_dir=REVIEW_DIR,
        reviewed_head=TARGET,
        files=tuple(ReviewFile(source_file=name, model_name=model) for name, model in sorted(files.items())),
        findings=findings,
    )


def _response(answers: dict[str, dict[str, object]]) -> SystemOneResponse:
    return SystemOneResponse.model_validate(
        {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 0}}
    )


def _finding_outcome(
    finding: ReviewFinding,
    *,
    applies: float = YES,
    proposal: str = "fix_now",
    confidence: float = CONFIDENT,
    request_sha: str = "1" * 64,
) -> ClientOutcome:
    key = finding.finding_key
    applies_answer: dict[str, object] = {"type": "noul", "noul": applies}
    proposal_answer: dict[str, object] = {"type": "choice", "choice": proposal, "confidence": confidence}
    answers = {applies_question_id(key): applies_answer, proposal_question_id(key): proposal_answer}
    return ClientOutcome(
        ok=True,
        response=_response(answers),
        model_requested=MODEL,
        request_sha256=request_sha,
        purpose="finding",
        finding_ids=(finding.finding_id,),
        calls_made=1,
    )


def _pair_outcome(first: ReviewFinding, second: ReviewFinding, noul: float) -> ClientOutcome:
    answer: dict[str, object] = {"type": "noul", "noul": noul}
    answers = {same_question_id(first.finding_key, second.finding_key): answer}
    return ClientOutcome(
        ok=True,
        response=_response(answers),
        model_requested=MODEL,
        request_sha256="2" * 64,
        purpose="pair",
        finding_ids=(first.finding_id, second.finding_id),
        calls_made=1,
    )


def _run(batch: ReviewBatch, outcomes: list[ClientOutcome], thresholds: Thresholds = THRESHOLDS) -> TriageReport:
    return triage(batch, outcomes, thresholds=thresholds, generated_at=GENERATED_AT)


def _decision(report: TriageReport, finding: ReviewFinding) -> TriageDecision:
    matches = [decision for decision in report.decisions if decision.finding_id == finding.finding_id]
    assert len(matches) == 1
    return matches[0]


def _codes(decision: TriageDecision) -> list[str]:
    return [reason.split(":", 1)[0] for reason in decision.escalation_reasons]


def _single(
    finding: ReviewFinding, *, applies: float = YES, proposal: str = "fix_now", confidence: float = CONFIDENT
) -> TriageDecision:
    """1 指摘に 1 件の回答（既定は applies が yes、確信ありの fix_now）を返した report の decision。"""
    outcome = _finding_outcome(finding, applies=applies, proposal=proposal, confidence=confidence)
    return _decision(_run(_batch(finding), [outcome]), finding)


def _assert_escalated(decision: TriageDecision, code: EscalationCode) -> None:
    """助言を出さず、理由が ``code`` だけであること（ほかの規則に当たらない入力で確かめる）。"""
    assert decision.proposal is Proposal.HUMAN_REVIEW
    assert _codes(decision) == [code.value]


# --- 回答の読み取り -------------------------------------------------------------


def test_low_confidence_proposal_is_escalated() -> None:
    """確信度が閾値に届かない提案を、確信ありの助言として出す。"""
    assert _single(_finding()).proposal is Proposal.FIX_NOW
    low = THRESHOLDS.choice_min_confidence / 2
    _assert_escalated(_single(_finding(), confidence=low), EscalationCode.PROPOSAL_LOW_CONFIDENCE)


@pytest.mark.parametrize(
    ("noul", "proposal", "code"),
    [
        pytest.param(UNCERTAIN, "fix_now", EscalationCode.APPLIES_UNCERTAIN, id="uncertain"),
        pytest.param(YES, "reject", EscalationCode.APPLIES_PROPOSAL_CONFLICT, id="conflict"),
    ],
)
def test_noul_is_read_with_separate_yes_and_no_thresholds(noul: float, proposal: str, code: EscalationCode) -> None:
    """applies が不確かな指摘や、applies と提案が食い違う指摘を、確信ありの助言として出す。"""
    _assert_escalated(_single(_finding(), applies=noul, proposal=proposal), code)


# --- 障害・未校正 ---------------------------------------------------------------


def test_client_failure_returns_every_finding_to_human_review() -> None:
    """送信の失敗を数えず、失敗した指摘を除いた残りを助言のまま出す。"""
    first, second = _finding("alpha"), _finding("beta")
    failed = ClientOutcome(
        ok=False,
        error=ClientError(kind=ErrorKind.SERVICE, message="sanitized reason", retryable=True),
        model_requested=MODEL,
        request_sha256="3" * 64,
        purpose="finding",
        finding_ids=(second.finding_id,),
        calls_made=1,
    )
    report = _run(_batch(first, second), [_finding_outcome(first), failed])
    assert report.status is ReportStatus.FALLBACK
    assert [decision.finding_id for decision in report.decisions] == [first.finding_id, second.finding_id]
    assert all(decision.proposal is Proposal.HUMAN_REVIEW for decision in report.decisions)


def test_uncalibrated_thresholds_return_every_finding_to_human_review() -> None:
    """閾値の校正対象と違う model への回答を、その閾値で読んで助言にする。"""
    finding = _finding()
    report = _run(_batch(finding), [_finding_outcome(finding)], Thresholds(model_id="jev-1.13.0"))
    assert report.status is ReportStatus.UNCALIBRATED
    assert [decision.proposal for decision in report.decisions] == [Proposal.HUMAN_REVIEW]


# --- 構造検査（HEAD・根拠・マーカー）--------------------------------------------


def test_head_is_compared_with_the_target() -> None:
    """別の HEAD で書かれた指摘を、対象 HEAD の助言として出す。"""
    _assert_escalated(_single(_finding(reviewed_head=OTHER_HEAD)), EscalationCode.HEAD_MISMATCH)


def test_insufficient_evidence_is_escalated() -> None:
    """根拠の欄が無い指摘を、確信ありの助言にする。"""
    _assert_escalated(_single(_finding(evidence=None)), EscalationCode.INSUFFICIENT_EVIDENCE)


def test_design_judgment_marker_is_always_escalated() -> None:
    """【要設計判断】の指摘を、Jev の回答だけで助言にする。"""
    _assert_escalated(_single(_finding(marker=Marker.DESIGN_JUDGMENT)), EscalationCode.DESIGN_JUDGMENT_MARKER)


def test_verified_finding_is_not_rejected_by_the_model() -> None:
    """【検証済み】の指摘を、Jev の reject で却下する助言を出す。"""
    decision = _single(_finding(marker=Marker.VERIFIED), applies=NO, proposal="reject")
    _assert_escalated(decision, EscalationCode.VERIFIED_FINDING_REJECTED)


# --- severity 保護と重複候補 ----------------------------------------------------


def test_severe_findings_are_never_rejected_or_deferred_by_the_model() -> None:
    """high の指摘を、別 Issue への先送り（または却下）の助言で出す。"""
    decision = _single(_finding(severity=Severity.HIGH), proposal="separate_issue")
    _assert_escalated(decision, EscalationCode.SEVERE_NOT_FIX_NOW)


def test_severity_protection_follows_the_duplicate_group() -> None:
    """high の重複候補を持つ low の指摘を、reject の助言で出す（重複候補の severity を保護に含めない）。"""
    low, high = _finding("alpha", "L-1", severity=Severity.LOW), _finding("beta", "H-1", severity=Severity.HIGH)
    outcomes = [
        _finding_outcome(low, applies=NO, proposal="reject", request_sha="a" * 64),
        _finding_outcome(high, applies=NO, proposal="reject", request_sha="b" * 64),
        _pair_outcome(low, high, YES),
    ]
    _assert_escalated(_decision(_run(_batch(low, high), outcomes), low), EscalationCode.SEVERE_NOT_FIX_NOW)


def test_duplicate_pairs_are_shown_as_candidates() -> None:
    """重複かどうか不確かな組を、確信ありの助言として出す。"""
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [
        _finding_outcome(first, request_sha="a" * 64),
        _finding_outcome(second, request_sha="b" * 64),
        _pair_outcome(first, second, UNCERTAIN),
    ]
    report = _run(_batch(first, second), outcomes)
    for mine, partner in ((first, second), (second, first)):
        decision = _decision(report, mine)
        _assert_escalated(decision, EscalationCode.DUPLICATE_UNCERTAIN)
        assert decision.duplicate_candidates == (partner.finding_id,)


def test_confident_duplicates_with_different_proposals_are_escalated() -> None:
    """同じ問題とされた組の提案が食い違ったまま、両方を助言で出す。"""
    first, second = _finding("alpha"), _finding("beta")
    outcomes = [
        _finding_outcome(first, proposal="fix_now", request_sha="a" * 64),
        _finding_outcome(second, proposal="separate_issue", request_sha="b" * 64),
        _pair_outcome(first, second, YES),
    ]
    report = _run(_batch(first, second), outcomes)
    for finding in (first, second):
        _assert_escalated(_decision(report, finding), EscalationCode.DUPLICATE_PROPOSAL_CONFLICT)
