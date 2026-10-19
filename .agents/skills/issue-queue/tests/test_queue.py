import fcntl
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.issue_queue import CLI, QueueEnv, pane


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
    assert attempt["pane_id"] not in queue_env.data()["panes"]
    assert queue_env.call("reconcile", "--run", run)["next"] == "advance"
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "followup"
    assert sum(c[:3] == ["herdr", "pane", "close"] for c in queue_env.data()["calls"]) == 1


@pytest.mark.parametrize(
    "stderr",
    [
        '{"error":{"code":"server_error","message":"pane lookup failed"},"id":"cli:pane:get"}',
        '{"error":{"code":"pane_not_found_later"}}',
        "pane_not_found",
        "connection refused: /run/herdr.sock",
    ],
)
def test_pane_lookup_failure_after_close_is_not_treated_as_absent(queue_env: QueueEnv, stderr: str) -> None:
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
    data = queue_env.data()
    data.update(fail_after=None, fail="herdr pane get", fail_stderr=stderr)
    queue_env.save(data)
    assert queue_env.call("advance", "--run", run, code=3)["run"]["phase"] == "implement"
    data = queue_env.data()
    data.update(fail=None, fail_stderr=None)
    queue_env.save(data)
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
    observed = queue_env.call("reconcile", "--run", run)
    assert observed["status"] == "blocked"
    assert observed["reason"] == "子のpaneが見つかりません。再起動せず調査してください"
    queue_env.call("launch", "--run", run, code=3)
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"]) == 1


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


def test_recovered_submit_respects_pending_decision(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    data = queue_env.data()
    data["fail_after"] = "herdr agent start"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    data = queue_env.data()
    data["fail_after"] = None
    queue_env.save(data)
    assert queue_env.call("reconcile", "--run", run)["next"] == "submit"
    decision = ("decision", "--run", run, "--id", "scope", "--category", "blocking", "--reason", "確認待ち")
    queue_env.call(*decision)
    assert queue_env.call("reconcile", "--run", run)["next"] == "decision"
    queue_env.call("submit", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "agent", "prompt"] for c in queue_env.data()["calls"])
    queue_env.call(*decision, "--resolve")
    queue_env.call("submit", "--run", run)
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in queue_env.data()["calls"]) == 1


def test_monitor_limit_resumes_only_after_verified_child_completion(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    file = Path(run) / "state.json"
    state = json.loads(file.read_text())
    state["attempts"][-1]["created_at"] = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    file.write_text(json.dumps(state))
    attempt = state["attempts"][-1]
    assert queue_env.call("reconcile", "--run", run)["status"] == "blocked"
    queue_env.call("resume", "--run", run, "--continue", "--reason", "まだ実行中", code=3)
    queue_env.merge(attempt)
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    resumed = queue_env.call("resume", "--run", run, "--continue", "--reason", "終了とマージを確認")
    assert resumed["next"] == "advance"
    queue_env.call("advance", "--run", run)
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"]) == 1


@pytest.mark.parametrize("boundary", ["prepared", "pane_created"])
def test_known_unsent_launch_resumes_after_saved_boundary(queue_env: QueueEnv, boundary: str) -> None:
    run = queue_env.init()
    queue_env.interrupt_launch(run, boundary)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert saved["launch"] == boundary
    assert not any(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"])
    assert queue_env.call("reconcile", "--run", run)["next"] == "launch"
    resumed = queue_env.launch(run)
    assert resumed["id"] == saved["id"]
    assert resumed["launch"] == "submitted"
    calls = queue_env.data()["calls"]
    for command in (["herdr", "pane", "split"], ["herdr", "agent", "start"], ["herdr", "agent", "prompt"]):
        assert sum(c[:3] == command for c in calls) == 1


def test_unsent_start_does_not_take_over_reused_pane(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    queue_env.interrupt_launch(run, "pane_created")
    attempt = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    data = queue_env.data()
    data["panes"][attempt["pane_id"]] = pane(attempt["pane_id"], "another-session", name="other")
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"])


def test_child_publish_waits_for_controller_wait_and_posts_once(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    data = queue_env.data()
    data["wait_seconds"] = 5
    queue_env.save(data)
    marker = Path(str(queue_env.data_file) + ".waiting")
    body = queue_env.json_file(
        "comment.json",
        {"key": "summary", "kind": "comment", "target": 100, "title": None, "body": "待機中の投稿\n複数行の本文"},
    )
    waiting = subprocess.Popen(
        [sys.executable, str(CLI), "wait", "--run", run],
        cwd=queue_env.root,
        env=queue_env.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            assert waiting.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        published = queue_env.call(
            "publish", "--run", run, "--attempt", attempt["id"], "--input", str(body), pane_id=attempt["pane_id"]
        )
    finally:
        stdout, stderr = waiting.communicate(timeout=30)
    assert waiting.returncode == 0, (stdout, stderr)
    assert [e["state"] for e in published["run"]["attempts"][-1]["effects"]] == ["confirmed"]
    assert len(queue_env.data()["comments"]) == 1


@pytest.mark.parametrize("restored", ["new_pane", "new_terminal"])
def test_same_session_restored_elsewhere_takes_over(queue_env: QueueEnv, restored: str) -> None:
    run = queue_env.init()
    data = queue_env.data()
    if restored == "new_pane":
        caller = "w1:p9"
        del data["panes"]["w1:p1"]
        data["panes"][caller] = pane(caller, "controller-session")
    else:
        caller = "w1:p1"
        data["panes"][caller]["terminal_id"] = "term_after_herdr_restart"
    queue_env.save(data)
    resumed = queue_env.call("resume", "--run", run, pane_id=caller)
    assert resumed["run"]["owner"]["pane_id"] == caller
    assert queue_env.call("launch", "--run", run, pane_id=caller)["run"]["attempts"][-1]["launch"] == "submitted"


def test_same_session_does_not_take_over_while_old_pane_lives(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    data = queue_env.data()
    data["panes"]["w1:p9"] = pane("w1:p9", "controller-session")
    queue_env.save(data)
    queue_env.call("resume", "--run", run, pane_id="w1:p9", code=3)
    queue_env.call("launch", "--run", run, pane_id="w1:p9", code=3)
    assert queue_env.call("status", "--run", run)["run"]["owner"]["pane_id"] == "w1:p1"


def open_pr(queue_env: QueueEnv, number: int, sha: str) -> None:
    data = queue_env.data()
    url = f"https://github.com/usaturn/xrift-poc/pull/{number}"
    data["prs"].append(
        {
            "number": number,
            "url": url,
            "html_url": url,
            "state": "OPEN",
            "body": "利用者の別作業",
            "head": {"ref": "docs/readme", "sha": sha},
            "base": {"ref": "main"},
        }
    )
    queue_env.save(data)


def retain_pr(queue_env: QueueEnv, run: str, *extra: str, number: str = "90", code: int = 0) -> dict[str, Any]:
    reason = "無関係なREADMEのPR。\n競合しないことを確認して保持"
    args = ("decision", "--run", run, "--id", "keep-" + number, "--category", "retain", "--pr", number)
    return queue_env.call(*args, "--reason", reason, *extra, code=code)


def test_confirmed_open_pr_is_retained_without_merging_or_closing(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    open_pr(queue_env, 90, "a" * 40)
    queue_env.call("launch", "--run", run, code=3)
    retain_pr(queue_env, run, number="91", code=3)
    retain_pr(queue_env, run, "--path", str(queue_env.home), code=3)
    queue_env.call(
        "decision", "--run", run, "--id", "x", "--category", "blocking", "--pr", "90", "--reason", "r", code=3
    )
    decision = retain_pr(queue_env, run)["run"]["decisions"][-1]
    assert (decision["pr"], decision["pr_head"], decision["path"]) == (90, "a" * 40, None)
    attempt = queue_env.launch(run)
    assert attempt["phase"] == "implement"
    assert queue_env.data()["prs"][0]["state"] == "OPEN"
    assert not any(c[:2] == ["gh", "pr"] and c[2] in {"merge", "close"} for c in queue_env.data()["calls"])


@pytest.mark.parametrize("change", ["head_moved", "another_pr", "claim", "worktree"])
def test_retained_pr_does_not_admit_other_resources(queue_env: QueueEnv, change: str) -> None:
    run = queue_env.init()
    open_pr(queue_env, 90, "a" * 40)
    retain_pr(queue_env, run)
    if change == "head_moved":
        data = queue_env.data()
        data["prs"][0]["head"]["sha"] = "b" * 40
        queue_env.save(data)
    elif change == "another_pr":
        open_pr(queue_env, 91, "c" * 40)
    elif change == "claim":
        queue_env.git("update-ref", "refs/heads/agent-claims/issue-999", "HEAD")
    else:
        queue_env.git("worktree", "add", "--detach", str(queue_env.home / "other-work"), "HEAD")
    queue_env.call("launch", "--run", run, code=3)
    assert len(queue_env.data()["panes"]) == 1


def test_codex_without_initial_session_gets_one_prompt_and_fixes_session_at_ack(queue_env: QueueEnv) -> None:
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    assert (attempt["launch"], attempt["identity"]) == ("submitted", None)
    assert queue_env.call("reconcile", "--run", run)["next"] == "wait"
    queue_env.ack(run, attempt)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert saved["identity"]["pane_id"] == attempt["pane_id"]
    assert json.loads(saved["identity"]["session"])["value"] == "session-" + attempt["pane_id"]
    queue_env.merge(attempt)
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "followup"
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in queue_env.data()["calls"]) == 1


@pytest.mark.parametrize("caller", ["other_pane", "new_terminal", "other_agent", "other_kind"])
def test_first_ack_without_session_comes_only_from_started_child(queue_env: QueueEnv, caller: str) -> None:
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    data = queue_env.data()
    child = data["panes"][attempt["pane_id"]]
    pane_id = attempt["pane_id"]
    if caller == "other_pane":
        pane_id = "w1:p9"
        data["panes"][pane_id] = pane(pane_id, "intruder-session", name=attempt["agent_name"])
        data["panes"][pane_id]["agent"] = "codex"
    elif caller == "new_terminal":
        child["terminal_id"] = "term_replaced"
    elif caller == "other_agent":
        child["name"] = "someone-else"
    else:
        child["agent"] = "claude"
    queue_env.save(data)
    queue_env.call("ack", "--run", run, "--attempt", attempt["id"], pane_id=pane_id, code=3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["identity"], saved["acknowledged"]) == (None, False)


def test_session_change_after_first_ack_is_rejected(queue_env: QueueEnv) -> None:
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    data = queue_env.data()
    data["panes"][attempt["pane_id"]]["agent_session"]["value"] = "session-after-clear"
    queue_env.save(data)
    queue_env.call("reconcile", "--run", run, code=3)
    queue_env.call("ack", "--run", run, "--attempt", attempt["id"], pane_id=attempt["pane_id"], code=3)
    file = queue_env.result(run, attempt)
    queue_env.call(
        "report", "--run", run, "--attempt", attempt["id"], "--input", str(file), pane_id=attempt["pane_id"], code=3
    )


@pytest.mark.parametrize("lost", ["herdr agent start", "herdr agent prompt"])
def test_codex_response_loss_never_sends_prompt_twice(queue_env: QueueEnv, lost: str) -> None:
    run = queue_env.init(kind="codex")
    data = queue_env.data()
    data["fail_after"] = lost
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    data = queue_env.data()
    data["fail_after"] = None
    queue_env.save(data)
    observed = queue_env.call("reconcile", "--run", run)
    if lost == "herdr agent start":
        assert observed["next"] == "submit"
        queue_env.call("submit", "--run", run)
    else:
        assert observed["next"] == "wait"
        queue_env.call("submit", "--run", run, code=3)
    attempt = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    queue_env.ack(run, attempt)
    queue_env.call("submit", "--run", run, code=3)
    queue_env.call("launch", "--run", run, code=3)
    calls = queue_env.data()["calls"]
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in calls) == 1
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in calls) == 1


def test_lost_start_with_mismatched_terminal_is_not_submitted(queue_env: QueueEnv) -> None:
    run = queue_env.init(kind="codex")
    data = queue_env.data()
    data["fail_after"] = "herdr agent start"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    attempt = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    data = queue_env.data()
    data["fail_after"] = None
    data["panes"][attempt["pane_id"]]["terminal_id"] = "term_replaced"
    queue_env.save(data)
    queue_env.call("reconcile", "--run", run, code=3)
    queue_env.call("submit", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "agent", "prompt"] for c in queue_env.data()["calls"])


@pytest.mark.parametrize(
    ("kind", "args", "expected"),
    [
        ("codex", [], ["--no-daemon"]),
        ("codex", ["-m", "gpt-x", "--search"], ["--no-daemon", "-m", "gpt-x", "--search"]),
        ("codex", ["--no-daemon", "-m", "gpt-x"], ["--no-daemon", "-m", "gpt-x"]),
        ("claude", ["--model", "opus"], ["--model", "opus"]),
    ],
)
def test_child_starts_with_saved_launch_args(
    queue_env: QueueEnv, kind: str, args: list[str], expected: list[str]
) -> None:
    # Codexの共有daemonは起動時の環境でツールとフックを動かすので、子ごとにdaemonを使わずに起動する。
    run = queue_env.init(kind=kind, args=args)
    attempt = queue_env.launch(run)
    assert attempt["launch_args"] == expected
    start = next(c for c in queue_env.data()["calls"] if c[:3] == ["herdr", "agent", "start"])
    assert start[start.index("--") + 1 :] == expected
    assert queue_env.call("status", "--run", run)["run"]["policy"]["agent_args"] == args


@pytest.mark.parametrize("remote", [["--remote", "unix://"], ["--remote=unix://"]])
def test_codex_remote_is_rejected_before_side_effects(queue_env: QueueEnv, remote: list[str]) -> None:
    queue_env.call("init", "--input", str(queue_env.init_file(kind="codex", args=remote)), code=3)
    assert not list((queue_env.root / "tmp/agents").glob("issue-queue.*"))
    # 旧helperが受け付けた方針のrunも、paneを作る前に止める。
    run = queue_env.init(kind="codex")
    state_file = Path(run) / "state.json"
    state = json.loads(state_file.read_text())
    state["policy"]["agent_args"] = remote
    state_file.write_text(json.dumps(state))
    queue_env.call("launch", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])
