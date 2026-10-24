"""Issue #494: review-records v1 のマージの関門（記録の検査・照合）と、記録の読み取りで判断を黙って落とさないこと。"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "review_records.py"
SPEC = importlib.util.spec_from_file_location("review_records", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
review_records = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_records)

HEAD_1 = "25e76f6c513f8083cb229077cde193047be4cf24"
HEAD_2 = "a78ab78d6b329b647864d4c850dc03e612a0f057"
COMMIT_2 = "63c4d79d5298e2d709d6b76b78189f012914612d"
UNSYNCED = "000000000000"

Mutation = Callable[[dict[str, Any]], None]


def seat(name: str, file_head: str, *, rerun: bool = True) -> dict[str, Any]:
    return {
        "review_file": name,
        "model": name,
        "runtime": "codex",
        "file_head": file_head,
        "mode": "Initial",
        "rerun": rerun,
        "merge_recommendation": "Approve",
    }


def decision(decision_id: str, state: str, *, change: str = "new", **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": decision_id,
        "sources": [decision_id.split("/")[0]],
        "summary": f"{decision_id} の要約",
        "locations": ["tools/example.py:1"],
        "state": state,
        "change": change,
        "severity": "Medium",
        "pr_relation": "introduced",
        "reason": "理由",
    }
    if state == "skip":
        value["revisit_when"] = "新しい根拠が出たとき"
    if state == "unverified":
        value["unverified_reason"] = "足りない確認"
    value.update(extra)
    return value


def round_one() -> dict[str, Any]:
    return {
        "schema": "review-records/v1",
        "pr": {"repository": "usaturn/xrift-poc", "number": 484},
        "round": 1,
        "reviewed_head": HEAD_1,
        "recorded_at": "2026-10-01T01:40:13Z",
        "planner": {"model": "claude-opus-5-5", "runtime": "claude-code", "agent_id": "claude-opus-5-5"},
        "seats": [seat("Grok-4.7", "25e76f6c513f"), seat("qwen3.8-max", "25e76f6c513f")],
        "decisions": [
            decision("Grok-4.7#M-1@25e76f6c513f", "fix"),
            decision("Grok-4.7#M-2@25e76f6c513f", "skip"),
            decision("Grok-4.7#L-1@25e76f6c513f/b", "skip", severity="Low"),
        ],
        "outcome": "fixes_pushed",
        "commit": HEAD_2,
    }


def round_two() -> dict[str, Any]:
    d1 = {
        "reproduced_by": "claude-opus-5-5#M-1@a78ab78d6b32",
        "usage_path": "herdr-pr-review <公開側 PR> --loop の検証",
        "result_kind": "stop",
    }
    return {
        "schema": "review-records/v1",
        "pr": {"repository": "usaturn/xrift-poc", "number": 484},
        "round": 2,
        "reviewed_head": HEAD_2,
        "recorded_at": "2026-10-01T02:25:26Z",
        "planner": {"model": "claude-opus-5-5", "runtime": "claude-code", "agent_id": "claude-opus-5-5"},
        "seats": [seat("claude-opus-5-5", "a78ab78d6b32"), seat("qwen3.8-max", "25e76f6c513f", rerun=False)],
        "decisions": [
            decision("claude-opus-5-5#M-1@a78ab78d6b32", "fix", d1=d1),
            decision("Grok-4.7#M-1@25e76f6c513f", "resolved", change="changed", previous_state="fix"),
            decision("Grok-4.7#M-2@25e76f6c513f", "skip", change="inherited"),
        ],
        "outcome": "fixes_pushed",
        "commit": COMMIT_2,
    }


def problems_of(call: Callable[[], object]) -> tuple[str, ...]:
    try:
        call()
    except review_records.RecordError as error:
        return tuple(error.problems)
    raise AssertionError("RecordError was not raised")


def set_value(*path: object, value: object) -> Mutation:
    def mutate(record: dict[str, Any]) -> None:
        target: Any = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def delete(*path: object) -> Mutation:
    def mutate(record: dict[str, Any]) -> None:
        target: Any = record
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def apply(*mutations: Mutation) -> Mutation:
    def mutate(record: dict[str, Any]) -> None:
        for mutation in mutations:
            mutation(record)

    return mutate


def add_decision(entry: dict[str, Any]) -> Mutation:
    def mutate(record: dict[str, Any]) -> None:
        record["decisions"].append(entry)

    return mutate


INVALID_RECORDS = [
    # no_fix_needed に Fix が残ると、finalize が未解決のままマージする
    pytest.param(
        apply(set_value("outcome", value="no_fix_needed"), delete("commit")), "outcome", id="no-fix-needed-with-fix"
    ),
    # 知らない state は no_fix_needed の検査と check_against の未解決の判定をすり抜ける
    pytest.param(
        apply(
            set_value("outcome", value="no_fix_needed"),
            delete("commit"),
            set_value("decisions", 0, "state", value="wontfix"),
        ),
        "decisions[0].state",
        id="unknown-state",
    ),
    # 今の HEAD をレビューしたレビュアーのいないサイクルで no_fix_needed にする
    pytest.param(
        apply(
            set_value("outcome", value="no_fix_needed"),
            delete("commit"),
            set_value("decisions", 0, "state", value="resolved"),
            set_value("seats", 0, "file_head", value=UNSYNCED),
            set_value("seats", 0, "rerun", value=False),
            set_value("seats", 1, "file_head", value=UNSYNCED),
            set_value("seats", 1, "rerun", value=False),
        ),
        "seats",
        id="no-rerun-seat",
    ),
    # 古いレビューを rerun: true と偽ると、上の検査が意味を失う
    pytest.param(
        apply(
            set_value("seats", 0, "file_head", value=UNSYNCED),
            set_value("seats", 0, "rerun", value=False),
            set_value("seats", 1, "file_head", value=UNSYNCED),
            set_value("seats", 1, "rerun", value=True),
        ),
        "seats[1].rerun",
        id="rerun-disagrees-with-heads",
    ),
    # seats が空だと上の検査をすり抜け、レビュアー 0 人の no_fix_needed が通る
    pytest.param(
        apply(
            set_value("outcome", value="no_fix_needed"),
            delete("commit"),
            set_value("decisions", 0, "state", value="resolved"),
            set_value("seats", value=[]),
        ),
        "seats",
        id="empty-seats",
    ),
    # file_head の書式を確かめないと、rerun と HEAD の照合を飛ばし、短い古い HEAD を今のレビューとして数える
    pytest.param(
        apply(
            set_value("seats", 0, "file_head", value="a78ab78"),
            set_value("seats", 0, "rerun", value=True),
            set_value("seats", 1, "file_head", value=UNSYNCED),
            set_value("seats", 1, "rerun", value=False),
        ),
        "seats[0].file_head",
        id="short-file-head",
    ),
    # 同じ ID の判断を 2 つ書けると、fold が後の記載で前を上書きし、Hold が状態から消える
    pytest.param(
        apply(
            set_value("decisions", 2, "state", value="hold"),
            delete("decisions", 2, "revisit_when"),
            add_decision(decision("Grok-4.7#L-1@25e76f6c513f/b", "skip", severity="Low")),
        ),
        "decisions",
        id="duplicate-decision-id",
    ),
    # planner.agent_id を model から作らない記録を通すと、
    # 別の planner の同じサイクルの状況コメントを自分のものとして上書きし、同じサイクルの照合もすり抜ける
    pytest.param(
        set_value("planner", "agent_id", value="gpt-6-sol"), "planner.agent_id", id="agent-id-not-derived-from-model"
    ),
    # 空白の planner.model を通すと、agent_id の導出の照合（空白でない model にだけ働く）を飛ばし、
    # 別の planner の同じサイクルの状況コメントを上書きする
    pytest.param(
        apply(set_value("planner", "model", value=" "), set_value("planner", "agent_id", value="gpt-6-sol")),
        "planner.model",
        id="blank-planner-model",
    ),
]


@pytest.mark.parametrize(("mutate", "where"), INVALID_RECORDS)
def test_invalid_records_are_rejected(mutate: Mutation, where: str) -> None:
    """マージの関門（finalize・upsert が最初に通す検査）が、
    未解決の判断・今の HEAD をレビューしていない記録・別の planner を名乗る記録を黙って通すと、
    finalize がマージするか、他者の状況コメントを上書きする。"""
    record = round_one()
    mutate(record)
    problems = problems_of(lambda: review_records.validate_record(record))
    assert all(problem.startswith(f"{where}:") for problem in problems), problems


POC_PR: dict[str, Any] = {"repository": "usaturn/xrift-poc", "number": 484}


def test_fold_keeps_the_latest_state_of_each_decision() -> None:
    """fold がサイクルの順に後の記載を優先しなくなると、
    判断の今の状態を黙って誤り、マージの判定・持ち越し・次のサイクルの照合がそれを使う。"""
    first = review_records.validate_record(round_one())
    second = review_records.validate_record(round_two())
    state = review_records.fold([(202, second), (101, first)])
    assert {entry["id"]: entry["state"] for entry in state["decisions"]} == {
        "Grok-4.7#M-1@25e76f6c513f": "resolved",
        "Grok-4.7#M-2@25e76f6c513f": "skip",
        "Grok-4.7#L-1@25e76f6c513f/b": "skip",
        "claude-opus-5-5#M-1@a78ab78d6b32": "fix",
    }
    assert [(entry["round"], entry["comment_id"]) for entry in state["rounds"]] == [(1, 101), (2, 202)]


def test_fold_keeps_split_decisions_apart() -> None:
    """指摘の部分（`/a`・`/b`）を 1 つの判断にまとめると、
    後の部分が前の部分の状態を黙って上書きし、Fix・Hold の部分が状態から消える。"""
    record = round_one()
    record["decisions"].append(decision("Grok-4.7#L-1@25e76f6c513f/a", "resolved", severity="Low"))
    state = review_records.fold([(101, review_records.validate_record(record))])
    decisions = {entry["id"]: entry for entry in state["decisions"]}
    assert decisions["Grok-4.7#L-1@25e76f6c513f/a"]["state"] == "resolved"
    assert decisions["Grok-4.7#L-1@25e76f6c513f/b"]["state"] == "skip"


def test_fold_blames_the_records_that_differ_from_the_expected_pull_request() -> None:
    """別の PR から貼り付けた状況コメントの記録を黙って混ぜると、判断の状態を誤り、マージの判定にも使われる。"""
    other = round_one()
    other["pr"] = {"repository": "usaturn/xrift-sample-world", "number": 484}
    records = [
        (7, review_records.validate_record(other)),
        (101, review_records.validate_record(round_one())),
    ]
    problems = problems_of(lambda: review_records.fold(records, expected_pr=POC_PR))
    assert problems and all(problem.startswith("comment 7:") for problem in problems)


def test_check_against_keeps_one_planner_per_round() -> None:
    """同じサイクルを別の planner が記録できると、fold が後の記録で前の判断を黙って上書きし、
    前の planner の Fix・Hold が消えたまま `no_fix_needed` に進める（境界: 並行する planner のときだけ起きる）。"""
    prior = review_records.fold(
        [
            (101, review_records.validate_record(round_one())),
            (202, review_records.validate_record(round_two())),
        ]
    )
    record = round_two()
    record["planner"] = {"model": "gpt-6-sol", "runtime": "codex", "agent_id": "gpt-6-sol"}
    # 2 人目の planner は前の状態を引き継ぐだけにし、サイクルの番号以外の検査が反応しないようにする
    for entry in record["decisions"]:
        entry["change"] = "inherited"
        entry.pop("previous_state", None)
    checked = review_records.validate_record(record)
    problems = problems_of(lambda: review_records.check_against(checked, prior))
    assert problems and all(problem.startswith("round:") for problem in problems)


def test_check_against_rejects_finishing_with_an_earlier_hold() -> None:
    """前のサイクルの Hold を残したまま `no_fix_needed` を通すと、
    finalize がマージする（記録の中だけの検査では見えない、サイクルをまたぐ未解決）。"""
    first = round_one()
    first["decisions"][1]["state"] = "hold"
    del first["decisions"][1]["revisit_when"]
    held = first["decisions"][1]["id"]
    prior = review_records.fold([(101, review_records.validate_record(first))])
    record = round_two()
    record["decisions"] = [decision("Grok-4.7#M-1@25e76f6c513f", "resolved", change="changed", previous_state="fix")]
    record["outcome"] = "no_fix_needed"
    del record["commit"]
    checked = review_records.validate_record(record)
    problems = problems_of(lambda: review_records.check_against(checked, prior))
    assert problems and all(problem.startswith("outcome:") for problem in problems)
    assert any(held in problem for problem in problems)


HUMAN_PART = "\n".join(["## review-fix-planning（claude-opus-5-5）", "", "### レビュー状況", "", "- 分類: 例"])
LOOP_COMMENT = "<!-- review-fix-planning:agent:xrift-poc-pr-review-loop -->\nPR review fix loop: round 1"


def status_body(record: dict[str, Any]) -> str:
    return review_records.compose_comment(record, HUMAN_PART)


def comment(comment_id: int, body: str, *, association: str = "OWNER") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "author": "usaturn", "author_association": association}


def api_comment(comment_id: int, body: str, *, association: str = "OWNER") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": "usaturn"}, "author_association": association}


def test_extract_rejects_two_blocks() -> None:
    """記録のブロックが 2 つあるコメントで先頭を黙って使うと、
    引用された別の記録を読む（境界: 人がコメントを編集したときだけ起きる）。"""
    first = review_records.render_block(round_one())
    second = review_records.render_block(round_two())
    assert problems_of(lambda: review_records.extract_record(first + "\n" + second))


def test_comments_from_api_keeps_every_page() -> None:
    """`--paginate --slurp` の 2 ページ目以降を落とすと、
    後のページの状況コメント（Fix・Hold）が黙って読まれず、`no_fix_needed` でマージできる。"""
    pages = [[api_comment(1, "a")], [api_comment(2, "b")]]
    assert review_records.comments_from_api(pages) == [comment(1, "a"), comment(2, "b")]


def test_collect_records_reads_trusted_round_status_comments_only() -> None:
    """書き込み権限の無い投稿者が同じ形で書いた記録（判断を Skip にした `no_fix_needed`）を読むと、
    前の状態が書き換わり、マージの判定を黙って通す。"""
    first = review_records.validate_record(round_one())
    second = review_records.validate_record(round_two())
    forged = round_two()
    forged["decisions"][0] = decision("claude-opus-5-5#M-1@a78ab78d6b32", "skip")
    forged["outcome"] = "no_fix_needed"
    del forged["commit"]
    comments = [
        comment(101, status_body(first)),
        comment(150, "普通のコメント"),
        comment(160, f"文書の例を引用する: {review_records.BLOCK_START}"),
        comment(170, LOOP_COMMENT),
        comment(180, status_body(review_records.validate_record(forged)), association="NONE"),
        comment(202, status_body(second)),
    ]
    records, _ = review_records.collect_records(comments)
    assert records == [(101, first), (202, second)]


def test_collect_records_names_every_broken_comment() -> None:
    """読むべき状況コメントの記録が壊れているときに黙って読み飛ばすと、
    そのサイクルの判断（Hold・Fix）が状態から消え、`no_fix_needed` で最終化できる。"""
    marker_lines = "<!-- review-fix-planning:agent:claude-opus-5-5 -->\n" + review_records.round_marker(1, HEAD_1)
    broken = status_body(review_records.validate_record(round_one())).replace('"round": 1,', '"round": 0,')
    comments = [
        comment(7, broken),
        comment(8, marker_lines + "\n" + review_records.BLOCK_START),
        comment(9, marker_lines + "\n本文だけで、記録のブロックが無い"),
    ]
    problems = problems_of(lambda: review_records.collect_records(comments))
    for comment_id in (7, 8, 9):
        assert any(problem.startswith(f"comment {comment_id}:") for problem in problems)
