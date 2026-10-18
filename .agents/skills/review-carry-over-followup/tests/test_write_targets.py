"""入口 3 の書き込み先の制作物の列挙を、SKILL.md のブロックのまま bash と zsh で実行する。

``items/``・``worlds/`` の片方が空・無い親（制作物を片方だけ作った checkout）でも、``route_target.py`` が
``product`` と判定し ``repository`` が PR のリポジトリと一致する制作物だけを返す。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.repos import create_env_repo, create_product

REPO_ROOT = Path(__file__).resolve().parents[4]
SHELLS = ("bash", "zsh")
WORLD_URL = "https://github.com/someone/my-world.git"
ITEM_URL = "https://github.com/someone/my-item.git"


def make_parent(tmp_path: Path, layout: str) -> Path:
    """``layout`` の制作物を置いた親の main checkout。どの構成にも、書き込み先にならない制作物を混ぜない。"""
    env = create_env_repo(tmp_path / "parent")
    scripts = env.root / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("route_target.py", "review_target.py", "agent_tmp.py"):
        shutil.copy2(REPO_ROOT / ".agents" / "scripts" / name, scripts / name)
    if layout in ("worlds-only", "no-items", "both"):
        create_product(env.root, "worlds", "my-world", origin_url=WORLD_URL)
    if layout in ("items-only", "both"):
        create_product(env.root, "items", "my-item", origin_url=ITEM_URL)
    if layout == "no-items":
        (env.root / "items").rmdir()
    if layout == "both":
        # origin の無い制作物（product だが repository が null）と、Git 未初期化の制作物（stop）
        create_product(env.root, "worlds", "no-origin")
        create_product(env.root, "items", "plain", git=False)
    return env.root


def write_targets(skill_shell: Any, parent: Path, shell: str, pr_repo: str) -> list[tuple[str, str]]:
    result = skill_shell.run(
        shell,
        skill_shell.block("write targets"),
        cwd=parent,
        env={"PARENT": str(parent), "XRIFT_POC_ROOT": str(parent), "PR_REPO": pr_repo},
    )
    assert result.returncode == 0, result.stderr
    return [(entry["repository"], Path(entry["project_root"]).name) for entry in json.loads(result.stdout)]


@pytest.mark.parametrize(
    ("layout", "pr_repo", "expected"),
    [
        ("worlds-only", "someone/my-world", [("someone/my-world", "my-world")]),
        ("no-items", "someone/my-world", [("someone/my-world", "my-world")]),
        ("items-only", "someone/my-item", [("someone/my-item", "my-item")]),
        ("both", "someone/my-world", [("someone/my-world", "my-world")]),
        ("both", "Someone/My-Item", [("someone/my-item", "my-item")]),
        ("both", "someone/other", []),
    ],
)
@pytest.mark.parametrize("shell", SHELLS)
def test_write_targets_find_the_matching_product(
    skill_shell: Any, tmp_path: Path, shell: str, layout: str, pr_repo: str, expected: list[tuple[str, str]]
) -> None:
    parent = make_parent(tmp_path, layout)
    assert write_targets(skill_shell, parent, shell, pr_repo) == expected
