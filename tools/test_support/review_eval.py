"""review-eval のテスト補助: 設定・親と制作物の作業場所。"""

import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from tools.review_eval.config import ROUTES
from tools.test_support.repos import ENV_ORIGIN, create_env_repo, create_product

FAKE_SCANNER: list[str] = [
    sys.executable,
    "-c",
    "import sys; open(sys.argv[2], 'w').write('[]')",
    "{source}",
    "{report}",
]


def make_collection_workspace(base_dir: Path, products: Sequence[str] = ()) -> tuple[Path, list[str]]:
    """親の main checkout を作り、`products` の各 `owner/repo` を origin 付きの制作物として `items/` に作る。

    戻り値は `(workspace_root, [親の repository, *products])`。
    """
    env = create_env_repo(base_dir)
    for repo in products:
        create_product(env.root, "items", repo.split("/", 1)[1], origin_url=f"https://github.com/{repo}.git")
    parent = ENV_ORIGIN.removeprefix("https://github.com/").removesuffix(".git")
    return env.root, [parent, *products]


def write_collection_config(
    path: Path,
    *,
    store: Path,
    workspace_root: Path,
    repos: Sequence[str],
    routes: Sequence[str] = ROUTES,
    scanner: Sequence[str] | None = FAKE_SCANNER,
    max_store_bytes: int = 64 * 1024 * 1024,
    **overrides: object,
) -> Path:
    values: dict[str, object] = {
        "schema_version": 1,
        "enabled": True,
        "approved_by": "test",
        "store": str(store),
        "workspace_root": str(workspace_root),
        "max_store_bytes": max_store_bytes,
    }
    if scanner is not None:
        values["secret_scanner"] = list(scanner)
    values.update(overrides)
    lines = [f"{key} = {json.dumps(value)}" for key, value in values.items()]
    lines.insert(2, f"approved_on = {date.today().isoformat()}")
    for repo in repos:
        lines += ["", "[[sources]]", f"repo = {json.dumps(repo)}", f"routes = {json.dumps(list(routes))}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
