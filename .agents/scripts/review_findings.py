"""review-findings v1: レビューの候補一覧・確認済みの一覧・工程の記録。

Issue #547。形式と規則の定義元は ``.agents/review-findings.md`` にある。
このモジュールは、pr-review・review-verify・review-fix-planning が受け渡す
JSON の検査・Markdown からの読み取り・突き合わせ・集計を、標準ライブラリだけで実装する。
``uv run --no-project`` で動く。

- ``validate FILE``: 候補一覧・確認済みの一覧・工程の記録・実行記録を検査する
- ``from-markdown FILE``: レビューファイル（Markdown）の指摘を候補一覧の形に読み取る
- ``check-pair MARKDOWN JSON``: レビューファイルと候補一覧の指摘が一致するかを確かめる
- ``statement-candidates``: 修正の差分の語で、条件を述べている文書の候補を列挙する
- ``stats DIR...``: レビューディレクトリの ``mode: cycle`` の確認済みの一覧から、レビュアーごとの精度を集計する
  （集計しないと分かっている一覧は検査もしない。``kind``・``mode`` が無い・知らない値の一覧は検査で止める）。
  ``runs/`` の実行記録も検査し、完走率・失敗した工程・工程ごとの時間を ``runs`` に集計する
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

SCHEMA_ID = "review-findings/v1"
PROVENANCE_SCHEMA_ID = "review-findings/v2"

# standalone CLI と importlib を使う利用者の両方から同じ検査を読む。
_records_spec = importlib.util.spec_from_file_location(
    "_review_provenance_records", Path(__file__).with_name("review_records.py")
)
assert _records_spec is not None and _records_spec.loader is not None
_records = importlib.util.module_from_spec(_records_spec)
_records_spec.loader.exec_module(_records)

STAGES = ("find", "verify", "decide", "implement", "self_review", "fix_check", "pre_push_review")

KINDS = ("candidates", "verified", "planning", "statement_candidates", "review_run")
RUN_STATUSES = ("started", "success", "failed")
RUN_FAILED_AT = ("prepare", "review", "complete", "save", "cleanup")
RUN_REVIEW_KINDS = ("pr_head", "commit")
RUN_MODES = ("Initial", "Update")
RUN_SEVERITY_KEYS = ("critical", "high", "medium", "low", "info")
RUN_STAGE_SECONDS = ("prepare_seconds", "review_seconds", "save_seconds", "cleanup_seconds", "total_seconds")
RUNNER_IDENTITY_KEYS = ("model", "provider", "effort", "session_id", "cli_version")
# tools/agent_runner/models.py の State と同じ値。標準ライブラリだけで動かすために写している。State を変えたら合わせる
RUNNER_STATES = (
    "success",
    "self",
    "unsupported",
    "launch_failed",
    "awaiting_authentication",
    "awaiting_approval",
    "response_failed",
    "missing_result",
    "invalid_result",
    "timed_out",
    "interrupted",
)
SOURCES = ("reviewer", "markdown")
MODES = ("cycle", "fix_check")
SEVERITIES = ("Critical", "High", "Medium", "Low", "Info")
CONFIDENCES = ("verified", "needs_check", "needs_design_decision")
PR_RELATIONS = ("introduced", "surfaced", "preexisting", "unknown")
REPRODUCTION_STATUSES = ("executed", "not_executed", "not_applicable")
USAGE_PATHS = ("normal", "limited", "unknown", "not_applicable")
RESULT_KINDS = ("stop", "wrong_result", "safety", "performance", "maintainability", "documentation", "other")
SCENARIO_RESULT_KINDS = ("stop", "wrong_result", "safety", "performance")
PRIOR_FIX_RELATIONS = ("not_applicable", "added_mechanism", "rule_copy", "same_rule_claim", "unknown")
VARIANT_RESULTS = ("violates", "holds", "unchecked")
JUDGMENT_RESULTS = ("consistent", "inconsistent", "unrelated", "unchecked")
STATEMENT_RESULTS = ("consistent", "inconsistent", "unchecked")
VERDICTS = ("confirmed", "not_reproduced", "unverified")
INPUT_KINDS = ("json", "markdown")
SAFETY_ITEMS = (1, 2, 3)
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
CONFIDENCE_LABELS = {"検証済み": "verified", "確認推奨": "needs_check", "要設計判断": "needs_design_decision"}

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_ID = re.compile(_NAME)
_VERIFIED_ID = re.compile(r"V-[1-9][0-9]*")
_SHORT_HEAD = re.compile(r"[0-9a-f]{12}|unknown")
_FULL_HEAD = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FINDING_REF = re.compile(rf"{_NAME}#{_NAME}@(?:[0-9a-f]{{12}}|unknown)")
_REVIEW_FILE = re.compile(rf"{_NAME}\.md")
_REPOSITORY = re.compile(rf"{_NAME}/{_NAME}")
_CANDIDATES_FILE = re.compile(r"[0-9a-f]{12}\.statement-candidates\.json")
_VERIFIED_FILE = re.compile(r"verified/[0-9a-f]{12}\.json")
_RUN_FINDINGS_FILE = re.compile(rf"findings/{_NAME}\.json")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class FindingsError(ValueError):
    """文書が形式を満たさない。``problems`` に、場所付きの問題をすべて持つ。"""

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
            raise FindingsError(self.items)


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _array(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return cast(list[object], value)


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 32 or 127 <= code < 160


def _object(problems: _Problems, where: str, value: object) -> dict[str, object] | None:
    mapping = _mapping(value)
    if mapping is None:
        problems.add(where, "must be an object")
    return mapping


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


def _text(problems: _Problems, where: str, value: object) -> None:
    """自由記述の欄。空でない文字列で、タブと改行以外の制御文字を含まない。"""
    if not isinstance(value, str):
        problems.add(where, "must be a string")
    elif not value.strip():
        problems.add(where, "must not be blank")
    elif any(_is_control(char) and char not in "\t\n\r" for char in value):
        problems.add(where, "must not contain control characters other than tabs and line breaks")


def _pattern(problems: _Problems, where: str, value: object, pattern: re.Pattern[str], description: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        problems.add(where, f"must be {description}")


def _choice(problems: _Problems, where: str, value: object, choices: Sequence[object]) -> None:
    if isinstance(value, bool) or value not in choices:
        problems.add(where, "must be one of " + ", ".join(json.dumps(choice) for choice in choices))


def _count(problems: _Problems, where: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        problems.add(where, "must be a non-negative integer")


def _boolean(problems: _Problems, where: str, value: object) -> None:
    if not isinstance(value, bool):
        problems.add(where, "must be true or false")


def _items(problems: _Problems, where: str, value: object, *, non_empty: bool = False) -> list[object]:
    items = _array(value)
    if items is None or (non_empty and not items):
        problems.add(where, "must be a non-empty array" if non_empty else "must be an array")
        return []
    return items


def _texts(problems: _Problems, where: str, value: object, *, non_empty: bool = False) -> None:
    for index, item in enumerate(_items(problems, where, value, non_empty=non_empty)):
        _text(problems, f"{where}[{index}]", item)


def _record(
    problems: _Problems,
    where: str,
    value: object,
    *,
    texts: Sequence[str] = (),
    optional_texts: Sequence[str] = (),
    choices: Mapping[str, Sequence[object]] | None = None,
) -> dict[str, object] | None:
    """文字列の欄と選択肢の欄だけを持つ、小さなオブジェクトを検査する。"""
    mapping = _object(problems, where, value)
    if mapping is None:
        return None
    choices = choices or {}
    required = frozenset(texts) | frozenset(choices)
    _keys(problems, where, mapping, required | frozenset(optional_texts), required)
    for key in (*texts, *optional_texts):
        if key in mapping:
            _text(problems, _join(where, key), mapping[key])
    for key, options in choices.items():
        if key in mapping:
            _choice(problems, _join(where, key), mapping[key], options)
    return mapping


def _valid_time(value: object) -> bool:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _check_pr(problems: _Problems, value: object, *, where: str = "pr") -> None:
    pr = _object(problems, where, value)
    if pr is None:
        return
    _keys(problems, where, pr, frozenset({"repository", "number"}), frozenset({"repository", "number"}))
    if "repository" in pr:
        _pattern(problems, f"{where}.repository", pr["repository"], _REPOSITORY, "owner/name")
    if "number" in pr:
        number = pr["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            problems.add(f"{where}.number", "must be a positive integer")


def _check_skill(problems: _Problems, value: object, *, v2: bool = False) -> None:
    skill = _object(problems, "skill", value)
    if skill is None:
        return
    _keys(problems, "skill", skill, frozenset({"path", "sha256"}), frozenset({"path", "sha256"}))
    if "path" in skill:
        _text(problems, "skill.path", skill["path"])
    if "sha256" in skill:
        pattern = re.compile(r"[0-9a-f]{64}|unknown") if v2 else _SHA256
        _pattern(
            problems,
            "skill.sha256",
            skill["sha256"],
            pattern,
            "a SHA-256 or unknown" if v2 else "a 64-character lowercase hex SHA-256",
        )


def _check_seconds(problems: _Problems, where: str, value: object) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
    ):
        problems.add(where, "must be finite non-negative seconds or null")


def _check_duration(problems: _Problems, where: str, value: Mapping[str, object]) -> None:
    for key in ("seconds", "waiting_seconds"):
        if key in value:
            _check_seconds(problems, f"{where}.{key}", value[key])
    elapsed, waiting = value.get("seconds"), value.get("waiting_seconds")
    if isinstance(elapsed, (int, float)) and isinstance(waiting, (int, float)) and waiting > elapsed:
        problems.add(f"{where}.waiting_seconds", "must not exceed seconds")


def _check_timing(problems: _Problems, value: object, *, v2: bool = False) -> None:
    timing = _object(problems, "timing", value)
    if timing is None:
        return
    keys = frozenset({"started_at", "finished_at", "seconds"} | ({"waiting_seconds"} if v2 else set()))
    _keys(problems, "timing", timing, keys, keys)
    for key in ("started_at", "finished_at"):
        if key in timing and not _valid_time(timing[key]):
            problems.add(f"timing.{key}", "must be a UTC time like 2026-10-05T04:48:47Z")
    if v2:
        _check_duration(problems, "timing", timing)
        if (
            _valid_time(timing.get("started_at"))
            and _valid_time(timing.get("finished_at"))
            and cast(str, timing["finished_at"]) < cast(str, timing["started_at"])
        ):
            problems.add("timing.finished_at", "must not precede started_at")
    elif "seconds" in timing:
        _count(problems, "timing.seconds", timing["seconds"])


def _check_identity(problems: _Problems, where: str, value: object, *, v2: bool) -> None:
    if v2:
        problems.items.extend(_records.identity_problems(value, where))
    else:
        _text(problems, where, value)


def _check_agent(
    problems: _Problems, where: str, value: object, *, v2: bool = False, texts: Sequence[str] = ("model", "runtime")
) -> dict[str, object] | None:
    agent = _object(problems, where, value)
    if agent is None:
        return None
    keys = frozenset(texts)
    if v2:
        keys = keys | {"provenance"}
    _keys(problems, where, agent, frozenset(keys), frozenset(keys))
    for key in texts:
        if key in agent:
            _check_identity(problems, f"{where}.{key}", agent[key], v2=v2)
    if v2:
        problems.items.extend(_records.provenance_problems(agent.get("provenance"), f"{where}.provenance"))
    return agent


def _check_reproduction_attempt(problems: _Problems, where: str, value: object) -> None:
    """候補一覧の再現（4.2 節の `再現`）。"""
    keys = frozenset({"status", "commands", "place", "conditions", "result", "reason"})
    reproduction = _object(problems, where, value)
    if reproduction is None:
        return
    _keys(problems, where, reproduction, keys, frozenset({"status"}))
    status = reproduction.get("status")
    if "status" in reproduction:
        _choice(problems, f"{where}.status", status, REPRODUCTION_STATUSES)
    executed = ("commands", "place", "conditions", "result")
    for key in executed:
        if key in reproduction:
            if key == "commands":
                _texts(problems, f"{where}.commands", reproduction[key], non_empty=True)
            else:
                _text(problems, f"{where}.{key}", reproduction[key])
        elif status == "executed":
            problems.add(f"{where}.{key}", "required when status is executed")
    if "reason" in reproduction:
        _text(problems, f"{where}.reason", reproduction["reason"])
    elif status == "not_executed":
        problems.add(f"{where}.reason", "required when status is not_executed")


def _check_reproduction_record(problems: _Problems, where: str, value: object) -> None:
    """確認担当の再現（コマンド・場所・条件・結果）。"""
    keys = frozenset({"commands", "place", "conditions", "result"})
    reproduction = _object(problems, where, value)
    if reproduction is None:
        return
    _keys(problems, where, reproduction, keys, keys)
    if "commands" in reproduction:
        _texts(problems, f"{where}.commands", reproduction["commands"], non_empty=True)
    for key in ("place", "conditions", "result"):
        if key in reproduction:
            _text(problems, f"{where}.{key}", reproduction[key])


def _check_same_kind(problems: _Problems, where: str, value: object) -> None:
    same_kind = _object(problems, where, value)
    if same_kind is None:
        return
    if "not_applicable" in same_kind:
        _keys(problems, where, same_kind, frozenset({"not_applicable"}), frozenset({"not_applicable"}))
        _text(problems, f"{where}.not_applicable", same_kind["not_applicable"])
        return
    _record(problems, where, same_kind, texts=("property", "range", "search", "result"))


def _check_failure_scenario(problems: _Problems, where: str, value: object) -> None:
    scenario = _object(problems, where, value)
    if scenario is None:
        return
    texts = ("entry", "input", "environment", "expected", "actual")
    _keys(problems, where, scenario, frozenset({*texts, "steps"}), frozenset({*texts, "steps"}))
    for key in texts:
        if key in scenario:
            _text(problems, f"{where}.{key}", scenario[key])
    if "steps" in scenario:
        _texts(problems, f"{where}.steps", scenario["steps"], non_empty=True)


def _check_variants(problems: _Problems, where: str, value: object) -> None:
    for index, item in enumerate(_items(problems, where, value)):
        _record(problems, f"{where}[{index}]", item, texts=("input", "how"), choices={"result": VARIANT_RESULTS})


def _check_statement_sites(problems: _Problems, where: str, value: object) -> None:
    for index, item in enumerate(_items(problems, where, value)):
        _record(problems, f"{where}[{index}]", item, texts=("path", "claim"), choices={"result": STATEMENT_RESULTS})


def _check_pr_relation(problems: _Problems, where: str, value: object) -> None:
    _record(problems, where, value, texts=("basis",), choices={"value": PR_RELATIONS})


def _check_unique_ids(problems: _Problems, findings: Sequence[object], where: str) -> list[str]:
    seen: dict[str, int] = {}
    ids: list[str] = []
    for index, item in enumerate(findings):
        mapping = _mapping(item)
        if mapping is None or not isinstance(mapping.get("id"), str):
            continue
        finding_id = cast(str, mapping["id"])
        if finding_id in seen:
            problems.add(f"{where}[{index}].id", f"duplicates {where}[{seen[finding_id]}].id")
        else:
            seen[finding_id] = index
            ids.append(finding_id)
    return ids


_CANDIDATE_KEYS = frozenset(
    {
        "id",
        "severity",
        "confidence",
        "title",
        "locations",
        "detected_head",
        "reproduction",
        "usage_path",
        "result_kind",
        "pr_relation",
        "same_kind",
        "prior_fix_relation",
        "review_basis",
        "failure_scenario",
        "property",
        "variants",
        "statement_sites",
        "suggested_fix",
        "raw",
    }
)
_CANDIDATE_REQUIRED = frozenset({"id", "severity", "title", "locations", "detected_head"})
_REVIEWER_REQUIRED = frozenset({"confidence", "pr_relation", "property", "variants", "statement_sites"})


def _check_candidate(problems: _Problems, where: str, value: object, *, by_reviewer: bool) -> None:
    finding = _object(problems, where, value)
    if finding is None:
        return
    required = _CANDIDATE_REQUIRED | (_REVIEWER_REQUIRED if by_reviewer else frozenset[str]())
    _keys(problems, where, finding, _CANDIDATE_KEYS, required)
    if "id" in finding:
        _pattern(problems, f"{where}.id", finding["id"], _ID, "an ID such as H-1")
    if "severity" in finding:
        _choice(problems, f"{where}.severity", finding["severity"], SEVERITIES)
    if "confidence" in finding:
        _choice(problems, f"{where}.confidence", finding["confidence"], CONFIDENCES)
    for key in ("title", "property", "review_basis", "suggested_fix", "raw"):
        if key in finding:
            _text(problems, f"{where}.{key}", finding[key])
    if "locations" in finding:
        _texts(problems, f"{where}.locations", finding["locations"], non_empty=True)
    if "detected_head" in finding:
        _pattern(problems, f"{where}.detected_head", finding["detected_head"], _SHORT_HEAD, "12 hex or unknown")
    if "reproduction" in finding:
        _check_reproduction_attempt(problems, f"{where}.reproduction", finding["reproduction"])
    if "usage_path" in finding:
        _record(
            problems,
            f"{where}.usage_path",
            finding["usage_path"],
            optional_texts=("detail",),
            choices={"value": USAGE_PATHS},
        )
    result_kind: object = None
    if "result_kind" in finding:
        mapping = _record(
            problems,
            f"{where}.result_kind",
            finding["result_kind"],
            optional_texts=("detail",),
            choices={"value": RESULT_KINDS},
        )
        result_kind = mapping.get("value") if mapping is not None else None
    if "pr_relation" in finding:
        _check_pr_relation(problems, f"{where}.pr_relation", finding["pr_relation"])
    if "same_kind" in finding:
        _check_same_kind(problems, f"{where}.same_kind", finding["same_kind"])
    if "prior_fix_relation" in finding:
        _record(
            problems,
            f"{where}.prior_fix_relation",
            finding["prior_fix_relation"],
            optional_texts=("detail",),
            choices={"value": PRIOR_FIX_RELATIONS},
        )
    if "failure_scenario" in finding:
        _check_failure_scenario(problems, f"{where}.failure_scenario", finding["failure_scenario"])
    elif by_reviewer and result_kind in SCENARIO_RESULT_KINDS:
        problems.add(f"{where}.failure_scenario", f"required when result_kind is {result_kind}")
    if "variants" in finding:
        _check_variants(problems, f"{where}.variants", finding["variants"])
    if "statement_sites" in finding:
        _check_statement_sites(problems, f"{where}.statement_sites", finding["statement_sites"])


def _check_coverage_candidates(problems: _Problems, value: object) -> None:
    coverage = _object(problems, "coverage", value)
    if coverage is None:
        return
    keys = frozenset({"read", "checks", "declined"})
    _keys(problems, "coverage", coverage, keys, keys)
    if "read" in coverage:
        _texts(problems, "coverage.read", coverage["read"])
    if "checks" in coverage:
        for index, item in enumerate(_items(problems, "coverage.checks", coverage["checks"])):
            _record(problems, f"coverage.checks[{index}]", item, texts=("command", "result"))
    if "declined" in coverage:
        for index, item in enumerate(_items(problems, "coverage.declined", coverage["declined"])):
            _record(problems, f"coverage.declined[{index}]", item, texts=("target", "reason"))


def _check_candidates(problems: _Problems, document: Mapping[str, object]) -> None:
    base_keys = {"schema", "kind", "source", "pr", "reviewed_head", "reviewer", "findings", "unverified_candidates"}
    reviewer_keys = {"base", "skill", "timing", "coverage"}
    source = document.get("source")
    _choice(problems, "source", source, SOURCES)
    by_reviewer = source != "markdown"
    required = frozenset(base_keys | (reviewer_keys if by_reviewer else set()))
    _keys(problems, "", document, frozenset(base_keys | reviewer_keys), required)
    if "pr" in document:
        _check_pr(problems, document["pr"])
    if "reviewed_head" in document:
        head = document["reviewed_head"]
        if by_reviewer:
            _pattern(problems, "reviewed_head", head, _FULL_HEAD, "a full commit hash")
        elif not isinstance(head, str) or (_FULL_HEAD.fullmatch(head) is None and _SHORT_HEAD.fullmatch(head) is None):
            problems.add("reviewed_head", "must be a commit hash or unknown")
    if "base" in document:
        _pattern(problems, "base", document["base"], _FULL_HEAD, "a full commit hash")
    if "reviewer" in document:
        reviewer = _check_agent(
            problems,
            "reviewer",
            document["reviewer"],
            v2=document.get("schema") == PROVENANCE_SCHEMA_ID,
            texts=("model", "runtime", "review_file"),
        )
        if reviewer is not None and "review_file" in reviewer:
            _pattern(problems, "reviewer.review_file", reviewer["review_file"], _REVIEW_FILE, "<model>.md")
    if "skill" in document:
        _check_skill(problems, document["skill"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "timing" in document:
        _check_timing(problems, document["timing"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "coverage" in document:
        _check_coverage_candidates(problems, document["coverage"])
    if "findings" in document:
        findings = _items(problems, "findings", document["findings"])
        for index, item in enumerate(findings):
            _check_candidate(problems, f"findings[{index}]", item, by_reviewer=by_reviewer)
        _check_unique_ids(problems, findings, "findings")
    if "unverified_candidates" in document:
        unverified = _items(problems, "unverified_candidates", document["unverified_candidates"])
        for index, item in enumerate(unverified):
            mapping = _record(problems, f"unverified_candidates[{index}]", item, texts=("id", "summary"))
            if mapping is not None and "id" in mapping:
                _pattern(problems, f"unverified_candidates[{index}].id", mapping["id"], _ID, "an ID such as U-1")
        _check_unique_ids(problems, unverified, "unverified_candidates")


_VERIFIED_FINDING_KEYS = frozenset(
    {
        "id",
        "sources",
        "property",
        "locations",
        "verdict",
        "basis",
        "variants",
        "statement_sites",
        "pr_relation",
        "reported_severities",
        "reproduction",
        "missing_checks",
        "safety_path",
        "cause_commit",
        "first_present_head",
        "late_detection",
    }
)
_VERIFIED_FINDING_REQUIRED = frozenset(
    {
        "id",
        "sources",
        "property",
        "locations",
        "verdict",
        "basis",
        "variants",
        "statement_sites",
        "pr_relation",
        "reported_severities",
    }
)


def _check_safety_path(problems: _Problems, where: str, value: object) -> None:
    safety = _object(problems, where, value)
    if safety is None:
        return
    texts = ("entry", "input", "environment", "destination")
    keys = frozenset({*texts, "item", "confirmed"})
    _keys(problems, where, safety, keys, keys)
    if "item" in safety:
        _choice(problems, f"{where}.item", safety["item"], SAFETY_ITEMS)
    for key in texts:
        if key in safety:
            _text(problems, f"{where}.{key}", safety[key])
    if "confirmed" in safety:
        _boolean(problems, f"{where}.confirmed", safety["confirmed"])


def _check_verified_finding(problems: _Problems, where: str, value: object, *, mode: object) -> None:
    finding = _object(problems, where, value)
    if finding is None:
        return
    _keys(problems, where, finding, _VERIFIED_FINDING_KEYS, _VERIFIED_FINDING_REQUIRED)
    if "id" in finding:
        _pattern(problems, f"{where}.id", finding["id"], _VERIFIED_ID, "V-<n>")
    if "sources" in finding:
        sources = _items(problems, f"{where}.sources", finding["sources"], non_empty=mode != "fix_check")
        for index, source in enumerate(sources):
            _pattern(problems, f"{where}.sources[{index}]", source, _FINDING_REF, "<file>#<ID>@<12 hex or unknown>")
    for key in ("property", "basis"):
        if key in finding:
            _text(problems, f"{where}.{key}", finding[key])
    if "locations" in finding:
        _texts(problems, f"{where}.locations", finding["locations"], non_empty=True)
    verdict = finding.get("verdict")
    if "verdict" in finding:
        _choice(problems, f"{where}.verdict", verdict, VERDICTS)
    if "variants" in finding:
        _check_variants(problems, f"{where}.variants", finding["variants"])
    if "statement_sites" in finding:
        _check_statement_sites(problems, f"{where}.statement_sites", finding["statement_sites"])
    if "pr_relation" in finding:
        _check_pr_relation(problems, f"{where}.pr_relation", finding["pr_relation"])
    if "reported_severities" in finding:
        for index, item in enumerate(_items(problems, f"{where}.reported_severities", finding["reported_severities"])):
            mapping = _record(
                problems,
                f"{where}.reported_severities[{index}]",
                item,
                texts=("source",),
                choices={"severity": SEVERITIES},
            )
            if mapping is not None and "source" in mapping:
                _pattern(
                    problems,
                    f"{where}.reported_severities[{index}].source",
                    mapping["source"],
                    _FINDING_REF,
                    "<file>#<ID>@<12 hex or unknown>",
                )
    if "reproduction" in finding:
        if verdict == "confirmed":
            _check_reproduction_record(problems, f"{where}.reproduction", finding["reproduction"])
        else:
            problems.add(f"{where}.reproduction", "allowed only when verdict is confirmed")
    elif verdict == "confirmed":
        problems.add(f"{where}.reproduction", "required when verdict is confirmed")
    if "missing_checks" in finding:
        if verdict == "unverified":
            _text(problems, f"{where}.missing_checks", finding["missing_checks"])
        else:
            problems.add(f"{where}.missing_checks", "allowed only when verdict is unverified")
    elif verdict == "unverified":
        problems.add(f"{where}.missing_checks", "required when verdict is unverified")
    if "safety_path" in finding:
        _check_safety_path(problems, f"{where}.safety_path", finding["safety_path"])
    if "cause_commit" in finding:
        _pattern(problems, f"{where}.cause_commit", finding["cause_commit"], _COMMIT, "a commit hash")
    if "first_present_head" in finding:
        _pattern(
            problems, f"{where}.first_present_head", finding["first_present_head"], _SHORT_HEAD, "12 hex or unknown"
        )
    if "late_detection" in finding:
        _boolean(problems, f"{where}.late_detection", finding["late_detection"])


def _check_inputs(problems: _Problems, value: object, *, v2: bool = False) -> None:
    keys = frozenset(
        {"review_file", "model", "runtime", "input_kind", "findings_file", "reviewed_head", "stale"}
        | ({"provenance", "waiting_seconds"} if v2 else set())
    )
    for index, item in enumerate(_items(problems, "inputs", value)):
        where = f"inputs[{index}]"
        entry = _object(problems, where, item)
        if entry is None:
            continue
        _keys(problems, where, entry, keys, keys)
        if "review_file" in entry:
            _pattern(problems, f"{where}.review_file", entry["review_file"], _REVIEW_FILE, "<model>.md")
        for key in ("model", "runtime"):
            if key in entry:
                _check_identity(problems, f"{where}.{key}", entry[key], v2=v2)
        if "input_kind" in entry:
            _choice(problems, f"{where}.input_kind", entry["input_kind"], INPUT_KINDS)
        if "findings_file" in entry:
            findings_file = entry["findings_file"]
            if findings_file is None:
                if entry.get("input_kind") == "json":
                    problems.add(f"{where}.findings_file", "required when input_kind is json")
            else:
                _text(problems, f"{where}.findings_file", findings_file)
        if "reviewed_head" in entry:
            _pattern(problems, f"{where}.reviewed_head", entry["reviewed_head"], _SHORT_HEAD, "12 hex or unknown")
        if "stale" in entry:
            _boolean(problems, f"{where}.stale", entry["stale"])
        if v2:
            problems.items.extend(_records.provenance_problems(entry.get("provenance"), f"{where}.provenance"))
            if "waiting_seconds" in entry:
                _check_seconds(problems, f"{where}.waiting_seconds", entry["waiting_seconds"])


def _check_coverage_verified(problems: _Problems, value: object) -> None:
    coverage = _object(problems, "coverage", value)
    if coverage is None:
        return
    keys = frozenset({"closed", "remaining"})
    _keys(problems, "coverage", coverage, keys, keys)
    if "closed" in coverage:
        for index, item in enumerate(_items(problems, "coverage.closed", coverage["closed"])):
            _record(problems, f"coverage.closed[{index}]", item, texts=("target", "how"))
    if "remaining" in coverage:
        for index, item in enumerate(_items(problems, "coverage.remaining", coverage["remaining"])):
            _record(problems, f"coverage.remaining[{index}]", item, texts=("target", "reason"))


def _check_accuracy(problems: _Problems, value: object, finding_ids: Sequence[str], *, v2: bool = False) -> None:
    accuracy = _object(problems, "accuracy", value)
    if accuracy is None:
        return
    keys = frozenset({"reviewers", "find_seconds", "verify_seconds"})
    _keys(problems, "accuracy", accuracy, keys, keys)
    counts = ("candidates", "confirmed", "not_reproduced", "unverified", "missed")
    reviewer_keys = frozenset(
        {"review_file", "model", "runtime", "skill_sha256", "unique", *counts} | ({"provenance"} if v2 else set())
    )
    if "reviewers" in accuracy:
        for index, item in enumerate(_items(problems, "accuracy.reviewers", accuracy["reviewers"])):
            where = f"accuracy.reviewers[{index}]"
            reviewer = _object(problems, where, item)
            if reviewer is None:
                continue
            _keys(problems, where, reviewer, reviewer_keys, reviewer_keys)
            if v2:
                problems.items.extend(_records.provenance_problems(reviewer.get("provenance"), f"{where}.provenance"))
            if "review_file" in reviewer:
                _pattern(problems, f"{where}.review_file", reviewer["review_file"], _REVIEW_FILE, "<model>.md")
            for key in ("model", "runtime"):
                if key in reviewer:
                    _check_identity(problems, f"{where}.{key}", reviewer[key], v2=v2)
            if "skill_sha256" in reviewer and reviewer["skill_sha256"] is not None:
                _pattern(problems, f"{where}.skill_sha256", reviewer["skill_sha256"], _SHA256, "a SHA-256 or null")
            for key in counts:
                if key in reviewer:
                    _count(problems, f"{where}.{key}", reviewer[key])
            if "unique" in reviewer:
                for position, unique in enumerate(_items(problems, f"{where}.unique", reviewer["unique"])):
                    if unique not in finding_ids:
                        problems.add(f"{where}.unique[{position}]", "must be the ID of a finding in this document")
    if "find_seconds" in accuracy:
        seconds = _object(problems, "accuracy.find_seconds", accuracy["find_seconds"])
        for name, amount in (seconds or {}).items():
            if _REVIEW_FILE.fullmatch(name) is None:
                problems.add(f"accuracy.find_seconds.{name}", "key must be <model>.md")
            if v2:
                _check_seconds(problems, f"accuracy.find_seconds.{name}", amount)
            elif amount is not None:
                _count(problems, f"accuracy.find_seconds.{name}", amount)
    if "verify_seconds" in accuracy:
        if v2:
            _check_seconds(problems, "accuracy.verify_seconds", accuracy["verify_seconds"])
        else:
            _count(problems, "accuracy.verify_seconds", accuracy["verify_seconds"])


def _check_statement_section(problems: _Problems, value: object, reviewed_head: object) -> None:
    section = _object(problems, "statement_candidates", value)
    if section is None:
        return
    keys = frozenset({"file", "judgments"})
    _keys(problems, "statement_candidates", section, keys, keys)
    if "file" in section:
        _pattern(
            problems,
            "statement_candidates.file",
            section["file"],
            _CANDIDATES_FILE,
            "<12 hex>.statement-candidates.json",
        )
        if isinstance(reviewed_head, str) and section["file"] != f"{reviewed_head[:12]}.statement-candidates.json":
            problems.add("statement_candidates.file", "must be named after reviewed_head")
    if "judgments" in section:
        for index, item in enumerate(_items(problems, "statement_candidates.judgments", section["judgments"])):
            where = f"statement_candidates.judgments[{index}]"
            entry = _object(problems, where, item)
            if entry is None:
                continue
            judgment_keys = frozenset({"ids", "result", "basis"})
            _keys(problems, where, entry, judgment_keys, judgment_keys)
            if "ids" in entry:
                for position, candidate_id in enumerate(
                    _items(problems, f"{where}.ids", entry["ids"], non_empty=True)
                ):
                    _pattern(problems, f"{where}.ids[{position}]", candidate_id, _CANDIDATE_ID, "S-<n>")
            if "result" in entry:
                _choice(problems, f"{where}.result", entry["result"], JUDGMENT_RESULTS)
            if "basis" in entry:
                _text(problems, f"{where}.basis", entry["basis"])


def _check_fix_evidence(problems: _Problems, document: Mapping[str, object]) -> None:
    section = _object(problems, "fix_evidence", document["fix_evidence"])
    if section is None:
        return
    keys = frozenset({"claims", "files"})
    _keys(problems, "fix_evidence", section, keys, keys)
    for group, identifier, results in (
        ("claims", "id", ("holds", "violates", "unchecked")),
        ("files", "path", ("passed", "failed", "not_applicable", "unchecked")),
    ):
        for index, value in enumerate(_items(problems, f"fix_evidence.{group}", section.get(group))):
            where = f"fix_evidence.{group}[{index}]"
            entry = _object(problems, where, value)
            if entry is None:
                continue
            required = frozenset({identifier, "result", "basis", "checks", "findings"})
            if group == "files":
                required |= {"rule"}
            _keys(problems, where, entry, required, required)
            for key in (identifier, "basis", *(("rule",) if group == "files" else ())):
                if key in entry:
                    _text(problems, f"{where}.{key}", entry[key])
            if group == "claims" and "id" in entry:
                _pattern(problems, f"{where}.id", entry["id"], re.compile(r"C-[1-9][0-9]*"), "C-<n>")
            _choice(problems, f"{where}.result", entry.get("result"), results)
            _texts(problems, f"{where}.findings", entry.get("findings"))
            checks = _items(problems, f"{where}.checks", entry.get("checks"))
            for position, check in enumerate(checks):
                place = f"{where}.checks[{position}]"
                record = _object(problems, place, check)
                if record is None:
                    continue
                fields = frozenset({"command", "head", "mode", "exit_code", "result", "artifact"})
                _keys(problems, place, record, fields, fields)
                for key in ("command", "result"):
                    if key in record:
                        _text(problems, f"{place}.{key}", record[key])
                if record.get("head") != document.get("reviewed_head"):
                    problems.add(f"{place}.head", "must equal reviewed_head")
                _choice(problems, f"{place}.mode", record.get("mode"), ("executed", "static"))
                code = record.get("exit_code")
                if code is not None and type(code) is not int:
                    problems.add(f"{place}.exit_code", "must be an integer or null")
                if record.get("artifact") is not None:
                    _text(problems, f"{place}.artifact", record["artifact"])
            check_maps = [_mapping(check) for check in checks]
            if entry.get("result") == "passed" and not any(
                check is not None
                and check.get("mode") == "executed"
                and type(check.get("exit_code")) is int
                and check["exit_code"] == 0
                for check in check_maps
            ):
                problems.add(where, "passed requires executed evidence at reviewed_head")


def _check_verified(problems: _Problems, document: Mapping[str, object]) -> None:
    keys = {
        "schema",
        "kind",
        "mode",
        "pr",
        "reviewed_head",
        "base",
        "verifier",
        "skill",
        "timing",
        "inputs",
        "coverage",
        "findings",
        "accuracy",
    }
    mode = document.get("mode")
    # fix_check の有無は mode に応じて下で確かめる
    _keys(
        problems,
        "",
        document,
        frozenset(keys | {"fix_check", "statement_candidates", "fix_evidence"}),
        frozenset(keys),
    )
    if "fix_evidence" in document:
        if mode != "fix_check":
            problems.add("fix_evidence", "allowed only when mode is fix_check")
        else:
            _check_fix_evidence(problems, document)
    elif mode == "fix_check" and document.get("schema") == PROVENANCE_SCHEMA_ID:
        problems.add("fix_evidence", "required for v2 fix_check")
    _choice(problems, "mode", mode, MODES)
    if "pr" in document:
        _check_pr(problems, document["pr"])
    for key in ("reviewed_head", "base"):
        if key in document:
            _pattern(problems, key, document[key], _FULL_HEAD, "a full commit hash")
    if "verifier" in document:
        _check_agent(problems, "verifier", document["verifier"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "skill" in document:
        _check_skill(problems, document["skill"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "timing" in document:
        _check_timing(problems, document["timing"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "fix_check" in document:
        if mode != "fix_check":
            problems.add("fix_check", "allowed only when mode is fix_check")
        else:
            fix = _object(problems, "fix_check", document["fix_check"])
            if fix is not None:
                fix_keys = frozenset({"from", "to", "fix_ids"})
                _keys(problems, "fix_check", fix, fix_keys, fix_keys)
                for key in ("from", "to"):
                    if key in fix:
                        _pattern(problems, f"fix_check.{key}", fix[key], _FULL_HEAD, "a full commit hash")
                if "to" in fix and fix["to"] != document.get("reviewed_head"):
                    problems.add("fix_check.to", "must equal reviewed_head")
                if "fix_ids" in fix:
                    _texts(problems, "fix_check.fix_ids", fix["fix_ids"], non_empty=True)
    elif mode == "fix_check":
        problems.add("fix_check", "required when mode is fix_check")
    if "statement_candidates" in document:
        if mode != "fix_check":
            problems.add("statement_candidates", "allowed only when mode is fix_check")
        else:
            _check_statement_section(problems, document["statement_candidates"], document.get("reviewed_head"))
    elif mode == "fix_check":
        problems.add("statement_candidates", "required when mode is fix_check")
    if "inputs" in document:
        _check_inputs(problems, document["inputs"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "coverage" in document:
        _check_coverage_verified(problems, document["coverage"])
    finding_ids: list[str] = []
    if "findings" in document:
        findings = _items(problems, "findings", document["findings"])
        for index, item in enumerate(findings):
            _check_verified_finding(problems, f"findings[{index}]", item, mode=mode)
        finding_ids = _check_unique_ids(problems, findings, "findings")
    if "accuracy" in document:
        _check_accuracy(problems, document["accuracy"], finding_ids, v2=document.get("schema") == PROVENANCE_SCHEMA_ID)


def _check_planning(problems: _Problems, document: Mapping[str, object]) -> None:
    keys = frozenset(
        {
            "schema",
            "kind",
            "pr",
            "reviewed_head",
            "planner",
            "verified_file",
            "stages",
            "fix_checks",
            "outcome",
            "comment_id",
        }
    )
    if document.get("schema") == PROVENANCE_SCHEMA_ID:
        keys = keys | {"activities", "rework"}
        _check_activities(problems, document)
    _keys(problems, "", document, keys, keys)
    if "pr" in document:
        _check_pr(problems, document["pr"])
    if "reviewed_head" in document:
        _pattern(problems, "reviewed_head", document["reviewed_head"], _FULL_HEAD, "a full commit hash")
    if "planner" in document:
        _check_agent(problems, "planner", document["planner"], v2=document.get("schema") == PROVENANCE_SCHEMA_ID)
    if "verified_file" in document:
        _pattern(problems, "verified_file", document["verified_file"], _VERIFIED_FILE, "verified/<12 hex>.json")
    if "stages" in document:
        stages = _object(problems, "stages", document["stages"])
        if stages is not None:
            stage_keys = frozenset({"decide_seconds", "fix_seconds", "fix_check_seconds"})
            _keys(problems, "stages", stages, stage_keys, stage_keys)
            for key in ("decide_seconds", "fix_seconds"):
                if key in stages and stages[key] is not None:
                    _count(problems, f"stages.{key}", stages[key])
            if "fix_check_seconds" in stages:
                for index, amount in enumerate(
                    _items(problems, "stages.fix_check_seconds", stages["fix_check_seconds"])
                ):
                    _count(problems, f"stages.fix_check_seconds[{index}]", amount)
    if "fix_checks" in document:
        for index, item in enumerate(_items(problems, "fix_checks", document["fix_checks"])):
            where = f"fix_checks[{index}]"
            mapping = _object(problems, where, item)
            if mapping is None:
                continue
            check_keys = frozenset({"file", "blocking_fixes"})
            _keys(problems, where, mapping, check_keys, check_keys)
            if "file" in mapping:
                _text(problems, f"{where}.file", mapping["file"])
            if "blocking_fixes" in mapping:
                _count(problems, f"{where}.blocking_fixes", mapping["blocking_fixes"])
    if "outcome" in document:
        _choice(problems, "outcome", document["outcome"], OUTCOMES)
    if "comment_id" in document:
        comment_id = document["comment_id"]
        if comment_id is not None and (
            not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1
        ):
            problems.add("comment_id", "must be a positive integer or null")


def _check_activities(problems: _Problems, document: Mapping[str, object]) -> None:
    for index, item in enumerate(_items(problems, "activities", document.get("activities"))):
        where = f"activities[{index}]"
        activity = _object(problems, where, item)
        if activity is None:
            continue
        keys = frozenset({"stage", "actor", "seconds", "waiting_seconds"})
        _keys(problems, where, activity, keys, keys)
        _choice(problems, f"{where}.stage", activity.get("stage"), STAGES)
        _check_agent(problems, f"{where}.actor", activity.get("actor"), v2=True)
        _check_duration(problems, where, activity)
    for index, item in enumerate(_items(problems, "rework", document.get("rework"))):
        where = f"rework[{index}]"
        link = _object(problems, where, item)
        if link is None:
            continue
        keys = frozenset({"source", "implementation", "detected_stage"})
        _keys(problems, where, link, keys, keys)
        _pattern(problems, f"{where}.source", link.get("source"), _FINDING_REF, "a finding reference")
        _check_agent(problems, f"{where}.implementation", link.get("implementation"), v2=True)
        _choice(
            problems,
            f"{where}.detected_stage",
            link.get("detected_stage"),
            ("self_review", "fix_check", "pre_push_review", "next_cycle"),
        )


_RUN_KEYS = frozenset(
    {
        "schema",
        "kind",
        "pr",
        "review_kind",
        "mode",
        "pr_head",
        "reviewed_head",
        "base",
        "reviewer",
        "inputs",
        "status",
        "failed_at",
        "reason",
        "outputs",
        "timing",
        "runner",
    }
)


def _check_review_run(problems: _Problems, document: Mapping[str, object]) -> None:
    """実行記録（4.1 節）。キーは常にすべて持ち、まだ分からない値は null。"""
    _keys(problems, "", document, _RUN_KEYS, _RUN_KEYS)
    if document.get("schema") != PROVENANCE_SCHEMA_ID:
        problems.add("schema", "review_run requires review-findings/v2")
    if "pr" in document:
        _check_pr(problems, document["pr"])
    _choice(problems, "review_kind", document.get("review_kind"), RUN_REVIEW_KINDS)
    if document.get("mode") is not None:
        _choice(problems, "mode", document["mode"], RUN_MODES)
    for key in ("pr_head", "reviewed_head"):
        _pattern(problems, key, document.get(key), _FULL_HEAD, "a full commit hash")
    if document.get("base") is not None:
        _pattern(problems, "base", document["base"], _FULL_HEAD, "a full commit hash or null")
    if "reviewer" in document:
        _check_agent(problems, "reviewer", document["reviewer"], v2=True)
    if document.get("inputs") is not None:
        _check_run_inputs(problems, document["inputs"])
    _choice(problems, "status", document.get("status"), RUN_STATUSES)
    if document.get("failed_at") is not None:
        _choice(problems, "failed_at", document["failed_at"], RUN_FAILED_AT)
    if document.get("reason") is not None:
        _text(problems, "reason", document["reason"])
    if document.get("outputs") is not None:
        _check_run_outputs(problems, document["outputs"])
    timing = _check_run_timing(problems, document.get("timing"))
    if document.get("runner") is not None:
        _check_runner(problems, document["runner"])
    _check_run_status(problems, document, timing or {})


def _check_run_status(problems: _Problems, document: Mapping[str, object], timing: Mapping[str, object]) -> None:
    """状態ごとに、値を持つべき欄と null であるべき欄（4.1 節の表）。"""
    status = document.get("status")
    failed_at = document.get("failed_at")
    present: list[str] = []
    absent: list[str] = []
    if status == "started":
        absent = ["failed_at", "reason", "outputs", "timing.finished_at", "timing.total_seconds"]
    elif status == "success":
        absent = ["failed_at", "reason"]
        present = ["mode", "base", "inputs", "outputs", "timing.finished_at"]
        present += [f"timing.{key}" for key in RUN_STAGE_SECONDS]
    elif status == "failed":
        present = ["failed_at", "reason", "timing.finished_at", "timing.total_seconds"]
        if failed_at not in (None, "prepare"):
            present += ["mode", "base", "inputs"]
        (present if failed_at == "cleanup" else absent).append("outputs")
    for where in present + absent:
        section, _, key = where.rpartition(".")
        value = (timing if section else document).get(key)
        if where in present and value is None:
            problems.add(where, f"required when status is {status}")
        elif where in absent and value is not None:
            problems.add(where, f"must be null when status is {status}")


def _check_run_inputs(problems: _Problems, value: object) -> None:
    inputs = _object(problems, "inputs", value)
    if inputs is None:
        return
    keys = frozenset({"request", "previous_review", "records", "diff"})
    _keys(problems, "inputs", inputs, keys, keys)
    request = inputs.get("request")
    if request is not None and (mapping := _object(problems, "inputs.request", request)) is not None:
        request_keys = frozenset({"sha256", "deadline_seconds"})
        _keys(problems, "inputs.request", mapping, request_keys, request_keys)
        _pattern(
            problems, "inputs.request.sha256", mapping.get("sha256"), _SHA256, "a 64-character lowercase hex SHA-256"
        )
        deadline = mapping.get("deadline_seconds")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline <= 0
        ):
            problems.add("inputs.request.deadline_seconds", "must be finite positive seconds")
    previous = inputs.get("previous_review")
    if previous is not None and (mapping := _object(problems, "inputs.previous_review", previous)) is not None:
        _keys(problems, "inputs.previous_review", mapping, frozenset({"sha256"}), frozenset({"sha256"}))
        _pattern(
            problems,
            "inputs.previous_review.sha256",
            mapping.get("sha256"),
            _SHA256,
            "a 64-character lowercase hex SHA-256",
        )
    if "records" in inputs and (records := _object(problems, "inputs.records", inputs["records"])) is not None:
        record_keys = frozenset({"sha256", "decisions"})
        _keys(problems, "inputs.records", records, record_keys, record_keys)
        _pattern(
            problems, "inputs.records.sha256", records.get("sha256"), _SHA256, "a 64-character lowercase hex SHA-256"
        )
        decisions = (
            _object(problems, "inputs.records.decisions", records["decisions"]) if "decisions" in records else None
        )
        if decisions is not None:
            for state, count in decisions.items():
                where = f"inputs.records.decisions.{state}"
                _choice(problems, where, state, _records.STATES)
                _count(problems, where, count)
    if "diff" in inputs and (diff := _object(problems, "inputs.diff", inputs["diff"])) is not None:
        diff_keys = frozenset({"files", "added", "deleted", "binary"})
        _keys(problems, "inputs.diff", diff, diff_keys, diff_keys)
        for key in sorted(diff_keys & set(diff)):
            _count(problems, f"inputs.diff.{key}", diff[key])
        files, binary = diff.get("files"), diff.get("binary")
        if isinstance(files, int) and isinstance(binary, int) and binary > files:
            problems.add("inputs.diff.binary", "must not exceed files")


def _check_run_outputs(problems: _Problems, value: object) -> None:
    outputs = _object(problems, "outputs", value)
    if outputs is None:
        return
    keys = frozenset(
        {
            "review_file",
            "review_sha256",
            "findings_file",
            "findings_sha256",
            "active_findings",
            "by_severity",
            "unverified",
        }
    )
    _keys(problems, "outputs", outputs, keys, keys)
    _pattern(problems, "outputs.review_file", outputs.get("review_file"), _REVIEW_FILE, "<model>.md")
    _pattern(
        problems, "outputs.findings_file", outputs.get("findings_file"), _RUN_FINDINGS_FILE, "findings/<name>.json"
    )
    for key in ("review_sha256", "findings_sha256"):
        _pattern(problems, f"outputs.{key}", outputs.get(key), _SHA256, "a 64-character lowercase hex SHA-256")
    for key in ("active_findings", "unverified"):
        _count(problems, f"outputs.{key}", outputs.get(key))
    severity = _object(problems, "outputs.by_severity", outputs.get("by_severity"))
    if severity is None:
        return
    severity_keys = frozenset(RUN_SEVERITY_KEYS)
    _keys(problems, "outputs.by_severity", severity, severity_keys, severity_keys)
    counts = [severity.get(key) for key in RUN_SEVERITY_KEYS]
    for key, count in zip(RUN_SEVERITY_KEYS, counts, strict=True):
        _count(problems, f"outputs.by_severity.{key}", count)
    active = outputs.get("active_findings")
    if all(type(count) is int for count in counts) and type(active) is int and sum(cast(list[int], counts)) != active:
        problems.add("outputs.by_severity", "must add up to active_findings")


def _check_run_timing(problems: _Problems, value: object) -> dict[str, object] | None:
    timing = _object(problems, "timing", value)
    if timing is None:
        return None
    keys = frozenset({"started_at", "finished_at", *RUN_STAGE_SECONDS})
    _keys(problems, "timing", timing, keys, keys)
    if not _valid_time(timing.get("started_at")):
        problems.add("timing.started_at", "must be a UTC time like 2026-10-05T04:48:47Z")
    finished = timing.get("finished_at")
    if finished is not None and not _valid_time(finished):
        problems.add("timing.finished_at", "must be a UTC time like 2026-10-05T04:48:47Z or null")
    elif (
        _valid_time(finished)
        and _valid_time(timing.get("started_at"))
        and cast(str, finished) < cast(str, timing["started_at"])
    ):
        problems.add("timing.finished_at", "must not precede started_at")
    for key in RUN_STAGE_SECONDS:
        if key in timing:
            _check_seconds(problems, f"timing.{key}", timing[key])
    return timing


def _check_runner(problems: _Problems, value: object) -> None:
    """ランナーが観測した値（attach-runner が書く）。"""
    runner = _object(problems, "runner", value)
    if runner is None:
        return
    _keys(problems, "runner", runner, frozenset({"identity", "attempt"}), frozenset({"identity", "attempt"}))
    if "identity" in runner and (identity := _object(problems, "runner.identity", runner["identity"])) is not None:
        identity_keys = frozenset(RUNNER_IDENTITY_KEYS)
        _keys(problems, "runner.identity", identity, identity_keys, identity_keys)
        for key in RUNNER_IDENTITY_KEYS:
            if key in identity:
                problems.items.extend(_records.identity_problems(identity[key], f"runner.identity.{key}"))
    if "attempt" in runner and (attempt := _object(problems, "runner.attempt", runner["attempt"])) is not None:
        attempt_keys = frozenset({"state", "exit_code", "elapsed_seconds", "stop_confirmed"})
        _keys(problems, "runner.attempt", attempt, attempt_keys, attempt_keys)
        _choice(problems, "runner.attempt.state", attempt.get("state"), RUNNER_STATES)
        exit_code = attempt.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            problems.add("runner.attempt.exit_code", "must be an integer or null")
        elapsed = attempt.get("elapsed_seconds")
        if elapsed is None:
            problems.add("runner.attempt.elapsed_seconds", "must be finite non-negative seconds")
        else:
            _check_seconds(problems, "runner.attempt.elapsed_seconds", elapsed)
        _boolean(problems, "runner.attempt.stop_confirmed", attempt.get("stop_confirmed"))


def _check_provenance_links(problems: _Problems, document: Mapping[str, object]) -> None:
    """同じ記録の中で識別情報の写しと出典を照合する。v1は変更しない。"""
    owner = _mapping(document.get("reviewer") if document.get("kind") == "candidates" else document.get("verifier"))
    skill = _mapping(document.get("skill"))
    metadata = _mapping((owner or {}).get("provenance"))
    read_skills = _array((metadata or {}).get("skills"))
    if skill is not None and read_skills is not None and skill not in read_skills:
        problems.add("skill", "must occur in owner provenance.skills")
    if document.get("kind") != "verified":
        return
    inputs: dict[str, dict[str, object]] = {}
    for index, item in enumerate(_array(document.get("inputs")) or []):
        entry = _mapping(item)
        name = (entry or {}).get("review_file")
        if entry is None or not isinstance(name, str):
            continue
        if name in inputs:
            problems.add(f"inputs[{index}].review_file", "duplicate input file")
        inputs[name] = entry
    accuracy = _mapping(document.get("accuracy")) or {}
    seen: set[str] = set()
    find_seconds = _mapping(accuracy.get("find_seconds")) or {}
    for index, item in enumerate(_array(accuracy.get("reviewers")) or []):
        reviewer = _mapping(item)
        name = (reviewer or {}).get("review_file")
        if reviewer is None or not isinstance(name, str):
            continue
        where = f"accuracy.reviewers[{index}]"
        if name in seen:
            problems.add(f"{where}.review_file", "duplicate reviewer file")
        seen.add(name)
        original = inputs.get(name)
        if original is None:
            problems.add(f"{where}.review_file", "must occur in inputs")
            continue
        for key in ("model", "runtime", "provenance"):
            if reviewer.get(key) != original.get(key):
                problems.add(f"{where}.{key}", "must equal the input identity")
        _check_duration(
            problems,
            f"inputs[{name}]",
            {"seconds": find_seconds.get(name), "waiting_seconds": original.get("waiting_seconds")},
        )
    if seen != set(inputs):
        problems.add("accuracy.reviewers", "must cover every input")
    if set(find_seconds) != set(inputs):
        problems.add("accuracy.find_seconds", "must cover exactly the input files")
    if document.get("mode") == "cycle":
        for index, item in enumerate(_array(document.get("findings")) or []):
            finding = _mapping(item) or {}
            for source in _array(finding.get("sources")) or []:
                if not isinstance(source, str) or _FINDING_REF.fullmatch(source) is None:
                    continue
                file = source.split("#", 1)[0]
                if file.removesuffix(".md") + ".md" not in inputs:
                    problems.add(f"findings[{index}].sources", "must resolve to an input file")


_CANDIDATE_ID = re.compile(r"S-[1-9][0-9]*")
_CANDIDATE_PATH = re.compile(r".+:[1-9][0-9]*")


def _check_claim_inventory(problems: _Problems, document: Mapping[str, object]) -> None:
    fix_ids = _items(problems, "fix_ids", document.get("fix_ids"), non_empty=True)
    _texts(problems, "fix_ids", fix_ids, non_empty=True)
    claims = _items(problems, "claims", document.get("claims"), non_empty=True)
    seen: set[str] = set()
    covered: set[str] = set()
    for index, value in enumerate(claims):
        where = f"claims[{index}]"
        entry = _record(
            problems,
            where,
            value,
            texts=("id", "fix_id", "claim", "source"),
            choices={"kind": ("artifact", "behavior", "static")},
        )
        if entry is None:
            continue
        claim_id = entry.get("id")
        _pattern(problems, f"{where}.id", claim_id, re.compile(r"C-[1-9][0-9]*"), "C-<n>")
        if isinstance(claim_id, str):
            if claim_id in seen:
                problems.add(where, f"duplicate claim {claim_id}")
            seen.add(claim_id)
        fix_id = entry.get("fix_id")
        if fix_id not in fix_ids:
            problems.add(where, "claim fix_id is not in fix_ids")
        if isinstance(fix_id, str):
            covered.add(fix_id)
    for fix_id in fix_ids:
        if isinstance(fix_id, str) and fix_id not in covered:
            problems.add("claims", f"{fix_id} has no claim")
    files = _items(problems, "changed_files", document.get("changed_files"))
    _texts(problems, "changed_files", files)
    for key, values in (("fix_ids", fix_ids), ("changed_files", files)):
        strings = [value for value in values if isinstance(value, str)]
        if len(strings) != len(set(strings)):
            problems.add(key, "duplicate entries")


def _check_statement_candidates(problems: _Problems, document: Mapping[str, object]) -> None:
    keys = frozenset({"schema", "kind", "from", "to", "generated_at", "terms", "common_terms", "candidates"})
    _keys(problems, "", document, keys | {"fix_ids", "claims", "changed_files"}, keys)
    if any(key in document for key in ("fix_ids", "claims", "changed_files")):
        _check_claim_inventory(problems, document)
    for key in ("from", "to"):
        if key in document:
            _pattern(problems, key, document[key], _FULL_HEAD, "a full commit hash")
    if "generated_at" in document and not _valid_time(document["generated_at"]):
        problems.add("generated_at", "must be UTC YYYY-MM-DDTHH:MM:SSZ")
    if "terms" in document:
        for index, item in enumerate(_items(problems, "terms", document["terms"])):
            where = f"terms[{index}]"
            entry = _object(problems, where, item)
            if entry is None:
                continue
            _keys(problems, where, entry, frozenset({"term", "origins"}), frozenset({"term", "origins"}))
            if "term" in entry:
                _text(problems, f"{where}.term", entry["term"])
            if "origins" in entry:
                _texts(problems, f"{where}.origins", entry["origins"], non_empty=True)
    if "common_terms" in document:
        for index, item in enumerate(_items(problems, "common_terms", document["common_terms"])):
            where = f"common_terms[{index}]"
            entry = _object(problems, where, item)
            if entry is None:
                continue
            _keys(problems, where, entry, frozenset({"term", "count"}), frozenset({"term", "count"}))
            if "term" in entry:
                _text(problems, f"{where}.term", entry["term"])
            if "count" in entry:
                _count(problems, f"{where}.count", entry["count"])
    if "candidates" in document:
        candidates = _items(problems, "candidates", document["candidates"])
        seen: set[str] = set()
        for index, item in enumerate(candidates):
            where = f"candidates[{index}]"
            entry = _object(problems, where, item)
            if entry is None:
                continue
            candidate_keys = frozenset({"id", "path", "terms", "text"})
            _keys(problems, where, entry, candidate_keys, candidate_keys)
            if "id" in entry:
                _pattern(problems, f"{where}.id", entry["id"], _CANDIDATE_ID, "S-<n>")
                if isinstance(entry["id"], str):
                    if entry["id"] in seen:
                        problems.add(f"{where}.id", f"duplicate ID {entry['id']}")
                    seen.add(entry["id"])
            if "path" in entry:
                _pattern(problems, f"{where}.path", entry["path"], _CANDIDATE_PATH, "<path>:<line>")
            if "terms" in entry:
                _texts(problems, f"{where}.terms", entry["terms"], non_empty=True)
            if "text" in entry:
                _text(problems, f"{where}.text", entry["text"])


def validate_document(value: object) -> dict[str, Any]:
    """候補一覧・確認済みの一覧・工程の記録・実行記録のどれかを検査し、そのまま返す。"""
    problems = _Problems()
    document = _mapping(value)
    if document is None:
        problems.add("document", "must be an object")
        problems.raise_if_any()
        raise AssertionError("unreachable")
    if document.get("schema") not in (SCHEMA_ID, PROVENANCE_SCHEMA_ID):
        problems.add("schema", f"must be {SCHEMA_ID} or {PROVENANCE_SCHEMA_ID}")
    kind = document.get("kind")
    _choice(problems, "kind", kind, KINDS)
    if kind == "candidates":
        _check_candidates(problems, document)
    elif kind == "verified":
        _check_verified(problems, document)
    elif kind == "planning":
        _check_planning(problems, document)
    elif kind == "review_run":
        _check_review_run(problems, document)
    elif kind == "statement_candidates":
        _check_statement_candidates(problems, document)
        if document.get("schema") != SCHEMA_ID:
            problems.add("schema", "statement_candidates remains v1")
    if document.get("schema") == PROVENANCE_SCHEMA_ID:
        _check_provenance_links(problems, document)
    problems.raise_if_any()
    return cast(dict[str, Any], document)


# --- Markdown ---------------------------------------------------------------------------------------------------

_SEVERITY_HEADING = re.compile(r"## (Critical|High|Medium|Low|Info)\s*")
_UNVERIFIED_HEADING = re.compile(r"## Unverified candidates\s*")
_FINDING_HEADING = re.compile(
    r"### (?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\s*[:：]\s*(?P<title>.*?)\s*(?:【(?P<label>[^】]+)】)?\s*"
)
_FIELD = re.compile(r"- (?P<key>[^:：]+?)\s*[:：]\s*(?P<value>.*)")
_TARGET_HEAD = re.compile(r"- 対象\s*[:：].*?head\s+`?(?P<head>[0-9a-f]{12})`?")
_HEX12 = re.compile(r"[0-9a-f]{12}")
_ID_HEADING = re.compile(r"### [A-Z]+-[0-9]+\b")
_SUMMARY_ROW = re.compile(r"\|\s*(Critical|High|Medium|Low|Info)\s*\|\s*([0-9]+)\s*\|")
# 指摘を意図して数えない節（判断済み・解消済み・前回の未再検証）
_EXCLUDED_SECTIONS = ("## Decided findings", "## Resolved findings", "## Pending previous findings")


class ParsedFinding:
    """レビューファイルの active な指摘 1 件。"""

    def __init__(self, *, id: str, severity: str, title: str, confidence: str | None) -> None:
        self.id = id
        self.severity = severity
        self.title = title
        self.confidence = confidence
        self.locations_text: str | None = None
        self.detected_head = "unknown"
        self.body = ""


class ParsedReview:
    """レビューファイルから読み取った、active な指摘と未確認の候補。

    ``importlib`` で ``sys.modules`` に登録せずに読み込まれても動くよう、dataclass を使わない。
    """

    def __init__(self) -> None:
        self.reviewed_head = "unknown"
        self.findings: list[ParsedFinding] = []
        self.unverified: list[str] = []
        self.unverified_summaries: dict[str, str] = {}
        # 指摘らしいのに読めなかった見出しと、Summary の件数表（重大度 → 件数）
        self.unparsed: list[str] = []
        self.summary_counts: dict[str, int] = {}

    def problems(self) -> list[str]:
        """読めなかった指摘と、Summary の件数との食い違い。"""
        found = [f"unparsed finding heading: {heading}" for heading in self.unparsed]
        for severity, expected in self.summary_counts.items():
            actual = sum(1 for finding in self.findings if finding.severity == severity)
            if expected != actual:
                found.append(f"Summary counts {expected} {severity} finding(s) but {actual} were read")
        return found


def parse_review_markdown(text: str) -> ParsedReview:
    """pr-review のレビューファイルを読む。コードブロックの中の見出しは読まない。"""
    review = ParsedReview()
    section: str | None = None
    heading_line = ""
    current: ParsedFinding | None = None
    body: list[str] = []
    in_fence = False

    def finish() -> None:
        nonlocal current, body
        if current is not None:
            current.body = "\n".join(body).strip()
            review.findings.append(current)
        current = None
        body = []

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if current is not None:
                body.append(line)
            continue
        if in_fence:
            if current is not None:
                body.append(line)
            continue
        target = _TARGET_HEAD.match(line)
        if target is not None and review.reviewed_head == "unknown":
            review.reviewed_head = target.group("head")
        if line.startswith("## "):
            finish()
            heading_line = line.strip()
            severity = _SEVERITY_HEADING.fullmatch(line)
            if severity is not None:
                section = severity.group(1)
            elif _UNVERIFIED_HEADING.fullmatch(line):
                section = "unverified"
            else:
                section = None
            continue
        if heading_line == "## Summary":
            row = _SUMMARY_ROW.match(line.strip())
            if row is not None:
                review.summary_counts[row.group(1)] = int(row.group(2))
        if line.startswith("### "):
            finish()
            heading = _FINDING_HEADING.fullmatch(line)
            if section is None:
                if _ID_HEADING.match(line) is not None and not heading_line.startswith(_EXCLUDED_SECTIONS):
                    review.unparsed.append(line.strip())
                continue
            if heading is None:
                review.unparsed.append(line.strip())
                continue
            if section == "unverified":
                review.unverified.append(heading.group("id"))
                review.unverified_summaries[heading.group("id")] = heading.group("title").strip()
                continue
            label = heading.group("label")
            current = ParsedFinding(
                id=heading.group("id"),
                severity=section,
                title=heading.group("title").strip(),
                confidence=CONFIDENCE_LABELS.get(label) if label else None,
            )
            continue
        if current is None:
            continue
        body.append(line)
        field_match = _FIELD.fullmatch(line)
        if field_match is None:
            continue
        key, value = field_match.group("key").strip(), field_match.group("value").strip()
        if key == "位置" and current.locations_text is None:
            current.locations_text = value
        elif key == "検出時 HEAD":
            head = _HEX12.search(value)
            current.detected_head = head.group(0) if head is not None else "unknown"
    finish()
    return review


def candidates_from_markdown(
    text: str, *, review_file: str, repository: str, number: int, model: str, runtime: str
) -> dict[str, Any]:
    """レビューファイルの指摘を、候補一覧（``source: markdown``）の形にする。

    読めなかった指摘や Summary の件数との食い違いがあれば、黙って落とさずに ``FindingsError`` を送出する。
    呼び出し側（review-verify）は、そのレビューファイルを直接読む。
    """
    parsed = parse_review_markdown(text)
    if parsed.problems():
        raise FindingsError(parsed.problems())
    findings: list[dict[str, Any]] = []
    for item in parsed.findings:
        finding: dict[str, Any] = {
            "id": item.id,
            "severity": item.severity,
            "title": item.title or item.id,
            "locations": [item.locations_text or "不明"],
            "detected_head": item.detected_head,
        }
        if item.confidence is not None:
            finding["confidence"] = item.confidence
        if item.body:
            finding["raw"] = item.body
        findings.append(finding)
    return {
        "schema": SCHEMA_ID,
        "kind": "candidates",
        "source": "markdown",
        "pr": {"repository": repository, "number": number},
        "reviewed_head": parsed.reviewed_head,
        "reviewer": {"model": model, "runtime": runtime, "review_file": review_file},
        "findings": findings,
        "unverified_candidates": [
            {"id": finding_id, "summary": parsed.unverified_summaries.get(finding_id) or finding_id}
            for finding_id in parsed.unverified
        ],
    }


def check_pair(markdown: str, candidates: Mapping[str, Any]) -> None:
    """レビューファイルと候補一覧の、指摘 ID・重大度・位置・未確認の候補が一致するかを確かめる。"""
    problems = _Problems()
    parsed = parse_review_markdown(markdown)
    for problem in parsed.problems():
        problems.add("markdown", problem)
    in_markdown = {item.id: item for item in parsed.findings}
    findings = cast(list[dict[str, Any]], candidates.get("findings") or [])
    in_json = {cast(str, item.get("id")): index for index, item in enumerate(findings)}
    for finding_id in in_markdown:
        if finding_id not in in_json:
            problems.add("findings", f"{finding_id} is in the Markdown but not in the JSON")
    for finding_id in in_json:
        if finding_id not in in_markdown:
            problems.add("findings", f"{finding_id} is in the JSON but not in the Markdown")
    for finding_id, index in in_json.items():
        parsed_finding = in_markdown.get(finding_id)
        if parsed_finding is None:
            continue
        finding = findings[index]
        if finding.get("severity") != parsed_finding.severity:
            problems.add(f"findings[{index}].severity", f"the Markdown says {parsed_finding.severity}")
        locations_text = parsed_finding.locations_text or ""
        for position, location in enumerate(cast(list[str], finding.get("locations") or [])):
            if location not in locations_text:
                problems.add(f"findings[{index}].locations[{position}]", "not in the Markdown 位置 line")
    unverified = [
        cast(str, item.get("id")) for item in cast(list[dict[str, Any]], candidates.get("unverified_candidates") or [])
    ]
    for finding_id in parsed.unverified:
        if finding_id not in unverified:
            problems.add("unverified_candidates", f"{finding_id} is in the Markdown but not in the JSON")
    for finding_id in unverified:
        if finding_id not in parsed.unverified:
            problems.add("unverified_candidates", f"{finding_id} is in the JSON but not in the Markdown")
    problems.raise_if_any()


# --- 条件を述べる文書の候補 ---------------------------------------------------------------------------------------

COMMON_TERM_LIMIT = 30
"""ヒットがこの行数を超える語は一般的すぎるので、候補に展開せず ``common_terms`` に件数だけを書く。"""

_EXCLUDED_DIRECTORIES = frozenset({"tests", "evals", "fixtures"})
_DOCUMENT_SUFFIXES = (".md", ".rst")
_DOCUMENT_PATHSPECS = (
    "*.md",
    "*.rst",
    *(f":(exclude,glob)**/{name}/**" for name in sorted(_EXCLUDED_DIRECTORIES)),
)
_STATE_NAME = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_LITERAL = re.compile(r"[\"']([A-Za-z_][A-Za-z0-9_.-]{2,})[\"']")
_OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]+")
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_CODE_SPAN = re.compile(r"`([^`\s]{3,})`")
_KEBAB_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
_RST_DIRECTIVE = re.compile(r"^\s*\.\.\s+([A-Za-z][A-Za-z0-9_:+-]*)::")
_RST_ROLE = re.compile(r"(?<![A-Za-z0-9_`]):([A-Za-z][A-Za-z0-9_+-]*(?::[A-Za-z][A-Za-z0-9_+-]*)*):(?=`)")
# 文書の行の中の語（コードスパンの中を含む）。前後の境界は ASCII で判定する（日本語に直接続く「MERGED後も」も取る）。
# `.` を含む名前は取らない（コードスパン全体としてだけ取る）。
_DOCUMENT_WORD = re.compile(
    r"(?<![A-Za-z0-9_./:-])[A-Za-z_][A-Za-z0-9_]*(?:[-:][A-Za-z0-9_]+)*(?![A-Za-z0-9_/]|[.:-][A-Za-z0-9_])"
)
_ERE_SPECIAL = re.compile(r"([.\[\\()*+?{|^$])")
_GREP_LINE = re.compile(r"(?P<path>.+?):(?P<line>[1-9][0-9]*):(?P<text>.*)")
_HUNK_HEADER = re.compile(r"@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_CANDIDATE_TEXT_LIMIT = 200
_CANDIDATE_TEXT_CONTEXT = 80


def _identifier_like(term: str) -> bool:
    """小文字だけの一般的な単語ではなく、状態名・識別子・オプション・ファイル名・kebab-case の名前らしい語か。"""
    return len(term) >= 3 and (
        term.startswith("--")
        or re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", term) is not None
        or "_" in term
        or "." in term
        or re.search(r"[a-z][A-Z]", term) is not None
        or _KEBAB_NAME.fullmatch(term) is not None
    )


def _excluded(path: str) -> bool:
    return any(part in _EXCLUDED_DIRECTORIES for part in Path(path).parts[:-1])


def _document_line_terms(body: str) -> set[str]:
    """文書の変更行の語。識別子らしいコードスパン、RST の directive 名と role 名、CLI オプション、
    行の中の識別子らしい語と ``:`` でつないだ名前（``rst:directive``）。小文字だけの語は directive・role 名だけ取る。
    """
    found = {term for term in _CODE_SPAN.findall(body) if _identifier_like(term)}
    found |= set(_RST_DIRECTIVE.findall(body)) | set(_RST_ROLE.findall(body)) | set(_OPTION.findall(body))
    found |= {term for term in _DOCUMENT_WORD.findall(body) if _identifier_like(term) or ":" in term}
    return {term for term in found if len(term) >= 3}


def _line_terms(body: str, *, document: bool, changed: bool) -> set[str]:
    if document:
        return _document_line_terms(body) if changed else set()
    found = set(_STATE_NAME.findall(body)) | set(_LITERAL.findall(body)) | set(_OPTION.findall(body))
    if changed:
        found |= set(_IDENTIFIER.findall(body))
    return {term for term in found if _identifier_like(term)}


def extract_terms(diff_text: str) -> dict[str, list[str]]:
    """``git diff -U3`` の出力から、条件を述べる文書を探すための語を取り出す。

    コードのファイルでは、変更行と前後の文脈行から状態名・識別子の形の文字列リテラル・CLI オプションを、
    変更行だけから識別子らしい語を取り出す。kebab-case の名前も識別子らしい語に含める。文書のファイル
    （``.md``・``.rst``）では、変更行（追加行と削除行）だけから、識別子らしいコードスパン・RST の directive 名と
    role 名・CLI オプション・行の中の識別子らしい語を取り出す。小文字だけの語と、コードスパンでない ``.`` を含む名前は
    取らない。
    ``tests/``・``evals/``・``fixtures/`` の下は除く。値は語の出どころの一覧。追加行・文脈行は差分の新しい側の
    ``<path>:<line>``、削除行は古い側の ``<path>:<line>``（削除される前にあった位置）で書く。
    """
    terms: dict[str, list[str]] = {}
    path: str | None = None
    old_path: str | None = None
    in_header = False
    line = old_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            in_header, path, old_path = True, None, None
            continue
        header = _HUNK_HEADER.match(raw)
        if header is not None:
            in_header = False
            line = int(header.group("start"))
            old_line = int(header.group("old_start"))
            continue
        if in_header:
            # ファイルの見出しは diff --git と最初の @@ の間だけで読む。
            # hunk の中の「-- 」で始まる行の削除は「--- 」になるので、見出しと区別する。
            if raw.startswith("--- "):
                if not raw.startswith(("--- a/", "--- /dev/null")):
                    raise FindingsError([f"diff header not understood: {raw}"])
                old_path = raw[6:] if raw.startswith("--- a/") else None
            elif raw.startswith("+++ "):
                if not raw.startswith(("+++ b/", "+++ /dev/null")):
                    raise FindingsError([f"diff header not understood: {raw}"])
                path = raw[6:] if raw.startswith("+++ b/") else old_path
            continue
        if path is None or not raw or raw[0] not in " +-" or _excluded(path):
            continue
        marker, body = raw[0], raw[1:]
        found = _line_terms(
            body,
            document=path.endswith(_DOCUMENT_SUFFIXES),
            changed=marker != " ",
        )
        origin = f"{old_path or path}:{old_line}" if marker == "-" else f"{path}:{line}"
        for term in found:
            origins = terms.setdefault(term, [])
            if origin not in origins:
                origins.append(origin)
        if marker != "+":
            old_line += 1
        if marker != "-":
            line += 1
    return terms


def _git_output(root: Path, *args: str, allow_no_match: bool = False, stdin: str | None = None) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(root), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode == 0 or (allow_no_match and completed.returncode == 1 and not completed.stderr):
        return completed.stdout
    message = completed.stderr.strip() or f"exit {completed.returncode}"
    raise FindingsError([f"git {args[0]}: {message}"])


def _resolve(root: Path, revision: str) -> str:
    return _git_output(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}").strip()


def _clean_text(text: str, terms: Iterable[str] = ()) -> str:
    """候補の本文。長い行は、候補の語ごとに最初に一致した位置の前後を抜粋する（どの語も本文に現れる）。"""
    cleaned = "".join(" " if _is_control(char) else char for char in text).strip()
    if len(cleaned) <= _CANDIDATE_TEXT_LIMIT:
        return cleaned or "(空行)"
    spans: list[list[int]] = []
    for term in sorted(terms):
        match = _word_pattern(term).search(cleaned)
        if match is not None:
            spans.append(
                [
                    max(0, match.start() - _CANDIDATE_TEXT_CONTEXT),
                    min(len(cleaned), match.end() + _CANDIDATE_TEXT_CONTEXT),
                ]
            )
    if not spans:
        return cleaned[:_CANDIDATE_TEXT_LIMIT]
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    excerpt = " … ".join(cleaned[start:end] for start, end in merged)
    return ("… " if merged[0][0] > 0 else "") + excerpt + (" …" if merged[-1][1] < len(cleaned) else "")


def _ere_literal(term: str) -> str:
    """``term`` の文字どおりに一致する POSIX 拡張正規表現。"""
    return _ERE_SPECIAL.sub(r"\\\1", term)


def _word_pattern(term: str) -> re.Pattern[str]:
    """``term`` が、前後に英数字・``_`` の無い位置に現れることを確かめる正規表現。"""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")


def statement_candidates(
    root: Path, from_rev: str, to_rev: str, *, generated_at: str, claims: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """修正の差分（``from_rev..to_rev``）の語で ``to_rev`` の文書を検索し、候補ファイルの文書を返す。"""
    start = _resolve(root, from_rev)
    end = _resolve(root, to_rev)
    terms = extract_terms(
        _git_output(
            root, "diff", "-U3", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", f"{start}..{end}"
        )
    )
    hits: dict[str, list[tuple[str, int, str]]] = {term: [] for term in terms}
    if terms:
        # 語ごとに -e で渡すと git grep は行ごとにすべての語を試すので、語が多いと遅い。どれかの語を含む行を
        # 1 つの拡張正規表現で探し、語の前後の境界は下で確かめる。引数の長さの上限を避けるため、標準入力で渡す。
        any_term = "|".join(_ere_literal(term) for term in sorted(terms))
        output = _git_output(
            root,
            "grep",
            "-n",
            "-E",
            "-I",
            "--no-color",
            "-f",
            "-",
            end,
            "--",
            *_DOCUMENT_PATHSPECS,
            allow_no_match=True,
            stdin=any_term,
        )
        prefix = f"{end}:"
        # 語ごとの正規表現は先に 1 回だけ作る。行ごとに作ると、語が re のキャッシュ（512 個）を超えたとき
        # 作り直しが続き、大きな差分で fix_check の時間を使い切る。
        patterns = [(term, _word_pattern(term)) for term in terms]
        for raw in output.splitlines():
            entry = _GREP_LINE.fullmatch(raw.removeprefix(prefix))
            if entry is None:
                continue
            text = entry["text"]
            for term, pattern in patterns:
                # 部分文字列で先に絞ってから、前後の境界を確かめる（一致の規則は変わらない）。
                if term in text and pattern.search(text) is not None:
                    hits[term].append((entry["path"], int(entry["line"]), text))
    common = sorted(
        ({"term": term, "count": len(found)} for term, found in hits.items() if len(found) > COMMON_TERM_LIMIT),
        key=lambda item: (-cast(int, item["count"]), cast(str, item["term"])),
    )
    common_names = {cast(str, item["term"]) for item in common}
    by_line: dict[tuple[str, int], tuple[set[str], str]] = {}
    for term, found in hits.items():
        if term in common_names:
            continue
        for path, number, text in found:
            names, _ = by_line.setdefault((path, number), (set(), text))
            names.add(term)
    candidates = [
        {"id": f"S-{index}", "path": f"{path}:{number}", "terms": sorted(names), "text": _clean_text(text, names)}
        for index, ((path, number), (names, text)) in enumerate(sorted(by_line.items()), start=1)
    ]
    document: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "kind": "statement_candidates",
        "from": start,
        "to": end,
        "generated_at": generated_at,
        "terms": [{"term": term, "origins": terms[term]} for term in sorted(terms)],
        "common_terms": common,
        "candidates": candidates,
    }
    if claims is not None:
        problems = _Problems()
        _keys(problems, "claims input", claims, frozenset({"fix_ids", "claims"}), frozenset({"fix_ids", "claims"}))
        paths = _git_output(root, "diff", "--no-renames", "--name-only", "-z", start, end, "--").split("\0")
        document.update(
            fix_ids=claims.get("fix_ids"),
            claims=claims.get("claims"),
            changed_files=sorted(path for path in paths if path),
        )
        _check_claim_inventory(problems, document)
        problems.raise_if_any()
    return document


def check_statement_judgments(document: Mapping[str, Any], candidates: Mapping[str, Any]) -> None:
    """fix_check の一覧が、候補ファイルの全候補を 1 回ずつ判定しているかを確かめる。

    候補ファイルの ``from``・``to`` は、``fix_check`` の両端と一致する必要がある。
    ``inconsistent`` と判定した候補は、どれかの指摘の ``statement_sites`` に ``inconsistent`` で載っている必要がある。

    ``document`` が ``validate_document`` を通った後であることを前提にする（欠けたキーや型の誤りは検査しない）。
    ``candidates`` はこの関数の中で検査する。
    """
    validate_document(candidates)
    problems = _Problems()
    section = cast(Mapping[str, Any], document["statement_candidates"])
    name = cast(str, section["file"])
    if candidates.get("kind") != "statement_candidates":
        problems.add("statement_candidates.file", f"{name} is not a statement_candidates document")
        problems.raise_if_any()
    if candidates["from"] != document["fix_check"]["from"]:
        problems.add("statement_candidates.file", f"{name} was made from {candidates['from']}, not fix_check.from")
    if candidates["to"] != document["fix_check"]["to"]:
        problems.add("statement_candidates.file", f"{name} was made for {candidates['to']}, not fix_check.to")
    paths = {cast(str, item["id"]): cast(str, item["path"]) for item in candidates["candidates"]}
    inconsistent_sites = {
        cast(str, site["path"])
        for finding in cast(list[dict[str, Any]], document["findings"])
        for site in cast(list[dict[str, Any]], finding["statement_sites"])
        if site.get("result") == "inconsistent"
    }
    judged: dict[str, int] = {}
    for index, judgment in enumerate(cast(list[dict[str, Any]], section["judgments"])):
        where = f"statement_candidates.judgments[{index}]"
        for candidate_id in cast(list[str], judgment["ids"]):
            judged[candidate_id] = judged.get(candidate_id, 0) + 1
            if candidate_id not in paths:
                problems.add(where, f"{candidate_id} is not in {name}")
            elif judgment["result"] == "inconsistent" and paths[candidate_id] not in inconsistent_sites:
                problems.add(
                    where,
                    f"{candidate_id} ({paths[candidate_id]}) is inconsistent "
                    "but no finding lists it in statement_sites",
                )
    for candidate_id in paths:
        if candidate_id not in judged:
            problems.add("statement_candidates.judgments", f"{candidate_id} ({paths[candidate_id]}) is not judged")
    for candidate_id, count in judged.items():
        if count > 1:
            problems.add("statement_candidates.judgments", f"{candidate_id} is judged more than once")
    if "fix_evidence" in document:
        _check_evidence_judgments(problems, document, candidates)
    problems.raise_if_any()


def _check_evidence_judgments(problems: _Problems, document: Mapping[str, Any], candidates: Mapping[str, Any]) -> None:
    if not all(key in candidates for key in ("fix_ids", "claims", "changed_files")):
        problems.add("fix_evidence", "candidate inventory requires fix_ids, claims and changed_files")
        return
    if set(candidates["fix_ids"]) != set(document["fix_check"]["fix_ids"]):
        problems.add("fix_evidence", "inventory fix_ids must equal fix_check.fix_ids")
    confirmed = {finding["id"] for finding in document["findings"] if finding["verdict"] == "confirmed"}
    remaining = {item["target"] for item in document["coverage"]["remaining"]}
    claims = {claim["id"]: claim for claim in candidates["claims"]}
    for group, identifier, expected, prefix in (
        ("claims", "id", set(claims), "claim"),
        ("files", "path", set(candidates["changed_files"]), "file"),
    ):
        judged: dict[str, int] = {}
        for index, entry in enumerate(document["fix_evidence"][group]):
            where = f"fix_evidence.{group}[{index}]"
            key = entry[identifier]
            judged[key] = judged.get(key, 0) + 1
            if key not in expected:
                problems.add(where, f"{key} is not in inventory")
            if any(finding not in confirmed for finding in entry["findings"]):
                problems.add(where, "findings must reference a confirmed finding")
            if entry["result"] in ("violates", "failed") and not entry["findings"]:
                problems.add(where, "requires a confirmed finding")
            if entry["result"] == "unchecked" and f"{prefix}:{key}" not in remaining:
                problems.add(where, f"{prefix}:{key} must be in coverage.remaining")
            if entry["result"] in ("holds", "passed"):
                checks = entry["checks"]
                if not checks or any(check["exit_code"] != 0 for check in checks):
                    problems.add(where, "success requires executed evidence or static checks with exit_code 0")
                claim = claims.get(key) if group == "claims" else None
                if (
                    claim is not None
                    and claim["kind"] != "static"
                    and not any(
                        check["mode"] == "executed"
                        and check["exit_code"] == 0
                        and (claim["kind"] != "artifact" or check["artifact"] is not None)
                        for check in checks
                    )
                ):
                    problems.add(
                        where,
                        "artifact/behavior success requires executed evidence; "
                        "artifacts need a generated result and predicate",
                    )
        for key in expected:
            if key not in judged:
                problems.add(f"fix_evidence.{group}", f"{key} is not judged")
        for key, count in judged.items():
            if count > 1:
                problems.add(f"fix_evidence.{group}", f"{key} is judged more than once")


def _write_atomically(path: Path, value: object) -> None:
    """同じディレクトリの一時ファイルに書いてから置き換える（途中の状態を残さない）。"""
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


# --- 集計 ---------------------------------------------------------------------------------------------------------

_COUNTS = ("candidates", "confirmed", "not_reproduced", "unverified", "missed")


def reference_key(ref: str) -> str:
    """指摘の参照を、判断の記録（review-records 2.1 節）の書き方にそろえる。

    レビューファイル名の ``.md`` を除く。段階2 で作った一覧の ``.md`` 付きの参照も、同じ指摘として扱うため。
    """
    name, separator, rest = ref.partition("#")
    return f"{name.removesuffix('.md')}{separator}{rest}"


def _review_file_of(ref: str) -> str:
    return f"{reference_key(ref).split('#', 1)[0]}.md"


_METRICS = (
    *_COUNTS,
    "adopted",
    "late_detections",
    "rework",
    "seconds",
    "waiting_seconds",
    "timed_activities",
    "unknown_activities",
    "unknown_waiting_activities",
)


def _actor_identity(actor: Mapping[str, Any], role: str) -> dict[str, Any]:
    observed = actor.get("provenance")
    if observed is None:
        metadata: dict[str, Any] = dict.fromkeys(
            ("agent_id", "provider", "effort", "session_id", "cli_version", "run_id", "request_id"), "unknown"
        )
        metadata["role"] = role
        skill = actor.get("skill") or {"path": "unknown", "sha256": actor.get("skill_sha256") or "unknown"}
        metadata["skills"] = [skill]
    else:
        metadata = dict(observed)
    metadata["skills"] = sorted(
        (dict(skill) for skill in metadata["skills"]), key=lambda skill: (skill["path"], skill["sha256"])
    )
    return {"model": actor["model"], "runtime": actor["runtime"], "provenance": metadata}


def _check_stats_records(value: object) -> None:
    problems = _Problems()
    records = _object(problems, "records", value)
    if records is not None:
        if "schema" in records:
            _choice(problems, "records.schema", records["schema"], (_records.SCHEMA_ID, _records.PROVENANCE_SCHEMA_ID))
        if records.get("pr") is not None:
            _check_pr(problems, records["pr"], where="records.pr")
        for index, item in enumerate(_items(problems, "records.decisions", records.get("decisions"))):
            where = f"records.decisions[{index}]"
            decision = _object(problems, where, item)
            if decision is None:
                continue
            for source in _items(problems, f"{where}.sources", decision.get("sources")):
                _pattern(problems, f"{where}.sources", source, _FINDING_REF, "a finding reference")
            if "state" in decision:
                _choice(problems, f"{where}.state", decision["state"], _records.STATES)
            if decision.get("severity") is not None:
                _choice(problems, f"{where}.severity", decision["severity"], SEVERITIES)
            if "history" in decision:
                for position, history in enumerate(_items(problems, f"{where}.history", decision["history"])):
                    site = f"{where}.history[{position}]"
                    event = _object(problems, site, history)
                    if event is not None:
                        _choice(problems, f"{site}.state", event.get("state"), _records.STATES)
                        if records.get("schema") == _records.PROVENANCE_SCHEMA_ID:
                            _count(problems, f"{site}.round", event.get("round"))
        if records.get("schema") == _records.PROVENANCE_SCHEMA_ID:
            for index, item in enumerate(_items(problems, "records.rounds", records.get("rounds"))):
                where = f"records.rounds[{index}]"
                round_entry = _object(problems, where, item)
                if round_entry is None:
                    continue
                _count(problems, f"{where}.round", round_entry.get("round"))
                for seat in _items(problems, f"{where}.seats", round_entry.get("seats")):
                    agent = _object(problems, f"{where}.seats", seat)
                    if agent is not None:
                        _pattern(
                            problems, f"{where}.seats.review_file", agent.get("review_file"), _ID, "a review file"
                        )
                        _pattern(
                            problems,
                            f"{where}.seats.file_head",
                            agent.get("file_head"),
                            _SHORT_HEAD,
                            "12 hex or unknown",
                        )
                        if "provenance" in agent:
                            _check_agent(
                                problems,
                                f"{where}.seats.identity",
                                {key: agent.get(key) for key in ("model", "runtime", "provenance")},
                                v2=True,
                            )
    problems.raise_if_any()


def _adoption_matches(records: Mapping[str, Any], source: str, actor: Mapping[str, Any]) -> bool | None:
    """v2の判断に実担当が残っていれば、同じ参照の別セッションへ採用を転用しない。

    actor 自身の実行識別子がすべて未観測（unknown）なら、実担当を同一実行と確認できないため None とする。
    同席が actor と同じ実行識別子で観測されていれば一致し、その判断を後の巡に採用する。
    一致する同席がなく、あとに未観測の同席が残る場合だけ、実担当の特定を保留して None とする。
    """
    if records.get("schema") != _records.PROVENANCE_SCHEMA_ID:
        return True
    provenance = actor.get("provenance")
    if provenance is None:
        return None
    if all(provenance.get(key) == "unknown" for key in ("session_id", "run_id", "request_id")):
        return None
    file = reference_key(source).split("#", 1)[0]
    adoption_rounds = {
        event.get("round")
        for decision in records["decisions"]
        if reference_key(source) in {reference_key(ref) for ref in decision["sources"]}
        for event in decision.get("history", [])
        if event.get("state") == "fix" and event.get("change") != "inherited"
    }
    candidates = [
        _actor_identity(seat, "find")
        for round_entry in records["rounds"]
        if round_entry.get("round") in adoption_rounds
        for seat in round_entry["seats"]
        if seat.get("review_file") == file and "provenance" in seat
    ]
    if not candidates:
        return None
    identity = _actor_identity(actor, "find")
    if identity in candidates:
        return True
    if any(
        all(candidate["provenance"].get(key) == "unknown" for key in ("session_id", "run_id", "request_id"))
        for candidate in candidates
    ):
        return None
    return False


def _provenance_stats(documents: Sequence[Mapping[str, Any]], records: Mapping[str, Any] | None) -> dict[str, Any]:
    """発見の採用実績と、実担当に帰属する時間・手戻りを集計する。"""
    entries: dict[str, dict[str, Any]] = {}
    adopted_refs: set[str] = set()
    for decision in (records or {}).get("decisions", []):
        if decision.get("state") == "fix" or any(item.get("state") == "fix" for item in decision.get("history", [])):
            adopted_refs.update(reference_key(source) for source in decision.get("sources", []))
    counted_adoptions: set[tuple[str, str, str]] = set()
    counted_rework: set[tuple[str, str, str]] = set()

    def entry_for(actor: Mapping[str, Any], role: str) -> tuple[str, dict[str, Any]]:
        identity = {"stage": role, **_actor_identity(actor, role)}
        key = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        if key not in entries:
            entries[key] = {**identity, **dict.fromkeys(_METRICS, 0)}
        return key, entries[key]

    def add_time(entry: dict[str, Any], seconds: Any, waiting: Any) -> None:
        if seconds is None:
            entry["unknown_activities"] += 1
        else:
            entry["seconds"] += seconds
            entry["timed_activities"] += 1
        if waiting is None:
            entry["unknown_waiting_activities"] += 1
        else:
            entry["waiting_seconds"] += waiting

    explicit_times: set[tuple[str, str, str]] = set()
    for document in documents:
        if document.get("kind") == "planning" and document.get("schema") == PROVENANCE_SCHEMA_ID:
            for activity in document["activities"]:
                key, _ = entry_for(activity["actor"], activity["stage"])
                explicit_times.add((json.dumps(document["pr"], sort_keys=True), document["reviewed_head"], key))
    for document in documents:
        pr_key = json.dumps(document.get("pr"), sort_keys=True)
        if document.get("kind") == "planning" and document.get("schema") == PROVENANCE_SCHEMA_ID:
            for activity in document["activities"]:
                _, entry = entry_for(activity["actor"], activity["stage"])
                add_time(entry, activity["seconds"], activity["waiting_seconds"])
            for link in document["rework"]:
                key, entry = entry_for(link["implementation"], "implement")
                token = (key, pr_key, reference_key(link["source"]))
                if token not in counted_rework:
                    counted_rework.add(token)
                    entry["rework"] += 1
            continue
        if document.get("kind") != "verified" or document.get("mode") != "cycle":
            continue
        inputs = {item["review_file"]: item for item in document["inputs"]}
        by_file: dict[str, tuple[str, dict[str, Any]]] = {}
        for reviewer in document["accuracy"]["reviewers"]:
            name = reviewer["review_file"]
            key, entry = entry_for(reviewer, "find")
            by_file[name] = (key, entry)
            for metric in _COUNTS:
                entry[metric] += reviewer[metric]
            if (pr_key, document["reviewed_head"], key) not in explicit_times:
                add_time(
                    entry, document["accuracy"]["find_seconds"].get(name), inputs.get(name, {}).get("waiting_seconds")
                )
            if records is None or records.get("pr") != document["pr"]:
                entry["adopted"] = None
        for finding in document["findings"]:
            late_keys: set[str] = set()
            for source in finding["sources"]:
                reporter = by_file.get(_review_file_of(source))
                if reporter is None:
                    continue
                key, entry = reporter
                token = (key, pr_key, reference_key(source))
                if (
                    entry["adopted"] is not None
                    and reference_key(source) in adopted_refs
                    and token not in counted_adoptions
                ):
                    original = inputs.get(_review_file_of(source), {})
                    matched = _adoption_matches(cast(Mapping[str, Any], records), source, original)
                    if matched is None:
                        entry["adopted"] = None
                    elif matched:
                        counted_adoptions.add(token)
                        entry["adopted"] += 1
                if (
                    finding.get("verdict") == "confirmed"
                    and finding.get("late_detection") is True
                    and key not in late_keys
                ):
                    late_keys.add(key)
                    entry["late_detections"] += 1
        verifier = {**document["verifier"], "skill": document["skill"]}
        key, entry = entry_for(verifier, "verify")
        timing = document["timing"]
        if (pr_key, document["reviewed_head"], key) not in explicit_times:
            add_time(entry, timing["seconds"], timing.get("waiting_seconds"))
    rows = [entries[key] for key in sorted(entries)]
    if records is None:
        for entry in rows:
            entry["adopted"] = None
    axes: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in rows:
        metadata = entry["provenance"]
        values = {name: [entry[name]] for name in ("model", "runtime", "stage")}
        values.update(
            {
                name: [metadata[name]]
                for name in (
                    "role",
                    "effort",
                    "agent_id",
                    "cli_version",
                    "provider",
                    "session_id",
                    "run_id",
                    "request_id",
                )
            }
        )
        values["skill"] = [skill["path"] + "@" + skill["sha256"] for skill in metadata["skills"]]
        for axis, labels in values.items():
            groups = axes.setdefault(axis, {})
            for label in labels:
                total = groups.setdefault(label, {"value": label, **dict.fromkeys(_METRICS, 0)})
                for metric in _METRICS:
                    if total[metric] is None or entry[metric] is None:
                        total[metric] = None
                    else:
                        total[metric] += entry[metric]
    return {
        "executions": rows,
        "by_dimension": {axis: [groups[label] for label in sorted(groups)] for axis, groups in sorted(axes.items())},
    }


_RUN_PROVENANCE_AXES = ("role", "effort", "agent_id", "cli_version", "provider", "session_id", "run_id", "request_id")
_RUN_STAGES = ("prepare", "review", "save", "cleanup", "total")


def _run_totals() -> dict[str, Any]:
    return {
        "runs": 0,
        "success": 0,
        "failed": 0,
        "incomplete": 0,
        "timed_out": 0,
        "completion_rate": None,
        "failed_at": dict.fromkeys(RUN_FAILED_AT, 0),
        "seconds": {stage: {"total": 0, "measured": 0} for stage in _RUN_STAGES},
        "changed_lines": 0,
        "review_seconds_per_changed_line": None,
        "_line_seconds": 0,
    }


def _add_run(totals: dict[str, Any], document: Mapping[str, Any]) -> None:
    totals["runs"] += 1
    runner = document["runner"]
    if document["status"] != "started":
        totals[document["status"]] += 1
    elif runner is not None and runner["attempt"]["state"] == "timed_out":
        totals["timed_out"] += 1
    else:
        totals["incomplete"] += 1
    if document["failed_at"] is not None:
        totals["failed_at"][document["failed_at"]] += 1
    timing = document["timing"]
    for stage in _RUN_STAGES:
        seconds = timing[f"{stage}_seconds"]
        if seconds is not None:
            totals["seconds"][stage]["total"] += seconds
            totals["seconds"][stage]["measured"] += 1
    inputs = cast(Mapping[str, Any] | None, document["inputs"])
    diff = inputs["diff"] if inputs is not None else None
    if diff is not None and timing["review_seconds"] is not None:
        totals["changed_lines"] += diff["added"] + diff["deleted"]
        totals["_line_seconds"] += timing["review_seconds"]


def _finish_run_totals(totals: dict[str, Any]) -> dict[str, Any]:
    line_seconds = totals.pop("_line_seconds")
    runs, lines = totals["runs"], totals["changed_lines"]
    totals["completion_rate"] = totals["success"] / runs if runs else None
    totals["review_seconds_per_changed_line"] = line_seconds / lines if lines else None
    return totals


def _run_labels(document: Mapping[str, Any]) -> dict[str, list[str]]:
    """7.1 節の軸と、ランナーが観測した値の軸（runner_*）。runner 欄が無い記録は unattached。"""
    reviewer = document["reviewer"]
    metadata = reviewer["provenance"]
    runner = document["runner"]
    labels = {"model": [reviewer["model"]], "runtime": [reviewer["runtime"]]}
    labels.update({name: [metadata[name]] for name in _RUN_PROVENANCE_AXES})
    labels["skill"] = [skill["path"] + "@" + skill["sha256"] for skill in metadata["skills"]]
    for key in RUNNER_IDENTITY_KEYS:
        labels[f"runner_{key}"] = [runner["identity"][key] if runner is not None else "unattached"]
    labels["runner_state"] = [runner["attempt"]["state"] if runner is not None else "unattached"]
    return labels


def run_stats(documents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """実行記録（``runs/``）から、軸ごとの完走率・失敗した工程・工程ごとの時間を集計する（7.2 節）。

    status が started の記録は、ランナーが timed_out と観測していればタイムアウト、それ以外は未完了に数える。
    """
    totals = _run_totals()
    axes: dict[str, dict[str, dict[str, Any]]] = {}
    for document in documents:
        _add_run(totals, document)
        for axis, values in _run_labels(document).items():
            groups = axes.setdefault(axis, {})
            for value in values:
                _add_run(groups.setdefault(value, {"value": value, **_run_totals()}), document)
    return {
        "documents": len(documents),
        "total": _finish_run_totals(totals),
        "by_dimension": {
            axis: [_finish_run_totals(groups[label]) for label in sorted(groups)]
            for axis, groups in sorted(axes.items())
        },
    }


def stats(
    verified: Sequence[Mapping[str, Any]],
    records: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """サイクルの確認（``mode: cycle``）の一覧から、レビュアーごとの精度を集計する。

    ``records`` は ``review_records.py fold`` の出力。判断に使った重大度と比べて、重大度のずれを数える。
    ``runs`` は検査済みの実行記録で、``runs`` キーに集計する。既存のキーの値には影響しない。
    """
    if records is not None:
        _check_stats_records(records)
    decided: dict[str, str] = {}
    for decision in cast(list[dict[str, Any]], (records or {}).get("decisions") or []):
        for source in cast(list[str], decision.get("sources") or []):
            decided[reference_key(source)] = cast(str, decision.get("severity"))
    totals: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    drift: list[dict[str, str]] = []
    documents = 0
    for document in verified:
        if document.get("kind") != "verified" or document.get("mode") != "cycle":
            continue
        documents += 1
        keys_by_file: dict[str, tuple[str, str, str | None]] = {}
        for reviewer in cast(list[dict[str, Any]], document["accuracy"]["reviewers"]):
            key = (cast(str, reviewer["model"]), cast(str, reviewer["runtime"]), reviewer.get("skill_sha256"))
            keys_by_file[cast(str, reviewer["review_file"])] = key
            entry = totals.setdefault(
                key,
                {
                    "model": key[0],
                    "runtime": key[1],
                    "skill_sha256": key[2],
                    **dict.fromkeys(_COUNTS, 0),
                    "late_detections": 0,
                    "severity_drift": 0,
                },
            )
            for count in _COUNTS:
                entry[count] += cast(int, reviewer[count])
        for finding in cast(list[dict[str, Any]], document["findings"]):
            reporters = {keys_by_file.get(_review_file_of(source)) for source in finding["sources"]}
            if finding.get("verdict") == "confirmed" and finding.get("late_detection") is True:
                for key in reporters:
                    if key is not None:
                        totals[key]["late_detections"] += 1
            for reported in cast(list[dict[str, str]], finding["reported_severities"]):
                source = reported["source"]
                decision = decided.get(reference_key(source))
                if records is not None and records.get("pr") is not None and records["pr"] != document["pr"]:
                    decision = None
                if decision is None or decision == reported["severity"]:
                    continue
                drift.append({"source": source, "reported": reported["severity"], "decided": decision})
                key = keys_by_file.get(_review_file_of(source))
                if key is not None:
                    totals[key]["severity_drift"] += 1
    reviewers: list[dict[str, Any]] = []
    for key in sorted(totals, key=lambda item: (item[0], item[1], item[2] or "")):
        entry = totals[key]
        judged = entry["confirmed"] + entry["not_reproduced"]
        entry["false_positive_rate"] = entry["not_reproduced"] / judged if judged else None
        reviewers.append(entry)
    return {
        "documents": documents,
        "reviewers": reviewers,
        "severity_drift": drift,
        "provenance": _provenance_stats(verified, records),
        "runs": run_stats(runs),
    }


# --- CLI ----------------------------------------------------------------------------------------------------------


def _load_json(text: str, source: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise FindingsError([f"{source}: invalid JSON at line {error.lineno}: {error.msg}"]) from error


def _read_json(path: Path) -> object:
    return _load_json(path.read_text(encoding="utf-8"), str(path))


def _not_aggregated(document: Mapping[str, object]) -> bool:
    """修正の確認・v1工程記録・機械的候補を従来どおり読み飛ばす。v2工程記録は集計する。"""
    kind = document.get("kind")
    return (
        kind in ("candidates", "statement_candidates")
        or (kind == "planning" and document.get("schema") == SCHEMA_ID)
        or (kind == "verified" and document.get("mode") == "fix_check")
    )


def _verified_documents(directories: Sequence[Path]) -> list[dict[str, Any]]:
    """``verified/`` の一覧を検査して返す。

    集計しないと分かっている文書は、検査せずに読み飛ばす。段階 2 までに作った修正の確認の一覧が、
    後から足した必須の節を持たなくても止まらないようにする。``kind``・``mode`` が無い・知らない値の文書は
    サイクルの確認の一覧の書き損じかもしれないので、読み飛ばさずに検査で止める。
    """
    documents: list[dict[str, Any]] = []
    for directory in directories:
        for path in sorted((directory / "verified").glob("*.json")):
            value = _read_json(path)
            document = _mapping(value)
            if document is not None and _not_aggregated(document):
                continue
            try:
                documents.append(validate_document(value))
            except FindingsError as error:
                raise FindingsError([f"{path}: {problem}" for problem in error.problems]) from error
    return documents


def _run_documents(directories: Sequence[Path]) -> list[dict[str, Any]]:
    """各ディレクトリの ``runs/*.json`` を検査して返す。実行記録以外のファイルは置き場所の書き損じとして止める。"""
    documents: list[dict[str, Any]] = []
    for directory in directories:
        for path in sorted((directory / "runs").glob("*.json")):
            try:
                document = validate_document(_read_json(path))
                if document["kind"] != "review_run":
                    raise FindingsError(["kind: runs/ holds only review_run documents"])
            except FindingsError as error:
                raise FindingsError([f"{path}: {problem}" for problem in error.problems]) from error
            documents.append(document)
    return documents


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review_findings.py", description="review-findings v1 / v2 の文書を扱う")
    commands = parser.add_subparsers(dest="command", required=True)
    validate_command = commands.add_parser("validate", help="候補一覧・確認済みの一覧・工程の記録を検査する")
    validate_command.add_argument("file", type=Path)
    markdown_command = commands.add_parser("from-markdown", help="レビューファイルを候補一覧の形に読み取る")
    markdown_command.add_argument("file", type=Path)
    markdown_command.add_argument("--review-file", required=True)
    markdown_command.add_argument("--repo", required=True)
    markdown_command.add_argument("--pr", type=int, required=True)
    markdown_command.add_argument("--model", required=True)
    markdown_command.add_argument("--runtime", default="unknown")
    pair_command = commands.add_parser("check-pair", help="レビューファイルと候補一覧が一致するかを確かめる")
    pair_command.add_argument("markdown", type=Path)
    pair_command.add_argument("json", type=Path)
    candidates_command = commands.add_parser(
        "statement-candidates", help="修正の差分の語で、条件を述べている文書の候補を列挙する"
    )
    candidates_command.add_argument(
        "--root",
        type=Path,
        required=True,
        help="対象の PR のリポジトリの checkout（TARGET_REPO_ROOT。from と to の commit があること）",
    )
    candidates_command.add_argument("--from", dest="from_rev", required=True, help="push 済みの PR head")
    candidates_command.add_argument("--to", dest="to_rev", required=True, help="確かめるローカルの HEAD")
    candidates_command.add_argument("--output", type=Path, required=True)
    candidates_command.add_argument(
        "--claims", type=Path, help="fix_ids と列挙済みclaimsのJSON。全変更ファイルも列挙する"
    )
    stats_command = commands.add_parser(
        "stats", help="確認済みの一覧からレビュアーごとの精度を、実行記録から完走率と工程ごとの時間を集計する"
    )
    stats_command.add_argument("directories", type=Path, nargs="+")
    stats_command.add_argument("--records", type=Path, help="review_records.py fold の出力")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            document = validate_document(_read_json(args.file))
            if document.get("kind") == "verified" and document.get("mode") == "fix_check":
                section = cast(dict[str, Any], document["statement_candidates"])
                candidates_path = args.file.parent / cast(str, section["file"])
                check_statement_judgments(document, cast(dict[str, Any], _read_json(candidates_path)))
        elif args.command == "from-markdown":
            value = candidates_from_markdown(
                args.file.read_text(encoding="utf-8"),
                review_file=args.review_file,
                repository=args.repo,
                number=args.pr,
                model=args.model,
                runtime=args.runtime,
            )
            validate_document(value)
            sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        elif args.command == "check-pair":
            candidates = validate_document(_read_json(args.json))
            check_pair(args.markdown.read_text(encoding="utf-8"), candidates)
        elif args.command == "statement-candidates":
            generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            claims = None
            if args.claims is not None:
                raw = _read_json(args.claims)
                if not isinstance(raw, dict):
                    raise FindingsError(["claims input: must be an object"])
                claims = cast(dict[str, Any], raw)
            document = statement_candidates(
                args.root, args.from_rev, args.to_rev, generated_at=generated_at, claims=claims
            )
            validate_document(document)
            _write_atomically(args.output, document)
            summary = {
                "output": str(args.output),
                "candidates": len(document["candidates"]),
                "common_terms": len(document["common_terms"]),
            }
            sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
        else:
            records = None if args.records is None else cast(dict[str, Any], _read_json(args.records))
            result = stats(_verified_documents(args.directories), records, _run_documents(args.directories))
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except FindingsError as error:
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
