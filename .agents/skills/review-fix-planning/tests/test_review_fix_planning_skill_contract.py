from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = REPO_ROOT / ".agents/skills/review-fix-planning"
AGENTS_SKILL = SKILL_DIR / "SKILL.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def posting_snippet(content: str) -> str:
    """サイクルの状況コメントを投稿する bash のブロック（upsert_pr_comment.py に heredoc で本文を渡す）。"""
    blocks = [
        block for block in re.findall(r"```bash\n(.*?)\n```", content, re.DOTALL) if "upsert_pr_comment.py" in block
    ]
    assert len(blocks) == 1, blocks
    return blocks[0]


def test_posting_snippet_runs_verbatim_and_keeps_names_literal(tmp_path: Path) -> None:
    """投稿のブロックで逃がしが外れるとブランチ名をコマンドとして実行し、--records-file が外れると記録が欠ける。"""
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
        "PR_REPO": "usaturn/xrift-sample-world",
        "MODEL_NAME": "model-x",
        "ROUND_JSON": str(round_json),
        "ROUND": "1",
        "HEAD_BRANCH": "feature-x",
        "REVIEWED_HEAD": "a" * 12,
        "HEAD_SHA": "b" * 12,
    }
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{posting_snippet(read(AGENTS_SKILL))}"],
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
    assert args[args.index("--records-file") + 1] == str(round_json)
    body = stdin_file.read_text(encoding="utf-8")
    for value in ("feature-x", "a" * 12, "b" * 12):
        assert f"`{value}`" in body, value
