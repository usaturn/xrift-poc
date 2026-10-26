"""外部の観測と記録から、次に許される工程を決める。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .external import CommandError, External, agent_args, herdr_error_code, identity
from .models import Attempt, Identity, IgnoredWorktree, Init, Result, Run, now
from .publishing import check_followup_result, reconcile_effects
from .storage import QueueError, Store

# 確認していないworktreeを照合する検査。無視したworktreeの記録に残す。
Check = Literal["implement_launch", "cleanup_launch", "cleanup_finish"]
MainState = Literal["branch", "current", "outdated"]
MAIN_ON_BRANCH = "親main checkoutがbranchにいます。queueは更新しません。detached HEADに戻してから照合し直してください"
# ack済みで結果の無いattemptの子の状態。閉じる操作（retry・end）は子の不在を照合してから受け付ける。
UNREPORTED_PANE_MISSING = (
    "子のpaneが見つかりません。子が結果を残さずに終わったか調べ、retryで同じ工程をやり直すかendでrunを終えてください"
)
UNREPORTED_AGENT_EXITED = (
    "子が結果を残さずに終了しました（paneにagentがいません）。"
    "子の作業を確かめ、retryで同じ工程をやり直すかendでrunを終えてください"
)
# 子を起動する前のattempt。どちらもabandonで閉じ、同じ工程を新しいattemptとして起動し直せる。
LAUNCH_PLAN_OUTDATED = "子の起動前に使用版が変わりました。abandonで閉じてから起動し直してください"
SPLIT_UNKNOWN = (
    "pane作成の成否不明です。所有を照合するまで再作成しません。"
    "作られたかもしれないpaneを調べた後はabandonで閉じられます"
)
UNREPORTED_TURN_ENDED = (
    "子が結果を残さずにturnを終えています。バックグラウンドの処理を待っているか、止まっています。"
    "子のpaneを確かめてください"
)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise QueueError("UTCを含む時刻が必要です")
    return parsed


def mentions_issue(text: str, number: int) -> bool:
    """issue-<番号>を番号の境界で探す。issue-11はissue-110やxissue-11に当たらない。"""
    return re.search(rf"(?<![0-9a-z])issue-{number}(?![0-9])", text, re.IGNORECASE) is not None


def main_state(inventory: dict[str, Any]) -> MainState:
    """HEAD・ローカルのorigin/main・remoteのmainが一致するdetached HEADだけをcurrentとする。"""
    if inventory["branch"]:
        return "branch"
    if inventory["head"] == inventory["origin_main"] == inventory["remote_main"]:
        return "current"
    return "outdated"


def outdated_message(inventory: dict[str, Any]) -> str:
    return (
        f"親main checkout（{inventory['head'][:12]}）が最新のorigin/main（{inventory['remote_main'][:12]}）と"
        "一致しません。repo-cleanup-after-mergeの更新の手順で更新してから照合し直してください"
    )


def start_refused(pane_id: str) -> str:
    """新しいpaneがシェルの入力待ちにならず、agent startの拒否が上限まで続いた停止の理由の書き出し。

    attemptはpane_created（startは実行されていない）のまま残る。resume --continueは、今のattemptのpaneの
    この書き出しの停止だけを解除し、次のlaunchが同じpaneから起動し直す。
    """
    return f"子のpane {pane_id} がシェルの入力待ちになりません"


def ended_message(reason: str) -> str:
    return f"終了したrunです（{reason}）。新しいrunはinitで作ってください"


def initialize(store: Store, ext: External, request: Init) -> tuple[Path, Run]:
    if any(n <= 0 for n in request.issues) or len(set(request.issues)) != len(request.issues):
        raise QueueError("Issue番号は正の整数で、重複なく指定してください")
    agent_args(request.policy.agent_kind, request.policy.agent_args)
    for number in request.issues:
        issue = ext.issue(number)
        if issue["state"] != "OPEN" or ext.is_board_child(issue):
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
        if self.run.ended_reason is not None:
            return "ended", "none", ended_message(self.run.ended_reason)
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
        """受領前に子が消えたattemptと、子を起動する前のattemptを閉じる。同じ工程は次のlaunchで新しいattemptとして起動する。

        子を起動する前のattemptは、使用版の変わったprepared（splitを送っていない）と、pane作成の成否不明のsplitting
        （startを送っていない）。paneは閉じず、ownerも移さない。工程の境界の操作なので使用版は照合しない。
        """
        if not reason.strip():
            raise QueueError("打ち切る理由を指定してください")
        caller = self.ext.caller()
        if caller != self.run.owner:
            self.check_takeover(caller)
        attempt = self.run.current()
        if attempt is None:
            raise QueueError("打ち切れるattemptがありません")
        if attempt.launch == "prepared":
            # 使用版が同じなら、保存した起動予定をlaunchで続けられる。
            if self.ext.version() == attempt.version:
                raise QueueError("打ち切れるattemptがありません。pane作成前の起動予定はlaunchで続けてください")
        elif attempt.launch == "splitting":
            # 名前の子が居れば起動の経緯が記録と合わない。splitが作ったかもしれないpaneは閉じず、利用者が調べる。
            if self.named_agent(attempt) is not None:
                raise QueueError("子のagentが存在します。起動の経緯を調べてください")
        else:
            if attempt.acknowledged or attempt.result is not None:
                raise QueueError("受領済みのattemptは打ち切れません。子の結果と作業を確認してください")
            named, in_pane = self.child_agents(attempt, caller)
            if named is not None:
                raise QueueError("子のagentがまだ存在します。停止を確認してから打ち切ってください")
            if in_pane is not None:
                raise QueueError("子のpaneで同じkindのagentが動いています。停止を確認してから打ち切ってください")
            if self.marked_pr(attempt):
                raise QueueError("このattemptのマーカーを持つPRがあります。打ち切らずに照合してください")
        attempt.closed = True
        attempt.abandon_reason = reason
        self.run.event("attempt_abandoned", attempt=attempt.id, reason=reason)
        if self.run.stopped_reason:
            # blocked・監視上限は閉じたattemptの子についての停止なので、打ち切りで解消する。
            self.run.event("stop_resolved", previous=self.run.stopped_reason, reason=reason)
            self.run.stopped_reason = None
        self.save()

    def retry(self, reason: str, handover: list[Path] | None = None) -> None:
        """受領済みの子が止まったattemptを閉じる。同じ工程は次のlaunchで新しいattemptとして起動し、作業を引き継ぐ。

        結果を残した子は終了処理とバックグラウンドの停止を報告済みなので、残っていてもidle・doneならよい。
        結果を残さずに終わった子は、居なくなったことを照合する。handoverは司令塔が照合して引き継ぐ資源のパス。
        paneは閉じず、ownerも移さず、使用版は照合しない。
        """
        if not reason.strip():
            raise QueueError("やり直す理由を指定してください")
        caller = self.ext.caller()
        if caller != self.run.owner:
            self.check_takeover(caller)
        attempt = self.run.current()
        if attempt is None or not attempt.acknowledged:
            raise QueueError("やり直せるattemptがありません。受領前に子が消えたattemptはabandonで閉じてください")
        result = attempt.result
        if result is not None:
            if result.outcome == "success":
                raise QueueError("成功した結果はadvanceで進めてください")
            if not result.finalized or not result.background_stopped:
                raise QueueError("子の終了処理またはバックグラウンドの停止を確認できません")
        if attempt.effects:
            # 投稿をすべて読み戻して照合する。成否不明・重複・不一致の投稿は再送しないので、やり直さない。
            # 照合した投稿の記録は次のattemptへ写し、同じkeyの投稿は照合だけになる（lifecycle.launch）。
            reconcile_effects(self, attempt)
        if not self.child_stopped(attempt, caller):
            if result is None:
                raise QueueError(
                    "子がまだ居ます。結果を残さずに止まった子は、利用者が子を終了させてからやり直してください"
                )
            raise QueueError("子がまだ動いています。turnの終了を確認してからやり直してください")
        if self.marked_pr(attempt):
            raise QueueError("このattemptのマーカーを持つPRがあります。やり直さずに照合してください")
        handed = self.handover_paths(attempt, handover or [])
        attempt.closed = True
        attempt.retry_reason = reason
        attempt.handover = handed
        self.run.event(
            "attempt_retried",
            attempt=attempt.id,
            reason=reason,
            outcome=result.outcome if result else None,
            handover=handed,
        )
        if self.run.stopped_reason:
            self.run.event("stop_resolved", previous=self.run.stopped_reason, reason=reason)
            self.run.stopped_reason = None
        self.save()

    def handover_paths(self, attempt: Attempt, paths: list[Path]) -> list[str]:
        """retryで新しい子へ引き継ぐ資源を照合する。実装の工程だけで、親main checkout以外の存在する絶対パスのうち、
        パス（worktreeならbranchも）に処理中のIssueの番号が番号の境界で現れるもの。worktreeは一覧の形のパスで返す。

        作り手は確かめられない（retainedと同じ）。新しい子はissue-to-prの引き継ぎでworktreeを照合し直す。
        """
        if not paths:
            return []
        if attempt.phase != "implement":
            raise QueueError("資源を引き継げるのは実装の工程だけです")
        worktrees = {str(Path(w["worktree"]).resolve()): w for w in self.ext.worktrees()}
        handed: list[str] = []
        for path in paths:
            if not path.is_absolute() or not path.exists():
                raise QueueError(f"引き継ぐ資源は存在する絶対パスで指定してください: {path}")
            resolved = path.resolve()
            if resolved == self.ext.parent:
                raise QueueError("親main checkoutは引き継げません")
            worktree = worktrees.get(str(resolved))
            names = [worktree["worktree"], worktree.get("branch", "").removeprefix("refs/heads/")] if worktree else []
            if not any(mentions_issue(name, attempt.issue) for name in [str(path), *names]):
                raise QueueError(f"処理中のIssue #{attempt.issue} の資源ではありません: {path}")
            handed.append(worktree["worktree"] if worktree else str(path))
        return handed

    def end(self, reason: str) -> None:
        """続けられないrunを、子の停止を照合して理由とともに終える。終えたrunは新しいrunのinitを止めない。

        attempt・pane・資源には触れず、ownerも移さない。次へ進めないので、PR・投稿・判断待ちは条件にしない。
        """
        if not reason.strip():
            raise QueueError("終える理由を指定してください")
        if self.run.complete or self.run.ended_reason is not None:
            raise QueueError("完了または終了したrunは終えられません", next_action="none")
        caller = self.ext.caller()
        if caller != self.run.owner:
            self.check_takeover(caller)
        attempt = self.run.current()
        if attempt is not None and attempt.launch != "prepared":
            if attempt.launch in {"splitting", "pane_created"}:
                # startを送っていないので、子は名前で見つからないはず。見つかれば起動の経緯を調べる。
                if self.named_agent(attempt) is not None:
                    raise QueueError("子のagentが存在します。停止を確認してから終えてください")
            elif not self.child_stopped(attempt, caller):
                raise QueueError(
                    "子が動いているか、停止を確認できる結果を残さずに残っています。子の停止を確認してから終えてください"
                )
        self.run.ended_reason = reason
        self.run.event(
            "run_ended",
            reason=reason,
            issue=self.run.queue[self.run.index],
            phase=self.run.phase,
            attempt=attempt.id if attempt else None,
            by=caller.model_dump(),
        )
        self.save()

    def named_agent(self, attempt: Attempt) -> dict[str, Any] | None:
        """名前で引いた子。不在はHerdrの構造化エラーのcodeの完全一致だけで判定する。接続失敗・別のcodeは不在にしない。"""
        try:
            return self.ext.herdr("agent", "get", attempt.agent_name)["agent"]
        except CommandError as error:
            if herdr_error_code(error.stderr) != "agent_not_found":
                raise
            return None

    def child_stopped(self, attempt: Attempt, caller: Identity) -> bool:
        """子が動いていないか。終了処理とバックグラウンドの停止を報告した結果があれば、居る子がすべてidle・doneであること。
        結果が無いか、結果がどちらかの停止を確認できていなければ、子が居ないこと。

        そうした子はidle・doneでも、バックグラウンドの処理の完了で再開しうるので止まったと数えない。
        """
        agents = [agent for agent in self.child_agents(attempt, caller) if agent is not None]
        result = attempt.result
        if result is None or not result.finalized or not result.background_stopped:
            return not agents
        return all(agent.get("agent_status") in {"idle", "done"} for agent in agents)

    def child_agents(self, attempt: Attempt, caller: Identity) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """子でありうるagent。名前で見つかった子と、子のpaneに保存したterminalのまま残る同じkindのagent。

        不在はHerdrの構造化エラーのcodeの完全一致だけで判定する。接続失敗・別のcodeは不在にしない。
        """
        named = self.named_agent(attempt)
        pane = self.ext.pane(attempt.pane_id) if attempt.pane_id else None
        if (
            pane
            and pane.get("terminal_id") == attempt.pane_terminal_id
            and pane.get("agent") == self.run.policy.agent_kind
            # paneのagentは1つなので、呼び出し元がそのpane・terminalにいれば子ではない。
            and (caller.pane_id, caller.terminal_id) != (attempt.pane_id, attempt.pane_terminal_id)
        ):
            return named, pane
        return named, None

    def marked_pr(self, attempt: Attempt) -> bool:
        """attemptのマーカーを持つPRが（どの状態でも）あるか。"""
        return any(attempt.marker in (p.get("body") or "") for p in self.ext.pages("pulls?state=all&per_page=100"))

    def require_absent_child(self, attempt: Attempt) -> None:
        """成功した子の消失から進めるときは、不在を毎回確かめる。idle・doneや呼び出し元も例外にしない。"""
        if self.named_agent(attempt) is not None:
            raise QueueError("子のagentがまだ存在します。消失後の続行はできません")
        pane = self.ext.pane(attempt.pane_id) if attempt.pane_id else None
        if pane is not None and (
            pane.get("terminal_id") != attempt.pane_terminal_id or pane.get("agent") == self.run.policy.agent_kind
        ):
            raise QueueError("子のpaneのterminalが変わったか、同じkindのagentが居ます。続行できません")

    def resume_absent_success(self, attempt: Attempt, caller: Identity, reason: str) -> None:
        """closeの意図の前に消えた成功の子を、通常と同じ完了照合を通したときだけ続行する。"""
        self.require_absent_child(attempt)
        previous = self.run.stopped_reason
        if previous and not previous.startswith(("監視上限", "子がblocked")):
            raise QueueError("停止原因が未解消です。子の結果と判断待ちを確認してください")
        old_reason, old_paused = attempt.absent_child_reason, self.run.paused
        accepted = False
        try:
            attempt.absent_child_reason = reason
            self.run.stopped_reason = None
            self.run.paused = False
            status, action, detail = self.observe()
            if (status, action) not in {("ok", "advance"), ("ok", "update_main")}:
                raise QueueError(detail, next_action=action)
            accepted = True
        finally:
            # CLIは拒否した操作も保存する。失敗した照合の仮の許可・停止解除を残さない。
            if not accepted:
                attempt.absent_child_reason = old_reason
                self.run.stopped_reason = previous
                self.run.paused = old_paused
        if caller != self.run.owner:
            self.run.event("owner_transferred", previous=self.run.owner.model_dump(), current=caller.model_dump())
            self.run.owner = caller
        if previous:
            self.run.event("stop_resolved", previous=previous, reason=reason)
        self.run.stop_after = None
        self.run.event("child_absence_confirmed", attempt=attempt.id, reason=reason, by=caller.model_dump())
        self.run.event("continue")
        self.save()

    def resume(self, *, proceed: bool, reason: str | None = None) -> None:
        caller = self.ext.caller()
        if caller != self.run.owner:
            self.check_takeover(caller)
        attempt = self.run.current()
        if (
            proceed
            and reason
            and reason.strip()
            and attempt
            and attempt.acknowledged
            and not attempt.closing
            and attempt.result
            and attempt.result.outcome == "success"
            and attempt.result.finalized
            and attempt.result.background_stopped
        ):
            pane = self.ext.pane(attempt.pane_id) if attempt.pane_id else None
            if pane is None or pane.get("agent") != self.run.policy.agent_kind or not pane.get("agent_session"):
                self.resume_absent_success(attempt, caller, reason)
                return
        if caller != self.run.owner:
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
                        # 片付けの終了のupdate_mainも子の完了を照合できた結果。更新は次の操作として残る。
                        if (status, action) not in {("ok", "advance"), ("ok", "update_main")}:
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
                elif (
                    attempt
                    and attempt.launch == "pane_created"
                    and attempt.pane_id
                    and previous.startswith(start_refused(attempt.pane_id))
                ):
                    # startは実行されていない。利用者がpaneを調べた後、同じpaneから起動し直す。
                    self.run.stopped_reason = None
                else:
                    raise QueueError("停止原因が未解消です。子の結果と判断待ちを確認してください")
                self.run.event("stop_resolved", previous=previous, reason=reason)
            self.run.paused = False
            self.run.stop_after = None
            self.run.event("continue")
        self.save()

    def preflight(self, *, new: bool) -> None:
        """実装の工程の起動の前の検査。newは新しいattemptを作るとき（工程の境界）に真。"""
        issue = self.ext.issue(self.run.queue[self.run.index])
        if issue["state"] != "OPEN" or self.ext.is_board_child(issue):
            raise QueueError("Issueがclosedまたはボードの子です")
        dependencies = self.ext.pages(f"issues/{issue['number']}/dependencies/blocked_by")
        if any(d["state"].lower() == "open" for d in dependencies):
            raise QueueError("先行Issueが未完了です")
        inventory = self.resource_barrier("implement_launch")
        state = main_state(inventory)
        if state == "branch":
            raise QueueError(MAIN_ON_BRANCH)
        # 保存した起動予定の続きは境界ではない。更新すると保存した使用版と合わず続けられないので、版の照合に任せる。
        if state == "outdated" and new:
            raise QueueError(outdated_message(inventory), next_action="update_main")

    def resource_barrier(self, check: Check, inventory: dict[str, Any] | None = None) -> dict[str, Any]:
        """claim・open PR・worktreeを照合する。worktreeは次工程を妨げるものだけで止め、他は無視して記録する。"""
        if inventory is None:
            inventory = self.ext.inventory()
            self.run.observation = inventory
        if inventory["claims"] or inventory["local_claims"]:
            raise QueueError("未解放のclaimがあります")
        # 確認時のHEADのまま残るopen PRだけを保持として通す。
        retained = {(d.pr, d.pr_head) for d in self.run.decisions if d.category == "retain" and d.pr is not None}
        unconfirmed = [p["number"] for p in inventory["open_prs"] if (p["number"], p["head_sha"]) not in retained]
        if unconfirmed:
            raise QueueError(
                f"未確認または確認後に更新されたopen PRがあります。処理対象と競合を確認してください: {unconfirmed}"
            )
        issue = self.run.queue[self.run.index]
        known = {str(self.ext.parent)}
        known.update(d.path for d in self.run.decisions if d.category == "retain" and d.path)
        blocking: dict[str, str] = {}
        handed: set[str] = set()
        for attempt in self.run.attempts:
            if attempt.retry_reason is not None:
                # retryで同じ工程の次の子へ引き継いだ資源（子の結果に残したものと、司令塔が照合したもの）。
                # 引き継いだ子が結果を残すまで通し（Run.handovers）、その後はその子が結果に残した資源として扱う。
                if any(a.id == attempt.id for a in self.run.handovers(attempt.issue, attempt.phase)):
                    handed.update(kept.path for kept in (attempt.result.retained if attempt.result else []))
                    handed.update(attempt.handover)
                continue
            for kept in attempt.result.retained if attempt.result else []:
                if kept.blocks_next:
                    blocking.setdefault(kept.path, kept.reason)
                elif attempt.issue == issue:
                    # 子が確認済みにできるのは自分のIssueの照合だけ。別のIssueの子が残したものは、
                    # 別のセッションの作業を書いたものでありうるので、ほかのworktreeと同じく判定する。
                    known.add(kept.path)
        known.update(handed - blocking.keys())
        problems: list[str] = []
        ignored: list[IgnoredWorktree] = []
        for worktree in inventory["worktrees"]:
            path = worktree["worktree"]
            # 利用者のretain、処理中のIssueの子が保持して続けられるとした資源、retryで引き継いだ資源は通す。
            if path in known:
                continue
            branch = worktree["branch"].removeprefix("refs/heads/") if "branch" in worktree else None
            if path in blocking:
                problems.append(f"{path}: 子が次工程を妨げる資源として残しました（{blocking[path]}）")
            elif mentions_issue(path, issue) or (branch is not None and mentions_issue(branch, issue)):
                problems.append(f"{path}: Issue #{issue} のworktreeまたはbranch（{branch or 'detached'}）です")
            else:
                # 別のセッションの作業でありうるので、中身を検査せず（indexを書き換えうるgit statusを使わず）無視する。
                basis = f"Issue #{issue} のbranch・パスに当たらず、子が次工程を妨げる資源として残したものでもない"
                ignored.append(IgnoredWorktree(path=path, head=worktree.get("HEAD"), branch=branch, basis=basis))
        if problems:
            raise QueueError(
                "次工程を妨げるworktreeがあります。処理対象と競合を確認してください: " + "; ".join(problems)
            )
        self.record_ignored(check, issue, ignored)
        return inventory

    def record_ignored(self, check: Check, issue: int, ignored: list[IgnoredWorktree]) -> None:
        """無視したworktreeをrunのイベントに残す。直前の記録と同じなら重ねない。"""
        if not ignored:
            return
        values: dict[str, Any] = {"check": check, "issue": issue, "worktrees": [w.model_dump() for w in ignored]}
        last = next((e for e in reversed(self.run.events) if e["action"] == "worktrees_ignored"), None)
        if last is None or any(last.get(key) != value for key, value in values.items()):
            self.run.event("worktrees_ignored", **values)

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
        if self.run.ended_reason is not None:
            return "ended", "none", ended_message(self.run.ended_reason)
        inventory = self.ext.inventory()
        self.run.observation = {"at": now(), **inventory}
        attempt = self.run.current()
        if attempt is None:
            if barrier := self.progress_barrier():
                return barrier
            # 工程の境界。実装の子は親main checkoutのスキル・helperを使うので、起動の前に最新にさせる。
            if self.run.phase == "implement":
                state = main_state(inventory)
                if state == "branch":
                    return "blocked", "inspect", MAIN_ON_BRANCH
                if state == "outdated":
                    return "ok", "update_main", outdated_message(inventory)
            return "ok", "launch", "次の工程を起動できます"
        if attempt.launch in {"prepared", "pane_created"} and self.ext.version() != attempt.version:
            # 子を起動する前のattempt。版の照合で止めず、閉じて起動し直す操作を案内する。
            return self.progress_barrier() or ("blocked", "inspect", LAUNCH_PLAN_OUTDATED)
        if attempt.launch == "prepared":
            return self.progress_barrier() or ("ok", "launch", "保存した起動予定の未実行部分を進められます")
        if attempt.launch == "splitting" or not attempt.pane_id:
            return "blocked", "inspect", SPLIT_UNKNOWN
        # 片付けの子は作業の中で親main checkoutを更新する。子の作業中は版の対象のファイルを読まず、
        # 置き換え中のファイルの読み取りの失敗で照合を止めない。
        if attempt.phase != "cleanup" and self.ext.version() != attempt.version:
            raise QueueError("工程の途中でスキル・helper・mainの版が変わりました。境界で照合してください")
        recovered = attempt.absent_child_reason is not None
        if recovered:
            self.require_absent_child(attempt)
        # 復旧したattemptのpaneは他者が使っている可能性がある。子の不在と工程の完了を照合し、paneは触らない。
        pane = None if recovered else self.ext.pane(attempt.pane_id)
        unreported = attempt.acknowledged and attempt.result is None
        if pane is None and not attempt.closing and not recovered:
            if unreported:
                return "blocked", "inspect", UNREPORTED_PANE_MISSING
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
        if pane and attempt.identity and not pane.get("agent_session"):
            # 子のagentが終了し、paneにはshellだけが残る（クラッシュ・/exit）。閉じる操作が子の不在を照合する。
            if unreported:
                return "blocked", "inspect", UNREPORTED_AGENT_EXITED
            return "blocked", "inspect", "子のpaneにagentがいません。子の終了を調べてください"
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
            if pane is not None and pane.get("agent_status") in {"idle", "done"}:
                # 止まった子とバックグラウンドの処理の完了を待つ子を区別できないので、待機と分けて返し待機を続ける。
                return "stalled", "wait", UNREPORTED_TURN_ENDED
            return "waiting", "wait", "明示的な終了結果を待っています"
        result = attempt.result
        if result.outcome != "success" or not result.finalized or not result.background_stopped:
            self.run.stopped_reason = "子の依頼が正常終了していません: " + result.summary
            return "blocked", "inspect", self.run.stopped_reason
        if pane is None:
            if not attempt.closing and not recovered:
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
            self.resource_barrier("cleanup_finish", inventory)
            if main_state(inventory) == "branch":
                raise QueueError("片付け後の" + MAIN_ON_BRANCH)
        if any(d.category == "blocking" and d.resolution is None for d in self.run.decisions):
            return "blocked", "decision", "次工程を妨げる判断待ちがあります"
        if any(r.blocks_next for r in result.retained):
            return "blocked", "inspect", "次工程を妨げる資源があります"
        if self.run.stopped_reason:
            return "blocked", "inspect", self.run.stopped_reason
        # 片付けの子の終了を確かめた後なので、子の作業と重ならない。片付けの間にorigin/mainが進んだ場合など。
        if attempt.phase == "cleanup" and main_state(inventory) == "outdated":
            return "ok", "update_main", outdated_message(inventory)
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
            raise QueueError(reason, next_action=next_action)
        attempt = self.run.current()
        assert attempt is not None and attempt.pane_id
        attempt.closing = True
        self.save()
        if attempt.absent_child_reason is None and self.ext.pane(attempt.pane_id) is not None:
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
