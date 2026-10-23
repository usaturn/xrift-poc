---
name: pr-review-fix-loop
description: 利用者が `/pr-review-fix-loop <PR>`（Pi は `/skill:pr-review-fix-loop <PR>`）で明示的に起動したとき、または issue-to-pr が利用者の明示した `--merge` で PR を作った後に呼ぶときだけ使う。runner で並行に起動したレビュアーのレビューと、review-fix-planning による修正・push・最終化を、マージか停止かサイクルの上限まで繰り返す。通常の PR レビューや修正計画の依頼からは起動しない。
disable-model-invocation: true
argument-hint: "<PR番号またはURL> [--max-cycles N] [--hold-action skip|fix] [--assign ROLE=AGENT[,AGENT]] [--template PATH] [--config PATH] [--fresh-review]"
---

# PR 修正ループ

この文書では、レビュー修正ループの 1 回分を「サイクル」、レビューを行う 1 モデル分を「レビュアー」と書く。

このスキルを起動したセッション（あなた）が司令塔である。

- **レビュアー**は、helper が agent-runner で並行に起動する（workflow `review` の role `inspect`）。各レビュアーは pr-review を非対話で 1 回実行する。
- **計画と修正**は、あなたが review-fix-planning の手順で自分で行う。採否・修正・push・状況コメント・最終化（マージと関連 Issue の更新）まで含む。実装・自己レビュー・修正の確認は、review-fix-planning が `run_roles.py` で設定の割り当てどおりに起動する。
- **helper**（`scripts/loop.py`）が、PR ごとのロック、PR の HEAD の照合、レビュアーの結果の検査、同じ commit の再レビューの省略、判断の記録の照合、資源の回収を行う。

## 守ること

- 利用者が明示的に起動したとき、または issue-to-pr が利用者の明示した `--merge` で作った PR に対して呼ぶとき（`.agents/skills/issue-to-pr/SKILL.md`）だけ使う。PR の URL や、通常のレビュー・修正の依頼から自動で始めない。
- helper は、成功・失敗とも標準出力に JSON を 1 つ書く（引数の書式の誤りだけは、終了コード 2 と標準エラー出力の usage）。`next` は次に実行する完全なコマンドである。コマンドを自分で組み立て直さない。`requires_input` があるときは、そこに書かれたオプションに、自分で確かめた事実を書き足してから実行する（`<...>` のような書きかけの値は拒否される）。`status` が `error` なら、`stage` と `reason` を読んで「止まったとき」に従う。
- ロックと記録（`<review_dir>/loop/`）を手で作成・編集・削除しない。
- `rejected` のレビュアーを「指摘なし」として扱わない。
- loop 自身は PR にコメントしない。PR への書き込みは、review-fix-planning の状況コメントと最終化、範囲外の不具合の記録のコメントだけである。ループの途中（レビュアーの起動、helper、CI の待機、最終化の再実行など）でこの PR の範囲外の不具合が判明したら、司令塔が `.agents/out-of-scope-defects.md` の記録のコメントを PR に 1 件足す。
- 新しい PR を作らない。force push、`main` への直接の push、admin マージをしない。

## 起動

利用者が渡した PR とオプションを、そのまま `start` に渡す。オプションを足したり、既定値を作ったりしない。issue-to-pr から呼ぶときは、issue-to-pr が検査した `merge.loop_args` を渡し、`TOOL_ROOT` は親の main worktree（`.agents/scripts/agent_tmp.py root` が出力するパス）にする。feature worktree の `loop.py` を使わない（PR で変更中のスキル・helper でレビューさせない）。

単独の明示起動では `TOOL_ROOT="$(git rev-parse --show-toplevel)"` を設定する。issue-to-pr からは代わりに `TOOL_ROOT="$PARENT"` とする（feature worktree の cwd から設定し直さない）。設定後、次を実行する。

```bash
uv run --project "$TOOL_ROOT/tools/devenv" --locked python \
  "$TOOL_ROOT/.agents/skills/pr-review-fix-loop/scripts/loop.py" start "<PR>" \
  --controller-model "<あなたの正確なモデル ID>" --controller-runtime "<ランタイム>" \
  <利用者が指定したオプション>
```

- `--controller-model` は、review-fix-planning が状況コメントに使う `MODEL_NAME` と同じ値にする。`--controller-runtime` は `claude-code`・`devin`・`pi`・`codex`・`cursor`・`grok-build` のどれか。
- `--max-cycles N`: サイクルの上限（1〜10、既定 3）。
- `--hold-action skip|fix`: 利用者が指定したときだけ渡す。新規起動で未指定なら helper が `skip` を適用する。明示した `skip`・`fix` はそのまま適用する。
- `--assign ROLE=AGENT[,AGENT]`（複数可）: `inspect`（レビュアー、複数可）と、`implement`・`self-review`・`fix-check`（各 1 つ。review-fix-planning の `run_roles.py` に渡す）。agent は agent-runner の設定に定義された名前。
  - writes=false の役割を読み取り専用で起動する担当（runtime が `codex` の担当）は、`inspect`・`self-review`・`fix-check` のどれにも、一次担当にも fallback にも割り当てられない（#52。理由は `tools/agent_runner/runtimes/codex.rst`）。`implement` には割り当てられる。
  - `start` は、`run_roles.py` が設定だけで拒否する fix の割り当て（一次担当が 2 つ以上、`self-review`・`fix-check` の `writes = true` など。個人設定で決まるものを含む）を、run を作る前に終了コード 2（`config`）で止める。`reason` を利用者に伝えて止まる。別の担当や `self` に自分で切り替えない。
- `--template`・`--config`: agent-runner の雛形と個人設定。既定は雛形 `.devcontainer/agent-runner.toml` と既存の個人設定。
- `--fresh-review`: 現在の HEAD のレビューの成果があっても、レビュアーを起動する。
- `check-options`: `start` と同じオプション（`--fresh-review` を除く）と agent-runner の設定だけを、同じ関数で検査する。PR・ロック・run・GitHub・Herdr に触れず記録を書かない。成功の `options`・`assignments` は `start` の `run.json` と同じ（`options` の `fresh_review` を除く）、`next` は null。issue-to-pr が `--merge` のとき worktree 前に呼ぶ。exit 2 なら `reason` を伝えて停止する。
- 対象は `usaturn` 配下の open な PR だけ（review-fix-planning と同じ）。
- 終了コード 3（`lock_held`）は、別の run がこの PR を使っていることを示す。`next`（`status`）で持ち主を確かめ、利用者に報告して止まる。古く見えても消さない。
- `warnings` に未 commit の変更があるときは、レビュアーが作業ツリーのスキルと helper（未 commit の変更を含む）を読むことを利用者に伝える。
- Herdr のペインの中（`HERDR_ENV=1` で `HERDR_PANE_ID` がある）で起動すると、`start` は run を始めた後で、現在のペインの名前を `PR#<番号>` に変える。結果は出力と `run.json` の `herdr_pane` にある。`herdr` を自分で呼ばない。名前を変えられなくても run は続き、理由が `warnings` に入るので、利用者に伝える。

## サイクル

1. **`cycle-begin`**: 返った `review.mode` を見る。
   - `run`: `next`（`review`）を実行する。
   - `reuse`: 現在の HEAD のレビューの成果が、すでにレビューディレクトリにある（前のサイクルの push 前の PR 全体レビューなど）。レビュアーを起動せずに 3 へ進む。
2. **`review`**: 全レビュアーが終わるまで同期で待つ（「監視」）。`accepted` と `rejected` を確かめる。`rejected` は失敗したレビュアーで、指摘 0 件ではない。`rejected_stage` は却下した段階（`runner`・`payload`・`binding`・`head`・`artifacts`）である。返った `next` が `review` のとき（再実行待ち。対象は `rerun.agents`）は、runner は成功したが結果を受け付けられなかったレビュアーを、同じサイクルで 1 回だけ起動し直す。次に何を実行するかは `next` だけで決める（`rerun` は再実行を終えた後の出力にも残る）。計画に進まず、その `next` を同じように同期で待って実行する。runner の失敗（期限切れ・認証待ち・起動失敗）は再実行しない。代わりに helper が、runner の段階で失敗したレビュアー（`rejected_stage: runner`）ごとに、同じ担当で短い確認（固定の小さな依頼。期限 300 秒）を 1 回だけ起動し、結果をそのレビュアーの `probe` に、失敗した試行の所要時間と診断の要約を `attempts` に記録する。確認はレビューではなく、`verdict` を変えず、失敗してもループは止まらない（`references/records.md` の 4 章）。保存の後に失敗した（後片付けの失敗、期限切れなど）レビュアーの成果は、このサイクルの HEAD のままレビューディレクトリに残ることがあり、review-fix-planning はそれも読む。状況コメントには、loop の判定（`rejected` と理由）と、そのレビュアーの成果を計画の入力にしたかを書く。出力の `retained`（回収で残した runner の出力と、レビュアーが報告した残存パス）と `outside_changes`（runner の監査が観測した、作業場所の外の変更）は、再実行の後の出力でも、同じサイクルの 1 回目と再実行を合わせた値である。`retained` のパスは最終報告に書く。終了コード 6 は全員の失敗である（「終わり方」）。
3. **計画**: review-fix-planning の SKILL.md を読み、`planning.review_dir`（レビューディレクトリの完全パス）を第 1 引数にして、その手順を最後まで行う。
   - `planning.hold_action` をこの run の Hold 方針として渡す。新規 run は未指定時の既定 `skip` または利用者の明示 `skip`・`fix` であり、既定の適用を利用者の明示選択と書かない。このループの既定 `skip` は Hold にだけ適用し、理由付き `Skip` として判断を残す（`Resolved` にしない）。必須修正や `Unverified` は、review-fix-planning の既存規則で扱う。
   - 旧 run の `planning.hold_action` が null なら未選択のまま扱い、Hold が残れば review-fix-planning の認可不足で `stopped` にする。単独の review-fix-planning の未指定時の規則は変えない。
   - `run_roles.py` の呼び出しには、`planning.run_roles_options` をそのまま足す。
   - 挙動の再現が要るときは、review-fix-planning 2 章の専用 worktree で確かめる。
   - 関連検証が失敗したら、review-fix-planning の規則どおり原因を診断して直す。loop は計画の結果を待ち、サイクルを終えない。
   - review-fix-planning は、状況コメント（判断の記録）を投稿して、`fixes_pushed`・`no_fix_needed`・`stopped` のどれかで終わる。
4. **`cycle-end`**: 次の引数を付けて呼ぶ。
   - `--role-run <dir>`: この計画で実行した `run_roles.py` の出力ディレクトリ（`--output-dir`）ごとに 1 つ。実際の割り当ての記録に使う。
   - `--finalize-result <file>` と `--round-dir <dir>`: 結果が `no_fix_needed` のとき。finalize.py の `--result-file` と、記録・本文・検証記録を置いたディレクトリ（`ROUND_DIR`）。
   - `--record-file <file>`: 状況コメントを投稿できなかったときだけ。投稿しようとした記録の JSON。
5. `next` が `cycle-begin` なら 1 へ戻る。`finish` なら「終わり方」へ進む。

`cycle-end` が返す主な拒否:

| 終了コード・`stage` | 意味と対応 |
| --- | --- |
| 4 `record_missing` | このサイクルの HEAD の判断の記録が、まだ PR に無い。計画を最後まで進めて投稿してから呼び直す。状態は変わらない |
| 4 `record_inconsistent` | 記録が前の判断と食い違う（引き継いだ判断の状態を根拠なく変えた、など）。review-fix-planning の規則で記録を直して投稿し直す |
| 4 `pushed_head_mismatch`・`merge_mismatch` | 記録と PR の実際の状態（HEAD・マージ）が合わない。実際の状態を確かめて利用者に報告する |
| 2 `arguments` | `no_fix_needed` なのに `--finalize-result` が無い、など。正しい引数で呼び直す |

## 終わり方

ループの結果は、最後のサイクルの結果である。

| 結果 | 意味 |
| --- | --- |
| `no_fix_needed` | 修正不要を確かめ、マージと関連 Issue の更新まで終えた |
| `stopped` | 権限・環境・外部の具体的な障害で止まった（`stop_reason`） |
| `fixes_pushed`（`ended_by: cycle_limit`） | 修正を push したが、サイクルの上限に達した。マージしていない |

- `next` の `finish` を実行する。ロックを解放し、要約を返す。要約の `retained`・`reviewer_retained`・`cycles[].outside_changes` は「最終報告」に使う。
- サイクルの途中で続けられなくなったとき（全レビュアーの失敗、計画を実行できない障害など）は、`finish --run <run_dir> --abort "<permission|environment|external>: <具体的な障害>"` で終える。
- `finish` の終了コード 7（`resources`）は、runner の試行の停止を確認できない資源があることを示す。ロックは保持される（「止まったとき」）。

## 止まったとき（中断・再開・回収）

- **再開**: `status --run <run_dir>`（PR しか分からなければ `status <PR>`）の `next` を実行する。状態は `<review_dir>/loop/` にだけあり、会話の記憶に頼らない。保存済みの `options.hold_action`（旧 run の null を含む）を維持し、新規起動の既定値で上書きしない。
- **`review` の終了コード 3（`interrupted_review`）**: 前のレビューが結果を残さずに止まった。レビュアーを起動し直さない。runner とレビュアーのプロセスが残っていないことを `ps` などで確かめ、その事実を `recover --run <run_dir> --operator-confirmed-stopped "<確かめた事実>"` に渡す。記録した資源だけを回収し、run は `abandoned` になる。続けるときは `start` からやり直す。出力の `retained` は回収で残したパスと、過去の `cleanup.retained` に記録したパス、`reviewer_retained` はレビュアーの pr-review が残したパス（「最終報告」と同じ扱い）である。
- **終了コード 7（`resources`）**: 同じく、停止を確かめてから `recover` に事実を渡す。確かめられないときは、ロックと資源を残したまま、完全パスを利用者に報告して止まる。
- **`review` の終了コード 4（`head_moved`）**: `cycle-begin` の後に PR の HEAD が動いた。`next` のとおり `finish --abort` で止め、新しい run を始める。
- **`review` の終了コード 5（`runner`）**: runner がレビュアーを起動できなかった。何も起動していなければ `next` は `review` で、原因（agent-runner の設定、入れ子の起動など）を直してからやり直す。出力が残っているときは `status` で状態を確かめる。
- **最終化の停止**: finalize.py が `stopped`（CI の完了待ち、mergeable の計算中、一時的な API の失敗など）で終わり、同じ HEAD のまま原因が解消したら、`cycle-end` に渡した `--round-dir` の記録・本文・検証記録で finalize.py を再実行する（`.agents/skills/review-fix-planning/references/execution.md` の 3 章。手動マージ済みでも Issue の更新だけを行う）。新しい commit が push されたら、新しい run を `start` から始める。
- 他の run のロック・資源・worktree を消さない。glob や `rm -rf` で推測して消さない。

## 監視

`review` は、全レビュアーの終了と停止の確認まで戻らない（役割の期限に停止の猶予を足した時間。雛形の `inspect` は 2700 秒（45 分））。runner の段階で失敗したレビュアーがいると、その後に短い確認（期限 300 秒）も待つ。`monitoring-long-tasks` に従う。

- 完了通知のあるランタイム（Claude Code の `run_in_background` など）は、`review` をバックグラウンドで実行し、終了コード付きの通知を待つ。
- 完了通知の無いランタイム（Pi など）は、前景で実行する。期限は runner が守る。
- stall は、`cycle-<n>.json` の `review.output_dir` の下のログの更新時刻で判定する。再実行の間（`review.rerun` があり、その `batch` が無い）は `review.rerun.output_dir` の下を見る（1 回目の出力は回収済み）。短い確認の間（`review.probe` か `review.rerun.probe` があり、その `ended_at` が無い）は、その `output_dir` の下を見る。10 分間隔の確認で 2 回続けて変わらなければ stall を疑い、プロセスを止めずに利用者へ報告する。
- 計画の工程はあなた自身の作業である。その中で起動する `run_roles.py` の待ち方は review-fix-planning に従う。

## 最終報告

- PR の URL、run ID、`<review_dir>/loop/<run_id>/` の完全パス
- 現在の Herdr ペインの名前を変えた結果（`run.json` の `herdr_pane` の `state`。`skipped`・`failed` は `reason` も）
- サイクルごとに: HEAD、レビューの `mode`、レビュアーごとの状態・実際の担当・モデル、判断の記録（round とコメントの URL）、結果、push した commit、`outside_changes`（runner の監査が観測した、作業場所の外の変更。1 回目と再実行を合わせた値）
- runner の段階で失敗したレビュアーごとに（`finish` の `cycles[].reviewers[]` の `rejected_stage` が `runner` のもの）: 試行の所要時間（`attempts[].elapsed_seconds`）、診断の要約（`attempts[].diagnosis` の `ending`・`code`・`status`・`message`・`steps`・`last_event`・`last_record_at`）、短い確認の結果（`probe` の `state`・`reason`・`after_failure_seconds`）。確認の読み方と限界は `references/records.md` の 4 章
- 最終の結果、`ended_by`、マージしたか、`stop_reason`
- 関連 Issue は、finalize の結果に含まれる、更新後に観測した state を書く。GitHub 側の自動 close を含め、Issue が閉じたことを要件が完了したことの証明として扱わない（要件の評価は review-fix-planning の記録に従う）
- loop が残した出力（`finish` の `retained`）の完全パスと、回収の操作（「止まったとき」の `recover`）。過去の `cleanup.retained` も記録の値を報告し、パスを消さない
- レビュアーの pr-review が残したパス（`finish` の `reviewer_retained`。レビュアーが報告したパス）。パスごとに、観測したサイクル・レビュアー・試行と、レビュアーの報告（`observed` の `source`）と、`finish` の時点の状態（`exists`・`registered`）を分けて書く。loop はこれを回収しない。司令塔も消さず、完全パスを利用者に報告する
- 所要時間（全体とサイクルごと）

## 記録

loop の記録の形式・置き場所・終了コードは [references/records.md](references/records.md) にある。記録は進行と参考データの索引で、レビュー・確認・判断の中身は既存の成果（レビューファイル、`findings/`、`verified/`、判断の記録）をパスと SHA-256 で参照する。

## ランタイムごとの違い

- **起動**: Claude Code は `/pr-review-fix-loop <PR> ...`、Pi は `/skill:pr-review-fix-loop <PR> ...`。そのほかのランタイムは、このスキル（`.agents/skills/pr-review-fix-loop`）を明示して呼ぶ。
- **`disable-model-invocation: true`**: Claude Code がモデルの判断でこのスキルを起動しないようにする設定。ほかのランタイムでも、本文の「明示的に起動したときだけ使う」を守る。issue-to-pr が `--merge` で呼ぶときは、Skill ツールで起動できないので、親の main worktree のこのファイルを読んで従う。
- **`--controller-model`**: Pi は pi-model-name で `PI_PROVIDER` と `PI_MODEL` を確かめる。ほかのランタイムも、現在提示されている正確なモデル ID を使い、設定や過去の記録から推測しない。
- **監視の手段**: 上の「監視」と `.agents/monitoring-runtimes.md`。
