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

値は利用者の指示に合わせる。上の番号・kindは例である。`agent_args` はargv配列で、シェルへ展開しない。秘密を含む引数を保存しない。`additional_issues` は `propose` または `enqueue`。remote branchの自動削除は提供しない。監視上限は60〜28800秒。

すべてのコマンドはJSONを返す。終了コード0はコマンドの処理成功であり、キュー完了とは限らない。`status` が `complete` のときだけ全件完了。`waiting`・`paused`・`blocked` と `next`・`reason` を読む。入力や観測、順序の拒否は3、argparseの引数エラーは2。失敗時も `run_dir` と保存済みの状態を返す。

`status` は保存状態を表示する。実環境の照合には `reconcile` または `resume` を使う。`wait` のHerdrイベントは再確認のきっかけであり、工程の成功を記録するものではない。

## 子への依頼と結果

helperは毎回、親main checkoutをcwdとする新しいHerdr paneを作り、一意のagent名を付ける。最初の操作として `ack --run ... --attempt ...` を依頼する。呼び出し元のpane・terminal・native sessionが保存値と一致することを検査する。子に渡したスキルとhelperのhash、mainのcommitをattemptに保存する。

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
- `retained`: `{ "path": "完全パス", "reason": "保持理由", "blocks_next": false }` の一覧。残した実装worktreeも必ず含める。担当・停止・マージと競合の有無を確認する。削除してよいという意味ではない。
- `urls`: 持ち越しで投稿し、読み戻しを確認したURLのすべて。
- `issues`: 作成・集約した同repositoryのIssue URL。追加なしの空配列も正常。
- `loop_run`: 実装は今回のloop/run.jsonの完全パスが必須。他工程はnull。

終了結果は上書きしない。同じ結果の再送は同じ記録として受け付ける。結果を保存しただけではpaneを閉じず、司令塔がidle/doneと外部状態を再確認してから `advance` する。followupのSESSION削除後も外側の結果は残る。

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

コメントはkind=comment、title=null、target=元PRまたはopenの同repositoryの集約先Issue番号。新しいIssue → 元PRの事後調査コメント → 集約先コメントの順序を守る。helperは本文末尾に一意マーカーを付け、intentを保存してからPOSTする。stateに保存された本文が送信内容であり、スキルの元ファイルとの比較では末尾マーカーを含める。

同じkeyを再実行したときは読み取りによる照合だけを行う。全ページからマーカーと本文を照合し、投稿済みならそのURLを採用する。見つからなくても再POSTしない。成否不明の投稿があれば次の投稿も拒否する。別keyに変えて同じ投稿を送らない。外部への書き込みが未確認なら正常終了にしない。

## 保存・排他・版

state.jsonのschema_versionは1。helperがflockの下で一時ファイルをfsyncし、replaceして保存する。未対応schemaは拒否する。排他はコマンド単位のflockと、runに保存する司令塔のnative sessionの両方で行う。古さだけでロックを奪わない。司令塔を移すときはHerdrの現在の一覧から旧sessionの不在を確かめる。

意図保存と外部の操作は一つのトランザクションにはできない。意図があり結果が無い窓は「不明」として扱う。startの応答消失は一意agent名とpaneで照合できれば未送信の状態へ戻せる。prompt送信の成否不明はackを待つ。splitの応答消失など、所有paneを確定できない場合は手動調査で止め、再実行しない。

main更新はcleanup工程でのみ行う。次の子は更新後の親main checkoutから起動し、使用版を新たに保存する。helperは呼び出しごとに再ロードし、schemaを検査する。実装・持ち越しの途中で使用版が変わった場合は止める。cleanup後に未確認worktree・claim・更新の衝突があれば、次のIssueへ進めない。

同じOSユーザーによる状態の直接改ざん、Herdrサーバーの交換、別repositoryのキュー、常駐継続運転、失敗した子の自動修復は対象外。状態や識別情報が不一致なら止める。
