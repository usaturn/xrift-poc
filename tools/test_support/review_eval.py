"""review-eval のテスト補助: 設定・親と制作物の作業場所。"""

import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from tools.review_eval.config import ROUTES
from tools.review_eval.models import FormatInfo, Observation, TargetInfo, observation_identity
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


def make_observation(
    sha256: str,
    size: int,
    *,
    repository: str = "o/r",
    pr: int = 1,
    review_dir: str | None = "#1-b",
    path: str = "model.md",
    observed_at: str = "2026-10-30T00:00:00Z",
) -> Observation:
    return Observation(
        schema="review-eval-observation/v1",
        observation_id=observation_identity(repository, pr, review_dir, path, sha256),
        repository=repository,
        pr=pr,
        review_dir=review_dir,
        path=path,
        sha256=sha256,
        bytes=size,
        body="stored",
        format=FormatInfo(schema=None, kind=None, known=False),
        target=TargetInfo(reviewed_head=None, base=None, source_key=None, missing_reason=None),
        observed_at=observed_at,
        file_mtime=None,
        trigger="save",
        route="pr-review",
        event="test",
        context={},
        inputs=[],
        collector={"name": "test"},
    )


def put_revisions_worker(store_root: str, revisions: Sequence[bytes]) -> list[bool]:
    """spawn されたプロセスで各 revision を put し、observation を作れたかを返す。"""
    from tools.review_eval.store import Store

    store = Store.open(Path(store_root))
    created: list[bool] = []
    for data in revisions:
        sha = store.put_blob(data)
        created.append(store.put_observation(make_observation(sha, len(data))))
    return created
