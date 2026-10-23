"""Issue #547: review-findings の読み取り・突き合わせ・修正の確認の検査が、指摘や確認を黙って落とさないこと。"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "review_findings.py"
SPEC = importlib.util.spec_from_file_location("review_findings", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
review_findings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_findings)

HEAD = "30f27c22a4a27b84ecf43a2ef23a3b10f2ce421a"
BASE = "e342dc8b6cb7fdc1f2c1f9a3d2b8c46b5a0a1f23"
FIX = "d7b1085f0e1d2c3b4a5968778695a4b3c2d1e0f9"
SHA = "0123456789abcdef" * 4


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
        "pr": {"repository": "usaturn/xrift-poc", "number": 542},
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


def verified_finding() -> dict[str, Any]:
    return {
        "id": "V-1",
        "sources": [f"claude-opus-5-5#H-1@{HEAD[:12]}", f"gpt-6.1-sol#M-2@{HEAD[:12]}"],
        "property": "checkbox は `- [ ]` か `- [x]` の後に空白が続く行だけ",
        "locations": ["tools/example.py:41"],
        "verdict": "confirmed",
        "basis": "隔離 worktree で再現した",
        "variants": [{"input": "`- [x][ref]`", "result": "violates", "how": "repro.py"}],
        "statement_sites": [{"path": "README.md:120", "claim": "未完了なら止まる", "result": "inconsistent"}],
        "pr_relation": {"value": "introduced", "basis": "base には無い"},
        "reported_severities": [
            {"source": f"claude-opus-5-5#H-1@{HEAD[:12]}", "severity": "High"},
            {"source": f"gpt-6.1-sol#M-2@{HEAD[:12]}", "severity": "Medium"},
        ],
        "reproduction": {
            "commands": ["uv run --no-project python repro.py"],
            "place": "隔離 worktree",
            "conditions": "参照リンクを含む本文",
            "result": "終了コード 0、誤って数える",
        },
    }


def verified() -> dict[str, Any]:
    return {
        "schema": "review-findings/v1",
        "kind": "verified",
        "mode": "cycle",
        "pr": {"repository": "usaturn/xrift-poc", "number": 542},
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


# 以下の 3 つ（と fix_check・HEAD・FIX）は、review-verify の tests/test_dispatch.py がこのファイルを読み込んで使う。


def planning() -> dict[str, Any]:
    return {
        "schema": "review-findings/v1",
        "kind": "planning",
        "pr": {"repository": "usaturn/xrift-poc", "number": 542},
        "reviewed_head": HEAD,
        "planner": {"model": "claude-opus-5-5", "runtime": "claude-code"},
        "verified_file": f"verified/{HEAD[:12]}.json",
        "stages": {"decide_seconds": 600, "fix_seconds": 1200, "fix_check_seconds": [700]},
        "fix_checks": [{"file": f"verified/{FIX[:12]}.fix-check.json", "blocking_fixes": 0}],
        "outcome": "fixes_pushed",
        "comment_id": 5987695309,
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


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


# --- validate -------------------------------------------------------------------------------------------------


def test_fix_check_to_must_equal_reviewed_head() -> None:
    """fix_check.to と reviewed_head の食い違いを通すと、候補ファイルの範囲と確かめた commit が違うまま受け入れる。"""
    value = fix_check()
    review_findings.validate_document(value)
    value["fix_check"]["to"] = HEAD
    with pytest.raises(review_findings.FindingsError):
        review_findings.validate_document(value)


def test_duplicate_ids_are_rejected() -> None:
    """同じ ID の指摘を通すと、check_pair と参照 <file>#<ID>@<head> が 1 つにまとめ、片方が判断から黙って漏れる。"""
    value = candidates()
    review_findings.validate_document(value)
    value["findings"].append(candidate_finding())
    with pytest.raises(review_findings.FindingsError):
        review_findings.validate_document(value)


# --- 文書の候補の判定（fix_check） --------------------------------------------------------------------------------


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


def test_check_statement_judgments_reports_a_different_to() -> None:
    """別の範囲で作った候補ファイルを使い回しても通ると、今回の修正で変わった文書を確かめない。"""
    document, candidates_value = judged_pair()
    candidates_value["to"] = HEAD
    with pytest.raises(review_findings.FindingsError):
        review_findings.check_statement_judgments(document, candidates_value)


def test_check_statement_judgments_requires_inconsistent_sites_in_findings() -> None:
    """inconsistent と判定した文書がどの指摘にも載らないと、修正の対象から黙って漏れる。"""
    document, candidates_value = judged_pair()
    document["findings"][0]["statement_sites"] = []
    with pytest.raises(review_findings.FindingsError):
        review_findings.check_statement_judgments(document, candidates_value)


def test_cli_validate_checks_the_fix_check_against_its_candidates(tmp_path: Path) -> None:
    """CLI の validate（self の経路の唯一の関門）が判定漏れを通すと、確かめていない fix-check を保存する。"""
    document, candidates_value = judged_pair()
    verified_dir = tmp_path / "verified"
    _write(verified_dir / f"{FIX[:12]}.statement-candidates.json", candidates_value)
    good = _write(verified_dir / f"{FIX[:12]}.fix-check.json", document)
    assert review_findings.main(["validate", str(good)]) == 0
    document["statement_candidates"]["judgments"].pop()
    bad = _write(verified_dir / f"{FIX[:12]}.fix-check.json", document)
    assert review_findings.main(["validate", str(bad)]) != 0


def test_cli_validate_fix_check_without_candidates_file_fails(tmp_path: Path) -> None:
    """候補ファイルが無いときに照合を飛ばすと、判定漏れの fix-check を終了コード 0 で受け入れる。"""
    lone = _write(tmp_path / "verified" / f"{FIX[:12]}.fix-check.json", fix_check())
    assert review_findings.main(["validate", str(lone)]) != 0


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


def test_from_markdown_reads_active_findings_and_unverified_candidates() -> None:
    """読み取りを誤ると、JSON の無いレビュアーの指摘が黙って欠ける・誤読され、確かめも判断もされない。"""
    value = review_findings.candidates_from_markdown(
        REVIEW_MD,
        review_file="claude-opus-5-5.md",
        repository="usaturn/xrift-poc",
        number=542,
        model="claude-opus-5-5",
        runtime="claude-code",
    )
    assert [(item["id"], item["severity"]) for item in value["findings"]] == [("H-1", "High"), ("L-2", "Low")]
    first, second = value["findings"]
    assert first["locations"] == ["`tools/example.py:41`、`tools/example.py:77`"]
    assert first["detected_head"] == HEAD[:12]
    assert first["confidence"] == "verified"
    assert second["detected_head"] == "unknown"
    assert "confidence" not in second
    assert value["reviewed_head"] == HEAD[:12]
    assert [item["id"] for item in value["unverified_candidates"]] == ["U-1"]


@pytest.mark.parametrize(
    "markdown",
    [
        "## High\n\n### GraphQL の型エラー\n\n- 位置: a.py:1\n",
        "## Active findings\n\n### H-1: 例【検証済み】\n\n- Severity: High\n",
        "## Summary\n\n| Severity | 件数 |\n| --- | --- |\n| High | 2 |\n\n"
        "## High\n\n### H-1: 例【検証済み】\n\n- 位置: a.py:1\n",
    ],
    ids=["heading-without-id", "unknown-section", "summary-count"],
)
def test_from_markdown_refuses_findings_it_cannot_read(markdown: str) -> None:
    """型どおりでない見出し・知らない節の指摘・件数の食い違いを読み飛ばすと、指摘を黙って落とす。"""
    with pytest.raises(review_findings.FindingsError):
        review_findings.candidates_from_markdown(
            markdown, review_file="m.md", repository="usaturn/xrift-poc", number=1, model="m", runtime="r"
        )


# --- check_pair -----------------------------------------------------------------------------------------------


def paired() -> dict[str, Any]:
    value = candidates()
    value["findings"][0]["locations"] = ["tools/example.py:41", "tools/example.py:77"]
    low = candidate_finding("L-2", "Low")
    low["locations"] = ["tools/other.py:3"]
    value["findings"].append(low)
    return value


def _drop_last_finding(value: dict[str, Any]) -> None:
    value["findings"].pop()


def _drop_unverified(value: dict[str, Any]) -> None:
    value["unverified_candidates"] = []


@pytest.mark.parametrize(("mutation", "dropped"), [(_drop_last_finding, "L-2"), (_drop_unverified, "U-1")])
def test_check_pair_reports_what_the_json_dropped(mutation: Callable[[dict[str, Any]], None], dropped: str) -> None:
    """JSON から落ちた指摘・未確認の候補を通すと、JSON を読む review-verify・loop がそれを黙って確かめない。"""
    value = paired()
    mutation(value)
    with pytest.raises(review_findings.FindingsError) as caught:
        review_findings.check_pair(REVIEW_MD, value)
    assert any(dropped in problem for problem in caught.value.problems), caught.value.problems


def test_cli_check_pair_fails_when_the_json_drops_a_finding(tmp_path: Path) -> None:
    """review-verify は check-pair の終了コードで JSON を使うか決めるので、照合を飛ばすと指摘の落ちた JSON を使う。"""
    markdown = tmp_path / "claude-opus-5-5.md"
    markdown.write_text(REVIEW_MD, encoding="utf-8")
    pair = _write(tmp_path / "pair.json", paired())
    assert review_findings.main(["check-pair", str(markdown), str(pair)]) == 0
    mismatched = paired()
    _drop_last_finding(mismatched)
    other = _write(tmp_path / "other.json", mismatched)
    assert review_findings.main(["check-pair", str(markdown), str(other)]) != 0


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
    """diff の文脈行の状態名・変更行の識別子で文書を探せないと、確かめるべき文書が黙って候補から消える。"""
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

    assert [candidate["path"] for candidate in document["candidates"]] == ["README.md:3", "docs/usage.rst:1"]
    assert document["candidates"][0]["terms"] == ["ARCHIVED"]


def test_statement_candidates_puts_frequent_terms_in_common_terms(tmp_path: Path) -> None:
    """多すぎる語を common_terms に残さず捨てると、その語の文書を誰も確かめず、確かめていないことも分からない。"""
    lines = "".join(f"MODE_FLAG の説明 {index}\n" for index in range(review_findings.COMMON_TERM_LIMIT + 1))
    root = _repo(tmp_path, {"tool.py": "x = 1\n", "notes.md": lines, "README.md": "MODE_FLAG を見る\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tool.py": "x = MODE_FLAG\n"}, "use flag")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [item["term"] for item in document["common_terms"]] == ["MODE_FLAG"]


def test_statement_candidates_finds_terms_containing_regex_characters(tmp_path: Path) -> None:
    """git grep -E に渡す語のエスケープが壊れると、正規表現の文字を含む語の文書が黙って候補から消える。"""
    spans = [
        "load_all()",
        "cfg[key_name]",
        "^ANCHOR_X$",
        "a_b+c*d?",
        "x_y{2}|z",
        "path\\to_file",
        "conf.d_x",
        "[A-Z]_X",
        "x(y)_z",
    ]
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
    # スパン全体の語が正しい行に当たることだけを見る。[A-Z]_X・x(y)_z は部分の語を持たないので、
    # git grep -E に渡すエスケープが外れると、その行だけが候補から消える。
    candidates = document["candidates"]
    expected_paths = [f"README.md:{2 * index + 1}" for index in range(len(spans))]
    assert [candidate["path"] for candidate in candidates] == expected_paths
    assert all(span in candidate["terms"] for span, candidate in zip(spans, candidates, strict=True))


def test_statement_candidates_handles_deleted_files(tmp_path: Path) -> None:
    """削除したファイルの行を読まないと、消した名前を述べる文書が黙って候補から消える。"""
    root = _repo(tmp_path, {"legacy.py": "MODE = 'LEGACY_MODE'\n", "README.md": "LEGACY_MODE は廃止予定\n"})
    base = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"legacy.py": None}, "remove")

    document = review_findings.statement_candidates(root, base, head, generated_at="2026-10-05T00:00:00Z")

    assert [candidate["path"] for candidate in document["candidates"]] == ["README.md:1"]


def test_extract_terms_rejects_headers_it_cannot_read() -> None:
    """読めない見出しを読み飛ばすと、そのファイルの語が黙って抜ける（引用されたパス・接頭辞の設定を含む）。"""
    diff = """\
diff --git "a/x.py" "b/x.py"
--- "a/x.py"
+++ "b/x.py"
@@ -1 +1 @@
-a = 1
+a = LIMIT_VALUE
"""
    with pytest.raises(review_findings.FindingsError):
        review_findings.extract_terms(diff)


def test_extract_terms_reads_changed_lines_of_documents() -> None:
    """role・オプション・日本語に続く状態名などを文書の変更行から取れないと、その文書が黙って候補から消える。"""
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
    """Issue #620 の再現: テンプレートだけの修正で語が取れず、文書の候補が黙って 0 件になる。"""
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

    paths = {candidate["path"] for candidate in document["candidates"]}
    assert {
        "references/markup.md:1",
        "assets/template-directive-reference.rst:2",
        "assets/template-directive-reference.rst:11",
    } <= paths


def test_statement_candidates_reads_kebab_case_names(tmp_path: Path) -> None:
    """#555 の 33 の再現: kebab-case の名前を取れず、文書が黙って候補から漏れる。"""
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


# --- 修正の主張と変更ファイルの証拠 --------------------------------------------------------------------------------


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
    """証拠の拒否のテストの基準の入力が有効であることの対照（無効になると、拒否のテストが黙って通る）。"""
    validate_evidence(*evidence_pair())


@pytest.mark.parametrize("section", ["claims", "files"])
def test_fix_evidence_requires_every_claim_and_changed_file(section: str) -> None:
    """主張・変更ファイルの判定漏れを通すと、確かめていない修正を確認済みとして受け入れる。"""
    document, inventory = evidence_pair()
    document["fix_evidence"][section].pop()
    with pytest.raises(review_findings.FindingsError):
        validate_evidence(document, inventory)


def test_fix_evidence_rejects_a_check_run_at_another_head() -> None:
    """修正前の HEAD で実行した確認を通すと、修正後を確かめていないのに成功と記録する。"""
    document, inventory = evidence_pair()
    document["fix_evidence"]["claims"][0]["checks"][0]["head"] = HEAD
    with pytest.raises(review_findings.FindingsError):
        validate_evidence(document, inventory)


def test_artifact_claim_requires_executed_generated_evidence() -> None:
    """成果物の主張を静的な確認だけで成功にできると、生成物を確かめないまま確認済みにする。"""
    document, inventory = evidence_pair()
    document["fix_evidence"]["claims"][0]["checks"][0]["mode"] = "static"
    with pytest.raises(review_findings.FindingsError):
        validate_evidence(document, inventory)


def test_unchecked_evidence_requires_remaining_coverage() -> None:
    """未確認の主張が coverage.remaining に載らないと、確認が完了したように見える。"""
    document, inventory = evidence_pair()
    document["fix_evidence"]["claims"][0].update(result="unchecked", checks=[])
    with pytest.raises(review_findings.FindingsError):
        validate_evidence(document, inventory)
    document["coverage"]["remaining"].append({"target": "claim:C-1", "reason": "環境がない"})
    validate_evidence(document, inventory)


def test_failed_evidence_requires_confirmed_finding() -> None:
    """違反の判定が確認済みの指摘を指さないと、修正の対象にならず黙って残る。"""
    document, inventory = evidence_pair()
    document["fix_evidence"]["claims"][0]["result"] = "violates"
    with pytest.raises(review_findings.FindingsError):
        validate_evidence(document, inventory)
    document["fix_evidence"]["claims"][0]["findings"] = ["V-1"]
    validate_evidence(document, inventory)


def test_evidence_inventory_cannot_omit_a_fix() -> None:
    """主張の無い修正を通すと、その修正の証拠を誰も判定しない。"""
    document, inventory = evidence_pair()
    document["fix_check"]["fix_ids"].append("F2")
    inventory["fix_ids"].append("F2")
    with pytest.raises(review_findings.FindingsError):
        validate_evidence(document, inventory)


def test_v2_fix_check_requires_evidence_without_breaking_legacy() -> None:
    """v2 の fix_check で fix_evidence が必須でなくなると、証拠の関門全体を黙って迂回できる。"""
    value = fix_check()
    review_findings.validate_document(value)
    value["schema"] = "review-findings/v2"
    value["verifier"]["provenance"] = provenance(role="fix_check")
    value["verifier"]["provenance"]["skills"] = [copy.deepcopy(value["skill"])]
    value["timing"]["waiting_seconds"] = None
    value["inputs"] = []
    value["accuracy"]["reviewers"] = []
    value["accuracy"]["find_seconds"] = {}
    with pytest.raises(review_findings.FindingsError):
        review_findings.validate_document(value)
    value["fix_evidence"] = evidence_pair()[0]["fix_evidence"]
    review_findings.validate_document(value)


def test_cli_evidence_validation_reports_missing_inventory(tmp_path: Path) -> None:
    """主張の inventory の無い候補ファイルで照合を飛ばすと、証拠の照合を黙って迂回する。"""
    document, inventory = evidence_pair()
    path = _write(tmp_path / f"{FIX[:12]}.fix-check.json", document)
    sidecar = _write(tmp_path / f"{FIX[:12]}.statement-candidates.json", inventory)
    assert review_findings.main(["validate", str(path)]) == 0
    for key in ("fix_ids", "claims", "changed_files"):
        del inventory[key]
    _write(sidecar, inventory)
    assert review_findings.main(["validate", str(path)]) != 0


def test_claim_inventory_lists_all_changed_paths_including_rename_and_deletion(tmp_path: Path) -> None:
    """改名前・削除したパスが変更ファイルの一覧から落ちると、その判定を求めず、確かめないまま通す。"""
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
