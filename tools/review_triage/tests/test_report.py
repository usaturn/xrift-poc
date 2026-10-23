import re

from tools.review_triage.models import (
    ChoiceAnswer,
    NoulAnswer,
    Proposal,
    ReportStatus,
    Repository,
    ScoreAnswer,
    Severity,
    Thresholds,
    TriageDecision,
    TriageReport,
    summarize_severity,
)
from tools.review_triage.report import render_report

HEAD = "fdfc294f57fb773d9024d041d834e7ede1c7cd78"
_CELL_SEPARATOR = re.compile(r"(?<!\\)\|")


def _decision(
    finding_id: str,
    proposal: Proposal,
    *,
    severities: dict[str, Severity] | None = None,
    candidates: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
    refs: tuple[str, ...] = ("review:alpha.md#L1-L5",),
    answers: dict[str, NoulAnswer | ChoiceAnswer | ScoreAnswer] | None = None,
) -> TriageDecision:
    return TriageDecision(
        finding_id=finding_id,
        proposal=proposal,
        duplicate_candidates=candidates,
        severity_summary=summarize_severity(severities or {finding_id: Severity.LOW}),
        evidence_refs=refs,
        escalation_reasons=reasons,
        answers=dict(answers or {}),
    )


def _report(*decisions: TriageDecision) -> TriageReport:
    return TriageReport(
        repository=Repository.ENV,
        review_dir="reviews/#999-feature-example",
        reviewed_head=HEAD,
        model_id_requested="jev-latest",
        model_id_responded="jev-1.13.0",
        question_set_version="1",
        thresholds=Thresholds(model_id="jev-latest"),
        status=ReportStatus.ADVISORY,
        reason="advisory: synthetic",
        decisions=decisions,
        unparsed_count=0,
        generated_at="2026-09-26T00:00:00Z",
    )


def _rich_report() -> TriageReport:
    """提案がすべて違う 4 件の report。行や節の取り違えが、別の提案の値として表に出るようにする。"""
    return _report(
        _decision(
            "alpha/active/H-1",
            Proposal.FIX_NOW,
            severities={"alpha/active/H-1": Severity.HIGH, "beta/active/M-1": Severity.MEDIUM},
            candidates=("beta/active/M-1",),
            refs=("review:alpha.md#L10-L16", "request:" + "a" * 64, "response:req-a"),
            answers={
                "applies_0123456789ab": NoulAnswer(type="noul", noul=0.97),
                "proposal_0123456789ab": ChoiceAnswer(type="choice", choice="fix_now", confidence=0.93),
            },
        ),
        _decision(
            "beta/active/M-1",
            Proposal.HUMAN_REVIEW,
            severities={"beta/active/M-1": Severity.MEDIUM},
            reasons=("proposal_low_confidence: proposal_ba9876543210 confidence=0.5 < 0.9", "head_unknown"),
            answers={"score_x": ScoreAnswer(type="score", score=2.0, confidence=0.8)},
        ),
        _decision("gamma/active/L-1", Proposal.REJECT),
        _decision("delta/active/I-1", Proposal.SEPARATE_ISSUE, severities={"delta/active/I-1": Severity.INFO}),
    )


def _sections(markdown: str) -> dict[str, str]:
    """`## 指摘ごとの詳細` の各 `### N. `finding_id`` 節を finding_id で引けるようにする。"""
    details = markdown.split("## 指摘ごとの詳細\n", 1)[1]
    sections: dict[str, str] = {}
    for chunk in details.split("\n### ")[1:]:
        heading, _, body = chunk.partition("\n")
        finding_id = heading.split(". ", 1)[1].strip("`")
        sections[finding_id] = body
    return sections


def _summary_rows(markdown: str) -> list[str]:
    """指摘一覧の表の、番号で始まる行。"""
    return [line for line in markdown.splitlines() if line.startswith("| ") and line[2].isdigit()]


def _table_cells(row: str) -> list[str]:
    """GFM の表の行を両端の `|` の内側にあるセルに分け、各セルを inline として読まれる文字列にする。

    cmark-gfm の表と同じく、直前が `\\` の `|` はセルの区切りにしない（`table_cell` は最長一致で読むため、
    `\\` の直後の `|` は常にセルの中に入る）。各セルでは `unescape_pipes` と同じく、`|` の直前の `\\` を 1 個だけ外す。
    """
    pieces = _CELL_SEPARATOR.split(row)
    assert pieces[0] == "" and pieces[-1].strip() == ""
    return [piece.strip().replace("\\|", "|") for piece in pieces[1:-1]]


def test_json_and_markdown_carry_the_same_meaning() -> None:
    """人が読む report.md が、report.json の提案・severity・候補・理由・根拠・回答を欠くか、別の指摘の行・節に書く。"""
    report = _rich_report()
    markdown = render_report(report)
    sections = _sections(markdown)
    assert list(sections) == [decision.finding_id for decision in report.decisions]
    rows = _summary_rows(markdown)
    assert len(rows) == len(report.decisions)
    proposals = {decision.proposal.value for decision in report.decisions}
    for row, decision in zip(rows, report.decisions, strict=True):
        cells = _table_cells(row)
        summary = decision.severity_summary
        assert decision.finding_id in cells[1]
        assert decision.proposal.value in cells[2]
        assert summary.highest.value in cells[3] and summary.lowest.value in cells[3]
        body = sections[decision.finding_id]
        assert decision.proposal.value in body
        assert all(other not in body for other in proposals - {decision.proposal.value})
        for value in (
            *summary.by_model,
            *decision.duplicate_candidates,
            *decision.escalation_reasons,
            *decision.evidence_refs,
            *decision.answers,
        ):
            assert value in body


def test_values_cannot_break_the_table() -> None:
    """値の `|` が表の区切りとして読まれ、列がずれて別の列の値（提案など）として表示される。"""
    finding_id = "a|b/active/L-1"
    lines = render_report(_report(_decision(finding_id, Proposal.FIX_NOW))).splitlines()
    [row] = _summary_rows("\n".join(lines))
    header = lines[lines.index(row) - 2]
    cells = _table_cells(row)
    assert len(cells) == len(_table_cells(header))
    assert finding_id in cells[1]
    assert Proposal.FIX_NOW.value in cells[2]
