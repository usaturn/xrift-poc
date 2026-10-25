"""入口 3 の書き込み先の制作物の列挙を、SKILL.md のブロックのまま bash で実行する。

``route_target.py`` が ``product`` と判定し ``repository`` が PR のリポジトリと一致する制作物だけを返す。
結果が空になる方向の誤り（zsh の一致しない glob、``items/``・``worlds/`` の片方が無い親、大文字小文字）は、
書き込まずに報告するだけなので確かめない。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tools.test_support.repos import create_env_repo, create_product

REPO_ROOT = Path(__file__).resolve().parents[4]
WORLD_URL = "https://github.com/someone/my-world.git"
ITEM_URL = "https://github.com/someone/my-item.git"


def make_parent(tmp_path: Path) -> Path:
    """書き込み先の制作物 1 つと、書き込み先にならない制作物 3 つを置いた親の main checkout。"""
    env = create_env_repo(tmp_path / "parent")
    scripts = env.root / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("route_target.py", "review_target.py", "agent_tmp.py"):
        shutil.copy2(REPO_ROOT / ".agents" / "scripts" / name, scripts / name)
    create_product(env.root, "worlds", "my-world", origin_url=WORLD_URL)
    # 別のリポジトリの制作物、origin の無い制作物（product だが repository が null）、Git 未初期化の制作物（stop）
    create_product(env.root, "items", "my-item", origin_url=ITEM_URL)
    create_product(env.root, "worlds", "no-origin")
    create_product(env.root, "items", "plain", git=False)
    return env.root


def test_write_targets_find_the_matching_product(skill_shell: Any, tmp_path: Path) -> None:
    """照合が緩むと、PR と別のリポジトリの制作物を書き込み先にし、管理外のリポジトリへ Issue・コメントを書く。"""
    parent = make_parent(tmp_path)
    result = skill_shell.run(
        "bash",
        skill_shell.block("write targets"),
        cwd=parent,
        env={"PARENT": str(parent), "XRIFT_POC_ROOT": str(parent), "PR_REPO": "someone/my-world"},
    )
    assert result.returncode == 0, result.stderr
    found = [(entry["repository"], Path(entry["project_root"]).name) for entry in json.loads(result.stdout)]
    assert found == [("someone/my-world", "my-world")]
