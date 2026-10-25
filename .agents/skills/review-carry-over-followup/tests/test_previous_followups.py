"""前回の事後調査を見つけ、判断の記録の候補を分ける jq のフィルタ（scripts/previous_followups.jq）。

GitHub の issue comment の形と、本物の ``fold`` の出力をフィルタに通す。値はすべて架空の PR #900 のもの。
候補を誤って ``investigated`` にする方向と、候補を落とす方向だけを確かめる。前回の事後調査を見つけられない・
``needs_check`` になる方向は、調べ直して同じ内容を投稿し直すだけなので確かめない。
"""

from __future__ import annotations

from typing import Any

MARKER = "<!-- review-carry-over-followup:v1 -->"
HEADING = "## 持ち越し候補の再調査（マージ後）"


def by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in result["candidates"]}


def followup(heading: str, *lines: str) -> str:
    return "\n".join((heading, "", *lines, ""))


def test_other_comments_are_not_previous_followups(carry_over_kit: Any) -> None:
    """信頼しない投稿者の目印や状況コメントを前回の事後調査と誤読すると、候補を調べ直さずに黙って見送る。"""
    kit = carry_over_kit
    pages = [
        [
            kit.comment(21, f"{MARKER}\n{HEADING}", association="NONE", login="someone"),
            kit.comment(22, followup(HEADING), association="CONTRIBUTOR", login="someone"),
            kit.comment(23, f"前置き\n{HEADING}\n本文"),
            kit.comment(24, followup("## PR #900 の持ち越し候補の再調査", "集約先の Issue へのコメントの形")),
            kit.comment(25, None),
            kit.status_comment(26, [kit.decision(f"claude-opus-5-5#L-1@{kit.short_head}", "skip")]),
        ]
    ]
    assert kit.classify(pages)["followups"] == []


def test_candidates_are_classified_by_full_and_short_ids(carry_over_kit: Any) -> None:
    """短い形だけの一致を調査済みと誤ると、別の HEAD の判断を調べずに黙って飛ばす。"""
    kit = carry_over_kit
    full = f"claude-opus-5-5#L-1@{kit.short_head}"
    short_only = f"claude-opus-5-5#L-2@{kit.short_head}"
    missing = f"claude-opus-5-5#I-1@{kit.short_head}"
    pages = [
        [
            kit.status_comment(31, [kit.decision(ref, "skip") for ref in (full, short_only, missing)]),
            kit.comment(32, followup(HEADING, f"- `{full}`: 見送る。理由と見直す条件")),
            kit.comment(33, followup(HEADING, "- claude-opus-5-5#L-2 は短い形で書いた")),
        ]
    ]
    candidates = by_id(kit.classify(pages))
    assert (candidates[full]["status"], candidates[full]["comments"]) == ("investigated", [32])
    assert (candidates[short_only]["status"], candidates[short_only]["comments"]) == ("needs_check", [33])
    assert (candidates[missing]["status"], candidates[missing]["comments"]) == ("uninvestigated", [])


def test_ids_match_only_at_boundaries(carry_over_kit: Any) -> None:
    """ID の前後の境界や特殊文字の扱いを誤ると、調べていない候補が調査済みになる。"""
    kit = carry_over_kit
    opus = f"claude-opus-5-5#L-1@{kit.short_head}"
    suffix = f"opus-5-5#L-3@{kit.short_head}"
    grok = f"Grok-4.7#L-1@{kit.short_head}"
    pages = [
        [
            kit.status_comment(41, [kit.decision(ref, "skip") for ref in (opus, suffix, grok)]),
            kit.comment(
                42,
                followup(
                    HEADING,
                    f"- claude-opus-5-5#L-10@{kit.short_head} と xclaude-opus-5-5#L-1",
                    f"- claude-opus-5-5#L-3@{kit.short_head}",
                    f"- Grok-4x7#L-1@{kit.short_head}",
                ),
            ),
        ]
    ]
    candidates = by_id(kit.classify(pages))
    assert [candidates[ref]["status"] for ref in (opus, suffix, grok)] == ["uninvestigated"] * 3


def test_only_undecided_and_skipped_decisions_are_candidates(carry_over_kit: Any) -> None:
    """Skip・Hold・未確認の判断や carry_over が落ちると、候補を黙って調べず、既存の Issue と重複して起票する。"""
    kit = carry_over_kit
    head = kit.short_head
    decisions = [
        kit.decision(f"a#M-1@{head}", "fix"),
        kit.decision(f"a#M-2@{head}", "resolved"),
        kit.decision(f"a#L-1@{head}", "skip", carry_over={"issue": "#901"}),
        kit.decision(f"a#L-2@{head}", "hold"),
        kit.decision(f"a#U-1@{head}", "unverified", severity=None),
    ]
    candidates = kit.classify([[kit.status_comment(51, decisions)]])["candidates"]
    assert [(entry["id"], entry["state"], entry["carry_over"]) for entry in candidates] == [
        (f"a#L-1@{head}", "skip", {"issue": "#901"}),
        (f"a#L-2@{head}", "hold", None),
        (f"a#U-1@{head}", "unverified", None),
    ]


def test_two_previous_followups_are_both_reported(carry_over_kit: Any) -> None:
    """候補に一致した事後調査を最初の 1 件に絞ると、後の事後調査の訂正を読まずに古い結論で投稿しうる。"""
    kit = carry_over_kit
    ref = f"claude-opus-5-5#L-1@{kit.short_head}"
    pages = [
        [
            kit.status_comment(71, [kit.decision(ref, "skip")]),
            kit.comment(72, followup(HEADING, f"- {ref}: 見送る")),
            kit.comment(73, f"{MARKER}\n{HEADING}\n\n- {ref}: 前回の判断を補足する"),
        ]
    ]
    assert by_id(kit.classify(pages))[ref]["comments"] == [72, 73]
