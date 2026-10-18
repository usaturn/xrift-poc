"""前回の事後調査を見つけ、判断の記録の候補を分ける jq のフィルタ（scripts/previous_followups.jq）。

GitHub の issue comment の形と、本物の ``fold`` の出力をフィルタに通す。値はすべて架空の PR #900 のもの。
"""

from __future__ import annotations

from typing import Any

MARKER = "<!-- review-carry-over-followup:v1 -->"
HEADING = "## 持ち越し候補の再調査（マージ後）"
OLD_HEADING = "## 持ち越しの候補の再調査（2026-10-17、`main` = `0123abc`）"


def comment_ids(result: dict[str, Any]) -> list[int]:
    return [entry["comment_id"] for entry in result["followups"]]


def by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in result["candidates"]}


def followup(heading: str, *lines: str) -> str:
    return "\n".join((heading, "", *lines, ""))


def test_marker_and_both_headings_are_previous_followups(carry_over_kit: Any) -> None:
    kit = carry_over_kit
    pages = [
        [
            kit.comment(11, f"{MARKER}\n{HEADING}\n\n本文"),
            kit.comment(12, followup(HEADING, "本文"), association="MEMBER"),
            kit.comment(13, followup(OLD_HEADING, "本文"), association="COLLABORATOR"),
        ]
    ]
    result = kit.classify(pages)
    assert comment_ids(result) == [11, 12, 13]
    assert [entry["kind"] for entry in result["followups"]] == ["marker", "heading", "heading"]
    assert result["followups"][0]["url"].endswith("#issuecomment-11")


def test_other_comments_are_not_previous_followups(carry_over_kit: Any) -> None:
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


def test_crlf_null_body_and_pages(carry_over_kit: Any) -> None:
    kit = carry_over_kit
    ref = f"claude-opus-5-5#L-1@{kit.short_head}"
    status = kit.status_comment(61, [kit.decision(ref, "skip")])
    crlf = kit.comment(62, f"{MARKER}\r\n{HEADING}\r\n\r\n- `{ref}`\r\n")
    pages = [[status, kit.comment(63, None)], [crlf]]
    state = kit.fold(pages)
    for comments in (pages, [entry for page in pages for entry in page]):
        result = kit.run_filter(comments, state)
        assert comment_ids(result) == [62]
        assert by_id(result)[ref]["status"] == "investigated"


def test_two_previous_followups_are_both_reported(carry_over_kit: Any) -> None:
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


def test_split_and_unknown_head_ids(carry_over_kit: Any) -> None:
    kit = carry_over_kit
    split = f"Grok-4.7#L-1@{kit.short_head}/a"
    legacy = "legacy#M-1@unknown"
    pages = [
        [
            kit.status_comment(81, [kit.decision(split, "skip"), kit.decision(legacy, "skip")]),
            kit.comment(82, followup(HEADING, f"- {split}", "- legacy#M-1 は旧形式のレビュー")),
        ]
    ]
    candidates = by_id(kit.classify(pages))
    assert candidates[split]["status"] == "investigated"
    assert candidates[legacy]["status"] == "needs_check"
