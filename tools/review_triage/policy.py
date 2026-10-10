"""Jev の回答を助言表へ変換し、不確実な結果を人間確認へ戻す判定（policy、v1）。

純粋な判定だけを持ち、入力整形・API 送信・Git 取得・ファイル入出力は行わない。
出力は助言ドラフトであり、Fix / Skip / Hold / Resolved の確定や検証済みマークの付与をしない。
"""

import enum
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tools.review_triage.models import (
    Answer,
    ChoiceAnswer,
    ClientOutcome,
    ErrorKind,
    FindingStatus,
    Marker,
    NoulAnswer,
    Proposal,
    ReportStatus,
    ReviewBatch,
    ReviewFinding,
    Severity,
    Thresholds,
    TriageDecision,
    TriageReport,
    summarize_severity,
)
from tools.review_triage.questions import (
    QUESTION_SET_VERSION,
    applies_question_id,
    proposal_question_id,
    same_question_id,
)


@enum.unique
class EscalationCode(enum.StrEnum):
    """``TriageDecision.escalation_reasons`` の各要素の先頭に置く理由コード。"""

    REPORT_FALLBACK = "report_fallback"
    REPORT_UNCALIBRATED = "report_uncalibrated"
    STATUS_RESOLVED = "status_resolved"
    STATUS_PENDING = "status_pending"
    STATUS_UNVERIFIED = "status_unverified"
    HEAD_UNKNOWN = "head_unknown"
    HEAD_MISMATCH = "head_mismatch"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DESIGN_JUDGMENT_MARKER = "design_judgment_marker"
    MISSING_OUTCOME = "missing_outcome"
    DUPLICATE_OUTCOME = "duplicate_outcome"
    INVALID_REQUEST = "invalid_request"
    ANSWER_MISSING = "answer_missing"
    ANSWER_TYPE_MISMATCH = "answer_type_mismatch"
    ANSWER_UNEXPECTED = "answer_unexpected"
    CHOICE_OUTSIDE_CRITERIA = "choice_outside_criteria"
    APPLIES_UNCERTAIN = "applies_uncertain"
    PROPOSAL_LOW_CONFIDENCE = "proposal_low_confidence"
    MODEL_CHOSE_HUMAN_REVIEW = "model_chose_human_review"
    APPLIES_PROPOSAL_CONFLICT = "applies_proposal_conflict"
    VERIFIED_FINDING_REJECTED = "verified_finding_rejected"
    SEVERE_NOT_FIX_NOW = "severe_not_fix_now"
    PAIR_INVALID = "pair_invalid"
    DUPLICATE_UNCERTAIN = "duplicate_uncertain"
    DUPLICATE_PROPOSAL_CONFLICT = "duplicate_proposal_conflict"


SEVERE_SEVERITIES = frozenset({Severity.CRITICAL, Severity.HIGH, Severity.UNKNOWN})
_DEFERRING = frozenset({Proposal.REJECT, Proposal.SEPARATE_ISSUE})
_PROPOSAL_VALUES = frozenset(proposal.value for proposal in Proposal)


def _reason(code: EscalationCode, detail: str = "") -> str:
    return f"{code.value}: {detail}" if detail else code.value


@dataclass
class _Assessment:
    """1 指摘の判定途中の状態。"""

    finding: ReviewFinding
    reasons: list[str] = field(default_factory=list[str])
    answers: dict[str, Answer] = field(default_factory=dict[str, Answer])
    refs: set[str] = field(default_factory=set[str])
    candidates: set[str] = field(default_factory=set[str])
    confident_candidates: set[str] = field(default_factory=set[str])
    applies: bool | None = None
    proposal: Proposal | None = None  # 確信ありで human_review 以外のモデルの提案


def triage(
    batch: ReviewBatch,
    outcomes: Sequence[ClientOutcome],
    *,
    thresholds: Thresholds,
    generated_at: str | None = None,
) -> TriageReport:
    """判断済みの指摘は件数だけを記録し、それ以外は 1 件ずつ decision を残す。

    ``generated_at`` を省略したときだけ UTC の現在時刻を読む。決定論が要る呼び出し側は値を渡す。
    """
    report_findings = tuple(finding for finding in batch.findings if finding.status is not FindingStatus.DECIDED)
    excluded_ids = {
        finding.finding_id
        for finding in batch.findings
        if finding.status in (FindingStatus.UNVERIFIED, FindingStatus.DECIDED)
    }
    # CLI は active だけを送る。直接渡された対象外の回答も、助言や重複判定に使わない
    outcomes = tuple(outcome for outcome in outcomes if not excluded_ids.intersection(outcome.finding_ids))
    order = {finding.finding_id: index for index, finding in enumerate(report_findings)}
    assessments = {finding.finding_id: _Assessment(finding) for finding in report_findings}
    active_count = len(batch.active_findings())

    unknown = sorted({fid for outcome in outcomes for fid in outcome.finding_ids if fid not in order})
    failures = Counter(
        outcome.error.kind.value
        for outcome in outcomes
        if outcome.error is not None and outcome.error.kind is not ErrorKind.INVALID_REQUEST
    )
    invalid_count = sum(
        1 for outcome in outcomes if outcome.error is not None and outcome.error.kind is ErrorKind.INVALID_REQUEST
    )
    requested = sorted({outcome.model_requested for outcome in outcomes})
    responded = sorted({outcome.response.model for outcome in outcomes if outcome.response is not None})

    mismatched = [model for model in requested if model != thresholds.model_id]
    notes: list[str] = []
    if unknown:
        notes.append(f"outcomes reference {len(unknown)} finding id(s) outside the batch")
    if failures:
        notes.append("client failures: " + ", ".join(f"{kind} x{count}" for kind, count in sorted(failures.items())))
    if mismatched:
        notes.append(
            f"requested model {','.join(mismatched)} does not match thresholds.model_id {thresholds.model_id}"
        )
    if active_count and not outcomes:
        notes.append(f"no outcomes for {active_count} active finding(s)")
    if unknown or failures or (active_count and not outcomes):
        status = ReportStatus.FALLBACK
    elif mismatched:
        status = ReportStatus.UNCALIBRATED
    else:
        status = ReportStatus.ADVISORY

    finding_outcomes: defaultdict[str, list[ClientOutcome]] = defaultdict(list)
    pair_outcomes: defaultdict[tuple[str, str], list[ClientOutcome]] = defaultdict(list)
    for outcome in outcomes:
        if any(fid not in order for fid in outcome.finding_ids):
            continue
        for fid in outcome.finding_ids:
            assessments[fid].refs.update(_outcome_refs(outcome))
        if outcome.purpose == "finding":
            finding_outcomes[outcome.finding_ids[0]].append(outcome)
        else:
            first, second = sorted(outcome.finding_ids, key=order.__getitem__)
            pair_outcomes[(first, second)].append(outcome)

    for assessment in assessments.values():
        assessment.reasons.extend(_structural_reasons(assessment.finding, batch.reviewed_head))
    if status is ReportStatus.ADVISORY:
        for fid, assessment in assessments.items():
            if assessment.finding.status is FindingStatus.ACTIVE:
                _assess_finding(assessment, finding_outcomes.get(fid, []), thresholds)
        for pair in sorted(pair_outcomes, key=lambda ids: (order[ids[0]], order[ids[1]])):
            _assess_pair(assessments[pair[0]], assessments[pair[1]], pair_outcomes[pair], thresholds)
        for assessment in assessments.values():
            if assessment.finding.status is FindingStatus.ACTIVE:
                _cross_check(assessment, assessments, order)
    else:
        code = (
            EscalationCode.REPORT_FALLBACK if status is ReportStatus.FALLBACK else EscalationCode.REPORT_UNCALIBRATED
        )
        for assessment in assessments.values():
            assessment.reasons.insert(0, _reason(code))

    decisions = tuple(_decision(assessments[finding.finding_id], assessments, order) for finding in report_findings)
    advisory_count = sum(1 for decision in decisions if decision.proposal is not Proposal.HUMAN_REVIEW)
    if status is ReportStatus.ADVISORY:
        notes = [f"{advisory_count} of {active_count} active finding(s) have an advisory proposal"]
    if invalid_count:
        notes.append(f"invalid_request outcomes: {invalid_count} (sender-side error, not a service failure)")
    if batch.unparsed:
        notes.append(f"unparsed blocks: {len(batch.unparsed)} (not triaged; check the review files)")
    if batch.anomalies:
        notes.append(f"input anomalies: {len(batch.anomalies)}")

    return TriageReport(
        repository=batch.repository,
        review_dir=batch.review_dir,
        reviewed_head=batch.reviewed_head,
        model_id_requested=",".join(requested) if requested else thresholds.model_id,
        model_id_responded=",".join(responded) if responded else None,
        question_set_version=QUESTION_SET_VERSION,
        thresholds=thresholds,
        status=status,
        reason=f"{status.value}: " + "; ".join(notes),
        decisions=decisions,
        unparsed_count=len(batch.unparsed),
        decided_count=sum(finding.status is FindingStatus.DECIDED for finding in batch.findings),
        unverified_count=sum(finding.status is FindingStatus.UNVERIFIED for finding in batch.findings),
        generated_at=generated_at if generated_at is not None else _utc_now(),
    )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _outcome_refs(outcome: ClientOutcome) -> set[str]:
    refs = {f"request:{outcome.request_sha256}"}
    if outcome.response is not None and outcome.response.request_id:
        refs.add(f"response:{outcome.response.request_id}")
    return refs


def _same_commit(head: str, target: str) -> bool:
    shorter, longer = sorted((head, target), key=len)
    return longer.startswith(shorter)


def _structural_reasons(finding: ReviewFinding, target_head: str) -> list[str]:
    """モデルの回答を使わない検査。resolved / pending / unverified は状態だけを理由にする。"""
    if finding.status is FindingStatus.RESOLVED:
        return [_reason(EscalationCode.STATUS_RESOLVED)]
    if finding.status is FindingStatus.PENDING:
        return [_reason(EscalationCode.STATUS_PENDING)]
    if finding.status is FindingStatus.UNVERIFIED:
        return [_reason(EscalationCode.STATUS_UNVERIFIED)]
    reasons: list[str] = []
    head = finding.reviewed_head or finding.detected_head
    if head is None:
        reasons.append(_reason(EscalationCode.HEAD_UNKNOWN))
    elif not _same_commit(head, target_head):
        reasons.append(_reason(EscalationCode.HEAD_MISMATCH, f"finding={head} target={target_head}"))
    missing = [
        name
        for name, value in (("location", finding.location), ("evidence", finding.evidence))
        if value is None or not value.strip()
    ]
    if missing:
        reasons.append(_reason(EscalationCode.INSUFFICIENT_EVIDENCE, "missing=" + ",".join(missing)))
    if finding.marker is Marker.DESIGN_JUDGMENT:
        reasons.append(_reason(EscalationCode.DESIGN_JUDGMENT_MARKER))
    return reasons


def _assess_finding(assessment: _Assessment, outcomes: Sequence[ClientOutcome], thresholds: Thresholds) -> None:
    """active な指摘の applies / proposal の回答を閾値で読む。"""
    reasons = assessment.reasons
    if not outcomes:
        reasons.append(_reason(EscalationCode.MISSING_OUTCOME))
        return
    if len(outcomes) > 1:
        reasons.append(_reason(EscalationCode.DUPLICATE_OUTCOME, f"count={len(outcomes)}"))
        return
    response = outcomes[0].response
    if response is None:
        # advisory では invalid_request 以外の失敗はレポート全体の fallback になっている
        reasons.append(_reason(EscalationCode.INVALID_REQUEST))
        return
    key = assessment.finding.finding_key
    applies_id, proposal_id = applies_question_id(key), proposal_question_id(key)
    for question_id in sorted(response.answers):
        if question_id not in (applies_id, proposal_id):
            reasons.append(_reason(EscalationCode.ANSWER_UNEXPECTED, question_id))

    for question_id in (applies_id, proposal_id):
        if question_id in response.answers:
            assessment.answers[question_id] = response.answers[question_id]

    applies = response.answers.get(applies_id)
    if applies is None:
        reasons.append(_reason(EscalationCode.ANSWER_MISSING, applies_id))
    elif not isinstance(applies, NoulAnswer):
        reasons.append(_reason(EscalationCode.ANSWER_TYPE_MISMATCH, f"{applies_id} expected noul got {applies.type}"))
    elif applies.noul >= thresholds.noul_yes_min:
        assessment.applies = True
    elif applies.noul <= thresholds.noul_no_max:
        assessment.applies = False
    else:
        reasons.append(_reason(EscalationCode.APPLIES_UNCERTAIN, f"{applies_id} noul={applies.noul!r}"))

    proposal = response.answers.get(proposal_id)
    if proposal is None:
        reasons.append(_reason(EscalationCode.ANSWER_MISSING, proposal_id))
    elif not isinstance(proposal, ChoiceAnswer):
        reasons.append(
            _reason(EscalationCode.ANSWER_TYPE_MISMATCH, f"{proposal_id} expected choice got {proposal.type}")
        )
    elif proposal.choice not in _PROPOSAL_VALUES:
        reasons.append(
            _reason(EscalationCode.CHOICE_OUTSIDE_CRITERIA, f"{proposal_id} choice={proposal.choice[:64]!r}")
        )
    elif proposal.confidence < thresholds.choice_min_confidence:
        reasons.append(
            _reason(
                EscalationCode.PROPOSAL_LOW_CONFIDENCE,
                f"{proposal_id} confidence={proposal.confidence!r} < {thresholds.choice_min_confidence!r}",
            )
        )
    elif proposal.choice == Proposal.HUMAN_REVIEW.value:
        reasons.append(_reason(EscalationCode.MODEL_CHOSE_HUMAN_REVIEW))
    else:
        assessment.proposal = Proposal(proposal.choice)

    chosen = assessment.proposal
    if chosen is not None and assessment.applies is not None:
        if assessment.applies == (chosen is Proposal.REJECT):
            applies_text = "yes" if assessment.applies else "no"
            reasons.append(
                _reason(EscalationCode.APPLIES_PROPOSAL_CONFLICT, f"applies={applies_text} proposal={chosen.value}")
            )
        if chosen is Proposal.REJECT and assessment.finding.marker is Marker.VERIFIED:
            reasons.append(_reason(EscalationCode.VERIFIED_FINDING_REJECTED))


def _assess_pair(
    first: _Assessment, second: _Assessment, outcomes: Sequence[ClientOutcome], thresholds: Thresholds
) -> None:
    """重複の組の回答を読み、候補と理由を両方の active な指摘へ付ける。"""
    question_id = same_question_id(first.finding.finding_key, second.finding.finding_key)
    problem = ""
    answer: NoulAnswer | None = None
    if len(outcomes) > 1:
        problem = f"{EscalationCode.DUPLICATE_OUTCOME.value} count={len(outcomes)}"
    elif outcomes[0].response is None:
        problem = EscalationCode.INVALID_REQUEST.value
    else:
        answers = outcomes[0].response.answers
        found = answers.get(question_id)
        extra = sorted(set(answers) - {question_id})
        if found is None:
            problem = f"{EscalationCode.ANSWER_MISSING.value} {question_id}"
        elif not isinstance(found, NoulAnswer):
            problem = f"{EscalationCode.ANSWER_TYPE_MISMATCH.value} {question_id} expected noul got {found.type}"
        elif extra:
            problem = f"{EscalationCode.ANSWER_UNEXPECTED.value} " + ",".join(extra)
        else:
            answer = found
    for mine, partner in ((first, second), (second, first)):
        if mine.finding.status is not FindingStatus.ACTIVE:
            continue
        partner_id = partner.finding.finding_id
        if answer is None:
            mine.reasons.append(_reason(EscalationCode.PAIR_INVALID, f"partner={partner_id} {problem}"))
            continue
        mine.answers[question_id] = answer
        if answer.noul >= thresholds.noul_yes_min:
            mine.candidates.add(partner_id)
            mine.confident_candidates.add(partner_id)
        elif answer.noul > thresholds.noul_no_max:
            mine.candidates.add(partner_id)
            mine.reasons.append(
                _reason(EscalationCode.DUPLICATE_UNCERTAIN, f"partner={partner_id} noul={answer.noul!r}")
            )


def _severity_group(
    assessment: _Assessment, assessments: Mapping[str, _Assessment], order: Mapping[str, int]
) -> dict[str, Severity]:
    group = {assessment.finding.finding_id: assessment.finding.severity}
    for candidate in sorted(assessment.candidates, key=order.__getitem__):
        group[candidate] = assessments[candidate].finding.severity
    return group


def _cross_check(assessment: _Assessment, assessments: Mapping[str, _Assessment], order: Mapping[str, int]) -> None:
    """重複候補を含めた severity 保護と、確信ありの重複間の提案の食い違いを検査する。"""
    chosen = assessment.proposal
    if chosen is None:
        return
    severe = sorted(
        {severity.value for severity in _severity_group(assessment, assessments, order).values()}
        & {severity.value for severity in SEVERE_SEVERITIES}
    )
    if chosen in _DEFERRING and severe:
        assessment.reasons.append(
            _reason(EscalationCode.SEVERE_NOT_FIX_NOW, f"proposal={chosen.value} severe={','.join(severe)}")
        )
    for partner in sorted(assessment.confident_candidates, key=order.__getitem__):
        other = assessments[partner].proposal
        if other is not None and other is not chosen:
            assessment.reasons.append(
                _reason(EscalationCode.DUPLICATE_PROPOSAL_CONFLICT, f"partner={partner} {chosen.value}!={other.value}")
            )


def _decision(
    assessment: _Assessment, assessments: Mapping[str, _Assessment], order: Mapping[str, int]
) -> TriageDecision:
    finding = assessment.finding
    reasons = tuple(dict.fromkeys(assessment.reasons))
    proposal = assessment.proposal if assessment.proposal is not None and not reasons else Proposal.HUMAN_REVIEW
    refs = [f"review:{finding.source_file}#L{finding.line_start}-L{finding.line_end}"]
    if finding.location and finding.location.strip():
        refs.append(f"location:{finding.location}")
    if finding.reviewed_head:
        refs.append(f"reviewed_head:{finding.reviewed_head}")
    if finding.detected_head:
        refs.append(f"detected_head:{finding.detected_head}")
    refs.extend(sorted(assessment.refs))
    return TriageDecision(
        finding_id=finding.finding_id,
        proposal=proposal,
        duplicate_candidates=tuple(sorted(assessment.candidates, key=order.__getitem__)),
        severity_summary=summarize_severity(_severity_group(assessment, assessments, order)),
        evidence_refs=tuple(refs),
        escalation_reasons=reasons,
        answers={question_id: assessment.answers[question_id] for question_id in sorted(assessment.answers)},
    )
