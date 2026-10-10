import json
import re
from pathlib import Path

import pytest

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
from tools.review_triage.report import ADVISORY_NOTICE, HUMAN_REVIEW_NOTICE, render_report

FIXTURES = Path(__file__).parent / "fixtures" / "policy"
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


def _report(
    *decisions: TriageDecision,
    status: ReportStatus = ReportStatus.ADVISORY,
    unparsed_count: int = 0,
    responded: str | None = "jev-1.13.0",
) -> TriageReport:
    return TriageReport(
        repository=Repository.PARENT,
        review_dir="reviews/#999-feature-example",
        reviewed_head=HEAD,
        model_id_requested="jev-latest",
        model_id_responded=responded,
        question_set_version="1",
        thresholds=Thresholds(model_id="jev-latest"),
        status=status,
        reason=f"{status.value}: synthetic",
        decisions=decisions,
        unparsed_count=unparsed_count,
        generated_at="2026-09-26T00:00:00Z",
    )


def _rich_report() -> TriageReport:
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
            reasons=("proposal_low_confidence: proposal_ba9876543210 confidence=0.5 < 0.9", "head_unknown"),
            answers={"score_x": ScoreAnswer(type="score", score=2.0, confidence=0.8)},
        ),
        _decision("gamma/active/L-1", Proposal.REJECT),
        _decision("delta/active/I-1", Proposal.SEPARATE_ISSUE),
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


def _table_cells(row: str) -> list[str]:
    """GFM の表の行を両端の `|` の内側にあるセルに分け、各セルを inline として読まれる文字列にする。

    cmark-gfm の表と同じく、直前が `\\` の `|` はセルの区切りにしない（`table_cell` は最長一致で読むため、
    `\\` の直後の `|` は常にセルの中に入る）。各セルでは `unescape_pipes` と同じく、`|` の直前の `\\` を 1 個だけ外す。
    """
    pieces = _CELL_SEPARATOR.split(row)
    assert pieces[0] == "" and pieces[-1].strip() == ""
    return [piece.strip().replace("\\|", "|") for piece in pieces[1:-1]]


def test_report_opens_with_the_advisory_notice() -> None:
    markdown = render_report(_rich_report())
    assert markdown.startswith(f"# Jev トリアージ助言ドラフト\n\n> {ADVISORY_NOTICE}\n")
    assert HUMAN_REVIEW_NOTICE in markdown
    assert "【検証済み】" not in markdown
    assert markdown.endswith("\n") and not markdown.endswith("\n\n")


def test_missing_responded_model_is_shown_as_none() -> None:
    markdown = render_report(_report(responded=None))
    assert "| model_id_responded | （なし） |" in markdown.splitlines()


@pytest.mark.parametrize("status", [ReportStatus.FALLBACK, ReportStatus.UNCALIBRATED])
def test_non_advisory_status_points_back_to_the_existing_procedure(status: ReportStatus) -> None:
    decision = _decision("alpha/active/H-1", Proposal.HUMAN_REVIEW, reasons=("report_fallback",))
    markdown = render_report(_report(decision, status=status))
    assert f"> status が `{status.value}` のため、全件を人間確認（`human_review`）に回した。" in markdown


def test_advisory_status_has_no_fallback_notice() -> None:
    assert "全件を人間確認" not in render_report(_rich_report())


def test_unparsed_blocks_are_called_out() -> None:
    markdown = render_report(_report(unparsed_count=2))
    assert "> 解釈できなかったブロックが 2 件ある。" in markdown
    assert "解釈できなかったブロック" not in render_report(_report())


def test_empty_report_says_there_are_no_findings() -> None:
    markdown = render_report(_report())
    assert markdown.count("指摘はない。") == 2
    assert "| `human_review` | 0 |" in markdown.splitlines()


def test_recorded_counts_are_serialized_and_rendered() -> None:
    data = _report().model_dump()
    data.update(decided_count=2, unverified_count=1)
    report = TriageReport.model_validate(data)
    markdown = render_report(report)
    assert report.model_dump(mode="json")["decided_count"] == 2
    assert report.model_dump(mode="json")["unverified_count"] == 1
    assert "| decided_count | `2` |" in markdown.splitlines()
    assert "| unverified_count | `1` |" in markdown.splitlines()
    assert "判断済みの 2 件は件数のみを記録し、助言対象から除外した。" in markdown
    assert "確認未完了の候補 1 件は Jev に送信せず、人間確認に回した。" in markdown


def test_old_v1_report_defaults_recorded_counts_to_zero() -> None:
    data = _rich_report().model_dump(mode="json")
    data.pop("decided_count", None)
    data.pop("unverified_count", None)
    report = TriageReport.model_validate(data)
    assert report.model_dump(mode="json")["decided_count"] == 0
    assert report.model_dump(mode="json")["unverified_count"] == 0


@pytest.mark.parametrize("field", ["decided_count", "unverified_count"])
def test_recorded_counts_cannot_be_negative(field: str) -> None:
    data = _report().model_dump()
    data[field] = -1
    with pytest.raises(ValueError, match=field):
        TriageReport.model_validate(data)


def test_json_and_markdown_carry_the_same_meaning() -> None:
    report = _rich_report()
    markdown = render_report(report)
    data = json.loads(report.model_dump_json())
    sections = _sections(markdown)
    assert list(sections) == [decision["finding_id"] for decision in data["decisions"]]
    table_rows = [line for line in markdown.splitlines() if line.startswith("| ") and line[2].isdigit()]
    assert len(table_rows) == len(data["decisions"])
    for proposal in Proposal:
        count = sum(1 for decision in data["decisions"] if decision["proposal"] == proposal.value)
        assert f"| `{proposal.value}` | {count} |" in markdown.splitlines()
    for row, decision in zip(table_rows, data["decisions"], strict=True):
        body = sections[decision["finding_id"]]
        assert f"- 提案: `{decision['proposal']}`（助言）" in body
        summary = decision["severity_summary"]
        highest, lowest = summary["highest"], summary["lowest"]
        agreement = "一致" if summary["agreement"] else "不一致"
        assert f" | `{highest}` / `{lowest}` / {agreement} | " in row
        assert f"- severity: 最高 `{highest}` / 最低 `{lowest}` / {agreement}" in body.splitlines()
        for finding_id, severity in summary["by_model"].items():
            assert f"  - `{finding_id}`: `{severity}`" in body
        for value in decision["duplicate_candidates"] + decision["escalation_reasons"] + decision["evidence_refs"]:
            assert f"  - `{value}`" in body
        for question_id in decision["answers"]:
            assert f"  - `{question_id}`: " in body


def test_answers_show_the_raw_numbers() -> None:
    body = _sections(render_report(_rich_report()))["alpha/active/H-1"]
    assert "  - `applies_0123456789ab`: noul `0.97`" in body
    assert "  - `proposal_0123456789ab`: choice `fix_now` confidence `0.93`" in body
    assert "  - `score_x`: score `2.0` confidence `0.8`" in _sections(render_report(_rich_report()))["beta/active/M-1"]


@pytest.mark.parametrize(
    ("raw", "rendered", "shown"),
    [
        ("a|b/active/L-1", "`a\\|b/active/L-1`", "`a|b/active/L-1`"),
        # raw の `\|` は `|` だけが `\|` になる（`\\|`）。
        # cmark-gfm は `|` の直前の `\` を 1 個だけ外すので、表示は raw の `\|` に戻る
        ("a\\|b/active/L-1", "`a\\\\|b/active/L-1`", "`a\\|b/active/L-1`"),
        ("a`b/active/L-1", "``a`b/active/L-1``", "``a`b/active/L-1``"),
        ("`ab/active/L-1", "`` `ab/active/L-1 ``", "`` `ab/active/L-1 ``"),
        ("a\nb/active/L-1", "`a\\nb/active/L-1`", "`a\\nb/active/L-1`"),
        ("a\rb/active/L-1", "`a\\rb/active/L-1`", "`a\\rb/active/L-1`"),
        ("a\x01b/active/L-1", "`a\\x01b/active/L-1`", "`a\\x01b/active/L-1`"),
        ("a\x85b/active/L-1", "`a\\u0085b/active/L-1`", "`a\\u0085b/active/L-1`"),
        ("a\u2028b/active/L-1", "`a\\u2028b/active/L-1`", "`a\\u2028b/active/L-1`"),
        (" a ", "`  a  `", "`  a  `"),
    ],
)
def test_values_cannot_break_the_table(raw: str, rendered: str, shown: str) -> None:
    markdown = render_report(_report(_decision(raw, Proposal.FIX_NOW)))
    row = next(line for line in markdown.splitlines() if line.startswith("| 1 | "))
    assert row.startswith(f"| 1 | {rendered} | ")
    cells = _table_cells(row)
    assert len(cells) == 6
    assert cells[1] == shown


def test_unknown_answer_fields_stay_in_json_and_do_not_break_markdown() -> None:
    answer = NoulAnswer.model_validate({"type": "noul", "noul": 0.97, "calibrated": True})
    report = _report(_decision("alpha/active/H-1", Proposal.FIX_NOW, answers={"applies_0123456789ab": answer}))
    data = json.loads(report.model_dump_json())
    assert data["decisions"][0]["answers"]["applies_0123456789ab"]["calibrated"] is True
    body = _sections(render_report(report))["alpha/active/H-1"]
    assert "  - `applies_0123456789ab`: noul `0.97`" in body
    assert "calibrated" not in body


def test_render_is_deterministic() -> None:
    report = _rich_report()
    assert render_report(report) == render_report(TriageReport.model_validate_json(report.model_dump_json()))


def test_golden_report_markdown() -> None:
    """golden_report.md の再生成手順と更新の判断基準。

    golden_report.json（test_policy.py の `_mixed_case()` を `triage` にかけた report）を読み、
    `render_report` にかけた文字列で置き換える。
    理由コード・提案・候補・件数が変わる差分は fixture を置き換えず規則の変更として扱い、表記だけの差分なら置き換える。
    """
    report = TriageReport.model_validate_json((FIXTURES / "golden_report.json").read_text(encoding="utf-8"))
    assert render_report(report) == (FIXTURES / "golden_report.md").read_text(encoding="utf-8")
