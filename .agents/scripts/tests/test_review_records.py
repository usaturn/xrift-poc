"""Issue #494: review-records v1 の検査・照合・集約・描画・取り出しと、文書の記入例。"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
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
SHA256_HEAD = "0123456789abcdef" * 4
UNSYNCED = "000000000000"

Mutation = Callable[[dict[str, Any]], None]


def provenance(agent: str = "self", role: str = "decide") -> dict[str, Any]:
    return {
        "agent_id": agent,
        "role": role,
        "provider": "example-provider",
        "effort": "high",
        "session_id": "example-session",
        "cli_version": "1.2.3",
        "run_id": "example-run",
        "request_id": "example-request",
        "skills": [{"path": ".agents/skills/example/SKILL.md", "sha256": SHA256_HEAD}],
    }


def round_v2() -> dict[str, Any]:
    value = round_one()
    value["schema"] = "review-records/v2"
    value["planner"]["provenance"] = provenance()
    for entry in value["seats"]:
        entry["provenance"] = provenance(entry["model"], "find")
    return value


def test_v2_record_keeps_session_through_render_extract_and_fold() -> None:
    value = round_v2()
    block = review_records.render_block(value)
    restored = review_records.extract_record(block)
    assert restored == value
    state = review_records.fold([(123, restored)])
    assert state["rounds"][0]["planner"]["provenance"]["session_id"] == "example-session"
    assert state["rounds"][0]["seats"][0]["provenance"]["role"] == "find"


@pytest.mark.parametrize(
    "key", ["agent_id", "role", "provider", "effort", "session_id", "cli_version", "run_id", "request_id", "skills"]
)
def test_v2_record_rejects_missing_identity_field(key: str) -> None:
    value = round_v2()
    del value["planner"]["provenance"][key]
    with pytest.raises(review_records.RecordError, match=rf"planner.provenance.{key}"):
        review_records.validate_record(value)


@pytest.mark.parametrize("bad", [None, "", "a\nline", "<!-- broken -->", 123])
def test_v2_record_rejects_unusable_session_identity(bad: object) -> None:
    value = round_v2()
    value["seats"][0]["provenance"]["session_id"] = bad
    with pytest.raises(review_records.RecordError, match="session_id"):
        review_records.validate_record(value)


def test_v2_record_accepts_explicit_unknown_but_not_bad_skill_hash() -> None:
    value = round_v2()
    metadata = value["planner"]["provenance"]
    for key in metadata:
        metadata[key] = [{"path": "unknown", "sha256": "unknown"}] if key == "skills" else "unknown"
    review_records.validate_record(value)
    metadata["skills"][0]["sha256"] = "not-a-hash"
    with pytest.raises(review_records.RecordError, match="sha256"):
        review_records.validate_record(value)


def test_v2_record_rejects_duplicate_skill_paths() -> None:
    value = round_v2()
    metadata = value["planner"]["provenance"]
    metadata["skills"].append(copy.deepcopy(metadata["skills"][0]))
    with pytest.raises(review_records.RecordError, match="duplicate"):
        review_records.validate_record(value)


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


def duplicate_second_decision(record: dict[str, Any]) -> None:
    record["decisions"].append(copy.deepcopy(record["decisions"][1]))


def share_a_source_across_bases(record: dict[str, Any]) -> None:
    record["decisions"][1]["sources"].append("Grok-4.7#M-1@25e76f6c513f")


def add_decision(entry: dict[str, Any]) -> Mutation:
    def mutate(record: dict[str, Any]) -> None:
        record["decisions"].append(entry)

    return mutate


PLANNER_D1: dict[str, Any] = {
    "reproduced_by": "planner",
    "usage_path": "通常の入口・入力・環境",
    "result_kind": "stop",
}
REPRODUCTION: dict[str, Any] = {
    "commands": ["uv run --no-sync pytest tests/test_extension.py -q"],
    "place": "公開側のコピー（.venv なし）",
    "conditions": "uv run --project tools/devenv の中から実行した",
    "result": "ModuleNotFoundError: No module named 'sphinx'（終了コード 4）",
}
PLANNER_REPRODUCTION: dict[str, Any] = {**PLANNER_D1, "reproduction": REPRODUCTION}
VERIFIER_D1: dict[str, Any] = {**PLANNER_D1, "reproduced_by": "verifier"}
VERIFIER_REPRODUCTION: dict[str, Any] = {**VERIFIER_D1, "reproduction": REPRODUCTION}
OUTCOME_CHOICES = ", ".join(f'"{value}"' for value in review_records.OUTCOMES)

INVALID_RECORDS = [
    pytest.param(set_value("extra", value=1), "extra: unknown key", id="unknown-top-level-key"),
    pytest.param(delete("planner"), "planner: required", id="missing-planner"),
    pytest.param(
        set_value("schema", value="review-records/v999"),
        "schema: must be review-records/v1 or review-records/v2",
        id="other-schema",
    ),
    pytest.param(set_value("pr", value="usaturn/xrift-poc#484"), "pr: must be an object", id="pr-as-text"),
    pytest.param(
        set_value("pr", "repository", value="xrift-poc"),
        "pr.repository: must be owner/name",
        id="repository-without-owner",
    ),
    pytest.param(
        set_value("pr", "number", value=0), "pr.number: must be an integer of 1 or more", id="pr-number-zero"
    ),
    pytest.param(set_value("round", value=True), "round: must be an integer of 1 or more", id="round-as-boolean"),
    pytest.param(
        set_value("reviewed_head", value="a78ab78"),
        "reviewed_head: must be a full lowercase commit hash",
        id="short-reviewed-head",
    ),
    pytest.param(
        set_value("recorded_at", value="2026-10-01 01:40:13"),
        "recorded_at: must be a UTC time YYYY-MM-DDTHH:MM:SSZ",
        id="time-without-utc-format",
    ),
    pytest.param(
        set_value("recorded_at", value="2026-13-01T00:00:00Z"),
        "recorded_at: must be a UTC time YYYY-MM-DDTHH:MM:SSZ",
        id="impossible-date",
    ),
    pytest.param(
        set_value("planner", "model", value=" "), "planner.model: must not be blank", id="blank-planner-model"
    ),
    pytest.param(
        set_value("planner", "runtime", value="Codex"),
        "planner.runtime: must be a lowercase runtime name",
        id="uppercase-runtime",
    ),
    pytest.param(
        set_value("planner", "runtime", value="unknown"),
        "planner.runtime: must be known for the planner",
        id="unknown-planner-runtime",
    ),
    pytest.param(
        set_value("planner", "agent_id", value="claude"),
        "planner.agent_id: must be claude-opus-5-5 (agent_id_for(planner.model))",
        id="agent-id-not-derived-from-model",
    ),
    pytest.param(set_value("seats", value=[]), "seats: must not be empty unless the round stopped", id="empty-seats"),
    pytest.param(
        set_value("seats", 0, "review_file", value="Grok-4.7.md"),
        "seats[0].review_file: must not end with .md",
        id="review-file-with-suffix",
    ),
    pytest.param(
        set_value("seats", 1, "review_file", value="Grok-4.7"),
        "seats: must not repeat a review_file",
        id="repeated-review-file",
    ),
    pytest.param(delete("seats", 0, "model"), "seats[0].model: required", id="seat-without-model"),
    pytest.param(
        set_value("seats", 0, "runtime", value="unknown"),
        "seats[0].note: required when runtime is unknown",
        id="unknown-runtime-without-note",
    ),
    pytest.param(
        set_value("seats", 0, "file_head", value="25e76f6"),
        "seats[0].file_head: must be 12 lowercase hex characters or unknown",
        id="short-file-head",
    ),
    pytest.param(
        set_value("seats", 0, "mode", value="Rerun"),
        'seats[0].mode: must be one of "Initial", "Update"',
        id="unknown-mode",
    ),
    pytest.param(set_value("seats", 0, "rerun", value="yes"), "seats[0].rerun: must be a boolean", id="rerun-as-text"),
    pytest.param(
        set_value("seats", 0, "rerun", value=False),
        "seats[0].rerun: must be true exactly when file_head is the first 12 characters of reviewed_head",
        id="rerun-disagrees-with-heads",
    ),
    pytest.param(
        set_value("seats", 0, "merge_recommendation", value="マージ推奨"),
        'seats[0].merge_recommendation: must be one of "Request changes", "Needs manual verification", '
        '"Approve", null',
        id="merge-recommendation-outside-the-three-values",
    ),
    pytest.param(
        apply(
            set_value("seats", 0, "file_head", value=UNSYNCED),
            set_value("seats", 0, "rerun", value=False),
            set_value("seats", 1, "file_head", value=UNSYNCED),
            set_value("seats", 1, "rerun", value=False),
        ),
        "seats: at least one seat must have rerun true unless the round stopped",
        id="no-rerun-seat",
    ),
    pytest.param(set_value("decisions", value={}), "decisions: must be an array", id="decisions-as-object"),
    pytest.param(
        set_value("decisions", 2, "id", value="Grok-4.7#L-1@25e76f6c513f/B"),
        "decisions[2].id: must be a finding reference <file>#<id>@<head>, optionally followed by /<part>",
        id="uppercase-part",
    ),
    pytest.param(
        set_value("decisions", 0, "sources", value=[]),
        "decisions[0].sources: must be a non-empty array",
        id="empty-sources",
    ),
    pytest.param(
        set_value("decisions", 0, "sources", value=["Grok-4.7#M-1@25e76f6c513f", "Grok-4.7#M-1@25e76f6c513f"]),
        "decisions[0].sources: must not contain duplicates",
        id="duplicate-sources",
    ),
    pytest.param(
        set_value("decisions", 0, "sources", value=["Grok-4.7#M-1"]),
        "decisions[0].sources[0]: must be a finding reference <file>#<id>@<head>",
        id="reference-without-head",
    ),
    pytest.param(
        set_value("decisions", 0, "sources", value=["Grok-4.7#M-9@25e76f6c513f"]),
        "decisions[0].sources: must contain the reference part of the decision id",
        id="sources-without-decision-id",
    ),
    pytest.param(
        set_value("decisions", 0, "summary", value="  "),
        "decisions[0].summary: must not be blank",
        id="blank-summary",
    ),
    pytest.param(
        set_value("decisions", 0, "summary", value="1 行目\n2 行目"),
        "decisions[0].summary: must be a single line without control characters",
        id="multiline-summary",
    ),
    pytest.param(
        set_value("decisions", 0, "locations", value=[]),
        "decisions[0].locations: must be a non-empty array",
        id="empty-locations",
    ),
    pytest.param(
        set_value("decisions", 0, "state", value="wontfix"),
        'decisions[0].state: must be one of "fix", "hold", "skip", "resolved", "unverified"',
        id="unknown-state",
    ),
    pytest.param(
        set_value("decisions", 0, "state", value=["fix"]),
        'decisions[0].state: must be one of "fix", "hold", "skip", "resolved", "unverified"',
        id="state-as-array",
    ),
    pytest.param(
        set_value("decisions", 0, "state", value={"kind": "fix"}),
        'decisions[0].state: must be one of "fix", "hold", "skip", "resolved", "unverified"',
        id="state-as-object",
    ),
    pytest.param(
        set_value("decisions", 0, "change", value="kept"),
        'decisions[0].change: must be one of "new", "inherited", "changed"',
        id="unknown-change",
    ),
    pytest.param(
        set_value("decisions", 0, "change", value="changed"),
        "decisions[0].previous_state: required when change is changed",
        id="changed-without-previous-state",
    ),
    pytest.param(
        apply(
            set_value("decisions", 0, "change", value="changed"),
            set_value("decisions", 0, "previous_state", value="fix"),
        ),
        "decisions[0].previous_state: must differ from state",
        id="previous-state-equal-to-state",
    ),
    pytest.param(
        set_value("decisions", 0, "previous_state", value="hold"),
        "decisions[0].previous_state: allowed only when change is changed",
        id="previous-state-on-a-new-decision",
    ),
    pytest.param(
        apply(
            set_value("decisions", 1, "state", value="fix"),
            set_value("decisions", 1, "change", value="changed"),
            set_value("decisions", 1, "previous_state", value="skip"),
            delete("decisions", 1, "revisit_when"),
        ),
        "decisions[1].new_evidence: required when previous_state is skip",
        id="reopened-skip-without-new-evidence",
    ),
    pytest.param(
        set_value("decisions", 0, "new_evidence", value="再現した"),
        "decisions[0].new_evidence: allowed only when previous_state is skip",
        id="new-evidence-without-reopening",
    ),
    pytest.param(
        delete("decisions", 1, "revisit_when"),
        "decisions[1].revisit_when: required when state is skip",
        id="skip-without-revisit-condition",
    ),
    pytest.param(
        set_value("decisions", 0, "revisit_when", value="条件"),
        "decisions[0].revisit_when: allowed only when state is skip",
        id="revisit-condition-on-fix",
    ),
    pytest.param(
        apply(set_value("decisions", 1, "state", value="unverified"), delete("decisions", 1, "revisit_when")),
        "decisions[1].unverified_reason: required when state is unverified",
        id="unverified-without-reason",
    ),
    pytest.param(
        set_value("decisions", 0, "severity", value="Major"),
        'decisions[0].severity: must be one of "Critical", "High", "Medium", "Low", "Info", null',
        id="unknown-severity",
    ),
    pytest.param(
        set_value("decisions", 0, "severity", value=None),
        "decisions[0].severity: may be null only when every source is an Unverified candidate",
        id="null-severity-for-a-finding",
    ),
    pytest.param(
        set_value("decisions", 0, "pr_relation", value="new"),
        'decisions[0].pr_relation: must be one of "introduced", "surfaced", "preexisting", "unknown"',
        id="unknown-pr-relation",
    ),
    pytest.param(
        set_value("decisions", 0, "reason", value="理由\x07"),
        "decisions[0].reason: must not contain control characters other than tabs and line breaks",
        id="control-character-in-reason",
    ),
    pytest.param(
        set_value("decisions", 0, "reason", value="理由 <!-- x -->"),
        "decisions[0].reason: must not contain '<!--' or '-->'",
        id="comment-marker-in-text",
    ),
    pytest.param(
        set_value("decisions", 0, "reason", value="a --> b"),
        "decisions[0].reason: must not contain '<!--' or '-->'",
        id="comment-end-in-text",
    ),
    pytest.param(
        set_value("decisions", 1, "d1", value=PLANNER_REPRODUCTION),
        "decisions[1].d1: allowed only when state is fix",
        id="d1-on-skip",
    ),
    pytest.param(
        apply(
            set_value("decisions", 0, "d1", value=PLANNER_REPRODUCTION),
            set_value("decisions", 0, "pr_relation", value="preexisting"),
        ),
        "decisions[0].d1: requires pr_relation introduced or surfaced",
        id="d1-on-a-preexisting-problem",
    ),
    pytest.param(
        set_value("decisions", 0, "d1", value=PLANNER_D1),
        "decisions[0].d1.reproduction: required when reproduced_by is planner or verifier",
        id="planner-d1-without-reproduction",
    ),
    pytest.param(
        set_value("decisions", 0, "d1", value=VERIFIER_D1),
        "decisions[0].d1.reproduction: required when reproduced_by is planner or verifier",
        id="verifier-d1-without-reproduction",
    ),
    pytest.param(
        set_value(
            "decisions",
            0,
            "d1",
            value={**PLANNER_D1, "reproduced_by": "Grok-4.7#M-2@25e76f6c513f"},
        ),
        "decisions[0].d1.reproduced_by: must be one of the decision's sources, planner, or verifier",
        id="d1-reproduced-by-another-finding",
    ),
    pytest.param(
        set_value("decisions", 0, "d1", value={**PLANNER_REPRODUCTION, "result_kind": "slow"}),
        'decisions[0].d1.result_kind: must be one of "stop", "wrong_result"',
        id="unknown-result-kind",
    ),
    pytest.param(
        set_value(
            "decisions",
            0,
            "d1",
            value={**PLANNER_REPRODUCTION, "reproduction": {**REPRODUCTION, "commands": []}},
        ),
        "decisions[0].d1.reproduction.commands: must be a non-empty array",
        id="reproduction-without-commands",
    ),
    pytest.param(
        set_value("decisions", 0, "carry_over", value={"candidate": "別 Issue の候補"}),
        "decisions[0].carry_over: allowed only when state is skip",
        id="carry-over-on-fix",
    ),
    pytest.param(
        set_value("decisions", 1, "carry_over", value={"issue": "#486", "candidate": "別 Issue の候補"}),
        "decisions[1].carry_over: must have exactly one of issue or candidate",
        id="carry-over-with-both",
    ),
    pytest.param(
        set_value("decisions", 1, "carry_over", value={"issue": "486"}),
        "decisions[1].carry_over.issue: must be #<number> or an issue URL",
        id="carry-over-issue-without-hash",
    ),
    pytest.param(
        duplicate_second_decision,
        "decisions: duplicate decision id: Grok-4.7#M-2@25e76f6c513f",
        id="duplicate-decision-id",
    ),
    pytest.param(
        share_a_source_across_bases,
        "decisions: Grok-4.7#M-1@25e76f6c513f is in more than one decision "
        "(Grok-4.7#M-1@25e76f6c513f, Grok-4.7#M-2@25e76f6c513f); only its parts may share it",
        id="source-shared-across-bases",
    ),
    pytest.param(
        add_decision(decision("Grok-4.7#L-1@25e76f6c513f", "skip", severity="Low")),
        "decisions: Grok-4.7#L-1@25e76f6c513f is in more than one decision "
        "(Grok-4.7#L-1@25e76f6c513f/b, Grok-4.7#L-1@25e76f6c513f); only its parts may share it",
        id="whole-reference-shared-with-its-part",
    ),
    pytest.param(
        set_value("outcome", value="done"), "outcome: must be one of " + OUTCOME_CHOICES, id="unknown-outcome"
    ),
    pytest.param(delete("commit"), "commit: required when outcome is fixes_pushed", id="pushed-without-commit"),
    pytest.param(
        set_value("commit", value="a78ab78"), "commit: must be a full lowercase commit hash", id="short-commit"
    ),
    pytest.param(
        set_value("commit", value=HEAD_1), "commit: must differ from reviewed_head", id="commit-equal-to-reviewed"
    ),
    pytest.param(
        apply(
            set_value("decisions", 0, "state", value="skip"),
            set_value("decisions", 0, "revisit_when", value="条件"),
        ),
        "outcome: fixes_pushed requires at least one fix decision",
        id="pushed-without-fix",
    ),
    pytest.param(
        set_value("outcome", value="awaiting_approval"),
        "commit: allowed only when outcome is fixes_pushed",
        id="commit-while-awaiting-approval",
    ),
    pytest.param(
        apply(set_value("outcome", value="stopped_other"), delete("commit")),
        "stop_reason: required when outcome is a stopped result",
        id="stopped-without-reason",
    ),
    pytest.param(
        set_value("stop_reason", value="理由"),
        "stop_reason: allowed only when outcome is a stopped result",
        id="stop-reason-on-a-pushed-round",
    ),
    pytest.param(
        apply(
            set_value("outcome", value="awaiting_approval"),
            delete("commit"),
            set_value("decisions", 0, "state", value="skip"),
            set_value("decisions", 0, "revisit_when", value="条件"),
        ),
        "outcome: awaiting_approval requires a fix or hold decision",
        id="awaiting-approval-without-fix-or-hold",
    ),
    pytest.param(
        apply(set_value("outcome", value="no_fix_needed"), delete("commit")),
        "outcome: no_fix_needed must not leave fix, hold, or unverified decisions",
        id="no-fix-needed-with-fix",
    ),
]

VALID_VARIANTS = [
    pytest.param(
        apply(
            set_value("seats", 0, "merge_recommendation", value=None),
            set_value("seats", 0, "note", value="Merge recommendation の記載が「マージ推奨」で、3 つの値に当たらない"),
        ),
        id="merge-recommendation-null-with-note",
    ),
    pytest.param(
        apply(
            set_value("reviewed_head", value=SHA256_HEAD),
            set_value("commit", value="f" * 64),
            set_value("seats", 0, "file_head", value=SHA256_HEAD[:12]),
            set_value("seats", 1, "file_head", value=SHA256_HEAD[:12]),
        ),
        id="sha256-heads",
    ),
    pytest.param(
        set_value("decisions", 1, "carry_over", value={"issue": "https://github.com/usaturn/xrift-poc/issues/486"}),
        id="carry-over-issue-url",
    ),
    pytest.param(
        apply(
            set_value("seats", value=[]),
            set_value("outcome", value="stopped_other"),
            set_value("stop_reason", value="レビューファイルが 1 つも無い"),
            delete("commit"),
        ),
        id="stopped-round-without-seats",
    ),
    pytest.param(
        add_decision(decision("claude-opus-5-5#U-1@a78ab78d6b32", "skip", severity=None)),
        id="unverified-candidate-with-null-severity",
    ),
    pytest.param(
        add_decision(decision("Grok-4.7#L-1@25e76f6c513f/a", "resolved", severity="Low")),
        id="parts-share-their-base",
    ),
    pytest.param(
        apply(
            set_value("seats", 0, "runtime", value="unknown"),
            set_value("seats", 0, "note", value="レビュアーの行が無い旧形式のため、ランタイムは不明"),
        ),
        id="legacy-seat-with-unknown-runtime",
    ),
    pytest.param(set_value("decisions", 0, "d1", value=PLANNER_REPRODUCTION), id="planner-reproduction-for-d1"),
    pytest.param(set_value("decisions", 0, "d1", value=VERIFIER_REPRODUCTION), id="verifier-reproduction-for-d1"),
    pytest.param(
        set_value(
            "decisions",
            0,
            "d1",
            value={
                **PLANNER_D1,
                "reproduction": {**REPRODUCTION, "commands": ["python - <<'EOF'\nprint(1)\nEOF", "cmd\t--flag"]},
            },
        ),
        id="reproduction-commands-over-several-lines",
    ),
    pytest.param(
        apply(
            set_value("decisions", 1, "reason", value="1 行目\n\t2 行目\r\n3 行目"),
            set_value("decisions", 1, "revisit_when", value="条件 1\n条件 2"),
        ),
        id="free-text-with-tabs-and-line-breaks",
    ),
    pytest.param(
        set_value("decisions", 1, "reason", value="段落 1 段落 2 段落 3"),
        id="free-text-with-unicode-separators",
    ),
]


def test_round_records_pass_validation() -> None:
    for record in (round_one(), round_two()):
        assert review_records.validate_record(record) == record


@pytest.mark.parametrize("mutate", VALID_VARIANTS)
def test_valid_variants_pass_validation(mutate: Mutation) -> None:
    record = round_one()
    mutate(record)
    assert review_records.validate_record(record) == record


@pytest.mark.parametrize(("mutate", "expected"), INVALID_RECORDS)
def test_invalid_records_are_rejected(mutate: Mutation, expected: str) -> None:
    record = round_one()
    mutate(record)
    assert expected in problems_of(lambda: review_records.validate_record(record))


def test_validation_reports_every_problem() -> None:
    record = round_one()
    del record["decisions"][1]["revisit_when"]
    del record["commit"]
    problems = problems_of(lambda: review_records.validate_record(record))
    assert "decisions[1].revisit_when: required when state is skip" in problems
    assert "commit: required when outcome is fixes_pushed" in problems


def test_a_record_must_be_an_object() -> None:
    assert problems_of(lambda: review_records.validate_record([])) == ("record: must be a JSON object",)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("opencode/deepseek-v4-flash-free", "opencode-deepseek-v4-flash-free"),
        ("Cursor Grok 4.6", "Cursor-Grok-4.6"),
        ("a / b", "a-b"),
    ],
)
def test_agent_id_for_follows_the_marker_rule(model: str, expected: str) -> None:
    assert review_records.agent_id_for(model) == expected


def test_agent_id_for_rejects_a_model_without_a_safe_id() -> None:
    assert problems_of(lambda: review_records.agent_id_for("..")) == (
        "planner.model: cannot be turned into an agent id: '..'",
    )


EMPTY_STATE: dict[str, Any] = {
    "schema": "review-records/v1",
    "pr": None,
    "rounds": [],
    "decisions": [],
    "ignored_comments": [],
}
POC_PR: dict[str, Any] = {"repository": "usaturn/xrift-poc", "number": 484}


def prior_after_round_one() -> dict[str, Any]:
    return review_records.fold([(101, review_records.validate_record(round_one()))])


def test_fold_without_records_is_empty() -> None:
    assert review_records.fold([]) == EMPTY_STATE


def test_fold_keeps_the_latest_state_merged_sources_and_planner_models() -> None:
    first = review_records.validate_record(round_one())
    second_record = round_two()
    second_record["planner"] = {"model": "gpt-6-sol", "runtime": "codex", "agent_id": "gpt-6-sol"}
    second_record["decisions"][2]["sources"] = ["Grok-4.7#M-2@25e76f6c513f", "SWE-2-Max#M-3@a78ab78d6b32"]
    second = review_records.validate_record(second_record)
    state = review_records.fold([(202, second), (101, first)])
    decisions = {entry["id"]: entry for entry in state["decisions"]}
    assert [entry["id"] for entry in state["decisions"]] == [
        "Grok-4.7#M-1@25e76f6c513f",
        "Grok-4.7#M-2@25e76f6c513f",
        "Grok-4.7#L-1@25e76f6c513f/b",
        "claude-opus-5-5#M-1@a78ab78d6b32",
    ]
    assert decisions["Grok-4.7#M-1@25e76f6c513f"]["state"] == "resolved"
    assert decisions["Grok-4.7#M-2@25e76f6c513f"]["sources"] == [
        "Grok-4.7#M-2@25e76f6c513f",
        "SWE-2-Max#M-3@a78ab78d6b32",
    ]
    history = decisions["Grok-4.7#M-2@25e76f6c513f"]["history"]
    assert [(item["planner_model"], item["outcome"]) for item in history] == [
        ("claude-opus-5-5", "fixes_pushed"),
        ("gpt-6-sol", "fixes_pushed"),
    ]
    assert decisions["Grok-4.7#L-1@25e76f6c513f/b"]["history"][0]["comment_id"] == 101
    assert [(entry["round"], entry["comment_id"], entry["commit"]) for entry in state["rounds"]] == [
        (1, 101, HEAD_2),
        (2, 202, COMMIT_2),
    ]
    assert state["pr"] == POC_PR


def test_fold_orders_one_round_by_time_and_then_comment_id() -> None:
    early = review_records.validate_record(round_one())
    late_record = round_one()
    late_record["recorded_at"] = "2026-10-01T01:50:00Z"
    late_record["decisions"][1]["state"] = "hold"
    del late_record["decisions"][1]["revisit_when"]
    late = review_records.validate_record(late_record)
    by_time = review_records.fold([(300, early), (100, late)])
    assert by_time["decisions"][1]["state"] == "hold"
    same_time = review_records.fold([(300, early), (100, review_records.validate_record(round_one()))])
    assert [entry["comment_id"] for entry in same_time["rounds"]] == [100, 300]


def test_fold_keeps_split_decisions_apart() -> None:
    record = round_one()
    record["decisions"].append(decision("Grok-4.7#L-1@25e76f6c513f/a", "resolved", severity="Low"))
    state = review_records.fold([(101, review_records.validate_record(record))])
    decisions = {entry["id"]: entry for entry in state["decisions"]}
    assert decisions["Grok-4.7#L-1@25e76f6c513f/a"]["state"] == "resolved"
    assert decisions["Grok-4.7#L-1@25e76f6c513f/b"]["state"] == "skip"
    assert decisions["Grok-4.7#L-1@25e76f6c513f/a"]["sources"] == ["Grok-4.7#L-1@25e76f6c513f"]


def test_fold_rejects_records_of_another_pull_request() -> None:
    other = round_two()
    other["pr"] = {"repository": "usaturn/xrift-sample-world", "number": 484}
    records = [
        (101, review_records.validate_record(round_one())),
        (202, review_records.validate_record(other)),
    ]
    assert problems_of(lambda: review_records.fold(records)) == ("comment 202: belongs to a different pull request",)


def test_fold_blames_the_records_that_differ_from_the_expected_pull_request() -> None:
    other = round_one()
    other["pr"] = {"repository": "usaturn/xrift-sample-world", "number": 484}
    records = [
        (7, review_records.validate_record(other)),
        (101, review_records.validate_record(round_one())),
    ]
    assert problems_of(lambda: review_records.fold(records, expected_pr=POC_PR)) == (
        "comment 7: belongs to a different pull request",
    )


def test_check_against_accepts_the_next_round() -> None:
    review_records.check_against(review_records.validate_record(round_two()), prior_after_round_one())


INVALID_NEXT_ROUNDS = [
    pytest.param(
        set_value("decisions", 2, "change", value="new"),
        "decisions[2].change: Grok-4.7#M-2@25e76f6c513f is already recorded; use inherited or changed",
        id="new-but-recorded",
    ),
    pytest.param(
        set_value("decisions", 0, "change", value="inherited"),
        "decisions[0].change: claude-opus-5-5#M-1@a78ab78d6b32 is not in the earlier records; use new",
        id="inherited-but-unknown",
    ),
    pytest.param(
        apply(set_value("decisions", 2, "state", value="fix"), delete("decisions", 2, "revisit_when")),
        "decisions[2].state: an inherited decision must keep the earlier state skip",
        id="inherited-with-another-state",
    ),
    pytest.param(
        set_value("decisions", 1, "previous_state", value="hold"),
        "decisions[1].previous_state: must be the earlier state fix",
        id="changed-from-the-wrong-state",
    ),
    pytest.param(set_value("round", value=3), "round: must be 2", id="skipped-round-number"),
    pytest.param(
        set_value("pr", "number", value=485),
        "pr: must match the earlier records of this pull request",
        id="another-pull-request",
    ),
    pytest.param(
        delete("decisions", 1),
        "decisions: must list every decision whose earlier state is fix: Grok-4.7#M-1@25e76f6c513f",
        id="earlier-fix-omitted",
    ),
    pytest.param(
        set_value(
            "decisions",
            2,
            value=decision(
                "qwen3.8-max#M-5@a78ab78d6b32",
                "fix",
                sources=["qwen3.8-max#M-5@a78ab78d6b32", "Grok-4.7#M-2@25e76f6c513f"],
            ),
        ),
        "decisions[2].sources: Grok-4.7#M-2@25e76f6c513f already belongs to Grok-4.7#M-2@25e76f6c513f",
        id="skip-reference-moved-to-a-new-decision",
    ),
    pytest.param(
        set_value("decisions", 2, value=decision("Grok-4.7#M-2@25e76f6c513f/b", "fix")),
        "decisions[2].id: Grok-4.7#M-2@25e76f6c513f is already recorded under Grok-4.7#M-2@25e76f6c513f; "
        "inherit or change it",
        id="new-part-of-a-recorded-reference",
    ),
]


@pytest.mark.parametrize(("mutate", "expected"), INVALID_NEXT_ROUNDS)
def test_check_against_rejects_inconsistent_rounds(mutate: Mutation, expected: str) -> None:
    record = round_two()
    mutate(record)
    checked = review_records.validate_record(record)
    assert expected in problems_of(lambda: review_records.check_against(checked, prior_after_round_one()))


def test_check_against_keeps_one_planner_per_round() -> None:
    prior = review_records.fold(
        [
            (101, review_records.validate_record(round_one())),
            (202, review_records.validate_record(round_two())),
        ]
    )
    record = round_two()
    record["planner"] = {"model": "gpt-6-sol", "runtime": "codex", "agent_id": "gpt-6-sol"}
    checked = review_records.validate_record(record)
    assert "round: round 2 is already recorded by claude-opus-5-5" in problems_of(
        lambda: review_records.check_against(checked, prior)
    )


def test_check_against_refuses_to_update_a_round_after_a_later_one() -> None:
    prior = review_records.fold([(202, review_records.validate_record(round_two()))])
    checked = review_records.validate_record(round_one())
    assert "round: round 1 can no longer be updated because round 2 exists" in problems_of(
        lambda: review_records.check_against(checked, prior)
    )


def test_check_against_rejects_finishing_with_an_earlier_hold() -> None:
    first = round_one()
    first["decisions"][1]["state"] = "hold"
    del first["decisions"][1]["revisit_when"]
    prior = review_records.fold([(101, review_records.validate_record(first))])
    record = round_two()
    record["decisions"] = [decision("Grok-4.7#M-1@25e76f6c513f", "resolved", change="changed", previous_state="fix")]
    record["outcome"] = "no_fix_needed"
    del record["commit"]
    checked = review_records.validate_record(record)
    assert "outcome: no_fix_needed leaves pending decisions: Grok-4.7#M-2@25e76f6c513f" in problems_of(
        lambda: review_records.check_against(checked, prior)
    )


def test_check_against_keeps_d1_of_an_inherited_fix() -> None:
    first = round_one()
    first["decisions"][0]["d1"] = PLANNER_REPRODUCTION
    prior = review_records.fold([(101, review_records.validate_record(first))])
    record = round_two()
    record["decisions"][1] = decision("Grok-4.7#M-1@25e76f6c513f", "fix", change="inherited")
    checked = review_records.validate_record(record)
    assert "decisions[1].d1: an inherited decision must keep the earlier d1" in problems_of(
        lambda: review_records.check_against(checked, prior)
    )


def test_check_against_accepts_a_renumbered_reference() -> None:
    first = round_one()
    first["decisions"].append(decision("SWE-2-Max#planner-2@25e76f6c513f", "skip", severity="Info"))
    prior = review_records.fold([(101, review_records.validate_record(first))])
    record = round_two()
    renumbered = ["SWE-2-Max#planner-2@25e76f6c513f", "SWE-2-Max#I-2@25e76f6c513f"]
    record["decisions"].append(
        decision("SWE-2-Max#planner-2@25e76f6c513f", "skip", change="inherited", severity="Info", sources=renumbered)
    )
    review_records.check_against(review_records.validate_record(record), prior)


SPLIT_BASE = "Grok-4.7#L-1@25e76f6c513f"
ANOTHER_SEAT_FINDING = "qwen3.8-max#L-2@25e76f6c513f"


def prior_with_a_part_holding_another_finding() -> dict[str, Any]:
    first = round_one()
    first["decisions"].append(
        decision(f"{SPLIT_BASE}/a", "skip", severity="Low", sources=[SPLIT_BASE, ANOTHER_SEAT_FINDING])
    )
    return review_records.fold([(101, review_records.validate_record(first))])


def test_check_against_keeps_a_reference_with_the_part_that_holds_it() -> None:
    record = round_two()
    record["decisions"].append(
        decision(
            f"{SPLIT_BASE}/b", "skip", change="inherited", severity="Low", sources=[SPLIT_BASE, ANOTHER_SEAT_FINDING]
        )
    )
    checked = review_records.validate_record(record)
    prior = prior_with_a_part_holding_another_finding()
    assert (
        "decisions[3].sources: qwen3.8-max#L-2@25e76f6c513f already belongs to Grok-4.7#L-1@25e76f6c513f/a"
        in problems_of(lambda: review_records.check_against(checked, prior))
    )


@pytest.mark.parametrize(
    ("part", "sources"),
    [
        pytest.param("a", [SPLIT_BASE, ANOTHER_SEAT_FINDING], id="part-keeps-its-own-reference"),
        pytest.param("b", [SPLIT_BASE], id="part-keeps-the-shared-base"),
    ],
)
def test_check_against_lets_parts_keep_their_references(part: str, sources: list[str]) -> None:
    record = round_two()
    record["decisions"].append(
        decision(f"{SPLIT_BASE}/{part}", "skip", change="inherited", severity="Low", sources=sources)
    )
    review_records.check_against(review_records.validate_record(record), prior_with_a_part_holding_another_finding())


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("stopped_insufficient_verification", "公開側 PR の検証環境を用意できなかった"),
        ("stopped", "environment: 公開側 PR の検証環境を用意できなかった"),
    ],
)
def test_check_against_lets_a_stopped_round_omit_earlier_fixes(outcome: str, reason: str) -> None:
    record = round_two()
    del record["decisions"][1]
    del record["commit"]
    record["outcome"] = outcome
    record["stop_reason"] = reason
    review_records.check_against(review_records.validate_record(record), prior_after_round_one())


@pytest.mark.parametrize("category", ["permission", "environment", "external"])
def test_stopped_round_preserves_category_and_pending_decisions(category: str) -> None:
    record = round_one()
    del record["commit"]
    record.update(outcome="stopped", stop_reason=f"{category}: 認証を更新できず push を完了できない")
    record["seats"] = []
    checked = review_records.validate_record(record)
    extracted = review_records.extract_record(review_records.render_block(checked))
    assert extracted == checked
    assert review_records.fold([(1, checked)])["rounds"][0]["stop_reason"] == checked["stop_reason"]


@pytest.mark.parametrize("reason", [None, "", "permission:", "external:   ", "iteration_limit: 1", "確認待ち"])
def test_stopped_round_requires_a_concrete_allowed_category(reason: object) -> None:
    record = round_one()
    del record["commit"]
    record["outcome"] = "stopped"
    if reason is not None:
        record["stop_reason"] = reason
    assert any("stop_reason" in problem for problem in problems_of(lambda: review_records.validate_record(record)))


HUMAN_PART = "\n".join(["## review-fix-planning（claude-opus-5-5）", "", "### レビュー状況", "", "- 分類: 例"])
LOOP_COMMENT = "<!-- review-fix-planning:agent:xrift-poc-pr-review-loop -->\nPR review fix loop: round 1"


def status_body(record: dict[str, Any]) -> str:
    return review_records.compose_comment(record, HUMAN_PART)


def comment(comment_id: int, body: str, *, association: str = "OWNER") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "author": "usaturn", "author_association": association}


def api_comment(comment_id: int, body: str, *, association: str = "OWNER") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": "usaturn"}, "author_association": association}


def test_round_marker_holds_the_round_number_and_the_full_head() -> None:
    marker = review_records.round_marker(2, HEAD_2)
    assert marker == f"<!-- review-fix-planning:round:2:{HEAD_2} -->"
    assert review_records.ROUND_MARKER_PATTERN.fullmatch(marker) is not None


@pytest.mark.parametrize(("round_number", "head"), [(2, "a78ab78"), (0, HEAD_2)])
def test_round_marker_rejects_a_short_head_or_round_zero(round_number: int, head: str) -> None:
    assert problems_of(lambda: review_records.round_marker(round_number, head)) == (
        "round marker: needs a round of 1 or more and a full lowercase commit hash",
    )


def test_rendered_block_round_trips_through_extract() -> None:
    record = review_records.validate_record(round_two())
    block = review_records.render_block(record)
    assert block.startswith(review_records.BLOCK_SUMMARY + "\n\n" + review_records.BLOCK_START + "\n```json\n")
    assert block.endswith("\n```\n" + review_records.BLOCK_END + "\n\n</details>")
    assert review_records.extract_record(status_body(record)) == record


def test_records_with_unicode_line_separators_round_trip() -> None:
    record = round_one()
    record["decisions"][1]["reason"] = "段落 1 段落 2 段落 3"
    checked = review_records.validate_record(record)
    assert review_records.extract_record(review_records.render_block(checked)) == checked


def test_extract_accepts_crlf_line_endings() -> None:
    record = review_records.validate_record(round_two())
    assert review_records.extract_record(status_body(record).replace("\n", "\r\n")) == record


def test_extract_returns_none_without_a_block() -> None:
    assert review_records.extract_record(LOOP_COMMENT) is None


def test_extract_rejects_two_blocks() -> None:
    block = review_records.render_block(round_one())
    assert problems_of(lambda: review_records.extract_record(block + "\n" + block)) == (
        "body: must contain exactly one review-records block",
    )


def test_extract_rejects_an_end_marker_before_the_start() -> None:
    body = review_records.BLOCK_END + "\n" + review_records.BLOCK_START
    assert problems_of(lambda: review_records.extract_record(body)) == (
        "body: the end marker comes before the start marker",
    )


def test_extract_rejects_a_block_without_a_json_fence() -> None:
    body = "\n".join([review_records.BLOCK_START, "{}", review_records.BLOCK_END])
    assert problems_of(lambda: review_records.extract_record(body)) == (
        "body: the block must hold one fenced json code block",
    )


def test_extract_rejects_invalid_json() -> None:
    body = "\n".join([review_records.BLOCK_START, "```json", "{", "```", review_records.BLOCK_END])
    problems = problems_of(lambda: review_records.extract_record(body))
    assert len(problems) == 1
    assert problems[0].startswith("body: invalid JSON at line 1:")


def test_compose_comment_puts_the_markers_before_the_body() -> None:
    record = review_records.validate_record(round_two())
    lines = status_body(record).split("\n")
    assert lines[0] == "<!-- review-fix-planning:agent:claude-opus-5-5 -->"
    assert lines[1] == review_records.round_marker(2, HEAD_2)
    assert lines[2] == "## review-fix-planning（claude-opus-5-5）"
    assert status_body(record).endswith("\n\n" + review_records.render_block(record))


def test_compose_comment_rejects_markers_in_the_body() -> None:
    body = HUMAN_PART + "\n<!-- review-fix-planning:agent:x -->\n" + review_records.BLOCK_START
    assert problems_of(lambda: review_records.compose_comment(round_one(), body)) == (
        "body: must not contain an agent marker; the helper adds it",
        "body: must not contain a records block marker; the helper adds it",
    )


def test_compose_comment_rejects_a_blank_body() -> None:
    assert problems_of(lambda: review_records.compose_comment(round_one(), "  \n")) == ("body: must not be blank",)


def test_compose_comment_rejects_a_comment_over_the_limit() -> None:
    problems = problems_of(
        lambda: review_records.compose_comment(round_one(), "x" * review_records.COMMENT_BODY_LIMIT)
    )
    assert len(problems) == 1
    assert problems[0].startswith("comment: ")
    assert problems[0].endswith("characters exceed the limit of 65536")


def test_comments_from_api_accepts_slurped_pages_and_flat_arrays() -> None:
    raw = {**api_comment(1, "a"), "html_url": "https://github.com/usaturn/xrift-poc/pull/484#issuecomment-1"}
    expected = [comment(1, "a")]
    assert review_records.comments_from_api([[raw]]) == expected
    assert review_records.comments_from_api([raw]) == expected
    assert review_records.comments_from_api([[]]) == []
    assert review_records.comments_from_api([]) == []


def test_comments_from_api_treats_a_null_body_as_empty() -> None:
    raw = {**api_comment(3, ""), "body": None}
    assert review_records.comments_from_api([raw]) == [comment(3, "")]


def test_comments_from_api_rejects_comments_without_id_or_body() -> None:
    message = "must be an object with an integer id and a body"
    payload = [{"id": "1", "body": "a"}, {"id": 2}]
    assert problems_of(lambda: review_records.comments_from_api(payload)) == (
        f"comments[0]: {message}",
        f"comments[1]: {message}",
    )


def test_comments_from_api_reports_gh_errors() -> None:
    assert problems_of(lambda: review_records.comments_from_api({"message": "Not Found"})) == (
        "comments: gh api returned an error: Not Found",
    )
    assert problems_of(lambda: review_records.comments_from_api([{"message": "Bad credentials"}])) == (
        "comments[0]: gh api returned an error: Bad credentials",
    )


def test_collect_records_reads_trusted_round_status_comments_only() -> None:
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
    records, ignored = review_records.collect_records(comments)
    assert records == [(101, first), (202, second)]
    assert ignored == [
        {
            "comment_id": 160,
            "author": "usaturn",
            "author_association": "OWNER",
            "reason": "has a records block but is not a round status comment",
        },
        {
            "comment_id": 180,
            "author": "usaturn",
            "author_association": "NONE",
            "reason": "the author is not trusted",
        },
    ]
    assert review_records.collect_records(comments, exclude_comment_ids=[202])[0] == [(101, first)]


def test_collect_records_names_every_broken_comment() -> None:
    marker_lines = "<!-- review-fix-planning:agent:claude-opus-5-5 -->\n" + review_records.round_marker(1, HEAD_1)
    broken = status_body(review_records.validate_record(round_one())).replace('"round": 1,', '"round": 0,')
    comments = [
        comment(7, broken),
        comment(8, marker_lines + "\n" + review_records.BLOCK_START),
        comment(9, marker_lines + "\n本文だけで、記録のブロックが無い"),
    ]
    problems = problems_of(lambda: review_records.collect_records(comments))
    assert "comment 7: round: must be an integer of 1 or more" in problems
    assert "comment 8: body: must contain exactly one review-records block" in problems
    assert "comment 9: body: a round status comment must contain a records block" in problems


def test_collect_records_rejects_markers_that_disagree_with_the_record() -> None:
    body = status_body(review_records.validate_record(round_one()))
    lines = body.split("\n")
    lines[0] = "<!-- review-fix-planning:agent:gpt-6-sol -->"
    lines[1] = review_records.round_marker(3, HEAD_1)
    problems = problems_of(lambda: review_records.collect_records([comment(7, "\n".join(lines))]))
    assert problems == (
        "comment 7: body: the agent marker does not match planner.agent_id",
        "comment 7: body: the round marker does not match round and reviewed_head",
    )


def test_read_state_lists_ignored_comments_and_checks_the_pull_request() -> None:
    first = review_records.validate_record(round_one())
    comments = [comment(101, status_body(first)), comment(180, status_body(first), association="CONTRIBUTOR")]
    state = review_records.read_state(comments, expected_pr=POC_PR)
    assert [entry["round"] for entry in state["rounds"]] == [1]
    assert [entry["comment_id"] for entry in state["ignored_comments"]] == [180]
    other_pr = {"repository": "usaturn/xrift-sample-world", "number": 484}
    assert problems_of(lambda: review_records.read_state(comments, expected_pr=other_pr)) == (
        "comment 101: belongs to a different pull request",
    )


def test_updating_the_same_round_excludes_its_own_comment() -> None:
    first = review_records.validate_record(round_one())
    second = review_records.validate_record(round_two())
    comments = [comment(101, status_body(first)), comment(202, status_body(second))]
    review_records.check_against(second, review_records.read_state(comments, exclude_comment_ids=[202]))
    with_own = review_records.read_state(comments)
    assert "round: round 2 is already recorded by claude-opus-5-5" in problems_of(
        lambda: review_records.check_against(second, with_own)
    )


def test_collect_records_lists_a_status_comment_with_a_line_added_on_top() -> None:
    body = "追記: 人が先頭に足した行\n" + status_body(review_records.validate_record(round_one()))
    records, ignored = review_records.collect_records([comment(7, body)])
    assert records == []
    assert [entry["reason"] for entry in ignored] == ["has a records block but is not a round status comment"]


def test_comments_from_api_reports_an_error_page_inside_slurped_output() -> None:
    payload = [[api_comment(1, "a")], {"message": "API rate limit exceeded"}]
    assert problems_of(lambda: review_records.comments_from_api(payload)) == (
        "comments[1]: gh api returned an error: API rate limit exceeded",
    )


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_fold_prints_the_current_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = status_body(review_records.validate_record(round_one()))
    pages = [[api_comment(101, body), api_comment(150, "普通のコメント")]]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pages, ensure_ascii=False)))
    assert review_records.main(["fold", "--repo", "usaturn/xrift-poc", "--pr", "484"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in state["decisions"]] == [
        "Grok-4.7#M-1@25e76f6c513f",
        "Grok-4.7#M-2@25e76f6c513f",
        "Grok-4.7#L-1@25e76f6c513f/b",
    ]
    assert state["rounds"][0]["comment_id"] == 101
    assert state["ignored_comments"] == []


def test_cli_fold_accepts_a_pull_request_without_comments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("[[]]"))
    assert review_records.main(["fold"]) == 0
    assert json.loads(capsys.readouterr().out) == EMPTY_STATE


def test_cli_fold_reports_records_of_another_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = status_body(review_records.validate_record(round_one()))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([[api_comment(101, body)]], ensure_ascii=False)))
    assert review_records.main(["fold", "--repo", "usaturn/xrift-sample-world", "--pr", "484"]) == 65
    assert "comment 101: belongs to a different pull request" in capsys.readouterr().err


def test_cli_fold_needs_both_repo_and_pr() -> None:
    with pytest.raises(SystemExit) as raised:
        review_records.main(["fold", "--repo", "usaturn/xrift-poc"])
    assert raised.value.code == 2


def test_cli_fold_exits_65_on_a_broken_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    markers = "<!-- review-fix-planning:agent:claude-opus-5-5 -->\n" + review_records.round_marker(1, HEAD_1)
    body = markers + "\n" + review_records.BLOCK_START
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([api_comment(8, body)])))
    assert review_records.main(["fold"]) == 65
    assert "comment 8: body: must contain exactly one review-records block" in capsys.readouterr().err


def test_cli_fold_reports_gh_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"message": "Not Found"}'))
    assert review_records.main(["fold"]) == 65
    assert "comments: gh api returned an error: Not Found" in capsys.readouterr().err


def test_cli_validate_checks_the_record_against_earlier_comments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first_body = status_body(review_records.validate_record(round_one()))
    second_body = status_body(review_records.validate_record(round_two()))
    comments = write_json(tmp_path / "comments.json", [[api_comment(101, first_body)]])
    with_own = write_json(tmp_path / "with-own.json", [[api_comment(101, first_body), api_comment(202, second_body)]])
    record_file = write_json(tmp_path / "round-2.json", round_two())
    assert review_records.main(["validate", "--record-file", str(record_file)]) == 0
    with_comments = ["validate", "--record-file", str(record_file), "--comments-file", str(comments)]
    assert review_records.main(with_comments) == 0
    update = ["validate", "--record-file", str(record_file), "--comments-file", str(with_own)]
    assert review_records.main([*update, "--exclude-comment", "202"]) == 0
    assert review_records.main(update) == 65
    assert "round: round 2 is already recorded by claude-opus-5-5" in capsys.readouterr().err
    broken = round_two()
    del broken["decisions"][1]
    broken_file = write_json(tmp_path / "broken.json", broken)
    arguments = ["validate", "--record-file", str(broken_file), "--comments-file", str(comments)]
    assert review_records.main(arguments) == 65
    expected = "decisions: must list every decision whose earlier state is fix: Grok-4.7#M-1@25e76f6c513f"
    assert expected in capsys.readouterr().err


def test_cli_render_prints_the_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record_file = write_json(tmp_path / "round-1.json", round_one())
    assert review_records.main(["render", "--record-file", str(record_file)]) == 0
    assert capsys.readouterr().out == review_records.render_block(round_one()) + "\n"


def test_cli_reports_an_unreadable_file_with_exit_66(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert review_records.main(["render", "--record-file", str(tmp_path / "missing.json")]) == 66
    assert "missing.json" in capsys.readouterr().err


def test_cli_reports_invalid_json_with_exit_65(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    assert review_records.main(["validate", "--record-file", str(path)]) == 65
    assert "invalid JSON" in capsys.readouterr().err


def test_cli_reports_input_that_is_not_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "latin1.json"
    path.write_bytes(b'{"reason": "\xff"}')
    assert review_records.main(["validate", "--record-file", str(path)]) == 65
    assert "input: not valid UTF-8" in capsys.readouterr().err


def test_cli_validate_ignores_an_unknown_excluded_comment(tmp_path: Path) -> None:
    body = status_body(review_records.validate_record(round_one()))
    comments = write_json(tmp_path / "comments.json", [[api_comment(101, body)]])
    record_file = write_json(tmp_path / "round-2.json", round_two())
    arguments = ["validate", "--record-file", str(record_file), "--comments-file", str(comments)]
    assert review_records.main([*arguments, "--exclude-comment", "999"]) == 0


DOCUMENT = Path(__file__).resolve().parents[2] / "review-records.md"
EXAMPLE = re.compile(
    r"<!-- review-records:example (?P<name>[a-z0-9-]+) -->\n"
    r"(?P<fence>`{3,})[a-z]*\n(?P<body>.*?)\n(?P=fence)\n",
    re.DOTALL,
)
FINDINGS_SPEC = importlib.util.spec_from_file_location("review_findings", SCRIPT.with_name("review_findings.py"))
assert FINDINGS_SPEC is not None and FINDINGS_SPEC.loader is not None
review_findings = importlib.util.module_from_spec(FINDINGS_SPEC)
FINDINGS_SPEC.loader.exec_module(review_findings)


def document_examples() -> dict[str, str]:
    text = DOCUMENT.read_text(encoding="utf-8")
    return {match["name"]: match["body"] for match in EXAMPLE.finditer(text)}


def document_rounds() -> tuple[dict[str, Any], dict[str, Any], str]:
    examples = document_examples()
    first = review_records.validate_record(json.loads(examples["round-1"]))
    comment_text = examples["status-comment-round-2"]
    second = review_records.extract_record(comment_text)
    assert second is not None
    return first, second, comment_text


def test_document_finding_example_is_read_by_from_markdown() -> None:
    # 4.2 節の記入例を、レビューファイルの読み取り（review_findings.py from-markdown）にかける。
    # 重大度の節はレビューファイルにあり記入例には無いので、テストで足す。
    value = review_findings.candidates_from_markdown(
        "## Medium\n\n" + document_examples()["finding"] + "\n",
        review_file="claude-opus-5-5.md",
        repository="usaturn/xrift-poc",
        number=900,
        model="claude-opus-5-5",
        runtime="claude-code",
    )
    (finding,) = value["findings"]
    assert "confidence" in finding
    assert finding["detected_head"] != "unknown"
    assert finding["locations"] != ["不明"]


def test_document_status_comment_is_what_compose_comment_builds() -> None:
    first, second, comment_text = document_rounds()
    review_records.check_against(second, review_records.fold([(1, first)]))
    body = comment_text.split("\n", 2)[2].removesuffix("\n\n" + review_records.render_block(second))
    assert review_records.compose_comment(second, body) == comment_text
