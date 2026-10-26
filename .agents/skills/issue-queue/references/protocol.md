# CLIと結果の形式

## 方針JSON

```json
{
  "issues": [11, 12],
  "policy": {
    "agent_kind": "claude",
    "agent_args": [],
    "authorization": "このrepositoryの指定Issueを実装・マージし、持ち越しを調査する",
    "exclusions": ["デプロイ", "他repositoryへの投稿"],
    "remote_branches": "keep",
    "additional_issues": "propose",
    "trial": true,
    "monitor_seconds": 28800
  }
}
```

値は利用者の指示に合わせる。上の番号・kindは例である。`agent_args` はargv配列で、シェルへ展開しない。秘密を含む引数を保存しない。helperは順序を保って子へ渡す。`agent_kind` が `codex` のときは先頭に `--no-daemon` を加え（既にあれば加えない）、`--remote` はinitとlaunchで拒否する。`additional_issues` は `propose` または `enqueue`。remote branchの自動削除は提供しない。監視上限は60〜28800秒。

すべてのコマンドはJSONを返す。既定は要約で、`status`・`next`・`reason`・`run_dir`・`issue`（処理中のIssue。完了ならnull）・`phase`・`attempt`（今のattemptの `id`・`launch`・`pane_id`・`agent_name`・`acknowledged`・`outcome`・`pr_url` と、`effects` の `key`・`state`・`url`。無ければnull）を持つ。runの全体は、どのコマンドでも `--full` を付けたときだけ `run` に入る（`state.json` でも読める）。終了コード0はコマンドの処理成功であり、キュー完了とは限らない。`status` が `complete` のときだけ全件完了。`ended` は `end` で終えたrun（`next: none`）。`stalled` はack済みの子が結果を残さずにturnを終えた状態で、止まった子とバックグラウンドの処理を待つ子を区別できないので `next` は `wait` になる。`waiting`・`paused`・`blocked` と `next`・`reason` を読む。`busy` は、別のqueue操作がロックを持っていてコマンドを何も実行しなかった状態で、`next` は `wait`（終了コード3）。入力や観測、順序の拒否は3、argparseの引数エラーは2。失敗時も `run_dir` と保存済みの状態の要約を返す。拒否の `next` は既定で `inspect`。親main checkoutが古いために `launch`・`advance` を拒否したときは `update_main` を返し、`advance` のそれ以外の拒否は照合した結果の `next` を返す。

`status` は保存状態を表示する。実環境の照合には `reconcile` または `resume` を使う。`wait` のHerdrイベントは再確認のきっかけであり、工程の成功を記録するものではない。`wait` は子のpaneのHerdrのイベント（最長30秒）をロックの外で待ってから、ロックの中で照合する。`--max-seconds N`（30〜3600）を付けると、これを約30秒おきに繰り返し、`next` が `wait` 以外になる、`status` か `next` がこの呼び出しの最初の照合から変わる、照合が失敗する（終了コード3）、上限に達する、のどれかで返す。出力の `waited_seconds` は待った秒数。照合のロックが取れなければ1秒おきに、上限（少なくとも30秒）までやり直し、取れなければ `busy` を返す。`--max-seconds` を省くと1回だけ照合する。

## 子への依頼と結果

helperは毎回、親main checkoutをcwdとする新しいHerdr paneを作り、一意のagent名を付ける。`agent start` へ渡す引数は、起動前にattemptの `launch_args` へ保存する。新しいpaneのシェルの準備の前の `agent start` は、Herdrが構造化エラー `agent_pane_busy` で拒否する（startは実行されない）。helperは、agent名の `get` が `agent_not_found` で終わることを確かめてattemptを `pane_created` に戻し、同じpaneの照合（保存したterminalのまま、agentもnative sessionも無い）から1秒おきに、最初のstartから30秒までやり直す。拒否の回数はイベント `start_refused` に残す。30秒を超えたら `pane_created` のまま「子のpane <ID> がシェルの入力待ちになりません」の停止を保存し、`reconcile` も同じ停止を返す。`resume --continue --reason` は、今のattemptが `pane_created` でそのpaneの停止のときだけ解除し、次の `launch` が同じpaneから起動し直す。timeout・JSONでない応答・別のcodeでは、今と同じく `starting`（成否不明）で止まる。最初の操作として `ack --run ... --attempt ...` を依頼する。startの応答とその後のgetにnative sessionが無いagent（Codexなど）は、一意のagent名・kind・pane・terminalが起動時と一致することを照合して依頼を1回だけ送り、最初のackで呼び出し元のnative sessionを確定する。最初のackも、別のpane・terminal・agentからは受け付けない。確定後は呼び出し元のpane・terminal・native sessionが保存値と一致することを検査し、sessionの変更は拒否する。子に渡したスキルとhelperのhash、mainのcommitをattemptに保存する。片付けの子への依頼では、整理の対象を今回のPRとIssueのために作られたbranch・worktree・残骸に限り、それ以外は削除せずに報告させる。

実装のPR本文には `<!-- issue-queue:<run ID>:<attempt ID> -->` を付ける。IssueがCLOSEDでも、対応するマーカーを持つ同repositoryのPRがMERGEDで、closedByPullRequestsReferencesに含まれることが確認できなければ次へ進まない。今回の `pr-review-fix-loop` の `run.json`、ended/no_fix_needed/merged、開始時刻、ロック解放も確認する。

子は終了処理を済ませてから結果を一意ファイルへ書き、`report --run ... --attempt ... --input <ファイル>` で保存する。

```json
{
  "outcome": "success",
  "summary": "実施した作業と所要時間。未実施の確認も記載する",
  "finalized": true,
  "background_stopped": true,
  "retained": [],
  "urls": [],
  "issues": [],
  "loop_run": null
}
```

- `outcome`: success / stopped / failed。監視上限やcycle_limitはsuccessにしない。
- `finalized`: 内部スキルの終了処理が完了したか。`background_stopped`: 追跡する処理の停止を確かめたか。
- `retained`: `{ "path": "完全パス", "reason": "保持理由", "blocks_next": false }` の一覧。今回のIssueのためにqueueの工程が作って残した資源を書き、残した実装worktreeも必ず含める。担当・停止・マージと競合の有無を確認する。削除してよいという意味ではない。削除せずに報告するだけの別のセッションの資源は含めず、`summary` に完全パスと理由を書く。helperがこの一覧を確認済みとして通すのは、その子と同じIssueの照合だけである（「確認していないworktree」）。
- `urls`: 持ち越しで投稿し、読み戻しを確認したURLのすべて。
- `issues`: 作成・集約した同repositoryのIssue URL。追加なしの空配列も正常。
- `loop_run`: 実装は今回のloop/run.jsonの完全パスが必須。他工程はnull。

終了結果は上書きしない。同じ結果の再送は同じ記録として受け付ける。結果を保存しただけではpaneを閉じず、司令塔がidle/doneと外部状態を再確認してから `advance` する。followupのSESSION削除後も外側の結果は残る。

ack済みで結果の無い子は、paneがidle・doneなら `stalled`、子のagentが終了した（paneにagentがいない）か子のpaneが見つからなければ `blocked`・`inspect` になる。どれも完了の証明ではない。閉じるのは `retry`、終えるのは `end` で、どちらも子が居ないことを照合する（下の「保存・排他・版」）。

## 持ち越しの投稿

`review-carry-over-followup` の対象・権限・本文検査と投稿順序を維持する。スキルの `check_body` とwrite targetの照合を済ませた本文を、次のJSONで `publish --run ... --attempt ... --input <ファイル>` に渡す。

```json
{
  "key": "candidate-A",
  "kind": "issue",
  "target": null,
  "title": "調査で確かめた不具合",
  "body": "検査済みの本文\n複数行もそのまま保存する"
}
```

コメントはkind=comment、title=null、target=元PRまたはopenの同repositoryの集約先Issue番号。新しいIssue → 元PRの事後調査コメント → 集約先コメントの順序を守る。helperは本文末尾に一意マーカーを付け、intentを保存してからPOSTする。POSTの応答のJSONから番号（Issueは `number`、コメントは `id`）を読んで `remote_id` に保存し、その1件（`issues/<number>`・`issues/comments/<id>`）を読んでマーカー・本文・Issueのタイトルを照合する。1件を読めなければ2秒おきに3回まで読み直す（GETだけ）。応答を読めない・番号が無いときは止まり、POSTし直さない。stateに保存された本文が送信内容であり、スキルの元ファイルとの比較では末尾マーカーを含める。

同じkeyを再実行したときは読み取りによる照合だけを行う。`remote_id` があればその1件を、無ければ（POSTの応答が失われた）一覧の全ページからマーカーと本文を照合し、投稿済みならそのURLと番号を採用する。一覧は作成の直後に新しい投稿を含まないことがある。待機・`retry`・`advance` の照合も、番号の分かった投稿は1件を読む。見つからなくても再POSTしない。成否不明の投稿があれば次の投稿も拒否する。別keyに変えて同じ投稿を送らない。helperも、記録済みの投稿とkind・target・title・本文が同じ投稿と、記録済みの投稿のマーカーを含む本文（送信内容やその引用）をkind・target・titleによらず、別のkeyでもPOSTの前に拒否する。外部への書き込みが未確認なら正常終了にしない。

`retry` で閉じた持ち越しのattemptの投稿の記録は、次のattemptを作るときに写す（`inherited_from` に投稿したattemptのID）。写した投稿と同じkeyの `publish` は照合だけを行う。持ち越しの結果の `urls` は、このattemptが投稿したURLをすべて含み、照合済みのURL（写した投稿を含む）だけからなること。

## 確認していないworktree

実装の工程の起動・片付けの工程の起動・片付けの工程の終了の照合で、親main checkout、retainで登録したパス、処理中のIssueの子が結果に `blocks_next=false` で残したパス、`retry` で引き継いだ資源（引き継いだattemptが結果を残すまで）以外のworktreeを判定する。別のIssueの子が `blocks_next=false` で残したパスは確認済みにせず、ほかのworktreeと同じく判定する。子が別のセッションの作業を `retained` に書いても、後のIssueの照合は飛ばさない。処理中のIssueの番号Nについて、worktreeのパスか、`refs/heads/` を除いたbranch名に `issue-N` が番号の境界（直前が英数字でなく、直後が数字でない。大文字と小文字を区別しない）で現れるもの、結果に `blocks_next=true` で残されたものがあれば、`次工程を妨げるworktreeがあります` にパスと理由を並べて止まる。それ以外は中身を検査せずに無視し、runのイベントに残す。直前の `worktrees_ignored` と同じ内容なら重ねない。

```json
{
  "action": "worktrees_ignored",
  "check": "implement_launch | cleanup_launch | cleanup_finish",
  "issue": 11,
  "worktrees": [{"path": "完全パス", "head": "SHA", "branch": "feat/issue-110-x（detachedはnull）", "basis": "判定の根拠"}]
}
```

## 保存・排他・版

state.jsonのschema_versionは1。helperがflockの下で一時ファイルをfsyncし、replaceして保存する。未対応schemaは拒否する。排他はコマンド単位のflockと、runに保存する司令塔のnative sessionの両方で行う。子が呼ぶ `ack`・`report`・`publish` は、司令塔の照合などが終わるまでflockを待ってから実行する。司令塔のコマンドは待たずに、何も実行しないで `status: busy`・`next: wait` を返す（`wait` は照合のロックをやり直す）。`wait` はHerdrのイベントの待機の間ロックを持たず、照合のたびにロックの中で `state.json` を読み直すので、待機の間の子の記録を上書きしない。`observation` には照合の時点のmain checkout・worktree・claimと、open PRの `number`・`head_sha`・`head_ref` だけを残す。古さだけでロックを奪わない。司令塔を移すときはHerdrの現在の一覧で、呼び出し元以外に旧sessionのagentが無いことを確かめる。同じsessionの会話を別pane・新しいterminalで復旧した場合も引き継げる。

意図保存と外部の操作は一つのトランザクションにはできない。意図があり結果が無い窓は「不明」として扱う。startの応答消失は一意agent名・kind・pane・terminalで照合できれば未送信の状態へ戻せる。startの拒否を未実行と判断するのは、stderrの構造化エラーの `error.code` が `agent_pane_busy` に完全一致し、agent名の `get` が `agent_not_found` で終わるときだけである（「子への依頼と結果」）。prompt送信の成否不明はackを待つ。splitの応答消失など、所有paneを確定できない場合は手動調査で止め、再実行しない。paneの不在は、Herdrがstderrに返す構造化エラーの `error.code` が `pane_not_found` に完全一致するときだけと判断する。接続失敗・JSONでない応答・別のcodeは不在にしない。closeの意図を保存した後にpaneが無ければ、終了結果と外部状態を確認し直して次の工程へ進む。closeの意図が無いのにpaneが無ければ、再起動せず調査で止める。子のagentだけが終了してpaneにshellが残る場合も同じである。

成功した子の消失後に利用者が続行を指示したときは、`resume --run ... --continue --reason <調査した事実>` で再開する。helperは旧ownerの不在（ownerを移す場合）、ack済みの結果がsuccess/finalized/background_stoppedの全条件を満たすこと、agent名が構造化 `agent_not_found` であることを確認する。paneは構造化 `pane_not_found` か、保存したterminalのままで同じkindのagentが居ないことを求める（呼び出し元やidle/doneも例外にしない）。別のエラーは不在にせず、terminalが違えば止める。その上で通常の終了照合（版、実装のマージ・Issueのclose・loop、持ち越しの投稿の読み戻しと結果、片付けの資源・main checkout）を行い、`ok/advance` または片付けの `ok/update_main` のときだけ続行理由をattemptの `absent_child_reason` とイベント `child_absence_confirmed`（attempt・reason・by）に保存する。schema_versionは1のまま、旧stateでこの欄が無ければnullとして読み、許可があるとは扱わない。

この照合の間だけ仮に設定した許可は、失敗すれば保存前に戻す。監視上限・子blockedの保存された停止は、同じ照合に成功したときだけ解除する。判断待ち・保持資源の停止を迂回しない。理由・続行指示が無ければ既定の停止を維持する。以後も子の不在と外部状態を照合し直し、`advance` はこの経路のpaneを閉じずに工程だけを進める。残ったshellや別kindのagentには触れず、別のattemptに許可を引き継がない。

ackの前に子が消えたattemptは `abandon --run ... --reason <理由>` で閉じる。helperは、呼び出し元がownerか、resumeと同じ照合で旧ownerが不在であること、ackと結果が無いこと、agent名の `get` がstderrの構造化エラー `agent_not_found` で終わること、paneが残っていれば、保存したterminalで呼び出し元以外の同じkindのagentが動いていないこと、attemptのマーカーを持つPR（全状態）が無いことを確かめる。閉じたattemptは `abandon_reason` に理由を残し、そのattemptの子による `stopped_reason` を解除する。paneは閉じず、ownerと判断待ちは変えず、使用版は照合しない。同じ工程は次の `launch` で新しいattemptとして起動する。

子を起動する前のattemptも `abandon` で閉じる。`prepared` は保存した使用版が今と違うときだけ閉じる（同じなら `launch` で続ける）。`splitting` は、agent名の `get` が `agent_not_found` で終わることを確かめて閉じる。splitが作ったかもしれないpaneは閉じない。`reconcile` は、`prepared`・`pane_created` の使用版が変わっていれば版の照合の例外にせず、`blocked`・`inspect` で `abandon` を案内する（`pane_created` は上のack前の条件で閉じる）。

ack済みの子が止まったattemptは `retry --run ... --reason <理由> [--handover <絶対パス> ...]` で閉じる。helperは、呼び出し元がownerか、resumeと同じ照合で旧ownerが不在であること、投稿の記録（`effects`）があればすべて読み戻して照合できること（成否不明・重複・不一致があれば拒否する）、attemptのマーカーを持つPR（全状態）が無いことを確かめる。agent名の `get` で見つかる子と、paneが保存したterminalのまま残る呼び出し元以外の同じkindのagentについて、結果があれば `stopped`・`failed` で `finalized` と `background_stopped` が真であり、見つかったagentの `agent_status` がidle・doneであること、結果が無ければどれも居ないこと（`agent_not_found`、paneに同じkindのagentがいない）を確かめる。結果の無い子はidle・doneでもバックグラウンドの処理の完了で再開しうるので受け付けない。Herdrの別のエラーは不在にしない。成功の結果は `advance` で進める。`--handover` は実装の工程だけで（1つの `--handover` に複数のパスを並べても、繰り返してもよい）、各パスが存在する絶対パスで、親main checkoutでなく、パス（`git worktree list` のworktreeならbranchも）に処理中のIssueの `issue-N` が番号の境界で現れることを確かめ、attemptの `handover` に保存する（worktreeは一覧の形のパス）。資源の作り手は確かめられない。閉じたattemptは `retry_reason` に理由を残し、イベント `attempt_retried`（結果が無ければ `outcome` は `null`）を記録して `stopped_reason` を解除する。paneは閉じず、ownerと判断待ちは変えず、使用版は照合しない。古い子の `ack`・`report`・`publish` は、attemptが今回の工程のものでなくなるので拒否される。

`retry` の後の `launch` は同じIssue・工程を新しいattemptとして起動し、依頼文に、引き継ぎが続くattemptのID・outcome（結果が無ければその旨）・理由・結果の要約・`retained`・`handover` を起動の順に書く。引き継ぎは、`retry` で閉じたattemptのうち、同じIssue・工程で後のattemptが結果を残していないものから続く。ackの前に `abandon` で閉じたattemptと、結果を残さずに `retry` で閉じたattemptは、引き継ぎを終わらせない。引き継ぎが続く間、そのattemptの `retained` と `handover` は、起動の前の検査で次工程を妨げる資源として数えない。ほかのattemptが `blocks_next=true` で残したパスは止める。引き継いだattemptが結果を残した後は、その結果に残された資源として扱う。

続けられないrunは `end --run ... --reason <理由>` で終える。helperは、呼び出し元がownerか、resumeと同じ照合で旧ownerが不在であること、runが完了も終了もしていないこと、現在のattemptの子が動いていないことを確かめる。attemptが無いか `prepared` なら子は居ない。`splitting`・`pane_created` は、agent名の `get` が `agent_not_found` で終わること。それ以外は、`retry` と同じ範囲のagentが、結果が `finalized` と `background_stopped` の両方が真ならidle・doneであること、結果が無いか、どちらかが偽ならどれも居ないこと（停止を確認できない子はidle・doneでも再開しうる）。マーカー付きのPR・投稿・資源・判断待ちは条件にせず、変えない。終えたrunは `ended_reason` に理由を残し、イベント `run_ended`（理由・Issue・工程・attempt・呼び出し元）を記録する。attempt・paneは変えない。終えたrunは `status`・`reconcile`・`wait` だけを受け付け（`status: ended`・`next: none`）、子の `ack`・`report`・`publish` を含むほかのコマンドを拒否する。`init` は、完了したrunと終えたrunを未完了に数えない。

`prepared` はsplit、`pane_created` はstartの意図をまだ保存していない。`launch` は保存したattemptを再利用して、その未実行部分だけを続ける。pane_createdではpaneのterminal IDと、別のagentが起動していないことも確認する。`splitting` / `starting` / `submitting` を未実行に戻さない。起動・送信とも、保存されたblocking判断や停止要求を副作用の前に検査する。

main checkoutを更新するのは、cleanup工程の子と、helperが `update_main` を返したときの司令塔だけである。helperは、親main checkoutのHEAD・ローカルの `origin/main`・remoteの `main` が一致するdetached HEADを最新とし、実装の工程の境界（現在のattemptが無い）と、cleanupの子の成功の結果・終了処理・バックグラウンドの停止・paneのidle/doneを確かめた後の終了の照合でだけ `update_main` を返す。判断待ち・停止・一時停止があればそれを先に返し、branchにいれば止める。保存した起動予定（`prepared`・`pane_created`）の続きは境界ではないので、使用版が一致すれば更新を求めずに続ける。司令塔は更新だけを行い、削除はしない。次の子は更新後の親main checkoutから起動し、使用版を新たに保存する。helperは呼び出しごとに再ロードし、schemaを検査する。実装・持ち越しの途中で使用版が変わった場合は止める。cleanup後に次工程を妨げるworktree・claim・更新の衝突があれば、次のIssueへ進めない。

同じOSユーザーによる状態の直接改ざん、Herdrサーバーの交換、別repositoryのキュー、常駐継続運転、失敗した子の自動修復は対象外。状態や識別情報が不一致なら止める。
