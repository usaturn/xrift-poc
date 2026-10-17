# pr-review-fix-loop の記録（`pr-review-fix-loop/v1`）

helper（`scripts/loop.py`）が書く記録の形式・置き場所・終了コードを定める。記録を書くのは helper だけで、司令塔とレビュアーは書かない。

この文書では、レビュー修正ループの 1 回分を「サイクル」、レビューを行う 1 モデル分を「レビュアー」と書く。

## 1. 置き場所

レビューディレクトリ（親の main worktree の `reviews/<owner>/<repo>/#<PR>-<ブランチ>/`。保存先の規則は `.agents/scripts/review_target.py`）の下に置く。どれも commit しない。

```text
reviews/<owner>/<repo>/#<PR>-<ブランチ>/
└── loop/
    ├── lock.json                   # PR ごとのロック（使用中の run だけ）
    └── <run_id>/
        ├── run.json                # run の設定・割り当て・状態・結果・資源
        ├── cycle-<n>.json          # サイクルごとの記録
        └── cycle-<n>/
            ├── comments.json       # レビュアーに渡した PR の全コメント（gh api --paginate --slurp の出力）
            └── review-result.json  # runner の結果（BatchResult）の写し
```

- レビューディレクトリの直下には `<model>.md` 以外のファイルを置かない（`.agents/review-findings.md` 2 章）。`loop/` はサブディレクトリである。
- runner の worktree と出力は、`${TMPDIR:-/tmp}/xrift-poc-pr-loop-XXXXXX/cycle-<n>-review/` に一意に作る。停止を確認した後に helper が回収する（`run.json` の `resources`）。

## 2. ロック（`lock.json`）

| キー | 値 |
| --- | --- |
| `schema`・`kind` | `"pr-review-fix-loop/v1"`・`"lock"` |
| `run_id` | 32 桁の hex |
| `pr` | `{"repository", "number", "url"}` |
| `run_dir` | run のディレクトリの完全パス |
| `created_at` | UTC の `YYYY-MM-DDTHH:MM:SSZ` |

- `start` が新規作成だけで取る（既にあれば終了コード 3）。古く見えても上書きしない。
- 消すのは `finish` と `recover` だけで、`run_id` が一致する場合に限る。

## 3. run（`run.json`）

| キー | 値 |
| --- | --- |
| `run_id`・`pr`・`review_dir` | 対象 |
| `tool_root`・`tool_commit` | helper とスキルを読んだ checkout と、その commit（レビュアーの worktree はこの commit から作る） |
| `controller` | `{"model", "runtime"}`（司令塔） |
| `options` | `max_cycles`、`hold_action`、`assign`（`--assign` の値）、`template`、`config`、`fresh_review` |
| `assignments` | 読み込んだ設定の有効な割り当て。`review.inspect` と `fix.implement`・`fix.self-review`・`fix.fix-check`（`agents`・`fallback`） |
| `started_at`・`ended_at` | 時刻 |
| `state` | `running`・`ended`・`held`（停止を確認できない資源があり、ロックを保持）・`abandoned`（結果の無いまま回収した） |
| `result` | `{"outcome", "ended_by", "stop_reason", "merged", "cycles"}`。`outcome` は `fixes_pushed`・`no_fix_needed`・`stopped`、`ended_by` は `outcome`・`cycle_limit`・`abort` |
| `resources` | `[{"path", "kind": "runner_output", "cycle", "state", "reason"}]`。`state` は `created`（未回収）・`removed`・`retained`（消さずに残した） |
| `notes` | `recover` で受け取った確認の事実など |

`options.hold_action` はこの run の実効方針である。新規 `start` は未指定（API の `None` を含む）を `skip` として保存し、明示した `skip`・`fix` は維持する。`cycle-begin`（再利用時）と `review` が返す `planning.hold_action` はこの保存値と同じで、計画担当はそれを適用する。既定値の適用を利用者の明示指定と記録しない。既存 run の再開では保存値を変えず、旧 run の null も未選択のまま維持する。

## 4. サイクル（`cycle-<n>.json`）

| キー | 値 |
| --- | --- |
| `cycle`・`head` | サイクルの番号（run の中で 1 から）と、対象の PR HEAD |
| `state` | `reviewing`・`planning`・`ended` |
| `prior_round` | `cycle-begin` の時点で PR にあった判断の記録の最大の `round`。`cycle-end` はこれより後の記録だけをこのサイクルの記録として受け付ける（前の run の同じ HEAD の記録を拾わない） |
| `review.mode` | `run`（レビュアーを起動した）・`reuse`（現在の HEAD の成果を使った） |
| `review.reviewers[]` | `requested_agent`・`actual_agent`、runner の state、`verdict`（`accepted`・`rejected`）と `reason`、runner が観測した `identity`、payload の `reviewer`・`timing`、成果 2 件のパスと SHA-256、`active_findings`、pr-review の `cleanup`・`warnings`・`retained_paths` |
| `review.reused[]` | 再利用した成果 2 件のパスと SHA-256、`model`・`runtime`・`reviewed_head`・`active_findings` |
| `review.batch` | runner の run ID・state・assignments・`result_file`（写し）・`outside_changes` |
| `review.output_dir`・`comments_file`・`started_at`・`ended_at`・`seconds`・`cleanup` | レビューの実行と回収 |
| `planning` | `round`・`comment_id`・`posted`、`outcome`・`commit`・`stop_reason`・`merged`、判断の件数（`state` 別・`change` 別）、`planning_file`・`verified_file` とその SHA-256、`finalize`（結果の内容・`result_file`・`round_dir`）、`role_runs[]`（`run_roles.py` の出力ごとの実際の担当と状態）、`ended_at` |

レビュー・確認・判断の中身は写さない。参考データ（`.agents/review-findings.md` の識別情報・時間）は、パスで参照する成果の側にある。

## 5. 終了コードと `stage`

| コード | 意味 | 主な `stage` |
| --- | --- | --- |
| 0 | 成功 | ― |
| 1 | 想定外の内部エラー（stderr に traceback） | ― |
| 2 | 引数・入力の誤り | `arguments`、`config` |
| 3 | 排他・状態の順序の違反 | `lock_held`、`not_owner`、`state`、`interrupted_review` |
| 4 | 対象の食い違い | `pr_not_open`、`head_moved`、`record_missing`、`record_inconsistent`、`pushed_head_mismatch`、`merge_mismatch` |
| 5 | 外部（GitHub・git・runner）の失敗 | `github`、`git`、`parent`、`runner` |
| 6 | 全レビュアーの結果を受け付けられなかった | `review` |
| 7 | 資源の停止を確認できない（ロックを保持） | `resources` |

失敗の出力は `{"status": "error", "stage", "reason", "run_dir", "next", "requires_input"}`。`requires_input` は、`next` に足すオプションと、そこに書く事実の説明（`recover` の `--operator-confirmed-stopped` など）。書きかけの値を実行させないよう、`next` にはそのオプションを含めない。失敗しても、それまでに保存した記録と runner の結果は書き換えない。引数の書式の誤り（argparse が拒否するもの）だけは、標準エラー出力の usage と終了コード 2 で、JSON を書かない。

## 6. 読む側の注意

- サイクルの番号は run の中の通し番号で、判断の記録の `round`（PR 全体の通し番号）とは別である。対応は `planning.round` にある。
- `review.reviewers[].identity` は runner の事後の観測、`reviewer` は pr-review が成果に記録した値で、同じとは限らない。未観測の値を設定から補わない。
- `merged` は、finalize の結果と PR の実際の状態の両方で確かめた値である。
- `recover` は、停止を確かめた事実を受け取った資源でも、秘密が残りうるファイル（runner の `sensitive_paths`。runner の結果が無いときはディレクトリ全体）を消さずに残し、`retained` に完全パスを書く。
