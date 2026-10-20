"""runner の段階で失敗したレビュアーの記録（試行の所要時間・診断の要約）を確かめる（#96）。

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
    # runner が知る秘密（名前に API_KEY を含む環境変数の値）が provider のエラー文に出る
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", SECRET)
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers(
        {"alpha": "success", "beta": "crash"}, errors={"beta": f"provider が {SECRET} を拒否した\n日本語の詳細"}
    )

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    cycle = read(run_dir / "cycle-1.json")
    beta = reviewer(cycle, "beta")
    assert (beta["verdict"], beta["rejected_stage"]) == ("rejected", "runner")
    [attempt] = beta["attempts"]
    assert (attempt["agent_id"], attempt["state"], attempt["exit_code"]) == ("beta", "response_failed", 3)
    assert attempt["stop_confirmed"] is True and attempt["elapsed_seconds"] > 0
    assert attempt["diagnosis"]["ending"] == "error_event"
    assert attempt["diagnosis"]["message"] == "provider が [REDACTED] を拒否した\n日本語の詳細"
    assert reviewer(cycle, "alpha")["attempts"] == []
    assert next(item for item in reviewed["rejected"] if item["agent"] == "beta")["attempts"] == beta["attempts"]
    for text in (
        (run_dir / "cycle-1.json").read_text(encoding="utf-8"),
        (run_dir / "cycle-1" / "review-result.json").read_text(encoding="utf-8"),
        json.dumps(reviewed, ensure_ascii=False),
    ):
        assert SECRET not in text
    summary = loop.finish(run_dir, abort="external: テストの終了")["cycles"][0]["reviewers"]
    assert next(item for item in summary if item["agent"] == "beta")["attempts"] == beta["attempts"]


def test_rejection_after_runner_success_has_no_attempt_summary(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed", "success"], "beta": "success"})

    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    alpha = reviewer(read(run_dir / "cycle-1.json"), "alpha")
    assert (alpha["rejected_stage"], alpha["attempts"]) == ("payload", [])
