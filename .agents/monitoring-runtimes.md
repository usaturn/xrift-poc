# 長時間タスク監視のランタイム対応表

`monitoring-long-tasks` が定める監視手順を、この Dev Container の各ランタイムでどのツールに割り当てるかだけを示す。

- OR 待機の型、レポートが無いまま対象が消えた場合の異常終了扱い、`verify-devenv.sh` の PID の控えと終了コードの確認は、スキル step 1〜3 に従う。ここでは重複させない
- controller がターンを完了通知に委ねてよいかは扱わない

## 対応表

| ランタイム | 完了 | 生存 | 進捗 / stall | OR 待機の組み方 |
| --- | --- | --- | --- | --- |
| Claude Code | Bash `run_in_background: true` の完了通知（終了コードを含む）。サブエージェントは `Agent` の完了通知 | 既知の PID を `kill -0` | 完了通知やツール結果が返す出力ファイル path を `Read`。`verify-devenv.sh` はログ path を印字しないので、起動時に出力先を自分で決める | 下の参考実装を `run_in_background: true` に渡す。進捗を都度イベントとして受け取るときだけ `Monitor` を使う |
| Devin | バックグラウンド `exec`（`timeout: 0`、または既定の自動バックグラウンド化）で起動した対象を `get_output` で待つ。`get_output` は対象プロセスの終了で即復帰し、終了コードは出力末尾の `Exit code: <n>` で確認する。1 回の待機上限は約 280 秒のため、タイムアウトしたら呼び直す。非同期の完了通知が届くのは `run_subagent` のサブエージェント（`<subagent_completion_notification>`）だけであり、シェルプロセスの終了は push されない | 既知の PID を `kill -0`（`exec` 経由）。`verify-devenv.sh` の PID 同一性確認はスキル step 3 のまま適用する | 起動時に決めたログ path を `read` で読む。mtime / サイズが必要なら `exec` で `stat` を使う | 下の参考実装をそのままバックグラウンド `exec`（`timeout: 0`）で動かし、その監視シェルを `get_output` で待つ。ループの脱出がそのまま `report exists OR worker is not alive` の解消になる |
| pi | 完了通知もバックグラウンド実行も無い（`.pi/APPEND_SYSTEM.md`）。PR 修正ループの `loop.py review` は同期で戻るので、bash ツールの foreground で実行し、標準出力の JSON と終了コードを確認する。期限は runner が守る | 生存は runner の監督が保持する。既知の PID は `kill -0`（bash ツール） | `cycle-<n>.json` の `review.output_dir` の下のログを読む。mtime / サイズは bash ツールの `stat`。10 分間隔で 2 回 mtime が変わらなければ stall を報告する | `loop.py review` を foreground で実行する。ターンを占有できない事情があるときは、バックグラウンドに置かず、中断・保持の回収に `loop.py status` と `recover --operator-confirmed-stopped "<確かめた事実>"`（`pr-review-fix-loop` の「止まったとき」）を使う |
| Cursor | TBD | TBD | TBD | TBD |

```bash
# $pid の導出と、待機終了後の EXIT=<code> 確認はスキル step 3 に従う
until [ -f "$REPORT" ] || ! kill -0 "$pid" 2>/dev/null; do
  sleep 30
done
```

## PR レビュー委譲の起動可否

`pr-review` の補助委譲は、バックグラウンド起動・期限付き待機・停止・停止確認をそのランタイムで利用できる場合だけ起動できる。委譲 worker の停止・停止確認の手段と起動可否の対応は次のとおり。TBD のセルは、停止系ツール名をリポジトリまたは公式文書で確認できたときだけ埋める。

| ランタイム | 委譲 worker の停止・停止確認 | 起動可否 |
| --- | --- | --- |
| Claude Code | TBD | TBD |
| Devin | サブエージェント（`run_subagent`）に停止手段が無い。`kill_shell` は `exec` シェルのみが対象 | 起動条件を満たせず常時省略 |
| pi | 単発サブエージェントは `async: false` の同期起動のみ（`.pi/APPEND_SYSTEM.md`）。バックグラウンド起動と停止ができない | 起動条件を満たせず常時省略 |
| Cursor | TBD | TBD |

## task_watch による途中の判断と停止

`monitoring-long-tasks` の「事前宣言した打ち切り」の対象は、同スキルの `scripts/task_watch.py run` で起動する。判定と停止は watcher が行うので、controller が同期の呼び出しで待っている間も、宣言した期限・無進捗・予約枠・失敗の条件で止まる。controller は watcher の終了（完了通知・同期の戻り）を待ち、途中では `status` の短い JSON だけを読む。

| ランタイム | 起動と完了 | 途中の確認 | 方針を変えるとき（宣言どおりの停止） |
| --- | --- | --- | --- |
| Claude Code | `run` を Bash の `run_in_background: true` で起動する。watcher の終了が終了コード付きの完了通知になる | `status` を Bash で実行する | `stop` を Bash で実行する |
| Devin | `run` をバックグラウンド `exec`（`timeout: 0`）で起動し、`get_output` で待つ | `status` を `exec` で実行する | `stop` を `exec` で実行する |
| pi | `run` を bash ツールの foreground で実行する。controller は戻るまで待つが、watcher が宣言どおりに判定・停止する。これが同期のみのランタイムでの代替策である | 途中では確認できない。戻った後に標準出力の JSON か `final.json` を読む | 途中ではできない。起動前に宣言した条件（期限・無進捗・予約枠・失敗）が代わりに働くので、止める条件は起動前に決めておく |
| Cursor | TBD | TBD | TBD |

- `run` の終了コードは 0 = `completed`、1 = 完了していない、2 = 入力の誤り、3 = `hold`（停止を確認できない、終了後も同じ group にプロセスが残っている、または `--stop none` の watcher に `stop` を送った）。3 のときは `final.json` の `residual_pids` と `paths` を報告し、依存する作業を始めない
- `--stop none` の watcher は停止の条件を満たしても止めず、対象が自分で終わるまで動き続ける。pi の行の「宣言した条件が代わりに働く」は `INT`・`TERM` を宣言した場合に限る
- agent_runner を包むときは `--stop INT` にし、`--escalate-kill` を付けない。下の節の「中断」と同じ経路で runner が子孫を止め、結果を保存する

## agent-runner の同期実行と停止確認

`uv run --project tools/devenv python -m tools.agent_runner run ...` は、
司令塔のランタイムによらず foreground の同期コマンドで待てる。
完了・生存・進捗を分ける原則は `monitoring-long-tasks` のまま適用する。
Herdr の pane・idle 判定や別ランタイムへの暗黙の切り替えは使わない。

- 完了: コマンドの終了コードと、summary が示した `result.json` を確認する。
  成功/self=0、失敗/部分成功=1、設定/入力=2、SIGINT=130。
  result の state・識別情報・各 attempt の `stop_confirmed` も確認し、
  ログの更新や receipt ファイルの存在だけで成功と扱わない
- 生存: 既知の runner PID の `kill -0` とコマンド/cwd の同一性を確認する。
  プロセスを自分で background 化できない pi では bash ツールの同期呼び出しを保持する
- 進捗: 指定した一意の `--output-dir`、または完了 summary の実際の artifact_dir の
  担当別 stdout.log / stderr.log を必要な範囲だけ読む。
  十分な時間がかかる仕事は起動時に新規の output-dir を明示し、既存領域を再利用しない
- 中断: runner 自体へ SIGINT を送り、終了まで待つ。runner は共有 cancel と
  process group/観測済み子孫の TERM→KILL により停止を試み、確認結果を保存する。
  runner をいきなり SIGKILL すると、この終了処理と結果保存は保証できない
- 停止未確認: `stop_confirmed=false` と保存された work_dir/log の実際のパスを報告し、
  依存する仕事・fallback を開始しない。使用中の領域は消さない。
  sensitive_paths に報告された receipt は秘密が残る可能性があるため、その内容を転載しない

role の期限は実行枠を得た時点から準備・明示 fallback を含めて数え、
停止確認の猶予で超過することがある。既存 checkout の外部変更は
outside_changes として報告されるが、他セッションの変更との因果は断定しない。
ランタイムの adapter はローカルの準備・出力解析だけを短時間で行う。

ログ・worktree は成功時も保持する。停止を確認して成果を保存した後に、
対象 repository の `git worktree remove <work_dir>` で登録を外してから、
その run が所有する領域だけを回収する。停止未確認・変更が残る worktree を
勝手に削除したり、他の run の領域を掃除したりしない。

## 注記

- スキル step 1 が禁じる foreground sleep ループとは、エージェント本体のターンを `sleep` で占有することを指す。上の `sleep 30` のように監視スクリプト内に閉じた `sleep` は該当しない。待機はバックグラウンドへ置き、ターンは通知で戻す
- `BashOutput` と `TaskOutput` は使わない
- Devin にはシェルプロセス終了を届ける非同期通知が無く、`Monitor` 相当のツールも無い。controller は `get_output` のブロッキング呼び出しを繰り返してターンを保持し、stall ポーリングはその合間にログを読んで行う
- TBD のセルは埋めない。ツール名をリポジトリまたは公式文書で確認できたときだけ書く。Cursor / Codex は CLI が導入済みでも、監視ツール名が確認できるまで行を追加しない。pi の行は `loop.py review` の同期実行と bash ツールという、このリポジトリで確認済みの仕組みだけを書く
