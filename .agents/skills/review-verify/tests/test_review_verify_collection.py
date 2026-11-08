"""review_findings.py validate が、同じ HEAD の再確認で置き換わる前の verified を収集することを確かめる。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.review_eval.store import Store
from tools.test_support.review_eval import make_collection_workspace, write_collection_config

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")

ROOT = Path(__file__).resolve().parents[4]
FINDINGS_TESTS = ROOT / ".agents/scripts/tests/test_review_findings.py"


def _document(title: str) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("review_findings_builders", FINDINGS_TESTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    document: dict[str, Any] = module.document_v2("verified")
    document["verifier"]["model"] = title
    return document


def test_validate_keeps_published_revision_before_same_head_rerun(tmp_path: Path) -> None:
    """validate が同じ HEAD の再確認で置き換わる前の verified を保存する。無いと、公開済みの版が黙って失われる。"""
    workspace, repos = make_collection_workspace(tmp_path / "workspace")
    config = write_collection_config(
        tmp_path / "review-eval.toml",
        store=tmp_path / "store",
        workspace_root=workspace,
        repos=repos,
    )
    Store.init(tmp_path / "store")
    verified_dir = workspace / "reviews/usaturn/xrift-poc/#542-b/verified"
    verified_dir.mkdir(parents=True)
    published = verified_dir / "30f27c22a4a2.json"
    original = (json.dumps(_document("first"), ensure_ascii=False, indent=2) + "\n").encode()
    published.write_bytes(original)
    rerun = verified_dir / ".30f27c22a4a2.json"
    rerun.write_text(json.dumps(_document("second"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    env = {key: value for key, value in os.environ.items() if key != "AGENT_RUNNER_DEPTH"}
    env["REVIEW_EVAL_CONFIG"] = str(config)
    # shim が起動する uv run --project <checkout>/tools/devenv の環境を、checkout の外（一時領域）に作らせる
    env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path / "venv")
    result = subprocess.run(
        ["uv", "run", "--no-project", "python", ".agents/scripts/review_findings.py", "validate", str(rerun)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    os.replace(rerun, published)

    store = Store.open(tmp_path / "store")
    assert original in [store.read_blob(o.sha256) for o in store.observations("usaturn/xrift-poc", 542)]
