"""スキルの置き場所の規則（AGENTS.md「スキルの置き場所」）を、すべてのスキルについて確かめる。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_SKILLS = REPO_ROOT / ".agents/skills"
CLAUDE_SKILLS = REPO_ROOT / ".claude/skills"
# 写しを置かない、ランタイム専用の置き場所。
RUNTIME_SKILL_DIRS = tuple(
    REPO_ROOT / runtime / "skills" for runtime in (".devin", ".cognition", ".codex", ".cursor", ".grok", ".pi")
)
# まだ一本化していないスキル（親 #564 の各子が担当する）。一本化する変更で、ここから外す。
PENDING = frozenset[str]()


def skill_names() -> list[str]:
    return sorted(path.name for path in AGENTS_SKILLS.iterdir())


def unified_names() -> list[str]:
    return [name for name in skill_names() if name not in PENDING]


def frontmatter(skill_md: Path) -> dict[str, object]:
    # pi などのランタイムは frontmatter を YAML として読み、解析できないスキルを登録しない。
    # 行の照合では引用符の無い値に含まれる `: ` などの構文エラーを見逃すため、YAML として読む。
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", f"{skill_md}: frontmatter が無い"
    try:
        meta = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
    except yaml.YAMLError as error:
        pytest.fail(f"{skill_md}: frontmatter を YAML として解析できない: {error}")
    assert isinstance(meta, dict), f"{skill_md}: frontmatter がマッピングではない"
    return cast(dict[str, object], meta)


@pytest.mark.parametrize("name", skill_names())
def test_body_is_a_real_skill_directory(name: str) -> None:
    body = AGENTS_SKILLS / name
    assert not body.is_symlink(), f"{name}: 本体は .agents/skills に実体で置く"
    meta = frontmatter(body / "SKILL.md")
    assert meta.get("name") == name
    description = meta.get("description")
    assert isinstance(description, str) and description.strip(), f"{name}: description は空でない文字列にする"


@pytest.mark.parametrize("name", unified_names())
def test_claude_entry_links_to_the_body(name: str) -> None:
    entry = CLAUDE_SKILLS / name
    assert entry.is_symlink(), f"{name}: .claude/skills/{name} は本体へのリンクにする"
    assert os.readlink(entry) == f"../../.agents/skills/{name}"
    assert entry.resolve() == (AGENTS_SKILLS / name).resolve()


@pytest.mark.parametrize("name", sorted(path.name for path in CLAUDE_SKILLS.iterdir()))
def test_claude_has_no_skill_of_its_own(name: str) -> None:
    assert (AGENTS_SKILLS / name).is_dir(), f"{name}: .claude にだけあるスキルは作らない"


@pytest.mark.parametrize("name", unified_names())
def test_no_runtime_copies(name: str) -> None:
    copies = [str(path) for base in RUNTIME_SKILL_DIRS if (path := base / name).exists() or path.is_symlink()]
    assert copies == [], f"{name}: ランタイム専用の置き場所に写しを置かない"


@pytest.mark.parametrize("name", sorted(PENDING))
def test_pending_skill_is_not_unified_yet(name: str) -> None:
    entry = CLAUDE_SKILLS / name
    assert entry.is_dir() and not entry.is_symlink(), f"{name} は一本化済み。PENDING から外す"
