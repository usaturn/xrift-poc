---
name: issue-queue
argument-hint: "init <方針JSON> | resume <runディレクトリ> | status <runディレクトリ>"
description: "複数の独立した Issue を実装・マージ、持ち越し調査、マージ後の整理の順で直列に処理するとき、または会話の消失後に保存したキューを再開するときに使う。単一 Issue の着手、PR のレビュー、ボードの子の並行作業には使わない。"
---

# Issue キュー

会話を復旧しても、新しい会話を作っても、保存した run から再開する。司令塔が不在の間は進行が止まってよい。状態を書き換えるのは `scripts/queue.py` だけである。

## 開始

`repository-ownership`、`herdr`、`monitoring-long-tasks` を読む。初期対応は親 `usaturn/xrift-poc` の独立した Issue に限る。制作物・ボードの子は対応する既存スキルへ戻す。Herdr の外では起動しない。

利用者の既存の指示から、対象と順序、remote branch の保持、持ち越しで増えた Issue を提案だけにするか追加するか、子の kind と起動引数、1件で試運転を止めるかを確認する。不足する選択だけを尋ねる。起動引数へ権限を緩めるオプションを補わない。初期版は remote branch を保持し、削除の判断は終了時に利用者へ返す。

```bash
PARENT="${XRIFT_POC_ROOT:?}"
SCRIPT="$PARENT/.agents/skills/issue-queue/scripts/queue.py"
queue() { uv run --project "$PARENT/tools/devenv" --locked python "$SCRIPT" "$@"; }
queue init --input <方針JSONの絶対パス>
```

方針JSONと結果の形式は [references/protocol.md](references/protocol.md) を読む。`init` が返す `run_dir` を報告し、以降はその絶対パスを `--run` に渡す。記録は親main checkoutの `tmp/agents/issue-queue.*/state.json` にあり、worktreeの削除後も残る。未完了runがあれば新規作成せず再開する。

## 進行

1. `queue reconcile --run <run>` で現在の GitHub・Herdr・worktree と照合する。
2. 返された `next` に従う。

| next | 操作 |
|---|---|
| `launch` | `queue launch --run <run>`。起動予定を保存して子を起動する。中断前に保存した `prepared` / `pane_created` は、所有を確認して未実行の操作から続ける |
| `submit` | `queue submit --run <run>`。起動を照合し、未送信と確認できた依頼だけを送る |
| `wait` | `queue wait --run <run>`。最長30秒のイベント待機の後で現在状態を再取得する。idleで即戻る場合はランタイムの待機を挟み、外部状態は60秒以内を目安に再確認する |
| `advance` | `queue advance --run <run>`。終了を再確認し、所有paneを閉じて次の工程へ移る |
| `update_main` | 工程の境界で親main checkoutが最新のorigin/mainより古い。下の「main checkoutの更新」に従って更新し、`queue reconcile` で照合し直す。`launch`・`advance` の拒否でも返る |
| `resume` | 保存した停止位置を利用者へ報告する。続行指示があるときに `queue resume --run <run> --continue` |
| `decision` / `inspect` | 理由・子・残存資源を報告する。成否不明の操作を再送しない |
| `none` | 完了を報告する。runの記録は残す |

`issue-to-pr --merge → review-carry-over-followup → repo-cleanup-after-merge` の3工程を直列に行う。スキルの内部記録とロックは、そのスキルのhelperに管理させる。外側の司令塔は子の承認・質問に答えず、マージ・投稿・削除の条件を緩めない。片付けの子に頼む整理は、今回のPRとIssueのために作られた資源だけを対象にし、別のセッションの作業を消さない。

マージは GitHub、依頼の終了は今回の子の明示結果で確認する。35分のidleでも、マージや完了の証明にならない。マージ後のworking、未確認の結果、blockedではpaneを閉じない。監視上限、CI失敗、利用上限、子のstoppedは外側の停止であり、子をkillする許可ではない。停止・資源の回収は `monitoring-long-tasks` と担当スキルに従う。

### main checkoutの更新

helperが `update_main` を返したら、司令塔が親main checkoutで `repo-cleanup-after-merge` の「作業ツリーを origin/main へ更新する」手順（HEADと作業ツリーの状態の記録、`git fetch` の後の `git checkout --detach origin/main`）だけを行い、`queue reconcile` で照合し直す。worktree・branch・残骸・`refs/pr-review/*` の削除とremote branchの操作はしない。queueの資源は片付けの子が整理済みで、残っているのは別のセッションが使っている可能性のある作業である。

checkoutの衝突、fetch・ghの失敗で更新できなければ、`--force`・`--merge`・stash・resetで回避しない。衝突したパスと理由を `queue decision --category blocking` に記録して利用者へ返す。helperはmain checkoutが最新になるまで起動・advanceを拒否する。`update_main` が返るのは、実装の工程の起動の前と、片付けの子の終了を確かめた後だけである。実装・持ち越しの途中で司令塔がmain checkoutを更新すると、使用版の照合で止まる。

## 会話の復旧と停止

```bash
queue status --run <run>
queue resume --run <run>
queue abandon --run <run> --reason <理由>
queue retry --run <run> --reason <理由>
queue stop --run <run> --after followup
```

`resume` は旧ownerの生存と子・GitHub・worktreeを照合する。呼び出し元の会話自身は旧ownerと数えないので、元の会話を別のpaneや新しいterminalで復旧しても引き継げる。呼び出し元以外に旧ownerと同じnative sessionのagentが残っていれば横取りしない。動いている子は監視に戻し、新しい子を作らない。起動・送信のtimeoutは未実行の証拠ではない。pane作成の応答が失われて所有を証明できない場合は手動調査で止める。

子がackの前に終わった場合（子のpaneが閉じられた、別のagentに使われた、起動直後に止まった）は、子が動いていないことと、attemptのマーカーを持つPRが無いことを確かめてから `abandon` でattemptを閉じ、理由を残す。helperも同じ条件を照合し、使用版が変わった後でも閉じられる。この会話や別のkindのagentが子のpaneを使っていても閉じられる。子のpaneの保存したterminalに、この会話以外の同じkindのagentがいれば、名前の消えた子と区別できないので閉じない。paneは閉じず、ownerと判断待ちは変えない。旧ownerが不在なら続けて `resume` で引き継ぎ、判断待ちを解決してから `reconcile` → `launch` で同じ工程を新しいattemptとして起動する。ack済みの子は打ち切れない。失敗した子を自動で作り直す機能ではない。

ack済みの子が `stopped`・`failed` の結果を残して止まり、利用者の続行指示があって原因を取り除いた後は、子の停止・残した資源・PRの有無を確かめてから `retry` でattemptを閉じ、理由を残す。helperは、呼び出し元がownerか旧ownerが不在であること、結果の終了処理とバックグラウンドの停止が確認済みであること、投稿の記録が無いこと、子と子のpaneに残る同じkindのagentが不在かidle・doneであること、attemptのマーカーを持つPRが無いことを照合する。使用版は照合しないので、helperやmainが更新された後でも閉じられる。paneは閉じず、ownerと判断待ちは変えない。続けて `resume`（旧ownerが不在なら引き継ぐ）→ 判断待ちの解決 → `reconcile` → `launch` で同じ工程を新しいattemptとして起動する。新しい子への依頼文には前の結果と残した資源が入り（その子がackの前に終わり、`abandon` で閉じて起動し直した子にも入る）、実装の子はissue-to-prの「issue-queueの引き継ぎ」で前のworktreeを引き継ぐ。前のattemptが残した資源は、作業を引き継いだattempt（ackの前に `abandon` で閉じた結果の無いattemptを除く、同じ工程の次のattempt）が結果を残すまで起動の前の検査で通す。止まった子の作業を消すかは利用者が決める。

`--after followup` は持ち越しの調査後、片付けの前で止める。未実施のcleanupを保存し、再開はそこから始める。片付け後に止める場合だけ `--after cleanup` を使う。試運転は最初のcleanup後に止まる。利用者が子のblockedを解消した後は、事実を `resume --continue --reason <理由>` に記録できる。失敗した子を自動再起動する機能ではない。

監視上限後に子が正常終了した場合も、続行指示と確認理由を同じ `resume --continue --reason` に渡す。helperが今回の終了結果・子の停止・外部状態を再照合できた時だけ解除する。実行中の子の監視上限をこの操作で延長することはできない。

## 判断と報告

追加Issueは終了結果を読んで優先順位を決め、`queue queue --run <run> --issues <全順序> --reason <理由>` で未着手部分だけを変更する。初期方針が `enqueue` のときだけ、持ち越し結果にある同repositoryのIssueを追加できる。`propose` のときは候補を報告する。

判断待ちは `queue decision --run <run> --id <名前> --category blocking|retain|final --reason <理由>` で保存する。`blocking` は次へ進めない。`retain` は資源を保持して続行できる判断、`final` は最後にまとめて確認する判断。解決した事実は同じIDへ `--resolve` を付けて残す。既存の許可を復旧のたびに尋ね直さず、対象や操作の範囲を広げない。

helperは、実装・片付けの工程の起動の前と片付けの工程の終了の照合で、確認していないworktreeを1つずつ判定する。処理中のIssueのworktree・branch（パスかbranch名に `issue-<番号>` が番号の境界で現れる）と、子が次工程を妨げる資源として残したworktreeだけで止め、パスと理由を返す。それ以外（別のIssueやセッションの作業、レビューの残骸、未commitの変更を持つworktreeを含む）は無視して進み、パス・HEAD・branch・判定の根拠をrunのイベント `worktrees_ignored` に残す。止まったworktreeは、担当・停止・マージと競合の有無を既存スキルで確かめ、保持して続けられる場合だけ `--category retain --path <絶対パス>` と確認理由を記録する。キューと無関係なopen PRは、担当と競合の有無を確かめてから `--category retain --pr <番号>` と確認理由を記録する。helperは確認した時点のPRのHEADを保存し、同じPRが同じHEADのままの間だけ通す。pathとPRは別の判断IDで記録する。未確認のopen PR、確認後にHEADが変わったPR、未解放のclaimがあれば実装・片付けを起動しない。保持の登録と無視の記録は、削除・マージの許可ではない。

各周回ではIssue・PR・loop run・子の識別情報、所要時間、マージ検出時刻、判断待ち、保持資源、使用版を報告する。正常終了・停止・確認できないことを分ける。終了時も `reviews/`・`tmp/agents/`・remote branch を一括削除しない。

## ランタイムごとの違い

本体は `.agents/skills/issue-queue`、Claude Codeは `.claude/skills/issue-queue` の相対リンクから読む。司令塔はどのランタイムでも同じCLIを使い、Herdrのnative sessionで識別する。子は明示した `claude`・`codex`・`pi` のkindとargvで起動する。Codexのように起動直後はnative sessionが無いkindも、一意のagent名・kind・pane・terminalを照合して依頼を1回だけ送り、子の最初の `ack` でsessionを確定する。別モデルや別ランタイムへ暗黙に切り替えない。

helperは利用者のargvを順序どおり渡し、加えるのはCodexの `--no-daemon` だけである。Codexの共有app-server daemonは、daemonを起動したpaneの環境でツールとフックを動かすので、子の `ack` とHerdrへのnative sessionの報告が別のpaneを指す。Codexの子では、共有のapp-serverへ接続する `--remote` を受け付けない。共有daemonは他の会話も使うので止めない。
