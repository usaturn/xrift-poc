import fcntl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.issue_queue import CLI, QueueEnv, pane


def test_queue_refuses_legacy_board_child_before_launch(queue_env: QueueEnv) -> None:
    """native parent でだけ分かるボードの子にも claim を経ずに子を起動し、別のセッションと競合する変更を残す。"""
    run = queue_env.init()
    data = queue_env.data()
    data["issues"]["101"] = {
        "number": 101,
        "url": "https://github.com/usaturn/xrift-poc/issues/101",
        "html_url": "https://github.com/usaturn/xrift-poc/issues/101",
        "state": "OPEN",
        "body": "<!-- planning-board:test-reduction-101:begin -->\n親の説明",
        "repository": {"full_name": "usaturn/xrift-poc"},
    }
    data["parents"]["11"] = 101
    queue_env.save(data)

    assert queue_env.call("launch", "--run", run, code=3)["status"] == "blocked"
    assert len(queue_env.data()["panes"]) == 1


def test_init_excludes_a_second_unfinished_run(queue_env: QueueEnv) -> None:
    """未完了の run があるのに 2 つ目の run を作り、同じ Issue を 2 つの run が並行に処理して子と PR が重複する。"""
    queue_env.init()
    assert queue_env.call("init", "--input", str(queue_env.home / "input.json"), code=3)["status"] == "blocked"


def test_child_idle_without_result_is_not_advanced(queue_env: QueueEnv) -> None:
    """結果の無いまま入力待ちになった子を完了とし、バックグラウンドの処理で再開しうる子の pane を閉じる。"""
    # 結果の無い入力待ちは、止まった子とバックグラウンドの処理を待つ子を区別できない。長い idle でも完了にしない。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run, code=3)
    assert attempt["pane_id"] in queue_env.data()["panes"]


def test_merge_and_receipt_do_not_close_unsettled_child(queue_env: QueueEnv) -> None:
    """マージと結果があっても動いている子の pane を閉じ、終了処理の途中の子を止める。"""
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt)
    queue_env.report(run, attempt)
    queue_env.child(attempt, "working")
    result = queue_env.call("advance", "--run", run, code=3)
    assert result["run"]["phase"] == "implement"
    assert attempt["pane_id"] in queue_env.data()["panes"]


@pytest.mark.parametrize("case", ["manual_close", "other_pr"])
def test_unrelated_or_unverified_success_is_not_accepted(queue_env: QueueEnv, case: str) -> None:
    """今回の PR が Issue を閉じたと確かめないまま、または目印の無い PR のマージで、次の工程へ進む。"""
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt, marker=case != "other_pr")
    queue_env.report(run, attempt)
    if case == "manual_close":
        data = queue_env.data()
        data["issues"]["11"]["closedByPullRequestsReferences"] = []
        queue_env.save(data)
    queue_env.child(attempt, "done")
    result = queue_env.call("advance", "--run", run, code=3)
    assert result["run"]["phase"] == "implement"


def test_stop_after_followup_survives_new_controller_and_requires_cleanup(queue_env: QueueEnv) -> None:
    """利用者の stop --after followup を無視し、止めたはずの片付けへ進む。"""
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


def test_lost_start_response_recovers_session_and_submits_only_once(queue_env: QueueEnv) -> None:
    """start の応答が失われた後の送信をやり直し、子へ同じ依頼を 2 回送る。"""
    run = queue_env.init()
    data = queue_env.data()
    data["fail_after"] = "herdr agent start"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    attempt = queue_env.call("reconcile", "--run", run)["run"]["attempts"][-1]
    queue_env.call("submit", "--run", run)
    queue_env.ack(run, attempt)
    # ack 済みでターンの間（入力待ち）の子にも、2 回目の依頼は送らない。
    queue_env.child(attempt, "idle")
    queue_env.call("submit", "--run", run, code=3)
    calls = queue_env.data()["calls"]
    assert sum(c[:3] == ["herdr", "agent", "start"] for c in calls) == 1
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in calls) == 1


def test_pane_lookup_failure_after_close_is_not_treated_as_absent(queue_env: QueueEnv) -> None:
    """Herdr の照会の失敗を pane の不在と読み、子が残っていても abandon・retry・end や pane を閉じる処理を進める。"""
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
    # 構造化されていない応答に code と同じ文字列があっても、不在とは読まない。
    data = queue_env.data()
    data.update(fail_after=None, fail="herdr pane get", fail_stderr="pane_not_found")
    queue_env.save(data)
    assert queue_env.call("advance", "--run", run, code=3)["run"]["phase"] == "implement"
    data = queue_env.data()
    data.update(fail=None, fail_stderr=None)
    queue_env.save(data)
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "followup"


def test_followup_post_response_loss_is_reconciled_without_duplicate(queue_env: QueueEnv) -> None:
    """投稿の応答が失われた後に送り直し、同じ Issue を 2 件公開する。"""
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
    """成否不明の投稿を送り直して重複を公開する、または投稿を確かめないまま成功として進む。"""
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


def test_queue_edit_cannot_replace_the_current_issue(queue_env: QueueEnv) -> None:
    """処理中の Issue を並べ替えで差し替え、動いている子を見失って別の Issue の子を起動する。"""
    run = queue_env.init()
    queue_env.call("queue", "--run", run, "--issues", "11", "12", "--reason", "順序を確認")
    queue_env.call("queue", "--run", run, "--issues", "12", "11", "--reason", "実行中の変更", code=3)
    assert queue_env.call("status", "--run", run)["run"]["queue"] == [11, 12]


def test_process_lock_prevents_interleaved_commands(queue_env: QueueEnv) -> None:
    """司令塔と子の操作が並行して state.json を上書きし、結果・投稿の記録を失う。"""
    run = queue_env.init()
    with (Path(run).parent / ".issue-queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue_env.call("launch", "--run", run, code=3)
    assert len(queue_env.data()["panes"]) == 1


def test_resume_does_not_ignore_helper_update_mid_implementation(queue_env: QueueEnv) -> None:
    """工程の途中でスキル・helper が変わっても照合せず、手順が途中で変わった子の結果を受け付ける。"""
    run = queue_env.init()
    queue_env.launch(run)
    skills = queue_env.root / ".agents/skills/issue-to-pr"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("updated skills\n")
    queue_env.call("reconcile", "--run", run, code=3)


def test_outdated_main_at_boundary_is_updated_before_launch_and_recorded(queue_env: QueueEnv) -> None:
    """古い親 main checkout のまま実装の子を起動し、子が古いスキル・helper で動く。"""
    run = queue_env.init()
    new_main = queue_env.push_main("README.md", "merged elsewhere\n")
    queue_env.call("launch", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])
    # 更新すれば起動でき、子の使用版は新しい main になる。
    queue_env.update_main()
    attempt = queue_env.launch(run)
    assert attempt["version"]["main_commit"] == new_main


def test_issue_worktree_left_before_cleanup_blocks_cleanup_launch(queue_env: QueueEnv) -> None:
    """処理中の Issue の確認していない worktree のまま片付けの子を起動し、別のセッションの worktree を消させる。"""
    run, attempt = queue_env.followup()
    queue_env.report(run, attempt)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)
    # 実装の子が結果に残さなかった、今回のIssueのworktree。
    left = queue_env.home / "left-work"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-11-left", str(left), "HEAD")
    queue_env.call("launch", "--run", run, code=3)
    assert len(queue_env.data()["panes"]) == 1


def test_worktree_of_launching_issue_blocks_launch(queue_env: QueueEnv) -> None:
    """処理中の Issue の worktree・branch が残っているのに実装の子を起動し、作業を重ねる。"""
    run = queue_env.init()
    path = queue_env.home / "retry-work"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-11-retry", str(path), "HEAD")
    queue_env.call("launch", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"])


def test_recovered_submit_respects_pending_decision(queue_env: QueueEnv) -> None:
    """blocking の判断待ちがあるのに依頼を送り、利用者の判断の前に子が作業を始める。"""
    run = queue_env.init()
    data = queue_env.data()
    data["fail_after"] = "herdr agent start"
    queue_env.save(data)
    queue_env.call("launch", "--run", run, code=3)
    data = queue_env.data()
    data["fail_after"] = None
    queue_env.save(data)
    # 照合で、応答の失われた start を起動済み（未送信）として確定する。
    queue_env.call("reconcile", "--run", run)
    decision = ("decision", "--run", run, "--id", "scope", "--category", "blocking", "--reason", "確認待ち")
    queue_env.call(*decision)
    queue_env.call("submit", "--run", run, code=3)
    assert not any(c[:3] == ["herdr", "agent", "prompt"] for c in queue_env.data()["calls"])
    queue_env.call(*decision, "--resolve")
    queue_env.call("submit", "--run", run)
    assert sum(c[:3] == ["herdr", "agent", "prompt"] for c in queue_env.data()["calls"]) == 1


def test_child_publish_waits_for_controller_wait_and_posts_once(queue_env: QueueEnv) -> None:
    """司令塔の wait と子の publish が並行し、子の投稿の記録を司令塔の保存が上書きして失う。"""
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
        queue_env.call(
            "publish", "--run", run, "--attempt", attempt["id"], "--input", str(body), pane_id=attempt["pane_id"]
        )
    finally:
        stdout, stderr = waiting.communicate(timeout=30)
    assert waiting.returncode == 0, (stdout, stderr)
    # 司令塔の wait の保存の後にも、子の投稿の記録が残る。
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]["effects"]
    assert [e["state"] for e in saved] == ["confirmed"]
    assert len(queue_env.data()["comments"]) == 1


@pytest.mark.parametrize(("kind", "args"), [("codex", ["-m", "gpt-x", "--search"]), ("claude", ["--model", "opus"])])
def test_child_starts_with_saved_launch_args(queue_env: QueueEnv, kind: str, args: list[str]) -> None:
    """利用者の起動引数を落とす・並べ替えて、子が指定と違うモデルなどで動く。"""
    run = queue_env.init(kind=kind, args=args)
    queue_env.launch(run)
    start = next(c for c in queue_env.data()["calls"] if c[:3] == ["herdr", "agent", "start"])
    passed = start[start.index("--") + 1 :]
    # 利用者の引数は順序どおり最後に並ぶ。Codex には共有の daemon を使わない指定が前に加わる。
    assert passed[len(passed) - len(args) :] == args
    if kind != "codex":
        assert passed == args


@pytest.mark.parametrize("case", ["child_left", "child_alive", "same_kind_in_pane", "marker_pr", "herdr_failure"])
def test_abandon_accepts_only_unacknowledged_attempt_whose_child_is_gone(queue_env: QueueEnv, case: str) -> None:
    """子が残る・照会に失敗する・子の PR がある attempt を閉じ、次の launch で同じ工程の子と PR を重ねる。"""
    # child_leftだけを受け付ける。他のcaseはchild_leftから条件を1つだけ変える。
    run = queue_env.init(kind="codex")
    attempt = queue_env.launch(run)
    data = queue_env.data()
    child = data["panes"][attempt["pane_id"]]
    if case != "child_alive":
        # 子が終了し、paneにはshellだけが残る。
        child.update(name=None, agent=None, agent_session=None, agent_status="unknown")
    if case == "same_kind_in_pane":
        child["agent"] = "codex"
    if case == "herdr_failure":
        data["fail"] = "herdr agent get " + attempt["agent_name"]
        data["fail_stderr"] = '{"error":{"code":"server_error","message":"lookup failed"},"id":"cli:agent:get"}'
    queue_env.save(data)
    if case == "marker_pr":
        queue_env.merge(attempt, close=False)
    accepted = case == "child_left"
    queue_env.call("abandon", "--run", run, "--reason", "子の停止を確認", code=0 if accepted else 3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["closed"], saved["abandon_reason"]) == ((True, "子の停止を確認") if accepted else (False, None))


def stop_child(queue_env: QueueEnv, run: str, attempt: dict[str, Any], **overrides: Any) -> None:
    """子がstoppedの結果を残してturnを終えた状態にする（#81の#69の実装の子）。"""
    queue_env.report(run, attempt, **{"outcome": "stopped", "loop_run": None, **overrides})
    queue_env.child(attempt, "done")


@pytest.mark.parametrize("case", ["stopped", "success", "child_working", "marker_pr"])
def test_retry_accepts_only_stopped_child_with_finished_work(queue_env: QueueEnv, case: str) -> None:
    """動いている子・成功した子・PR のある attempt を閉じ、同じ工程の子と PR を重ねる。"""
    # stoppedだけを受け付ける。他のcaseはstoppedから条件を1つだけ変える。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    if case == "success":
        # 成功の結果はadvanceで進める。
        queue_env.report(run, attempt)
        queue_env.child(attempt, "done")
    else:
        stop_child(queue_env, run, attempt)
    if case == "child_working":
        queue_env.child(attempt, "working")
    if case == "marker_pr":
        queue_env.merge(attempt, close=False)
    accepted = case == "stopped"
    queue_env.call("retry", "--run", run, "--reason", "子の停止を確認", code=0 if accepted else 3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["closed"], saved["retry_reason"]) == ((True, "子の停止を確認") if accepted else (False, None))


def test_retry_continues_followup_without_reposting(queue_env: QueueEnv) -> None:
    """やり直した持ち越しで、前の attempt の投稿や同じ内容・送信本文とその引用を送り直し、重複を公開する。"""
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
    stop_child(queue_env, run, attempt, urls=[url])
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
    # 引き継いだ投稿の送信本文（末尾にマーカー）や、その引用を別keyで渡しても、kindによらずPOSTしない。
    sent = inherited["body"]
    issues = len(queue_env.data()["issues"])
    resent = [
        {**summary, "key": "summary-sent", "body": sent},
        {**summary, "key": "summary-quoted", "body": "引用します。\n\n" + sent},
        {**summary, "key": "summary-issue", "kind": "issue", "target": None, "title": "調査結果", "body": sent},
    ]
    for index, request in enumerate(resent):
        publish(queue_env.json_file(f"resent-{index}.json", request), code=3)
    assert (len(queue_env.data()["comments"]), len(queue_env.data()["issues"])) == (1, issues)
    extra = queue_env.json_file("extra.json", {**summary, "key": "addendum", "body": "## 追記\n候補Bを起票した"})
    new_url = publish(extra)["run"]["attempts"][-1]["effects"][-1]["url"]
    assert len(queue_env.data()["comments"]) == 2
    queue_env.report(run, relaunched, urls=[new_url])
    queue_env.child(relaunched, "done")
    queue_env.call("advance", "--run", run)


def test_retry_refuses_followup_with_unconfirmed_post(queue_env: QueueEnv) -> None:
    """成否不明の投稿を残した持ち越しをやり直し、新しい子が同じ投稿を重ねる。"""
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


def test_retry_hands_over_only_previous_attempt_resources(queue_env: QueueEnv) -> None:
    """引き継ぎの間に、前の attempt が残していない Issue の worktree まで通し、作業を重ねる。"""
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    stopped_with_worktree(queue_env, run, attempt)
    other = queue_env.home / "issue-11-other"
    queue_env.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
    queue_env.call("retry", "--run", run, "--reason", "作業を引き継ぐ")
    # 引き継いだworktreeは通し、前のattemptが残していないIssueのworktreeでは止める。
    queue_env.call("launch", "--run", run, code=3)
    retain = ("decision", "--run", run, "--id", "retain-other", "--category", "retain", "--path", str(other))
    queue_env.call(*retain, "--reason", "別のセッションの作業。競合なし")
    assert queue_env.launch(run)["id"] != attempt["id"]


def test_handover_ends_when_the_next_attempt_reports(queue_env: QueueEnv) -> None:
    """引き継いだ子が結果を残した後も、次工程を妨げると報告された前の worktree を通し、そのまま片付けを起動する。"""
    # 引き継いだworktreeを通すのは、同じ工程の次のattemptが結果を残すまで。
    # 次の子が結果に残さなかったIssueのworktreeは、片付けの起動の前に止める。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    stopped_with_worktree(queue_env, run, attempt)
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
    assert not any(a["phase"] == "cleanup" for a in blocked["run"]["attempts"])


def finish_stage(queue_env: QueueEnv, run: str, retained: list[dict[str, Any]]) -> None:
    """今の工程の子を起動し、retainedを指定した成功の結果を残して次の工程へ進める。実装の工程はマージも再現する。"""
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    if attempt["phase"] == "implement":
        queue_env.merge(attempt)
    queue_env.report(run, attempt, retained=retained)
    queue_env.child(attempt, "done")
    queue_env.call("advance", "--run", run)


def test_worktree_of_later_issue_retained_by_earlier_child_still_blocks(queue_env: QueueEnv) -> None:
    """前の Issue の子が retained に書いた別のセッションの worktree を、後の Issue の照合で確認済みとして通す。"""
    # #94: 別のセッションの#12のworktreeを、#11の片付けの子が削除せずに報告するためblocks_next=falseで残した。
    other = queue_env.home / "other-session"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-12-other", str(other), "HEAD")
    kept = [{"path": str(other), "reason": "別のセッションの作業。\n削除せずに報告", "blocks_next": False}]
    run = queue_env.init()
    for stage in ("implement", "followup", "cleanup"):
        finish_stage(queue_env, run, kept if stage == "cleanup" else [])
    blocked = queue_env.call("launch", "--run", run, code=3)
    assert not [a for a in blocked["run"]["attempts"] if a["issue"] == 12]


def end_run(queue_env: QueueEnv, run: str, reason: str, *, code: int = 0) -> dict[str, Any]:
    return queue_env.call("end", "--run", run, "--reason", reason, code=code)


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


@pytest.mark.parametrize("case", ["settled_result", "background_running_done"])
def test_end_accepts_only_run_whose_child_is_stopped(queue_env: QueueEnv, case: str) -> None:
    """バックグラウンドの処理で再開しうる子が残る run を終え、監視の無い子が作業を続ける。"""
    # settled_resultだけを受け付ける。background_running_doneは結果のバックグラウンドの停止の報告だけを偽にする。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    stop_child(queue_env, run, attempt, background_stopped=case == "settled_result")
    reason = "子の停止を確認。\n続けられないので終える"
    accepted = case == "settled_result"
    end_run(queue_env, run, reason, code=0 if accepted else 3)
    assert queue_env.call("status", "--run", run)["run"]["ended_reason"] == (reason if accepted else None)


def test_ended_run_refuses_progress(queue_env: QueueEnv) -> None:
    """終えた run の進める操作を受け付け、利用者が終えたはずの run で同じ Issue の子を起動する。"""
    # ack済み・結果なしで子のpaneが閉じられた run を、残りを新しい run で処理するために終える。
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    lose_child(queue_env, attempt)
    end_run(queue_env, run, "子が結果を残さずに終わり、paneも無い。\n残りは新しいrunで処理する")
    queue_env.call("launch", "--run", run, code=3)
    assert sum(c[:3] == ["herdr", "pane", "split"] for c in queue_env.data()["calls"]) == 1


def test_retry_without_result_refuses_an_idle_child(queue_env: QueueEnv) -> None:
    """結果を残さずに入力待ちの子（再開しうる）が居る attempt を閉じ、同じ工程の子を重ねる。"""
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.child(attempt, "idle")
    reason = "子は結果を残さずに止まった。\n同じ工程をやり直す"
    queue_env.call("retry", "--run", run, "--reason", reason, code=3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["closed"], saved["retry_reason"]) == (False, None)


def test_retry_hands_over_worktree_of_attempt_without_result(queue_env: QueueEnv) -> None:
    """結果を残さずに止まった子から引き継いだ worktree を、次の子が結果を残した後も通し、そのまま片付けを起動する。"""
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
    # 1つの--handoverに並べても、資源ごとに繰り返してもよい。司令塔のagentは並べて書いた（行動比較の場面8）。
    queue_env.call("retry", "--run", run, "--reason", reason, "--handover", str(work), str(spec))
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
    assert not any(a["phase"] == "cleanup" for a in blocked["run"]["attempts"])


def test_retry_handover_refuses_another_issue_worktree(queue_env: QueueEnv) -> None:
    """別の Issue の worktree を引き継ぎの資源として受け付け、新しい子に別のセッションの worktree で作業させる。"""
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    other = queue_env.home / "other-work"
    queue_env.git("worktree", "add", "-q", "-b", "feat/issue-12-x", str(other), "HEAD")
    lose_child(queue_env, attempt)
    queue_env.call("retry", "--run", run, "--reason", "r", "--handover", str(other), code=3)
    assert queue_env.call("status", "--run", run)["run"]["attempts"][-1]["closed"] is False


def test_abandon_refuses_splitting_attempt_while_named_agent_exists(queue_env: QueueEnv) -> None:
    """pane 作成の成否不明の attempt を、子の名前の agent が居るのに閉じ、同じ工程の子を重ねる。"""
    # splitの応答が失われた。startを送っていないので、名前のagentが居れば起動の経緯が記録と合わない。
    run = queue_env.init()
    lose_split_response(queue_env, run)
    attempt = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert attempt["launch"] == "splitting"
    data = queue_env.data()
    data["panes"]["w1:p8"] = pane("w1:p8", "unexpected-session", name=attempt["agent_name"])
    queue_env.save(data)
    reason = "splitが作ったpaneは無いと確認した。\n起動し直す"
    queue_env.call("abandon", "--run", run, "--reason", reason, code=3)
    saved = queue_env.call("status", "--run", run)["run"]["attempts"][-1]
    assert (saved["closed"], saved["abandon_reason"]) == (False, None)
