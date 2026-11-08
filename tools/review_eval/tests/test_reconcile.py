"""reconcile が未観測のファイルと loop の失われた revision を見つけ、`--apply` で取れるものだけを取ると確かめる。"""

import hashlib
import json
from pathlib import Path

import pytest

from tools.review_eval.capture import collect
from tools.review_eval.cli import main
from tools.review_eval.store import Store
from tools.test_support.review_eval import make_collection_workspace, write_collection_config

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")

RUN_ID = "0123456789abcdef0123456789abcdef"
HEAD = "9" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _review_markdown(body: str) -> bytes:
    return f"# レビュー\n\n## Medium\n\n### M-1 指摘\n\n{body}\n".encode()


def test_reconcile_detects_lost_loop_revision_and_applies_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """reconcile が失われた版と未観測の版を見落とすと、上書きで消えた版の取りこぼしが黙って残る。"""
    root, repos = make_collection_workspace(tmp_path / "base")
    repository = repos[0]
    owner, name = repository.split("/")
    review_dir = root / "reviews" / owner / name / "#5-feat"
    review_dir.mkdir(parents=True)
    store = Store.init(tmp_path / "store")
    config = write_collection_config(tmp_path / "review-eval.toml", store=store.root, workspace_root=root, repos=repos)
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(config))
    review = review_dir / "claude-opus-5-5.md"
    referenced = _review_markdown("loop のサイクルが参照した版\n2 つ目の主張: 改行を含む")
    review.write_bytes(referenced)
    cycle = review_dir / "loop" / RUN_ID / "cycle-1.json"
    cycle.parent.mkdir(parents=True)
    record = {
        "schema": "pr-review-fix-loop/v1",
        "kind": "cycle",
        "run_id": RUN_ID,
        "cycle": 1,
        "head": HEAD,
        "state": "planning",
        "review": {
            "mode": "run",
            "reviewers": [
                {
                    "actual_agent": "alpha",
                    "verdict": "accepted",
                    "review_file": str(review),
                    "review_sha256": _sha256(referenced),
                }
            ],
            "reused": [],
        },
        "planning": None,
    }
    cycle.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    # サイクルの記録だけを取り、参照先の版は取らないまま書き換える（後片付け・上書きで失われた版）
    collect(route="pr-review-fix-loop", event="review", review_dir=review_dir, files=[cycle], sweep=False)
    current = _review_markdown("後で書き換えた版")
    review.write_bytes(current)

    assert main(["reconcile", "--repo", repository]) == 1
    before = json.loads(capsys.readouterr().out)
    assert [(item["path"], item["sha256"]) for item in before["lost_revisions"]] == [
        (review.name, _sha256(referenced))
    ]
    assert [(item["path"], item["sha256"]) for item in before["unobserved"]] == [(review.name, _sha256(current))]

    assert main(["reconcile", "--repo", repository, "--apply"]) == 1
    after = json.loads(capsys.readouterr().out)
    assert after["unobserved"] == []
    assert [(item["path"], item["sha256"]) for item in after["lost_revisions"]] == [(review.name, _sha256(referenced))]
    assert any(o.path == review.name and o.sha256 == _sha256(current) for o in store.observations())
    assert not store.has_blob(_sha256(referenced))
