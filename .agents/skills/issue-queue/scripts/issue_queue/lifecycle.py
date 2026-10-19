"""Herdrの副作用は意図を保存してから一度だけ行う。"""

from __future__ import annotations

from uuid import uuid4

from .external import identity
from .models import Attempt
from .prompts import child_prompt
from .storage import QueueError
from .workflow import Workflow


def launch(work: Workflow) -> None:
    work.owner()
    run = work.run
    if run.complete or run.paused or run.stopped_reason:
        raise QueueError("完了・一時停止・停止中には起動できません")
    if any(d.category == "blocking" and d.resolution is None for d in run.decisions):
        raise QueueError("次工程を妨げる判断待ちがあります")
    if run.current() is not None:
        raise QueueError("起動済みまたは起動の成否不明です。reconcileで照合し再送しないでください")
    if run.phase == "implement":
        work.preflight()
    elif run.phase == "cleanup":
        work.resource_barrier()
    token = uuid4().hex
    attempt = Attempt(
        id=token,
        issue=run.queue[run.index],
        phase=run.phase,
        agent_name=f"iq-{token[:24]}",
        marker=f"<!-- issue-queue:{run.id}:{token} -->",
        version=work.ext.version(),
        launch="prepared",
    )
    attempt.prompt = child_prompt(run, attempt, work.path)
    run.attempts.append(attempt)
    run.event("launch_prepared", attempt=token)
    work.save()
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
        f"XRIFT_QUEUE_ATTEMPT={token}",
    )["pane"]
    attempt.pane_id = pane["pane_id"]
    attempt.launch = "pane_created"
    work.save()
    attempt.launch = "starting"
    work.save()
    assert attempt.pane_id is not None
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
        *run.policy.agent_args,
    )["agent"]
    attempt.identity = identity(agent)
    attempt.launch = "started"
    work.save()
    submit(work)


def submit(work: Workflow) -> None:
    work.owner()
    attempt = work.run.current()
    if attempt is None or attempt.launch != "started" or not attempt.identity:
        raise QueueError("未送信と確認できたstarted状態でだけ送信できます")
    agent = work.ext.herdr("agent", "get", attempt.agent_name)["agent"]
    if identity(agent) != attempt.identity or agent["agent_status"] not in {"idle", "done"}:
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
    caller = work.ext.caller()
    if attempt.identity != caller:
        raise QueueError("起動した子のsessionと一致しません")
    attempt.acknowledged = True
    work.run.event("ack", attempt=attempt_id)
    work.save()
