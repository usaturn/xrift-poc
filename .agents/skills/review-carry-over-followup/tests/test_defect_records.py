"""範囲外の不具合の記録のコメントを取り出す jq のフィルタ（scripts/defect_records.jq）。

記録のコメント（``.agents/out-of-scope-defects.md``）は、信頼する投稿者のコメントのうち
1 行目がマーカーのものだけである。引用や、信頼しない投稿者が同じ形で書いたコメント、状況コメント、
前回の事後調査は候補の出典にしない。
GitHub の issue comment の形をフィルタに通す。値はすべて架空の PR #900 のもの。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
FILTER = SKILL_DIR / "scripts/defect_records.jq"
MARKER = "<!-- out-of-scope-defects:v1 -->"
RECORD = (
    f"{MARKER}\n## 作業中に判明した範囲外の不具合（claude-opus-5-5）\n\n"
    "判明した場面: サイクル 1 の最終化の CI の待機 / その時点の PR の HEAD: `2222bbbb3333`\n\n"
    "- `loop.py status` の経過時間の単位が「分」になっている（値は秒）\n"
    "  - 確かめた commit と位置: `2222bbbb3333` の `.agents/skills/pr-review-fix-loop/scripts/loop.py:512`\n"
    "  - 確かめ方: コードを読んだ\n"
    "  - 回避策: 使っていない\n"
)


def records(tmp_path: Path, comments: object) -> list[dict[str, Any]]:
    """SKILL.md の手順 1 と同じ引数でフィルタを実行する。``comments`` は ``--slurp`` の出力（平らな配列も可）。"""
    path = tmp_path / "comments.json"
    path.write_text(json.dumps(comments, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["jq", "-n", "--slurpfile", "comments", str(path), "-f", str(FILTER)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_only_marker_comments_from_trusted_authors_are_records(carry_over_kit: Any, tmp_path: Path) -> None:
    kit = carry_over_kit
    crlf = RECORD.replace("\n", "\r\n")
    pages = [
        [
            kit.comment(41, RECORD, association="MEMBER"),
            # 信頼しない投稿者が、記録のコメントと同じ本文を書いた
            kit.comment(42, RECORD, association="NONE", login="someone"),
            kit.comment(43, f"次の記録を見てください。\n\n> {MARKER}\n> 本文"),
        ],
        [
            kit.status_comment(44, [kit.decision(f"claude-opus-5-5#L-1@{kit.short_head}", "skip")]),
            # Web の画面から投稿すると改行が CRLF になる
            kit.comment(45, crlf, association="COLLABORATOR"),
            kit.comment(46, "<!-- review-carry-over-followup:v1 -->\n## 持ち越し候補の再調査（前回）\n\n本文"),
        ],
    ]
    found = records(tmp_path, pages)
    assert [entry["comment_id"] for entry in found] == [41, 45]
    assert [entry["body"] for entry in found] == [RECORD, crlf]
    assert found[0]["url"].endswith("#issuecomment-41")
    assert found[0]["author"] == "usaturn"


def test_no_record_gives_an_empty_list(carry_over_kit: Any, tmp_path: Path) -> None:
    kit = carry_over_kit
    comments = [kit.comment(51, "レビューをお願いします"), kit.comment(52, None), kit.comment(53, "")]
    assert records(tmp_path, comments) == []
