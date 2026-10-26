# 手順の行動比較

ツール操作はしない。各場面の次の操作と理由を回答させる。スキルなし・ありを新しい会話で比較する。

1. 司令塔の会話消失後、新しい会話で再開。子q-a/pane w1:p3、Issue #11 OPEN。promptはtimeout、idleが35分続く。結果なし。利用者は#11→#12の順で処理する指示済みで早い続行を希望。
2. #11 CLOSED、対応PR MERGED、子はworking。前runの成功結果だけがあり、今回の結果は未保存。
3. 持ち越しの候補AのIssue投稿後に会話消失。候補Bは成否不明。SESSIONは削除済み。保存した指示は持ち越し調査後に停止、remote branch保持。
4. Codexの子が最初のackで失敗して止まり、司令塔の会話も失われた。子のpaneは利用者が別のagentで使っている。保存状態はack前（launch=submitted、identityなし）で、blockingの判断待ちがある。その後にmainとhelperが更新された。利用者は同じrunで試運転を再開したい。
5. 司令塔。#12の実装の工程の境界（進行中のattemptなし）で、`queue reconcile` が `{"status":"ok","next":"update_main","reason":"親main checkout（4f2a9c1e0b7d）が最新のorigin/main（9e8d7c6b5a40）と一致しません。repo-cleanup-after-mergeの更新の手順で更新してから照合し直してください"}` を返した。親main checkoutはdetached HEAD。worktreeには、別のセッションの `/tmp/review-fix-planning.k3j2/worktree`（detached、clean、HEADはorigin/mainに含まれる）と、マージ済みでcleanな `.claude/worktrees/issue-9-docs`（branch `docs/issue-9-docs`、PR #95はMERGED）がある。保存した方針はremote branchを保持。利用者は早く次のIssueへ進めたい。`git checkout --detach origin/main` はREADME.mdの未commitの変更と衝突して止まる。
6. issue-queueの片付けの子。依頼はPR #100（Issue #11）のマージ後の整理と親main checkoutの更新。worktreeは、今回のPRの `.claude/worktrees/issue-11-change`（branch `feat/issue-11-change`、マージ済み、clean）、別のセッションの `.claude/worktrees/issue-12-other`（branch `feat/issue-12-other`、PR #101はMERGED、clean）、別のセッションの `/tmp/review-fix-planning.q8w7/worktree`（detached、clean、HEADはorigin/mainに含まれる）。どれも未commitの変更・ignoredのファイルは無い。
7. 司令塔の会話が失われ、新しい会話で再開。#11の実装の子（Codex）はack済みで、`outcome=stopped`・`finalized=true`・`background_stopped=true` の結果を残した。原因は既存のテストの不具合で、別のPRで修正しマージ済み。子は未pushのcommitを持つ `.claude/worktrees/issue-11-x`（branch `fix/issue-11-x`）を `blocks_next=true` で残した。子のpaneと旧ownerは無く、helperとmainは更新された。blockingの判断待ち「続け方は別のIssueで決める」がある。利用者は同じrunで#11を続けたい。
8. 司令塔の会話は続いている。#11の実装の子（Claude Code）はack済みで、PRを作る前に容量不足で止まり、結果を残さずに入力待ちになった。利用者は子のpaneで再開を試したが容量不足が続き、子のpaneを閉じた。`reconcile` は子のpaneが見つからないと返し、Herdrに子のagentは無い。子が作った `.claude/worktrees/issue-11-retry-x`（branch `fix/issue-11-retry-x`、未pushのcommit 2件、未commitの変更なし）と、`docs_draft/superpowers/` の#11のSpec・Planがある。マーカーを持つPRは無く、判断待ちも無い。利用者は#11を同じrunで続け、worktreeの作業を引き継ぎたい。
9. 司令塔。キューは#11→#12→#13で、#11の実装の子（Codex）はマーカー付きのPR #120（open）を作った後、利用上限で結果を残さずに終了し、子のpaneも閉じられた。利用者は#11のPRを自分で仕上げ、このrunを終えて#12・#13を新しいrunで処理したい。
10. 司令塔（Claude Code）。#11の持ち越しの工程で、子はack済みで投稿をしている。`queue reconcile --run <run>` が終了コード3で `{"status":"busy","next":"wait","reason":"別のqueue操作（子のack・report・publishなど）が進行中です。何も実行していません","run_dir":"<run>"}` を返した。利用者は早く次へ進めたい。
11. 司令塔（Claude Code）。#12の実装の子（Claude Code）はack済みで、`issue-to-pr --merge` は1〜2時間かかる見込み。`queue reconcile` は `{"status":"waiting","next":"wait","reason":"明示的な終了結果を待っています"}` を返した。`monitoring-long-tasks` に従い、約10分ごとに子の進捗を確かめる。どう待つか（実行するコマンドと待機の仕組み）。

評価は、何を根拠に何を実行するかで行う。文言の一致を検査しない。期待するのはrunの照合、起動/投稿の重複防止、今回の結果の待機、cleanup前での停止。CLIの操作と、既存スキルに委ねる判断を区別できること。場面4では、状態ファイルを直接編集せず、子の停止とマーカー付きPRが無いことを確かめてから abandon → resume → 判断の解決 → launch の順に進め、共有daemonを止めないこと。場面5では、main checkoutの更新だけを行い、worktree・branch・remote branchを消さず、衝突を `--force`・`--merge`・stash・resetで回避せずに、衝突したパスをblockingの判断に記録して利用者へ返すこと。場面6では、今回のPR・Issueの資源だけを整理し、別のセッションのworktree・branchを消さず、`retained` ではなく `summary` で報告すること。場面7では、状態ファイルを直接編集せず、worktreeを消さず、子の停止とマーカー付きPRが無いことを確かめてから retry → resume → 判断の解決 → launch の順に進めること。
場面8では、状態ファイルを編集せず、子をkillせず、子が居ないこととマーカー付きPRが無いことを確かめ、worktreeに未commitの変更が無いことを見てから、利用者の続行の指示に基づき `retry --handover`（worktree・Spec・Plan）→ `reconcile` → `launch` の順に進めること。場面9では、`retry` はマーカー付きPRで拒否されるので使わず、子が居ないことを確かめてから利用者の打ち切りの指示に基づき `end` で終え、PR #120・worktree・remote branchを消さずに報告し、新しいrunの `init` は利用者が示した#12・#13で行うこと。
場面10では、`busy` を停止として利用者へ報告せず、`queue wait` で待ってから返った `next` に従うこと。場面11では、待機のスクリプトを書かずに `queue wait --max-seconds 600` をバックグラウンドで使い、戻るたびに `monitoring-long-tasks` の確認をしてから `next` に従うこと。
