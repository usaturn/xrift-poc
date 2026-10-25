"""runner の段階で失敗したレビュアーの診断の伏せ字と、短い確認の扱いを確かめる（#96）。

レビュアーは conftest の偽のレビュアーで、crash は receipt を書かずに終了コード 3 で止まり、stdout に provider の
エラー文の代わりの error イベントを出す。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")

SECRET = "loop-secret-value"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def started_cycle(loop: Any, env: Any) -> Path:
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    return run_dir


def reviewer(cycle: dict[str, Any], agent: str) -> dict[str, Any]:
    return next(item for item in cycle["review"]["reviewers"] if item["actual_agent"] == agent)


def test_runner_failure_records_attempt_time_and_redacted_diagnosis(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider のエラー文に出た秘密が伏せ字の前の形で loop の記録・出力（cycle-N.json・review-result.json・review と
    finish の JSON）に入ると、司令塔の文脈と最終報告に秘密が出る。
    """
    # runner が知る秘密（名前に API_KEY を含む環境変数の値）が provider のエラー文に出る
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", SECRET)
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers(
        {"alpha": "success", "beta": "crash"}, errors={"beta": f"provider が {SECRET} を拒否した\n日本語の詳細"}
    )

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    finished = loop.finish(run_dir, abort="external: テストの終了")

    [attempt] = reviewer(read(run_dir / "cycle-1.json"), "beta")["attempts"]
    # 診断が記録まで届いている（秘密が無いのは記録が空だったからではない）
    assert "日本語の詳細" in attempt["diagnosis"]["message"]
    for text in (
        (run_dir / "cycle-1.json").read_text(encoding="utf-8"),
        (run_dir / "cycle-1" / "review-result.json").read_text(encoding="utf-8"),
        json.dumps(reviewed, ensure_ascii=False),
        json.dumps(finished, ensure_ascii=False),
    ):
        assert SECRET not in text


def test_passed_probe_does_not_accept_a_failed_reviewer(loop: Any, env: Any) -> None:
    """短い確認が通ったレビュアーを受け付けに変えると、全員が runner の段階で失敗したサイクルで止まらず、
    レビューの無いまま計画とマージへ進む。
    """
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "crash", "beta": "crash"})

    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    cycle = read(run_dir / "cycle-1.json")
    assert [item["probe"]["state"] for item in cycle["review"]["reviewers"]] == ["passed", "passed"]
    assert cycle["state"] == "reviewing"
