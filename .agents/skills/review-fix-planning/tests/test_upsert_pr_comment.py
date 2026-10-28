from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "upsert_pr_comment.py"


def load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("upsert_pr_comment", HELPER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_helper()
records: Any = mod.records

MODEL = "claude-opus-5-5"
REPO = "usaturn/xrift-poc"
PR = 485
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
COMMIT = "3" * 40
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
BODY = (
    "## review-fix-planning（claude-opus-5-5）\n\n### レビュー状況\n- ok\n\n### 作業判断\n- ok\n\n### 作業結果\n- ok"
)


class FakeGh:
    def __init__(
        self,
        *,
        pr_view: dict[str, Any],
        comments: Any,
        upsert_response: dict[str, Any] | None = None,
    ) -> None:
        self.pr_view = pr_view
        self.comments = comments
        self.upsert_response = upsert_response or {"id": 99, "html_url": f"{PR_URL}#issuecomment-99"}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CompletedProcess[str]:
        argv = tuple(args)
        self.calls.append((argv, input_text))
        if argv[1:3] == ("pr", "view"):
            return CompletedProcess(list(args), 0, json.dumps(self.pr_view), "")
        if argv[1:4] == ("api", "--paginate", "--slurp"):
            return CompletedProcess(list(args), 0, json.dumps(self.comments), "")
        if argv[1:3] == ("api", "--method"):
            return CompletedProcess(list(args), 0, json.dumps(self.upsert_response), "")
        return CompletedProcess(list(args), 1, "", f"unexpected gh args: {argv}")

    def writes(self) -> list[tuple[tuple[str, ...], str | None]]:
        return [call for call in self.calls if "--method" in call[0]]


def pr_view(*, head: str = HEAD_1, state: str = "OPEN") -> dict[str, Any]:
    return {"url": PR_URL, "number": PR, "headRefOid": head, "state": state}


def comment(comment_id: int, body: str, association: str = "OWNER", login: str = "usaturn") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": login}, "author_association": association}


def seat(head: str) -> dict[str, Any]:
    return {
        "review_file": MODEL,
        "model": MODEL,
        "runtime": "claude-code",
        "file_head": head[:12],
        "mode": "Initial",
        "rerun": True,
        "merge_recommendation": "Request changes",
    }


def decision(ref: str, state: str, change: str = "new", **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": ref,
        "sources": [ref],
        "summary": "空の入力で export が落ちる",
        "locations": ["src/export.py:10"],
        "state": state,
        "change": change,
        "severity": "High",
        "pr_relation": "introduced",
        "reason": "H: PR が持ち込んだ検証済みの High",
    }
    if state == "skip":
        value["revisit_when"] = "利用者が指定したとき"
    value.update(extra)
    return value


def record(
    round_number: int = 1,
    *,
    head: str = HEAD_1,
    outcome: str = "no_fix_needed",
    decisions: list[dict[str, Any]] | None = None,
    model: str = MODEL,
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "review-records/v1",
        "pr": {"repository": REPO, "number": PR},
        "round": round_number,
        "reviewed_head": head,
        "recorded_at": f"2026-10-03T0{round_number}:00:00Z",
        "planner": {"model": model, "runtime": "claude-code", "agent_id": records.agent_id_for(model)},
        "seats": [seat(head)],
        "decisions": decisions or [],
        "outcome": outcome,
    }
    value.update(extra)
    return value


FIX_REF = f"{MODEL}#H-1@{HEAD_1[:12]}"


def awaiting_record() -> dict[str, Any]:
    return record(outcome="awaiting_approval", decisions=[decision(FIX_REF, "fix")])


def pushed_record() -> dict[str, Any]:
    return record(outcome="fixes_pushed", decisions=[decision(FIX_REF, "fix")], commit=COMMIT)


def status_comment(comment_id: int, value: dict[str, Any], association: str = "OWNER") -> dict[str, Any]:
    return comment(comment_id, records.compose_comment(value, BODY), association)


# --- record-less posting (the original behaviour and the loop controller) ---


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param("<!-- review-fix-planning:round:1:" + HEAD_1 + " -->\n### レビュー状況", id="round-marker"),
        pytest.param(
            "### レビュー状況\n<!-- review-records:v1:start -->\n```json\n{}\n```\n<!-- review-records:v1:end -->",
            id="records-block",
        ),
    ],
)
def test_record_less_body_rejects_round_markers_and_records_blocks(markdown: str) -> None:
    """記録の無いコメントに巡のマーカー・記録のブロックが入ると、後の fold が止まり、消すまでループが止まる。"""
    with pytest.raises(mod.CommentError):
        mod.compose_body("claude-opus-5", markdown)


def test_find_own_comment_ignores_other_agents() -> None:
    """他のエージェントのマーカーを自分のものと読むと、そのコメントを PATCH で黙って上書きする。"""
    comments = [
        comment(1, "<!-- review-fix-planning:agent:composer-2 -->\nnope"),
        comment(2, "<!-- review-fix-planning:agent:claude-opus-5 -->\nmine"),
        comment(3, "review-fix-planning without a marker"),
    ]
    assert mod.find_own_comment_id(comments, "claude-opus-5") == 2
    assert mod.find_own_comment_id(comments[:1], "claude-opus-5") is None


def test_find_own_comment_skips_round_comments_untrusted_authors_and_quoted_markers() -> None:
    """記録の無い投稿が、記録付きのコメント・信頼しない投稿者や引用した人のコメントを PATCH で上書きして消す。"""
    marker = "<!-- review-fix-planning:agent:claude-opus-5 -->"
    comments = [
        comment(1, f"{marker}\n<!-- review-fix-planning:round:1:{HEAD_1} -->\n## round"),
        comment(2, f"{marker}\nforged", association="NONE"),
        comment(3, f"人の補足。マーカーを引用する: {marker}"),
        comment(4, f"{marker}\nlegacy"),
    ]
    assert mod.find_own_comment_id(comments, "claude-opus-5") == 4
    assert mod.find_own_comment_id(comments[:3], "claude-opus-5") is None


# --- posting with a review-records v1 record (.agents/review-records.md 7.4) ---


def test_records_post_updates_the_same_round_after_the_push() -> None:
    """同じサイクルの更新で新しいコメントを作ると、同じサイクルのコメントが 2 件になり、消すまで次の照合が止まる。"""
    gh = FakeGh(pr_view=pr_view(head=COMMIT), comments=[status_comment(7, awaiting_record())])
    mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=pushed_record())
    (argv, payload), *_ = gh.writes()
    assert argv[3:5] == ("PATCH", f"repos/{REPO}/issues/comments/7")
    assert records.extract_record(json.loads(payload or "")["body"])["outcome"] == "fixes_pushed"


def test_records_post_creates_a_new_comment_for_the_next_round() -> None:
    """次のサイクルの記録で前のサイクルのコメントを上書きすると、前のサイクルの記録が黙って消える。"""
    first = pushed_record()
    second = record(
        2,
        head=COMMIT,
        decisions=[decision(FIX_REF, "resolved", change="changed", previous_state="fix")],
    )
    gh = FakeGh(
        pr_view=pr_view(head=COMMIT),
        comments=[
            status_comment(7, first),
            comment(8, "<!-- review-fix-planning:agent:claude-opus-5-5 -->\nlegacy"),
            comment(9, "<!-- review-fix-planning:agent:xrift-poc-pr-review-loop -->\nloop progress"),
        ],
    )
    mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=second)
    assert [call[0][3] for call in gh.writes()] == ["POST"]


def test_records_post_ignores_untrusted_comments_that_carry_markers() -> None:
    """信頼しない投稿者の偽のコメントを PATCH すると、他者のコメントを上書きし、記録は fold が読まない場所に載る。"""
    forged = status_comment(7, awaiting_record(), association="NONE")
    forged["body"] = forged["body"].replace('"fix"', '"bogus"')
    gh = FakeGh(pr_view=pr_view(), comments=[forged])
    mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=awaiting_record())
    assert [call[0][3] for call in gh.writes()] == ["POST"]


def assert_rejected(gh: FakeGh, value: dict[str, Any], *, body: str = BODY) -> None:
    with pytest.raises(mod.CommentError):
        mod.upsert(PR, REPO, MODEL, body, runner=gh, record=value)
    assert not gh.writes(), "a rejected records post must not fall back to any other write"


def test_records_post_rejects_an_invalid_record() -> None:
    """無効な記録を投稿すると、後の fold が止まる（投稿の後にエラーになる）。"""
    value = awaiting_record()
    # 他の規則（outcome と判断の state の組など）に響かない欄だけを不正にする
    value["decisions"][0]["pr_relation"] = "unrelated"
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), value)


def test_records_post_rejects_a_record_for_another_pull_request() -> None:
    """別の PR の記録をこの PR に投稿すると、後の fold が別の PR の記録として止まる（投稿の後にエラーになる）。"""
    value = awaiting_record()
    value["pr"] = {"repository": REPO, "number": 999}
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), value)


def test_records_post_requires_the_pull_request_head() -> None:
    """push が届いていないのに fixes_pushed を投稿すると、push 済みの誤った記録が残り、loop は投稿の後に止まる。"""
    assert_rejected(FakeGh(pr_view=pr_view(head=HEAD_1), comments=[]), pushed_record())


def test_no_fix_needed_record_rejects_a_head_changed_since_review() -> None:
    """修正なしの古いレビュー記録を別 HEAD に投稿すると、未レビューの HEAD を確認済みとして記録する。"""
    assert_rejected(FakeGh(pr_view=pr_view(head=HEAD_2), comments=[]), record())


def test_post_rejects_a_view_resolved_to_another_repository() -> None:
    """別リポジトリの PR 応答を採用すると、対象を取り違えた状況コメントの投稿や上書きを行う。"""
    view = pr_view()
    view["url"] = f"https://github.com/other/repo/pull/{PR}"
    value = record()
    gh = FakeGh(pr_view=view, comments=[status_comment(7, value)])
    with pytest.raises(mod.CommentError):
        mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=value)
    assert gh.writes() == []


def test_records_post_rejects_a_closed_pull_request() -> None:
    """マージの後の投稿を許すと、手動マージの後に記録を足して、finalize の Issue の更新の復旧を通せる。"""
    assert_rejected(FakeGh(pr_view=pr_view(state="MERGED"), comments=[]), awaiting_record())


def test_records_post_stops_when_another_agent_recorded_the_round() -> None:
    """別のエージェントが記録したサイクルに記録を足すと、同じサイクルの記録が 2 つになり、後の照合が止まる。"""
    other = awaiting_record()
    other["planner"] = {"model": "gpt-6", "runtime": "codex", "agent_id": "gpt-6"}
    gh = FakeGh(pr_view=pr_view(), comments=[status_comment(7, other)])
    # 判断は前の記録を引き継ぐ（巡の番号の規則のほかに拒否の理由を作らない）
    assert_rejected(gh, record(outcome="awaiting_approval", decisions=[decision(FIX_REF, "fix", change="inherited")]))


def test_records_post_stops_when_a_later_round_exists() -> None:
    """後のサイクルがあるのに前のサイクルのコメントを書き換えると、後のサイクルが照合した前提の記録が黙って変わる。"""
    later = record(2, head=COMMIT, decisions=[decision(FIX_REF, "resolved", change="changed", previous_state="fix")])
    gh = FakeGh(pr_view=pr_view(head=COMMIT), comments=[status_comment(7, pushed_record()), status_comment(8, later)])
    # 判断は後の巡の状態から変えたものにする（巡の番号の規則のほかに拒否の理由を作らない）
    rewritten = record(
        outcome="fixes_pushed",
        decisions=[decision(FIX_REF, "fix", change="changed", previous_state="resolved")],
        commit=COMMIT,
    )
    assert_rejected(gh, rewritten)


def test_records_post_rejects_no_fix_needed_with_pending_decisions() -> None:
    """Hold が残ったままの no_fix_needed を投稿すると、finalize がマージに進む。"""
    held = record(outcome="awaiting_approval", decisions=[decision(FIX_REF, "hold", severity="Medium")])
    gh = FakeGh(pr_view=pr_view(head=HEAD_2), comments=[status_comment(7, held)])
    assert_rejected(gh, record(2, head=HEAD_2))


def test_records_post_stops_on_a_broken_record() -> None:
    """壊れた記録のコメントを無視して照合すると、その判断（fix・hold）を落として投稿でき、finalize がマージする。"""
    broken = status_comment(7, awaiting_record())
    broken["body"] = broken["body"].replace('"schema": "review-records/v1"', '"schema": "review-records/v9"')
    gh = FakeGh(pr_view=pr_view(head=HEAD_2), comments=[broken])
    # 壊れた記録を読み飛ばせば最初の巡として通る入力にする（巡の番号の規則で止まらない）
    assert_rejected(gh, record(1, head=HEAD_2))


def test_records_post_rejects_markers_in_the_body() -> None:
    """本文にマーカーがある記録付きのコメントを投稿すると、マーカーや記録のブロックが重なり、後の fold が誤る。"""
    body = BODY + "\n<!-- review-fix-planning:round:1:" + HEAD_1 + " -->"
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), awaiting_record(), body=body)


# --- CLI ---


def test_cli_posts_with_a_records_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI が --records-file の記録を渡さないと、記録の無いコメントとして投稿し、そのサイクルの記録が黙って欠ける。"""
    gh = FakeGh(pr_view=pr_view(), comments=[])
    monkeypatch.setattr(mod, "GhRunner", lambda: gh)
    # 複数行・タブ・非 ASCII の自由記述も落とさずに載せる
    value = record(
        outcome="awaiting_approval",
        decisions=[decision(FIX_REF, "fix", reason="H: 1 行目\n2 行目の根拠\n\tタブ付きの行")],
    )
    record_file = tmp_path / "round.json"
    record_file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    argv = ["--pr", str(PR), "--repo", REPO, "--model-name", MODEL]
    assert mod.main([*argv, "--body-file", str(body_file), "--records-file", str(record_file)]) == 0
    assert records.extract_record(json.loads(gh.writes()[0][1] or "")["body"]) == value
