"""Issue #547: review-verify（候補を確かめてまとめる工程）のスキル文書の構造。"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_SKILL = REPO_ROOT / ".agents/skills/review-verify/SKILL.md"
EVALS = REPO_ROOT / ".agents/skills/review-verify/evals"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_links_resolve() -> None:
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(AGENTS_SKILL)):
        if target.startswith(("http://", "https://")):
            continue
        assert (AGENTS_SKILL.parent / target).resolve().exists(), target


def test_evals_have_unique_cases_with_expectations_and_existing_files() -> None:
    evals = json.loads(read(EVALS / "evals.json"))["evals"]
    assert evals
    names = [item["name"] for item in evals]
    assert len(names) == len(set(names))
    for item in evals:
        assert item["expectations"], item["name"]
        for name in item["files"]:
            assert (EVALS / name).is_file(), name
