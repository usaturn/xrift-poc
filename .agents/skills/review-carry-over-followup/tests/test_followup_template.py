"""references/templates.md の事後調査コメントのテンプレートを、本物の fold と判定フィルタに通す。

事後調査コメントは判断の記録を読む fold に影響してはならず（PR 修正の判断を変えない）、次の再調査では
前回の事後調査として見つからなければならない（同じ内容を投稿し直さない）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

TEMPLATES = Path(__file__).resolve().parents[1] / "references/templates.md"
MARKER = "<!-- review-carry-over-followup:v1 -->"
# テンプレートの中で判断 ID を書く場所のプレースホルダ
DECISION_ID_PLACEHOLDER = "<判断 ID>"


def followup_template() -> str:
    """1 行目がマーカーの ```markdown ブロックを 1 つだけ取り出す。"""
    blocks = re.findall(r"^```markdown\n(.*?)^```$", TEMPLATES.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE)
    found = [block for block in blocks if block.startswith(f"{MARKER}\n")]
    assert len(found) == 1, f"{TEMPLATES}: 事後調査コメントのテンプレートが {len(found)} 件"
    assert DECISION_ID_PLACEHOLDER in found[0], f"テンプレートに {DECISION_ID_PLACEHOLDER} が無い"
    return found[0]


def pages_with_records(kit: Any) -> tuple[list[list[dict[str, Any]]], str]:
    skipped = f"claude-opus-5-5#L-1@{kit.short_head}"
    decisions = [kit.decision(f"claude-opus-5-5#M-1@{kit.short_head}", "fix"), kit.decision(skipped, "skip")]
    return [[kit.status_comment(101, decisions)]], skipped


def test_template_comment_does_not_change_fold(carry_over_kit: Any) -> None:
    kit = carry_over_kit
    pages, skipped = pages_with_records(kit)
    before = kit.fold(pages)
    body = followup_template().replace(DECISION_ID_PLACEHOLDER, skipped)
    after = kit.fold([*pages, [kit.comment(102, body)]])
    assert after == before


def test_template_is_found_as_a_previous_followup(carry_over_kit: Any) -> None:
    kit = carry_over_kit
    pages, skipped = pages_with_records(kit)
    body = followup_template().replace(DECISION_ID_PLACEHOLDER, skipped)
    result = kit.classify([*pages, [kit.comment(102, body)]])
    assert [(entry["comment_id"], entry["kind"]) for entry in result["followups"]] == [(102, "marker")]
    assert [(entry["id"], entry["status"]) for entry in result["candidates"]] == [(skipped, "investigated")]
