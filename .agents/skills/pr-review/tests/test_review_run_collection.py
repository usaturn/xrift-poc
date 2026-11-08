"""pr-review の保存点で review 記録を収集しても、通常のレビューの結果を変えないことを確かめる。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import hooks
from tools.review_eval.capture import collect
from tools.review_eval.store import Store
from tools.test_support.repos import create_product
from tools.test_support.review_eval import write_collection_config


def _load_base() -> Any:
    path = Path(__file__).with_name("test_review_run.py")
    spec = importlib.util.spec_from_file_location("test_review_run_base", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = _load_base()

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")

prepared = base.prepared  # fixture を再利用する
REPOSITORY, mark_reviewed, review_run = base.REPOSITORY, base.mark_reviewed, base.review_run

VOLATILE = {
    "timing",
    "cleanup_details",
    "state_file",
    "draft",
    "findings_draft",
    "worktree",
    "retained_paths",
    "run_record",
}


def _enable(prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, init: bool) -> Store:
    parent = prepared["parent"]
    create_product(parent, "items", REPOSITORY.split("/", 1)[1], origin_url=f"https://github.com/{REPOSITORY}.git")
    store_root = tmp_path / "store"
    store = Store.init(store_root) if init else Store(store_root)
    config = write_collection_config(
        tmp_path / "review-eval.toml", store=store_root, workspace_root=parent, repos=["usaturn/xrift-poc", REPOSITORY]
    )
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(config))
    return store


def _after_run_attach(source: Path, parent: Path) -> None:
    """実際の after_run の経路（hooks.run_after_run と CLI）を、fixture の親の main checkout に向けて実行する。"""
    launcher = (
        "import importlib.util, sys; from pathlib import Path; "
        f"spec = importlib.util.spec_from_file_location('hook_review_run', {str(base.SCRIPT)!r}); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        f"module.workspace.load_review_target().default_parent_root = lambda: Path({str(parent)!r}); "
        "raise SystemExit(module.main(sys.argv[1:]))"
    )
    command = [sys.executable, "-c", launcher, "attach-runner", "--result", "{result}"]
    reports = hooks.run_after_run([command], result_path=source, cwd=parent, cancel=threading.Event())
    assert reports[0]["state"] == "success", Path(str(reports[0]["stderr_path"])).read_text()


def _stable(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key not in VOLATILE and not key.endswith("seconds")}


def _review_once(prepared: dict[str, Any], summary: str) -> tuple[int, dict[str, Any]]:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    draft = Path(result["draft"])
    draft.write_text(draft.read_text(encoding="utf-8").replace("全体差分を確認。", summary), encoding="utf-8")
    code = review_run.main(["complete", "--state", result["state_file"]])
    return code, result


def test_complete_result_is_unchanged_when_collection_fails(
    prepared: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """収集が失敗しても complete の stdout・終了コードが変わらない。無いと、レビューの判定が黙って変わる。"""
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", "")
    code_off, _ = _review_once(prepared, "全体差分を確認。")
    off = capsys.readouterr()
    _enable(prepared, tmp_path, monkeypatch, init=False)

    code_on, _ = _review_once(prepared, "全体差分を確認。")
    on = capsys.readouterr()

    assert code_on == code_off == 0
    assert _stable(json.loads(on.out)) == _stable(json.loads(off.out))
    assert "review-eval:" not in off.err
    assert [line for line in on.err.splitlines() if line.startswith("review-eval:")] != []


def test_rereview_keeps_previous_review_revision(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """再レビューで上書きされる前の版を保存する。無いと、前の版が黙って失われ取り返せない。"""
    store = _enable(prepared, tmp_path, monkeypatch, init=True)
    _review_once(prepared, "全体差分を確認。")
    review_file = prepared["parent"] / json.loads(capsys.readouterr().out)["review_file"]
    first_bytes = review_file.read_bytes()

    _review_once(prepared, "2 回目: 指摘はない。\n別の本文。")
    capsys.readouterr()

    revisions = [o for o in store.observations() if o.path == "observed-model.md"]
    assert len(revisions) == 2
    assert first_bytes in [store.read_blob(o.sha256) for o in revisions]
    assert review_file.read_bytes() != first_bytes


def test_runner_attachment_collects_the_review_saved_by_a_runner_child(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """runner の子が保存したレビューを attach-runner が取る。無いと、runner 経由のレビューが黙って収集されない。"""
    store = _enable(prepared, tmp_path, monkeypatch, init=True)
    # レビュアーは runner の子として保存し、attach-runner は runner の after_run が起動する（どちらも depth が付く）
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", "1")
    result = base.completed_review(prepared)
    assert store.observations() == []

    code, _ = base.attach(base.batch_result(tmp_path / "result.json"), capsys)

    assert code == 0
    run_record = Path(result["run_record"])
    review_dir = run_record.parent.parent
    stored = {(o.path, store.read_blob(o.sha256)) for o in store.observations() if o.body == "stored"}
    assert (run_record.relative_to(review_dir).as_posix(), run_record.read_bytes()) in stored
    review_file = prepared["parent"] / result["destination"]
    assert (review_file.relative_to(review_dir).as_posix(), review_file.read_bytes()) in stored


def test_after_run_finishes_a_stalled_collection_and_can_retry(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停滞した収集を attach-runner が期限内に終え、後で取り直せる。無いと、取れなかった版が黙って失われる。"""
    store = _enable(prepared, tmp_path, monkeypatch, init=True)
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", "1")
    result = base.completed_review(prepared)
    review_file = prepared["parent"] / result["destination"]
    review_bytes = review_file.read_bytes()
    review_dir = Path(result["run_record"]).parent.parent
    config_path = Path(os.environ["REVIEW_EVAL_CONFIG"])
    write_collection_config(
        config_path,
        store=store.root,
        workspace_root=prepared["parent"],
        repos=["usaturn/xrift-poc", REPOSITORY],
        scanner=[sys.executable, "-c", "import time; time.sleep(75)"],
        scan_timeout_seconds=120,
    )

    # run_after_run は起動時環境を使う。本テストで承認した一時設定を子へ渡す。
    monkeypatch.setattr(hooks, "literal_environ", lambda: dict(os.environ))
    _after_run_attach(base.batch_result(tmp_path / "result.json"), prepared["parent"])

    assert store.observations() == []
    pending = [failure for _, failure, unresolved in store.failures() if unresolved]
    assert any(failure.path == review_file.name and failure.sha256 for failure in pending)
    write_collection_config(
        config_path, store=store.root, workspace_root=prepared["parent"], repos=["usaturn/xrift-poc", REPOSITORY]
    )
    monkeypatch.delenv("AGENT_RUNNER_DEPTH")
    collect(route="pr-review", event="retry", review_dir=review_dir)

    assert (review_file.name, review_bytes) in [(o.path, store.read_blob(o.sha256)) for o in store.observations()]
    assert not any(unresolved for _, _, unresolved in store.failures())
