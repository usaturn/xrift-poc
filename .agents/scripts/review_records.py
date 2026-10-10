"""review-records v1: PR コメントで共有する判断の記録。

Issue #494。形式と規則の定義元は ``.agents/review-records.md`` にある。
このモジュールは、pr-review と review-fix-planning が受け渡す記録の
検査・照合・集約・描画・取り出しを、標準ライブラリだけで実装する。
``uv run --no-project`` で動く。

- ``fold``: 標準入力の ``gh api --paginate --slurp`` のコメント一覧から、
  判断の現在の状態を出力する
- ``validate --record-file PATH``: 巡の記録を検査する。
  ``--comments-file`` を渡すと、前の状態とも照合する
- ``render --record-file PATH``: 状況コメントに付ける記録のブロックを出力する
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

SCHEMA_ID = "review-records/v1"
PROVENANCE_SCHEMA_ID = "review-records/v2"

BLOCK_START = "<!-- review-records:v1:start -->"
BLOCK_END = "<!-- review-records:v1:end -->"
BLOCK_SUMMARY = "<details><summary>判断の記録（review-records v1）</summary>"
AGENT_MARKER_PREFIX = "<!-- review-fix-planning:agent:"
AGENT_MARKER_PATTERN = re.compile(r"<!-- review-fix-planning:agent:(?P<agent_id>[A-Za-z0-9][A-Za-z0-9._-]*) -->")
ROUND_MARKER_PREFIX = "<!-- review-fix-planning:round:"
ROUND_MARKER_PATTERN = re.compile(
    r"<!-- review-fix-planning:round:(?P<round>[1-9][0-9]*):(?P<head>[0-9a-f]{40}|[0-9a-f]{64}) -->"
)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
COMMENT_BODY_LIMIT = 65536

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_SHORT_HEAD = r"(?:[0-9a-f]{12}|unknown)"
FINDING_REF = re.compile(rf"(?P<file>{_NAME})#(?P<finding>{_NAME})@(?P<head>{_SHORT_HEAD})")
DECISION_ID = re.compile(rf"(?P<ref>{_NAME}#{_NAME}@{_SHORT_HEAD})(?:/[a-z0-9]+)?")
_FULL_HEAD = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FILE_HEAD = re.compile(_SHORT_HEAD)
_FILE_NAME = re.compile(_NAME)
_REPOSITORY = re.compile(rf"{_NAME}/{_NAME}")
_RUNTIME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_ISSUE = re.compile(r"#[1-9][0-9]*|https://github\.com/[^/\s]+/[^/\s]+/issues/[1-9][0-9]*")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_UNVERIFIED_PREFIX = "U-"

STATES = ("fix", "hold", "skip", "resolved", "unverified")
CHANGES = ("new", "inherited", "changed")
SEVERITIES = ("Critical", "High", "Medium", "Low", "Info")
PR_RELATIONS = ("introduced", "surfaced", "preexisting", "unknown")
RESULT_KINDS = ("stop", "wrong_result")
MODES = ("Initial", "Update")
MERGE_RECOMMENDATIONS = ("Request changes", "Needs manual verification", "Approve")
OUTCOMES = (
    "awaiting_approval",
    "fixes_pushed",
    "no_fix_needed",
    "stopped",
    "stopped_insufficient_verification",
    "stopped_iteration_limit",
    "stopped_no_progress",
    "stopped_other",
)

_TOP_REQUIRED = frozenset(
    {"schema", "pr", "round", "reviewed_head", "recorded_at", "planner", "seats", "decisions", "outcome"}
)
_TOP_KEYS = _TOP_REQUIRED | {"commit", "stop_reason"}
_PR_KEYS = frozenset({"repository", "number"})
_PLANNER_KEYS = frozenset({"model", "runtime", "agent_id"})
_SEAT_REQUIRED = frozenset({"review_file", "model", "runtime", "file_head", "mode", "rerun", "merge_recommendation"})
_SEAT_KEYS = _SEAT_REQUIRED | {"note"}
_DECISION_REQUIRED = frozenset(
    {"id", "sources", "summary", "locations", "state", "change", "severity", "pr_relation", "reason"}
)
_DECISION_KEYS = _DECISION_REQUIRED | {
    "previous_state",
    "new_evidence",
    "revisit_when",
    "unverified_reason",
    "d1",
    "carry_over",
}
_D1_REQUIRED = frozenset({"reproduced_by", "usage_path", "result_kind"})
_D1_KEYS = _D1_REQUIRED | {"reproduction"}
_REPRODUCTION_KEYS = frozenset({"commands", "place", "conditions", "result"})
# 判断の記録に再現を写して書く再現者（planner 自身と、review-verify の確認担当）
_OWN_REPRODUCERS = ("planner", "verifier")
_CARRY_OVER_KEYS = frozenset({"issue", "candidate"})


class RecordError(ValueError):
    """記録が形式を満たさない。

    ``problems`` に、場所付きの問題をすべて持つ。
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class _Problems:
    """検査で見つけた問題を、場所付きで集める。"""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"{where}: {message}")

    def raise_if_any(self) -> None:
        if self.items:
            raise RecordError(self.items)


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _array(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return cast(list[object], value)


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _base(decision_id: str) -> str:
    """判断 ID から /<部分> を除いた、指摘の参照の部分。"""
    return decision_id.split("/", 1)[0]


def _parts_of(ref: str, decision_ids: Iterable[str]) -> bool:
    """decision_ids がすべて、ref を基にした部分（ref/<部分>）か。"""
    return all(decision_id != ref and _base(decision_id) == ref for decision_id in decision_ids)


def _keys(
    problems: _Problems,
    where: str,
    mapping: Mapping[str, object],
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    for key in sorted(set(mapping) - allowed):
        problems.add(_join(where, key), "unknown key")
    for key in sorted(required - set(mapping)):
        problems.add(_join(where, key), "required")


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 32 or 127 <= code < 160


def _text(problems: _Problems, where: str, value: object, *, free: bool) -> None:
    """文字列の欄を検査する。

    free が偽なら 1 行の欄、真なら自由記述の欄（改行とタブを許す）。
    """
    if not isinstance(value, str):
        problems.add(where, "must be a string")
        return
    if not value.strip():
        problems.add(where, "must not be blank")
        return
    if free:
        if any(_is_control(char) and char not in "\t\n\r" for char in value):
            problems.add(where, "must not contain control characters other than tabs and line breaks")
    elif any(_is_control(char) for char in value):
        problems.add(where, "must be a single line without control characters")
    if "<!--" in value or "-->" in value:
        problems.add(where, "must not contain '<!--' or '-->'")


def _pattern(problems: _Problems, where: str, value: object, pattern: re.Pattern[str], description: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        problems.add(where, f"must be {description}")


def _choice(problems: _Problems, where: str, value: object, choices: Sequence[object]) -> None:
    if value not in choices:
        problems.add(where, "must be one of " + ", ".join(json.dumps(choice) for choice in choices))


def _conditional_text(
    problems: _Problems, where: str, mapping: Mapping[str, object], key: str, *, when: bool, condition: str
) -> None:
    """when が真なら key を必須の自由記述の欄にし、偽なら key を書かせない。"""
    path = _join(where, key)
    if when:
        if key not in mapping:
            problems.add(path, f"required when {condition}")
        else:
            _text(problems, path, mapping[key], free=True)
    elif key in mapping:
        problems.add(path, f"allowed only when {condition}")


def _valid_time(value: object) -> bool:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, _UTC_FORMAT)
    except ValueError:
        return False
    return True


def _is_unverified(ref: str) -> bool:
    match = FINDING_REF.fullmatch(ref)
    return match is not None and match["finding"].startswith(_UNVERIFIED_PREFIX)


def agent_id_for(model: str) -> str:
    """モデル識別子から、状況コメントのマーカーに使う ID を作る。

    ``/`` と半角空白を ``-`` にし、連続する ``-`` を 1 つにまとめる。
    """
    agent_id = model.replace("/", "-").replace(" ", "-")
    while "--" in agent_id:
        agent_id = agent_id.replace("--", "-")
    if _FILE_NAME.fullmatch(agent_id) is None:
        raise RecordError([f"planner.model: cannot be turned into an agent id: {model!r}"])
    return agent_id


def _check_pr(problems: _Problems, value: object) -> None:
    pr = _mapping(value)
    if pr is None:
        problems.add("pr", "must be an object")
        return
    _keys(problems, "pr", pr, _PR_KEYS, _PR_KEYS)
    if "repository" in pr:
        _pattern(problems, "pr.repository", pr["repository"], _REPOSITORY, "owner/name")
    if "number" in pr and not _positive_integer(pr["number"]):
        problems.add("pr.number", "must be an integer of 1 or more")


def identity_problems(value: object, where: str) -> list[str]:
    """実行の識別値は単一行で、コメントマーカーを含めない。"""
    problems = _Problems()
    _text(problems, where, value, free=False)
    return problems.items


def provenance_problems(value: object, where: str) -> list[str]:
    """実測された実行識別情報を検査する。未知の値は明示的な unknown。"""
    problems = _Problems()
    metadata = _mapping(value)
    if metadata is None:
        return [f"{where}: must be an object"]
    fields = frozenset(
        {"agent_id", "role", "provider", "effort", "session_id", "cli_version", "run_id", "request_id", "skills"}
    )
    _keys(problems, where, metadata, fields, fields)
    for key in fields - {"skills"}:
        if key in metadata:
            problems.items.extend(identity_problems(metadata[key], f"{where}.{key}"))
    if "skills" in metadata:
        skills = _array(metadata["skills"])
        if skills is None:
            problems.add(f"{where}.skills", "must be an array")
        else:
            paths: set[str] = set()
            for index, item in enumerate(skills):
                site = f"{where}.skills[{index}]"
                skill = _mapping(item)
                if skill is None:
                    problems.add(site, "must be an object")
                    continue
                keys = frozenset({"path", "sha256"})
                _keys(problems, site, skill, keys, keys)
                if "path" in skill:
                    path = skill["path"]
                    problems.items.extend(identity_problems(path, f"{site}.path"))
                    if isinstance(path, str):
                        if path in paths:
                            problems.add(f"{site}.path", "duplicate skill path")
                        paths.add(path)
                if "sha256" in skill:
                    _pattern(
                        problems,
                        f"{site}.sha256",
                        skill["sha256"],
                        re.compile(r"[0-9a-f]{64}|unknown"),
                        "SHA-256 or unknown",
                    )
    return problems.items


def _check_planner(problems: _Problems, value: object, *, v2: bool = False) -> None:
    planner = _mapping(value)
    if planner is None:
        problems.add("planner", "must be an object")
        return
    keys = _PLANNER_KEYS
    if v2:
        keys = keys | {"provenance"}
    _keys(problems, "planner", planner, frozenset(keys), frozenset(keys))
    if v2:
        problems.items.extend(provenance_problems(planner.get("provenance"), "planner.provenance"))
    model = planner.get("model")
    agent_id = planner.get("agent_id")
    if "model" in planner:
        _text(problems, "planner.model", model, free=False)
    if "runtime" in planner:
        _pattern(problems, "planner.runtime", planner["runtime"], _RUNTIME, "a lowercase runtime name")
        if planner["runtime"] == "unknown":
            problems.add("planner.runtime", "must be known for the planner")
    if "agent_id" in planner:
        _pattern(problems, "planner.agent_id", agent_id, _FILE_NAME, "a file-name-safe agent id")
    if isinstance(model, str) and model.strip() and isinstance(agent_id, str):
        try:
            expected = agent_id_for(model)
        except RecordError as error:
            problems.items.extend(error.problems)
        else:
            if agent_id != expected:
                problems.add("planner.agent_id", f"must be {expected} (agent_id_for(planner.model))")


def _check_seat(
    problems: _Problems, where: str, value: object, reviewed_head: object, *, v2: bool = False
) -> dict[str, object] | None:
    seat = _mapping(value)
    if seat is None:
        problems.add(where, "must be an object")
        return None
    extra: set[str] = {"provenance"} if v2 else set()
    _keys(problems, where, seat, frozenset(_SEAT_KEYS | extra), frozenset(_SEAT_REQUIRED | extra))
    if v2:
        problems.items.extend(provenance_problems(seat.get("provenance"), f"{where}.provenance"))
    if "review_file" in seat:
        name = seat["review_file"]
        _pattern(problems, f"{where}.review_file", name, _FILE_NAME, "a review file name")
        if isinstance(name, str) and name.endswith(".md"):
            problems.add(f"{where}.review_file", "must not end with .md")
    if "model" in seat:
        _text(problems, f"{where}.model", seat["model"], free=False)
    if "runtime" in seat:
        _pattern(problems, f"{where}.runtime", seat["runtime"], _RUNTIME, "a lowercase runtime name")
        if seat["runtime"] == "unknown" and "note" not in seat:
            problems.add(f"{where}.note", "required when runtime is unknown")
    if "note" in seat:
        _text(problems, f"{where}.note", seat["note"], free=True)
    file_head = seat.get("file_head")
    if "file_head" in seat:
        _pattern(problems, f"{where}.file_head", file_head, _FILE_HEAD, "12 lowercase hex characters or unknown")
    if "mode" in seat:
        _choice(problems, f"{where}.mode", seat["mode"], MODES)
    if "rerun" in seat:
        rerun = seat["rerun"]
        if not isinstance(rerun, bool):
            problems.add(f"{where}.rerun", "must be a boolean")
        elif (
            isinstance(file_head, str)
            and _FILE_HEAD.fullmatch(file_head) is not None
            and isinstance(reviewed_head, str)
            and _FULL_HEAD.fullmatch(reviewed_head) is not None
            and rerun != (file_head == reviewed_head[:12])
        ):
            problems.add(
                f"{where}.rerun", "must be true exactly when file_head is the first 12 characters of reviewed_head"
            )
    if "merge_recommendation" in seat:
        _choice(
            problems, f"{where}.merge_recommendation", seat["merge_recommendation"], (*MERGE_RECOMMENDATIONS, None)
        )
    return seat


def _check_seats(
    problems: _Problems, value: object, reviewed_head: object, *, stopped: bool, v2: bool = False
) -> list[dict[str, object]]:
    values = _array(value)
    if values is None:
        problems.add("seats", "must be an array")
        return []
    if not values and not stopped:
        problems.add("seats", "must not be empty unless the round stopped")
        return []
    seats: list[dict[str, object]] = []
    names: list[str] = []
    for index, item in enumerate(values):
        seat = _check_seat(problems, f"seats[{index}]", item, reviewed_head, v2=v2)
        if seat is None:
            continue
        seats.append(seat)
        name = seat.get("review_file")
        if isinstance(name, str):
            names.append(name)
    if len(set(names)) != len(names):
        problems.add("seats", "must not repeat a review_file")
    return seats


def _check_sources(problems: _Problems, where: str, value: object, id_match: re.Match[str] | None) -> list[str]:
    sources = _array(value)
    if not sources:
        problems.add(f"{where}.sources", "must be a non-empty array")
        return []
    refs: list[str] = []
    for index, ref in enumerate(sources):
        if isinstance(ref, str) and FINDING_REF.fullmatch(ref) is not None:
            refs.append(ref)
        else:
            problems.add(f"{where}.sources[{index}]", "must be a finding reference <file>#<id>@<head>")
    if len(set(refs)) != len(refs):
        problems.add(f"{where}.sources", "must not contain duplicates")
    if id_match is not None and id_match["ref"] not in refs:
        problems.add(f"{where}.sources", "must contain the reference part of the decision id")
    return refs


def _check_d1(problems: _Problems, where: str, value: object, refs: Sequence[str]) -> None:
    d1 = _mapping(value)
    if d1 is None:
        problems.add(where, "must be an object")
        return
    _keys(problems, where, d1, _D1_KEYS, _D1_REQUIRED)
    reproduced_by = d1.get("reproduced_by")
    if "reproduced_by" in d1 and reproduced_by not in _OWN_REPRODUCERS:
        if not isinstance(reproduced_by, str) or FINDING_REF.fullmatch(reproduced_by) is None:
            problems.add(f"{where}.reproduced_by", "must be a finding reference, planner, or verifier")
        elif reproduced_by not in refs:
            problems.add(f"{where}.reproduced_by", "must be one of the decision's sources, planner, or verifier")
    if "usage_path" in d1:
        _text(problems, f"{where}.usage_path", d1["usage_path"], free=True)
    if "result_kind" in d1:
        _choice(problems, f"{where}.result_kind", d1["result_kind"], RESULT_KINDS)
    if reproduced_by in _OWN_REPRODUCERS and "reproduction" not in d1:
        problems.add(f"{where}.reproduction", "required when reproduced_by is planner or verifier")
    if "reproduction" in d1:
        _check_reproduction(problems, f"{where}.reproduction", d1["reproduction"])


def _check_reproduction(problems: _Problems, where: str, value: object) -> None:
    reproduction = _mapping(value)
    if reproduction is None:
        problems.add(where, "must be an object")
        return
    _keys(problems, where, reproduction, _REPRODUCTION_KEYS, _REPRODUCTION_KEYS)
    if "commands" in reproduction:
        commands = _array(reproduction["commands"])
        if not commands:
            problems.add(f"{where}.commands", "must be a non-empty array")
        else:
            for index, command in enumerate(commands):
                _text(problems, f"{where}.commands[{index}]", command, free=True)
    for key in ("place", "conditions", "result"):
        if key in reproduction:
            _text(problems, f"{where}.{key}", reproduction[key], free=True)


def _check_carry_over(problems: _Problems, where: str, value: object) -> None:
    carry_over = _mapping(value)
    if carry_over is None:
        problems.add(where, "must be an object")
        return
    _keys(problems, where, carry_over, _CARRY_OVER_KEYS, frozenset())
    if len([key for key in ("issue", "candidate") if key in carry_over]) != 1:
        problems.add(where, "must have exactly one of issue or candidate")
    if "issue" in carry_over:
        _pattern(problems, f"{where}.issue", carry_over["issue"], _ISSUE, "#<number> or an issue URL")
    if "candidate" in carry_over:
        _text(problems, f"{where}.candidate", carry_over["candidate"], free=True)


def _check_decision(problems: _Problems, where: str, value: object) -> dict[str, object] | None:
    decision = _mapping(value)
    if decision is None:
        problems.add(where, "must be an object")
        return None
    _keys(problems, where, decision, _DECISION_KEYS, _DECISION_REQUIRED)
    decision_id = decision.get("id")
    id_match = DECISION_ID.fullmatch(decision_id) if isinstance(decision_id, str) else None
    if "id" in decision and id_match is None:
        problems.add(f"{where}.id", "must be a finding reference <file>#<id>@<head>, optionally followed by /<part>")
    refs = _check_sources(problems, where, decision["sources"], id_match) if "sources" in decision else []
    if "summary" in decision:
        _text(problems, f"{where}.summary", decision["summary"], free=False)
    if "locations" in decision:
        locations = _array(decision["locations"])
        if not locations:
            problems.add(f"{where}.locations", "must be a non-empty array")
        else:
            for index, location in enumerate(locations):
                _text(problems, f"{where}.locations[{index}]", location, free=False)
    state = decision.get("state")
    change = decision.get("change")
    if "state" in decision:
        _choice(problems, f"{where}.state", state, STATES)
    if "change" in decision:
        _choice(problems, f"{where}.change", change, CHANGES)
    previous_state = decision.get("previous_state")
    if change == "changed":
        if "previous_state" not in decision:
            problems.add(f"{where}.previous_state", "required when change is changed")
        elif previous_state not in STATES:
            _choice(problems, f"{where}.previous_state", previous_state, STATES)
        elif previous_state == state:
            problems.add(f"{where}.previous_state", "must differ from state")
    elif "previous_state" in decision:
        problems.add(f"{where}.previous_state", "allowed only when change is changed")
    reopened = change == "changed" and previous_state == "skip"
    _conditional_text(problems, where, decision, "new_evidence", when=reopened, condition="previous_state is skip")
    _conditional_text(problems, where, decision, "revisit_when", when=state == "skip", condition="state is skip")
    _conditional_text(
        problems, where, decision, "unverified_reason", when=state == "unverified", condition="state is unverified"
    )
    if "severity" in decision:
        severity = decision["severity"]
        _choice(problems, f"{where}.severity", severity, (*SEVERITIES, None))
        if severity is None and refs and not all(_is_unverified(ref) for ref in refs):
            problems.add(f"{where}.severity", "may be null only when every source is an Unverified candidate")
    if "pr_relation" in decision:
        _choice(problems, f"{where}.pr_relation", decision["pr_relation"], PR_RELATIONS)
    if "reason" in decision:
        _text(problems, f"{where}.reason", decision["reason"], free=True)
    if "d1" in decision:
        if state != "fix":
            problems.add(f"{where}.d1", "allowed only when state is fix")
        if decision.get("pr_relation") not in ("introduced", "surfaced"):
            problems.add(f"{where}.d1", "requires pr_relation introduced or surfaced")
        _check_d1(problems, f"{where}.d1", decision["d1"], refs)
    if "carry_over" in decision:
        if state != "skip":
            problems.add(f"{where}.carry_over", "allowed only when state is skip")
        _check_carry_over(problems, f"{where}.carry_over", decision["carry_over"])
    return decision


def _check_shared_sources(problems: _Problems, decisions: Sequence[Mapping[str, object]]) -> None:
    """1 つの参照を複数の判断に入れてよいのは、その参照を基にした部分どうしだけ。"""
    holders: dict[str, list[str]] = {}
    for decision in decisions:
        decision_id = decision.get("id")
        if not isinstance(decision_id, str) or DECISION_ID.fullmatch(decision_id) is None:
            continue
        for ref in _array(decision.get("sources")) or []:
            if isinstance(ref, str):
                holders.setdefault(ref, []).append(decision_id)
    for ref, ids in sorted(holders.items()):
        if len(ids) > 1 and not _parts_of(ref, ids):
            problems.add(
                "decisions", f"{ref} is in more than one decision ({', '.join(ids)}); only its parts may share it"
            )


def _check_decisions(problems: _Problems, value: object) -> list[dict[str, object]]:
    values = _array(value)
    if values is None:
        problems.add("decisions", "must be an array")
        return []
    decisions: list[dict[str, object]] = []
    ids: list[str] = []
    for index, item in enumerate(values):
        decision = _check_decision(problems, f"decisions[{index}]", item)
        if decision is None:
            continue
        decisions.append(decision)
        decision_id = decision.get("id")
        if isinstance(decision_id, str):
            ids.append(decision_id)
    duplicates = sorted({decision_id for decision_id in ids if ids.count(decision_id) > 1})
    if duplicates:
        problems.add("decisions", "duplicate decision id: " + ", ".join(duplicates))
    _check_shared_sources(problems, decisions)
    return decisions


def _check_outcome(
    problems: _Problems,
    record: Mapping[str, object],
    seats: Sequence[Mapping[str, object]],
    decisions: Sequence[Mapping[str, object]],
) -> None:
    outcome = record.get("outcome")
    if "outcome" in record:
        _choice(problems, "outcome", outcome, OUTCOMES)
    states = {state for decision in decisions if isinstance(state := decision.get("state"), str)}
    if outcome == "fixes_pushed":
        if "commit" not in record:
            problems.add("commit", "required when outcome is fixes_pushed")
        else:
            _pattern(problems, "commit", record["commit"], _FULL_HEAD, "a full lowercase commit hash")
            if record["commit"] == record.get("reviewed_head"):
                problems.add("commit", "must differ from reviewed_head")
        if "fix" not in states:
            problems.add("outcome", "fixes_pushed requires at least one fix decision")
    elif "commit" in record:
        problems.add("commit", "allowed only when outcome is fixes_pushed")
    stopped = outcome == "stopped" or isinstance(outcome, str) and outcome.startswith("stopped_")
    _conditional_text(problems, "", record, "stop_reason", when=stopped, condition="outcome is a stopped result")
    if outcome == "stopped" and isinstance(reason := record.get("stop_reason"), str):
        category, separator, detail = reason.partition(":")
        if category not in {"permission", "environment", "external"} or not separator or not detail.strip():
            problems.add(
                "stop_reason", "must be permission/environment/external followed by ': ' and a concrete reason"
            )
    if outcome == "awaiting_approval" and not states & {"fix", "hold"}:
        problems.add("outcome", "awaiting_approval requires a fix or hold decision")
    if outcome == "no_fix_needed" and states & {"fix", "hold", "unverified"}:
        problems.add("outcome", "no_fix_needed must not leave fix, hold, or unverified decisions")
    if seats and not stopped and not any(seat.get("rerun") is True for seat in seats):
        problems.add("seats", "at least one seat must have rerun true unless the round stopped")


def validate_record(value: object) -> dict[str, Any]:
    """巡の記録を単体で検査する。

    通れば記録を返す。問題があれば、すべてを RecordError で送出する。
    """
    record = _mapping(value)
    if record is None:
        raise RecordError(["record: must be a JSON object"])
    problems = _Problems()
    _keys(problems, "", record, _TOP_KEYS, _TOP_REQUIRED)
    v2 = record.get("schema") == PROVENANCE_SCHEMA_ID
    if "schema" in record and record["schema"] not in (SCHEMA_ID, PROVENANCE_SCHEMA_ID):
        problems.add("schema", f"must be {SCHEMA_ID} or {PROVENANCE_SCHEMA_ID}")
    if "pr" in record:
        _check_pr(problems, record["pr"])
    if "round" in record and not _positive_integer(record["round"]):
        problems.add("round", "must be an integer of 1 or more")
    reviewed_head = record.get("reviewed_head")
    if "reviewed_head" in record:
        _pattern(problems, "reviewed_head", reviewed_head, _FULL_HEAD, "a full lowercase commit hash")
    if "recorded_at" in record and not _valid_time(record["recorded_at"]):
        problems.add("recorded_at", "must be a UTC time YYYY-MM-DDTHH:MM:SSZ")
    if "planner" in record:
        _check_planner(problems, record["planner"], v2=v2)
    outcome = record.get("outcome")
    stopped = outcome == "stopped" or isinstance(outcome, str) and outcome.startswith("stopped_")
    seats = _check_seats(problems, record["seats"], reviewed_head, stopped=stopped, v2=v2) if "seats" in record else []
    decisions = _check_decisions(problems, record["decisions"]) if "decisions" in record else []
    _check_outcome(problems, record, seats, decisions)
    problems.raise_if_any()
    return cast(dict[str, Any], record)


_ROUND_KEYS = ("round", "reviewed_head", "recorded_at", "planner", "seats", "outcome")
_PENDING_STATES = ("fix", "hold", "unverified")


def _check_round_number(problems: _Problems, record: Mapping[str, Any], rounds: Sequence[Mapping[str, Any]]) -> None:
    """1 つの巡は 1 人の planner だけが記録し、更新できるのは最新の巡だけ。"""
    number = cast(int, record["round"])
    same = [entry for entry in rounds if entry["round"] == number]
    later = [cast(int, entry["round"]) for entry in rounds if entry["round"] > number]
    if same:
        problems.add("round", f"round {number} is already recorded by {same[0]['planner']['agent_id']}")
    elif later:
        problems.add("round", f"round {number} can no longer be updated because round {max(later)} exists")
    else:
        expected = max((cast(int, entry["round"]) for entry in rounds), default=0) + 1
        if number != expected:
            problems.add("round", f"must be {expected}")


def _check_change(
    problems: _Problems,
    where: str,
    decision: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    owners: Mapping[str, set[str]],
) -> None:
    """判断の変化（新規・引き継ぎ・変更）と出典を、前の状態と照らす。

    owners は、参照ごとの、前の状態でその参照を持つ判断 ID の集合。
    """
    decision_id = cast(str, decision["id"])
    base = _base(decision_id)
    change = decision["change"]
    if change == "new":
        if previous is not None:
            problems.add(f"{where}.change", f"{decision_id} is already recorded; use inherited or changed")
        elif base in owners:
            holders = ", ".join(sorted(owners[base]))
            problems.add(f"{where}.id", f"{base} is already recorded under {holders}; inherit or change it")
    elif previous is None:
        problems.add(f"{where}.change", f"{decision_id} is not in the earlier records; use new")
    elif change == "inherited":
        earlier_state = previous["state"]
        if decision["state"] != earlier_state:
            problems.add(f"{where}.state", f"an inherited decision must keep the earlier state {earlier_state}")
        if "d1" in previous and decision.get("d1") != previous["d1"]:
            problems.add(f"{where}.d1", "an inherited decision must keep the earlier d1")
    elif decision["previous_state"] != previous["state"]:
        problems.add(f"{where}.previous_state", f"must be the earlier state {previous['state']}")
    for ref in decision["sources"]:
        others = owners.get(ref, set()) - {decision_id}
        if others and not _parts_of(ref, others | {decision_id}):
            problems.add(f"{where}.sources", f"{ref} already belongs to {', '.join(sorted(others))}")


def check_against(record: Mapping[str, Any], prior: Mapping[str, Any]) -> None:
    """validate_record を通した巡の記録を、前の状態と照合する。

    prior は、更新するコメントを除いたほかの記録を fold した結果。
    """
    problems = _Problems()
    prior_pr = prior.get("pr")
    if prior_pr is not None and prior_pr != record["pr"]:
        problems.add("pr", "must match the earlier records of this pull request")
    _check_round_number(problems, record, cast(list[dict[str, Any]], prior.get("rounds", [])))
    earlier = {entry["id"]: entry for entry in cast(list[dict[str, Any]], prior.get("decisions", []))}
    owners: dict[str, set[str]] = {}
    for key, entry in earlier.items():
        for ref in entry["sources"]:
            owners.setdefault(ref, set()).add(key)
    states = {key: entry["state"] for key, entry in earlier.items()}
    listed: set[str] = set()
    for index, decision in enumerate(cast(list[dict[str, Any]], record["decisions"])):
        decision_id = cast(str, decision["id"])
        listed.add(decision_id)
        states[decision_id] = decision["state"]
        _check_change(problems, f"decisions[{index}]", decision, earlier.get(decision_id), owners)
    if record["outcome"] != "stopped" and not str(record["outcome"]).startswith("stopped_"):
        missing = sorted(key for key, entry in earlier.items() if entry["state"] == "fix" and key not in listed)
        if missing:
            problems.add("decisions", "must list every decision whose earlier state is fix: " + ", ".join(missing))
    if record["outcome"] == "no_fix_needed":
        pending = sorted(key for key, state in states.items() if state in _PENDING_STATES)
        if pending:
            problems.add("outcome", "no_fix_needed leaves pending decisions: " + ", ".join(pending))
    problems.raise_if_any()


def fold(
    records: Sequence[tuple[int, Mapping[str, Any]]], *, expected_pr: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """判断 ID ごとの現在の状態を作る。

    records は (コメント ID, validate_record を通した記録)。
    巡の番号・記録時刻・コメント ID の順に並べ、後の記載を優先する。
    expected_pr を渡すと、ほかの PR の記録を問題として示す。
    """
    ordered = sorted(records, key=lambda item: (item[1]["round"], item[1]["recorded_at"], item[0]))
    problems = _Problems()
    pr: object = None if expected_pr is None else dict(expected_pr)
    rounds: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}
    for comment_id, record in ordered:
        if pr is None:
            pr = record["pr"]
        elif record["pr"] != pr:
            problems.add(f"comment {comment_id}", "belongs to a different pull request")
            continue
        entry: dict[str, Any] = {key: copy.deepcopy(record[key]) for key in _ROUND_KEYS}
        for key in ("commit", "stop_reason"):
            if key in record:
                entry[key] = record[key]
        entry["comment_id"] = comment_id
        rounds.append(entry)
        for decision in record["decisions"]:
            decision_id = decision["id"]
            current = decisions.get(decision_id)
            sources: list[str] = list(current["sources"]) if current is not None else []
            for ref in decision["sources"]:
                if ref not in sources:
                    sources.append(ref)
            history: list[dict[str, Any]] = list(current["history"]) if current is not None else []
            history.append(
                {
                    "round": record["round"],
                    "reviewed_head": record["reviewed_head"],
                    "planner_model": record["planner"]["model"],
                    "state": decision["state"],
                    "change": decision["change"],
                    "outcome": record["outcome"],
                    "comment_id": comment_id,
                }
            )
            latest: dict[str, Any] = copy.deepcopy(dict(decision))
            latest["sources"] = sources
            latest["history"] = history
            decisions[decision_id] = latest
    problems.raise_if_any()
    return {
        "schema": PROVENANCE_SCHEMA_ID
        if any(record.get("schema") == PROVENANCE_SCHEMA_ID for _, record in ordered)
        else SCHEMA_ID,
        "pr": copy.deepcopy(pr),
        "rounds": rounds,
        "decisions": list(decisions.values()),
        "ignored_comments": [],
    }


def round_marker(round_number: int, reviewed_head: str) -> str:
    """状況コメントの 2 行目に置く、巡のマーカー。"""
    if not _positive_integer(round_number) or _FULL_HEAD.fullmatch(reviewed_head) is None:
        raise RecordError(["round marker: needs a round of 1 or more and a full lowercase commit hash"])
    return f"{ROUND_MARKER_PREFIX}{round_number}:{reviewed_head} -->"


def extract_record(body: str) -> dict[str, Any] | None:
    """コメント本文から記録を取り出して検査する。

    記録のブロックが無ければ None を返す。
    行は改行（LF と CRLF）だけで分ける。U+2028 などで分けない。
    """
    starts = body.count(BLOCK_START)
    ends = body.count(BLOCK_END)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise RecordError(["body: must contain exactly one review-records block"])
    start = body.index(BLOCK_START) + len(BLOCK_START)
    end = body.index(BLOCK_END)
    if end < start:
        raise RecordError(["body: the end marker comes before the start marker"])
    lines = body[start:end].replace("\r\n", "\n").strip().split("\n")
    if len(lines) < 3 or lines[0].strip() != "```json" or lines[-1].strip() != "```":
        raise RecordError(["body: the block must hold one fenced json code block"])
    try:
        value = json.loads("\n".join(lines[1:-1]))
    except json.JSONDecodeError as error:
        raise RecordError([f"body: invalid JSON at line {error.lineno}: {error.msg}"]) from error
    return validate_record(value)


def render_block(record: Mapping[str, Any]) -> str:
    """状況コメントの末尾に付ける、記録のブロック。

    描画した文字列から同じ記録を読み戻せることも確かめる。
    """
    checked = validate_record(dict(record))
    payload = json.dumps(checked, ensure_ascii=False, indent=2)
    block = "\n".join((BLOCK_SUMMARY, "", BLOCK_START, "```json", payload, "```", BLOCK_END, "", "</details>"))
    if extract_record(block) != checked:
        raise RecordError(["record: does not read back unchanged after rendering"])
    return block


def record_markers_in(text: str) -> tuple[str, ...]:
    """本文に含まれる、helper が付けるべきマーカーの種類。"""
    found: list[str] = []
    if AGENT_MARKER_PREFIX in text:
        found.append("an agent marker")
    if ROUND_MARKER_PREFIX in text:
        found.append("a round marker")
    if BLOCK_START in text or BLOCK_END in text:
        found.append("a records block marker")
    return tuple(found)


def compose_comment(record: Mapping[str, Any], body: str) -> str:
    """記録付きの状況コメントの全文を組み立てる。

    エージェントのマーカー、巡のマーカー、本文、記録のブロックの順に並べる。
    本文にマーカーがあるとき、全文が上限を超えるときは RecordError。
    """
    checked = validate_record(dict(record))
    problems = [f"body: must not contain {kind}; the helper adds it" for kind in record_markers_in(body)]
    text = body.strip()
    if not text:
        problems.append("body: must not be blank")
    if problems:
        raise RecordError(problems)
    lines = (
        f"{AGENT_MARKER_PREFIX}{checked['planner']['agent_id']} -->",
        round_marker(checked["round"], checked["reviewed_head"]),
        text,
        "",
        render_block(checked),
    )
    comment = "\n".join(lines)
    if len(comment) > COMMENT_BODY_LIMIT:
        raise RecordError([f"comment: {len(comment)} characters exceed the limit of {COMMENT_BODY_LIMIT}"])
    return comment


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def comments_from_api(payload: object) -> list[dict[str, Any]]:
    """gh api の出力から、コメントの一覧を作る。

    --slurp のページの配列と、平たい配列の両方を受け付ける。
    配列の要素のうち配列はページとして展開し、ページの間に混ざった
    gh のエラーオブジェクトはそのまま 1 件として扱う。
    各コメントは id・body・author・author_association に整える。
    body が null のコメントは、空の本文として扱う。
    """
    items = _array(payload)
    if items is None:
        error = _mapping(payload)
        if error is not None and isinstance(error.get("message"), str):
            raise RecordError([f"comments: gh api returned an error: {error['message']}"])
        raise RecordError(["comments: must be the JSON array printed by gh api"])
    flat: list[object] = []
    for item in items:
        if isinstance(item, list):
            flat.extend(cast(list[object], item))
        else:
            flat.append(item)
    problems = _Problems()
    comments: list[dict[str, Any]] = []
    for index, item in enumerate(flat):
        comment = _mapping(item)
        if comment is not None and "id" not in comment and isinstance(comment.get("message"), str):
            problems.add(f"comments[{index}]", f"gh api returned an error: {comment['message']}")
            continue
        if comment is None or not _is_integer(comment.get("id")) or "body" not in comment:
            problems.add(f"comments[{index}]", "must be an object with an integer id and a body")
            continue
        body = "" if comment["body"] is None else comment["body"]
        if not isinstance(body, str):
            problems.add(f"comments[{index}]", "must be an object with an integer id and a body")
            continue
        user = _mapping(comment.get("user"))
        login = None if user is None else user.get("login")
        association = comment.get("author_association")
        comments.append(
            {
                "id": comment["id"],
                "body": body,
                "author": login if isinstance(login, str) else "",
                "author_association": association if isinstance(association, str) else "",
            }
        )
    problems.raise_if_any()
    return comments


def _marker_problems(record: Mapping[str, Any], agent: re.Match[str], round_match: re.Match[str]) -> list[str]:
    problems: list[str] = []
    if agent["agent_id"] != record["planner"]["agent_id"]:
        problems.append("body: the agent marker does not match planner.agent_id")
    if int(round_match["round"]) != record["round"] or round_match["head"] != record["reviewed_head"]:
        problems.append("body: the round marker does not match round and reviewed_head")
    return problems


def collect_records(
    comments: Sequence[Mapping[str, Any]], *, exclude_comment_ids: Iterable[int] = ()
) -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    """記録を持つ巡の状況コメントから、(コメント ID, 記録) の一覧を作る。

    読むのは、書き込み権限のある投稿者のコメントのうち、1 行目がエージェントの
    マーカー、2 行目が巡のマーカーのものだけ。読まなかったコメントのうち、
    2 つ目の戻り値に理由とともに入れるのは、マーカーを持つ信頼しない投稿者の
    コメントと、巡の状況コメントの形でないのに記録のブロックを持つコメントだけ。
    それ以外の読まなかったコメントは黙って読み飛ばす。除外したコメント、
    マーカーの無いコメント、巡の状況コメントの形でなく記録のブロックも
    持たないコメント（PR 修正ループの進捗コメントなど）である。
    読むべきコメントの記録が壊れていれば、コメント ID を示して RecordError。
    """
    excluded = set(exclude_comment_ids)
    records: list[tuple[int, dict[str, Any]]] = []
    ignored: list[dict[str, Any]] = []
    problems: list[str] = []
    for comment in comments:
        comment_id = cast(int, comment["id"])
        body = cast(str, comment["body"])
        if comment_id in excluded or not record_markers_in(body):
            continue
        association = cast(str, comment.get("author_association", ""))
        note = {"comment_id": comment_id, "author": comment.get("author", ""), "author_association": association}
        if association not in TRUSTED_ASSOCIATIONS:
            ignored.append({**note, "reason": "the author is not trusted"})
            continue
        lines = body.replace("\r\n", "\n").split("\n")
        agent = AGENT_MARKER_PATTERN.fullmatch(lines[0])
        round_match = ROUND_MARKER_PATTERN.fullmatch(lines[1]) if len(lines) > 1 else None
        if agent is None or round_match is None:
            if BLOCK_START in body or BLOCK_END in body:
                ignored.append({**note, "reason": "has a records block but is not a round status comment"})
            continue
        try:
            record = extract_record(body)
            if record is None:
                raise RecordError(["body: a round status comment must contain a records block"])
            marker_problems = _marker_problems(record, agent, round_match)
            if marker_problems:
                raise RecordError(marker_problems)
        except RecordError as error:
            problems.extend(f"comment {comment_id}: {problem}" for problem in error.problems)
            continue
        records.append((comment_id, record))
    if problems:
        raise RecordError(problems)
    return records, ignored


def read_state(
    comments: Sequence[Mapping[str, Any]],
    *,
    expected_pr: Mapping[str, Any] | None = None,
    exclude_comment_ids: Iterable[int] = (),
) -> dict[str, Any]:
    """コメントの一覧から、判断の現在の状態と ignored_comments を作る。

    ignored_comments は collect_records の 2 つ目の戻り値で、読まなかった
    コメントの全部ではない。
    """
    records, ignored = collect_records(comments, exclude_comment_ids=exclude_comment_ids)
    state = fold(records, expected_pr=expected_pr)
    state["ignored_comments"] = ignored
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review_records.py", description="review-records v1 の記録を扱う")
    commands = parser.add_subparsers(dest="command", required=True)
    fold_command = commands.add_parser("fold", help="コメント一覧から判断の現在の状態を出力する")
    fold_command.add_argument("--repo", help="期待する PR のリポジトリ（owner/name）")
    fold_command.add_argument("--pr", type=int, help="期待する PR の番号")
    fold_command.add_argument("--exclude-comment", type=int, action="append", help="読まないコメント ID")
    validate_command = commands.add_parser("validate", help="巡の記録を検査する")
    validate_command.add_argument("--record-file", type=Path, required=True)
    validate_command.add_argument("--comments-file", type=Path, help="照合に使うコメント一覧")
    validate_command.add_argument("--exclude-comment", type=int, action="append", help="照合から除くコメント ID")
    render_command = commands.add_parser("render", help="記録のブロックを出力する")
    render_command.add_argument("--record-file", type=Path, required=True)
    return parser


def _load_json(text: str, source: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RecordError([f"{source}: invalid JSON at line {error.lineno}: {error.msg}"]) from error


def _read_json(path: Path) -> object:
    return _load_json(path.read_text(encoding="utf-8"), str(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "fold" and (args.repo is None) != (args.pr is None):
        parser.error("--repo and --pr must be given together")
    excluded: list[int] = getattr(args, "exclude_comment", None) or []
    try:
        if args.command == "fold":
            expected = None if args.repo is None else {"repository": args.repo, "number": args.pr}
            comments = comments_from_api(_load_json(sys.stdin.read(), "stdin"))
            state = read_state(comments, expected_pr=expected, exclude_comment_ids=excluded)
            sys.stdout.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        elif args.command == "validate":
            checked = validate_record(_read_json(args.record_file))
            if args.comments_file is not None:
                comments = comments_from_api(_read_json(args.comments_file))
                check_against(checked, read_state(comments, expected_pr=checked["pr"], exclude_comment_ids=excluded))
        else:
            sys.stdout.write(render_block(validate_record(_read_json(args.record_file))) + "\n")
    except RecordError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 65
    except UnicodeDecodeError as error:
        print(f"input: not valid UTF-8 ({error.reason})", file=sys.stderr)
        return 65
    except OSError as error:
        print(f"{error.filename or 'input'}: {error.strerror or type(error).__name__}", file=sys.stderr)
        return 66
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
