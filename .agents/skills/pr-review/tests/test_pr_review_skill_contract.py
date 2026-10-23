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

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_SKILL = REPO_ROOT / ".agents/skills/pr-review/SKILL.md"
EVALS = REPO_ROOT / ".agents/skills/pr-review/evals"
BUILD_FIXTURE = EVALS / "build_fixture.py"
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
    """参照先が消えると、非対話のレビュアーは読めなかった参照を飛ばして黙ってレビューを続ける。"""
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


# eval の fixture に仕込んだ欠陥が再現しなくなると、どのレビュアーも指摘できず、eval が「差が無い」と
# 黙って誤った結論を出す。以下の 3 件は、仕込んだ欠陥が実際に起きることを確かめる。


def test_multiline_input_fixture_rejects_the_documented_multiline_content(tmp_path: Path) -> None:
    """multiline-input の fixture が、複数行の内容を拒む欠陥を再現しなくなると eval が黙って誤る。"""
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
    accepted = _run_python(worktree, *command, str(one_line))
    assert accepted.returncode == 0, accepted.stderr


def test_same_pattern_fixture_fails_on_values_that_contain_equals(tmp_path: Path) -> None:
    """same-pattern の fixture の 3 つの関数が `=` を含む値で失敗しなくなると eval が黙って誤る。"""
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


def test_update_decided_fixture_records_the_round_and_the_prefix_defect(tmp_path: Path) -> None:
    """update-decided の fixture の修正の範囲と接頭辞の比較の欠陥が再現しなくなると eval が黙って誤る。"""
    built = _build("update-decided", tmp_path)
    worktree = Path(built["worktree"])
    heads: dict[str, str] = built["heads"]
    fix_range = _git(worktree, "log", "--first-parent", "--no-merges", "--format=%H", f"{heads['H1']}..HEAD")
    assert fix_range.stdout.split() == [heads["FIX"]]
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
    planned, stopped, written = completed.stdout.splitlines()
    # 接頭辞の比較の欠陥: load_plan は ../repo-evil/x を通し、apply_changes が止める
    assert planned == "planned 2"
    assert stopped.startswith("apply stopped:")
    assert written == "written True"
