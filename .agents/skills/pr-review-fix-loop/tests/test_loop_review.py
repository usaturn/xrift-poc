"""レビュアーの起動・結果の検査・再利用・資源の回収と、2 サイクル以上の流れを確かめる。

レビュアーは runner の adapter を差し替えた偽の CLI（conftest の REVIEWER_PROGRAM）で、PR は bare remote と偽の gh。
計画担当（review-fix-planning）の代わりに、テストが記録の投稿・push・指定 commit のレビューの保存・マージを行う。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")


def registered_worktrees(root: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    return [line.removeprefix("worktree ") for line in listing.splitlines() if line.startswith("worktree ")]


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- 結果の検査 ---------------------------------------------------------------------------------------------------


def accepted_result(env: Any, tmp_path: Path) -> tuple[Result, str]:
    """検査を通る 1 人分の runner の結果（成果 2 件をレビューディレクトリに置く）。"""
    head = env.gh.head()
    review, candidates = env.place_review(head, "alpha-model", findings=1)
    request = Request(
        run_id="run-1",
        request_id="request-1",
        agent_id="alpha",
        role="inspect",
        work_dir=tmp_path / "work",
        prompt="review",
        deadline_seconds=60.0,
        result_shape="json",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    document = read(candidates)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "success",
        "run_id": "run-1",
        "request_id": "request-1",
        "agent_id": "alpha",
        "role": "inspect",
        "repository": "usaturn/xrift-poc",
        "pr_number": 900,
        "pr_head": head,
        "reviewed_head": head,
        "review_file": str(review),
        "findings_file": str(candidates),
        "active_findings": 1,
        "cleanup": "success",
        "cleanup_details": {},
        "retained_paths": None,
        "reason": None,
        "reviewer": document["reviewer"],
        "timing": document["timing"],
    }
    attempt = Attempt(
        agent_id="alpha",
        state="success",
        identity=Identity(model="alpha-model"),
        exit_code=0,
        elapsed_seconds=1.0,
        stop_confirmed=True,
        work_dir=tmp_path / "work",
        request_path=request_path,
        payload=payload,
    )
    result = Result(
        request_id="request-1",
        role="inspect",
        requested_agent="alpha",
        actual_agent="alpha",
        state="success",
        attempts=[attempt],
    )
    return result, head


def broken(result: Result, case: str, env: Any, tmp_path: Path) -> Result:
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    attempt_update: dict[str, Any] = {}
    result_update: dict[str, Any] = {}
    if case == "outer_failed":
        result_update["state"] = "timed_out"
    elif case == "stop_unconfirmed":
        attempt_update["stop_confirmed"] = False
    elif case == "payload_failed":
        payload["status"] = "failed"
    elif case == "request_mismatch":
        payload["request_id"] = "request-2"
    elif case == "actual_agent_mismatch":
        result_update["actual_agent"] = "beta"
    elif case == "other_pr":
        payload["pr_number"] = 901
    elif case == "reviewed_head_mismatch":
        payload["reviewed_head"] = "0" * 40
    elif case == "review_outside_dir":
        outside = tmp_path / "outside.md"
        outside.write_text(Path(payload["review_file"]).read_text(encoding="utf-8"), encoding="utf-8")
        payload["review_file"] = str(outside)
    elif case == "review_symlink":
        link = env.review_dir / "link.md"
        link.symlink_to(payload["review_file"])
        payload["review_file"] = str(link)
    elif case == "findings_invalid":
        Path(payload["findings_file"]).write_text("{}", encoding="utf-8")
    elif case == "pair_mismatch":
        review = Path(payload["review_file"])
        review.write_text(review.read_text(encoding="utf-8").split("## High")[0], encoding="utf-8")
    elif case == "active_count_mismatch":
        payload["active_findings"] = 0
    attempt_update["payload"] = payload
    attempts = [attempt.model_copy(update=attempt_update)]
    return result.model_copy(update={**result_update, "attempts": attempts})


@pytest.mark.parametrize(
    "case",
    [
        "outer_failed",
        "stop_unconfirmed",
        "payload_failed",
        "request_mismatch",
        "actual_agent_mismatch",
        "other_pr",
        "reviewed_head_mismatch",
        "review_outside_dir",
        "review_symlink",
        "findings_invalid",
        "pair_mismatch",
        "active_count_mismatch",
    ],
)
def test_verify_reviewer_rejects_each_broken_condition(loop: Any, env: Any, tmp_path: Path, case: str) -> None:
    result, head = accepted_result(env, tmp_path)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(broken(result, case, env, tmp_path), pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected"
    assert entry.reason


def test_verify_reviewer_accepts_a_complete_payload_and_records_hashes(loop: Any, env: Any, tmp_path: Path) -> None:
    result, head = accepted_result(env, tmp_path)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(result, pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "accepted", entry.reason
    assert entry.active_findings == 1
    assert entry.review_sha256 == loop.sha256_file(Path(entry.review_file))
    assert entry.findings_sha256 == loop.sha256_file(Path(entry.findings_file))
    assert entry.identity["model"] == "alpha-model"


PR_URL = "https://github.com/usaturn/xrift-poc/pull/900"


@pytest.mark.parametrize("change", ["payload_failed", "payload_unchanged"])
def test_after_run_result_is_reloaded_before_reviewer_validation(
    loop: Any, env: Any, tmp_path: Path, change: str
) -> None:
    # 後処理（after_run）が result.json を更新したら、レビュアーの検査はその保存された結果を使う
    program = (
        "import json,pathlib,sys; path=pathlib.Path(sys.argv[1]); batch=json.loads(path.read_text())\n"
        "for item in batch['results']:\n"
        "    if item['actual_agent'] == 'alpha' and " + repr(change) + " == 'payload_failed':\n"
        "        item['attempts'][-1]['payload'].update(status='failed', reason='後処理が保存を拒否した')\n"
        "path.write_text(json.dumps(batch))\n"
    )
    with env.template.open("a") as stream:
        stream.write(
            "\n[workflows.review]\nafter_run = " + json.dumps([[sys.executable, "-c", program, "{result}"]]) + "\n"
        )
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "failed"})
    if change == "payload_unchanged":
        reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
        assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]
        return
    # 後処理が却下させた alpha と、失敗した beta を 1 回だけ再実行する。再実行の結果にも同じ後処理がかかる
    pending = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert pending["rerun"] == {"agents": ["alpha", "beta"]}
    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (error.value.code, error.value.stage) == (6, "review")
    reviewers = read(run_dir / "cycle-1.json")["review"]["reviewers"]
    alpha = next(item for item in reviewers if item["actual_agent"] == "alpha")
    assert alpha["verdict"] == "rejected" and "後処理が保存を拒否した" in alpha["reason"]
    saved = read(run_dir / "cycle-1" / "rerun-result.json")
    saved_alpha = next(item for item in saved["results"] if item["actual_agent"] == "alpha")
    assert saved_alpha["attempts"][-1]["payload"]["status"] == "failed"


def test_review_executes_after_run_against_saved_result(loop: Any, env: Any, tmp_path: Path) -> None:
    marker = tmp_path / "hook-result.json"
    program = f"import pathlib,sys; pathlib.Path({str(marker)!r}).write_bytes(pathlib.Path(sys.argv[1]).read_bytes())"
    with env.template.open("a") as stream:
        stream.write(
            "\n[workflows.review]\nafter_run = " + json.dumps([[sys.executable, "-c", program, "{result}"]]) + "\n"
        )
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    reviewed = loop.review(
        run_dir,
        github=env.github,
        adapter_loader=env.reviewers({"alpha": "success", "beta": "success"}),
        tmp_root=env.tmp_root,
    )
    assert len(reviewed["accepted"]) == 2
    assert read(marker)["state"] == "success"
    reports = read(run_dir / "cycle-1.json")["review"]["after_run"]
    assert reports[0]["state"] == "success" and reports[0]["stop_confirmed"]


def test_reuse_counts_only_pairs_for_the_current_head(loop: Any, env: Any, kit: Any) -> None:
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")
    old = env.gh.head()
    env.place_review(old, "gamma")
    new = env.push("新しい commit")
    env.place_review(new, "alpha", commit=True)
    # gamma の本文を新しい HEAD のものに上書きした（候補一覧は古い HEAD のまま）
    env.place_review(new, "gamma")
    stale = env.review_dir / "findings" / f"gamma-{old[:12]}.json"
    assert stale.exists()

    reused_new = loop.find_reusable(env.review_dir, pr, new)
    reused_old = loop.find_reusable(env.review_dir, pr, old)

    assert sorted(Path(item.review_file).name for item in reused_new) == ["alpha-commit-" + new + ".md", "gamma.md"]
    assert reused_old == []


# --- レビューの起動 -----------------------------------------------------------------------------------------------


def test_review_refuses_when_the_head_moved_after_cycle_begin(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "success"})
    env.push("cycle-begin の後の push")

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (4, "head_moved")
    assert adapter.launched == []


def test_review_records_partial_failure_and_cleans_owned_worktrees(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "failed"}, findings=1)

    pending = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert pending["rerun"] == {"agents": ["beta"]}
    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert sorted(adapter.launched) == ["alpha", "beta", "beta"]
    assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]
    assert [item["agent"] for item in reviewed["rejected"]] == ["beta"]
    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    cycle = read(run_dir / "cycle-1.json")
    assert cycle["state"] == "planning"
    beta = next(item for item in cycle["review"]["reviewers"] if item["actual_agent"] == "beta")
    assert beta["verdict"] == "rejected" and beta["active_findings"] is None
    assert (run_dir / "cycle-1/review-result.json").is_file()
    run = read(run_dir / "run.json")
    assert [resource["state"] for resource in run["resources"]] == ["removed", "removed"]
    assert not any(Path(resource["path"]).exists() for resource in run["resources"])
    assert registered_worktrees(env.parent) == [str(env.parent)]


def test_review_with_no_accepted_reviewer_fails_with_code_6(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "failed", "beta": "failed"})
    # 全員が runner の成功の後に却下されたので、まず全員を 1 回だけ再実行する
    pending = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert pending["accepted"] == [] and pending["rerun"] == {"agents": ["alpha", "beta"]}

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (6, "review")
    assert error.value.next_command[-4:-1] == ["finish", "--run", str(run_dir)]
    cycle = read(run_dir / "cycle-1.json")
    assert [item["verdict"] for item in cycle["review"]["reviewers"]] == ["rejected", "rejected"]
    finished = loop.finish(run_dir, abort="external: レビュアーが全員失敗した")
    assert finished["lock_released"] is True


# --- 2 サイクル以上 -----------------------------------------------------------------------------------------------


def test_two_cycles_reuse_the_pre_push_review_and_end_merged(loop: Any, env: Any, kit: Any, tmp_path: Path) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])

    # サイクル 1: レビュアー 2 人（1 人は失敗）→ 計画が skip 1 件・fix 1 件を決め、修正を push する
    first = loop.cycle_begin(run_dir, github=env.github)
    assert first["review"]["mode"] == "run"
    head1 = first["head"]
    adapter1 = env.reviewers({"alpha": "success", "beta": "failed"}, findings=1)
    loop.review(run_dir, github=env.github, adapter_loader=adapter1, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter1, tmp_root=env.tmp_root)  # beta の再実行
    skip_ref = f"alpha-model#M-1@{head1[:12]}"
    fix_ref = f"alpha-model#H-1@{head1[:12]}"
    head2 = env.push("H-1 の修正")
    # 計画の push 前の PR 全体レビュー（指定 commit のレビュー）の成果が、レビューディレクトリに残る
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
    ended1 = loop.cycle_end(run_dir, github=env.github)
    assert ended1["next"][-3:] == ["cycle-begin", "--run", str(run_dir)]

    # サイクル 2: HEAD は push した commit。レビュアーを起動せずに push 前のレビューを使う
    adapter2 = env.reviewers({"alpha": "success", "beta": "success"})
    second = loop.cycle_begin(run_dir, github=env.github)
    assert second["review"]["mode"] == "reuse"
    assert [Path(item["review_file"]).name for item in second["review"]["reused"]] == [
        f"planner-model-commit-{head2}.md"
    ]
    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter2, tmp_root=env.tmp_root)
    assert adapter2.launched == []
    env.gh.post_record(
        kit.round_record(
            2,
            head2,
            "no_fix_needed",
            [
                kit.decision(skip_ref, "skip", "inherited"),
                kit.decision(fix_ref, "resolved", "changed", previous_state="fix"),
            ],
            seats=[kit.seat(head1, "alpha-model", rerun=False), kit.seat(head2, f"planner-model-commit-{head2}")],
        )
    )
    final = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)
    env.gh.merge()
    ended2 = loop.cycle_end(run_dir, github=env.github, finalize_result=final, round_dir=tmp_path)
    finished = loop.finish(run_dir)

    assert ended2["outcome"] == "no_fix_needed"
    assert finished["result"] == {
        "outcome": "no_fix_needed",
        "ended_by": "outcome",
        "stop_reason": None,
        "merged": True,
        "cycles": 2,
    }
    assert not (env.review_dir / "loop/lock.json").exists()
    # サイクルごとに参考データの索引が残る
    cycle1, cycle2 = read(run_dir / "cycle-1.json"), read(run_dir / "cycle-2.json")
    alpha = next(item for item in cycle1["review"]["reviewers"] if item["actual_agent"] == "alpha")
    assert alpha["findings_sha256"] == loop.sha256_file(Path(alpha["findings_file"]))
    assert alpha["identity"]["model"] == "alpha-model"
    assert cycle1["planning"]["decisions"]["state"] == {"skip": 1, "fix": 1}
    assert cycle2["review"]["reused"][0]["reviewed_head"] == head2
    assert cycle2["planning"]["decisions"]["change"] == {"inherited": 1, "changed": 1}
    assert cycle2["planning"]["finalize"]["round_dir"] == str(tmp_path.resolve())


def test_reviewers_receive_the_previous_round_when_the_head_moved(loop: Any, env: Any, kit: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    head1 = loop.cycle_begin(run_dir, github=env.github)["head"]
    loop.review(run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root)
    skip_ref = f"alpha-model#M-1@{head1[:12]}"
    pushed = env.push("修正")
    env.gh.post_record(
        kit.round_record(
            1,
            head1,
            "fixes_pushed",
            [kit.decision(skip_ref, "skip", "new"), kit.decision(f"alpha-model#H-1@{head1[:12]}", "fix", "new")],
            commit=pushed,
            seats=[kit.seat(head1, "alpha-model")],
        )
    )
    loop.cycle_end(run_dir, github=env.github)
    head3 = env.push("別の人の push")

    second = loop.cycle_begin(run_dir, github=env.github)
    assert second["review"]["mode"] == "run"
    assert second["notes"]
    loop.review(run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root)

    received = kit.records.read_state(kit.records.comments_from_api(read(env.root / "copies/alpha.json")))
    assert [entry["round"] for entry in received["rounds"]] == [1]
    assert received["decisions"][0]["state"] == "skip"
    env.gh.post_record(
        kit.round_record(
            2,
            head3,
            "stopped",
            [kit.decision(skip_ref, "skip", "inherited")],
            stop_reason="permission: 追加レビューの認可が無い",
            seats=[kit.seat(head3, "alpha-model")],
        )
    )
    ended = loop.cycle_end(run_dir, github=env.github)
    assert ended["outcome"] == "stopped"
    assert loop.finish(run_dir)["result"]["outcome"] == "stopped"


# --- 排他の保持・中断と再開 ---------------------------------------------------------------------------------------


def test_unconfirmed_stop_holds_the_lock_until_operator_recovery(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    real = loop.run_workflow

    def unconfirmed(*args: Any, **kwargs: Any) -> BatchResult:
        batch: BatchResult = real(*args, **kwargs)
        results = [
            item.model_copy(
                update={
                    "attempts": [attempt.model_copy(update={"stop_confirmed": False}) for attempt in item.attempts]
                }
            )
            for item in batch.results
        ]
        return batch.model_copy(update={"results": results})

    monkeypatch.setattr(loop, "run_workflow", unconfirmed)
    with pytest.raises(loop.LoopError) as review_error:
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    assert review_error.value.code == 6
    resource = Path(read(run_dir / "run.json")["resources"][0]["path"])
    assert resource.exists()

    with pytest.raises(loop.LoopError) as held:
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")
    assert (held.value.code, held.value.stage) == (7, "resources")
    assert read(run_dir / "run.json")["state"] == "held"
    with pytest.raises(loop.LoopError) as refused:
        loop.recover(run_dir)
    assert refused.value.code == 7
    assert (env.review_dir / "loop/lock.json").exists()

    recovered = loop.recover(run_dir, operator_confirmed_stopped="ps で runner の子プロセスが無いことを確かめた")

    assert recovered["lock_released"] is True
    assert not resource.exists()
    assert registered_worktrees(env.parent) == [str(env.parent)]
    run = read(run_dir / "run.json")
    assert run["state"] == "ended"
    assert run["resources"][0]["state"] == "removed"
    assert "ps で" in run["resources"][0]["reason"]


def test_interrupted_review_without_result_is_not_rerun(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success"})

    def interrupted(*args: Any, **kwargs: Any) -> BatchResult:
        Path(kwargs["output_dir"]).mkdir()
        raise KeyboardInterrupt

    real = loop.run_workflow
    monkeypatch.setattr(loop, "run_workflow", interrupted)
    with pytest.raises(KeyboardInterrupt):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    monkeypatch.setattr(loop, "run_workflow", real)

    with pytest.raises(loop.LoopError) as again:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (again.value.code, again.value.stage) == (3, "interrupted_review")
    assert again.value.next_command[-3:] == ["recover", "--run", str(run_dir)]
    assert adapter.launched == []
    assert loop.status(run_dir=run_dir)["next"][-3:] == ["review", "--run", str(run_dir)]

    recovered = loop.recover(run_dir, operator_confirmed_stopped="中断した helper のプロセスが無いことを確かめた")
    assert recovered["lock_released"] is True
    run = read(run_dir / "run.json")
    assert run["state"] == "abandoned"
    # runner の結果が無いと秘密が残りうるファイルを判別できないので、ディレクトリは残して報告する
    resource = run["resources"][0]
    assert resource["state"] == "retained" and Path(resource["path"]).exists()
    assert resource["path"] in recovered["retained"]
    assert registered_worktrees(env.parent) == [str(env.parent)]


def test_resume_after_review_continues_from_status(loop: Any, env: Any, kit: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    head = loop.cycle_begin(run_dir, github=env.github)["head"]
    loop.review(run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root)

    # helper のプロセスが変わった後も、保存した状態だけから次の操作が分かる
    resumed = loop.status(run_dir=run_dir)
    assert resumed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    env.gh.post_record(
        kit.round_record(1, head, "stopped", [], stop_reason="external: テスト", seats=[kit.seat(head, "alpha-model")])
    )
    assert loop.cycle_end(run_dir, github=env.github)["outcome"] == "stopped"
    assert loop.status(run_dir=run_dir)["next"][-3:] == ["finish", "--run", str(run_dir)]


def test_cycle_end_reads_the_roles_json_that_run_roles_writes(loop: Any, env: Any, kit: Any, tmp_path: Path) -> None:
    # run_roles.py は roles.json の batch を BatchResult.model_dump(mode="json") で書く（パスは文字列になる）。
    # 実機の確認で、strict な runner のモデルを Python モードで検証して読めなかった不具合の再現。
    started = env.start()
    run_dir = Path(started["run_dir"])
    head = env.gh.head()
    env.place_review(head)
    loop.cycle_begin(run_dir, github=env.github)
    attempt = Attempt(
        agent_id="alpha",
        state="success",
        identity=Identity(model="alpha-model", session_id="s-1"),
        exit_code=0,
        elapsed_seconds=3.0,
        stop_confirmed=True,
        work_dir=tmp_path / "implement/worktree",
        stdout_path=tmp_path / "implement/stdout.log",
        stderr_path=tmp_path / "implement/stderr.log",
        request_path=tmp_path / "implement/request.json",
        payload={"status": "completed"},
    )
    batch = BatchResult(
        run_id="run-implement",
        state="success",
        assignments={"implement": ["alpha"]},
        results=[
            Result(
                request_id="r-1",
                role="implement",
                requested_agent="alpha",
                actual_agent="alpha",
                state="success",
                attempts=[attempt],
            )
        ],
        outside_changes=[],
        elapsed_seconds=3.0,
        artifact_dir=tmp_path / "implement",
    )
    role_run = tmp_path / "implement"
    role_run.mkdir()
    (role_run / "roles.json").write_text(
        json.dumps(
            {"request": {}, "batch": batch.model_dump(mode="json"), "self_prompts": {}, "review_changes": None}
        ),
        encoding="utf-8",
    )
    pushed = env.push("修正")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))

    loop.cycle_end(run_dir, github=env.github, role_runs=[role_run])

    recorded = read(run_dir / "cycle-1.json")["planning"]["role_runs"]
    assert recorded[0]["assignments"] == {"implement": ["alpha"]}
    assert recorded[0]["results"][0]["actual_agent"] == "alpha"
    assert recorded[0]["results"][0]["identity"]["session_id"] == "s-1"


# --- 最終レビューの指摘の再現 ---------------------------------------------------------------------------------------


def unconfirmed_batch(loop: Any, monkeypatch: pytest.MonkeyPatch, sensitive: Path | None = None) -> None:
    """runner が停止を確認できなかった結果の形（stop_confirmed=false と、秘密が残りうる receipt）を返させる。"""
    real = loop.run_workflow

    def unconfirmed(*args: Any, **kwargs: Any) -> BatchResult:
        batch: BatchResult = real(*args, **kwargs)
        results: list[Result] = []
        for item in batch.results:
            attempts: list[Attempt] = []
            for attempt in item.attempts:
                paths: list[Path] = (
                    [attempt.request_path.parent / "receipt.json"] if attempt.request_path is not None else []
                )
                if sensitive is not None:
                    paths.append(sensitive)
                for path in paths:
                    path.write_text("{}", encoding="utf-8")
                attempts.append(attempt.model_copy(update={"stop_confirmed": False, "sensitive_paths": paths}))
            results.append(item.model_copy(update={"attempts": attempts}))
        return batch.model_copy(update={"results": results})

    monkeypatch.setattr(loop, "run_workflow", unconfirmed)


def has_placeholder(argv: list[str] | None) -> bool:
    return argv is not None and any("<" in item and ">" in item for item in argv)


def test_next_commands_never_carry_placeholders_and_recover_rejects_them(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    unconfirmed_batch(loop, monkeypatch)
    with pytest.raises(loop.LoopError) as failed:
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    assert failed.value.code == 6
    assert not has_placeholder(failed.value.next_command)
    with pytest.raises(loop.LoopError) as held:
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")
    with pytest.raises(loop.LoopError) as refused:
        loop.recover(run_dir)
    assert not has_placeholder(held.value.next_command)
    assert not has_placeholder(refused.value.next_command)

    with pytest.raises(loop.LoopError) as placeholder:
        loop.recover(run_dir, operator_confirmed_stopped="<確かめた事実>")
    assert placeholder.value.code == 2
    with pytest.raises(loop.LoopError):
        loop.finish(run_dir, abort="external: <理由>")
    assert (env.review_dir / "loop/lock.json").exists()


def test_recover_keeps_files_that_may_hold_secrets(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    unconfirmed_batch(loop, monkeypatch)
    with pytest.raises(loop.LoopError):
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    receipts = list(Path(read(run_dir / "run.json")["resources"][0]["path"]).rglob("receipt.json"))
    assert receipts
    with pytest.raises(loop.LoopError):
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")

    recovered = loop.recover(run_dir, operator_confirmed_stopped="ps で runner の子プロセスが無いことを確かめた")

    assert all(path.exists() for path in receipts)
    assert set(map(str, receipts)) <= set(recovered["retained"])
    assert registered_worktrees(env.parent) == [str(env.parent)]
    assert recovered["lock_released"] is True


def test_failed_cleanup_paths_are_reported(loop: Any, env: Any, tmp_path: Path) -> None:
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    payload.update(
        status="failed",
        cleanup="failed",
        reason="cleanup refused",
        retained_paths={"worktree": "/tmp/pr-review-900-x/worktree", "session_dir": "/tmp/pr-review-900-x"},
    )
    failed = result.model_copy(update={"attempts": [attempt.model_copy(update={"payload": payload})]})
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(failed, pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected"
    assert sorted(entry.retained_paths) == ["/tmp/pr-review-900-x", "/tmp/pr-review-900-x/worktree"]


def test_status_after_every_reviewer_failed_points_to_abort(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "failed", "beta": "failed"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    next_command = loop.status(run_dir=run_dir)["next"]
    assert "finish" in next_command and any(item.startswith("--abort=external: ") for item in next_command)
    assert not has_placeholder(next_command)
    # 再実行の後も全員が失敗したら、3 回目は起動しない
    with pytest.raises(loop.LoopError) as again:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (again.value.code, again.value.stage) == (6, "review")
    assert sorted(adapter.launched) == ["alpha", "alpha", "beta", "beta"]


def test_runner_refusing_before_launch_can_be_retried(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)

    def refused(*args: Any, **kwargs: Any) -> BatchResult:
        raise loop.RunnerError()

    real = loop.run_workflow
    monkeypatch.setattr(loop, "run_workflow", refused)
    with pytest.raises(loop.LoopError) as error:
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    monkeypatch.setattr(loop, "run_workflow", real)
    assert (error.value.code, error.value.stage) == (5, "runner")
    assert error.value.next_command[-3:] == ["review", "--run", str(run_dir)]
    assert [resource["state"] for resource in read(run_dir / "run.json")["resources"]] == ["removed"]

    reviewed = loop.review(
        run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
    )
    assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]


@pytest.mark.parametrize("case", ["other_pr_document", "symlink_inside_dir", "stale_body"])
def test_verify_reviewer_rejects_artifacts_that_do_not_belong_to_this_head(
    loop: Any, env: Any, kit: Any, tmp_path: Path, case: str
) -> None:
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    review, candidates = Path(payload["review_file"]), Path(payload["findings_file"])
    if case == "other_pr_document":
        document = read(candidates)
        document["pr"] = {"repository": "usaturn/xrift-poc", "number": 901}
        candidates.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    elif case == "symlink_inside_dir":
        target = env.review_dir / "real.md"
        review.rename(target)
        review.symlink_to(target)
    else:
        review.write_text(review.read_text(encoding="utf-8").replace(head[:12], "1" * 12), encoding="utf-8")
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(result, pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected", case


@pytest.mark.parametrize("entrypoint", ["cleanup", "audit"])
@pytest.mark.parametrize("locked", [False, True])
def test_missing_output_parent_does_not_hide_registered_worktrees(
    loop: Any, env: Any, tmp_path: Path, entrypoint: str, locked: bool
) -> None:
    from tools.agent_runner.workspace import create_worktree

    run_dir = Path(env.start()["run_dir"])
    run = loop.load_run(run_dir)
    parent = tmp_path / "xrift-poc-pr-loop-owned"
    worktree = create_worktree(env.parent, "HEAD", parent / "child\n日本語" / "worktree")
    if locked:
        subprocess.run(["git", "-C", str(env.parent), "worktree", "lock", str(worktree)], check=True)
    other = create_worktree(env.parent, "HEAD", tmp_path / "other-run" / "worktree")
    (other / "untracked").write_text("do not remove")
    resource = loop.Resource(path=str(parent), cycle=1, state="created")
    run.resources.append(resource)
    shutil.rmtree(parent)
    if entrypoint == "cleanup":
        assert loop.cleanup_output(env.parent, resource, None, force=True) == [str(worktree)]
    else:
        assert loop.audit_resources(run) == [str(parent)]
        # A retained resource still requires stop evidence on later calls.
        assert loop.audit_resources(run) == [str(parent)]
    assert resource.state == "retained"
    assert str(worktree) in [str(path) for path in loop._registered_worktrees(env.parent)]
    assert (other / "untracked").read_text() == "do not remove"
    assert not parent.exists()


@pytest.mark.parametrize("entrypoint", ["cleanup", "audit"])
@pytest.mark.parametrize("failure", ["inaccessible", "dangling", "git_unavailable"])
def test_missing_output_checks_fail_closed(loop: Any, env: Any, tmp_path: Path, entrypoint: str, failure: str) -> None:
    run = loop.load_run(Path(env.start()["run_dir"]))
    ancestor = tmp_path / "protected"
    ancestor.mkdir()
    parent = ancestor / "xrift-poc-pr-loop-owned"
    gitdir = env.parent / ".git"
    moved_gitdir = env.parent / "hidden-git"
    if failure == "inaccessible":
        parent.mkdir()
        ancestor.chmod(0)
    elif failure == "dangling":
        parent.symlink_to(tmp_path / "absent-target", target_is_directory=True)
    else:
        gitdir.rename(moved_gitdir)
    resource = loop.Resource(path=str(parent), cycle=1, state="created")
    run.resources.append(resource)
    try:
        result = (
            loop.cleanup_output(env.parent, resource, None, force=True)
            if entrypoint == "cleanup"
            else loop.audit_resources(run)
        )
        assert result == [str(parent)] and resource.state != "removed"
    finally:
        if failure == "inaccessible":
            ancestor.chmod(0o755)
        elif failure == "git_unavailable":
            moved_gitdir.rename(gitdir)


@pytest.mark.parametrize("entrypoint", ["cleanup", "audit"])
def test_missing_parent_with_no_registration_is_removed(loop: Any, env: Any, tmp_path: Path, entrypoint: str) -> None:
    run = loop.load_run(Path(env.start()["run_dir"]))
    resource = loop.Resource(path=str(tmp_path / "xrift-poc-pr-loop-gone"), cycle=1, state="created")
    run.resources.append(resource)
    result = loop.cleanup_output(env.parent, resource, None) if entrypoint == "cleanup" else loop.audit_resources(run)
    assert result == [] and resource.state == "removed"


# --- 未実施レビュアーの再実行 -------------------------------------------------------------------------------------


def started_cycle(loop: Any, env: Any) -> Path:
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    return run_dir


def test_reviewer_rejected_after_runner_success_is_rerun_once(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed", "success"], "beta": "success"})

    first = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert first["rerun"] == {"agents": ["alpha"]}
    assert first["next"][-3:] == ["review", "--run", str(run_dir)]
    assert [item["agent"] for item in first["accepted"]] == ["beta"]
    assert read(run_dir / "cycle-1.json")["state"] == "reviewing"
    assert loop.status(run_dir=run_dir)["next"][-3:] == ["review", "--run", str(run_dir)]

    second = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert sorted(adapter.launched) == ["alpha", "alpha", "beta"]
    assert sorted(item["agent"] for item in second["accepted"]) == ["alpha", "beta"]
    assert second["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    cycle = loop.load_cycles(run_dir)[-1]
    assert cycle.state == "planning"
    assert [entry.verdict for entry in cycle.review.reviewers] == ["accepted", "accepted"]
    replaced = cycle.review.rerun.replaced
    assert [(entry.actual_agent, entry.rejected_stage) for entry in replaced] == [("alpha", "payload")]
    kept = Path(replaced[0].request_file)
    assert kept.parent == run_dir / "cycle-1" / "requests"
    assert loop.sha256_file(kept) == replaced[0].request_sha256
    assert Request.model_validate_json(kept.read_text(encoding="utf-8")).agent_id == "alpha"
    assert (run_dir / "cycle-1" / "rerun-result.json").is_file()
    run = read(run_dir / "run.json")
    assert [resource["state"] for resource in run["resources"]] == ["removed", "removed"]
    assert registered_worktrees(env.parent) == [str(env.parent)]


def test_runner_level_failure_is_not_rerun(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "crash"})

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    assert "rerun" not in reviewed
    cycle = loop.load_cycles(run_dir)[-1]
    assert cycle.review.rerun is None and cycle.state == "planning"
    beta = next(entry for entry in cycle.review.reviewers if entry.actual_agent == "beta")
    assert beta.rejected_stage == "runner" and Path(beta.request_file).is_file()
    assert sorted(adapter.launched) == ["alpha", "beta"]


def record_attempts(loop: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """runner の各試行の状態と準備の stderr を、review の回収で消える前に 1 行ずつ写す（失敗の表示に使う）。"""
    notes: list[str] = []
    real = loop.run_workflow

    def recording(*args: Any, **kwargs: Any) -> BatchResult:
        batch: BatchResult = real(*args, **kwargs)
        for result in batch.results:
            for attempt in result.attempts:
                log = attempt.request_path.parent / "worktree.stderr.log" if attempt.request_path else None
                stderr = log.read_text(encoding="utf-8", errors="replace") if log and log.is_file() else "無い"
                notes.append(
                    f"{attempt.agent_id}: state={attempt.state} exit_code={attempt.exit_code} "
                    f"stop_confirmed={attempt.stop_confirmed} 準備の stderr={stderr!r}"
                )
        return batch

    monkeypatch.setattr(loop, "run_workflow", recording)
    return notes


def test_rerun_that_fails_again_continues_with_the_accepted_reviewers(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 負荷の高い verify で 2 つの形で落ちたが、原因が分からない（#694）。次に落ちたとき、各レビュアーの
    # 拒否の理由と runner の試行（準備の stderr を含む）が表示に出るようにする
    run_dir = started_cycle(loop, env)
    notes = record_attempts(loop, monkeypatch)
    adapter = env.reviewers({"alpha": "success", "beta": "failed"})

    def review(number: int) -> dict[str, Any]:
        try:
            return loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
        except loop.LoopError as error:
            pytest.fail(f"{number} 回目の review が失敗した（{error.stage}）: {error.reason}\n" + "\n".join(notes))

    first = review(1)
    explained = f"rejected={first['rejected']}\n" + "\n".join(notes)
    assert first.get("rerun") == {"agents": ["beta"]}, explained
    assert [(item["agent"], item["rejected_stage"]) for item in first["rejected"]] == [("beta", "payload")], explained

    second = review(2)

    assert [item["agent"] for item in second["accepted"]] == ["alpha"]
    assert [item["agent"] for item in second["rejected"]] == ["beta"]
    assert second["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    cycle = loop.load_cycles(run_dir)[-1]
    assert cycle.state == "planning" and len(cycle.review.rerun.replaced) == 1
    assert sorted(adapter.launched) == ["alpha", "beta", "beta"], "\n".join(notes)


def test_rerun_is_not_started_while_another_attempt_is_unconfirmed(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 再実行は依存する仕事なので、1 回目の batch の全試行の停止を確かめてからでないと始めない
    run_dir = started_cycle(loop, env)
    real = loop.run_workflow

    def alpha_unconfirmed(*args: Any, **kwargs: Any) -> BatchResult:
        batch: BatchResult = real(*args, **kwargs)
        results = [
            item.model_copy(
                update={
                    "attempts": [attempt.model_copy(update={"stop_confirmed": False}) for attempt in item.attempts]
                }
            )
            if item.actual_agent == "alpha"
            else item
            for item in batch.results
        ]
        return batch.model_copy(update={"results": results})

    monkeypatch.setattr(loop, "run_workflow", alpha_unconfirmed)
    adapter = env.reviewers({"alpha": "success", "beta": ["failed", "success"]})

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (6, "review")
    assert loop.load_cycles(run_dir)[-1].review.rerun is None
    assert sorted(adapter.launched) == ["alpha", "beta"]
    assert Path(read(run_dir / "run.json")["resources"][0]["path"]).exists()


def test_rerun_refuses_when_the_head_moved(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed", "success"], "beta": "success"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    env.push("1 回目のレビューの後の push")

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (4, "head_moved")
    assert sorted(adapter.launched) == ["alpha", "beta"]


def test_interrupted_rerun_is_not_relaunched(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed", "success"], "beta": "success"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    def interrupted(*args: Any, **kwargs: Any) -> BatchResult:
        kwargs["output_dir"].mkdir()
        raise KeyboardInterrupt

    monkeypatch.setattr(loop, "run_workflow", interrupted)
    with pytest.raises(KeyboardInterrupt):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    monkeypatch.undo()

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (error.value.code, error.value.stage) == (3, "interrupted_review")
    assert sorted(adapter.launched) == ["alpha", "beta"]


def test_cycle_record_without_the_rerun_fields_loads(loop: Any, env: Any) -> None:
    run_dir = started_cycle(loop, env)
    loop.review(
        run_dir,
        github=env.github,
        adapter_loader=env.reviewers({"alpha": "success", "beta": "crash"}),
        tmp_root=env.tmp_root,
    )
    path = run_dir / "cycle-1.json"
    record = read(path)
    # この変更より前の helper が書いた記録には、再実行と調査用の欄（試行の要約を含む）と、runner の run ID の欄が無い
    record["review"].pop("rerun")
    record["review"].pop("probe")
    record["review"].pop("runner_run_id", None)
    for entry in record["review"]["reviewers"]:
        for key in ("rejected_stage", "request_file", "request_sha256", "attempts", "probe"):
            entry.pop(key)
    path.write_text(json.dumps(record), encoding="utf-8")

    cycle = loop.load_cycles(run_dir)[-1]

    assert cycle.review.rerun is None
    assert cycle.review.probe is None
    assert all(
        entry.rejected_stage is None and entry.attempts == [] and entry.probe is None
        for entry in cycle.review.reviewers
    )
    finished = loop.finish(run_dir, abort="external: テストの終了")
    assert finished["reviewer_retained"] == [] and finished["cycles"][0]["outside_changes"] == []


def test_request_is_kept_only_from_a_regular_file(loop: Any, env: Any, tmp_path: Path) -> None:
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    payload = {**cast(dict[str, Any], attempt.payload), "status": "failed"}
    target = tmp_path / "elsewhere.json"
    target.write_text(cast(Path, attempt.request_path).read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "linked-request.json"
    link.symlink_to(target)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")
    kept = tmp_path / "kept"
    # 停止を確かめられない試行のファイルは、生きた書き手が差し替えうるので読まない
    for request_path, stopped, expected in (
        (link, True, False),
        (tmp_path / "missing.json", True, False),
        (attempt.request_path, False, False),
        (attempt.request_path, True, True),
    ):
        update = {"payload": payload, "request_path": request_path, "stop_confirmed": stopped}
        failed = result.model_copy(update={"attempts": [attempt.model_copy(update=update)]})
        entry = loop.verify_reviewer(failed, pr=pr, head=head, review_dir=env.review_dir)
        loop.keep_rejected_request(failed, entry, kept)
        assert (entry.request_file is not None) is expected
    assert sorted(path.name for path in kept.iterdir()) == ["request-1.json"]


# --- 再実行の後の報告 ---------------------------------------------------------------------------------------------


def session_worktrees(env: Any) -> list[str]:
    """偽のレビュアーが pr-review の helper のセッションを模して残した worktree（作った順）。"""
    worktrees = [path / "worktree" for path in env.sessions.glob("pr-review-*")]
    return [str(path) for path in sorted(worktrees, key=lambda path: path.stat().st_mtime_ns)]


def test_rerun_keeps_the_first_attempt_retained_paths_and_outside_changes(loop: Any, env: Any) -> None:
    # Issue #41 の再現: 1 回目の alpha は後片付けに失敗して worktree を残し、tool_root も書き換える。再実行は成功する
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed+retained+outside", "success"], "beta": "success"})

    first = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    [worktree] = session_worktrees(env)
    assert worktree in first["retained"]
    assert first["outside_changes"] == [str(env.parent)]

    second = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert sorted(item["agent"] for item in second["accepted"]) == ["alpha", "beta"]
    assert set(first["retained"]) <= set(second["retained"])
    assert second["outside_changes"] == [str(env.parent)]


def test_successful_first_review_reports_nothing_retained(loop: Any, env: Any) -> None:
    # 対照: 1 回目から全員が成功する
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "success"})

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (reviewed["retained"], reviewed["outside_changes"]) == ([], [])
    assert "rerun" not in reviewed
    finished = loop.finish(run_dir, abort="external: テストの終了")
    assert (finished["retained"], finished["reviewer_retained"]) == ([], [])
    assert finished["cycles"][0]["outside_changes"] == []


def finish_no_fix_needed(loop: Any, env: Any, kit: Any, run_dir: Path, tmp_path: Path) -> dict[str, Any]:
    """計画担当の代わりに no_fix_needed の記録とマージを置き、cycle-end から finish まで進める。"""
    env.gh.post_record(kit.round_record(1, env.gh.head(), "no_fix_needed", []))
    final = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)
    env.gh.merge()
    loop.cycle_end(run_dir, github=env.github, finalize_result=final, round_dir=tmp_path)
    finished: dict[str, Any] = loop.finish(run_dir)
    return finished


def retained_item(output: dict[str, Any], path: str) -> dict[str, Any]:
    return next(item for item in output["reviewer_retained"] if item["path"] == path)


def test_finish_and_status_report_the_first_attempt_worktree(loop: Any, env: Any, kit: Any, tmp_path: Path) -> None:
    # Issue #41 の再現を最終化まで進める: 1 回目の worktree が実在し登録も残るので、finish と status に出る
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed+retained+outside", "success"], "beta": "success"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    finished = finish_no_fix_needed(loop, env, kit, run_dir, tmp_path)

    [worktree] = session_worktrees(env)
    assert retained_item(finished, worktree) == {
        "path": worktree,
        "exists": True,
        "registered": True,
        "observed": [{"cycle": 1, "agent": "alpha", "attempt": "first", "source": "payload"}],
    }
    ref = retained_item(finished, f"refs/pr-review/{kit.number}")
    assert (ref["exists"], ref["registered"]) == (None, None)
    assert finished["retained"] == [] and finished["lock_released"] is True
    assert finished["cycles"][0]["outside_changes"] == [str(env.parent)]
    status = loop.status(run_dir=run_dir)
    assert (status["state"], status["next"]) == ("ended", None)
    assert status["reviewer_retained"] == finished["reviewer_retained"]
    # 報告のために消さない。パスに改行を含むので、-z で読む loop の一覧で照合する
    assert Path(worktree).is_dir() and Path(worktree) in loop._registered_worktrees(env.parent)


def test_rerun_that_fails_again_reports_both_attempts(loop: Any, env: Any, kit: Any) -> None:
    # beta は再実行でも失敗し、alpha の成果で計画へ進む。どちらの試行の残存パスも報告する
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": "success", "beta": "failed+retained"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    first, rerun = session_worktrees(env)
    assert {first, rerun} <= set(reviewed["retained"])

    finished = loop.finish(run_dir, abort="external: テストの終了")

    observed = {item["path"]: item["observed"] for item in finished["reviewer_retained"]}
    assert observed[first] == [{"cycle": 1, "agent": "beta", "attempt": "first", "source": "payload"}]
    assert observed[rerun] == [{"cycle": 1, "agent": "beta", "attempt": "rerun", "source": "payload"}]
    assert [item["attempt"] for item in observed[f"refs/pr-review/{kit.number}"]] == ["first", "rerun"]


def test_reviewer_retained_reports_the_state_after_removal(loop: Any, env: Any, kit: Any) -> None:
    # 報告された worktree を finish の前に外すと、過去の観測は残り、現在の状態は無い・未登録になる
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed+retained", "success"], "beta": "success"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    [worktree] = session_worktrees(env)
    kit.git(env.parent, "worktree", "remove", "--force", worktree)

    finished = loop.finish(run_dir, abort="external: テストの終了")

    removed = retained_item(finished, worktree)
    assert (removed["exists"], removed["registered"]) == (False, False)
    assert removed["observed"] == [{"cycle": 1, "agent": "alpha", "attempt": "first", "source": "payload"}]
    session = retained_item(finished, str(Path(worktree).parent))
    assert (session["exists"], session["registered"]) == (True, False)


@pytest.mark.parametrize("entrypoint", ["finish", "recover"])
def test_pending_rerun_does_not_count_the_same_attempt_twice(loop: Any, env: Any, entrypoint: str) -> None:
    # 再実行の前に終えると、置き換える予定の記録が reviewers と rerun.replaced の両方にある
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed+retained", "success"], "beta": "success"})
    pending = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert pending["rerun"] == {"agents": ["alpha"]}

    ended = (
        loop.finish(run_dir, abort="external: 再実行の前に止めた") if entrypoint == "finish" else loop.recover(run_dir)
    )

    [worktree] = session_worktrees(env)
    assert retained_item(ended, worktree)["observed"] == [
        {"cycle": 1, "agent": "alpha", "attempt": "first", "source": "payload"}
    ]


def test_reviewer_retained_survives_an_unreadable_worktree_list(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = started_cycle(loop, env)
    adapter = env.reviewers({"alpha": ["failed+retained", "success"], "beta": "success"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    def unreadable(root: Path) -> list[Path]:
        raise loop.LoopError(5, "git", "git worktree list が失敗した")

    monkeypatch.setattr(loop, "_registered_worktrees", unreadable)
    finished = loop.finish(run_dir, abort="external: テストの終了")

    [worktree] = session_worktrees(env)
    item = retained_item(finished, worktree)
    assert (item["exists"], item["registered"]) == (True, None)
    assert finished["lock_released"] is True


@pytest.mark.parametrize("terminal", ["finish", "recover"])
def test_legacy_cycle_preserves_runner_ids_and_retained_paths(loop: Any, env: Any, terminal: str) -> None:
    """旧 cycle の識別値や残存パスを落とすと、中断後の調査・回収に必要な記録が黙って失われる。"""
    run_dir = started_cycle(loop, env)
    path = run_dir / "cycle-1.json"
    original = read(path)
    legacy = original["review"]
    legacy["runner_run_id"] = uuid.uuid4().hex
    legacy["cleanup"] = {"state": "retained", "retained": [str(run_dir / uuid.uuid4().hex / "parent-pr-data")]}
    legacy["rerun"] = {
        "agents": ["alpha"],
        "replaced": [],
        "runner_run_id": uuid.uuid4().hex,
        "cleanup": {
            "state": "retained",
            "retained": [str(run_dir / uuid.uuid4().hex / "rerun-parent-pr-data")],
        },
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    [cycle] = loop.load_cycles(run_dir)
    loop.save_cycle(run_dir, cycle)
    assert loop.status(run_dir=run_dir)["status"] == "ok"
    result = (
        loop.finish(run_dir, abort="external: legacy record compatibility")
        if terminal == "finish"
        else loop.recover(run_dir)
    )

    saved = read(path)["review"]
    for part in ("review", "rerun"):
        before = legacy if part == "review" else legacy[part]
        after = saved if part == "review" else saved[part]
        assert after["runner_run_id"] == before["runner_run_id"]
        assert after["cleanup"] == before["cleanup"]
        assert set(before["cleanup"]["retained"]).issubset(result["retained"])
