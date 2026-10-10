"""レビュアーの起動・結果の検査・再利用・資源の回収と、2 サイクル以上の流れを確かめる。

レビュアーは runner の adapter を差し替えた偽の CLI（conftest の REVIEWER_PROGRAM）で、PR は bare remote と偽の gh。
計画担当（review-fix-planning）の代わりに、テストが記録の投稿・push・指定 commit のレビューの保存・マージを行う。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result


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

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert sorted(adapter.launched) == ["alpha", "beta"]
    assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]
    assert [item["agent"] for item in reviewed["rejected"]] == ["beta"]
    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    cycle = read(run_dir / "cycle-1.json")
    assert cycle["state"] == "planning"
    beta = next(item for item in cycle["review"]["reviewers"] if item["actual_agent"] == "beta")
    assert beta["verdict"] == "rejected" and beta["active_findings"] is None
    assert (run_dir / "cycle-1/review-result.json").is_file()
    run = read(run_dir / "run.json")
    assert [resource["state"] for resource in run["resources"]] == ["removed"]
    assert not Path(run["resources"][0]["path"]).exists()
    assert registered_worktrees(env.parent) == [str(env.parent)]


def test_review_with_no_accepted_reviewer_fails_with_code_6(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "failed", "beta": "failed"})

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
            stop_reason="permission: hold-action の選択が無い",
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
    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    next_command = loop.status(run_dir=run_dir)["next"]
    assert "finish" in next_command and any(item.startswith("--abort=external: ") for item in next_command)
    assert not has_placeholder(next_command)
    with pytest.raises(loop.LoopError) as again:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (again.value.code, again.value.stage) == (6, "review")
    assert sorted(adapter.launched) == ["alpha", "beta"]


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
