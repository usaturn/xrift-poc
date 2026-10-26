"""Herdrの副作用は意図を保存してから一度だけ行う。"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from .external import CommandError, agent_args, herdr_error_code, identity
from .models import Attempt
from .prompts import child_prompt
from .publishing import inherited_effects
from .storage import QueueError
from .workflow import Workflow, start_refused

# 新しいpaneのシェルの準備を待つ上限と間隔。上限はHerdrのagent startの準備の待機の既定と同じ。
START_REFUSAL_SECONDS = 30
START_REFUSAL_INTERVAL = 1


def launch(work: Workflow) -> None:
    work.owner()
    run = work.run
    work.require_progress()
    attempt = run.current()
    if attempt is not None and attempt.launch not in {"prepared", "pane_created"}:
        raise QueueError("起動済みまたは起動の成否不明です。reconcileで照合し再送しないでください")
    if run.phase == "implement":
        work.preflight(new=attempt is None)
    elif run.phase == "cleanup":
        work.resource_barrier("cleanup_launch")
    if attempt is None:
        token = uuid4().hex
        attempt = Attempt(
            id=token,
            issue=run.queue[run.index],
            phase=run.phase,
            agent_name=f"iq-{token[:24]}",
            marker=f"<!-- issue-queue:{run.id}:{token} -->",
            version=work.ext.version(),
            launch_args=agent_args(run.policy.agent_kind, run.policy.agent_args),
            launch="prepared",
        )
        # retryで引き継いだ持ち越しの投稿は、このattemptの記録として照合する。同じkeyの投稿は照合だけになる。
        attempt.effects = inherited_effects(run, attempt)
        attempt.prompt = child_prompt(run, attempt, work.path)
        run.attempts.append(attempt)
        run.event("launch_prepared", attempt=token)
        work.save()
    elif work.ext.version() != attempt.version:
        raise QueueError("保存した起動予定から使用版が変わっています。abandonで閉じてから起動し直してください")
    if attempt.launch == "prepared":
        attempt.launch = "splitting"
        work.save()
        pane = work.ext.herdr(
            "pane",
            "split",
            "--pane",
            run.owner.pane_id,
            "--direction",
            "right",
            "--cwd",
            run.parent,
            "--no-focus",
            "--env",
            f"XRIFT_QUEUE_ATTEMPT={attempt.id}",
        )["pane"]
        attempt.pane_id = pane["pane_id"]
        attempt.pane_terminal_id = pane["terminal_id"]
        attempt.launch = "pane_created"
        work.save()
    agent = start_agent(work, attempt)
    work.started(attempt, agent)
    attempt.launch = "started"
    work.save()
    submit(work)


def start_agent(work: Workflow, attempt: Attempt) -> dict[str, Any]:
    """保存したpaneでagent startを送り、起動したagentを返す。

    新しいpaneのシェルの準備の前のstartは、Herdrが構造化エラーagent_pane_busyで拒否する（startは実行されない）。
    名前の子が居ないことも確かめてからpane_createdへ戻し、同じpaneの照合からやり直す。timeout・JSONでない応答・
    別のcodeは成否不明なので、startingのまま送出する。上限を超えたら停止を保存し、paneを増やさずに止まる。
    """
    run = work.run
    assert attempt.pane_id is not None
    if attempt.launch_args is None:
        raise QueueError("保存した起動予定に起動引数がありません")
    deadline = time.monotonic() + START_REFUSAL_SECONDS
    refused = 0
    while True:
        pane = work.ext.pane(attempt.pane_id)
        if (
            not pane
            or pane["terminal_id"] != attempt.pane_terminal_id
            or pane.get("agent")
            or pane.get("agent_session")
        ):
            raise QueueError("保存したpaneが未使用であることを確認できません")
        attempt.launch = "starting"
        work.save()
        try:
            agent = work.ext.herdr(
                "agent",
                "start",
                attempt.agent_name,
                "--kind",
                run.policy.agent_kind,
                "--pane",
                attempt.pane_id,
                "--timeout",
                "30000",
                "--",
                *attempt.launch_args,
            )["agent"]
        except CommandError as error:
            if herdr_error_code(error.stderr) != "agent_pane_busy" or work.named_agent(attempt) is not None:
                if refused:
                    run.event("start_refused", attempt=attempt.id, pane=attempt.pane_id, count=refused)
                raise
            refused += 1
            attempt.launch = "pane_created"
            work.save()
            if time.monotonic() >= deadline:
                run.event("start_refused", attempt=attempt.id, pane=attempt.pane_id, count=refused)
                run.stopped_reason = (
                    f"{start_refused(attempt.pane_id)}（{START_REFUSAL_SECONDS}秒の間にstartを{refused}回拒否されました）。"
                    "paneを調べてからresume --continue --reasonで解除すると、launchで同じpaneから起動し直せます"
                )
                raise QueueError(run.stopped_reason) from error
            time.sleep(START_REFUSAL_INTERVAL)
            continue
        if refused:
            run.event("start_refused", attempt=attempt.id, pane=attempt.pane_id, count=refused)
        return agent


def submit(work: Workflow) -> None:
    work.owner()
    work.require_progress()
    attempt = work.run.current()
    if attempt is None or attempt.launch != "started":
        raise QueueError("未送信と確認できたstarted状態でだけ送信できます")
    agent = work.ext.herdr("agent", "get", attempt.agent_name)["agent"]
    work.started(attempt, agent)
    if agent["agent_status"] not in {"idle", "done"}:
        raise QueueError("起動した担当が入力待ちではありません")
    attempt.launch = "submitting"
    work.save()
    work.ext.herdr("agent", "prompt", attempt.agent_name, attempt.prompt)
    attempt.launch = "submitted"
    work.save()


def acknowledge(work: Workflow, attempt_id: str) -> None:
    attempt = work.run.current()
    if attempt is None or attempt.id != attempt_id:
        raise QueueError("今回のattemptではありません")
    if attempt.launch not in {"submitting", "submitted"}:
        raise QueueError("依頼の送信前にはackできません")
    # 初回のackで呼び出し元のnative sessionを確定する。別pane・terminal・agentからは受け付けない。
    agent = work.ext.caller_agent()
    work.started(attempt, agent)
    if attempt.identity is None or identity(agent) != attempt.identity:
        raise QueueError("起動した子のsessionと一致しません")
    attempt.acknowledged = True
    work.run.event("ack", attempt=attempt_id)
    work.save()
