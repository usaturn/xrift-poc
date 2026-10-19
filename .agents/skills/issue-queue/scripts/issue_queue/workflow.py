"""外部の観測と記録から、次に許される工程を決める。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .external import CommandError, External, agent_args, herdr_error_code, identity
from .models import Attempt, Identity, Init, Result, Run, now
from .publishing import check_followup_result, reconcile_effects
from .storage import QueueError, Store


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise QueueError("UTCを含む時刻が必要です")
    return parsed


def initialize(store: Store, ext: External, request: Init) -> tuple[Path, Run]:
    if any(n <= 0 for n in request.issues) or len(set(request.issues)) != len(request.issues):
        raise QueueError("Issue番号は正の整数で、重複なく指定してください")
    agent_args(request.policy.agent_kind, request.policy.agent_args)
    for number in request.issues:
        issue = ext.issue(number)
        if issue["state"] != "OPEN" or "<!-- planning-parallel-issues board:" in issue["body"]:
            raise QueueError("openの独立したIssueだけを指定してください")
    run = Run(
        id=uuid4().hex, parent=str(store.parent), owner=ext.caller(), queue=request.issues, policy=request.policy
    )
    run.event("init", version=ext.version())
    return store.create(run), run


class Workflow:
    def __init__(self, store: Store, ext: External, path: Path, run: Run):
        self.store, self.ext, self.path, self.run = store, ext, path, run

    def save(self) -> None:
        self.store.save(self.path, self.run)

    def owner(self) -> None:
        if self.ext.caller() != self.run.owner:
            raise QueueError("この会話はownerではありません。resumeで実環境と照合してください")

    def started(self, attempt: Attempt, agent: dict[str, Any]) -> None:
        """起動した一意名・kind・pane・terminalを照合し、native sessionが現れていれば確定する。

        Codexのようにstart時点でsessionの無いagentは、最初のackまでに確定する。確定後の変更は拒否する。
        """
        if (
            agent.get("name") != attempt.agent_name
            or agent.get("agent") != self.run.policy.agent_kind
            or agent.get("pane_id") != attempt.pane_id
            or agent.get("terminal_id") != attempt.pane_terminal_id
        ):
            raise QueueError("起動した子のagent名・kind・pane・terminalが一致しません")
        if agent.get("agent_session"):
            current = identity(agent)
            if attempt.identity is None:
                attempt.identity = current
            elif current != attempt.identity:
                raise QueueError("子のnative sessionが確定後に変わっています")

    def child(self, attempt_id: str) -> Attempt:
        attempt = self.run.current()
        if attempt is None or attempt.id != attempt_id:
            raise QueueError("今回の工程のattemptではありません")
        caller = self.ext.caller()
        if attempt.identity != caller:
            raise QueueError("子のpaneまたはnative sessionが一致しません")
        return attempt

    def progress_barrier(self) -> tuple[str, str, str] | None:
        if self.run.complete:
            return "complete", "none", "全Issueの処理が完了しました"
        if self.run.paused:
            return "paused", "resume", "保存した境界で停止しています"
        if self.run.stopped_reason:
            return "blocked", "inspect", self.run.stopped_reason
        if any(d.category == "blocking" and d.resolution is None for d in self.run.decisions):
            return "blocked", "decision", "次工程を妨げる判断待ちがあります"
        return None

    def require_progress(self) -> None:
        if barrier := self.progress_barrier():
            raise QueueError(barrier[2])

    def check_takeover(self, caller: Identity) -> None:
        for agent in self.ext.agents():
            # 呼び出し元自身は旧ownerと数えない。同じ会話を別pane・新しいterminalで復旧できる。
            if (agent.get("pane_id"), agent.get("terminal_id")) == (caller.pane_id, caller.terminal_id):
                continue
            if agent.get("agent_session") and identity(agent).session == self.run.owner.session:
                raise QueueError("元の司令塔がまだ存在します。古さを理由に引き継げません")

    def abandon(self, reason: str) -> None:
        """受領前に子が消えたattemptを閉じる。同じ工程は次のlaunchで新しいattemptとして起動する。

        paneは閉じず、ownerも移さない。工程の境界の操作なので使用版は照合しない。
        """
        if not reason.strip():
            raise QueueError("打ち切る理由を指定してください")
        caller = self.ext.caller()
        if caller != self.run.owner:
            self.check_takeover(caller)
        attempt = self.run.current()
        if attempt is None or not attempt.pane_id:
            raise QueueError("打ち切れるattemptがありません。pane作成前の起動予定はlaunchで続けてください")
        if attempt.acknowledged or attempt.result is not None:
            raise QueueError("受領済みのattemptは打ち切れません。子の結果と作業を確認してください")
        try:
            self.ext.herdr("agent", "get", attempt.agent_name)
        except CommandError as error:
            # 不在はHerdrの構造化エラーのcodeの完全一致だけで判定する。接続失敗・別のcodeは不在にしない。
            if herdr_error_code(error.stderr) != "agent_not_found":
                raise
        else:
            raise QueueError("子のagentがまだ存在します。停止を確認してから打ち切ってください")
        pane = self.ext.pane(attempt.pane_id)
        if (
            pane
            and pane.get("terminal_id") == attempt.pane_terminal_id
            and pane.get("agent") == self.run.policy.agent_kind
        ):
            raise QueueError("子のpaneで同じkindのagentが動いています。停止を確認してから打ち切ってください")
        if any(attempt.marker in (p.get("body") or "") for p in self.ext.pages("pulls?state=all&per_page=100")):
            raise QueueError("このattemptのマーカーを持つPRがあります。打ち切らずに照合してください")
        attempt.closed = True
        attempt.abandon_reason = reason
        self.run.event("attempt_abandoned", attempt=attempt.id, reason=reason)
        if self.run.stopped_reason:
            # blocked・監視上限は閉じたattemptの子についての停止なので、打ち切りで解消する。
            self.run.event("stop_resolved", previous=self.run.stopped_reason, reason=reason)
            self.run.stopped_reason = None
        self.save()

    def resume(self, *, proceed: bool, reason: str | None = None) -> None:
        caller = self.ext.caller()
        if caller != self.run.owner:
            self.check_takeover(caller)
            # 保存値以外に子・GitHub・worktreeも取得できた時だけownerを移す。
            self.observe()
            self.run.event("owner_transferred", previous=self.run.owner.model_dump(), current=caller.model_dump())
            self.run.owner = caller
        else:
            self.observe()
        if proceed:
            if self.run.stopped_reason:
                previous = self.run.stopped_reason
                attempt = self.run.current()
                pane = self.ext.pane(attempt.pane_id) if attempt and attempt.pane_id else None
                if not reason:
                    raise QueueError("停止原因が未解消です。子の結果と判断待ちを確認してください")
                if previous.startswith("監視上限"):
                    # 上限で子を止めてはいない。完了を照合できた時だけ、明示指示で進行を再開する。
                    self.run.stopped_reason = None
                    try:
                        status, action, _ = self.observe()
                        if (status, action) != ("ok", "advance"):
                            raise QueueError("監視上限後の子の完了を確認できません")
                    except QueueError, OSError, ValueError, KeyError:
                        self.run.stopped_reason = previous
                        raise
                elif (
                    previous.startswith("子がblocked")
                    and pane
                    and pane["agent_status"] in {"working", "idle", "done"}
                    and not (attempt and attempt.result and attempt.result.outcome != "success")
                ):
                    self.run.stopped_reason = None
                else:
                    raise QueueError("停止原因が未解消です。子の結果と判断待ちを確認してください")
                self.run.event("stop_resolved", previous=previous, reason=reason)
            self.run.paused = False
            self.run.stop_after = None
            self.run.event("continue")
        self.save()

    def preflight(self) -> None:
        issue = self.ext.issue(self.run.queue[self.run.index])
        if issue["state"] != "OPEN" or "<!-- planning-parallel-issues board:" in issue["body"]:
            raise QueueError("Issueがclosedまたはボードの子です")
        dependencies = self.ext.pages(f"issues/{issue['number']}/dependencies/blocked_by")
        if any(d["state"].lower() == "open" for d in dependencies):
            raise QueueError("先行Issueが未完了です")
        inventory = self.resource_barrier()
        if not inventory["head"] == inventory["origin_main"] == inventory["remote_main"] or inventory["branch"]:
            raise QueueError("親main checkoutを最新origin/mainのdetached HEADに更新してください")

    def resource_barrier(self) -> dict[str, Any]:
        inventory = self.ext.inventory()
        self.run.observation = inventory
        if inventory["claims"] or inventory["local_claims"]:
            raise QueueError("未解放のclaimがあります")
        # 確認時のHEADのまま残るopen PRだけを保持として通す。
        retained = {(d.pr, d.pr_head) for d in self.run.decisions if d.category == "retain" and d.pr is not None}
        unconfirmed = [p["number"] for p in inventory["open_prs"] if (p["number"], p["head"]["sha"]) not in retained]
        if unconfirmed:
            raise QueueError(
                f"未確認または確認後に更新されたopen PRがあります。処理対象と競合を確認してください: {unconfirmed}"
            )
        allowed = {str(self.ext.parent)}
        allowed.update(d.path for d in self.run.decisions if d.category == "retain" and d.path)
        for attempt in self.run.attempts:
            if attempt.result:
                allowed.update(r.path for r in attempt.result.retained if not r.blocks_next)
        unknown = [w["worktree"] for w in inventory["worktrees"] if w["worktree"] not in allowed]
        if unknown:
            raise QueueError(f"未確認のworktreeがあります: {unknown}")
        return inventory

    def observe_merge(self, attempt: Attempt) -> dict[str, Any] | None:
        issue = self.ext.issue(attempt.issue)
        candidates = self.ext.pages("pulls?state=all&per_page=100")
        matches = [p for p in candidates if attempt.marker in (p.get("body") or "")]
        if len(matches) > 1:
            raise QueueError("今回のattemptに対応するPRが複数あります")
        if not matches:
            return None
        pr = self.ext.pr(matches[0].get("html_url") or matches[0]["url"])
        if pr["isCrossRepository"] or pr["baseRefName"] != "main":
            raise QueueError("既定branch以外またはforkのPRは対象外です")
        if (
            attempt.marker not in pr["body"]
            or timestamp(pr["createdAt"]) < timestamp(attempt.created_at)
            or not re.search(rf"(?:^|/)issue-{attempt.issue}(?:-|$)", pr["headRefName"])
        ):
            raise QueueError("PRと今回のIssue/attemptの対応が不正です")
        attempt.pr_url = pr["url"]
        if issue["state"] != "CLOSED" or pr["state"] != "MERGED":
            return None
        if pr["url"] not in {p["url"] for p in issue["closedByPullRequestsReferences"]}:
            raise QueueError("今回のPRが対象Issueを閉じたことを確認できません")
        if not attempt.merged_at:
            attempt.merged_at = now()
            self.run.event("merge_observed", attempt=attempt.id, pr=pr["url"])
        return pr

    def check_loop(self, attempt: Attempt) -> None:
        assert attempt.result is not None
        if not attempt.result.loop_run:
            raise QueueError("レビュー修正ループの終了記録がありません")
        path = Path(attempt.result.loop_run).resolve()
        if not path.is_relative_to(self.ext.parent / "reviews") or path.name != "run.json":
            raise QueueError("ループの記録は親のreviews配下で指定してください")
        loop = json.loads(path.read_text())
        if (
            loop["pr"]["repository"] != self.run.repository
            or loop["pr"]["url"] != attempt.pr_url
            or loop["state"] != "ended"
            or loop["result"]["outcome"] != "no_fix_needed"
            or loop["result"]["merged"] is not True
            or loop["result"]["ended_by"] != "outcome"
            or timestamp(loop["started_at"]) < timestamp(attempt.created_at)
            or not loop.get("ended_at")
            or (path.parent.parent / "lock.json").exists()
            or any(r["state"] == "created" for r in loop["resources"])
        ):
            raise QueueError("今回のループの終了・マージ・資源の停止を確認できません")

    def observe(self) -> tuple[str, str, str]:
        if self.run.complete:
            return "complete", "none", "全Issueの処理が完了しました"
        inventory = self.ext.inventory()
        self.run.observation = {"at": now(), **inventory}
        attempt = self.run.current()
        if attempt is None:
            return self.progress_barrier() or ("ok", "launch", "次の工程を起動できます")
        if attempt.phase != "cleanup" and self.ext.version() != attempt.version:
            raise QueueError("工程の途中でスキル・helper・mainの版が変わりました。境界で照合してください")
        if attempt.launch == "prepared":
            return self.progress_barrier() or ("ok", "launch", "保存した起動予定の未実行部分を進められます")
        pane = self.ext.pane(attempt.pane_id) if attempt.pane_id else None
        if not attempt.pane_id:
            return "blocked", "inspect", "pane作成の成否不明です。所有を照合するまで再作成しません"
        if pane is None and not attempt.closing:
            return "blocked", "inspect", "子のpaneが見つかりません。再起動せず調査してください"
        if attempt.launch == "starting" or (attempt.identity is None and attempt.launch != "pane_created"):
            # startの応答が失われても、sessionが初回入力後に現れるagentでも、
            # 保存した一意名・kind・pane・terminalを照合できれば同じ起動先として扱う。
            self.started(attempt, self.ext.herdr("agent", "get", attempt.agent_name)["agent"])
            if attempt.launch == "starting":
                attempt.launch = "started"
        if pane is not None and pane.get("agent_status") == "blocked":
            self.run.stopped_reason = "子がblockedです。司令塔は代わりに回答しません"
            return "blocked", "inspect", self.run.stopped_reason
        if pane and attempt.identity and identity(pane) != attempt.identity:
            raise QueueError("paneの担当が変わっています")
        if barrier := self.progress_barrier():
            return barrier
        if attempt.launch == "pane_created":
            return "ok", "launch", "paneは保存済みです。未実行のstartから再開できます"
        if attempt.launch == "started":
            return "ok", "submit", "担当の起動を照合しました。まだ送信していません"
        merged = self.observe_merge(attempt) if attempt.phase == "implement" else None
        if attempt.phase == "followup":
            reconcile_effects(self, attempt)
        elapsed = (datetime.now(UTC) - timestamp(attempt.created_at)).total_seconds()
        if attempt.result is None and elapsed >= self.run.policy.monitor_seconds:
            self.run.stopped_reason = "監視上限です。子を強制終了せず状況を報告してください"
            return "blocked", "inspect", self.run.stopped_reason
        if not attempt.acknowledged:
            return "waiting", "wait", "依頼の受領を待っています。再送しません"
        if attempt.result is None:
            return "waiting", "wait", "明示的な終了結果を待っています"
        result = attempt.result
        if result.outcome != "success" or not result.finalized or not result.background_stopped:
            self.run.stopped_reason = "子の依頼が正常終了していません: " + result.summary
            return "blocked", "inspect", self.run.stopped_reason
        if pane is None:
            if not attempt.closing:
                raise QueueError("所有paneが終了確認前に消えました")
        elif pane["agent_status"] not in {"idle", "done"}:
            return "waiting", "wait", "結果は保存済みですが子の終了処理を待っています"
        if attempt.phase == "implement":
            if merged is None:
                return "waiting", "wait", "今回のPRによるマージとIssueのcloseを待っています"
            self.check_loop(attempt)
        if attempt.phase == "followup":
            check_followup_result(self, attempt)
        if attempt.phase == "cleanup":
            self.resource_barrier()
            if not inventory["head"] == inventory["origin_main"] == inventory["remote_main"] or inventory["branch"]:
                raise QueueError("片付け後のmain checkoutがorigin/mainと一致しません")
            if inventory["claims"] or inventory["local_claims"]:
                raise QueueError("片付け後に未解放のclaimがあります")
        if any(d.category == "blocking" and d.resolution is None for d in self.run.decisions):
            return "blocked", "decision", "次工程を妨げる判断待ちがあります"
        if any(r.blocks_next for r in result.retained):
            return "blocked", "inspect", "次工程を妨げる資源があります"
        if self.run.stopped_reason:
            return "blocked", "inspect", self.run.stopped_reason
        return "ok", "advance", "終了結果と外部状態を確認しました"

    def report(self, attempt_id: str, result: Result) -> None:
        attempt = self.child(attempt_id)
        if not attempt.acknowledged:
            raise QueueError("ackの前に結果を記録できません")
        if attempt.result is not None:
            if attempt.result != result:
                raise QueueError("終了結果は上書きできません")
            return
        attempt.result = result
        attempt.finished_at = now()
        self.run.event("result", attempt=attempt.id, outcome=result.outcome)
        self.save()

    def advance(self) -> None:
        self.owner()
        status, next_action, reason = self.observe()
        if status != "ok" or next_action != "advance":
            raise QueueError(reason)
        attempt = self.run.current()
        assert attempt is not None and attempt.pane_id
        attempt.closing = True
        self.save()
        if self.ext.pane(attempt.pane_id) is not None:
            self.ext.herdr("pane", "close", attempt.pane_id)
        attempt.closed = True
        self.run.event("advanced", issue=attempt.issue, phase=attempt.phase)
        if attempt.phase == "implement":
            self.run.phase = "followup"
        elif attempt.phase == "followup":
            self.run.phase = "cleanup"
        else:
            self.run.index += 1
            self.run.phase = "implement"
            self.run.complete = self.run.index == len(self.run.queue)
        if self.run.stop_after == attempt.phase or (
            self.run.policy.trial and attempt.phase == "cleanup" and self.run.index == 1
        ):
            self.run.paused = not self.run.complete
        self.save()
