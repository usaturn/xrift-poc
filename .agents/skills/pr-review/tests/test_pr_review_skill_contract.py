from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tools.review_triage.inputs import load_reviews
from tools.review_triage.models import Repository

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_SKILL = REPO_ROOT / ".agents/skills/pr-review/SKILL.md"
EVIDENCE_REFERENCE = REPO_ROOT / ".agents/skills/pr-review/references/evidence.md"
RECORDS_FORMAT = REPO_ROOT / ".agents/review-records.md"
EVALS = REPO_ROOT / ".agents/skills/pr-review/evals"
BUILD_FIXTURE = EVALS / "build_fixture.py"
REVIEW_RECORDS_SCRIPT = REPO_ROOT / ".agents/scripts/review_records.py"
# 実行時に作られ、新しい checkout には無い場所（gitignore 対象）。参照の実在の検査から外す。
RUNTIME_DIRS = ("docs_draft/", "tmp/", "reviews/")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def referenced_paths(skill: Path) -> list[Path]:
    """SKILL.md が読ませる・実行させるファイル（Markdown の相対リンクと、`$TOPLEVEL/` からのパス）。"""
    content = read(skill)
    links = [
        skill.parent / target for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", content) if "://" not in target
    ]
    toplevel = [
        REPO_ROOT / path for path in re.findall(r"\$TOPLEVEL/([\w./-]+)", content) if not path.startswith(RUNTIME_DIRS)
    ]
    return links + toplevel


def test_referenced_paths_exist() -> None:
    # 配置・リンクは共通layout検査で保証する。ここでは実体と参照文書の入口だけを確かめる。
    documents = [AGENTS_SKILL, *sorted((AGENTS_SKILL.parent / "references").glob("*.md"))]
    paths = [path for document in documents for path in referenced_paths(document)]
    assert paths
    missing = [str(path) for path in paths if not path.exists()]
    assert missing == []


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evals_reference_existing_fixtures() -> None:
    data = json.loads(read(EVALS / "evals.json"))
    assert data["skill_name"] == "pr-review"
    cases = data["evals"]
    assert cases
    builder = _load_module("pr_review_build_fixture", BUILD_FIXTURE)
    assert builder.fixture_names() == sorted(case["fixture"] for case in cases)
    for case in cases:
        assert case["expectations"]
        for relative in case["files"]:
            assert (EVALS.parent / relative).is_file(), relative


def _build(name: str, tmp_path: Path) -> dict[str, Any]:
    builder = _load_module("pr_review_build_fixture", BUILD_FIXTURE)
    built: dict[str, Any] = builder.build(name, tmp_path / "fixture")
    worktree = Path(built["worktree"])
    assert _git(worktree, "status", "--porcelain").stdout == ""
    pr = json.loads(Path(built["pr_json"]).read_text(encoding="utf-8"))
    assert pr["headRefOid"] == built["head_sha"]
    assert pr["changedFiles"] > 0
    assert Path(built["pr_diff"]).read_text(encoding="utf-8").startswith("diff --git ")
    return built


def _run_python(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONSAFEPATH", "PYTHONHOME"}}
    env["PYTHONPATH"] = str(worktree)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, *args], cwd=worktree.parent, env=env, check=False, capture_output=True, text=True
    )


def test_multiline_input_fixture_rejects_the_documented_multiline_content(tmp_path: Path) -> None:
    built = _build("multiline-input", tmp_path)
    worktree = Path(built["worktree"])
    store = tmp_path / "store"
    store.mkdir()
    two_lines = tmp_path / "two-lines.md"
    two_lines.write_text("- 位置: a.py:1\n- 内容: 入力を検査していない\n", encoding="utf-8")
    one_line = tmp_path / "one-line.md"
    one_line.write_text("位置: a.py:1 で入力を検査していない", encoding="utf-8")
    command = ("-m", "notes_tool.cli", "add", "--store", str(store), "--id", "H-1", "--content-file")
    rejected = _run_python(worktree, *command, str(two_lines))
    assert rejected.returncode == 2
    assert "control character" in rejected.stderr
    accepted = _run_python(worktree, *command, str(one_line))
    assert accepted.returncode == 0, accepted.stderr
    assert "複数行の Markdown でよい" in (worktree / "README.md").read_text(encoding="utf-8")


def test_same_pattern_fixture_fails_on_values_that_contain_equals(tmp_path: Path) -> None:
    built = _build("same-pattern", tmp_path)
    worktree = Path(built["worktree"])
    env_file = tmp_path / "app.env"
    env_file.write_text("URL=https://example.com/search?q=a\n", encoding="utf-8")
    probe = (
        "import pathlib, sys\n"
        "from confkit import loaders\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "calls = {\n"
        "    'load_env_file': lambda: loaders.load_env_file(path),\n"
        "    'load_labels': lambda: loaders.load_labels(path),\n"
        "    'parse_overrides': lambda: loaders.parse_overrides(['URL=https://example.com/search?q=a']),\n"
        "    'read_pairs': lambda: loaders.read_pairs(path),\n"
        "}\n"
        "for name, call in calls.items():\n"
        "    try:\n"
        "        call()\n"
        "    except ValueError:\n"
        "        print(name)\n"
    )
    completed = _run_python(worktree, "-c", probe, str(env_file))
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["load_env_file", "load_labels", "parse_overrides"]
    diff = Path(built["pr_diff"]).read_text(encoding="utf-8")
    assert diff.count('.split("=")') == 3


def test_update_decided_fixture_records_the_round_and_the_prefix_defect(tmp_path: Path) -> None:
    built = _build("update-decided", tmp_path)
    worktree = Path(built["worktree"])
    heads: dict[str, str] = built["heads"]
    records = _load_module("review_records_for_evals", REVIEW_RECORDS_SCRIPT)
    comments = records.comments_from_api(json.loads(Path(built["pr_comments"]).read_text(encoding="utf-8")))
    state = records.read_state(comments, expected_pr={"repository": "example/syncer", "number": 7})
    assert [(item["reviewed_head"], item["outcome"], item["commit"]) for item in state["rounds"]] == [
        (heads["H1"], "fixes_pushed", heads["FIX"])
    ]
    states = {item["id"]: item["state"] for item in state["decisions"]}
    assert states == {
        f"eval-reviewer#M-1@{heads['H1'][:12]}": "fix",
        f"eval-reviewer#L-1@{heads['H1'][:12]}": "skip",
    }
    fix_range = _git(worktree, "log", "--first-parent", "--no-merges", "--format=%H", f"{heads['H1']}..HEAD")
    assert fix_range.stdout.split() == [heads["FIX"]]
    previous = Path(built["previous_review"]).read_text(encoding="utf-8")
    assert "{{" not in previous and f"`{heads['H1'][:12]}` | Initial" in previous
    root = tmp_path / "work" / "repo"
    root.mkdir(parents=True)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"changes": [{"target": "docs/a.md", "text": "a"}, {"target": "../repo-evil/x", "text": "x"}]}),
        encoding="utf-8",
    )
    probe = (
        "import pathlib, sys\n"
        "from syncer.apply import apply_changes\n"
        "from syncer.plan import load_plan\n"
        "root, plan = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
        "changes = load_plan(plan, root)\n"
        "print('planned', len(changes))\n"
        "try:\n"
        "    apply_changes(root, changes)\n"
        "except ValueError as error:\n"
        "    print('apply stopped:', error)\n"
        "print('written', (root / 'docs/a.md').exists())\n"
    )
    completed = _run_python(worktree, "-c", probe, str(root), str(plan))
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "planned 2",
        "apply stopped: target escapes the worktree: ../repo-evil/x",
        "written True",
    ]


def review_records_labels() -> list[str]:
    """形式の定義元 4.2 節の表から、根拠の欄のラベルを順に取り出す（テストに写さない）。"""
    section = read(RECORDS_FORMAT).split("### 4.2 指摘の根拠の項目", 1)[1].split("**前回の修正の範囲**", 1)[0]
    labels = re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE)
    assert labels
    return labels


def finding_template(content: str) -> str:
    section = content.split("## 指摘 1 件のフォーマット", 1)[1].split("\n## ", 1)[0]
    return section.split("```markdown\n", 1)[1].split("\n```", 1)[0]


def test_finding_template_carries_review_records_labels() -> None:
    # 4.2 節は「位置」「検出時 HEAD」「内容」「根拠」の後、「推奨対応」の前に、表のラベルを表の順で書くと定める
    template = finding_template(read(AGENTS_SKILL))
    labels = [line[2:].split(":", 1)[0] for line in template.splitlines() if line.startswith("- ")]
    assert labels == ["位置", "検出時 HEAD", "内容", "根拠", *review_records_labels(), "推奨対応"]
    reproduction = template.split("- 再現:", 1)[1].split("\n- ", 1)[0]
    for item in ("コマンド", "場所", "条件", "結果"):
        assert f"\n  - {item}:" in reproduction


def output_template(content: str) -> str:
    section = content.split("\n## 出力\n", 1)[1]
    return section.split("```markdown\n", 1)[1].split("\n```\n", 1)[0]


# レビューファイルを読むコード（tools/review_triage/inputs.py・.agents/scripts/review_findings.py）と PR 修正ループは、
# この先頭行・見出し・Review history の列で対象と節を見分ける（review_findings は Summary の件数表を、読めた指摘の数と
# 照合する）。変えるときは、読む側とこのテストを合わせて更新する。
PARSED_HEADINGS = (
    "## Review history",
    "## Summary",
    "## Critical",
    "## High",
    "## Medium",
    "## Low",
    "## Info",
    "## Unverified candidates",
    "## Pending previous findings",
    "## Decided findings",
    "## Resolved findings",
)


def test_output_template_keeps_the_parts_that_code_reads() -> None:
    template = output_template(read(AGENTS_SKILL))
    assert template.splitlines()[0].startswith("- 対象: PR #<N> / head ")
    for heading in PARSED_HEADINGS:
        assert f"\n{heading}\n" in template, heading
    history_rows = [
        line for line in template.split("\n## Review history\n", 1)[1].splitlines() if line.startswith("|")
    ]
    assert history_rows
    assert [cell.strip() for cell in history_rows[0].strip("|").split("|")] == ["Reviewed at (UTC)", "HEAD", "Mode"]
    assert "### U-<n>:" in template


@pytest.mark.parametrize("mode_index", [0, 1])
def test_output_history_modes_can_be_recorded_for_commit_reviews(tmp_path: Path, mode_index: int) -> None:
    # テンプレートの選択肢を実際の読解・判断記録へ渡す。文言の一致ではなく後続処理の互換性を守る。
    template = output_template(read(AGENTS_SKILL))
    history = template.split("\n## Review history\n", 1)[1].split("\n## Summary\n", 1)[0]
    row = [line for line in history.splitlines() if line.startswith("|")][-1]
    modes = row.strip("|").split("|")[-1].split("/")
    mode = modes[mode_index].strip()
    head = "a" * 40
    rendered = template.split("\n## Summary\n", 1)[0].replace(row, row.rsplit("|", 2)[0] + f"| {mode} |")
    for placeholder, value in {
        "<N>": "593",
        "<12文字のshort-sha>": head[:12],
        "<YYYY-MM-DD>": "2026-10-07",
        "<RFC3339 UTC>": "2026-10-07T00:00:00Z",
    }.items():
        rendered = rendered.replace(placeholder, value)
    review_file = f"test-reviewer-commit-{head}"
    (tmp_path / f"{review_file}.md").write_text(rendered, encoding="utf-8")
    batch = load_reviews(tmp_path, repository=Repository.ENV, reviewed_head=head)
    assert not batch.anomalies
    latest = batch.files[0].history[-1]
    records = _load_module("review_records_for_history", REVIEW_RECORDS_SCRIPT)
    record = {
        "schema": records.SCHEMA_ID,
        "pr": {"repository": "usaturn/xrift-poc", "number": 593},
        "round": 1,
        "reviewed_head": head,
        "recorded_at": latest.reviewed_at,
        "planner": {"model": "test-planner", "runtime": "pi", "agent_id": "test-planner"},
        "seats": [
            {
                "review_file": review_file,
                "model": "test-reviewer",
                "runtime": "pi",
                "file_head": latest.head,
                "mode": latest.mode,
                "rerun": True,
                "merge_recommendation": "Approve",
            }
        ],
        "decisions": [],
        "outcome": "no_fix_needed",
    }
    assert records.validate_record(record) == record


def test_evidence_reference_examples_do_not_write_bytecode_into_the_worktree() -> None:
    content = read(EVIDENCE_REFERENCE)
    worktree_runs = [line for line in content.splitlines() if 'PYTHONPATH="$WT"' in line or "no:cacheprovider" in line]
    assert worktree_runs
    for line in worktree_runs:
        assert "PYTHONDONTWRITEBYTECODE=1" in line, line


def test_evidence_reference_examples_create_the_tar_destination_before_extracting() -> None:
    content = read(EVIDENCE_REFERENCE)
    extract_lines = [line for line in content.splitlines() if "tar -x -C" in line]
    assert extract_lines
    for line in extract_lines:
        destination = re.search(r'tar -x -C ("[^"]+")', line)
        assert destination, line
        mkdir = line.find(f"mkdir -p {destination.group(1)}")
        assert 0 <= mkdir < destination.start(), line


# evals の fixture の答えに当たる語（パッケージ名・関数名・評価用のレビュアー名）を参照文書に書かない。
# 書くと、評価が答えを読んだ結果になる。fixture を変えるときは、この一覧も合わせて更新する。
def test_evidence_reference_examples_do_not_reveal_eval_fixtures() -> None:
    content = read(EVIDENCE_REFERENCE)
    for fixture_word in ("notes_tool", "confkit", "syncer", "_is_inside", "eval-reviewer", 'split("="'):
        assert fixture_word not in content


def test_evidence_reference_examples_run_without_a_bare_python() -> None:
    content = read(EVIDENCE_REFERENCE)
    bare_python = re.compile(r"(?<![\w/.-])python(?![\w.])")
    for line in content.splitlines():
        if "$WT" in line or "$SCRATCH" in line:
            for match in bare_python.finditer(line):
                assert "uv run" in line[: match.start()], line
