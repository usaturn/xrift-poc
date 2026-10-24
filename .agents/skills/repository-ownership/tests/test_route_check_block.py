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


def test_route_check_block_routes_by_route_path_or_cwd(tmp_path: Path) -> None:
    """判定のブロックが ROUTE_PATH・cwd を渡さなくなり、制作物の変更を親と判定して親へ起票・PR を作る。"""
    env = create_env_repo(tmp_path / "workspace")
    scripts = env.root / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("route_target.py", "review_target.py", "agent_tmp.py"):
        shutil.copy2(REPO_ROOT / ".agents" / "scripts" / name, scripts / name)
    product = create_product(env.root, "worlds", "my-world", origin_url="https://github.com/someone/my-world.git")
    script = tmp_path / "route-check.bash"
    script.write_text(route_check_block() + "\n", encoding="utf-8")

    def run(cwd: Path, route_path: str | None = None) -> tuple[int, dict[str, object]]:
        environ = {key: value for key, value in os.environ.items() if key != "ROUTE_PATH"}
        environ["XRIFT_POC_ROOT"] = str(env.root)
        if route_path is not None:
            environ["ROUTE_PATH"] = route_path
        result = subprocess.run(["bash", str(script)], cwd=cwd, env=environ, capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout.strip().splitlines()[-1])

    # ROUTE_PATH が無ければ cwd（$PWD）で判定する。
    status, route = run(product)
    assert (status, route["kind"], route["repository"]) == (0, "product", "someone/my-world")
    # ROUTE_PATH には、これから作るファイルも指定できる（親のルートから起動していても、制作物で判定する）。
    status, route = run(env.root, "worlds/my-world/src/new.tsx")
    assert (status, route["kind"], route["repository"]) == (0, "product", "someone/my-world")
