"""既存スキルへの依頼と外側への報告方法。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .external import TOOL_ROOT
from .models import Attempt, Run


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
実装ではPR本文に次のマーカーを入れ、マージ前に保持してください: {attempt.marker}
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
実装worktreeなど残した資源はretainedに完全パス・保持理由・次工程を妨げるかを必ず記録する。
実装はloop finish後に結果を残す。持ち越しは全投稿の読み戻しとSESSIONの後片付け後に残す。
秘密公開、CI失敗、利用上限、承認待ち、cycle_limit/stoppedでは停止理由を報告し再試行しない。
"""
