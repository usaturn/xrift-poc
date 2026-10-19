import fcntl
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.test_support.issue_queue import QueueEnv, pane


def test_init_persists_policy_outside_worktrees_and_excludes_second_run(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    assert Path(run).parent == queue_env.root / "tmp/agents"
    state = queue_env.call("status", "--run", run)["run"]
    assert state["queue"] == [11, 12]
    assert state["policy"]["authorization"] == "#11と#12を順に処理。\n外部への報告は含めない。"
    assert queue_env.call("init", "--input", str(queue_env.home / "input.json"), code=3)["status"] == "blocked"


def test_launch_once_even_when_prompt_response_is_lost(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    data = queue_env.data()
    data["fail_after"] = "herdr agent prompt"
    queue_env.save(data)
    assert queue_env.call("launch", "--run", run, code=3)["status"] == "blocked"
    state = queue_env.call("status", "--run", run)["run"]
    attempt = state["attempts"][-1]
    queue_env.ack(run, attempt)
    assert queue_env.call("reconcile", "--run", run)["status"] == "waiting"
    queue_env.call("launch", "--run", run, code=3)
    calls = queue_env.data()["calls"]
    assert sum(c[:3] == ["herdr", "pane", "split"] for c in calls) == 1
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in calls) == 1


def test_long_idle_does_not_complete_or_close_pane(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.child(attempt, "idle")
    for _ in range(3):
        observed = queue_env.call("reconcile", "--run", run)
        assert observed["status"] == "waiting"
        assert observed["next"] == "wait"
    queue_env.call("advance", "--run", run, code=3)
    assert attempt["pane_id"] in queue_env.data()["panes"]


@pytest.mark.parametrize("state", ["working", "blocked", "unknown"])
def test_merge_and_receipt_do_not_close_unsettled_child(queue_env: QueueEnv, state: str) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt)
    queue_env.report(run, attempt)
    queue_env.child(attempt, state)
    result = queue_env.call("advance", "--run", run, code=3)
    assert result["run"]["phase"] == "implement"
    assert attempt["pane_id"] in queue_env.data()["panes"]


@pytest.mark.parametrize("case", ["manual_close", "other_pr", "old_loop", "no_result", "github_failure"])
def test_unrelated_or_unverified_success_is_not_accepted(queue_env: QueueEnv, case: str) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt, marker=case != "other_pr")
    if case != "no_result":
        queue_env.report(run, attempt)
    data = queue_env.data()
    if case == "manual_close":
        data["issues"]["11"]["closedByPullRequestsReferences"] = []
    if case == "github_failure":
        data["fail"] = "gh issue view"
    queue_env.save(data)
    if case == "old_loop":
        loop = queue_env.root / "reviews/usaturn/xrift-poc/#100-change/loop/test-run/run.json"
        value = json.loads(loop.read_text())
        value["started_at"] = "2020-01-01T00:00:00Z"
        loop.write_text(json.dumps(value))
    queue_env.child(attempt, "done")
    result = queue_env.call("advance", "--run", run, code=3)
    assert result["run"]["phase"] == "implement"


def test_stop_after_followup_survives_new_controller_and_requires_cleanup(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt)
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "followup"
    queue_env.call("stop", "--run", run, "--after", "followup")
    followup = queue_env.launch(run)
    queue_env.ack(run, followup)
    queue_env.report(run, followup)
    queue_env.child(followup, "done")
    paused = queue_env.call("advance", "--run", run)
    assert paused["status"] == "paused"
    assert paused["run"]["phase"] == "cleanup"
    data = queue_env.data()
    data["panes"]["w1:p1"] = pane("w1:p1", "new-controller")
    queue_env.save(data)
    resumed = queue_env.call("resume", "--run", run, "--continue")
    assert resumed["run"]["phase"] == "cleanup"
    assert resumed["run"]["queue"] == [11, 12]
    assert resumed["run"]["policy"]["remote_branches"] == "keep"


def test_second_live_controller_cannot_take_over(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    data = queue_env.data()
    data["panes"]["w1:p9"] = pane("w1:p9", "another-controller")
    queue_env.save(data)
    queue_env.call("resume", "--run", run, pane_id="w1:p9", code=3)
    queue_env.call("launch", "--run", run, pane_id="w1:p9", code=3)
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])


def test_split_response_loss_is_unknown_not_permission_to_split_again(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    data = queue_env.data()
    data["fail_after"] = "herdr pane split"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    assert queue_env.call("reconcile", "--run", run)["status"] == "blocked"
    queue_env.call("launch", "--run", run, code=3)
    assert len(queue_env.data()["panes"]) == 2


def test_lost_start_response_recovers_session_and_submits_only_once(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    data = queue_env.data()
    data["fail_after"] = "herdr agent start"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    observation = queue_env.call("reconcile", "--run", run)
    assert observation["next"] == "submit"
    attempt = observation["run"]["attempts"][-1]
    queue_env.call("submit", "--run", run)
    queue_env.ack(run, attempt)
    queue_env.call("submit", "--run", run, code=3)
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"]) == 1


def test_closed_pane_response_loss_resumes_without_skipping_followup(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt)
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    data = queue_env.data()
    data["fail_after"] = "herdr pane close"
    queue_env.save(data)
    queue_env.call("advance", "--run", run, code=3)
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "followup"


def test_followup_post_response_loss_is_reconciled_without_duplicate(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    data = queue_env.data()
    data["fail_after"] = "gh api repos/usaturn/xrift-poc/issues"
    queue_env.save(data)
    body = queue_env.json_file(
        "post.json",
        {
            "key": "candidate-A",
            "kind": "issue",
            "target": None,
            "title": "日本語の調査結果",
            "body": "複数行の根拠\n調査した結果\n実例 `$(not-a-command)`",
        },
    )
    args = ("publish", "--run", run, "--attempt", attempt["id"], "--input", str(body))
    queue_env.call(*args, pane_id=attempt["pane_id"], code=3)
    assert len(queue_env.data()["issues"]) == 3
    recovered = queue_env.call(*args, pane_id=attempt["pane_id"])
    effect = recovered["run"]["attempts"][-1]["effects"][0]
    assert effect["state"] == "confirmed"
    assert len(queue_env.data()["issues"]) == 3
    # 結果から投稿を落とすと次へ進めない。
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run, code=3)


def test_unknown_post_is_never_retried_and_blocks_success(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    data = queue_env.data()
    data["fail"] = "gh api repos/usaturn/xrift-poc/issues"
    queue_env.save(data)
    body = queue_env.json_file(
        "post.json",
        {
            "key": "candidate-B",
            "kind": "issue",
            "target": None,
            "title": "B",
            "body": "body",
        },
    )
    args = ("publish", "--run", run, "--attempt", attempt["id"], "--input", str(body))
    queue_env.call(*args, pane_id=attempt["pane_id"], code=3)
    data = queue_env.data()
    data["fail"] = None
    queue_env.save(data)
    queue_env.call(*args, pane_id=attempt["pane_id"], code=3)
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run, code=3)
    assert len(queue_env.data()["issues"]) == 2


def test_queue_edits_and_decisions_keep_reason_and_scope(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    queue_env.call(
        "decision",
        "--run",
        run,
        "--id",
        "remote",
        "--category",
        "final",
        "--reason",
        "remote branchは最後に確認\n保持して続行",
    )
    queue_env.call("queue", "--run", run, "--issues", "11", "12", "--reason", "順序を確認")
    queue_env.call("queue", "--run", run, "--issues", "12", "11", "--reason", "実行中の変更", code=3)
    state = queue_env.call("status", "--run", run)["run"]
    assert state["decisions"][0]["category"] == "final"
    assert state["policy"]["additional_issues"] == "propose"


def test_process_lock_prevents_interleaved_commands(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    with (Path(run).parent / ".issue-queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue_env.call("launch", "--run", run, code=3)
    assert len(queue_env.data()["panes"]) == 1


def test_cleanup_restart_finishes_before_next_issue_and_records_new_version(queue_env: QueueEnv) -> None:
    run, followup = queue_env.followup()
    queue_env.report(run, followup)
    queue_env.child(followup, "done")
    queue_env.call("advance", "--run", run)
    cleanup = queue_env.launch(run)
    queue_env.ack(run, cleanup)
    data = queue_env.data()
    data["panes"]["w1:p1"] = pane("w1:p1", "restored-session")
    queue_env.save(data)
    assert queue_env.call("resume", "--run", run)["status"] == "waiting"
    assert queue_env.call("status", "--run", run)["run"]["phase"] == "cleanup"
    (queue_env.root / "README.md").write_text("new helper version\n")
    queue_env.git("add", "README.md")
    queue_env.git("commit", "-qm", "updated main")
    queue_env.git("push", "origin", "HEAD:refs/heads/main")
    queue_env.report(run, cleanup)
    queue_env.child(cleanup, "done")
    queue_env.call("advance", "--run", run)
    next_attempt = queue_env.launch(run)
    assert next_attempt["issue"] == 12
    assert next_attempt["phase"] == "implement"
    assert next_attempt["version"]["main_commit"] != cleanup["version"]["main_commit"]


def test_result_cannot_bypass_stale_main_checkout(queue_env: QueueEnv) -> None:
    run, followup = queue_env.followup()
    queue_env.report(run, followup)
    queue_env.child(followup, "done")
    queue_env.call("advance", "--run", run)
    cleanup = queue_env.launch(run)
    queue_env.ack(run, cleanup)
    queue_env.git("switch", "-c", "unmerged")
    queue_env.report(run, cleanup)
    queue_env.child(cleanup, "done")
    queue_env.call("advance", "--run", run, code=3)


@pytest.mark.parametrize(("minutes", "expected"), [(35, "waiting"), (481, "blocked")])
def test_idle_and_monitor_limit_are_distinct(queue_env: QueueEnv, minutes: int, expected: str) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.child(attempt, "idle")
    state_file = Path(run) / "state.json"
    state = json.loads(state_file.read_text())
    state["attempts"][-1]["created_at"] = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    state_file.write_text(json.dumps(state))
    assert queue_env.call("reconcile", "--run", run)["status"] == expected
    assert attempt["pane_id"] in queue_env.data()["panes"]


def test_missing_child_is_blocked_even_without_ack(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    data = queue_env.data()
    del data["panes"][attempt["pane_id"]]
    queue_env.save(data)
    assert queue_env.call("reconcile", "--run", run)["status"] == "blocked"


def test_resume_reconciles_blocked_child_after_user_resolves_dialog(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.child(attempt, "blocked")
    assert queue_env.call("reconcile", "--run", run)["status"] == "blocked"
    queue_env.call("resume", "--run", run, "--continue", "--reason", "利用者の回答後に再開", code=3)
    queue_env.child(attempt, "working")
    resumed = queue_env.call("resume", "--run", run, "--continue", "--reason", "利用者の回答後に再開")
    assert resumed["status"] == "waiting"
    assert resumed["run"]["policy"]["authorization"]


def test_retained_logs_and_empty_followup_result_survive_session_cleanup(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    session = queue_env.root / "tmp/agents/followup-session"
    session.mkdir()
    session.rmdir()
    queue_env.report(
        run,
        attempt,
        retained=[
            {
                "path": str(queue_env.root / "reviews/retained.log"),
                "reason": "後で確認",
                "blocks_next": False,
            }
        ],
    )
    queue_env.child(attempt, "done")
    observed = queue_env.call("advance", "--run", run)
    assert observed["run"]["phase"] == "cleanup"
    assert observed["run"]["attempts"][-1]["result"]["issues"] == []
    assert Path(run).is_dir()


def test_successful_followup_comment_is_read_back_and_survives_restart(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    body = queue_env.json_file(
        "comment.json",
        {
            "key": "summary",
            "kind": "comment",
            "target": 100,
            "title": None,
            "body": "<!-- review-carry-over-followup:v1 -->\n## 調査結果\n追加Issueなし",
        },
    )
    published = queue_env.call(
        "publish", "--run", run, "--attempt", attempt["id"], "--input", str(body), pane_id=attempt["pane_id"]
    )
    url = published["run"]["attempts"][-1]["effects"][0]["url"]
    queue_env.report(run, attempt, urls=[url])
    queue_env.child(attempt, "done")
    data = queue_env.data()
    data["panes"]["w1:p1"] = pane("w1:p1", "another-session")
    queue_env.save(data)
    assert queue_env.call("resume", "--run", run)["next"] == "advance"
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "cleanup"
    assert len(queue_env.data()["comments"]) == 1


def test_resume_does_not_ignore_helper_update_mid_implementation(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    queue_env.launch(run)
    skills = queue_env.root / ".agents/skills/issue-to-pr"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("updated skills\n")
    queue_env.call("reconcile", "--run", run, code=3)


def test_cleanup_does_not_pass_when_only_local_origin_main_is_current(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)
    cleanup = queue_env.launch(run)
    queue_env.ack(run, cleanup)
    old = queue_env.git("rev-parse", "HEAD")
    (queue_env.root / "README.md").write_text("remote main moved\n")
    queue_env.git("add", "README.md")
    queue_env.git("commit", "-qm", "new main")
    queue_env.git("push", "origin", "HEAD:refs/heads/main")
    queue_env.git("checkout", "--detach", old)
    queue_env.git("update-ref", "refs/remotes/origin/main", old)
    queue_env.report(run, cleanup)
    queue_env.child(cleanup, "done")
    queue_env.call("advance", "--run", run, code=3)


def test_unknown_worktree_blocks_cleanup_launch(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)
    queue_env.git("worktree", "add", "--detach", str(queue_env.home / "other-work"), "HEAD")
    queue_env.call("launch", "--run", run, code=3)
    assert len(queue_env.data()["panes"]) == 1


def test_retained_worktree_is_checked_before_launch(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    retained = queue_env.home / "retained-work"
    queue_env.git("worktree", "add", "--detach", str(retained), "HEAD")
    queue_env.call("launch", "--run", run, code=3)
    queue_env.call(
        "decision",
        "--run",
        run,
        "--id",
        "retain-work",
        "--category",
        "retain",
        "--path",
        str(retained),
        "--reason",
        "マージ済みで停止を確認。削除せず保持",
    )
    attempt = queue_env.launch(run)
    assert retained.is_dir()
    assert attempt["phase"] == "implement"


def test_claim_created_after_result_blocks_next_stage(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)
    queue_env.git("update-ref", "refs/heads/agent-claims/issue-999", "HEAD")
    queue_env.call("launch", "--run", run, code=3)
