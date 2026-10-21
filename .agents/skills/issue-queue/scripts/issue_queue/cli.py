"""Issueを直列に処理する、手動復旧可能な司令塔CLI。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .external import External
from .lifecycle import acknowledge, launch, submit
from .models import Decision, Init, Result
from .publishing import Publication, publish
from .storage import QueueError, Store
from .workflow import Workflow, ended_message, initialize


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="方針JSONから新しいrunを作る")
    init.add_argument("--input", type=Path, required=True)
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
        if name in {"ack", "report", "publish"}:
            item.add_argument("--attempt", required=True)
        if name in {"report", "publish"}:
            item.add_argument("--input", required=True, type=Path)
        if name == "stop":
            item.add_argument("--after", choices=("followup", "cleanup"), required=True)
        if name == "resume":
            item.add_argument("--continue", dest="proceed", action="store_true")
            item.add_argument("--reason")
        if name in {"abandon", "retry", "end"}:
            item.add_argument("--reason", required=True)
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


def main() -> int:
    args = parser().parse_args()
    work: Workflow | None = None
    response: dict[str, Any] = {}
    try:
        parent = Path(os.environ["XRIFT_POC_ROOT"]).resolve()
        ext = External(parent)
        ext.validate_parent()
        store = Store(parent)
        # 子のコマンドは司令塔のwait・reconcileの終了を待って直列化する。司令塔同士の競合は拒否する。
        with store.lock(wait=args.command in {"ack", "report", "publish"}):
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
                    work.retry(args.reason)
                elif args.command == "end":
                    work.end(args.reason)
                elif args.command == "stop":
                    work.run.stop_after = args.after
                    work.run.event("stop_requested", after=args.after)
                elif args.command == "advance":
                    work.advance()
                elif args.command == "wait":
                    attempt = work.run.current()
                    if attempt and attempt.pane_id:
                        # settledは完了ではない。待機のtimeout後も必ず現在状態を取得する。
                        try:
                            work.ext.herdr("agent", "wait", attempt.pane_id, "--timeout", "30000")
                        except QueueError:
                            pass
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
            response.update(run_dir=str(work.path), run=work.run.model_dump(mode="json"))
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except (QueueError, OSError, ValueError, KeyError, ValidationError) as error:
        next_action = error.next_action if isinstance(error, QueueError) else "inspect"
        response = {"status": "blocked", "reason": str(error), "next": next_action}
        if work is not None:
            response.update(run_dir=str(work.path), run=work.run.model_dump(mode="json"))
        print(json.dumps(response, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
