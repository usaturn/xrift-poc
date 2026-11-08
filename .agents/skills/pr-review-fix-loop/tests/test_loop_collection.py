"""loop の保存点の収集が、サイクルの記録と成果の対応を残し、再利用した成果を二重登録しないことを確かめる。

手順は test_loop_review.py の 2 サイクル（2 サイクル目は push 前のレビューの再利用）と同じで、収集を有効にして
実行する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.review_eval.config import load_config
from tools.review_eval.inventory import reconcile
from tools.review_eval.store import Store
from tools.test_support.review_eval import write_collection_config

REPOSITORY = "usaturn/xrift-poc"
NUMBER = 900
pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")


def test_loop_cycles_link_outputs_without_double_registration(
    loop: Any, env: Any, kit: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再利用したサイクルの成果を二重に登録すると、同じ版が複数の観測になり、後の集計がずれて黙って壊れる。"""
    workspace = env.parent.resolve()
    store = Store.init(tmp_path / "store")
    config_path = write_collection_config(
        tmp_path / "review-eval.toml", store=store.root, workspace_root=workspace, repos=[REPOSITORY]
    )
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(config_path))
    started = env.start()
    run_dir = Path(started["run_dir"])
    review_dir = Path(started["review_dir"])

    head1 = loop.cycle_begin(run_dir, github=env.github)["head"]
    adapter1 = env.reviewers({"alpha": "success", "beta": "failed"}, findings=1)
    loop.review(run_dir, github=env.github, adapter_loader=adapter1, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter1, tmp_root=env.tmp_root)  # beta の再実行
    skip_ref = f"alpha-model#M-1@{head1[:12]}"
    fix_ref = f"alpha-model#H-1@{head1[:12]}"
    head2 = env.push("H-1 の修正")
    env.place_review(head2, "planner-model", commit=True)
    env.gh.post_record(
        kit.round_record(
            1,
            head1,
            "fixes_pushed",
            [kit.decision(skip_ref, "skip", "new"), kit.decision(fix_ref, "fix", "new")],
            commit=head2,
            seats=[kit.seat(head1, "alpha-model")],
        )
    )
    loop.cycle_end(run_dir, github=env.github)

    assert loop.cycle_begin(run_dir, github=env.github)["review"]["mode"] == "reuse"
    record = kit.round_record(
        2,
        head2,
        "no_fix_needed",
        [
            kit.decision(skip_ref, "skip", "inherited"),
            kit.decision(fix_ref, "resolved", "changed", previous_state="fix"),
        ],
        seats=[kit.seat(head1, "alpha-model", rerun=False), kit.seat(head2, f"planner-model-commit-{head2}")],
    )
    record_file = tmp_path / "round-2.json"
    record_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    env.gh.post_record(record)
    final = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)
    env.gh.merge()
    loop.cycle_end(run_dir, github=env.github, finalize_result=final, round_dir=tmp_path, record_file=record_file)
    loop.finish(run_dir)

    observations = store.observations(REPOSITORY, NUMBER)
    observed = {(o.path, o.sha256) for o in observations}
    for number in (1, 2):
        cycle = run_dir / f"cycle-{number}.json"
        assert (cycle.relative_to(review_dir).as_posix(), loop.sha256_file(cycle)) in observed
    reused = json.loads((run_dir / "cycle-2.json").read_text(encoding="utf-8"))["review"]["reused"]
    assert reused
    for item in reused:
        for key in ("review", "findings"):
            path = Path(item[f"{key}_file"]).relative_to(review_dir).as_posix()
            matches = [o for o in observations if o.path == path and o.sha256 == item[f"{key}_sha256"]]
            assert len(matches) == 1, path
    (kept,) = [o for o in observations if o.path == "@pr-comment/round-2/record.json"]
    assert store.read_blob(kept.sha256) == record_file.read_bytes()
    report = reconcile(store, load_config(config_path), repository=REPOSITORY, apply=False)
    assert report["unobserved"] == []
    assert report["lost_revisions"] == []
