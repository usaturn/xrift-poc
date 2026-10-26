"""Issueを直列に処理する、手動復旧可能な司令塔CLI。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .external import External
from .lifecycle import acknowledge, launch, submit
from .models import Decision, Init, Result, Run
from .publishing import Publication, publish
from .storage import LockBusy, QueueError, Store
from .workflow import Workflow, ended_message, initialize

# 子のコマンド。司令塔の照合などの終了を待ってから実行する。
CHILD_COMMANDS = {"ack", "report", "publish"}
# Herdrのイベントの待機の上限、wait --max-secondsの照合の間隔、照合のロックのやり直しの間隔・最短の期限（秒）。
HERDR_WAIT_SECONDS = 30
WAIT_POLL_SECONDS = 30
LOCK_RETRY_SECONDS = 1
LOCK_WAIT_SECONDS = 30
ERRORS = (QueueError, OSError, ValueError, KeyError, ValidationError)


def seconds(value: str) -> int:
    """wait --max-secondsの値。Herdrの1回の待機（30秒）から1時間まで。"""
    number = int(value)
    if not 30 <= number <= 3600:
        raise argparse.ArgumentTypeError("30〜3600秒で指定してください")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="方針JSONから新しいrunを作る")
    init.add_argument("--input", type=Path, required=True)
    init.add_argument("--full", action="store_true", help="runの全体も出力する")
    for name in (
        "status",
        "reconcile",
        "launch",
        "submit",
        "advance",
        "wait",
        "resume",
        "abandon",
        "retry",
        "end",
        "stop",
        "ack",
        "report",
        "publish",
        "queue",
        "decision",
    ):
        item = commands.add_parser(name)
        item.add_argument("--run", required=True)
        item.add_argument("--full", action="store_true", help="runの全体も出力する")
        if name in {"ack", "report", "publish"}:
            item.add_argument("--attempt", required=True)
        if name in {"report", "publish"}:
            item.add_argument("--input", required=True, type=Path)
        if name == "wait":
            item.add_argument("--max-seconds", type=seconds, help="この秒数まで待機を繰り返す")
        if name == "stop":
            item.add_argument("--after", choices=("followup", "cleanup"), required=True)
        if name == "resume":
            item.add_argument("--continue", dest="proceed", action="store_true")
            item.add_argument("--reason")
        if name in {"abandon", "retry", "end"}:
            item.add_argument("--reason", required=True)
        if name == "retry":
            # 1つの--handoverに並べても、資源ごとに繰り返してもよい。
            item.add_argument("--handover", type=Path, action="extend", nargs="+")
        if name == "queue":
            item.add_argument("--issues", nargs="+", type=int, required=True)
            item.add_argument("--reason", required=True)
        if name == "decision":
            item.add_argument("--id", required=True)
            item.add_argument("--category", choices=("blocking", "retain", "final"), required=True)
            item.add_argument("--reason", required=True)
            item.add_argument("--resolve", action="store_true")
            item.add_argument("--path", type=Path)
            item.add_argument("--pr", type=int)
    return root


def summary(run: Run) -> dict[str, Any]:
    """次の判断に使う項目。runの全体は--fullかstate.jsonで読む。"""
    attempt = run.current()
    return {
        "issue": None if run.complete else run.queue[run.index],
        "phase": run.phase,
        "attempt": None
        if attempt is None
        else {
            "id": attempt.id,
            "launch": attempt.launch,
            "pane_id": attempt.pane_id,
            "agent_name": attempt.agent_name,
            "acknowledged": attempt.acknowledged,
            "outcome": attempt.result.outcome if attempt.result else None,
            "pr_url": attempt.pr_url,
            # 子は投稿のURLを結果のurlsに書く。
            "effects": [{"key": e.key, "state": e.state, "url": e.url} for e in attempt.effects],
        },
    }


def describe(work: Workflow, *, full: bool) -> dict[str, Any]:
    described: dict[str, Any] = {"run_dir": str(work.path), **summary(work.run)}
    if full:
        described["run"] = work.run.model_dump(mode="json")
    return described


def blocked(error: Exception, work: Workflow | None, *, full: bool) -> tuple[int, dict[str, Any]]:
    next_action = error.next_action if isinstance(error, QueueError) else "inspect"
    response: dict[str, Any] = {"status": "blocked", "reason": str(error), "next": next_action}
    if work is not None:
        response.update(describe(work, full=full))
    return 3, response


def run_command(args: argparse.Namespace, store: Store, ext: External) -> tuple[int, dict[str, Any]]:
    """1つのコマンドをロックの中で実行し、終了コードと応答を返す。stateはロックの中で読み直す。"""
    work: Workflow | None = None
    response: dict[str, Any] = {}
    try:
        # 子のコマンドは司令塔の照合などの終了を待って直列化する。司令塔のコマンドは待たずにbusyで返す。
        with store.lock(wait=args.command in CHILD_COMMANDS):
            if args.command == "init":
                path, run = initialize(store, ext, Init.model_validate_json(args.input.read_text()))
                work = Workflow(store, ext, path, run)
            else:
                path = store.path(args.run)
                work = Workflow(store, ext, path, store.load(args.run))
            try:
                # 終えたrunは記録を見るコマンドだけを受け付ける。子のack・report・publishも記録しない。
                if work.run.ended_reason is not None and args.command not in {"status", "reconcile", "wait"}:
                    raise QueueError(ended_message(work.run.ended_reason), next_action="none")
                # resume・abandon・retry・endは、旧ownerの不在を照合して呼び出し元が実行できる。
                if args.command not in {
                    "status",
                    "init",
                    "ack",
                    "report",
                    "publish",
                    "resume",
                    "abandon",
                    "retry",
                    "end",
                }:
                    work.owner()
                if args.command == "launch":
                    launch(work)
                elif args.command == "submit":
                    submit(work)
                elif args.command == "ack":
                    acknowledge(work, args.attempt)
                elif args.command == "report":
                    work.report(args.attempt, Result.model_validate_json(args.input.read_text()))
                elif args.command == "publish":
                    publish(work, args.attempt, Publication.model_validate_json(args.input.read_text()))
                elif args.command == "queue":
                    prefix = work.run.queue[: work.run.index + 1]
                    if (
                        work.run.complete
                        or args.issues[: len(prefix)] != prefix
                        or len(set(args.issues)) != len(args.issues)
                        or any(n < 1 for n in args.issues)
                    ):
                        raise QueueError("完了済み・現在のIssueは変更できません。重複の無い正の番号を指定してください")
                    added = set(args.issues) - set(work.run.queue)
                    proposed = {
                        int(url.rsplit("/", 1)[-1]) for a in work.run.attempts if a.result for url in a.result.issues
                    }
                    if added and (work.run.policy.additional_issues != "enqueue" or not added <= proposed):
                        raise QueueError("保存した許可の範囲外のIssueは追加できません")
                    for number in args.issues[len(prefix) :]:
                        if work.ext.issue(number)["state"] != "OPEN":
                            raise QueueError("未着手キューにclosedのIssueを入れられません")
                    work.run.queue = args.issues
                    work.run.event("queue_changed", queue=args.issues, reason=args.reason)
                elif args.command == "decision":
                    decision = next((d for d in work.run.decisions if d.id == args.id), None)
                    if args.resolve:
                        if decision is None:
                            raise QueueError("未登録の判断待ちは解決できません")
                        decision.resolution = args.reason
                    elif decision is not None:
                        raise QueueError("判断IDは既に登録されています")
                    else:
                        if args.path and (args.category != "retain" or not args.path.is_absolute()):
                            raise QueueError("保持する資源はretainに絶対パスで指定してください")
                        head = None
                        if args.pr is not None:
                            if args.category != "retain" or args.path or args.pr < 1:
                                raise QueueError("保持するPRはpathと分けてretainにPR番号で指定してください")
                            # 確認した時点のHEADを保存する。保持は削除・マージの許可ではない。
                            pr = next(
                                (p for p in work.ext.pages("pulls?state=open&per_page=100") if p["number"] == args.pr),
                                None,
                            )
                            head = pr["head"]["sha"] if pr else None
                            if not isinstance(head, str) or not head:
                                raise QueueError("同repositoryのopen PRとそのHEADを確認できません")
                        work.run.decisions.append(
                            Decision(
                                id=args.id,
                                category=args.category,
                                reason=args.reason,
                                path=str(args.path.resolve()) if args.path else None,
                                pr=args.pr,
                                pr_head=head,
                            )
                        )
                    work.run.event("decision", id=args.id, reason=args.reason, resolved=args.resolve)
                elif args.command == "resume":
                    work.resume(proceed=args.proceed, reason=args.reason)
                elif args.command == "abandon":
                    work.abandon(args.reason)
                elif args.command == "retry":
                    work.retry(args.reason, args.handover)
                elif args.command == "end":
                    work.end(args.reason)
                elif args.command == "stop":
                    work.run.stop_after = args.after
                    work.run.event("stop_requested", after=args.after)
                elif args.command == "advance":
                    work.advance()
                # waitのHerdrのイベントの待機は、子の記録を待たせないようロックの外で済ませてある（wait_command）。
                if args.command in {"reconcile", "wait", "advance", "resume", "end"}:
                    state, next_action, reason = work.observe()
                else:
                    state, next_action, reason = "ok", "reconcile", "記録を保存しました"
                response = {"status": state, "next": next_action, "reason": reason}
            except (QueueError, OSError, ValueError, KeyError) as error:
                work.run.event("blocked", command=args.command, reason=str(error))
                raise
            finally:
                # 意図保存後のAPI失敗もrunに残す。unknownを未実行に戻さない。
                if args.command != "status":
                    work.save()
            response.update(describe(work, full=args.full))
        return 0, response
    except LockBusy as error:
        # 何も実行していない。一時的な競合で、利用者への報告が要る停止とは区別する。
        return 3, {"status": "busy", "reason": str(error), "next": error.next_action}
    except ERRORS as error:
        return blocked(error, work, full=args.full)


def current_pane(store: Store, run_dir: str) -> str | None:
    """ロックを取らずにstate.jsonを読み、今のattemptのpaneを返す。保存はreplaceなので書きかけの内容は読まない。

    Herdrの待機のきっかけにだけ使う。照合と保存は、run_commandがロックの中で読み直したstateで行う。
    """
    try:
        attempt = store.load(run_dir).current()
    except ERRORS:
        return None
    return attempt.pane_id if attempt else None


def herdr_wait(ext: External, pane_id: str, seconds: float) -> None:
    # settledは完了ではない。待機のtimeout・失敗の後も必ず現在状態を照合する。
    try:
        ext.herdr("agent", "wait", pane_id, "--timeout", str(int(seconds * 1000)))
    except QueueError:
        pass


def observe_locked(
    args: argparse.Namespace, store: Store, ext: External, deadline: float
) -> tuple[int, dict[str, Any]]:
    """照合を実行する。ロックが取れなければ、1秒おきに期限（少なくとも最初の競合から30秒）までやり直す。

    子のpublishなどは数秒でロックを放す。負荷で長引いても、待機を利用者への報告が要る停止にしない。
    """
    code, response = run_command(args, store, ext)
    until = max(deadline, time.monotonic() + LOCK_WAIT_SECONDS)
    while response["status"] == "busy" and time.monotonic() < until:
        time.sleep(LOCK_RETRY_SECONDS)
        code, response = run_command(args, store, ext)
    return code, response


def wait_command(args: argparse.Namespace, store: Store, ext: External) -> tuple[int, dict[str, Any]]:
    """子のpaneのHerdrのイベントをロックの外で待ってから、ロックの中で照合する。

    --max-secondsがあれば約30秒おきに繰り返し、次のどれかで返す: nextがwait以外になる、
    statusかnextがこの呼び出しの最初の照合から変わる、照合が失敗する（終了コード3）、期限に達する。
    各回の照合はロックの中でstateを読み直すので、待機の間の子のack・report・publishの記録を上書きしない。
    """
    start = time.monotonic()
    deadline = start + (args.max_seconds or 0)
    first: tuple[str, str] | None = None
    while True:
        began = time.monotonic()
        if pane_id := current_pane(store, args.run):
            remaining = HERDR_WAIT_SECONDS if args.max_seconds is None else deadline - began
            herdr_wait(ext, pane_id, max(1.0, min(HERDR_WAIT_SECONDS, remaining)))
        code, response = observe_locked(args, store, ext, deadline)
        now = time.monotonic()
        state = (response["status"], response["next"])
        first = first or state
        if code or response["next"] != "wait" or state != first or now >= deadline:
            response["waited_seconds"] = round(now - start)
            return code, response
        # Herdrの待機がすぐ戻った（子がidle・doneなど）ときも、外部状態の照合は約30秒おきにする。
        time.sleep(max(0.0, min(began + WAIT_POLL_SECONDS, deadline) - now))


def main() -> int:
    args = parser().parse_args()
    try:
        parent = Path(os.environ["XRIFT_POC_ROOT"]).resolve()
        ext = External(parent)
        ext.validate_parent()
        store = Store(parent)
    except ERRORS as error:
        code, response = blocked(error, None, full=args.full)
    else:
        code, response = (wait_command if args.command == "wait" else run_command)(args, store, ext)
    print(json.dumps(response, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
