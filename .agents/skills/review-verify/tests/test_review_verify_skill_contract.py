"""review-verify（候補を確かめてまとめる工程）のスキル文書が参照するファイルの実在を確かめる。"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_SKILL = REPO_ROOT / ".agents/skills/review-verify/SKILL.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_links_resolve() -> None:
    """
    参照先が消えると、runner が起動する非対話の fix-check 担当は、
    読めなかった参照（判定の規則など）を飛ばして黙って確認を続ける。
    """
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(AGENTS_SKILL)):
        if target.startswith(("http://", "https://")):
            continue
        assert (AGENTS_SKILL.parent / target).resolve().exists(), target
