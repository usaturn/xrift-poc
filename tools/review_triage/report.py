"""TriageReport を人が読む Markdown にする（report、v1）。

入力は TriageReport だけで、state 本文やレビュー原文を含めない。同じ report から同じ文字列を返す。
"""

import re

from tools.review_triage.models import (
    Answer,
    ChoiceAnswer,
    NoulAnswer,
    Proposal,
    ReportStatus,
    TriageDecision,
    TriageReport,
)

ADVISORY_NOTICE = (
    "この文書は Jev の回答から機械的に作った助言ドラフトであり、最終 disposition ではない。"
    "Fix / Skip / Hold / Resolved の確定、検証済みマークの付与、PR・Issue への投稿、コード修正は、"
    "既存の review-fix-planning 手順で人間またはエージェントが行う。"
)
HUMAN_REVIEW_NOTICE = "`human_review` の指摘は、既存の review-fix-planning 手順で人間またはエージェントが判断する。"
_PROPOSAL_ORDER = (Proposal.FIX_NOW, Proposal.SEPARATE_ISSUE, Proposal.REJECT, Proposal.HUMAN_REVIEW)
# C0・DEL・C1 の制御文字と U+2028 / U+2029。str.splitlines() が行の区切りとして扱う文字をすべて含む
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
_BACKTICK_RUN = re.compile(r"`+")


def _escape_control(char: str) -> str:
    """制御文字を ``\\n`` / ``\\r`` / ``\\t``、ほかの ASCII 範囲は ``\\xNN``、非 ASCII は ``\\uXXXX`` の表記にする。"""
    if char in _CONTROL_ESCAPES:
        return _CONTROL_ESCAPES[char]
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x80 else f"\\u{code:04x}"


def _code(value: object) -> str:
    """値を 1 行の inline code にする。制御文字は ``\\n`` 形式にし、中のバッククォートより長いフェンスで囲む。"""
    text = _CONTROL.sub(lambda match: _escape_control(match.group()), str(value))
    if not text:
        return "（空）"
    fence = "`" * (max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0) + 1)
    padded = text.startswith("`") or text.endswith("`") or (text[0] == " " and text[-1] == " " and bool(text.strip()))
    pad = " " if padded else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _cell(text: str) -> str:
    """表のセルでは `|` を `\\|` にして列の区切りと区別する。"""
    return text.replace("|", "\\|")


def _answer_text(answer: Answer) -> str:
    if isinstance(answer, NoulAnswer):
        return f"noul {_code(repr(answer.noul))}"
    if isinstance(answer, ChoiceAnswer):
        return f"choice {_code(answer.choice)} confidence {_code(repr(answer.confidence))}"
    return f"score {_code(repr(answer.score))} confidence {_code(repr(answer.confidence))}"


def _items(label: str, values: list[str]) -> list[str]:
    if not values:
        return [f"- {label}: なし"]
    return [f"- {label}:"] + [f"  - {value}" for value in values]


def _overview(report: TriageReport) -> list[str]:
    thresholds = report.thresholds
    threshold_text = (
        f"model_id={thresholds.model_id} choice>={thresholds.choice_min_confidence!r} "
        f"score>={thresholds.score_min_confidence!r} noul_yes>={thresholds.noul_yes_min!r} "
        f"noul_no<={thresholds.noul_no_max!r}"
    )
    responded = _code(report.model_id_responded) if report.model_id_responded is not None else "（なし）"
    rows = [
        ("status", _code(report.status.value)),
        ("reason", _code(report.reason)),
        ("repository", _code(report.repository.value)),
        ("review_dir", _code(report.review_dir)),
        ("reviewed_head", _code(report.reviewed_head)),
        ("model_id_requested", _code(report.model_id_requested)),
        ("model_id_responded", responded),
        ("question_set_version", _code(report.question_set_version)),
        ("thresholds", _code(threshold_text)),
        ("unparsed_count", _code(report.unparsed_count)),
        ("decided_count", _code(report.decided_count)),
        ("unverified_count", _code(report.unverified_count)),
        ("generated_at", _code(report.generated_at)),
    ]
    return ["## 概要", "", "| 項目 | 値 |", "| --- | --- |"] + [
        f"| {label} | {_cell(value)} |" for label, value in rows
    ]


def _summary_row(index: int, decision: TriageDecision) -> str:
    summary = decision.severity_summary
    agreement = "一致" if summary.agreement else "不一致"
    cells = [
        str(index),
        _code(decision.finding_id),
        _code(decision.proposal.value),
        f"{_code(summary.highest.value)} / {_code(summary.lowest.value)} / {agreement}",
        str(len(decision.duplicate_candidates)),
        str(len(decision.escalation_reasons)),
    ]
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _detail(index: int, decision: TriageDecision) -> list[str]:
    summary = decision.severity_summary
    agreement = "一致" if summary.agreement else "不一致"
    lines = [f"### {index}. {_code(decision.finding_id)}", "", f"- 提案: {_code(decision.proposal.value)}（助言）"]
    lines.append(f"- severity: 最高 {_code(summary.highest.value)} / 最低 {_code(summary.lowest.value)} / {agreement}")
    lines.extend(
        f"  - {_code(finding_id)}: {_code(severity.value)}" for finding_id, severity in summary.by_model.items()
    )
    lines.extend(_items("重複候補", [_code(candidate) for candidate in decision.duplicate_candidates]))
    lines.extend(_items("エスカレーション理由", [_code(reason) for reason in decision.escalation_reasons]))
    lines.extend(_items("根拠参照", [_code(ref) for ref in decision.evidence_refs]))
    answers = [f"{_code(question_id)}: {_answer_text(answer)}" for question_id, answer in decision.answers.items()]
    lines.extend(_items("回答", answers))
    return lines + [""]


def render_report(report: TriageReport) -> str:
    """TriageReport を Markdown にする。値はすべて inline code で出し、表の構造を値で崩さない。"""
    lines = ["# Jev トリアージ助言ドラフト", "", f"> {ADVISORY_NOTICE}", ""]
    lines += _overview(report) + [""]
    if report.status is not ReportStatus.ADVISORY:
        lines += [
            f"> status が {_code(report.status.value)} のため、全件を人間確認（`human_review`）に回した。"
            "各指摘は既存の review-fix-planning 手順で判断する。理由は概要の reason を参照。",
            "",
        ]
    if report.unparsed_count:
        lines += [
            f"> 解釈できなかったブロックが {report.unparsed_count} 件ある。"
            "これらはトリアージしていないため、元レビューを review-fix-planning 手順で確認する。",
            "",
        ]
    if report.decided_count:
        lines += [f"> 判断済みの {report.decided_count} 件は件数のみを記録し、助言対象から除外した。", ""]
    if report.unverified_count:
        lines += [f"> 確認未完了の候補 {report.unverified_count} 件は Jev に送信せず、人間確認に回した。", ""]
    counts = {proposal: 0 for proposal in _PROPOSAL_ORDER}
    for decision in report.decisions:
        counts[decision.proposal] += 1
    lines += ["## 提案の内訳", "", "| 提案 | 件数 |", "| --- | --- |"]
    lines += [f"| {_code(proposal.value)} | {counts[proposal]} |" for proposal in _PROPOSAL_ORDER]
    lines += ["", HUMAN_REVIEW_NOTICE, "", "## 指摘一覧", ""]
    if report.decisions:
        lines += [
            "| # | finding_id | 提案 | severity（最高 / 最低 / 一致） | 重複候補 | 理由 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines += [_summary_row(index, decision) for index, decision in enumerate(report.decisions, start=1)]
    else:
        lines.append("指摘はない。")
    lines += ["", "## 指摘ごとの詳細", ""]
    for index, decision in enumerate(report.decisions, start=1):
        lines += _detail(index, decision)
    if not report.decisions:
        lines += ["指摘はない。", ""]
    return "\n".join(lines).rstrip("\n") + "\n"
