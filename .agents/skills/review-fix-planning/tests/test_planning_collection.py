"""review-fix-planning の投稿の後に、判断の記録が仮想記録として残ることを確かめる（shim 経由の subprocess）。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.review_eval.store import Store
from tools.test_support.review_eval import make_collection_workspace, write_collection_config

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")

UPSERT_TESTS = Path(__file__).with_name("test_upsert_pr_comment.py")
FINALIZE_TESTS = Path(__file__).with_name("test_finalize.py")


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upsert_tests() -> Any:
    return _load(UPSERT_TESTS, "planning_collection_upsert_tests")


def test_round_record_is_kept_after_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """投稿した判断の記録を取らないと、後で上書きされた判断の履歴が黙って失われる。"""
    helpers = _upsert_tests()
    workspace, repos = make_collection_workspace(tmp_path / "base")
    store = Store.init(tmp_path / "store")
    config = write_collection_config(
        tmp_path / "review-eval.toml", store=store.root, workspace_root=workspace, repos=repos
    )
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(config))
    # shim が起動する uv run --project <checkout>/tools/devenv の環境を、checkout の外（一時領域）に作らせる
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "venv"))
    gh = helpers.FakeGh(pr_view=helpers.pr_view(), comments=[])
    monkeypatch.setattr(helpers.mod, "GhRunner", lambda: gh)
    record_file = tmp_path / "round.json"
    record_file.write_text(json.dumps(helpers.awaiting_record(), ensure_ascii=False, indent=2), encoding="utf-8")
    body_file = tmp_path / "body.md"
    body_file.write_text(helpers.BODY, encoding="utf-8")

    code = helpers.mod.main(
        [
            *("--pr", str(helpers.PR), "--repo", helpers.REPO, "--model-name", helpers.MODEL),
            *("--body-file", str(body_file), "--records-file", str(record_file)),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["action"] == "created"
    (observation,) = [
        o for o in store.observations(helpers.REPO, helpers.PR) if o.path == "@pr-comment/round-1/record.json"
    ]
    assert store.read_blob(observation.sha256) == record_file.read_bytes()


def test_finalize_keeps_the_round_record_it_posted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """finalize が投稿した判断の記録を取らないと、最終化の判断が黙って保存先から欠ける。"""
    helpers = _load(FINALIZE_TESTS, "planning_collection_finalize_tests")
    workspace, repos = make_collection_workspace(tmp_path / "base")
    store = Store.init(tmp_path / "store")
    config = write_collection_config(
        tmp_path / "review-eval.toml", store=store.root, workspace_root=workspace, repos=repos
    )
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(config))
    gh = helpers.FakeGh()
    monkeypatch.setattr(helpers.mod.poster, "GhRunner", lambda: gh)
    argv = helpers.cli_args(tmp_path, tmp_path / "result.json")

    code = helpers.mod.main(argv)

    assert code == 0
    record_file = tmp_path / "record.json"
    path = f"@pr-comment/round-{json.loads(record_file.read_bytes())['round']}/record.json"
    (observation,) = [o for o in store.observations(helpers.REPO, helpers.PR) if o.path == path]
    assert store.read_blob(observation.sha256) == record_file.read_bytes()


def test_finalize_collects_when_run_as_a_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本番の起動形で tools を import できないと、例外が捨てられて最終化の結果が黙って収集されない。"""
    workspace, repos = make_collection_workspace(tmp_path / "base")
    store = Store.init(tmp_path / "store")
    config = write_collection_config(
        tmp_path / "review-eval.toml", store=store.root, workspace_root=workspace, repos=repos
    )
    helpers = _load(FINALIZE_TESTS, "planning_collection_finalize_script_tests")
    argv = helpers.cli_args(tmp_path, tmp_path / "result.json")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "AGENT_RUNNER_DEPTH"}}
    env.update(REVIEW_EVAL_CONFIG=str(config), PATH=f"{fake_bin}{os.pathsep}{env['PATH']}")
    script = Path(helpers.HELPER)

    completed = subprocess.run(
        [sys.executable, str(script), *argv], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )

    assert "review-eval: hook failed" not in completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1])["outcome"] == "stopped"
    paths = {o.path for o in store.observations(helpers.REPO, helpers.PR)}
    assert f"@finalize/{helpers.HEAD[:12]}/result.json" in paths
