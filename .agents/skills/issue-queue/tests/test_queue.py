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


@pytest.mark.parametrize("state", ["idle", "done"])
def test_child_idle_without_result_is_reported_apart_from_waiting(queue_env: QueueEnv, state: str) -> None:
    # 結果の無いidleは、止まった子とバックグラウンドの処理を待つ子を区別できない。待機と区別して返し、待機は続ける。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.child(attempt, state)
    for _ in range(2):
        observed = queue_env.call("reconcile", "--run", run)
        assert (observed["status"], observed["next"], observed["run"]["stopped_reason"]) == ("stalled", "wait", None)
    # 長いidleでも完了にせず、paneを閉じない。
    queue_env.call("advance", "--run", run, code=3)
    assert attempt["pane_id"] in queue_env.data()["panes"]
    # バックグラウンドの処理の完了などで子が再開すれば、待機に戻る。
    queue_env.child(attempt, "working")
    assert queue_env.call("wait", "--run", run)["status"] == "waiting"


def test_child_agent_exited_without_result_is_blocked_without_error(queue_env: QueueEnv) -> None:
    # 子のagentが結果を残さずに終了し、paneにはshellだけが残る（クラッシュ・/exit）。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    data = queue_env.data()
    data["panes"][attempt["pane_id"]].update(name=None, agent=None, agent_session=None, agent_status="unknown")
    queue_env.save(data)
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["status"], observed["next"]) == ("blocked", "inspect")
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


def test_outdated_main_at_boundary_is_updated_before_launch_and_recorded(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    new_main = queue_env.push_main("README.md", "merged elsewhere\n")
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["status"], observed["next"]) == ("ok", "update_main")
    refused = queue_env.call("launch", "--run", run, code=3)
    assert refused["next"] == "update_main"
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])
    queue_env.update_main()
    assert queue_env.call("reconcile", "--run", run)["next"] == "launch"
    attempt = queue_env.launch(run)
    assert attempt["version"]["main_commit"] == new_main


@pytest.mark.parametrize("phase", ["implement", "followup", "cleanup"])
def test_main_is_not_updated_while_attempt_is_in_progress(queue_env: QueueEnv, phase: str) -> None:
    # どの工程も、子のack後・終了結果の前。
    if phase == "implement":
        run = queue_env.init()
        queue_env.ack(run, queue_env.launch(run))
    elif phase == "followup":
        run, _ = queue_env.followup()
    else:
        run, _ = queue_env.cleanup()
    before = queue_env.git("rev-parse", "HEAD")
    queue_env.push_main("README.md", "merged elsewhere\n")
    for command in ("reconcile", "wait"):
        assert queue_env.call(command, "--run", run)["next"] == "wait"
    assert queue_env.git("rev-parse", "HEAD") == before


def test_cleanup_finish_requests_update_when_origin_moved(queue_env: QueueEnv) -> None:
    run, cleanup = queue_env.cleanup()
    new_main = queue_env.push_main("README.md", "merged during cleanup\n")
    queue_env.report(run, cleanup)
    queue_env.child(cleanup, "done")
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["status"], observed["next"]) == ("ok", "update_main")
    assert queue_env.call("advance", "--run", run, code=3)["next"] == "update_main"
    queue_env.update_main()
    assert queue_env.call("advance", "--run", run)["run"]["index"] == 1
    assert queue_env.launch(run)["version"]["main_commit"] == new_main


def test_failed_update_keeps_queue_stopped(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    queue_env.push_main("README.md", "merged elsewhere\n")
    (queue_env.root / "README.md").write_text("利用者の未commitの変更\n")
    before = queue_env.git("rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        queue_env.update_main()
    assert queue_env.git("rev-parse", "HEAD") == before
    assert queue_env.call("launch", "--run", run, code=3)["next"] == "update_main"
    reason = "repo-cleanup-after-mergeの更新がREADME.mdの衝突で止まった。\n利用者へ返す"
    queue_env.call("decision", "--run", run, "--id", "main-update", "--category", "blocking", "--reason", reason)
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["status"], observed["next"]) == ("blocked", "decision")
    queue_env.call("launch", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])


def test_main_on_branch_stops_instead_of_requesting_update(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    queue_env.git("switch", "-q", "-c", "local-work")
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["status"], observed["next"]) == ("blocked", "inspect")
    assert queue_env.call("launch", "--run", run, code=3)["next"] == "inspect"


def test_saved_launch_resumes_with_saved_version_after_origin_moved(queue_env: QueueEnv) -> None:
    # 保存した起動予定の続きは工程の境界ではないので、更新を求めずに保存した使用版で起動する。
    run = queue_env.init()
    queue_env.interrupt_launch(run, "prepared")
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    queue_env.push_main("README.md", "merged elsewhere\n")
    assert queue_env.call("reconcile", "--run", run)["next"] == "launch"
    resumed = queue_env.launch(run)
    assert (resumed["id"], resumed["launch"]) == (saved["id"], "submitted")
    assert resumed["version"] == saved["version"]


def test_monitor_limit_on_cleanup_resumes_into_update(queue_env: QueueEnv) -> None:
    run, cleanup = queue_env.cleanup()
    state_file = Path(run) / "state.json"
    state = json.loads(state_file.read_text())
    state["attempts"][-1]["created_at"] = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    state_file.write_text(json.dumps(state))
    assert queue_env.call("reconcile", "--run", run)["status"] == "blocked"
    queue_env.push_main("README.md", "merged during cleanup\n")
    queue_env.report(run, cleanup)
    queue_env.child(cleanup, "done")
    resumed = queue_env.call("resume", "--run", run, "--continue", "--reason", "子の終了を確認")
    assert (resumed["next"], resumed["run"]["stopped_reason"]) == ("update_main", None)


def test_issue_worktree_left_before_cleanup_blocks_cleanup_launch(queue_env: QueueEnv) -> None:
    run, attempt = queue_env.followup()
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)
    # 実装の子が結果に残さなかった、今回のIssueのworktree。
    left = queue_env.home / "left-work"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-11-left", str(left), "HEAD")
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(left) in blocked["reason"]
    assert len(queue_env.data()["panes"]) == 1


def test_unrelated_worktrees_are_ignored_and_recorded_at_each_check(queue_env: QueueEnv) -> None:
    # 番号が前方一致する別のIssueのbranch、レビューの残骸の形のdetached、lockedのworktreeを残す。
    other = queue_env.home / "wt-issue-110"
    residue = queue_env.home / "pr-review-90-x" / "worktree"
    locked = queue_env.home / "locked-work"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-110-other", str(other), "HEAD")
    queue_env.git("worktree", "add", "-q", "--detach", str(residue), "HEAD")
    queue_env.git("worktree", "add", "-q", "--lock", "--detach", str(locked), "HEAD")
    run, cleanup = queue_env.cleanup()
    queue_env.report(run, cleanup)
    queue_env.child(cleanup, "done")
    for _ in range(2):
        assert queue_env.call("reconcile", "--run", run)["next"] == "advance"
    state = queue_env.call("advance", "--run", run)["run"]
    assert (state["phase"], state["index"]) == ("implement", 1)
    head = queue_env.git("rev-parse", "HEAD")
    expected = sorted(
        [(str(other), head, "feat/issue-110-other"), (str(residue), head, None), (str(locked), head, None)],
        key=str,
    )
    events = [e for e in state["events"] if e["action"] == "worktrees_ignored"]
    # 同じ内容のreconcileを繰り返しても、検査ごとに1件だけ残る。
    assert [e["check"] for e in events] == ["implement_launch", "cleanup_launch", "cleanup_finish"]
    for event in events:
        assert event["issue"] == 11
        assert sorted(((w["path"], w["head"], w["branch"]) for w in event["worktrees"]), key=str) == expected
        assert all("Issue #11" in w["basis"] for w in event["worktrees"])


@pytest.mark.parametrize("case", ["branch", "path"])
def test_worktree_of_launching_issue_blocks_with_path_and_reason(queue_env: QueueEnv, case: str) -> None:
    run = queue_env.init()
    if case == "branch":
        path = queue_env.home / "retry-work"
        queue_env.git("worktree", "add", "-q", "-b", "feat/issue-11-retry", str(path), "HEAD")
    else:
        path = queue_env.home / "issue-11-old"
        queue_env.git("worktree", "add", "-q", "--detach", str(path), "HEAD")
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(path) in blocked["reason"]
    assert "Issue #11" in blocked["reason"]
    assert blocked["next"] == "inspect"
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])


def test_worktree_kept_by_cleanup_child_as_blocking_stops_cleanup_finish(queue_env: QueueEnv) -> None:
    run, cleanup = queue_env.cleanup()
    kept = queue_env.home / "unpushed-work"
    queue_env.git("worktree", "add", "-q", "--detach", str(kept), "HEAD")
    reason = "未pushのcommitがある。\n利用者の確認待ち"
    queue_env.report(run, cleanup, retained=[{"path": str(kept), "reason": reason, "blocks_next": True}])
    queue_env.child(cleanup, "done")
    blocked = queue_env.call("advance", "--run", run, code=3)
    assert str(kept) in blocked["reason"]
    assert reason in blocked["reason"]
    assert blocked["run"]["phase"] == "cleanup"


def test_retained_worktree_is_checked_before_launch(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    retained = queue_env.home / "issue-11-retained"
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
        queue_env.git("worktree", "add", "--detach", str(queue_env.home / "issue-11-other"), "HEAD")
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


def test_stale_shared_daemon_ack_is_rejected_and_run_recovers_by_abandon(queue_env: QueueEnv) -> None:
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    child = attempt["pane_id"]
    # 共有daemonが継承した、閉じた接続確認のpaneの環境から子のackが動いた(#79)。
    queue_env.call("ack", "--run", run, "--attempt", attempt["id"], pane_id="w1:p0", code=3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["acknowledged"], saved["identity"]) == (False, None)
    decision = ("decision", "--run", run, "--id", "daemon", "--category", "blocking")
    queue_env.call(*decision, "--reason", "共有daemon\n旧pane")
    # 旧helperが書いた状態のまま司令塔の会話が失われ、子のpaneを新しい会話が使う。helperの版も変わった。
    state_file = Path(run) / "state.json"
    state = json.loads(state_file.read_text())
    for item in state["attempts"]:
        item.pop("launch_args", None)
        item.pop("abandon_reason", None)
    state_file.write_text(json.dumps(state))
    data = queue_env.data()
    del data["panes"]["w1:p1"]
    data["panes"][child].update(
        name=None,
        agent="claude",
        agent_status="working",
        agent_session={"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "restored-controller"},
    )
    queue_env.save(data)
    skills = queue_env.root / ".agents/skills/issue-to-pr"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("updated skills\n")
    queue_env.call("resume", "--run", run, pane_id=child, code=3)
    reason = "受領前に子が終了。\nマーカー付きPRなし"
    abandoned = queue_env.call("abandon", "--run", run, "--reason", reason, pane_id=child)["run"]
    assert (abandoned["attempts"][0]["closed"], abandoned["attempts"][0]["abandon_reason"]) == (True, reason)
    assert abandoned["owner"]["pane_id"] == "w1:p1"
    resumed = queue_env.call("resume", "--run", run, pane_id=child)
    assert (resumed["run"]["owner"]["pane_id"], resumed["next"]) == (child, "decision")
    queue_env.call(*decision, "--reason", "修正済み", "--resolve", pane_id=child)
    assert queue_env.call("reconcile", "--run", run, pane_id=child)["next"] == "launch"
    relaunched = queue_env.call("launch", "--run", run, pane_id=child)["run"]["attempts"][-1]
    assert relaunched["id"] != attempt["id"]
    assert (relaunched["issue"], relaunched["phase"], relaunched["launch_args"]) == (11, "implement", ["--no-daemon"])
    queue_env.ack(run, relaunched)
    calls = queue_env.data()["calls"]
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in calls) == 2
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in calls) == 2
    assert not any(c[:3] == ["herdr", "pane", "close"] for c in calls)
    assert child in queue_env.data()["panes"]


@pytest.mark.parametrize(
    "case",
    [
        "child_left",
        "acknowledged",
        "child_alive",
        "same_kind_in_pane",
        "caller_in_child_pane",
        "marker_pr",
        "owner_alive",
        "herdr_failure",
        "not_split",
    ],
)
def test_abandon_accepts_only_unacknowledged_attempt_whose_child_is_gone(queue_env: QueueEnv, case: str) -> None:
    # child_leftと、子のpaneで司令塔を復旧したcaller_in_child_paneだけを受け付ける。
    # 他のcaseはchild_leftから条件を1つだけ変える。
    run = queue_env.init(kind="codex")
    caller = "w1:p1"
    if case == "not_split":
        queue_env.interrupt_launch(run, "prepared")
    else:
        attempt = queue_env.launch(run)
        if case == "acknowledged":
            queue_env.ack(run, attempt)
        data = queue_env.data()
        child = data["panes"][attempt["pane_id"]]
        if case != "child_alive":
            # 子が終了し、paneにはshellだけが残る。
            child.update(name=None, agent=None, agent_session=None, agent_status="unknown")
        if case == "same_kind_in_pane":
            child["agent"] = "codex"
        if case == "caller_in_child_pane":
            # 旧ownerは消え、子と同じkindの司令塔の会話を子のpane・保存したterminalで復旧した。
            del data["panes"]["w1:p1"]
            child.update(
                agent="codex",
                agent_status="working",
                agent_session={
                    "agent": "codex",
                    "kind": "id",
                    "source": "herdr:codex",
                    "value": "recovered-controller",
                },
            )
            caller = attempt["pane_id"]
        if case == "owner_alive":
            caller = "w1:p9"
            data["panes"][caller] = pane(caller, "another-controller")
        if case == "herdr_failure":
            data["fail"] = "herdr agent get " + attempt["agent_name"]
            data["fail_stderr"] = '{"error":{"code":"server_error","message":"lookup failed"},"id":"cli:agent:get"}'
        queue_env.save(data)
        if case == "marker_pr":
            queue_env.merge(attempt, close=False)
    accepted = case in ("child_left", "caller_in_child_pane")
    queue_env.call("abandon", "--run", run, "--reason", "子の停止を確認", pane_id=caller, code=0 if accepted else 3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["closed"], saved["abandon_reason"]) == ((True, "子の停止を確認") if accepted else (False, None))
    starts = sum(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"])
    assert starts == (0 if case == "not_split" else 1)


def test_abandon_clears_stop_from_child_blocked_before_ack(queue_env: QueueEnv) -> None:
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    queue_env.child(attempt, "blocked")
    assert queue_env.call("reconcile", "--run", run)["status"] == "blocked"
    # 起動時の確認画面で止まった子を、利用者がpaneごと閉じた。
    data = queue_env.data()
    del data["panes"][attempt["pane_id"]]
    queue_env.save(data)
    queue_env.call("abandon", "--run", run, "--reason", "確認画面で止まった子を利用者が閉じた")
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["next"], observed["run"]["stopped_reason"]) == ("launch", None)


def stop_child(queue_env: QueueEnv, run: str, attempt: dict[str, Any], **overrides: Any) -> None:
    """子がstoppedの結果を残してturnを終えた状態にする（#81の#69の実装の子）。"""
    queue_env.report(run, attempt, **{"outcome": "stopped", "loop_run": None, **overrides})
    queue_env.child(attempt, "done")


def test_retry_closes_stopped_attempt_and_rejects_old_child(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    stop_child(queue_env, run, attempt, summary="正式検証が失敗。\n再開待ち")
    assert queue_env.call("reconcile", "--run", run)["next"] == "inspect"
    # 監視上限・blocked以外の停止は、resume --continueでは解除できない。
    queue_env.call("resume", "--run", run, "--continue", "--reason", "修正済み", code=3)
    reason = "原因を修正済み。\n同じ工程をやり直す"
    retried = queue_env.call("retry", "--run", run, "--reason", reason)["run"]
    saved = retried["attempts"][-1]
    assert (saved["closed"], saved["retry_reason"], retried["stopped_reason"]) == (True, reason, None)
    event = next(e for e in retried["events"] if e["action"] == "attempt_retried")
    assert (event["attempt"], event["reason"], event["outcome"]) == (attempt["id"], reason, "stopped")
    # 古い子は、閉じたattemptの受領・結果を記録できない。
    result = queue_env.result(run, attempt, outcome="stopped", loop_run=None)
    queue_env.call(
        "report", "--run", run, "--attempt", attempt["id"], "--input", str(result), pane_id=attempt["pane_id"], code=3
    )
    queue_env.call("ack", "--run", run, "--attempt", attempt["id"], pane_id=attempt["pane_id"], code=3)
    # やり直すattemptがもう無いので、2回目は拒否する。
    queue_env.call("retry", "--run", run, "--reason", reason, code=3)
    assert queue_env.call("reconcile", "--run", run)["next"] == "launch"
    calls = queue_env.data()["calls"]
    assert not any(c[:3] == ["herdr", "pane", "close"] for c in calls)
    assert attempt["pane_id"] in queue_env.data()["panes"]


@pytest.mark.parametrize(
    "case",
    [
        "stopped",
        "failed",
        "child_gone",
        "no_result",
        "success",
        "not_finalized",
        "background_running",
        "child_working",
        "same_kind_working",
        "herdr_failure",
        "marker_pr",
        "owner_alive",
    ],
)
def test_retry_accepts_only_stopped_child_with_finished_work(queue_env: QueueEnv, case: str) -> None:
    # stopped・failed・child_goneだけを受け付ける。他のcaseはstoppedから条件を1つだけ変える。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    overrides: dict[str, Any] = {
        "failed": {"outcome": "failed"},
        "not_finalized": {"finalized": False},
        "background_running": {"background_stopped": False},
    }.get(case, {})
    if case == "success":
        # 成功の結果はadvanceで進める。
        queue_env.report(run, attempt)
        queue_env.child(attempt, "done")
    elif case != "no_result":
        stop_child(queue_env, run, attempt, **overrides)
    caller = "w1:p1"
    data = queue_env.data()
    child = data["panes"][attempt["pane_id"]]
    if case == "child_gone":
        del data["panes"][attempt["pane_id"]]
    if case == "child_working":
        child["agent_status"] = "working"
    if case == "same_kind_working":
        # 子のagent名は消えたが、保存したterminalのpaneで同じkindのagentがまだ動いている。
        child.update(name=None, agent_status="working")
    if case == "herdr_failure":
        data["fail"] = "herdr agent get " + attempt["agent_name"]
        data["fail_stderr"] = '{"error":{"code":"server_error","message":"lookup failed"},"id":"cli:agent:get"}'
    if case == "owner_alive":
        caller = "w1:p9"
        data["panes"][caller] = pane(caller, "another-controller")
    queue_env.save(data)
    if case == "marker_pr":
        queue_env.merge(attempt, close=False)
    accepted = case in ("stopped", "failed", "child_gone")
    queue_env.call("retry", "--run", run, "--reason", "子の停止を確認", pane_id=caller, code=0 if accepted else 3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["closed"], saved["retry_reason"]) == ((True, "子の停止を確認") if accepted else (False, None))
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"]) == 1


@pytest.mark.parametrize(
    ("ending", "urls"),
    [("stopped", "own"), ("stopped", "with_inherited"), ("no_result", "own"), ("stopped", "missing_own")],
)
def test_retry_continues_followup_without_reposting(queue_env: QueueEnv, ending: str, urls: str) -> None:
    # 投稿（confirmed）を残した持ち越しをやり直す。引き継いだ投稿を重ねずに、残りの投稿だけを行う。
    run, attempt = queue_env.followup()
    summary = {
        "key": "summary",
        "kind": "comment",
        "target": 100,
        "title": None,
        "body": "## 調査結果\n候補Aは見送り。\n実例 `$(not-a-command)`",
    }
    body = queue_env.json_file("summary.json", summary)
    published = queue_env.call(
        "publish", "--run", run, "--attempt", attempt["id"], "--input", str(body), pane_id=attempt["pane_id"]
    )
    url = published["run"]["attempts"][-1]["effects"][0]["url"]
    if ending == "stopped":
        stop_child(queue_env, run, attempt, urls=[url])
    else:
        lose_child(queue_env, attempt)
    queue_env.call("retry", "--run", run, "--reason", "原因を取り除いた。\n投稿は済み")
    relaunched = queue_env.launch(run)
    inherited = relaunched["effects"][0]
    assert (inherited["state"], inherited["url"], inherited["inherited_from"]) == ("confirmed", url, attempt["id"])
    assert url in relaunched["prompt"]
    queue_env.ack(run, relaunched)

    def publish(file: Path, *, code: int = 0) -> dict[str, Any]:
        args = ("publish", "--run", run, "--attempt", relaunched["id"], "--input", str(file))
        return queue_env.call(*args, pane_id=relaunched["pane_id"], code=code)

    # 同じkeyは照合だけ、同じ内容の別keyは拒否、新しい内容は1回だけPOSTする。
    publish(body)
    publish(queue_env.json_file("same.json", {**summary, "key": "summary-again"}), code=3)
    extra = queue_env.json_file("extra.json", {**summary, "key": "addendum", "body": "## 追記\n候補Bを起票した"})
    new_url = publish(extra)["run"]["attempts"][-1]["effects"][-1]["url"]
    assert len(queue_env.data()["comments"]) == 2
    # 結果のurlsは、このattemptの投稿をすべて含み、照合済みのURLだけからなれば受け付ける。
    reported = {"own": [new_url], "with_inherited": [url, new_url], "missing_own": [url]}[urls]
    queue_env.report(run, relaunched, urls=reported)
    queue_env.child(relaunched, "done")
    queue_env.call("advance", "--run", run, code=3 if urls == "missing_own" else 0)


def test_retry_refuses_followup_with_unconfirmed_post(queue_env: QueueEnv) -> None:
    # 成否不明の投稿は再送しないので、新しい子に同じ工程をやり直させない。
    run, attempt = queue_env.followup()
    data = queue_env.data()
    data["fail"] = "gh api repos/usaturn/xrift-poc/issues"
    queue_env.save(data)
    body = queue_env.json_file(
        "post.json", {"key": "candidate-B", "kind": "issue", "target": None, "title": "B", "body": "body"}
    )
    args = ("publish", "--run", run, "--attempt", attempt["id"], "--input", str(body))
    queue_env.call(*args, pane_id=attempt["pane_id"], code=3)
    data = queue_env.data()
    data["fail"] = None
    queue_env.save(data)
    stop_child(queue_env, run, attempt)
    queue_env.call("retry", "--run", run, "--reason", "投稿の成否を確かめずにやり直す", code=3)
    assert queue_env.call("status", "--run", run)["run"]["attempts"][-1]["closed"] is False


def stopped_with_worktree(queue_env: QueueEnv, run: str, attempt: dict[str, Any]) -> Path:
    """実装の子がIssueのworktreeに未pushのcommitを残し、次工程を妨げる資源としてstoppedを報告した。"""
    work = queue_env.home / "issue-11-work"
    queue_env.git("worktree", "add", "-q", "-b", "fix/issue-11-work", str(work), "HEAD")
    retained = [
        {"path": str(work), "reason": "未pushのcommitを保持。\n明示の再開が必要", "blocks_next": True},
        {"path": str(queue_env.home / "logs"), "reason": "検証ログ", "blocks_next": False},
    ]
    stop_child(queue_env, run, attempt, summary="正式検証が既存の不具合で失敗。\n再試行しない", retained=retained)
    return work


def test_stopped_run_with_old_helper_state_continues_by_retry(queue_env: QueueEnv) -> None:
    # #81: 旧helperで起動した実装の子がworktreeを残してstoppedを報告した。その後helperが更新され、
    # 旧ownerと子のpaneは失われた。状態ファイルを書き換えずに、保存したrunを続ける。
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    work = stopped_with_worktree(queue_env, run, attempt)
    decision = ("decision", "--run", run, "--id", "continue-after-stopped", "--category", "blocking")
    queue_env.call(*decision, "--reason", "続け方は別のIssueで決める")
    state_file = Path(run) / "state.json"
    state = json.loads(state_file.read_text())
    for item in state["attempts"]:
        item.pop("retry_reason", None)
    state_file.write_text(json.dumps(state))
    skills = queue_env.root / ".agents/skills/issue-to-pr"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("updated skills\n")
    data = queue_env.data()
    del data["panes"]["w1:p1"]
    del data["panes"][attempt["pane_id"]]
    controller = "w1:p20"
    data["panes"][controller] = pane(controller, "new-controller")
    queue_env.save(data)
    # 使用版が変わったので、resumeでは照合の途中で止まり、ownerを移せない。
    queue_env.call("resume", "--run", run, pane_id=controller, code=3)
    reason = "テストの不具合を修正済み。\n#11の作業を引き継ぐ"
    queue_env.call("retry", "--run", run, "--reason", reason, pane_id=controller)
    resumed = queue_env.call("resume", "--run", run, pane_id=controller)
    assert (resumed["run"]["owner"]["pane_id"], resumed["next"]) == (controller, "decision")
    queue_env.call(*decision, "--reason", "retryで同じ工程をやり直す", "--resolve", pane_id=controller)
    assert queue_env.call("reconcile", "--run", run, pane_id=controller)["next"] == "launch"
    relaunched = queue_env.call("launch", "--run", run, pane_id=controller)["run"]["attempts"][-1]
    assert relaunched["id"] != attempt["id"]
    assert (relaunched["issue"], relaunched["phase"], relaunched["launch_args"]) == (11, "implement", ["--no-daemon"])
    # 新しい子に、前のattemptとその理由、引き継ぐworktreeを渡す。
    for handed in (attempt["id"], reason, str(work)):
        assert handed in relaunched["prompt"]
    assert work.is_dir()


def test_retry_hands_over_only_previous_attempt_resources(queue_env: QueueEnv) -> None:
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    work = stopped_with_worktree(queue_env, run, attempt)
    other = queue_env.home / "issue-11-other"
    queue_env.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
    queue_env.call("retry", "--run", run, "--reason", "作業を引き継ぐ")
    # 引き継いだworktreeは通し、前のattemptが残していないIssueのworktreeでは止める。
    refused = queue_env.call("launch", "--run", run, code=3)
    assert str(other) in refused["reason"] and str(work) not in refused["reason"]
    retain = ("decision", "--run", run, "--id", "retain-other", "--category", "retain", "--path", str(other))
    queue_env.call(*retain, "--reason", "別のセッションの作業。競合なし")
    assert queue_env.launch(run)["id"] != attempt["id"]


def test_handover_ends_when_the_next_attempt_reports(queue_env: QueueEnv) -> None:
    # 引き継いだworktreeを通すのは、同じ工程の次のattemptが結果を残すまで。
    # 次の子が結果に残さなかったIssueのworktreeは、片付けの起動の前に止める。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    work = stopped_with_worktree(queue_env, run, attempt)
    queue_env.call("retry", "--run", run, "--reason", "作業を引き継ぐ")
    relaunched = queue_env.launch(run)
    queue_env.ack(run, relaunched)
    queue_env.merge(relaunched)
    queue_env.report(run, relaunched)
    queue_env.child(relaunched, "done")
    queue_env.call("advance", "--run", run)
    followup = queue_env.launch(run)
    queue_env.ack(run, followup)
    queue_env.report(run, followup)
    queue_env.child(followup, "done")
    queue_env.call("advance", "--run", run)
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(work) in blocked["reason"] and "Issue #11" in blocked["reason"]


@pytest.mark.parametrize("abandons", [1, 2])
def test_retry_handover_survives_abandoned_relaunch(queue_env: QueueEnv, abandons: int) -> None:
    # retryの後に起動した子がackの前に終わり、abandonで閉じてから起動し直した(#79の状況)。
    # 結果の無いattemptを挟んでも引き継ぎは次の子に渡り、引き継いだ子が結果を残した後は通さない。
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    work = stopped_with_worktree(queue_env, run, attempt)
    reason = "テストの不具合を修正済み。\n#11の作業を引き継ぐ"
    queue_env.call("retry", "--run", run, "--reason", reason)
    for _ in range(abandons):
        lost = queue_env.launch(run)
        data = queue_env.data()
        del data["panes"][lost["pane_id"]]
        queue_env.save(data)
        queue_env.call("abandon", "--run", run, "--reason", "最初のackの前に子が終了")
    relaunched = queue_env.launch(run)
    for handed in (attempt["id"], reason, str(work)):
        assert handed in relaunched["prompt"]
    queue_env.ack(run, relaunched)
    queue_env.merge(relaunched)
    queue_env.report(run, relaunched)
    queue_env.child(relaunched, "done")
    queue_env.call("advance", "--run", run)
    followup = queue_env.launch(run)
    queue_env.ack(run, followup)
    queue_env.report(run, followup)
    queue_env.child(followup, "done")
    queue_env.call("advance", "--run", run)
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(work) in blocked["reason"] and "Issue #11" in blocked["reason"]


def finish_stage(queue_env: QueueEnv, run: str, retained: list[dict[str, Any]]) -> None:
    """今の工程の子を起動し、retainedを指定した成功の結果を残して次の工程へ進める。実装の工程はマージも再現する。"""
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    if attempt["phase"] == "implement":
        queue_env.merge(attempt)
    queue_env.report(run, attempt, retained=retained)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)


@pytest.mark.parametrize("phase", ["implement", "followup", "cleanup"])
def test_worktree_of_later_issue_retained_by_earlier_child_still_blocks(queue_env: QueueEnv, phase: str) -> None:
    # #94: 別のセッションの#12のworktreeを、#11の子が削除せずに報告するためblocks_next=falseで残した。
    other = queue_env.home / "other-session"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-12-other", str(other), "HEAD")
    kept = [{"path": str(other), "reason": "別のセッションの作業。\n削除せずに報告", "blocks_next": False}]
    run = queue_env.init()
    for stage in ("implement", "followup", "cleanup"):
        finish_stage(queue_env, run, kept if stage == phase else [])
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(other) in blocked["reason"] and "Issue #12" in blocked["reason"]
    assert blocked["next"] == "inspect"
    assert not [a for a in blocked["run"]["attempts"] if a["issue"] == 12]


def test_own_issue_worktree_retained_by_child_passes_cleanup_and_is_judged_later(queue_env: QueueEnv) -> None:
    # 実装の子が自分のIssueのworktreeをblocks_next=falseで残す流れ。片付けの起動・終了の照合を通る。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    work = queue_env.home / "issue-11-work"
    queue_env.git("worktree", "add", "-q", "-b", "fix/issue-11-work", str(work), "HEAD")
    queue_env.merge(attempt)
    kept = [{"path": str(work), "reason": "実装worktree。\n片付けで整理", "blocks_next": False}]
    queue_env.report(run, attempt, retained=kept)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)
    finish_stage(queue_env, run, [])  # 持ち越し
    finish_stage(queue_env, run, [])  # 片付け。起動と終了の照合でworkを通す
    # 次のIssueの照合では確認済みにせず、番号に当たらないので止めずに無視して記録する。
    assert queue_env.launch(run)["issue"] == 12
    state = queue_env.call("status", "--run", run)["run"]
    event = [e for e in state["events"] if e["action"] == "worktrees_ignored"][-1]
    assert (event["check"], event["issue"]) == ("implement_launch", 12)
    assert str(work) in [w["path"] for w in event["worktrees"]]


def end_run(queue_env: QueueEnv, run: str, reason: str, *, pane_id: str = "w1:p1", code: int = 0) -> dict[str, Any]:
    return queue_env.call("end", "--run", run, "--reason", reason, pane_id=pane_id, code=code)


def lose_child(queue_env: QueueEnv, attempt: dict[str, Any]) -> None:
    """子のpaneが閉じられた。子のagentと名前も無くなる。"""
    data = queue_env.data()
    del data["panes"][attempt["pane_id"]]
    queue_env.save(data)


def lose_split_response(queue_env: QueueEnv, run: str) -> None:
    """splitの応答が失われ、attemptがsplittingのまま残る。fakeはpaneを作ってから応答を落とす。"""
    data = queue_env.data()
    data["fail_after"] = "herdr pane split"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    data = queue_env.data()
    data["fail_after"] = None
    queue_env.save(data)


@pytest.mark.parametrize(
    "case",
    [
        "boundary",
        "prepared",
        "splitting",
        "child_gone",
        "settled_result",
        "idle_without_result",
        "working",
        "same_kind_in_pane",
        "herdr_failure",
        "owner_alive",
        "already_ended",
    ],
)
def test_end_accepts_only_run_whose_child_is_stopped(queue_env: QueueEnv, case: str) -> None:
    # boundary〜settled_resultを受け付ける。idle_without_result以降はchild_goneから条件を1つだけ変える
    # （idle_without_result・working・same_kind_in_paneは子のpaneを残す）。
    run = queue_env.init()
    caller = "w1:p1"
    if case == "prepared":
        queue_env.interrupt_launch(run, "prepared")
    elif case == "splitting":
        lose_split_response(queue_env, run)
    elif case != "boundary":
        attempt = queue_env.launch(run)
        queue_env.ack(run, attempt)
        if case == "settled_result":
            stop_child(queue_env, run, attempt)
        elif case == "idle_without_result":
            queue_env.child(attempt, "idle")
        elif case == "same_kind_in_pane":
            # 子のagent名は消えたが、保存したterminalのpaneで同じkindのagentが入力待ちで残る。
            data = queue_env.data()
            data["panes"][attempt["pane_id"]].update(name=None, agent_status="idle")
            queue_env.save(data)
        elif case != "working":
            lose_child(queue_env, attempt)
        data = queue_env.data()
        if case == "herdr_failure":
            data["fail"] = "herdr agent get " + attempt["agent_name"]
            data["fail_stderr"] = '{"error":{"code":"server_error","message":"lookup failed"},"id":"cli:agent:get"}'
        if case == "owner_alive":
            caller = "w1:p9"
            data["panes"][caller] = pane(caller, "another-controller")
        queue_env.save(data)
    if case == "already_ended":
        end_run(queue_env, run, "最初の終了")
    reason = "子の停止を確認。\n続けられないので終える"
    accepted = case in ("boundary", "prepared", "splitting", "child_gone", "settled_result")
    end_run(queue_env, run, reason, pane_id=caller, code=0 if accepted else 3)
    saved = queue_env.call("status", "--run", run)["run"]
    assert saved["ended_reason"] == ("最初の終了" if case == "already_ended" else reason if accepted else None)
    # 終える操作はpaneを閉じない。
    assert not any(c[:3] == ["herdr", "pane", "close"] for c in queue_env.data()["calls"])


def test_ended_run_refuses_progress_and_allows_new_init(queue_env: QueueEnv) -> None:
    # 1bの形: ack済み・結果なしで子のpaneが閉じられた。旧helperが書いた形（ended_reasonの欄なし）から終える。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    lose_child(queue_env, attempt)
    state_file = Path(run) / "state.json"
    state = json.loads(state_file.read_text())
    state.pop("ended_reason", None)
    state_file.write_text(json.dumps(state))
    init = ("init", "--input", str(queue_env.home / "input.json"))
    queue_env.call(*init, code=3)
    reason = "子が結果を残さずに終わり、paneも無い。\n残りは新しいrunで処理する"
    ended = end_run(queue_env, run, reason)
    assert (ended["status"], ended["next"]) == ("ended", "none")
    event = next(e for e in ended["run"]["events"] if e["action"] == "run_ended")
    observed_event = (event["reason"], event["issue"], event["phase"], event["attempt"])
    assert observed_event == (reason, 11, "implement", attempt["id"])
    observed = queue_env.call("reconcile", "--run", run)
    assert (observed["status"], observed["next"]) == ("ended", "none")
    for command in (("launch",), ("advance",), ("resume",), ("abandon", "--reason", "r"), ("retry", "--reason", "r")):
        assert queue_env.call(command[0], "--run", run, *command[1:], code=3)["next"] == "none"
    new_run = queue_env.call(*init)["run_dir"]
    assert new_run != run
    assert (Path(run) / "state.json").is_file()
    assert sum(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"]) == 1


def test_ended_run_refuses_records_from_its_child(queue_env: QueueEnv) -> None:
    # 結果を残して入力待ちの子が、runを終えた後にackや結果を送っても記録しない。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    stop_child(queue_env, run, attempt)
    end_run(queue_env, run, "停止を確認して終える")
    queue_env.call("ack", "--run", run, "--attempt", attempt["id"], pane_id=attempt["pane_id"], code=3)
    result = queue_env.result(run, attempt, outcome="stopped", loop_run=None)
    report = ("report", "--run", run, "--attempt", attempt["id"], "--input", str(result))
    queue_env.call(*report, pane_id=attempt["pane_id"], code=3)
    events = queue_env.call("status", "--run", run)["run"]["events"]
    assert sum(e["action"] == "ack" for e in events) == 1


@pytest.mark.parametrize(
    "case",
    [
        "pane_closed",
        "agent_exited",
        "monitor_limit",
        "idle",
        "working",
        "same_kind_in_pane",
        "herdr_failure",
        "marker_pr",
        "owner_alive",
        "not_acknowledged",
    ],
)
def test_retry_without_result_accepts_only_child_that_is_gone(queue_env: QueueEnv, case: str) -> None:
    # pane_closed・agent_exited・monitor_limitだけを受け付ける。他はpane_closedから条件を1つだけ変える。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    if case != "not_acknowledged":
        queue_env.ack(run, attempt)
    if case == "monitor_limit":
        # 子が結果を残さずにidleのまま監視上限を過ぎ、その後に利用者がpaneを閉じた。
        queue_env.child(attempt, "idle")
        state_file = Path(run) / "state.json"
        state = json.loads(state_file.read_text())
        state["attempts"][-1]["created_at"] = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
        state_file.write_text(json.dumps(state))
        assert queue_env.call("reconcile", "--run", run)["run"]["stopped_reason"]
    caller = "w1:p1"
    data = queue_env.data()
    child = data["panes"][attempt["pane_id"]]
    if case == "agent_exited":
        child.update(name=None, agent=None, agent_session=None, agent_status="unknown")
    elif case == "idle":
        child["agent_status"] = "idle"
    elif case == "same_kind_in_pane":
        child.update(name=None, agent_status="idle")
    elif case != "working":
        del data["panes"][attempt["pane_id"]]
    if case == "herdr_failure":
        data["fail"] = "herdr agent get " + attempt["agent_name"]
        data["fail_stderr"] = '{"error":{"code":"server_error","message":"lookup failed"},"id":"cli:agent:get"}'
    if case == "owner_alive":
        caller = "w1:p9"
        data["panes"][caller] = pane(caller, "another-controller")
    queue_env.save(data)
    if case == "marker_pr":
        queue_env.merge(attempt, close=False)
    accepted = case in ("pane_closed", "agent_exited", "monitor_limit")
    reason = "子は結果を残さずに止まった。\n同じ工程をやり直す"
    queue_env.call("retry", "--run", run, "--reason", reason, pane_id=caller, code=0 if accepted else 3)
    saved = queue_env.call("status", "--run", run)["run"]
    closed = (saved["attempts"][-1]["closed"], saved["attempts"][-1]["retry_reason"])
    assert closed == ((True, reason) if accepted else (False, None))
    if accepted:
        assert saved["stopped_reason"] is None
        event = next(e for e in saved["events"] if e["action"] == "attempt_retried")
        assert (event["attempt"], event["outcome"]) == (attempt["id"], None)
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in queue_env.data()["calls"]) == 1


def test_retry_hands_over_worktree_of_attempt_without_result(queue_env: QueueEnv) -> None:
    # #69の形: 実装の子がworktreeに作業を残し、結果を残さずにpaneが閉じられた。司令塔が照合して引き継ぐ。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    work = queue_env.home / "issue-11-work"
    queue_env.git("worktree", "add", "-q", "-b", "fix/issue-11-work", str(work), "HEAD")
    spec = queue_env.root / "docs_draft/superpowers/2026-10-21-issue-11-design.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# 前の子のSpec\n")
    lose_child(queue_env, attempt)
    reason = "子は容量不足で止まり、paneが閉じられた。\n作業を引き継ぐ"
    queue_env.call("retry", "--run", run, "--reason", reason, "--handover", str(work), "--handover", str(spec))
    # 起動の前の検査は、引き継いだworktreeで止まらない。
    relaunched = queue_env.launch(run)
    for handed in (attempt["id"], reason, str(work), str(spec)):
        assert handed in relaunched["prompt"]
    # 引き継ぎは次のattemptが結果を残すまで。結果に残さなかったworkは、片付けの起動の前の検査で止まる。
    queue_env.ack(run, relaunched)
    queue_env.merge(relaunched)
    queue_env.report(run, relaunched)
    queue_env.child(relaunched, "done")
    queue_env.call("advance", "--run", run)
    finish_stage(queue_env, run, [])  # 持ち越し
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(work) in blocked["reason"] and "Issue #11" in blocked["reason"]


@pytest.mark.parametrize("case", ["missing", "unrelated_dir", "other_issue", "parent", "relative", "followup"])
def test_retry_handover_refuses_unverified_paths(queue_env: QueueEnv, case: str) -> None:
    # 引き継げるのは、実装の工程で、処理中のIssueの番号がパスかbranchに現れる、存在する絶対パスだけ。
    if case == "followup":
        run, attempt = queue_env.followup()
    else:
        run = queue_env.init()
        attempt = queue_env.launch(run)
        queue_env.ack(run, attempt)
    work = queue_env.home / "issue-11-work"
    path = {
        "missing": queue_env.home / "issue-11-gone",
        "unrelated_dir": queue_env.home / "plain-dir",
        "other_issue": queue_env.home / "other-work",
        "parent": queue_env.root,
        "relative": Path("issue-11-work"),
        "followup": work,
    }[case]
    if case == "unrelated_dir":
        path.mkdir()
    elif case == "other_issue":
        queue_env.git("worktree", "add", "-q", "-b", "feat/issue-12-x", str(path), "HEAD")
    elif case in ("relative", "followup"):
        queue_env.git("worktree", "add", "-q", "-b", "fix/issue-11-work", str(work), "HEAD")
    lose_child(queue_env, attempt)
    queue_env.call("retry", "--run", run, "--reason", "r", "--handover", str(path), code=3)
    assert queue_env.call("status", "--run", run)["run"]["attempts"][-1]["closed"] is False


def test_handover_survives_retried_attempt_without_result(queue_env: QueueEnv) -> None:
    # A: worktreeを残してstopped → retry → B: ack後に結果を残さずpaneが閉じられた → retry → C。
    # Bは結果を残していないので、Aの引き継ぎはCまで続き、Cの依頼文にAとBが入る。Cが結果を残すと終わる。
    run = queue_env.init()
    first = queue_env.launch(run)
    queue_env.ack(run, first)
    work = stopped_with_worktree(queue_env, run, first)
    queue_env.call("retry", "--run", run, "--reason", "テストの不具合を修正済み")
    second = queue_env.launch(run)
    queue_env.ack(run, second)
    lose_child(queue_env, second)
    reason = "引き継いだ子も結果を残さずに止まった。\n続けてやり直す"
    queue_env.call("retry", "--run", run, "--reason", reason)
    third = queue_env.launch(run)
    for handed in (first["id"], second["id"], reason, str(work)):
        assert handed in third["prompt"]
    queue_env.ack(run, third)
    queue_env.merge(third)
    queue_env.report(run, third)
    queue_env.child(third, "done")
    queue_env.call("advance", "--run", run)
    finish_stage(queue_env, run, [])  # 持ち越し
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert str(work) in blocked["reason"] and "Issue #11" in blocked["reason"]
