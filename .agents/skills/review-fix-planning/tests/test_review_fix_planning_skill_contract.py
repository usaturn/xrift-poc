from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = REPO_ROOT / ".agents/skills/review-fix-planning"
AGENTS_SKILL = SKILL_DIR / "SKILL.md"
SKILLS = (AGENTS_SKILL,)  # 配置/Claudeリンクは共通test_skill_layoutが検査する
# 実行時に作られ、新しい checkout には無い場所（gitignore 対象）。参照の実在の検査から外す。
RUNTIME_DIRS = ("docs_draft/", "tmp/", "reviews/")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def referenced_paths(skill: Path) -> list[Path]:
    """SKILL.md が読ませる・実行させるファイル（`references/` と、`$TOPLEVEL/` からのパス）。"""
    content = read(skill)
    references = [skill.parent / path for path in re.findall(r"`(references/[^`\s]+)`", content)]
    toplevel = [
        REPO_ROOT / path for path in re.findall(r"\$TOPLEVEL/([\w./-]+)", content) if not path.startswith(RUNTIME_DIRS)
    ]
    return references + toplevel


@pytest.mark.parametrize("skill", SKILLS)
def test_referenced_paths_exist(skill: Path) -> None:
    paths = referenced_paths(skill)
    assert paths
    missing = [str(path) for path in paths if not path.exists()]
    assert missing == []


def posting_snippet(content: str) -> str:
    """サイクルの状況コメントを投稿する bash のブロック（upsert_pr_comment.py に heredoc で本文を渡す）。"""
    blocks = [
        block for block in re.findall(r"```bash\n(.*?)\n```", content, re.DOTALL) if "upsert_pr_comment.py" in block
    ]
    assert len(blocks) == 1, blocks
    return blocks[0]


@pytest.mark.parametrize("skill", SKILLS)
def test_posting_snippet_runs_verbatim_and_keeps_names_literal(skill: Path, tmp_path: Path) -> None:
    """投稿のブロックはそのまま実行される。heredoc の中の裸のバッククォートは、ブランチ名をコマンドとして実行する。"""
    parent = tmp_path / "parent"
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "uv.args"
    stdin_file = tmp_path / "uv.stdin"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\ncat > "{stdin_file}"\n', encoding="utf-8")
    uv.chmod(0o755)
    round_json = tmp_path / "round.json"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PR_NUMBER": "7",
        "PR_REPO": "usaturn/maatlog",
        "MODEL_NAME": "model-x",
        "ROUND_JSON": str(round_json),
        "ROUND": "1",
        "HEAD_BRANCH": "feature-x",
        "REVIEWED_HEAD": "a" * 12,
        "HEAD_SHA": "b" * 12,
    }
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{posting_snippet(read(skill))}"],
        cwd=parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = args_file.read_text(encoding="utf-8").splitlines()
    helper = parent.resolve() / ".agents/skills/review-fix-planning/scripts/upsert_pr_comment.py"
    assert Path(args[args.index("python") + 1]) == helper
    # PR の番号だけでは親と公開側のどちらの PR か決まらないので、リポジトリを渡す
    assert args[args.index("--repo") + 1] == "usaturn/maatlog"
    assert args[args.index("--pr") + 1] == "7"
    assert args[args.index("--records-file") + 1] == str(round_json)
    body = stdin_file.read_text(encoding="utf-8")
    for value in ("feature-x", "a" * 12, "b" * 12):
        assert f"`{value}`" in body, value


def test_evals_have_unique_cases_with_expectations_and_existing_files() -> None:
    evals = json.loads(read(SKILL_DIR / "evals/evals.json"))["evals"]
    assert evals
    names = [item["name"] for item in evals]
    assert len(names) == len(set(names))
    for item in evals:
        assert item["expectations"], item["name"]
        for relative in item["files"]:
            assert (SKILL_DIR / relative).is_file(), relative
