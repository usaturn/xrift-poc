"""スキルの置き場所の規則（AGENTS.md「スキルの置き場所」）を、すべてのスキルについて確かめる。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

from tools.test_support.repos import create_env_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_SKILLS = REPO_ROOT / ".agents/skills"
CLAUDE_SKILLS = REPO_ROOT / ".claude/skills"
# 写しを置かない、ランタイム専用の置き場所。
RUNTIME_SKILL_DIRS = tuple(
    REPO_ROOT / runtime / "skills" for runtime in (".devin", ".cognition", ".codex", ".cursor", ".pi")
)
# まだ一本化していないスキル（親 #564 の各子が担当する）。一本化する変更で、ここから外す。
PENDING = frozenset[str]()


def tracked_skill_names() -> list[str]:
    """Git が追跡している ``.agents/skills/<name>/`` の名前。"""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", ".agents/skills"],
        capture_output=True,
        check=True,
    )
    paths = [Path(item.decode()) for item in result.stdout.split(b"\0") if item]
    return sorted({path.parts[2] for path in paths if len(path.parts) > 3})


def test_tracked_skill_names_includes_a_skill_with_only_skill_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKILL.md 単独のスキルを探索から落とすと、frontmatter が壊れても検査を黙ってすり抜ける。"""
    root = create_env_repo(tmp_path / "env", copy_paths=()).root
    skill = root / ".agents" / "skills" / tmp_path.name
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text(f"---\nname: {skill.name}\ndescription: 探索するスキル\n---\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", str(skill_md.relative_to(root))], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", root)

    assert skill.name in tracked_skill_names()


def unified_names() -> list[str]:
    return [name for name in tracked_skill_names() if name not in PENDING]


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


@pytest.mark.parametrize("name", tracked_skill_names())
def test_body_is_a_real_skill_directory(name: str) -> None:
    """frontmatter が壊れると、Claude Code は description で起動できず、pi・Codex はヘッドレスで黙って読み込まない。"""
    meta = frontmatter(AGENTS_SKILLS / name / "SKILL.md")
    assert meta.get("name") == name
    description = meta.get("description")
    assert isinstance(description, str) and description.strip(), f"{name}: description は空でない文字列にする"


@pytest.mark.parametrize("name", unified_names())
def test_claude_entry_links_to_the_body(name: str) -> None:
    """リンクが壊れると Claude Code は黙ってスキルを持たず、絶対パスなら worktree で main checkout の版を読む。"""
    entry = CLAUDE_SKILLS / name
    assert entry.is_symlink(), f"{name}: .claude/skills/{name} は本体へのリンクにする"
    assert not os.path.isabs(os.readlink(entry)), f"{name}: リンクは相対パスにする"
    assert entry.resolve() == (AGENTS_SKILLS / name).resolve()


@pytest.mark.parametrize("name", sorted(path.name for path in CLAUDE_SKILLS.iterdir()))
def test_claude_has_no_skill_of_its_own(name: str) -> None:
    """.claude/skills にだけあるスキルは、.agents/skills を読む pi・Codex から黙って見えない。"""
    assert (AGENTS_SKILLS / name).is_dir(), f"{name}: .claude にだけあるスキルは作らない"


@pytest.mark.parametrize("name", unified_names())
def test_no_runtime_copies(name: str) -> None:
    """ランタイム専用の置き場所の写しは名前の衝突で先に見つかった方が使われ、古い写しが黙って使われる。"""
    copies = [str(path) for base in RUNTIME_SKILL_DIRS if (path := base / name).exists() or path.is_symlink()]
    assert copies == [], f"{name}: ランタイム専用の置き場所に写しを置かない"


@pytest.mark.parametrize("name", tracked_skill_names())
def test_skill_text_has_no_numbered_positional_references(name: str) -> None:
    """Claude Code が番号付き参照を Skill 引数へ置換すると、秘密検査が別ファイルを読んで通りうる。"""
    skill = AGENTS_SKILLS / name / "SKILL.md"
    assert re.search(r"\$[0-9]", skill.read_text(encoding="utf-8")) is None, str(skill)
