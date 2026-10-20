"""runner の段階で失敗したレビュアーの記録（試行の所要時間・診断の要約）を確かめる（#96）。

レビュアーは conftest の偽のレビュアーで、crash は receipt を書かずに終了コード 3 で止まり、stdout に provider の
エラー文の代わりの error イベントを出す。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner.models import BatchResult

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


def unconfirmed(batch: BatchResult, agent: str | None = None) -> BatchResult:
    """agent（None なら全員）の試行を、停止を確認できなかったものに書き換える。"""
    results = [
        item.model_copy(
            update={"attempts": [step.model_copy(update={"stop_confirmed": False}) for step in item.attempts]}
        )
        if agent is None or item.actual_agent == agent
        else item
        for item in batch.results
    ]
    return batch.model_copy(update={"results": results})


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


# --- 同じ担当での短い確認 -----------------------------------------------------------------------------------------


def test_runner_failure_is_probed_once_with_the_same_agent(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "crash"})

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (sorted(adapter.launched), adapter.probed) == (["alpha", "beta"], ["beta"])
    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    cycle = read(run_dir / "cycle-1.json")
    beta = reviewer(cycle, "beta")
    # 確認はレビューではない: verdict・段階・理由は runner の失敗のまま
    assert (beta["verdict"], beta["rejected_stage"]) == ("rejected", "runner")
    probe = beta["probe"]
    assert (probe["state"], probe["first_line_matched"]) == ("passed", True)
    assert probe["attempt"]["state"] == "success" and probe["attempt"]["elapsed_seconds"] > 0
    assert probe["after_failure_seconds"] >= 0
    assert reviewer(cycle, "alpha")["probe"] is None
    assert next(item for item in reviewed["rejected"] if item["agent"] == "beta")["probe"] == probe
    assert cycle["review"]["probe"]["batch"]["result_file"] == str(run_dir / "cycle-1" / "probe-result.json")
    assert (run_dir / "cycle-1" / "probe-result.json").is_file()
    run = read(run_dir / "run.json")
    assert [(item["kind"], item["state"]) for item in run["resources"]] == [
        ("runner_output", "removed"),
        ("probe_output", "removed"),
    ]
    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert adapter.probed == ["beta"]
    summary = loop.finish(run_dir, abort="external: テストの終了")["cycles"][0]["reviewers"]
    assert next(item for item in summary if item["agent"] == "beta")["probe"] == probe


@pytest.mark.parametrize(
    "answer,attempt_state",
    [("crash", "response_failed"), ("wrong", "success"), ("hang", "timed_out")],
)
def test_probe_failure_or_timeout_does_not_stop_the_loop(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch, answer: str, attempt_state: str
) -> None:
    monkeypatch.setattr(loop, "PROBE_DEADLINE_SECONDS", 1.0)
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "crash"}, probes={"beta": answer})

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    beta = reviewer(read(run_dir / "cycle-1.json"), "beta")
    assert (beta["verdict"], beta["probe"]["state"]) == ("rejected", "failed")
    assert beta["probe"]["attempt"]["state"] == attempt_state


def test_every_runner_failure_is_probed_before_code_6(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "crash", "beta": "crash"})

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (6, "review")
    assert sorted(adapter.probed) == ["alpha", "beta"]
    assert error.value.reason.count("短い確認: passed") == 2
    summary = loop.finish(run_dir, abort="external: テストの終了")["cycles"][0]["reviewers"]
    assert [item["probe"]["state"] for item in summary] == ["passed", "passed"]


def test_no_probe_for_accepted_or_payload_rejections(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed", "success"], "beta": "success"})

    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert adapter.probed == []
    cycle = loop.load_cycles(run_dir)[-1]
    assert cycle.review.probe is None and cycle.review.rerun.probe is None
    assert all(entry.probe is None for entry in [*cycle.review.reviewers, *cycle.review.rerun.replaced])


def test_probe_is_skipped_when_a_stop_is_unconfirmed(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = started_cycle(loop, env)
    real = loop.run_workflow

    def beta_unconfirmed(*args: Any, **kwargs: Any) -> Any:
        return unconfirmed(real(*args, **kwargs), "beta")

    monkeypatch.setattr(loop, "run_workflow", beta_unconfirmed)
    adapter = env.reviewers({"alpha": "success", "beta": "crash"})

    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert adapter.probed == []
    assert reviewer(read(run_dir / "cycle-1.json"), "beta")["probe"]["state"] == "skipped"


def test_runner_failure_in_the_rerun_is_probed(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed", "crash"], "beta": "success"})

    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert adapter.probed == []
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert adapter.probed == ["alpha"]
    cycle = read(run_dir / "cycle-1.json")
    assert reviewer(cycle, "alpha")["probe"]["state"] == "passed"
    assert cycle["review"]["rerun"]["probe"]["batch"]["result_file"] == str(
        run_dir / "cycle-1" / "rerun-probe-result.json"
    )


def probe_runs_only(loop: Any, monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    """workflow が probe の run_workflow の呼び出しだけを change で置き換える。"""
    real = loop.run_workflow

    def wrapped(config: Any, workflow: str, *args: Any, **kwargs: Any) -> Any:
        if workflow != loop.PROBE_WORKFLOW:
            return real(config, workflow, *args, **kwargs)
        return change(real, config, workflow, *args, **kwargs)

    monkeypatch.setattr(loop, "run_workflow", wrapped)


def test_interrupted_probe_is_not_relaunched(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "crash"})

    def interrupted(real: Any, config: Any, workflow: str, *args: Any, **kwargs: Any) -> Any:
        kwargs["output_dir"].mkdir()
        raise KeyboardInterrupt

    probe_runs_only(loop, monkeypatch, interrupted)
    with pytest.raises(KeyboardInterrupt):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    monkeypatch.undo()
    assert loop.status(run_dir=run_dir)["next"][-3:] == ["review", "--run", str(run_dir)]

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert adapter.probed == []
    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    assert reviewer(read(run_dir / "cycle-1.json"), "beta")["probe"]["state"] == "unknown"
    with pytest.raises(loop.LoopError) as held:
        loop.finish(run_dir, abort="external: テストの終了")
    assert (held.value.code, held.value.stage) == (7, "resources")
    recovered = loop.recover(run_dir, operator_confirmed_stopped="中断した helper のプロセスが無いことを確かめた")
    assert recovered["lock_released"] is True


def test_unconfirmed_probe_attempt_is_unknown(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "crash"})

    def probe_unconfirmed(real: Any, config: Any, workflow: str, *args: Any, **kwargs: Any) -> Any:
        return unconfirmed(real(config, workflow, *args, **kwargs))

    probe_runs_only(loop, monkeypatch, probe_unconfirmed)

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    assert reviewer(read(run_dir / "cycle-1.json"), "beta")["probe"]["state"] == "unknown"
    with pytest.raises(loop.LoopError) as held:
        loop.finish(run_dir, abort="external: テストの終了")
    assert (held.value.code, held.value.stage) == (7, "resources")
