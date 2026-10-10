"""PR 修正ループのスキルが、明示的に起動したときだけ使われ、参照する helper と文書が実在することを確かめる。"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL = SKILL_DIR / "SKILL.md"
LOCAL_REFERENCE = re.compile(r"(?<![\w/.-])(?:scripts/[A-Za-z0-9_]+\.py|references/[A-Za-z0-9_-]+\.md)")


def frontmatter_lines(skill: Path) -> list[str]:
    return skill.read_text(encoding="utf-8").split("---", 2)[1].splitlines()


# 修正ループは push・マージまで進むので、通常のレビューや計画の依頼からモデルが自分で起動しないよう、
# 利用者が明示的に起動したときだけ使う設定にしている。値を変えるときは、このテストも合わせて更新する。
def test_loop_skill_runs_only_when_explicitly_invoked() -> None:
    assert "disable-model-invocation: true" in frontmatter_lines(SKILL)


def test_referenced_helper_and_documents_exist() -> None:
    names = set(LOCAL_REFERENCE.findall(SKILL.read_text(encoding="utf-8")))

    assert "scripts/loop.py" in names
    for name in sorted(names):
        assert (SKILL_DIR / name).is_file(), name
