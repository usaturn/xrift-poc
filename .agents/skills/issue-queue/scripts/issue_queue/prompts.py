"""既存スキルへの依頼と外側への報告方法。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .external import TOOL_ROOT
from .models import Attempt, Run


def handover(run: Run, attempt: Attempt) -> str:
    """同じIssue・工程でretryで閉じ、引き継ぎが続くattemptの結果・残した資源・司令塔が照合した資源を新しい子へ渡す文。"""
    lines: list[str] = []
    for previous in run.handovers(attempt.issue, attempt.phase):
        if previous.id == attempt.id:
            continue
        result = previous.result
        ended = "結果を残さずに" if result is None else f" {result.outcome} で"
        lines += [
            f"前のattempt {previous.id} は{ended}終わり、司令塔が `queue retry` でこのattemptに引き継いだ。",
            f"引き継ぎの理由: {previous.retry_reason}",
        ]
        if result is not None:
            retained = json.dumps([kept.model_dump() for kept in result.retained], ensure_ascii=False)
            lines += [f"前の結果の要約: {result.summary}", f"前のattemptが残した資源（引き継ぐ）: {retained}"]
        if previous.handover:
            lines.append(f"司令塔が照合して引き継いだ資源: {json.dumps(previous.handover, ensure_ascii=False)}")
    if not lines:
        return ""
    if attempt.phase == "implement":
        lines.append(
            "issue-to-prの「issue-queueの引き継ぎ」に従い、引き継いだworktree・branchで続ける。"
            "前のattemptのマーカーは使わない。"
        )
    return "\n".join(lines) + "\n"


def child_prompt(run: Run, attempt: Attempt, run_dir: Path) -> str:
    cli = shlex.join(
        [
            "uv",
            "run",
            "--project",
            str(Path(run.parent) / "tools/devenv"),
            "--locked",
            "python",
            str(TOOL_ROOT / ".agents/skills/issue-queue/scripts/queue.py"),
        ]
    )
    arguments = shlex.join(["--run", str(run_dir), "--attempt", attempt.id])
    pr = next((a.pr_url for a in reversed(run.attempts) if a.issue == attempt.issue and a.phase == "implement"), None)
    command = {
        "implement": f"issue-to-pr https://github.com/{run.repository}/issues/{attempt.issue} --merge",
        "followup": f"review-carry-over-followup {pr}",
        "cleanup": f"repo-cleanup-after-merge: {pr} のマージ後の整理と親main checkoutの更新",
    }[attempt.phase]
    return f"""issue-queueの子です。依頼は {command}。当該スキルを読んで実行してください。
開始前に `{cli} ack {arguments}` を実行し、失敗したら停止してください。
run={run.id}, issue={attempt.issue}, phase={attempt.phase}, attempt={attempt.id}
親main checkout={run.parent}。SDD/Spec/Plan/ログの保存先はAGENTS.mdに従いworktreeの外。
SDD workspaceは親main checkoutのtmp/agentsに置き、worktree内に.superpowersを作らない。
利用者の方針(JSON): {run.policy.model_dump_json()}
{handover(run, attempt)}実装ではPR本文に次のマーカーを入れ、マージ前に保持してください: {attempt.marker}
持ち越しの投稿は既存スキルのcheck_body・write targetの検査を先に行い、ghの書き込みの代わりに
`{cli} publish {arguments} --input <投稿JSON>` を使う。投稿JSONは
{{"key":"候補ごとの一意名","kind":"issue または comment","target":nullまたは同repoのIssue/PR番号,
"title":nullまたはIssueタイトル,"body":"検査済みの本文"}}。
同じkeyの再実行は照合だけを行う。unknownを新しいkeyで再送してはいけない。
外部投稿の権限は既存スキルの範囲のまま。ユーザーの承認/質問に代わりに答えない。
cleanupは既存スキルのゲートを全て維持し、remote branch・reviews・tmp/agentsを保持する。
cleanupで整理するのは今回のPRとIssueのために作られたbranch・worktree・残骸（pr-reviewの残骸とrefs/pr-review/<PR番号>を含む）だけ。
それ以外のworktree・branch・残骸は別のセッションが使用している可能性があるので、マージ済み・cleanでも削除せずに報告する。
このrunディレクトリは再開に必要なので削除しない。未確認の資源を一括削除しない。
全工程終了後、バックグラウンドの停止と終了処理を確認し、次のJSONをこのrunの下の一意ファイルへ書き、
`{cli} report {arguments} --input <結果JSON>` を最後の操作として実行して最終報告してください。
{
        json.dumps(
            {
                "outcome": "success | stopped | failed",
                "summary": "結果と所要時間",
                "finalized": True,
                "background_stopped": True,
                "retained": [{"path": "完全パス", "reason": "保持理由", "blocks_next": False}],
                "urls": ["投稿して読み戻したURL"],
                "issues": ["追加/集約したIssueのURL"],
                "loop_run": "実装だけは今回のloop/run.jsonの完全パス。他はnull",
            },
            ensure_ascii=False,
        )
    }
空のurls/issues/retainedも正常な値です。未完了の確認をtrueやsuccessにしない。
retainedは、今回のIssueのためにqueueの工程（実装・持ち越し・片付け）が作って残した資源（実装worktreeなど）の一覧で、完全パス・保持理由・次工程を妨げるかを必ず記録する。
削除せずに報告するだけの別のセッションの資源（worktree・branchなど）は、summaryに完全パスと理由を書く。
実装はloop finish後に結果を残す。持ち越しは全投稿の読み戻しとSESSIONの後片付け後に残す。
秘密公開、CI失敗、利用上限、承認待ち、cycle_limit/stoppedでは停止理由を報告し再試行しない。
"""
