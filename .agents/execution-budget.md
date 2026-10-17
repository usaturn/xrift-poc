# Issue の実行予算と方針転換

Issue 対応が長時間化したときに、当初の計画の完遂や再試行そのものが目的にならないよう、予算と見直しの時点を先に決めておく。この文書は、Plan の「実行予算」節に書く項目、時間の数え方、checkpoint での判断、失敗の分類と再試行の上限を定める。

個々の長時間 worker の監視と、途中で止めてよい条件は `monitoring-long-tasks` に定義する。ここでは Issue 全体を扱う。

## 適用範囲

- Plan を書く Issue 対応（`superpowers:subagent-driven-development`・`superpowers:executing-plans`・`issue-to-pr`）と、評価・反復実行を含む run
- 30 分ほどで終わる単純な変更では、「実行予算」節を全体の目安 1 行にしてよい

## 時間の数え方

- **能動時間** は、壁時計の経過から利用者待ちの区間を除いたもの。利用者待ちの区間は、controller が質問・承認を求めてターンを終えた時点から、次の利用者の入力で再開するまでとする
- 並行して動く worker の時間は controller の壁時計の内側にあるので、能動時間に足さない
- 記録は `.agents/scripts/issue_budget.py` の ledger（JSONL）で始める。大きな計測基盤（#220）の完成を待たない。ledger は親の main worktree の `tmp/agents/<topic>.XXXXXX/budget.jsonl` に置く

```bash
# PARENT は親の main worktree、SESSION は agent_tmp.py mkdir で作ったセッションディレクトリ
budget() { uv run --project "$PARENT/tools/devenv" --locked python "$PARENT/.agents/scripts/issue_budget.py" "$@"; }
budget start --ledger "$SESSION/budget.jsonl" --budget-minutes 240
# 質問・承認を求めてターンを終える前と、再開した直後
budget mark --ledger "$SESSION/budget.jsonl" --event await-user --reason "設計の確認"
budget mark --ledger "$SESSION/budget.jsonl" --event resume
# checkpoint では show で数えてから、判断を 1 行で残す
budget show --ledger "$SESSION/budget.jsonl"
budget mark --ledger "$SESSION/budget.jsonl" --event checkpoint \
  --decision continue --reason "受け入れ条件 2 件を新たに満たした"
```

シェルの関数や変数は次の呼び出しに残らないランタイムがある。その場合は 1 回の呼び出しの中で定義して使う。

`show` は能動時間・利用者待ち・残り・使用率、期日を過ぎた checkpoint（閾値を越えた後に checkpoint の記録が無いもの）、signature ごとの失敗の回数と上限到達、安全の失敗の有無、最後の判断を JSON で返す。`mark` は利用者待ちを閉じずに他の記録を足すことを拒否する。壊れた行は行番号付きで拒否する（終了コード 2）。

## Plan の「実行予算」節

1. Issue 全体の能動時間の予算と、その根拠（似た Issue の実績、Task の数）
2. phase ごとの目安（準備・実装・検証・PR など）
3. worker ごとの絶対期限・予約枠・無進捗の打ち切り。`task_watch.py` の宣言にそのまま写す値にする
4. 失敗の種類ごとの再試行の上限。既定は、同じ失敗の再試行を 1 回まで
5. checkpoint の時点: phase の境界、予算の 50%・100%、worker が失敗した直後
6. 縮小・切り出し・方法変更の候補。困ってから考えず、最初に書いておく

## checkpoint での判断

判断の材料は、記録から機械的に取れるものに限る。

- 成果の増加: 前回の checkpoint から新たに満たした受け入れ条件・完了した Task
- 同じ失敗の回数（`show` の `failures`・`retry_limit_reached`）
- 付随処理（準備・監査・記録・再準備）と本体の作業の比率
- 能動時間の残り

| 状況 | 判断 |
|---|---|
| 安全の失敗がある | **全体停止**。予算に関係なく止め、再試行しない |
| 予算の 100% に達した | **停止して報告**。延長は自分で決めない。残作業・候補の対策・必要な予算を示す |
| 同じ失敗が上限に達した | **方法変更**（別の手段・別のランタイム・範囲の縮小）。同じ手段で再試行しない |
| 前回から成果が増えず、付随処理が本体を上回った | **縮小**（必要な受け入れ条件に絞る）または **切り出し**（残りを持ち越し候補にする） |
| 上のどれにも当たらない | **継続** |

- 判断は ledger に `checkpoint` として残す（`--decision` は `continue`・`shrink`・`split`・`change-method`・`stop`）。理由と次の行動を `--reason` に書く
- 「切り出し」で新しい Issue を作るのは、利用者の許可を得てから。許可が無い run では、PR 本文と Spec に持ち越し候補として書く
- 完了条件を変える縮小（例: 「30 件すべて有効」を「各設定 1 件」にする）は、Spec の決定記録に変更として明記する
- `issue-to-pr` の run では、予算の 100% と安全の失敗を停止条件として扱う

## 失敗の分類と再試行

| 分類 | 例 | 扱い |
|---|---|---|
| 信頼性 | timeout、成果形式の失敗、一時的な API エラー | 事前に決めた回数まで再試行し、上限で方法を変える。品質の値は unknown とし、0 や成功にしない |
| 安全 | 権限拒否の回避、削除ガード違反、想定外の外部送信、他タスクの情報の混入、実 CLI・認証情報の想定外の使用 | 全体停止。効率のために無視・再試行しない |

権限拒否は、拒否された操作を別の経路で通そうとしない。拒否の単位と理由を記録し、判断を利用者に返す。

## 段階式の評価

反復評価は次の順にする。安定する前に全件の完遂を完了条件にしない。

1. pilot: fake の CLI・fixture で、安全の境界と出力形式を確かめる。通らないランタイムは本評価に入れない
2. 探索: 各設定を 1 回ずつ
3. 必要な反復: 採用の候補と現行だけを、必要な回数

## 停止した後の記録

- worker を止めたら、`task_watch.py` の `final.json`（停止理由・停止確認・部分成果・残存パス・次の一手）を使う。長い handoff を毎回作らない
- Issue 全体を止めるときは、停止理由・残作業・次に選ぶ対策と理由・残存パスを数行で残す。未完了を完了と書かない
- 大きなログは全文を読み直さない。`task_watch.py status` と `issue_budget.py show` の JSON から判断し、根拠が足りないときだけログの末尾を必要な範囲で読む

## この規則の限界

- 能動時間は、controller が `await-user` を記録し忘れると多めに出る（早めに checkpoint が来る安全側の誤差）
- 判断の材料は記録の質に依存する。成果の増加は受け入れ条件と Task の単位で数え、作業量の多さで数えない
