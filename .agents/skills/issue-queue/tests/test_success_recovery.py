import json
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.issue_queue import QueueEnv, pane


def successful_stage(env: QueueEnv, phase: str) -> tuple[str, dict[str, Any]]:
    if phase == "implement":
        run = env.init()
        attempt = env.launch(run)
        env.ack(run, attempt)
        env.merge(attempt)
    else:
        run, attempt = env.followup()
        if phase == "cleanup":
            env.report(run, attempt)
            env.child(attempt, "done")
            env.call("advance", "--run", run)
            attempt = env.launch(run)
            env.ack(run, attempt)
    env.report(run, attempt)
    env.child(attempt, "done")
    return run, attempt


def disappear(env: QueueEnv, attempt: dict[str, Any], missing: str) -> None:
    data = env.data()
    if missing == "pane":
        data["panes"].pop(attempt["pane_id"])
    else:
        data["panes"][attempt["pane_id"]].update(name=None, agent=None, agent_session=None, agent_status="unknown")
    env.save(data)


@pytest.mark.parametrize("phase", ["implement", "followup", "cleanup"])
@pytest.mark.parametrize("missing", ["pane", "agent"])
def test_success_recovery_requires_explicit_reason_and_preserves_pane(
    queue_env: QueueEnv, phase: str, missing: str
) -> None:
    """成功した工程の記録を再利用できず後工程を落とす、または許可無しで進んで残った他者のpaneを閉じる。"""
    run, attempt = successful_stage(queue_env, phase)
    disappear(queue_env, attempt, missing)
    before = queue_env.data()["panes"].copy()
    for args in [("reconcile",), ("resume",), ("resume", "--continue")]:
        observed = queue_env.call(args[0], "--run", run, *args[1:])
        assert (observed["status"], observed["next"]) == ("blocked", "inspect")
    queue_env.call("advance", "--run", run, code=3)

    reason = "利用者が子の終了と作業を確認。\n残ったpaneには入力しない。"
    resumed = queue_env.call("resume", "--run", run, "--continue", "--reason", reason)
    assert (resumed["status"], resumed["next"]) == ("ok", "advance")
    assert any(e.get("reason") == reason and e.get("attempt") == attempt["id"] for e in resumed["run"]["events"])
    advanced = queue_env.call("advance", "--run", run)
    assert advanced["run"]["attempts"][-1]["closed"]
    assert (advanced["run"]["phase"], advanced["run"]["index"]) != (phase, 0)
    assert queue_env.data()["panes"] == before


@pytest.mark.parametrize("case", ["named_elsewhere", "same_kind", "terminal_changed", "lookup_failure"])
def test_success_recovery_never_treats_unverified_child_as_absent(queue_env: QueueEnv, case: str) -> None:
    """子の不在を誤認して同時に次の工程を走らせ、子または別のagentのpaneを閉じる。"""
    run, attempt = successful_stage(queue_env, "implement")
    disappear(queue_env, attempt, "agent")
    data = queue_env.data()
    if case == "named_elsewhere":
        data["panes"]["w1:p99"] = pane("w1:p99", "moved-child", name=attempt["agent_name"], state="done")
    elif case == "same_kind":
        data["panes"][attempt["pane_id"]] = pane(attempt["pane_id"], "renamed-child", name="renamed", state="idle")
    elif case == "terminal_changed":
        data["panes"][attempt["pane_id"]]["terminal_id"] = "replacement-terminal"
    else:
        data.update(fail="herdr agent get " + attempt["agent_name"], fail_stderr="agent_not_found")
    queue_env.save(data)
    before = data["panes"].copy()
    queue_env.call("resume", "--run", run, "--continue", "--reason", "終了を調査済み", code=3)
    rejected = queue_env.call("advance", "--run", run, code=3)
    assert rejected["run"]["phase"] == "implement"
    assert not rejected["run"]["attempts"][-1]["closed"]
    assert queue_env.data()["panes"] == before


@pytest.mark.parametrize("phase", ["implement", "followup", "cleanup"])
def test_success_recovery_rechecks_external_evidence_and_does_not_leak_permission(
    queue_env: QueueEnv, phase: str
) -> None:
    """失敗した復旧の許可や古い外部証拠を使い、未確認のマージ・投稿・片付けを完了として進める。"""
    if phase == "followup":
        run, attempt = queue_env.followup()
        post = queue_env.json_file(
            "post.json", {"key": "note", "kind": "comment", "target": 100, "title": None, "body": "調査済み"}
        )
        published = queue_env.call(
            "publish", "--run", run, "--attempt", attempt["id"], "--input", str(post), pane_id=attempt["pane_id"]
        )
        url = published["run"]["attempts"][-1]["effects"][0]["url"]
        queue_env.report(run, attempt, urls=[url])
        queue_env.child(attempt, "done")
    else:
        run, attempt = successful_stage(queue_env, phase)
    disappear(queue_env, attempt, "pane")
    worktree = queue_env.home / "issue-11-unconfirmed"
    for authorized in [False, True]:
        data = queue_env.data()
        if phase == "implement":
            original = data["issues"]["11"]["closedByPullRequestsReferences"]
            data["issues"]["11"]["closedByPullRequestsReferences"] = []
        elif phase == "followup":
            original = data["comments"][0]["body"]
            data["comments"][0]["body"] += "\n後で変わった内容"
        else:
            original = None
            queue_env.git("worktree", "add", "-q", "-b", "fix/issue-11-left", str(worktree), "HEAD")
        queue_env.save(data)
        if not authorized:
            queue_env.call("resume", "--run", run, "--continue", "--reason", "確認した", code=3)
        rejected = queue_env.call("advance", "--run", run, code=3)
        assert rejected["run"]["phase"] == phase
        assert not rejected["run"]["attempts"][-1]["closed"]
        data = queue_env.data()
        if phase == "implement":
            data["issues"]["11"]["closedByPullRequestsReferences"] = original
        elif phase == "followup":
            data["comments"][0]["body"] = original
        else:
            queue_env.git("worktree", "remove", str(worktree))
            queue_env.git("branch", "-d", "fix/issue-11-left")
        queue_env.save(data)
        if not authorized:
            # 完了の証拠を戻しても、失敗したresumeの仮の許可で進めてはいけない。
            queue_env.call("advance", "--run", run, code=3)
            resumed = queue_env.call("resume", "--run", run, "--continue", "--reason", "改めて確認した")
            assert resumed["next"] == "advance"


def test_success_recovery_rechecks_child_and_keeps_another_kind_pane(queue_env: QueueEnv) -> None:
    """一度確かめた不在を使い回して復活した子と並行に進む、または残った別kindのagentを閉じる。"""
    run, attempt = successful_stage(queue_env, "implement")
    disappear(queue_env, attempt, "agent")
    queue_env.call("resume", "--run", run, "--continue", "--reason", "調査済み")
    data = queue_env.data()
    data["panes"][attempt["pane_id"]] = pane(attempt["pane_id"], "new-child", name="renamed", state="done")
    queue_env.save(data)
    queue_env.call("advance", "--run", run, code=3)
    data = queue_env.data()
    data["panes"][attempt["pane_id"]]["agent"] = "pi"
    queue_env.save(data)
    before = data["panes"].copy()
    assert queue_env.call("advance", "--run", run)["run"]["phase"] == "followup"
    assert queue_env.data()["panes"] == before


def test_live_owner_blocks_takeover_from_new_pane(queue_env: QueueEnv) -> None:
    """生存中の旧ownerから別会話が所有権を奪い、同じrunを二重に操作する。"""
    run, attempt = successful_stage(queue_env, "implement")
    disappear(queue_env, attempt, "pane")
    data = queue_env.data()
    new_pane = pane("w1:p99", "recovery-session", name="recovery-controller")
    data["panes"][new_pane["pane_id"]] = new_pane
    queue_env.save(data)
    state_file = Path(run) / "state.json"
    before = json.loads(state_file.read_text())
    transfers_before = [e for e in before["events"] if e["action"] == "owner_transferred"]
    reason = "  旧ownerの終了を確認して続行。\n 子の完了と外部の記録を確認済み。  "
    args = ("resume", "--run", run, "--continue", "--reason", reason)

    rejected = queue_env.call(*args, pane_id=new_pane["pane_id"], code=3)
    assert rejected["status"] == "blocked"
    saved = json.loads(state_file.read_text())
    assert saved["owner"] == before["owner"]
    assert [e for e in saved["events"] if e["action"] == "owner_transferred"] == transfers_before
    assert saved["attempts"] == before["attempts"]
    after = queue_env.data()
    assert after["panes"] == data["panes"]
    # 照合中は入力・起動・閉鎖を含むHerdrへの書き込みを行わない。
    assert all(call[2] in {"get", "list"} for call in after["calls"][len(data["calls"]) :] if call[0] == "herdr")

    after["panes"].pop("w1:p1")
    queue_env.save(after)
    resumed = queue_env.call(*args, pane_id=new_pane["pane_id"])
    assert (resumed["status"], resumed["next"]) == ("ok", "advance")
    saved = json.loads(state_file.read_text())
    owner = saved["owner"]
    assert owner["pane_id"] == new_pane["pane_id"]
    assert owner["terminal_id"] == new_pane["terminal_id"]
    assert json.loads(owner["session"]) == new_pane["agent_session"]
    transfers = [e for e in saved["events"] if e["action"] == "owner_transferred"]
    assert transfers[:-1] == transfers_before
    assert transfers[-1]["previous"] == before["owner"]
    assert transfers[-1]["current"] == owner
    assert saved["attempts"][-1]["absent_child_reason"] == reason
    assert any(
        e["action"] == "child_absence_confirmed"
        and e["attempt"] == attempt["id"]
        and e["reason"] == reason
        and e["by"] == owner
        for e in saved["events"]
    )
    assert queue_env.data()["panes"] == after["panes"]


def test_success_recovery_cleanup_can_update_main_after_owner_takeover(queue_env: QueueEnv) -> None:
    """旧ownerの記録のまま復旧し続行理由を失う、または古いmainのまま次のIssueを起動する。"""
    run, attempt = successful_stage(queue_env, "cleanup")
    disappear(queue_env, attempt, "pane")
    new_head = queue_env.push_main("README.md", "new main after cleanup\n")
    data = queue_env.data()
    data["panes"]["w1:p1"] = pane("w1:p1", "new-controller")
    queue_env.save(data)
    resumed = queue_env.call("resume", "--run", run, "--continue", "--reason", "旧ownerと子の終了を確認")
    assert (resumed["status"], resumed["next"]) == ("ok", "update_main")
    assert any(e["action"] == "owner_transferred" for e in resumed["run"]["events"])
    queue_env.call("advance", "--run", run, code=3)
    queue_env.update_main()
    advanced = queue_env.call("advance", "--run", run)
    assert advanced["run"]["index"] == 1
    assert queue_env.git("rev-parse", "HEAD") == new_head


@pytest.mark.parametrize("field", ["finalized", "background_stopped"])
def test_success_recovery_does_not_accept_unfinished_work(queue_env: QueueEnv, field: str) -> None:
    """successという結果だけで終了処理・背景処理の停止を省き、まだ動く処理と並行に次工程へ進む。"""
    run = queue_env.init()
    attempt = queue_env.launch(run)
    queue_env.ack(run, attempt)
    queue_env.merge(attempt)
    queue_env.report(run, attempt, **{field: False})
    disappear(queue_env, attempt, "pane")
    queue_env.call("resume", "--run", run, "--continue", "--reason", "調べた")
    assert queue_env.call("advance", "--run", run, code=3)["run"]["phase"] == "implement"
