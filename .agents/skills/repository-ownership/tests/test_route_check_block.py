"""repository-ownership の判定手順のコードブロックを、実際の環境リポジトリと制作物で実行する。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from tools.test_support.repos import create_env_repo, create_product

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL = REPO_ROOT / ".agents/skills/repository-ownership/SKILL.md"


def route_check_block() -> str:
    match = re.search(
        r"```bash\n(# repository-ownership route check\n.*?)\n```", SKILL.read_text(encoding="utf-8"), re.DOTALL
    )
    assert match is not None, "route-check bash block not found in SKILL.md"
    return match.group(1)


def test_route_check_block_routes_env_product_and_uninitialized(tmp_path: Path) -> None:
    env = create_env_repo(tmp_path / "workspace")
    scripts = env.root / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("route_target.py", "review_target.py", "agent_tmp.py"):
        shutil.copy2(REPO_ROOT / ".agents" / "scripts" / name, scripts / name)
    product = create_product(env.root, "worlds", "my-world", origin_url="https://github.com/someone/my-world.git")
    plain = create_product(env.root, "items", "my-item", git=False)
    script = tmp_path / "route-check.bash"
    script.write_text(route_check_block() + "\n", encoding="utf-8")

    def run(cwd: Path) -> tuple[int, dict[str, object]]:
        result = subprocess.run(
            ["bash", str(script)],
            cwd=cwd,
            env={**os.environ, "XRIFT_POC_ROOT": str(env.root)},
            capture_output=True,
            text=True,
        )
        return result.returncode, json.loads(result.stdout.strip().splitlines()[-1])

    assert run(env.root)[1]["kind"] == "env"
    status, route = run(product)
    assert (status, route["kind"], route["repository"]) == (0, "product", "someone/my-world")
    status, route = run(plain)
    assert (status, route["kind"], route["code"]) == (3, "stop", "product_uninitialized")
