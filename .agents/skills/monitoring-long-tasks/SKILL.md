---
name: monitoring-long-tasks
description: 長時間かかるコマンド・検証ジョブ・バックグラウンドプロセス・サブエージェントを待つとき、または監視するときに使う。完了・生存・進捗・stall の検出が重要になる場面が対象。
---
# 長時間タスクの監視

完了（成果物または通知が届いた）、生存（対象プロセスがまだ存在する）、進捗（ログ出力が進んでいる）、実質進捗（調べた内容が新しく増えた）、成果の保存は別々のシグナルである。どれか 1 つから別のものを推測しない。

## 手順

1. `report exists OR worker is not alive` を待つ。レポートだけを待たない。この待機と stall のポーリングは controller が担当し、`report exists OR worker is not alive` が成立するまでターンを保持する。完了通知とは、ランタイムが出す終了コード付きのプロセス終了通知を指し、レポート・成果物・サブエージェントの結果は完了通知ではない。完了通知は `worker is not alive` の側を満たしうるが、worker が生存している間に controller のターンを終えてよいという許可ではない。待機にはランタイムの待機機能か通知機能を使い、フォアグラウンドの sleep ループは使わない。停止の例外は「事前宣言した打ち切り」の節の対象だけである。
2. 期待したレポートが無いままの終了は異常として扱う。対象の終了を観測した後にレポートを 1 回再確認し、その後は失敗として扱い、ログ末尾の `EXIT=<code>` を確認する。
3. 生存の確認には、既知の PID に対する `kill -0` を優先する。PID は対象を起動したときに控える（`verify-devenv.sh` はログのパスも PID も印字しない）。`verify-devenv.sh` が対象で `kill -0` が成功したら、その PID の同一性を確かめる。`ps -p <pid> -o args=` が `scripts/ci/verify-devenv.sh` を含み、かつ `readlink -f /proc/<pid>/cwd` が対象の worktree と一致すること。どちらかの同一性確認に失敗したら、対象の worker は生存していないものとして扱う。その PID について `pgrep` にフォールバックしない。それ以外の対象では、`kill -0` に加えて意図したコマンドラインと対象の cwd を確かめれば足りる。args に `scripts/ci/verify-devenv.sh` を含むことを求めない。`verify-devenv.sh` では、待機条件のどちらで待機が終わった場合も、起動時にログへ書き足した末尾の `EXIT=<code>`（またはランタイムが返す終了コード）を確認する。
4. `pgrep -f` は PID が得られないときだけ使う。対象のコマンドラインに一致させ、パターンの文字列自体が一致しないように、パターンをスクリプトファイルに置くか、`[s]cripts/ci/verify-devenv\.sh` のようなパス付きのブラケット形式を使う。ただし、監視コマンドの別の箇所に対象のパスをそのまま書くと、それが監視コマンド自身に一致することがある。一致した結果を確かめ、監視プロセスの PID とその祖先のすべての PID を除く。対象が `verify-devenv.sh` のときは、残った各 PID に手順 3 の `verify-devenv.sh` 用のコマンドライン・cwd の同一性確認を適用する。対象が `verify-devenv.sh` でないときは、コマンドラインが意図した対象のコマンドに一致し、かつ cwd が対象の worktree である PID を残す。1 つも残らなければ、対象の worker は生存していない。ログのパスをプロセスの目印に使わない。
5. `verify-devenv.sh` は起動する側がログの出力先を決める（例: `tmp/agents/<topic>.XXXXXX/` の下）。そのパスを控えて監視する。
6. 約 10 分ごとに、ログの mtime、サイズ、または最終行を確認し、確認した時刻と値を進捗の記録に残す。2 回続けて変化が無ければ stall として報告する。生存中で stall したタスクがあっても、それを kill してよい許可には決してならない。例外は「事前宣言した打ち切り」の節の対象だけである。
7. 依存する作業を起動する前に報告する。状況報告には毎回、タスクの識別情報、実際のログのパス、終了コード（判明しているか不明か）、最後の進捗を含める。

## 実質進捗

実質進捗とは、対象コードパス・確認した挙動・結論・残る具体的な疑問が新しく増えたことである。heartbeat、同じ内容の繰り返し、ログの mtime・サイズだけの更新は数えない。stall の報告（手順 6）はログの変化で判断するが、途中で打ち切るかどうかは実質進捗で判断する。

worker に実質進捗を報告させるときは、次の JSONL を進捗ファイルへ 1 行ずつ追記させる。

```jsonl
{"kind": "progress", "items": ["src/x.py:120 の分岐を確認", "空入力で例外になることを再現"]}
{"kind": "heartbeat"}
{"kind": "failure", "class": "reliability", "signature": "pytest-timeout"}
{"kind": "saved", "path": "/abs/path/partial.md"}
```

- `items` はこれまでに無い短い文字列にする。既知の項目だけの `progress` は heartbeat と同じ扱いになる
- `failure` の `class` は `reliability`（timeout・成果形式の失敗など）か `safety`（想定外の外部送信・他タスクの情報の混入・権限拒否の回避など）
- 結果や部分成果を書いたら、すぐに `saved` を追記させる。途中で止められても成果が残る
- `progress` を書く直前に毎回 `finalize.requested`（state dir の中）があるかを確かめさせる。あれば新しい項目を書かず、`saved` と結果だけを書かせる

## 事前宣言した打ち切り

生存中の worker を停止しないというルールの例外である。起動前に宣言を記録した worker だけを、宣言した条件で止めてよい。宣言が無い・欠けている worker は、従来どおり stall を報告して残す。起動した後に宣言を書き足して止める対象にはしない。

対象にしてよい worker:

- 読み取り専用の委譲（PR レビューの補助委譲、調査・評価の worker）
- 専用の使い捨て領域（agent_runner の attempt の worktree、評価用の scratch）だけに書く worker

対象にしない worker（宣言があっても止めない）:

- `verify-devenv.sh`・`xrift check`・テスト・lint などの検証コマンド
- 共有の feature worktree に書く実装 worker
- push・PR の作成・Issue の更新など、取り消せない外部操作を含むコマンド

起動前に記録する項目:

1. 識別情報: PID と starttime（`/proc/<pid>/stat`）、コマンドライン、cwd。caller 自身・その祖先・照合できない PID を対象にしない
2. 絶対期限と予約枠。調査の終了時刻は「絶対期限 − 予約枠」
3. 無進捗の打ち切りの時間と、実質進捗を読む進捗ファイル
4. 停止の手段（送るシグナル）と、停止確認の方法・上限（1 回、30 秒以内。SIGKILL まで送ると宣言した場合は最大でその 2 倍）
5. 結果・部分成果のパスと、停止した後の worktree・claim の扱い
6. 同じ失敗の再試行の上限

判定は次の優先順で行う。

0. 対象が終了した → 安全の失敗が記録されていれば完了にしない。同じ process group にプロセスが残っていれば保留にする。それ以外は、終了コード 0 で結果がそろっていれば完了
1. 安全の失敗 → 停止。controller は Issue 全体を止め、再試行しない
2. 絶対期限 → 停止。進捗があっても延長しない
3. 同じ失敗が上限に達した → 停止。controller は方法の変更を検討する
4. 予約枠に入った後の新規調査（新しい実質進捗）→ 停止
5. 予約枠の前で、実質進捗が無いまま打ち切りの時間を過ぎた → 停止
6. 調査の終了時刻 → 保存へ移るよう求める。新規調査はさせない。求めてから少なくとも 1 秒（ポーリング間隔が長ければその間隔）の間に書かれた項目は、行き違いとして新規調査に数えない
7. 上のどれでもない → 継続

停止した後:

- 停止を確認できたら、部分成果から保存し、停止理由・残作業・次の対策と理由を短く記録する。Issue 全体をどうするかは `.agents/execution-budget.md` の checkpoint の判断に従う
- 停止を確認できなければ「停止不能のため保留」とする。対象・停止状態・残存パスを報告し、依存する作業を始めず、対象の worktree を消さない。その報告をしてターンを終えてよい
- 止めた worker を同じ条件で起動し直さない。起動し直すのは、再試行の上限の範囲で方法を見直した場合だけである。PR レビューの補助委譲は起動し直さない
- PR レビューの補助委譲の具体値（起動から 10 分、無進捗 5 分）と起動の条件は、pr-review の time-budget の参照文書に従う

### task_watch で宣言・判定・停止する

`scripts/task_watch.py` は、対象を独立した session で起動して宣言を `declaration.json` に記録し、ポーリングごとに上の判定を行い、宣言どおりに止める。controller が読むのは短い JSON（`status.json`・`final.json`）だけで、ログを読み込まずに判断できる。controller が同期の呼び出しで待っている間も、watcher が判定と停止を行う。

```bash
# SKILL_DIR はこのスキルの実ディレクトリ、PARENT は親の main worktree、SESSION は作業用のディレクトリ
task_watch() { uv run --project "$PARENT/tools/devenv" --locked python "$SKILL_DIR/scripts/task_watch.py" "$@"; }
task_watch run --state-dir "$SESSION/watch-eval-1" \
  --deadline-seconds 900 --reserve-seconds 300 --no-progress-seconds 300 \
  --progress-file "$SESSION/eval-1.progress.jsonl" --result-file "$SESSION/eval-1.result.json" \
  --stop INT --stop-grace-seconds 30 -- <対象の argv>
task_watch status --state-dir "$SESSION/watch-eval-1"   # 途中の確認。ログは出さない
task_watch stop --state-dir "$SESSION/watch-eval-1"     # 方針を変えるときだけ。宣言どおりに止める
```

- `--stop` は必須。`INT`・`TERM` は、宣言したシグナルを対象の process group に送る。`none` は監視だけでシグナルを送らない。停止の条件を満たしても `status` の判定が `hold` になるだけで、watcher は対象が自分で終わるまで動き続ける。同期のみのランタイムで `none` を使うと、controller は終わるまで待つことになる
- 同じ失敗の上限は `--max-repeated-failures`（同じ signature の失敗の回数。既定 2 は「再試行 1 回まで」に当たる）で宣言する。無進捗の打ち切りは `--no-progress-seconds`、予約枠は `--reserve-seconds` で、どちらも省略すれば判定に使わない
- agent_runner を対象にするときは `--stop INT` にし、`--escalate-kill` を付けない。runner は SIGINT で子孫を止めて結果を保存するが、SIGKILL はその処理を壊す。止めた後は runner の result.json の各 attempt の `stop_confirmed` も確認する
- `--state-dir` は存在しないパスにする（watcher が作る）。中に `declaration.json`・`status.json`・`events.jsonl`・`final.json`・`finalize.requested`・`target.stdout.log`・`target.stderr.log` ができる
- `run` の終了コード: 0 = `completed`、1 = 完了していない（`stopped`・`exited_incomplete`）、2 = 入力の誤り、3 = `hold`（停止を確認できない、終了後も同じ group にプロセスが残っている、または `--stop none` の watcher に `stop` を送った）。終了時に `final.json` と同じ内容を標準出力にも出す
- `final.json` の `next_step` が判定ごとの次の一手、`partial_results` が残った部分成果、`residual_pids` が終了・停止の後も同じ process group に残った PID である
- `stop` は、watcher の識別情報（PID・starttime・コマンドライン）が一致しない場合と、caller 自身・その祖先の場合に、シグナルを送らずに拒否する（終了コード 2）
- `status` が `watcher_alive: false` と `target_alive: true` を返したら、watcher だけが消えて対象が監視の無いまま残っている。手で kill せず、「停止不能のため保留」として対象・残存パスを報告する
- sandbox ではない。setsid で process group を抜けた子孫は確認できない

ランタイムごとの使い方（バックグラウンド起動、同期のみのランタイムでの代替）は `.agents/monitoring-runtimes.md` を参照する。

## 早見表

| 状態 | 対応 |
| --- | --- |
| `completed` | レポートが届いた。レポートを読み取って先へ進む。 |
| `exited without report` | 異常。レポートを再確認し、失敗を報告し、`EXIT=<code>` を確認する。 |
| `alive and progressing` | 待機を続ける。最新のログ位置を記録する。 |
| `alive but stalled` | 4 項目すべてを付けて stall を報告する。事前宣言の対象でなければ、タスクは停止せずに残し、依存する作業を保留する。 |
| `research deadline reached` | 新規調査をやめさせ、保存へ移らせる。予約枠で新規調査が続けば停止する。 |
| `predeclared stop condition met` | 宣言どおりに停止し、停止を 1 回確認する。確認できれば部分成果から保存して次の対策を選ぶ。確認できなければ報告し、依存する作業を始めずにターンを終える。 |

## よくある誤り

| 誤り | 直し方 |
| --- | --- |
| レポートの存在だけを待つ | `report exists OR worker is not alive` を待つ。 |
| 監視プロセスに一致してしまうインラインの `pgrep -f` | スクリプトファイルかブラケット形式のパターンを使い、一致した結果を確かめる。 |
| ログのパスを `pgrep` の目印に使う | コマンドラインに一致させる。ログのパスは進捗の確認だけに使う。 |
| 固定先への手動のログリダイレクトを必須にする | 実行ごとに印字されたログのパスを使う。 |
| 期限に間に合わせるために、生存中で stall したタスクを kill する | stall を報告して判断を求める。止めてよいのは「事前宣言した打ち切り」の対象だけである。 |
| 検証コマンドや実装 worker を、宣言を書いて止める | 検証コマンド・共有の worktree に書く実装 worker・外部操作を含むコマンドは、宣言があっても止めない。 |
| caller 自身の PID・cwd を worker の識別情報として記録する | worker の PID・starttime・コマンドライン・cwd を記録する。task_watch は対象を自分で起動して記録する。 |
| heartbeat やログの更新を理由に打ち切りを延ばす | 実質進捗（新しい項目）だけが無進捗の時計を戻す。絶対期限は延ばさない。 |
| 予約枠を prompt に書くだけで、調査の継続を止めない | 調査の終了時刻を宣言し、予約枠での新規調査を停止の条件にする。 |
| 大きなログを全文読み直して状況を確かめる | `status` / `final.json` の短い JSON で判断し、根拠が足りないときだけログの末尾を読む。 |
| 報告する前に依存する作業を始める | 先に報告し、状況の 4 項目をすべて含める。 |

結果をそのまま返す短い同期コマンドには、このスキルを使わない。
