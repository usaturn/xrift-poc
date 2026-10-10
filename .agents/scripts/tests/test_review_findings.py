"""Issue #547: review-findings v1（候補一覧・確認済みの一覧・工程の記録）の検査・読み取り・突き合わせ・集計。"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "review_findings.py"
SPEC = importlib.util.spec_from_file_location("review_findings", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
review_findings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_findings)

FORMAT_DOC = Path(__file__).resolve().parents[2] / "review-findings.md"

HEAD = "30f27c22a4a27b84ecf43a2ef23a3b10f2ce421a"
BASE = "e342dc8b6cb7fdc1f2c1f9a3d2b8c46b5a0a1f23"
FIX = "d7b1085f0e1d2c3b4a5968778695a4b3c2d1e0f9"
SHA = "0123456789abcdef" * 4

Mutation = Callable[[dict[str, Any]], None]


def candidate_finding(finding_id: str = "H-1", severity: str = "High") -> dict[str, Any]:
    return {
        "id": finding_id,
        "severity": severity,
        "confidence": "verified",
        "title": "リンクの箇条書きを checkbox と誤認する",
        "locations": ["tools/example.py:41"],
        "detected_head": HEAD[:12],
        "reproduction": {
            "status": "executed",
            "commands": ["uv run --no-project python repro.py"],
            "place": "PR head の隔離 worktree",
            "conditions": "`- [x](url)` を含む本文",
            "result": "終了コード 0、checkbox として 1 件数える",
        },
        "usage_path": {"value": "normal", "detail": "スキルの手順が呼ぶ CLI"},
        "result_kind": {"value": "wrong_result", "detail": "完了していない項目を完了と数える"},
        "pr_relation": {"value": "introduced", "basis": "1077b78 が入れた正規表現"},
        "same_kind": {
            "property": "checkbox は `- [ ]` か `- [x]` の後に空白が続く行だけ",
            "range": "PR の差分",
            "search": "rg -n 'CHECKBOX' tools",
            "result": "他に無し",
        },
        "failure_scenario": {
            "entry": "loop の finalize",
            "input": "PR 本文の `- [x](https://example.com)`",
            "environment": "通常の Dev Container",
            "steps": ["PR 本文を読む", "未完了の checkbox を数える"],
            "expected": "リンクは checkbox に数えない",
            "actual": "完了済みの checkbox として数える",
        },
        "property": "checkbox は `- [ ]` か `- [x]` の後に空白が続く行だけ",
        "variants": [
            {"input": "`- [x][ref]`（参照リンク）", "result": "violates", "how": "repro.py に同じ本文を渡した"},
            {"input": "`- [ ] 項目`", "result": "holds", "how": "repro.py"},
        ],
        "statement_sites": [
            {"path": "README.md:120", "claim": "未完了の checkbox があれば止まる", "result": "consistent"},
        ],
        "suggested_fix": "`\\](?!\\()` ではなく、行頭の `- [ ] ` だけを数える",
    }


def candidates() -> dict[str, Any]:
    return {
        "schema": "review-findings/v1",
        "kind": "candidates",
        "source": "reviewer",
        "pr": {"repository": "usaturn/maatlog-poc", "number": 542},
        "reviewed_head": HEAD,
        "base": BASE,
        "reviewer": {"model": "claude-opus-5-5", "runtime": "claude-code", "review_file": "claude-opus-5-5.md"},
        "skill": {"path": ".claude/skills/pr-review/SKILL.md", "sha256": SHA},
        "timing": {"started_at": "2026-10-04T01:00:00Z", "finished_at": "2026-10-04T01:40:00Z", "seconds": 2400},
        "coverage": {
            "read": ["PR の差分全体", "README.md"],
            "checks": [{"command": "uv run --no-project python repro.py", "result": "誤認を再現"}],
            "declined": [{"target": "interfaces.rst の全文", "reason": "時間切れ"}],
        },
        "findings": [candidate_finding()],
        "unverified_candidates": [{"id": "U-1", "summary": "復旧の初期化に失敗したとき owner が残る疑い"}],
    }


def verified_finding(finding_id: str = "V-1", verdict: str = "confirmed") -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": finding_id,
        "sources": [f"claude-opus-5-5#H-1@{HEAD[:12]}", f"gpt-6.1-sol#M-2@{HEAD[:12]}"],
        "property": "checkbox は `- [ ]` か `- [x]` の後に空白が続く行だけ",
        "locations": ["tools/example.py:41"],
        "verdict": verdict,
        "basis": "隔離 worktree で再現した",
        "variants": [{"input": "`- [x][ref]`", "result": "violates", "how": "repro.py"}],
        "statement_sites": [{"path": "README.md:120", "claim": "未完了なら止まる", "result": "inconsistent"}],
        "pr_relation": {"value": "introduced", "basis": "base には無い"},
        "reported_severities": [
            {"source": f"claude-opus-5-5#H-1@{HEAD[:12]}", "severity": "High"},
            {"source": f"gpt-6.1-sol#M-2@{HEAD[:12]}", "severity": "Medium"},
        ],
    }
    if verdict == "confirmed":
        value["reproduction"] = {
            "commands": ["uv run --no-project python repro.py"],
            "place": "隔離 worktree",
            "conditions": "参照リンクを含む本文",
            "result": "終了コード 0、誤って数える",
        }
    if verdict == "unverified":
        value["missing_checks"] = "Windows の環境が無い"
    return value


def verified() -> dict[str, Any]:
    return {
        "schema": "review-findings/v1",
        "kind": "verified",
        "mode": "cycle",
        "pr": {"repository": "usaturn/maatlog-poc", "number": 542},
        "reviewed_head": HEAD,
        "base": BASE,
        "verifier": {"model": "claude-opus-5-5", "runtime": "claude-code"},
        "skill": {"path": ".claude/skills/review-verify/SKILL.md", "sha256": SHA},
        "timing": {"started_at": "2026-10-04T02:00:00Z", "finished_at": "2026-10-04T02:25:00Z", "seconds": 1500},
        "inputs": [
            {
                "review_file": "claude-opus-5-5.md",
                "model": "claude-opus-5-5",
                "runtime": "claude-code",
                "input_kind": "json",
                "findings_file": f"findings/claude-opus-5-5-{HEAD[:12]}.json",
                "reviewed_head": HEAD[:12],
                "stale": False,
            },
            {
                "review_file": "gpt-6.1-sol.md",
                "model": "gpt-6.1-sol",
                "runtime": "codex",
                "input_kind": "markdown",
                "findings_file": None,
                "reviewed_head": HEAD[:12],
                "stale": False,
            },
        ],
        "coverage": {
            "closed": [{"target": "interfaces.rst の全文", "how": "読んで条件の記述を照合した"}],
            "remaining": [],
        },
        "findings": [verified_finding()],
        "accuracy": {
            "reviewers": [
                {
                    "review_file": "claude-opus-5-5.md",
                    "model": "claude-opus-5-5",
                    "runtime": "claude-code",
                    "skill_sha256": SHA,
                    "candidates": 1,
                    "confirmed": 1,
                    "not_reproduced": 0,
                    "unverified": 0,
                    "missed": 0,
                    "unique": [],
                },
                {
                    "review_file": "gpt-6.1-sol.md",
                    "model": "gpt-6.1-sol",
                    "runtime": "codex",
                    "skill_sha256": None,
                    "candidates": 1,
                    "confirmed": 1,
                    "not_reproduced": 0,
                    "unverified": 0,
                    "missed": 0,
                    "unique": [],
                },
            ],
            "find_seconds": {"claude-opus-5-5.md": 2400, "gpt-6.1-sol.md": None},
            "verify_seconds": 1500,
        },
    }


def fix_check() -> dict[str, Any]:
    value = verified()
    value["mode"] = "fix_check"
    value["reviewed_head"] = FIX
    value["fix_check"] = {"from": HEAD, "to": FIX, "fix_ids": [f"claude-opus-5-5#H-1@{HEAD[:12]}"]}
    finding = verified_finding()
    finding["sources"] = []
    finding["reported_severities"] = []
    value["findings"] = [finding]
    value["statement_candidates"] = {
        "file": f"{FIX[:12]}.statement-candidates.json",
        "judgments": [{"ids": ["S-1"], "result": "inconsistent", "basis": "修正後の条件を書いていない"}],
    }
    return value


def planning() -> dict[str, Any]:
    return {
        "schema": "review-findings/v1",
        "kind": "planning",
        "pr": {"repository": "usaturn/maatlog-poc", "number": 542},
        "reviewed_head": HEAD,
        "planner": {"model": "claude-opus-5-5", "runtime": "claude-code"},
        "verified_file": f"verified/{HEAD[:12]}.json",
        "stages": {"decide_seconds": 600, "fix_seconds": 1200, "fix_check_seconds": [700]},
        "fix_checks": [{"file": f"verified/{FIX[:12]}.fix-check.json", "blocking_fixes": 0}],
        "outcome": "fixes_pushed",
        "comment_id": 5987695309,
    }


@pytest.mark.parametrize("outcome", ["stopped", "stopped_insufficient_verification", "awaiting_approval"])
def test_planning_accepts_new_and_legacy_outcomes(outcome: str) -> None:
    document = planning()
    document["outcome"] = outcome
    assert review_findings.validate_document(document)["outcome"] == outcome


def provenance(agent: str = "self", role: str = "verify", effort: str = "high") -> dict[str, Any]:
    return {
        "agent_id": agent,
        "role": role,
        "provider": "example-provider",
        "effort": effort,
        "session_id": "example-session",
        "cli_version": "1.2.3",
        "run_id": "example-run",
        "request_id": "example-request",
        "skills": [{"path": ".agents/skills/example/SKILL.md", "sha256": SHA}],
    }


def actor(agent: str = "self", role: str = "implement") -> dict[str, Any]:
    return {"model": "example-model", "runtime": "pi", "provenance": provenance(agent, role)}


def document_v2(kind: str = "verified") -> dict[str, Any]:
    value = {"candidates": candidates, "verified": verified, "planning": planning}[kind]()
    value["schema"] = "review-findings/v2"
    owner = {"candidates": "reviewer", "verified": "verifier", "planning": "planner"}[kind]
    value[owner]["provenance"] = provenance(
        role={"candidates": "find", "verified": "verify", "planning": "decide"}[kind]
    )
    if kind != "planning":
        value["timing"]["waiting_seconds"] = 12
        value[owner]["provenance"]["skills"] = [copy.deepcopy(value["skill"])]
    if kind == "verified":
        for entry in value["inputs"]:
            entry["provenance"] = provenance(entry["model"], "find")
            entry["waiting_seconds"] = 10
        for entry, original in zip(value["accuracy"]["reviewers"], value["inputs"], strict=True):
            entry["provenance"] = copy.deepcopy(original["provenance"])
        value["inputs"][1]["waiting_seconds"] = None
    if kind == "planning":
        value["activities"] = [{"stage": "implement", "actor": actor(), "seconds": 50.5, "waiting_seconds": 10}]
        value["rework"] = []
    return value


@pytest.mark.parametrize("kind", ["candidates", "verified", "planning"])
def test_v2_document_accepts_observed_identity_and_real_activities(kind: str) -> None:
    value = document_v2(kind)
    assert review_findings.validate_document(value) == value


@pytest.mark.parametrize("kind,owner", [("candidates", "reviewer"), ("verified", "verifier"), ("planning", "planner")])
def test_v2_document_requires_owner_provenance(kind: str, owner: str) -> None:
    value = document_v2(kind)
    del value[owner]["provenance"]
    with pytest.raises(review_findings.FindingsError, match=owner + ".provenance"):
        review_findings.validate_document(value)


@pytest.mark.parametrize("bad", [-1, True, float("inf"), float("nan"), "5"])
def test_v2_activity_rejects_invalid_duration(bad: object) -> None:
    value = document_v2("planning")
    value["activities"][0]["seconds"] = bad
    with pytest.raises(review_findings.FindingsError, match="activities.*seconds"):
        review_findings.validate_document(value)


def test_v2_timing_distinguishes_unknown_zero_and_waiting_overrun() -> None:
    value = document_v2("candidates")
    value["timing"].update(seconds=None, waiting_seconds=None)
    review_findings.validate_document(value)
    value["timing"].update(seconds=0, waiting_seconds=0)
    review_findings.validate_document(value)
    value["timing"]["waiting_seconds"] = 1
    with pytest.raises(review_findings.FindingsError, match="waiting_seconds"):
        review_findings.validate_document(value)


def test_v2_input_and_accuracy_cannot_claim_different_sessions() -> None:
    value = document_v2()
    value["accuracy"]["reviewers"][0]["provenance"]["session_id"] = "other-session"
    with pytest.raises(review_findings.FindingsError, match="provenance"):
        review_findings.validate_document(value)


def test_v2_source_must_resolve_to_an_input_file() -> None:
    value = document_v2()
    value["findings"][0]["sources"][0] = "other-model#H-1@000000000000"
    with pytest.raises(review_findings.FindingsError, match="sources"):
        review_findings.validate_document(value)


@pytest.mark.parametrize("suffix", ["", ".md"])
@pytest.mark.parametrize("detected", [HEAD[:12], "aaaa55556666", "unknown"])
def test_v2_cycle_sources_keep_the_detection_head_across_updates(detected: str, suffix: str) -> None:
    """継続指摘の検出時 HEAD（sources の @<head>）は Update 後の reviewed_head とは比較しない。"""
    value = document_v2()
    value["reviewed_head"] = FIX
    for item in value["inputs"]:
        item["reviewed_head"] = FIX[:12]
    value["findings"][0]["sources"] = [
        f"claude-opus-5-5{suffix}#H-1@{detected}",
        f"gpt-6.1-sol#M-2@{detected}",
    ]
    review_findings.validate_document(value)


def test_v2_rework_links_require_a_real_implementation_actor() -> None:
    value = document_v2("planning")
    implementation = actor()
    value["rework"] = [
        {"source": f"example-model#H-1@{HEAD[:12]}", "implementation": implementation, "detected_stage": "self_review"}
    ]
    review_findings.validate_document(value)
    del implementation["provenance"]["session_id"]
    with pytest.raises(review_findings.FindingsError, match="session_id"):
        review_findings.validate_document(value)


def statement_candidates_document() -> dict[str, Any]:
    return {
        "schema": "review-findings/v1",
        "kind": "statement_candidates",
        "from": HEAD,
        "to": FIX,
        "generated_at": "2026-10-05T00:00:00Z",
        "terms": [{"term": "LIMIT_VALUE", "origins": ["tool.py:1"]}],
        "common_terms": [{"term": "HEAD", "count": 40}],
        "candidates": [{"id": "S-1", "path": "README.md:120", "terms": ["LIMIT_VALUE"], "text": "LIMIT_VALUE を守る"}],
    }


def problems_of(value: object) -> list[str]:
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.validate_document(value)
    return list(caught.value.problems)


# --- validate ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [candidates, verified, fix_check, planning, statement_candidates_document])
def test_valid_documents_pass(factory: Callable[[], dict[str, Any]]) -> None:
    value = factory()
    assert review_findings.validate_document(copy.deepcopy(value)) == value


def _drop(*path: str | int) -> Mutation:
    def mutate(value: dict[str, Any]) -> None:
        target: Any = value
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _set(value_to_set: object, *path: str | int) -> Mutation:
    def mutate(value: dict[str, Any]) -> None:
        target: Any = value
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value_to_set

    return mutate


def test_statement_candidates_section_is_required_only_for_fix_check() -> None:
    missing = fix_check()
    del missing["statement_candidates"]
    assert "statement_candidates: required when mode is fix_check" in problems_of(missing)
    cycle = verified()
    cycle["statement_candidates"] = fix_check()["statement_candidates"]
    assert "statement_candidates: allowed only when mode is fix_check" in problems_of(cycle)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (_set("other.json", "statement_candidates", "file"), "statement_candidates.file"),
        (_set([], "statement_candidates", "judgments", 0, "ids"), "judgments[0].ids"),
        (_set(["V-1"], "statement_candidates", "judgments", 0, "ids"), "judgments[0].ids[0]"),
        (_set("maybe", "statement_candidates", "judgments", 0, "result"), "judgments[0].result"),
        (_drop("statement_candidates", "judgments", 0, "basis"), "judgments[0].basis"),
    ],
)
def test_invalid_statement_judgments_name_the_problem(
    mutation: Callable[[dict[str, Any]], None], expected: str
) -> None:
    value = fix_check()
    mutation(value)
    assert any(expected in problem for problem in problems_of(value))


def judged_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    candidates_value = statement_candidates_document()
    candidates_value["candidates"].append(
        {"id": "S-2", "path": "docs/usage.rst:3", "terms": ["LIMIT_VALUE"], "text": "LIMIT_VALUE の別の説明"}
    )
    document = fix_check()
    document["statement_candidates"]["judgments"].append(
        {"ids": ["S-2"], "result": "unrelated", "basis": "別のコマンドの同名の値"}
    )
    return document, candidates_value


def test_check_statement_judgments_accepts_a_complete_judgment() -> None:
    document, candidates_value = judged_pair()
    review_findings.check_statement_judgments(document, candidates_value)


def _judgment_problems(document: dict[str, Any], candidates_value: dict[str, Any]) -> list[str]:
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.check_statement_judgments(document, candidates_value)
    return list(caught.value.problems)


def test_check_statement_judgments_reports_an_unjudged_candidate() -> None:
    document, candidates_value = judged_pair()
    document["statement_candidates"]["judgments"].pop()
    assert any(
        "S-2" in problem and "not judged" in problem for problem in _judgment_problems(document, candidates_value)
    )


def test_check_statement_judgments_reports_unknown_and_repeated_ids() -> None:
    document, candidates_value = judged_pair()
    document["statement_candidates"]["judgments"].append({"ids": ["S-9", "S-1"], "result": "unrelated", "basis": "x"})
    problems = _judgment_problems(document, candidates_value)
    assert any("S-9" in problem and "not in" in problem for problem in problems)
    assert any("S-1" in problem and "more than once" in problem for problem in problems)


def test_check_statement_judgments_reports_a_different_to() -> None:
    document, candidates_value = judged_pair()
    candidates_value["to"] = HEAD
    assert any("to" in problem for problem in _judgment_problems(document, candidates_value))


def test_check_statement_judgments_reports_a_different_from() -> None:
    document, candidates_value = judged_pair()
    candidates_value["from"] = BASE
    assert any("fix_check.from" in problem for problem in _judgment_problems(document, candidates_value))


def test_check_statement_judgments_requires_inconsistent_sites_in_findings() -> None:
    document, candidates_value = judged_pair()
    document["findings"][0]["statement_sites"] = []
    problems = _judgment_problems(document, candidates_value)
    assert any("S-1" in problem and "statement_sites" in problem for problem in problems)


def test_cli_validate_checks_the_fix_check_against_its_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document, candidates_value = judged_pair()
    verified_dir = tmp_path / "verified"
    _write(verified_dir / f"{FIX[:12]}.statement-candidates.json", candidates_value)
    good = _write(verified_dir / f"{FIX[:12]}.fix-check.json", document)
    assert review_findings.main(["validate", str(good)]) == 0
    document["statement_candidates"]["judgments"].pop()
    bad = _write(verified_dir / f"{FIX[:12]}.fix-check.json", document)
    assert review_findings.main(["validate", str(bad)]) == 65
    assert "not judged" in capsys.readouterr().err


def test_cli_validate_fix_check_without_candidates_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lone = _write(tmp_path / "verified" / f"{FIX[:12]}.fix-check.json", fix_check())
    assert review_findings.main(["validate", str(lone)]) == 66
    assert f"{FIX[:12]}.statement-candidates.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("factory", "mutation", "expected"),
    [
        (candidates, _set("review-findings/v999", "schema"), "schema: must be"),
        (candidates, _set("notes", "kind"), "kind: must be one of"),
        (candidates, _set("model", "source"), "source: must be one of"),
        (candidates, _drop("findings", 0, "property"), "findings[0].property: required"),
        (candidates, _drop("findings", 0, "variants"), "findings[0].variants: required"),
        (candidates, _drop("findings", 0, "statement_sites"), "findings[0].statement_sites: required"),
        (candidates, _drop("findings", 0, "failure_scenario"), "findings[0].failure_scenario: required"),
        (candidates, _drop("coverage", "declined"), "coverage.declined: required"),
        (candidates, _set("Severe", "findings", 0, "severity"), "findings[0].severity: must be one of"),
        (candidates, _set([], "findings", 0, "locations"), "findings[0].locations: must be a non-empty array"),
        (candidates, _set("maybe", "findings", 0, "variants", 0, "result"), "findings[0].variants[0].result"),
        (candidates, _set("abc", "reviewed_head"), "reviewed_head: must be"),
        (candidates, _set("x", "findings", 0, "extra"), "findings[0].extra: unknown key"),
        (candidates, _drop("findings", 0, "reproduction", "commands"), "findings[0].reproduction.commands: required"),
        (candidates, _set("", "skill", "sha256"), "skill.sha256: must be"),
        (verified, _drop("findings", 0, "reproduction"), "findings[0].reproduction: required when verdict is"),
        (verified, _set("not_reproduced", "findings", 0, "verdict"), "findings[0].reproduction: allowed only when"),
        (verified, _set([], "findings", 0, "sources"), "findings[0].sources: must be a non-empty array"),
        (verified, _set("H-1", "findings", 0, "id"), "findings[0].id: must be"),
        (verified, _set("pdf", "inputs", 0, "input_kind"), "inputs[0].input_kind: must be one of"),
        (verified, _set(None, "inputs", 0, "findings_file"), "inputs[0].findings_file: required when input_kind"),
        (verified, _set(["V-9"], "accuracy", "reviewers", 0, "unique"), "accuracy.reviewers[0].unique[0]"),
        (verified, _set(-1, "accuracy", "verify_seconds"), "accuracy.verify_seconds: must be"),
        (verified, _set({"from": HEAD, "to": FIX, "fix_ids": ["x"]}, "fix_check"), "fix_check: allowed only when"),
        (fix_check, _drop("fix_check"), "fix_check: required when mode is fix_check"),
        (fix_check, _set(HEAD, "fix_check", "to"), "fix_check.to: must equal reviewed_head"),
        (planning, _set("done", "outcome"), "outcome: must be one of"),
        (planning, _set("verified/x.json", "verified_file"), "verified_file: must be"),
        (planning, _set(["x"], "stages", "fix_check_seconds"), "stages.fix_check_seconds[0]: must be"),
    ],
)
def test_invalid_documents_name_the_problem(
    factory: Callable[[], dict[str, Any]], mutation: Mutation, expected: str
) -> None:
    value = factory()
    mutation(value)
    problems = problems_of(value)
    assert any(problem.startswith(expected) for problem in problems), problems


def test_unverified_verdict_requires_missing_checks() -> None:
    value = verified()
    value["findings"] = [verified_finding(verdict="unverified")]
    review_findings.validate_document(value)
    del value["findings"][0]["missing_checks"]
    assert "findings[0].missing_checks: required when verdict is unverified" in problems_of(value)


def test_missing_checks_only_for_unverified() -> None:
    value = verified()
    value["findings"][0]["missing_checks"] = "x"
    assert "findings[0].missing_checks: allowed only when verdict is unverified" in problems_of(value)


def test_failure_scenario_is_optional_for_documentation_findings() -> None:
    value = candidates()
    finding = value["findings"][0]
    finding["result_kind"] = {"value": "documentation", "detail": "README の説明が古い"}
    del finding["failure_scenario"]
    review_findings.validate_document(value)


def test_duplicate_ids_are_rejected() -> None:
    value = candidates()
    value["findings"].append(candidate_finding())
    assert "findings[1].id: duplicates findings[0].id" in problems_of(value)


def test_markdown_source_relaxes_reviewer_only_fields() -> None:
    value: dict[str, Any] = {
        "schema": "review-findings/v1",
        "kind": "candidates",
        "source": "markdown",
        "pr": {"repository": "usaturn/maatlog-poc", "number": 542},
        "reviewed_head": "unknown",
        "reviewer": {"model": "gpt-6.1-sol", "runtime": "unknown", "review_file": "gpt-6.1-sol.md"},
        "findings": [
            {
                "id": "M-1",
                "severity": "Medium",
                "title": "古い形式",
                "locations": ["不明"],
                "detected_head": "unknown",
                "raw": "- 内容: 何か",
            }
        ],
        "unverified_candidates": [],
    }
    review_findings.validate_document(value)


def test_not_a_json_object_is_rejected() -> None:
    assert problems_of([]) == ["document: must be an object"]


def test_control_characters_are_rejected_in_text() -> None:
    value = candidates()
    value["findings"][0]["title"] = "bad\x07title"
    assert any(problem.startswith("findings[0].title: must not contain control") for problem in problems_of(value))


# --- Markdown -------------------------------------------------------------------------------------------------

REVIEW_MD = f"""- 対象: PR #542 / head {HEAD[:12]} / 2026-10-04
- レビュアー: claude-opus-5-5（ランタイム: claude-code）

## Review history

| Reviewed at (UTC) | HEAD | Mode |
| --- | --- | --- |
| 2026-10-04T01:40:00Z | `{HEAD[:12]}` | Initial |

## Summary

総評。

## High

### H-1: リンクの箇条書きを checkbox と誤認する【検証済み】

- 位置: `tools/example.py:41`、`tools/example.py:77`
- 検出時 HEAD: `{HEAD[:12]}`
- 内容: 条件。

```markdown
### X-9: コードブロックの中の見出しは指摘ではない
```

## Medium

## Low

### L-2: ラベルの無い旧形式の指摘

- 位置: tools/other.py:3
- 検出時 HEAD: 不明（旧形式）

## Review coverage

- 読んだ範囲: 全体

## Unverified candidates

### U-1: 復旧の初期化に失敗したとき owner が残る疑い

- 位置: tools/recover.py:10

## Decided findings

### M-9: 判断済みの指摘は数えない

## Resolved findings

### H-0: 解消済みの指摘は数えない
"""


def test_parse_review_markdown_reads_active_findings_and_unverified_candidates() -> None:
    parsed = review_findings.parse_review_markdown(REVIEW_MD)
    assert [(item.id, item.severity) for item in parsed.findings] == [("H-1", "High"), ("L-2", "Low")]
    first, second = parsed.findings
    assert first.title == "リンクの箇条書きを checkbox と誤認する"
    assert first.confidence == "verified"
    assert first.locations_text == "`tools/example.py:41`、`tools/example.py:77`"
    assert first.detected_head == HEAD[:12]
    assert "X-9" in first.body
    assert second.confidence is None
    assert second.detected_head == "unknown"
    assert parsed.unverified == ["U-1"]
    assert parsed.reviewed_head == HEAD[:12]


def test_parse_review_markdown_tolerates_empty_and_missing_sections() -> None:
    parsed = review_findings.parse_review_markdown("## Summary\n\n指摘なし。\n")
    assert parsed.findings == []
    assert parsed.unverified == []
    assert parsed.reviewed_head == "unknown"


def test_candidates_from_markdown_is_a_valid_markdown_source_document() -> None:
    value = review_findings.candidates_from_markdown(
        REVIEW_MD,
        review_file="claude-opus-5-5.md",
        repository="usaturn/maatlog-poc",
        number=542,
        model="claude-opus-5-5",
        runtime="claude-code",
    )
    review_findings.validate_document(value)
    assert value["source"] == "markdown"
    assert value["reviewed_head"] == HEAD[:12]
    assert [item["id"] for item in value["findings"]] == ["H-1", "L-2"]
    assert value["findings"][0]["confidence"] == "verified"
    assert "confidence" not in value["findings"][1]
    assert value["unverified_candidates"] == [{"id": "U-1", "summary": "復旧の初期化に失敗したとき owner が残る疑い"}]


# --- check_pair -----------------------------------------------------------------------------------------------


def paired() -> dict[str, Any]:
    value = candidates()
    value["findings"][0]["locations"] = ["tools/example.py:41", "tools/example.py:77"]
    low = candidate_finding("L-2", "Low")
    low["locations"] = ["tools/other.py:3"]
    value["findings"].append(low)
    return value


def test_check_pair_accepts_a_matching_pair() -> None:
    review_findings.check_pair(REVIEW_MD, paired())


def _drop_last_finding(value: dict[str, Any]) -> None:
    value["findings"].pop()


def _add_unknown_finding(value: dict[str, Any]) -> None:
    value["findings"].append(candidate_finding("M-5", "Medium"))


def _change_severity(value: dict[str, Any]) -> None:
    value["findings"][0]["severity"] = "Medium"


def _move_location(value: dict[str, Any]) -> None:
    value["findings"][1]["locations"] = ["tools/missing.py:1"]


def _drop_unverified(value: dict[str, Any]) -> None:
    value["unverified_candidates"] = []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (_drop_last_finding, "findings: L-2 is in the Markdown but not in the JSON"),
        (_add_unknown_finding, "findings: M-5 is in the JSON but not in the Markdown"),
        (_change_severity, "findings[0].severity: the Markdown says High"),
        (_move_location, "findings[1].locations[0]: not in the Markdown"),
        (_drop_unverified, "unverified_candidates: U-1 is in the Markdown"),
    ],
)
def test_check_pair_names_each_mismatch(mutation: Mutation, expected: str) -> None:
    value = paired()
    mutation(value)
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.check_pair(REVIEW_MD, value)
    assert any(problem.startswith(expected) for problem in caught.value.problems), caught.value.problems


# --- stats ----------------------------------------------------------------------------------------------------


def test_stats_reports_false_positives_misses_late_detections_and_severity_drift() -> None:
    first = verified()
    first["findings"][0]["late_detection"] = True
    rejected = verified_finding("V-2", "not_reproduced")
    rejected["sources"] = [f"gpt-6.1-sol#L-1@{HEAD[:12]}"]
    rejected["reported_severities"] = [{"source": f"gpt-6.1-sol#L-1@{HEAD[:12]}", "severity": "Low"}]
    first["findings"].append(rejected)
    reviewers = first["accuracy"]["reviewers"]
    reviewers[1].update(candidates=2, not_reproduced=1)
    reviewers[0].update(missed=0)
    second = verified()
    second["reviewed_head"] = FIX
    only = verified_finding("V-1")
    only["sources"] = [f"claude-opus-5-5#M-1@{FIX[:12]}"]
    only["reported_severities"] = [{"source": f"claude-opus-5-5#M-1@{FIX[:12]}", "severity": "Medium"}]
    second["findings"] = [only]
    second_reviewers = second["accuracy"]["reviewers"]
    second_reviewers[0].update(unique=["V-1"])
    second_reviewers[1].update(confirmed=0, candidates=0, missed=1)
    ignored = fix_check()
    records = {
        "decisions": [
            {
                "id": f"claude-opus-5-5#H-1@{HEAD[:12]}",
                "sources": [f"claude-opus-5-5#H-1@{HEAD[:12]}", f"gpt-6.1-sol#M-2@{HEAD[:12]}"],
                "severity": "High",
                "state": "fix",
            },
        ]
    }

    result = review_findings.stats([first, second, ignored], records)

    by_model = {item["model"]: item for item in result["reviewers"]}
    claude = by_model["claude-opus-5-5"]
    gpt = by_model["gpt-6.1-sol"]
    assert (claude["candidates"], claude["confirmed"], claude["not_reproduced"], claude["missed"]) == (2, 2, 0, 0)
    assert claude["false_positive_rate"] == 0.0
    assert claude["late_detections"] == 1
    assert (gpt["candidates"], gpt["confirmed"], gpt["not_reproduced"], gpt["missed"]) == (2, 1, 1, 1)
    assert gpt["false_positive_rate"] == 0.5
    assert gpt["severity_drift"] == 1
    assert result["documents"] == 2
    assert result["severity_drift"] == [
        {"source": f"gpt-6.1-sol#M-2@{HEAD[:12]}", "reported": "Medium", "decided": "High"}
    ]


def test_reference_key_drops_the_markdown_suffix() -> None:
    assert review_findings.reference_key(f"gpt-6.1-sol.md#M-2@{HEAD[:12]}") == f"gpt-6.1-sol#M-2@{HEAD[:12]}"
    assert review_findings.reference_key(f"gpt-6.1-sol#M-2@{HEAD[:12]}") == f"gpt-6.1-sol#M-2@{HEAD[:12]}"


@pytest.mark.parametrize("suffix", ["", ".md"])
def test_stats_counts_severity_drift_with_either_reference_style(suffix: str) -> None:
    document = verified()
    finding = document["findings"][0]
    finding["sources"] = [f"gpt-6.1-sol{suffix}#M-2@{HEAD[:12]}"]
    finding["reported_severities"] = [{"source": f"gpt-6.1-sol{suffix}#M-2@{HEAD[:12]}", "severity": "Medium"}]
    records = {
        "decisions": [
            {
                "id": f"gpt-6.1-sol#M-2@{HEAD[:12]}",
                "sources": [f"gpt-6.1-sol#M-2@{HEAD[:12]}"],
                "severity": "High",
                "state": "fix",
            }
        ]
    }

    result = review_findings.stats([document], records)

    by_model = {item["model"]: item for item in result["reviewers"]}
    assert by_model["gpt-6.1-sol"]["severity_drift"] == 1
    assert len(result["severity_drift"]) == 1


def test_stats_without_any_cycle_document_reports_nothing() -> None:
    result = review_findings.stats([fix_check()], None)
    assert result["documents"] == 0
    assert result["reviewers"] == []
    assert result["severity_drift"] == []


def adoption_records(document: dict[str, Any]) -> dict[str, Any]:
    sources = document["findings"][0]["sources"]
    return {
        "pr": document["pr"],
        "decisions": [
            {
                "id": sources[0] + "/a",
                "sources": sources,
                "state": "resolved",
                "severity": "High",
                "history": [{"state": "fix"}, {"state": "resolved"}],
            },
            {"id": sources[0] + "/b", "sources": sources, "state": "skip", "severity": "High", "history": []},
        ],
    }


def test_provenance_stats_retains_adoption_after_resolution_without_counting_split_sources_twice() -> None:
    document = document_v2()
    result = review_findings.stats([document], adoption_records(document))["provenance"]
    finders = [entry for entry in result["executions"] if entry["provenance"]["role"] == "find"]
    assert [entry["adopted"] for entry in finders] == [1, 1]
    assert [entry["confirmed"] for entry in finders] == [1, 1]
    assert result["by_dimension"]["effort"][0]["adopted"] == 2


@pytest.mark.parametrize(
    "axis,new_value",
    [
        ("effort", "low"),
        ("agent_id", "other-agent"),
        ("session_id", "other-session"),
        ("cli_version", "2.0"),
        ("provider", "other-provider"),
        ("run_id", "other-run"),
        ("request_id", "other-request"),
    ],
)
def test_provenance_stats_separates_same_model_with_different_observations(axis: str, new_value: str) -> None:
    first = document_v2()
    second = document_v2()
    second["inputs"][0]["provenance"][axis] = new_value
    second["accuracy"]["reviewers"][0]["provenance"][axis] = new_value
    result = review_findings.stats([first, second], None)["provenance"]
    finders = [
        entry
        for entry in result["executions"]
        if entry["model"] == "claude-opus-5-5" and entry["provenance"]["role"] == "find"
    ]
    assert len(finders) == 2
    assert {entry["provenance"][axis] for entry in finders} == {first["inputs"][0]["provenance"][axis], new_value}
    assert sum(entry["candidates"] for entry in finders) == 2


def test_provenance_stats_separates_skill_revisions() -> None:
    first = document_v2()
    second = document_v2()
    for entry in (second["inputs"][0], second["accuracy"]["reviewers"][0]):
        entry["provenance"]["skills"][0]["sha256"] = "f" * 64
    result = review_findings.stats([first, second], None)["provenance"]
    finders = [
        entry
        for entry in result["executions"]
        if entry["model"] == "claude-opus-5-5" and entry["provenance"]["role"] == "find"
    ]
    assert len(finders) == 2
    assert len(result["by_dimension"]["skill"]) == 3  # two discovery revisions and the verifier skill


def test_provenance_stats_does_not_apply_other_pr_or_head_decisions() -> None:
    document = document_v2()
    records = adoption_records(document)
    records["pr"] = {"repository": "other/repo", "number": document["pr"]["number"]}
    result = review_findings.stats([document], records)
    assert result["severity_drift"] == []
    finders = [entry for entry in result["provenance"]["executions"] if entry["provenance"]["role"] == "find"]
    assert all(entry["adopted"] is None for entry in finders)
    records = adoption_records(document)
    for decision in records["decisions"]:
        decision["sources"] = [source.replace(HEAD[:12], "000000000000") for source in decision["sources"]]
    finders = [
        entry
        for entry in review_findings.stats([document], records)["provenance"]["executions"]
        if entry["provenance"]["role"] == "find"
    ]
    assert all(entry["adopted"] == 0 for entry in finders)


def v2_adoption_round(document: dict[str, Any]) -> dict[str, Any]:
    sources = document["findings"][0]["sources"]
    record: dict[str, Any] = {
        "schema": "review-records/v2",
        "pr": document["pr"],
        "round": 1,
        "reviewed_head": HEAD,
        "recorded_at": "2026-10-04T03:00:00Z",
        "planner": {
            "model": "example-model",
            "runtime": "pi",
            "agent_id": "example-model",
            "provenance": provenance(),
        },
        "seats": [
            {
                "review_file": entry["review_file"].removesuffix(".md"),
                "model": entry["model"],
                "runtime": entry["runtime"],
                "provenance": copy.deepcopy(entry["provenance"]),
                "file_head": HEAD[:12],
                "mode": "Initial",
                "rerun": True,
                "merge_recommendation": "Approve",
            }
            for entry in document["inputs"]
        ],
        "decisions": [
            {
                "id": sources[0],
                "sources": sources,
                "summary": "Fictional defect",
                "locations": ["example.py:1"],
                "state": "fix",
                "change": "new",
                "severity": "High",
                "pr_relation": "introduced",
                "reason": "Fictional reproduction",
            }
        ],
        "outcome": "fixes_pushed",
        "commit": FIX,
    }
    return record


def test_provenance_stats_adopts_a_finding_fixed_in_a_later_round() -> None:
    """継続指摘を後の巡で初めて Fix にしても、同一実担当なら採用が残る。"""
    document = document_v2()
    first = v2_adoption_round(document)
    held = copy.deepcopy(first)
    held.update(outcome="stopped_insufficient_verification", stop_reason="No reproduction yet")
    del held["commit"]
    held["decisions"][0].update(state="unverified", unverified_reason="No reproduction yet")
    late = copy.deepcopy(first)
    late.update(round=2, reviewed_head=FIX, commit="a" * 40, recorded_at="2026-10-04T04:00:00Z")
    for seat in late["seats"]:
        seat["file_head"] = FIX[:12]
    late["decisions"][0].update(change="changed", previous_state="unverified")
    records = review_findings._records.fold(
        [
            (123, review_findings._records.validate_record(held)),
            (124, review_findings._records.validate_record(late)),
        ]
    )
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [1, 1]


def test_provenance_stats_does_not_credit_a_later_round_from_another_session() -> None:
    """Fix を決めた巡の同席が別セッションなら、採用は未採用（0）として評価される。"""
    document = document_v2()
    first = v2_adoption_round(document)
    held = copy.deepcopy(first)
    held.update(outcome="stopped_insufficient_verification", stop_reason="No reproduction yet")
    del held["commit"]
    held["decisions"][0].update(state="unverified", unverified_reason="No reproduction yet")
    late = copy.deepcopy(first)
    late.update(round=2, reviewed_head=FIX, commit="a" * 40, recorded_at="2026-10-04T04:00:00Z")
    for seat in late["seats"]:
        seat["file_head"] = FIX[:12]
        seat["provenance"]["session_id"] = "another-session"
    late["decisions"][0].update(change="changed", previous_state="unverified")
    records = review_findings._records.fold(
        [
            (123, review_findings._records.validate_record(held)),
            (124, review_findings._records.validate_record(late)),
        ]
    )
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [0, 0]


def test_provenance_stats_matches_v2_adoption_to_the_original_session() -> None:
    first = document_v2()
    second = document_v2()
    second["inputs"][0]["provenance"]["session_id"] = "unadopted-session"
    second["accuracy"]["reviewers"][0]["provenance"]["session_id"] = "unadopted-session"
    record = v2_adoption_round(first)
    resolved = copy.deepcopy(record)
    resolved["round"] = 2
    resolved["recorded_at"] = "2026-10-04T04:00:00Z"
    resolved["seats"][0]["provenance"]["session_id"] = "unadopted-session"
    resolved["decisions"][0].update(state="resolved", change="changed", previous_state="fix")
    resolved["outcome"] = "no_fix_needed"
    del resolved["commit"]
    records = review_findings._records.fold(
        [
            (123, review_findings._records.validate_record(record)),
            (124, review_findings._records.validate_record(resolved)),
        ]
    )
    rows = review_findings.stats([first, second], records)["provenance"]["executions"]
    finders = [row for row in rows if row["model"] == "claude-opus-5-5" and row["stage"] == "find"]
    assert {row["provenance"]["session_id"]: row["adopted"] for row in finders} == {
        "example-session": 1,
        "unadopted-session": 0,
    }


def test_provenance_stats_leaves_adoption_unevaluated_when_identifiers_are_unobserved() -> None:
    """文書と採用照合の双方で実行識別子が全て未観測なら、採用は確定できない。"""
    document = document_v2()
    for entry in document["inputs"]:
        entry["provenance"].update(session_id="unknown", run_id="unknown", request_id="unknown")
    records = review_findings._records.fold(
        [(123, review_findings._records.validate_record(v2_adoption_round(document)))]
    )
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [None, None]


def test_provenance_stats_leaves_adoption_unevaluated_when_only_the_document_row_is_unobserved() -> None:
    """実担当の巡は観測済みでも、発見行の識別子が全て未観測なら採用は確定できない。"""
    document = document_v2()
    for entry in document["inputs"]:
        entry["provenance"].update(session_id="unknown", run_id="unknown", request_id="unknown")
    record = v2_adoption_round(document)
    for seat in record["seats"]:
        seat["provenance"] = provenance(seat["model"], "find")
    records = review_findings._records.fold([(123, review_findings._records.validate_record(record))])
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [None, None]


def test_provenance_stats_leaves_adoption_unevaluated_when_the_fixing_seat_is_unobserved() -> None:
    """実担当が観測済みでも、決めた巡の同席の実行識別子が全て未観測なら採用は確定できない。"""
    document = document_v2()
    record = v2_adoption_round(document)
    for seat in record["seats"]:
        seat["provenance"].update(session_id="unknown", run_id="unknown", request_id="unknown")
    records = review_findings._records.fold([(123, review_findings._records.validate_record(record))])
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [None, None]


@pytest.mark.parametrize("known", ["session_id", "run_id", "request_id"])
def test_provenance_stats_adopts_with_at_least_one_observed_identifier(known: str) -> None:
    """1つでも観測済みの識別子が一致すれば、辞書一致の採用は維持する。"""
    document = document_v2()
    unobserved = {key: "unknown" for key in ("session_id", "run_id", "request_id") if key != known}
    for entry in document["inputs"]:
        entry["provenance"].update(**unobserved)
    records = review_findings._records.fold(
        [(123, review_findings._records.validate_record(v2_adoption_round(document)))]
    )
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [1, 1]


def test_provenance_stats_keeps_a_matched_seat_when_another_round_is_unobserved() -> None:
    """観測済みの一致する実担当がいれば、後の巡の未観測同席に採用を覆されない。"""
    document = document_v2()
    first = v2_adoption_round(document)
    resolved = copy.deepcopy(first)
    resolved.update(round=2, reviewed_head=FIX, recorded_at="2026-10-04T04:00:00Z")
    del resolved["commit"]
    resolved["outcome"] = "no_fix_needed"
    for seat in resolved["seats"]:
        seat["file_head"] = FIX[:12]
        seat["mode"] = "Update"
    resolved["decisions"][0].update(state="resolved", change="changed", previous_state="fix")
    unobserved = copy.deepcopy(first)
    unobserved.update(round=3, reviewed_head=FIX, commit="a" * 40, recorded_at="2026-10-04T05:00:00Z")
    for seat in unobserved["seats"]:
        seat["file_head"] = FIX[:12]
        seat["mode"] = "Update"
        seat["provenance"].update(session_id="unknown", run_id="unknown", request_id="unknown")
    unobserved["decisions"][0].update(state="fix", change="changed", previous_state="resolved")
    records_input: list[tuple[int, dict[str, Any]]] = []
    prior: dict[str, Any] = {
        "schema": "review-records/v1",
        "pr": None,
        "rounds": [],
        "decisions": [],
        "ignored_comments": [],
    }
    records = prior
    for comment_id, record in ((123, first), (124, resolved), (125, unobserved)):
        record = review_findings._records.validate_record(record)
        review_findings._records.check_against(record, records)
        records_input.append((comment_id, record))
        records = review_findings._records.fold(list(records_input))
    rows = review_findings.stats([document], records)["provenance"]["executions"]
    finders = [row for row in rows if row["stage"] == "find"]
    assert [row["adopted"] for row in finders] == [1, 1]


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"decisions": "bad"},
        {"schema": "review-records/v999", "decisions": []},
        {"decisions": [{"sources": [f"example#H-1@{HEAD[:12]}"], "severity": "Blocker"}]},
        {"decisions": [{"sources": [1]}]},
        {"decisions": [{"sources": [f"example#H-1@{HEAD[:12]}"], "history": [None]}]},
    ],
)
def test_stats_rejects_malformed_folded_records_without_a_traceback(
    bad: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review_dir = tmp_path / "review"
    _write(review_dir / "verified" / f"{HEAD[:12]}.json", document_v2())
    records_path = _write(tmp_path / "records.json", bad)
    assert review_findings.main(["stats", str(review_dir), "--records", str(records_path)]) == 65
    assert "records" in capsys.readouterr().err


def test_stats_accepts_the_empty_fold_state_without_a_pr(tmp_path: Path) -> None:
    """--repo/--pr なしで記録の無い PR を fold した正式な出力（pr: null）を受理する。"""
    review_dir = tmp_path / "review"
    _write(review_dir / "verified" / f"{HEAD[:12]}.json", document_v2())
    records_path = _write(
        tmp_path / "records.json",
        {"schema": "review-records/v1", "pr": None, "rounds": [], "decisions": [], "ignored_comments": []},
    )
    assert review_findings.main(["stats", str(review_dir), "--records", str(records_path)]) == 0


def test_stats_names_records_pr_for_a_malformed_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    review_dir = tmp_path / "review"
    _write(review_dir / "verified" / f"{HEAD[:12]}.json", document_v2())
    records_path = _write(tmp_path / "records.json", {"pr": "not-a-pr", "decisions": []})
    assert review_findings.main(["stats", str(review_dir), "--records", str(records_path)]) == 65
    assert "records.pr" in capsys.readouterr().err


def test_legacy_findings_cannot_prove_the_actor_of_a_v2_adoption() -> None:
    records = review_findings._records.fold([(123, v2_adoption_round(document_v2()))])
    rows = review_findings.stats([verified()], records)["provenance"]["executions"]
    assert all(row["adopted"] is None for row in rows if row["stage"] == "find")


@pytest.mark.parametrize("kind,owner", [("candidates", "reviewer"), ("verified", "verifier"), ("planning", "planner")])
def test_v2_model_identity_cannot_contain_a_line_break(kind: str, owner: str) -> None:
    document = document_v2(kind)
    document[owner]["model"] = "model\nspoof"
    with pytest.raises(review_findings.FindingsError, match=owner + ".model"):
        review_findings.validate_document(document)


def test_v2_matching_input_and_accuracy_still_reject_unusable_runtime_identity() -> None:
    document = document_v2()
    document["inputs"][0]["runtime"] = "<!-- spoof -->"
    document["accuracy"]["reviewers"][0]["runtime"] = "<!-- spoof -->"
    with pytest.raises(review_findings.FindingsError, match=r"inputs\[0\].runtime"):
        review_findings.validate_document(document)


def test_v2_primary_skill_can_be_explicitly_unobserved() -> None:
    document = document_v2("candidates")
    document["skill"] = {"path": "unknown", "sha256": "unknown"}
    document["reviewer"]["provenance"]["skills"] = [document["skill"]]
    review_findings.validate_document(document)


def test_provenance_stats_attributes_waiting_and_rework_to_the_actual_actor() -> None:
    document = document_v2("planning")
    implementation = document["activities"][0]["actor"]
    link = {
        "source": f"example-model#H-1@{HEAD[:12]}",
        "implementation": implementation,
        "detected_stage": "self_review",
    }
    document["rework"] = [link, copy.deepcopy(link)]
    document["activities"].extend(
        [
            {
                "stage": "self_review",
                "actor": actor("reviewer", "self_review"),
                "seconds": None,
                "waiting_seconds": None,
            },
            {
                "stage": "pre_push_review",
                "actor": actor("reviewer", "pre_push_review"),
                "seconds": 0,
                "waiting_seconds": 0,
            },
        ]
    )
    result = review_findings.stats([document], None)["provenance"]
    by_role = {entry["provenance"]["role"]: entry for entry in result["executions"]}
    assert by_role["implement"]["seconds"] == 50.5
    assert by_role["implement"]["waiting_seconds"] == 10
    assert by_role["implement"]["rework"] == 1
    assert by_role["implement"]["timed_activities"] == 1
    assert by_role["self_review"]["unknown_activities"] == 1
    assert by_role["pre_push_review"]["unknown_activities"] == 0
    assert by_role["pre_push_review"]["timed_activities"] == 1
    assert result["by_dimension"]["agent_id"][0]["value"] == "reviewer"


def test_provenance_stats_uses_planning_time_instead_of_counting_the_same_find_stage_twice() -> None:
    verified_doc = document_v2()
    planning_doc = document_v2("planning")
    input_actor = {key: verified_doc["inputs"][0][key] for key in ("model", "runtime", "provenance")}
    planning_doc["activities"] = [{"stage": "find", "actor": input_actor, "seconds": 2400, "waiting_seconds": 10}]
    result = review_findings.stats([verified_doc, planning_doc], None)["provenance"]
    row = next(
        entry
        for entry in result["executions"]
        if entry["model"] == "claude-opus-5-5" and entry["provenance"]["role"] == "find"
    )
    assert row["seconds"] == 2400
    assert row["waiting_seconds"] == 10
    assert row["timed_activities"] == 1


def test_provenance_stats_keeps_stage_separate_from_generic_runner_role() -> None:
    document = document_v2("planning")
    generic_actor = actor("shared-agent", "review")
    document["activities"] = [
        {"stage": "self_review", "actor": generic_actor, "seconds": 20, "waiting_seconds": 0},
        {"stage": "fix_check", "actor": generic_actor, "seconds": 30, "waiting_seconds": 2},
    ]
    rows = review_findings.stats([document], None)["provenance"]["executions"]
    assert {row["stage"]: row["seconds"] for row in rows} == {"self_review": 20, "fix_check": 30}
    assert all(row["provenance"]["role"] == "review" for row in rows)


def test_provenance_stats_ignores_skill_array_order_without_mutating_documents() -> None:
    first = document_v2()
    for entry in (first["inputs"][0], first["accuracy"]["reviewers"][0]):
        entry["provenance"]["skills"].append({"path": "other/SKILL.md", "sha256": "f" * 64})
    second = copy.deepcopy(first)
    for entry in (second["inputs"][0], second["accuracy"]["reviewers"][0]):
        entry["provenance"]["skills"].reverse()
    before = copy.deepcopy([first, second])
    rows = review_findings.stats([first, second], None)["provenance"]["executions"]
    finders = [row for row in rows if row["model"] == "claude-opus-5-5" and row["provenance"]["role"] == "find"]
    assert len(finders) == 1
    assert finders[0]["candidates"] == 2
    assert [first, second] == before


def test_provenance_stats_marks_legacy_identity_and_unmeasured_time_unknown() -> None:
    document = verified()
    document["accuracy"]["find_seconds"]["claude-opus-5-5.md"] = None
    result = review_findings.stats([document], None)["provenance"]
    entry = next(
        item
        for item in result["executions"]
        if item["model"] == "claude-opus-5-5" and item["provenance"]["role"] == "find"
    )
    assert entry["provenance"]["session_id"] == "unknown"
    assert entry["provenance"]["effort"] == "unknown"
    assert entry["seconds"] == 0
    assert entry["unknown_activities"] == 1
    assert entry["adopted"] is None


def test_standalone_stats_reads_v2_planning_and_rejects_its_bad_duration(tmp_path: Path) -> None:
    review_dir = tmp_path / "review"
    path = _write(review_dir / "verified" / f"{HEAD[:12]}.planning.json", document_v2("planning"))
    result = subprocess.run(
        ["uv", "run", "--no-project", "python", str(SCRIPT), "stats", str(review_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["provenance"]["executions"][0]["seconds"] == 50.5
    bad = document_v2("planning")
    bad["activities"][0]["seconds"] = -1
    _write(path, bad)
    result = subprocess.run(
        ["uv", "run", "--no-project", "python", str(SCRIPT), "stats", str(review_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 65
    assert str(path) in result.stderr


# --- CLI ------------------------------------------------------------------------------------------------------


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_validate_reports_problems_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _write(tmp_path / "good.json", candidates())
    assert review_findings.main(["validate", str(good)]) == 0
    bad_value = candidates()
    del bad_value["findings"][0]["property"]
    bad = _write(tmp_path / "bad.json", bad_value)
    assert review_findings.main(["validate", str(bad)]) == 65
    assert "findings[0].property: required" in capsys.readouterr().err


def test_cli_validate_reports_unreadable_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert review_findings.main(["validate", str(tmp_path / "missing.json")]) == 66
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert review_findings.main(["validate", str(broken)]) == 65
    assert "invalid JSON" in capsys.readouterr().err


def test_cli_from_markdown_and_check_pair(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    markdown = tmp_path / "claude-opus-5-5.md"
    markdown.write_text(REVIEW_MD, encoding="utf-8")
    assert (
        review_findings.main(
            [
                "from-markdown",
                str(markdown),
                "--review-file",
                "claude-opus-5-5.md",
                "--repo",
                "usaturn/maatlog-poc",
                "--pr",
                "542",
                "--model",
                "claude-opus-5-5",
                "--runtime",
                "claude-code",
            ]
        )
        == 0
    )
    converted = json.loads(capsys.readouterr().out)
    assert converted["source"] == "markdown"
    pair = _write(tmp_path / "pair.json", paired())
    assert review_findings.main(["check-pair", str(markdown), str(pair)]) == 0
    mismatched = paired()
    mismatched["findings"].pop()
    other = _write(tmp_path / "other.json", mismatched)
    assert review_findings.main(["check-pair", str(markdown), str(other)]) == 65


def test_cli_stats_reads_cycle_documents_under_verified(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    review_dir = tmp_path / "#542-feat-x"
    _write(review_dir / "verified" / f"{HEAD[:12]}.json", verified())
    _write(review_dir / "verified" / f"{FIX[:12]}.fix-check.json", fix_check())
    _write(review_dir / "verified" / f"{HEAD[:12]}.planning.json", planning())
    assert review_findings.main(["stats", str(review_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 1


def test_cli_stats_skips_lists_other_than_cycle_even_if_they_lack_newer_sections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review_dir = tmp_path / "#542-feat-x"
    _write(review_dir / "verified" / f"{HEAD[:12]}.json", verified())
    stage2 = fix_check()
    del stage2["statement_candidates"]
    _write(review_dir / "verified" / f"{FIX[:12]}.fix-check.json", stage2)
    _write(review_dir / "verified" / f"{FIX[:12]}.statement-candidates.json", statement_candidates_document())
    assert review_findings.main(["stats", str(review_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 1


def test_cli_stats_names_the_file_of_an_invalid_cycle_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    review_dir = tmp_path / "#542-feat-x"
    broken = verified()
    del broken["reviewed_head"]
    path = _write(review_dir / "verified" / f"{HEAD[:12]}.json", broken)
    assert review_findings.main(["stats", str(review_dir)]) == 65
    assert str(path) in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutation",
    [_drop("kind"), _drop("mode"), _set("verifed", "kind"), _set("cylce", "mode")],
    ids=["kind-missing", "mode-missing", "kind-misspelled", "mode-misspelled"],
)
def test_cli_stats_stops_at_a_list_whose_kind_or_mode_is_unknown(
    mutation: Mutation, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review_dir = tmp_path / "#542-feat-x"
    _write(review_dir / "verified" / f"{FIX[:12]}.fix-check.json", fix_check())
    broken = verified()
    mutation(broken)
    path = _write(review_dir / "verified" / f"{HEAD[:12]}.json", broken)
    assert review_findings.main(["stats", str(review_dir)]) == 65
    assert str(path) in capsys.readouterr().err


# --- 実行記録（kind: review_run） --------------------------------------------------------------------------------


def run_document(status: str = "success", failed_at: str | None = None) -> dict[str, Any]:
    """記入例（success）を元に、指定した状態の実行記録を作る。"""
    document = cast(dict[str, Any], copy.deepcopy(_examples()["review_run"]))
    timing = document["timing"]
    if status == "started":
        document.update(status="started", failed_at=None, reason=None, outputs=None, runner=None)
        timing.update(
            finished_at=None, review_seconds=None, save_seconds=None, cleanup_seconds=None, total_seconds=None
        )
    elif status == "failed":
        document.update(status="failed", failed_at=failed_at, reason="レビューの途中で止めた")
        if failed_at != "cleanup":
            document["outputs"] = None
    return document


@pytest.mark.parametrize(
    ("status", "failed_at"),
    [("started", None), ("success", None), ("failed", "prepare"), ("failed", "review"), ("failed", "cleanup")],
)
def test_review_run_accepts_each_status(status: str, failed_at: str | None) -> None:
    review_findings.validate_document(run_document(status, failed_at))


def test_review_run_started_before_prepare_finishes_may_lack_inputs() -> None:
    document = run_document("started")
    document.update(mode=None, base=None, inputs=None)
    document["timing"]["prepare_seconds"] = None
    review_findings.validate_document(document)


def test_review_run_failed_in_prepare_may_lack_inputs() -> None:
    document = run_document("failed", "prepare")
    document.update(mode=None, base=None, inputs=None)
    review_findings.validate_document(document)


def _assign(document: dict[str, Any], dotted: str, value: object) -> None:
    *parents, key = dotted.split(".")
    target = document
    for name in parents:
        target = target[name]
    target[key] = value


@pytest.mark.parametrize(
    ("changes", "where"),
    [
        ([("outputs", None)], "outputs: required when status is success"),
        ([("status", "started")], "timing.finished_at: must be null when status is started"),
        ([("status", "failed"), ("reason", "x")], "failed_at: required when status is failed"),
        ([("status", "failed"), ("failed_at", "review"), ("reason", "x")], "outputs: must be null"),
        (
            [("status", "failed"), ("failed_at", "complete"), ("reason", "x"), ("outputs", None), ("inputs", None)],
            "inputs: required when status is failed",
        ),
        ([("outputs.by_severity.high", 5)], "outputs.by_severity: must add up to active_findings"),
        ([("schema", "review-findings/v1")], "schema: review_run requires review-findings/v2"),
        ([("runner.attempt.state", "finished")], "runner.attempt.state"),
        ([("timing.finished_at", "2026-10-07T00:00:00Z")], "timing.finished_at: must not precede"),
        ([("inputs.records.decisions.adopt", 1)], "inputs.records.decisions.adopt"),
        ([("inputs.request.deadline_seconds", 0)], "inputs.request.deadline_seconds"),
        ([("outputs.findings_file", "other/x.json")], "outputs.findings_file"),
    ],
)
def test_review_run_rejects_inconsistent_fields(changes: list[tuple[str, object]], where: str) -> None:
    document = run_document()
    for dotted, value in changes:
        _assign(document, dotted, value)
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.validate_document(document)
    assert any(where in problem for problem in caught.value.problems), caught.value.problems


def test_run_stats_counts_completion_failures_timeouts_and_time_per_line() -> None:
    success = run_document()
    failed = run_document("failed", "review")
    timed_out = run_document("started")
    timed_out["runner"] = copy.deepcopy(success["runner"])
    timed_out["runner"]["attempt"]["state"] = "timed_out"
    incomplete = run_document("started")
    result = review_findings.stats([], None, [success, failed, timed_out, incomplete])["runs"]
    total = result["total"]
    assert result["documents"] == 4
    assert (total["runs"], total["success"], total["failed"], total["timed_out"], total["incomplete"]) == (
        4,
        1,
        1,
        1,
        1,
    )
    assert total["completion_rate"] == 0.25
    assert total["failed_at"] == {"prepare": 0, "review": 1, "complete": 0, "save": 0, "cleanup": 0}
    assert total["seconds"]["prepare"] == {"total": pytest.approx(8.1 * 4), "measured": 4}
    assert total["seconds"]["save"]["measured"] == 2
    assert total["seconds"]["total"]["measured"] == 2
    # review_seconds と diff のそろった success・failed の 2 件（各 120 + 30 行、540.2 秒）
    assert total["changed_lines"] == 300
    assert total["review_seconds_per_changed_line"] == pytest.approx(540.2 * 2 / 300)
    by_dimension = result["by_dimension"]
    assert {row["value"]: row["runs"] for row in by_dimension["runner_state"]} == {
        "success": 2,
        "timed_out": 1,
        "unattached": 1,
    }
    assert {row["value"] for row in by_dimension["runner_effort"]} == {"high", "unattached"}
    assert by_dimension["effort"][0]["value"] == "unknown"
    assert by_dimension["model"][0]["completion_rate"] == 0.25
    skill = by_dimension["skill"][0]
    assert skill["value"].startswith(".agents/skills/pr-review/SKILL.md@") and skill["runs"] == 4


def test_runs_do_not_change_the_existing_stats_keys() -> None:
    plain = review_findings.stats([verified()], None)
    with_runs = review_findings.stats([verified()], None, [run_document()])
    assert {key: plain[key] for key in plain if key != "runs"} == {
        key: with_runs[key] for key in plain if key != "runs"
    }
    assert plain["runs"] == {"documents": 0, "total": plain["runs"]["total"], "by_dimension": {}}
    assert plain["runs"]["total"]["completion_rate"] is None
    assert plain["runs"]["total"]["review_seconds_per_changed_line"] is None


def test_stats_cli_reads_runs_and_rejects_bad_ones(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    review_dir = tmp_path / "review"
    path = _write(review_dir / "runs" / "20261008T000000Z-example-model-0123abcd.json", run_document())
    assert review_findings.main(["stats", str(review_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["runs"]["documents"] == 1
    _write(path, {**run_document(), "status": "unknown"})
    assert review_findings.main(["stats", str(review_dir)]) == 65
    assert str(path) in capsys.readouterr().err
    _write(path, document_v2("planning"))
    assert review_findings.main(["stats", str(review_dir)]) == 65
    assert "runs/ holds only review_run documents" in capsys.readouterr().err


# --- 文書 -----------------------------------------------------------------------------------------------------


def _examples() -> dict[str, object]:
    text = FORMAT_DOC.read_text(encoding="utf-8")
    found = re.findall(r"<!-- review-findings:example (\w+) -->\n+```json\n(.*?)\n```", text, re.DOTALL)
    return {kind: json.loads(body) for kind, body in found}


def test_v2_planning_example_is_accepted_and_its_activity_is_aggregated() -> None:
    text = FORMAT_DOC.read_text(encoding="utf-8")
    match = re.search(r"<!-- review-provenance:example planning_v2 -->\n```json\n(.*?)\n```", text, re.DOTALL)
    assert match is not None
    value = review_findings.validate_document(json.loads(match[1]))
    row = review_findings.stats([value], None)["provenance"]["executions"][0]
    assert row["stage"] == "implement"
    assert row["seconds"] == 12
    assert row["waiting_seconds"] == 2
    assert row["provenance"]["agent_id"] == "self"


def test_format_document_examples_are_valid() -> None:
    examples = _examples()
    assert set(examples) == {"candidates", "verified", "fix_check", "planning", "statement_candidates", "review_run"}
    for example in examples.values():
        review_findings.validate_document(example)


def test_format_document_commands_are_accepted_by_the_cli() -> None:
    # 文書の bash ブロックにある review_findings.py の呼び出しを、CLI の引数解析にかける。
    # <...> は値の例に置き換え、[...] の省略できる引数も付けて確かめる。
    text = FORMAT_DOC.read_text(encoding="utf-8")
    lines = [
        line
        for block in re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
        for line in block.replace("\\\n", " ").splitlines()
    ]
    invocations = [line.split('review_findings.py"', 1)[1] for line in lines if 'review_findings.py"' in line]
    assert invocations
    for invocation in invocations:
        filled = re.sub(r"\[([^\]]*)\]", r"\1", re.sub(r"<[^>]+>", "1", invocation)).replace("...", "")
        argv = shlex.split(filled)
        try:
            review_findings._parser().parse_args(argv)
        except SystemExit:
            pytest.fail(f"review-findings.md のコマンドを CLI が受け付けない: {argv}")


# --- 最終レビュー I-1: 型どおりでない指摘を黙って落とさない ------------------------------------------------------


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("## High\n\n### GraphQL の型エラー\n\n- 位置: a.py:1\n", "### GraphQL の型エラー"),
        ("## Active findings\n\n### H-1: 例【検証済み】\n\n- Severity: High\n", "### H-1: 例【検証済み】"),
        (
            "## Summary\n\n| Severity | 件数 |\n| --- | --- |\n| High | 2 |\n\n"
            "## High\n\n### H-1: 例【検証済み】\n\n- 位置: a.py:1\n",
            "Summary counts 2 High finding(s) but 1 were read",
        ),
    ],
)
def test_from_markdown_refuses_findings_it_cannot_read(markdown: str, expected: str) -> None:
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.candidates_from_markdown(
            markdown, review_file="m.md", repository="usaturn/maatlog-poc", number=1, model="m", runtime="r"
        )
    assert any(expected in problem for problem in caught.value.problems), caught.value.problems


def test_from_markdown_still_reads_coverage_subsections_and_matching_summaries() -> None:
    markdown = (
        "## Summary\n\n| Severity | 件数 |\n| --- | --- |\n| High | 1 |\n| Low | 0 |\n\n"
        "## High\n\n### H-1: 例【検証済み】\n\n- 位置: a.py:1\n\n"
        "## Review coverage\n\n### 実行した確認\n\n- 何か\n"
    )
    value = review_findings.candidates_from_markdown(
        markdown, review_file="m.md", repository="usaturn/maatlog-poc", number=1, model="m", runtime="r"
    )
    assert [item["id"] for item in value["findings"]] == ["H-1"]


# --- 条件を述べる文書の候補 -------------------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, files: Mapping[str, str | None]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _commit(root, files, "base")
    return root


def _commit(root: Path, files: Mapping[str, str | None], message: str) -> str:
    for name, text in files.items():
        path = root / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


IMPORTER_BEFORE = """\
def accept(record):
    if record.status == "ARCHIVED":
        if (
            record.owner is None
        ):
            return False
    return True
"""

IMPORTER_AFTER = """\
def accept(record):
    if record.status == "ARCHIVED":
        if (
            record.owner is None
            or record.retention_days < 30
        ):
            return False
    return True
"""


def test_statement_candidates_lists_documents_naming_the_enclosing_state(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "importer.py": IMPORTER_BEFORE,
            "README.md": "# Importer\n\nARCHIVED のレコードも常に取り込む。\n\n別の話題。\n",
            "docs/usage.rst": "retention_days は保持日数。\n",
        },
    )
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"importer.py": IMPORTER_AFTER}, "narrow")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    review_findings.validate_document(document)
    assert document["from"] == base and document["to"] == head
    paths = [candidate["path"] for candidate in document["candidates"]]
    assert paths == ["README.md:3", "docs/usage.rst:1"]
    assert document["candidates"][0]["terms"] == ["ARCHIVED"]
    assert document["candidates"][0]["id"] == "S-1"
    assert "retention_days" in {term["term"] for term in document["terms"]}


def test_extract_terms_skips_plain_words_and_excluded_directories() -> None:
    diff = """\
diff --git a/tool.py b/tool.py
--- a/tool.py
+++ b/tool.py
@@ -1,2 +1,2 @@
-result = "success"
+result = compute_total(value, mode="FAST")
diff --git a/tests/test_tool.py b/tests/test_tool.py
--- a/tests/test_tool.py
+++ b/tests/test_tool.py
@@ -1 +1 @@
-assert ONLY_IN_TESTS
+assert ONLY_IN_TESTS_TOO
"""
    terms = review_findings.extract_terms(diff)
    assert "compute_total" in terms
    assert "FAST" in terms
    assert "success" not in terms
    assert "result" not in terms
    assert "value" not in terms
    assert not any(term.startswith("ONLY_IN_TESTS") for term in terms)
    assert terms["compute_total"] == ["tool.py:1"]


def test_statement_candidates_reads_code_spans_removed_from_documents(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "guide.md": "設定は `--fast-mode` で切り替える。\n",
            "README.md": "`--fast-mode` を付けると速くなる。\n",
        },
    )
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"guide.md": "設定は自動で切り替わる。\n"}, "rewrite")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [candidate["path"] for candidate in document["candidates"]] == ["README.md:1"]


def test_statement_candidates_puts_frequent_terms_in_common_terms(tmp_path: Path) -> None:
    lines = "".join(f"MODE_FLAG の説明 {index}\n" for index in range(review_findings.COMMON_TERM_LIMIT + 1))
    root = _repo(tmp_path, {"tool.py": "x = 1\n", "notes.md": lines, "README.md": "MODE_FLAG を見る\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "x = MODE_FLAG\n"}, "use flag")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert document["common_terms"] == [{"term": "MODE_FLAG", "count": review_findings.COMMON_TERM_LIMIT + 2}]
    assert document["candidates"] == []


def test_statement_candidates_merges_terms_on_the_same_line(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": "LEFT_SIDE と RIGHT_SIDE を比べる\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = LEFT_SIDE < RIGHT_SIDE\n"}, "compare")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert len(document["candidates"]) == 1
    assert document["candidates"][0]["terms"] == ["LEFT_SIDE", "RIGHT_SIDE"]


def test_statement_candidates_keeps_the_excerpt_around_a_term_far_into_a_long_line(tmp_path: Path) -> None:
    long_line = "前置き" * 100 + " deep_marker_alpha " + "後書き" * 100
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": f"{long_line}\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = deep_marker_alpha\n"}, "marker")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    review_findings.validate_document(document)
    text = document["candidates"][0]["text"]
    assert "deep_marker_alpha" in text
    assert text.startswith("… ") and text.endswith(" …")
    assert len(text) < len(long_line)


def test_statement_candidates_keeps_a_short_line_as_it_is(tmp_path: Path) -> None:
    line = "x" * 100 + " short_marker_beta " + "y" * 81
    assert len(line) == 200
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": f"  {line}  \n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = short_marker_beta\n"}, "marker")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert document["candidates"][0]["text"] == line


def test_statement_candidates_joins_excerpts_of_distant_terms_in_one_long_line(tmp_path: Path) -> None:
    long_line = "頭" * 40 + " first_marker_gamma " + "中" * 400 + " second_marker_delta " + "尾" * 40 + "終" * 200
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": f"{long_line}\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = (first_marker_gamma, second_marker_delta)\n"}, "markers")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    text = document["candidates"][0]["text"]
    assert document["candidates"][0]["terms"] == ["first_marker_gamma", "second_marker_delta"]
    assert text.startswith("頭") and text.endswith(" …")
    first, second = text.split(" … ")[:2], text.split(" … ")[1]
    assert "first_marker_gamma" in first[0] and "second_marker_delta" in second
    assert "first_marker_gamma" not in second


def test_statement_candidates_merges_overlapping_excerpts(tmp_path: Path) -> None:
    long_line = "前" * 150 + " near_marker_eps " + "あ" * 30 + " near_marker_zeta " + "後" * 150
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": f"{long_line}\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = (near_marker_eps, near_marker_zeta)\n"}, "markers")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    text = document["candidates"][0]["text"]
    assert text.count(" … ") == 0
    assert "near_marker_eps" in text and "near_marker_zeta" in text


def test_statement_candidates_does_not_count_a_term_inside_a_longer_word(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": "LIMIT_VALUE を守る\n\nLIMIT は上限\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = LIMIT_VALUE + LIMIT\n"}, "limit")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [(candidate["path"], candidate["terms"]) for candidate in document["candidates"]] == [
        ("README.md:1", ["LIMIT_VALUE"]),
        ("README.md:3", ["LIMIT"]),
    ]


def test_statement_candidates_finds_terms_containing_regex_characters(tmp_path: Path) -> None:
    spans = ["load_all()", "cfg[key_name]", "^ANCHOR_X$", "a_b+c*d?", "x_y{2}|z", "path\\to_file", "conf.d_x"]
    root = _repo(
        tmp_path,
        {
            "guide.md": "".join(f"`{span}` を使う。\n" for span in spans),
            "README.md": "".join(f"{span} の形\n\n" for span in spans),
        },
    )
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"guide.md": "使わない。\n"}, "rewrite")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    # コードスパンの中の識別子（load_all() の load_all など）も語になるので、
    # スパン全体の語が正しい行に当たることだけを見る。
    candidates = document["candidates"]
    expected_paths = [f"README.md:{2 * index + 1}" for index in range(len(spans))]
    assert [candidate["path"] for candidate in candidates] == expected_paths
    assert all(span in candidate["terms"] for span, candidate in zip(spans, candidates, strict=True))


def test_statement_candidates_ignores_documents_under_excluded_directories(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"tool.py": "a = 1\n", "evals/packet.md": "LIMIT_VALUE\n", "README.md": "LIMIT_VALUE を守る\n"},
    )
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = LIMIT_VALUE\n"}, "limit")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [candidate["path"] for candidate in document["candidates"]] == ["README.md:1"]


def test_statement_candidates_with_only_excluded_changes_writes_an_empty_list(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tests/test_tool.py": "X = 1\n", "README.md": "SOME_FLAG\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tests/test_tool.py": "X = SOME_FLAG\n"}, "test only")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    review_findings.validate_document(document)
    assert document["terms"] == [] and document["candidates"] == [] and document["common_terms"] == []


def test_statement_candidates_handles_deleted_files(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"legacy.py": "MODE = 'LEGACY_MODE'\n", "README.md": "LEGACY_MODE は廃止予定\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"legacy.py": None}, "remove")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [candidate["path"] for candidate in document["candidates"]] == ["README.md:1"]


def test_cli_statement_candidates_writes_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": "LIMIT_VALUE を守る\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = LIMIT_VALUE\n"}, "limit")
    output = tmp_path / "verified" / f"{head[:12]}.statement-candidates.json"
    output.parent.mkdir()

    code = review_findings.main(
        ["statement-candidates", "--root", str(root), "--from", base, "--to", head, "--output", str(output)]
    )

    assert code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["kind"] == "statement_candidates"
    assert json.loads(capsys.readouterr().out) == {"output": str(output), "candidates": 1, "common_terms": 0}
    assert sorted(path.name for path in output.parent.iterdir()) == [output.name]


def test_cli_statement_candidates_rejects_an_unknown_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "x\n"})
    head = _git(root, "rev-parse", "HEAD")
    output = tmp_path / "out.json"
    output.write_text("previous", encoding="utf-8")

    code = review_findings.main(
        ["statement-candidates", "--root", str(root), "--from", "0" * 40, "--to", head, "--output", str(output)]
    )

    assert code == 65
    assert output.read_text(encoding="utf-8") == "previous"
    assert "git" in capsys.readouterr().err


def test_extract_terms_does_not_mistake_removed_dash_lines_for_headers() -> None:
    diff = """\
diff --git a/query.sql b/query.sql
--- a/query.sql
+++ b/query.sql
@@ -1,2 +1,1 @@
--- old note about LEGACY_TABLE
 SELECT * FROM ORDERS_TABLE;
"""
    terms = review_findings.extract_terms(diff)
    assert terms["LEGACY_TABLE"] == ["query.sql:1"]
    assert terms["ORDERS_TABLE"] == ["query.sql:1"]


def test_extract_terms_places_lines_of_a_deleted_file_where_they_were() -> None:
    diff = """\
diff --git a/legacy.py b/legacy.py
deleted file mode 100644
index 1111111..0000000
--- a/legacy.py
+++ /dev/null
@@ -1,2 +0,0 @@
-import os
-MODE = "LEGACY_MODE"
"""
    assert review_findings.extract_terms(diff)["LEGACY_MODE"] == ["legacy.py:2"]


def test_extract_terms_places_removed_lines_in_the_old_file() -> None:
    diff = """\
diff --git a/old_tool.py b/new_tool.py
similarity index 80%
rename from old_tool.py
rename to new_tool.py
--- a/old_tool.py
+++ b/new_tool.py
@@ -10,4 +10,3 @@
 KEEP_BEFORE = 0
-REMOVED_ONE = 1
-REMOVED_TWO = 2
+ADDED_ONE = 1
 KEEP_AFTER = 3
"""
    terms = review_findings.extract_terms(diff)
    assert terms["REMOVED_ONE"] == ["old_tool.py:11"]
    assert terms["REMOVED_TWO"] == ["old_tool.py:12"]
    assert terms["ADDED_ONE"] == ["new_tool.py:11"]
    assert terms["KEEP_AFTER"] == ["new_tool.py:12"]


def test_statement_candidates_ignores_user_diff_prefix_settings(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"tool.py": "a = 1\n", "README.md": "LIMIT_VALUE を守る\n"})
    _git(root, "config", "diff.noprefix", "true")
    _git(root, "config", "diff.mnemonicPrefix", "true")
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "a = LIMIT_VALUE\n"}, "limit")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [candidate["path"] for candidate in document["candidates"]] == ["README.md:1"]


def test_statement_candidates_handles_non_ascii_paths(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"ツール.py": "a = 1\n", "docs/説明.md": "LIMIT_VALUE を守る\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"ツール.py": "a = LIMIT_VALUE\n"}, "limit")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [candidate["path"] for candidate in document["candidates"]] == ["docs/説明.md:1"]
    assert document["terms"][0]["origins"] == ["ツール.py:1"]


def test_extract_terms_rejects_headers_it_cannot_read() -> None:
    diff = """\
diff --git "a/x.py" "b/x.py"
--- "a/x.py"
+++ "b/x.py"
@@ -1 +1 @@
-a = 1
+a = LIMIT_VALUE
"""
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.extract_terms(diff)
    assert "diff header not understood" in caught.value.problems[0]


def test_extract_terms_reads_changed_lines_of_documents() -> None:
    diff = """\
diff --git a/guide.rst b/guide.rst
--- a/guide.rst
+++ b/guide.rst
@@ -1,3 +1,5 @@
 context_only_name は文脈行にある。
-旧い説明: MERGED後も `old_flag_name` で対応する。
+   .. versionadded:: <CONFIRMED_VERSION>
+version-info の範囲に置き、rst:directive:option の body に書く。:py:func:`load` と `--dry-run` を使う。
+設定は conf.py に書き、`setup.cfg` も読む。指摘 C-1 は直した。
 末尾の文脈行。
"""
    terms = review_findings.extract_terms(diff)
    assert terms["MERGED"] == ["guide.rst:2"]
    assert terms["old_flag_name"] == ["guide.rst:2"]
    assert terms["versionadded"] == ["guide.rst:2"]
    assert terms["CONFIRMED_VERSION"] == ["guide.rst:2"]
    assert terms["version-info"] == ["guide.rst:3"]
    assert terms["rst:directive:option"] == ["guide.rst:3"]
    assert terms["py:func"] == ["guide.rst:3"]
    assert terms["--dry-run"] == ["guide.rst:3"]
    assert terms["setup.cfg"] == ["guide.rst:4"]
    for skipped in ("context_only_name", "body", "load", "conf.py", "conf", "C-1", "dry-run"):
        assert skipped not in terms


TEMPLATE_BEFORE = """\
.. authoring: directive-reference 用。
   版情報が確認できない場合は versionadded を含む行を残さない。

.. optional-start: version-info

.. versionadded:: <CONFIRMED_VERSION>

.. optional-end: version-info

.. rst:directive:: <DIRECTIVE_NAME>

   <DESCRIPTION>
"""

TEMPLATE_AFTER = """\
.. authoring: directive-reference 用。
   版情報は対象の定義ブロック内の version-info 範囲に置く。
   版情報が確認できない場合は versionadded を含む行を残さない。

.. rst:directive:: <DIRECTIVE_NAME>

   <DESCRIPTION>

   .. optional-start: version-info

   .. versionadded:: <CONFIRMED_VERSION>

   .. optional-end: version-info
"""


def test_statement_candidates_lists_documents_for_a_template_only_change(tmp_path: Path) -> None:
    # テンプレートだけの修正は、語がほとんど取れず候補が 0 件になっていた（Issue #620）。
    root = _repo(
        tmp_path,
        {
            "assets/template-directive-reference.rst": TEMPLATE_BEFORE,
            "references/markup.md": "- `versionadded` は対象 object を定義する directive の body 内に置く。\n",
        },
    )
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"assets/template-directive-reference.rst": TEMPLATE_AFTER}, "move version info")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    review_findings.validate_document(document)
    paths = {candidate["path"] for candidate in document["candidates"]}
    assert {
        "references/markup.md:1",
        "assets/template-directive-reference.rst:2",
        "assets/template-directive-reference.rst:11",
    } <= paths


def test_statement_candidates_reads_kebab_case_names(tmp_path: Path) -> None:
    # #555 の 33: kebab-case の文字列リテラルと、文書から消えた kebab-case のコードスパン。
    root = _repo(
        tmp_path,
        {
            "worker.py": 'COMMANDS = ("input-read",)\n',
            "guide.md": "完了は `review-complete` で知らせる。\n",
            "README.md": "input-write で書き込む。\n\nreview-complete を待つ。\n",
        },
    )
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(
        root,
        {"worker.py": 'COMMANDS = ("input-read", "input-write")\n', "guide.md": "完了は終了コードで知らせる。\n"},
        "rename",
    )

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [(candidate["path"], candidate["terms"]) for candidate in document["candidates"]] == [
        ("README.md:1", ["input-write"]),
        ("README.md:3", ["review-complete"]),
    ]


def _duplicate_first_candidate(value: dict[str, Any]) -> None:
    value["candidates"].append(dict(value["candidates"][0]))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (_set("X-1", "candidates", 0, "id"), "candidates[0].id"),
        (_set("README.md", "candidates", 0, "path"), "candidates[0].path"),
        (_set([], "candidates", 0, "terms"), "candidates[0].terms"),
        (_duplicate_first_candidate, "duplicate"),
        (_set("abc", "to"), "to"),
        (_set(-1, "common_terms", 0, "count"), "common_terms[0].count"),
        (_set([], "terms", 0, "origins"), "terms[0].origins"),
    ],
)
def test_invalid_statement_candidates_name_the_problem(
    mutation: Callable[[dict[str, Any]], None], expected: str
) -> None:
    value = statement_candidates_document()
    mutation(value)
    assert any(expected in problem for problem in problems_of(value))


# 修正主張と変更ファイルの判定漏れ・実行証拠の取り違えを防ぐ。
def evidence_check() -> dict[str, Any]:
    return {
        "command": "uv run python probe.py",
        "head": FIX,
        "mode": "executed",
        "exit_code": 0,
        "result": "生成した全行のキーと値を比較した",
        "artifact": "export.json: rows[*].key/value",
    }


def evidence_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    document, inventory = judged_pair()
    fix_id = document["fix_check"]["fix_ids"][0]
    inventory.update(
        fix_ids=[fix_id],
        claims=[
            {
                "id": "C-1",
                "fix_id": fix_id,
                "claim": "全行のキーと値を保つ",
                "source": "Spec Fix F1 検証",
                "kind": "artifact",
            }
        ],
        changed_files=["exporter.py", "old name.rst"],
    )
    document["fix_evidence"] = {
        "claims": [
            {"id": "C-1", "result": "holds", "basis": "全行を比較", "checks": [evidence_check()], "findings": []}
        ],
        "files": [
            {
                "path": "exporter.py",
                "result": "passed",
                "rule": "AGENTS.md: lint",
                "basis": "lint実行",
                "checks": [evidence_check()],
                "findings": [],
            },
            {
                "path": "old name.rst",
                "result": "not_applicable",
                "rule": "削除ファイル",
                "basis": "toには無く検査対象の本文が無い",
                "checks": [],
                "findings": [],
            },
        ],
    }
    return document, inventory


def validate_evidence(document: dict[str, Any], inventory: dict[str, Any]) -> None:
    review_findings.validate_document(document)
    review_findings.check_statement_judgments(document, inventory)


def test_fix_evidence_accepts_executed_artifact_and_deleted_file() -> None:
    validate_evidence(*evidence_pair())


@pytest.mark.parametrize("section", ["claims", "files"])
def test_fix_evidence_requires_every_claim_and_changed_file(section: str) -> None:
    document, inventory = evidence_pair()
    document["fix_evidence"][section].pop()
    with pytest.raises(review_findings.FindingsError, match="not judged"):
        validate_evidence(document, inventory)


@pytest.mark.parametrize("section", ["claims", "files"])
def test_fix_evidence_rejects_duplicate_judgments(section: str) -> None:
    document, inventory = evidence_pair()
    document["fix_evidence"][section].append(copy.deepcopy(document["fix_evidence"][section][0]))
    with pytest.raises(review_findings.FindingsError, match="more than once"):
        validate_evidence(document, inventory)


@pytest.mark.parametrize("field,bad", [("head", HEAD), ("exit_code", True), ("mode", "assumed"), ("command", "")])
def test_fix_evidence_rejects_malformed_execution(field: str, bad: object) -> None:
    document, inventory = evidence_pair()
    document["fix_evidence"]["claims"][0]["checks"][0][field] = bad
    with pytest.raises(review_findings.FindingsError, match=field):
        validate_evidence(document, inventory)


@pytest.mark.parametrize("change", [{"mode": "static"}, {"exit_code": 1}, {"artifact": None}])
def test_artifact_claim_requires_executed_generated_evidence(change: dict[str, Any]) -> None:
    document, inventory = evidence_pair()
    document["fix_evidence"]["claims"][0]["checks"][0].update(change)
    with pytest.raises(review_findings.FindingsError, match="executed evidence"):
        validate_evidence(document, inventory)


@pytest.mark.parametrize("section,target", [("claims", "claim:C-1"), ("files", "file:exporter.py")])
def test_unchecked_evidence_requires_remaining_coverage(section: str, target: str) -> None:
    document, inventory = evidence_pair()
    document["fix_evidence"][section][0].update(result="unchecked", checks=[])
    with pytest.raises(review_findings.FindingsError, match="coverage.remaining"):
        validate_evidence(document, inventory)
    document["coverage"]["remaining"].append({"target": target, "reason": "環境がない"})
    validate_evidence(document, inventory)


@pytest.mark.parametrize("section,result", [("claims", "violates"), ("files", "failed")])
def test_failed_evidence_requires_confirmed_finding(section: str, result: str) -> None:
    document, inventory = evidence_pair()
    document["fix_evidence"][section][0]["result"] = result
    with pytest.raises(review_findings.FindingsError, match="confirmed finding"):
        validate_evidence(document, inventory)
    document["fix_evidence"][section][0]["findings"] = ["V-1"]
    validate_evidence(document, inventory)


def test_evidence_inventory_cannot_omit_a_fix_or_reuse_a_claim_id() -> None:
    document, inventory = evidence_pair()
    inventory["fix_ids"].append("F2")
    with pytest.raises(review_findings.FindingsError, match="claim"):
        validate_evidence(document, inventory)
    document, inventory = evidence_pair()
    inventory["claims"].append(copy.deepcopy(inventory["claims"][0]))
    with pytest.raises(review_findings.FindingsError, match="duplicate"):
        validate_evidence(document, inventory)


def test_v2_fix_check_requires_evidence_without_breaking_legacy() -> None:
    value = fix_check()
    review_findings.validate_document(value)
    value["schema"] = "review-findings/v2"
    value["verifier"]["provenance"] = provenance(role="fix_check")
    value["verifier"]["provenance"]["skills"] = [copy.deepcopy(value["skill"])]
    value["timing"]["waiting_seconds"] = None
    value["inputs"] = []
    value["accuracy"]["reviewers"] = []
    value["accuracy"]["find_seconds"] = {}
    with pytest.raises(review_findings.FindingsError, match="fix_evidence"):
        review_findings.validate_document(value)
    value["fix_evidence"] = evidence_pair()[0]["fix_evidence"]
    review_findings.validate_document(value)


def test_cli_evidence_validation_reports_missing_inventory(tmp_path: Path) -> None:
    document, inventory = evidence_pair()
    path = _write(tmp_path / f"{FIX[:12]}.fix-check.json", document)
    sidecar = _write(tmp_path / f"{FIX[:12]}.statement-candidates.json", inventory)
    assert review_findings.main(["validate", str(path)]) == 0
    del inventory["claims"]
    _write(sidecar, inventory)
    assert review_findings.main(["validate", str(path)]) == 65


def test_claim_inventory_lists_all_changed_paths_including_rename_and_deletion(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"old name.rst": "unchanged body\n", "削除.rst": "deleted\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(
        root, {"old name.rst": None, "new name.rst": "unchanged body\n", "削除.rst": None}, "rename and delete"
    )
    claims = {
        "fix_ids": ["F1"],
        "claims": [
            {"id": "C-1", "fix_id": "F1", "claim": "名称を変更する", "source": "Spec Fix F1", "kind": "static"}
        ],
    }
    claim_path = _write(tmp_path / "claims.json", claims)
    path = tmp_path / "inventory.json"
    assert (
        review_findings.main(
            [
                "statement-candidates",
                "--root",
                str(root),
                "--from",
                base,
                "--to",
                head,
                "--claims",
                str(claim_path),
                "--output",
                str(path),
            ]
        )
        == 0
    )
    inventory = json.loads(path.read_text())
    assert inventory["changed_files"] == ["new name.rst", "old name.rst", "削除.rst"]
    assert inventory["claims"] == claims["claims"]
    assert inventory["fix_ids"] == ["F1"]
