---
name: issue-to-pr
argument-hint: "<Issue> [--implement AGENT] [--merge [--max-cycles N] [--hold-action skip|fix] [--assign ROLE=AGENT[,AGENT]]... [--template PATH] [--config PATH]]"
description: "Issue の URL・#番号を指定して『着手して』『実装して PR まで』『推奨案で進めて』と依頼されたときに使う。PR の番号・URL、PR レビュー・レビュー指摘の修正計画、ボードの親 Issue からの子の選択には使わない。"
---

# Issue to PR

## 1. 入口と委任の範囲

このスキルによる着手を、**通常の設計判断は推奨案で決め、検証・commit・PR 作成まで継続する指示**として扱う。質問、設計・Spec・Plan の確認、実行方式、終了方法の選択は追加で尋ねない。AGENTS.md の曖昧な指示への確認は、ここでは仕様の意味を変える判断・安全境界・後述の停止条件に適用する。実在しない承認や検証結果を記録しない。

- **Claude Code など:** `superpowers:using-superpowers` を読み、以下の流れを開始する。
- **Pi:** 自動 bootstrap や設定を変更しない。明示 `/sp-brainstorm` から開始し、このスキルを使う。明示 `/skill:issue-to-pr` から読み込まれた場合も opt-in として、`brainstorming` を直接読んで開始できる。
- Pi で自然言語の依頼しかなく、明示 `/sp-*`・`/skill:*` 限定の指示がある場合は、読み取りだけで入力を解決し、次の入口を案内して終了する。モデルがスラッシュコマンドを実行したふりをしない。

```text
/sp-brainstorm <解決した Issue URL> issue-to-pr を使い、推奨案で進めて途中で止めずに PR まで作成せよ
```

一度明示入口から開始した run では、工程ごとに次のコマンド入力を求めず、各スキルを読んで最後まで続ける。ランタイムの明示された承認・ツール制限は迂回しない。

### `--merge`

`--merge` は、新規 PR を作った後にレビュー修正ループ（pr-review-fix-loop）を回し、修正不要と確かめられたらマージと関連 Issue の更新まで進める指示である。

- 有効なのは、**利用者のメッセージ（スラッシュコマンドの引数を含む）に `--merge` という語が書かれているときだけ**。自然文の「マージまで」「マージして」は `--merge` として扱わない。モデルが自分で補わない。
- ループ用オプション（`--max-cycles`・`--hold-action`・`--assign`・`--template`・`--config`）は、ループの `start` と名前・値・意味が同じで、`--merge` と一緒のときだけ受け付ける。**利用者が書いたものだけを、書いた順に渡す。**既定値を足さない（上限 3 サイクル、Hold は `skip`、担当は agent-runner の設定どおり）。
- 統合用の branch に向けるボードの子では、worktree の作成前に 2 章の 5 で `--merge` の不適用を決め、理由を伝える。以降は `--merge` が無い run として続ける。
- `--merge` が無ければ PR の作成（親は CI green、制作物は CI があれば green の確認）で終わる。ループもマージもしない。最後に `/pr-review-fix-loop <PR>`（Pi は `/skill:pr-review-fix-loop <PR>`）で続けられると 1 行で伝える。統合用の branch 宛てでは案内しない（ループと `finalize.py` は既定 branch へのマージを前提にする）。

### `--implement`

Plan の Task を実装する担当は、agent-runner の `workflows.issue` の `implement` で決まる（雛形 `.devcontainer/agent-runner.toml` < 個人設定 < `--implement`）。雛形の既定は `self`（このセッションが自分で実装する）。

- `--implement AGENT` は、その回だけ一次担当を置き換える。値は agent-runner の設定にある agent 名か `self`。`fallback` は設定のものを保つ
- `--merge` の有無と関係なく使え、書く位置は問わない。`--merge` の後ろに書いてもループ用オプションにならない
- ループ用オプションの `--assign implement=...` はレビュー修正の担当（`workflows.fix` の `implement`）であり、Plan の実装の担当ではない。ループ用オプションの `--template`・`--config` も Plan の実装の担当の決定には使わない

## 2. 入力・所有者・競合の確認

1. **必須:** `repository-ownership` と作業対象に適用される AGENTS.md を読む。`repository-ownership` の判定（`route_target.py`）を実行する。Issue の内容による担当と入力先が一致しなければ停止し、別 repo の同番号を推測しない。
2. スキルの実ディレクトリを `SKILL_DIR`、親 main worktree を `PARENT` とする（`$XRIFT_POC_ROOT`。無ければ親 checkout の `.agents/scripts/agent_tmp.py root`）。現在の checkout を cwd として、読み取り専用 helper を実行する。`INPUT` は、**利用者の引数の先頭から、Issue の参照とオプション（`--implement` とその値、`--merge`、ループ用オプションとその値）のうち最後のものまでを、書かれたまま切り出した文字列**。後ろの自然文（改行・非 ASCII を含む「推奨案で進めて」など）は含めない。1 つの文字列として渡し、語を足したり並べ替えたりしない。最後の Issue・オプションより前の自然文は取り除かず、helper の exit 2 で止まる。例: `#12 --merge --max-cycles 2 推奨案で進めて` の `INPUT` は `#12 --merge --max-cycles 2`。

   ```bash
   uv run --project "$PARENT/tools/devenv" --locked python "$SKILL_DIR/scripts/resolve_issue.py" -- "$INPUT"
   ```

   - URL: `https://github.com/usaturn/xrift-poc/issues/N`、または現在の場所を `product` と判定した制作物の Issue の URL。
   - `#N`: 現在の場所の判定（`env` なら `usaturn/xrift-poc`、`product` ならその `repository`）で解決する。`stop` や `repository` が null なら停止する。
   - 成功: exit 0、`repo/number/url/title/body/merge/implement` の JSON。`merge` は未指定なら `null`、指定ありなら `{"loop_args": [...]}`（`start` に足す引数列。パスは絶対パス）。`implement` は `--implement` の値か `null`。最初に `repo` と `title` を利用者に表示する。失敗: exit 2、stderr（`--merge` なしのループ用オプション、未知のオプション、`--merge`・`--implement` の重複、値の無い `--implement`、`--fresh-review` を含む）。失敗時は副作用に進まない。
   - `gh issue view` の成功だけでは Issue と証明できない。helper は返された URL の `/pull/`・不一致・closed を拒否する。
   - `merge` が `null` でなければ、**親の main worktree の** `loop.py` で事前検査する。`loop_args` の各要素を順に、それぞれ 1 引数として渡す。

     ```bash
     uv run --project "$PARENT/tools/devenv" --locked python \
       "$PARENT/.agents/skills/pr-review-fix-loop/scripts/loop.py" check-options <loop_args の各要素>
     ```

     空なら引数なしで既定の雛形・既存の個人設定を検査する。exit 0 の `options`・`assignments` を Spec の決定記録へ写す。exit 2 は `reason`（argparse は stderr）を伝えて停止する。worktree・Herdr・claim に触れる前に行う。
   - `--merge` の有無と関係なく、続けて Plan の実装の担当を検査する。`implement` が `null` でなければ `--implement <implement の値>` を付け、`null` なら付けない。

     ```bash
     uv run --project "$PARENT/tools/devenv" --locked python "$SKILL_DIR/scripts/implement_task.py" check [--implement <値>]
     ```

     exit 0 なら、出力の `assignment`（`agents`・`fallback`）と `self` を Spec の決定記録に写す。exit 2 なら `reason` を利用者に伝えて停止する（設定に無い agent、`agents` が 2 つ以上、`implement` の役割の `writes = false` など）。worktree・Herdr・claim に触れる前に行う。
   - 成功 JSON の `repo` から `repository-ownership` で **TARGET_ROOT（対象 repository の checkout）** を確定する。以降の ref・branch・worktree・fetch 検査は `git -C "$TARGET_ROOT" ...` で行い、起動元の checkout の一覧で別のリポジトリを検査したことにしない。
3. 本文、全ページのコメント、依存関係を読む。Issue 本文が利用者の承認・判断を求めていれば、推奨案での委任では代用せず停止する。
4. **worktree 作成より前に**次を検査する。どれかあれば、新規作業を作らず状態を報告する。古い claim も横取りしない。`--merge` があっても既存 PR をループに乗せない（再開は利用者の `/pr-review-fix-loop <PR>`）。
   - 同じ Issue に対応する open PR（linked Issue、本文、branch を確認。最初のページだけで判定しない）
   - 他セッションの `refs/heads/agent-claims/issue-N`、コメントに記録された未解放の担当
   - 対象 repository の既存 worktree、ローカル・remote branch（`issue-N` は番号の境界で照合し、N=58 と N=580 を混同しない）
   - 未完了の先行 Issue・必要な未マージ PR
   worktree 作成直前と、編集・push・PR 作成前に最新の Issue 状態と競合を再確認する。新規競合や closed 化があれば停止し、自分がこの run で作った worktree/branch と他者の作業を区別する。
5. `<!-- planning-parallel-issues board:... -->` がある子は **`planning-parallel-issues` 必須**。claim・先行条件・成果公開・所有権の再確認・解放はそちらに従う。このスキルにプロトコルを写さない。親からの子の選択は担当外。`--merge` の claim 解放の時点は 5 章。
   - **PR の向き先（`BASE`）:** 親 Issue（本文・コメント）か子の handoff が統合用の branch を指定していれば、その branch を使う。指定がなければ対象 repository の既定 branch（親は `main`、制作物は `repository-ownership` で確認した既定 branch）にする。ボード外の Issue も同じ既定値を使う。
   - **統合用の branch と `--merge`:** `BASE` が統合用の branch なら、この時点で `--merge` を適用しないと決め、理由（ループと `finalize.py` は既定 branch へのマージと Issue の close を前提にする）を利用者へ伝えて続ける。入力し直しは求めない。以降は `--merge` が無い run とし、ループ用オプションを使わず、最終全体レビューと CI の確認を残して PR の作成で終える。

`gh issue/pr` には毎回 `--repo "$REPO"` を付ける。`gh api` は `--repo` を受け付けないため、`repos/<owner>/<repo>/...` の完全な endpoint で対象を固定し、一覧は `--paginate` で取得する。helper は入力検証のみで、競合・承認・claim の確認済み証拠にはならない。

## 3. Herdr と作業場所

着手条件がそろったら、`HERDR_ENV=1` の場合に `herdr-agent-task` と `herdr` を読む。**現在の pane** をリネームし、担当タスクを設定する。他 pane や focus を変更しない。

```bash
herdr pane rename "${HERDR_PANE_ID:?}" "Issue#${NUMBER:?}"
herdr-agent-task set "Issue #$NUMBER 実装・PR作成"
```

現在 pane が特定できない・rename が失敗した場合は、別 pane を推測せず停止する。

**必須:** `superpowers:using-git-worktrees` と `repository-ownership` に従い、status・worktree 一覧・ignore を確認して、**対象 repository の最新の既定ブランチ（親は `origin/main`）** から新しい worktree を作る。名前は `issue-N-<要約>`、branch は `feat/issue-N-<要約>` または `fix/issue-N-<要約>`。URL と #番号で作成方法を分けない。制作物の worktree は制作物のディレクトリの中に作る。親の detached HEAD 直下では編集しない。

`BASE`（2 章の 5）が統合用の branch なら、worktree を作った直後（Spec・Plan・実装・テストの前）に対象 repository で `git fetch origin <BASE>` を実行し、`origin/<BASE>` を feature branch に merge する。競合などで取り込めなければ停止して報告する。

## 4. 推奨案 → Spec → Plan → 実装

1. **`superpowers:brainstorming` 必須。** 意図・受け入れ条件・編集禁止を整理し、推奨案を選ぶ。Spec は規模にかかわらず、親 main worktree の `docs_draft/superpowers/` に Markdown で保存する。`docs/superpowers/` に置かず、commit しない。
2. Spec に **決定記録** を設け、各項目に「選択した案 / 検討した別案 / 選択理由 / 委任指示または停止条件」を残す。依頼にある質問・承認要求と通常の設計確認を区別する。`--merge` の有無と、渡すループ用オプション（`check-options` の `options`）、Plan の実装の担当（`implement_task.py check` の `assignment`。`--implement` で変えたか）も書く。既定値（Hold の `skip`、雛形の `self` など）は既定の適用として書き、利用者の指定と書かない。
3. **`superpowers:writing-plans` 必須。** 同じ保存先に Plan を作り、実在する Spec パス、Task、担当ファイル、検証入口、実行方式・理由、contingency を記録する。AGENTS.md の SDD 実行運用に従う。`--merge` のときは、実行予算にループの段階を含める（目安はサイクルの上限 ×（レビュー最大 45 分＋計画・修正））。
4. 実行方式は次で決める。ランタイムのツール仕様を確認し、存在しない一覧操作や同期フラグを渡さない。リポジトリが要求する起動条件を満たせなければ inline を選ぶ。

   | 条件 | 選ぶ方式 |
   |---|---|
   | `implement_task.py check` の `self` が false | `superpowers:subagent-driven-development` の形で各 Task の実装を runner の担当に依頼する（下の「Task の実装を runner の担当に依頼する」） |
   | `self` が true で、Task が独立し、文脈を子に渡しきれ、同期委譲を使える | `superpowers:subagent-driven-development` |
   | `self` が true で、同じファイルの逐次変更、1–2 Task、委譲不可 | `superpowers:executing-plans` |

   Pi の `/sp-implement` は直列実行、`/sp-implement-parallel` は依存を満たした独立 Task を別 worktree で並列実行する入口。SDD なら常に parallel という対応ではない。同じファイルを同時に書かせない。run 中は選んだスキルで続け、コマンド再入力待ちにしない。
5. 選んだスキルの実装・TDD・レビューに従う。スキルを作る場合は `superpowers:writing-skills` の無し/ありの振る舞い比較を行う。固定文言の検査テストで代用しない。検証の失敗は `systematic-debugging` で調べ、解消できなければ停止する。テストの要否は AGENTS.md の「テストの採用基準」で判断し、守る挙動が無い変更に文言の照合テストを追加しない。

6. **`--merge` のときは、実装スキルの最終の全体レビュー（全 Task 後のブランチ全体レビュー）だけをループの 1 サイクル目で代える。**Task ごとのレビューは残す。最終レビューへ回す予定の minor（ledger の `deferred`）は PR 本文に一覧で書く。`--merge` が無ければ最終全体レビューを省かない。

### Task の実装を runner の担当に依頼する（`self` 以外のとき）

`superpowers:subagent-driven-development` を読み、Task を実装するサブエージェントの代わりに、`implement_task.py` で runner の担当に Task を 1 つずつ順に依頼する（並行しない）。runner の担当は入れ子に agent を起動できないので、Plan 全体を 1 回で渡さない。

1. **出力先:** 親の main worktree で `agent_tmp.py mkdir issue-<N>-implement` のセッションディレクトリを作り、依頼ごとにその下の存在しないパス（例: `<SESSION>/task-2-attempt-1`）を `RUN_DIR` にする。`/tmp` の下に置かない（DSH の bwrap のテストが失敗する。ログを後で参照する）
2. **依頼の JSON（`REQUEST_JSON`）:** 次の 6 項目だけを書く。
   - `repository`: feature worktree の root の絶対パス。`revision`: その HEAD の完全な SHA（未 commit の変更を残さない）
   - `task`: 1 行の Task 名。`allowed_files`: Plan のその Task の担当ファイル（root からの相対パス。`/` で終わればディレクトリ）
   - `commit_message`: Plan の commit の手順のメッセージ（担当が commit できなかったときに helper が使う）
   - `prompt`: 担当がそれだけで作業できる依頼。Plan のその Task の全文、Spec の関係する節、受け入れ基準（Task をレビューする担当と同じもの）、実行する関連テストと静的チェック、テストの要否と理由。担当がどのスキルを読めるかはランタイムで違うので、スキルを前提にしない。作業範囲・禁止事項・返す JSON の形は helper が足す
3. **依頼:** 同期で終わるまで待つ（`implement` の役割の期限は 90 分。`monitoring-long-tasks` で監視する）。

   ```bash
   uv run --project "$PARENT/tools/devenv" --locked python "$SKILL_DIR/scripts/implement_task.py" run \
     --request "$REQUEST_JSON" --output-dir "$RUN_DIR" [--implement <値>]
   ```

   出力の `verdict` で次を決める。どの `verdict` でも、次の依頼を出す前・止まる前に、その依頼の `RUN_DIR` を 5 のとおり片付ける。

   | `verdict` | 次の操作 |
   | --- | --- |
   | `importable` | `implement_task.py accept --run-dir "$RUN_DIR"`。判定を今の状態でやり直し、担当が commit しなかったとき（`helper_commit`）は helper が `commit_message` で commit してから、feature worktree に `--ff-only` で取り込む |
   | `no_changes` | 取り込むものは無い。Task のレビューで、変更が要らないことを確かめる |
   | `retry`（担当が `BLOCKED`・`NEEDS_CONTEXT`） | `result.concerns`・`result.incomplete` に答える文脈を `prompt` に足し、同じ担当に新しい依頼をする（新しい `RUN_DIR`）。回数は Plan の「実行予算」の再試行の上限まで。上限に達したら止まる |
   | `rejected`（`outside_changes`、取り込みの前の確認に反した） | 取り込まない。再試行の上限の中で依頼し直すか止まるかを決め、`reasons` と理由を ledger に書く |
   | `failed`（停止を確認できない、期限切れ・認証待ち・起動の失敗、`fallback` が尽きた。結果の形の誤り（`TaskResult` に合わない）は runner が `fallback` の次の候補を試し、候補が無いか尽きたらここに入る） | その Task で止まる |

4. **確認とレビュー:** 取り込んだ後、feature worktree で関連テストと静的チェックを実行し、SDD の Task のレビューを行う。指摘の修正も同じ担当に新しい依頼として出す（新しい `RUN_DIR`。`revision` は取り込んだ後の HEAD）
5. **依頼ごとの記録と後片付け:** 依頼し直し（`retry`・`rejected`）も指摘の修正も、それぞれ 1 つの依頼として数える。1 つの依頼の `verdict` を処理し終えるたびに、その依頼の `RUN_DIR` について次の順に行う。依頼し直すときも、前の依頼の `RUN_DIR` を片付けてから新しい依頼を出す（Task の最後にまとめると、止まる経路で前の依頼の runner の worktree の登録が残る）
   1. ledger に、Task 名と何回目の依頼か、`requested_agent`・`actual_agent`・`identity`（runner が観測したモデル）、`elapsed_seconds`、`verdict`（`accept` を実行したらその結果も）を書く。`cleanup` は `RUN_DIR`（`task.json` とログを含む）を消すので、先に書く
   2. `implement_task.py cleanup --run-dir "$RUN_DIR"` を実行する。時点は、`importable` は `accept` の後（取り込めなかった場合も）、`no_changes`・`retry`・`rejected` は ledger に書いた直後、止まるとき（`failed`、上限に達した `retry`・`rejected`）は停止の報告の前。停止を確認した試行の worktree の登録を外し（force しない）、何も残らなければ `RUN_DIR` を消す
   3. 終了コード 1 は、残したもの（`retained`。停止を確認できない試行、`sensitive_paths` のある試行など）があることを示す。それだけでは止まらない。完全パスを ledger に書き、最終報告（止まるときは停止の報告）に含める。force・`rm -rf` で消さない

   `run` が終了コード 2（`status: error`）で終わったときは `task.json` が無く、`cleanup` を使えない。`RUN_DIR` が存在すれば、その完全パスを停止の報告に書く。セッションディレクトリは、すべての依頼の `cleanup` の後に空になったときだけ `rmdir` で消す。中身が残っていれば消さずに完全パスを報告する（`rm -rf` で消すと runner の worktree の登録だけが残り、`implement_task.py run`・`run_roles.py`・`dispatch.py` が起動の前の監査で止まる）

失敗を理由に、このセッションが `self` で実装を引き取らない（agent-runner の「失敗を理由に別のモデルへ暗黙に切り替えない」）。止まった Task の後の Task・最後の全体レビュー・PR には進まない。担当を変えるかは利用者が決める。すべての Task を取り込めたときの最後の全体レビューと PR は、`self` のときと同じ。

## 5. 検証・PR・完了

- **`verification-before-completion` と `finishing-a-development-branch` 必須。** 終了方法は PR 作成を選び、改めて選択肢を尋ねない。作業対象 root と remote を確認し、AGENTS.md の正式入口で検証する。
  - 親: `./scripts/ci/verify-devenv.sh`
  - 制作物: `package.json` の検証スクリプトと `xrift check <world|item> --build`
- `--merge` のときは commit 後に正式入口で検証し、検証した HEAD、そのときの `git status --porcelain` が空だったこと、終了コード、親の `tmp/agents/` に残したログの完全パスを記録する。ループで再利用する条件になる。
- 長時間の検証・子・CI は `monitoring-long-tasks` 必須。同期の終了を待ち、終了コードと実ログを確認する。
- worktree の Git root で明示パスのみ stage・commit し、**repository-ownership「公開前の秘密検査」の `scan before push` と `scanned push` の2ブロックを同じシェルで実行する**。ここでは引数なしの `scan_before_push` を使い、検査した SHA だけを自分の feature branch へ push する（関数本文を複製しない）。検査の失敗・空の SHA では push しない。`gh pr create --repo "$REPO" --base "$BASE" --head <feature branch>`（`BASE` は2章の5）で PR を作る。SHA を送り元にした push は上流 branch を設定しないので `--head` を明示する。親では `items/`・`worlds/` を stage しない。既存 PR が見つかったら重複作成しない。`--merge` のときは draft にしない（`finalize.py` は draft を拒否する）。
- PR 本文: 同 repo の `Closes #N`、変更概要、**Spec の決定と理由、実行方式と理由**、実行した検証・結果、作業時間と主要 Task 別内訳。Plan の実装の担当が `self` 以外なら、割り当て（設定・`--implement`）、runner が観測した agent・モデル、Task ごとの依頼の回数と所要時間も書く。未実行の確認を成功と書かない。
- `--merge` が無いとき、親の PR は **CI green を確認してから完了**。制作物の PR は CI があれば green を確認する。`--merge` のときは PR の作成直後、CI を待たずにループへ進む。マージ前には `finalize.py` がどの repository でも CI green を確認し、実行中なら `wait_for: ci` で待ち、CI の無い制作物では停止してマージしない。`--merge` の `finalize.py` によるマージ以外に、マージ・タグ・リリース・デプロイ・プロビジョニングは行わない。admin マージ・auto-merge・force push・branch 削除もしない（自身の claim の解放は別途その手順に従う）。
- ボード子の成果公開・claim 解放は `planning-parallel-issues` に従う。自身の所有物だけを扱う。`--merge` のときは、ループの `finish` 後に結果を状況コメントへ書いてから解放する。
- 完了時は Herdr task を completed にする。PR URL、検証結果、worktree/branch、実在する Spec/Plan・残存ログ（runner の出力先と残したパスを含む）の絶対パス、所要時間を報告する。

### `--merge` のとき: PR の作成後のループ

1. **始めない場合:** 2 章の 5 で不適用と決めた統合用の branch の子は、この節を使わず PR の作成で終える。ここで改めて判断しない。
2. Herdr のタスクを `herdr-agent-task set "PR #$PR_NUMBER レビュー・マージ"` に変える。ペインの名前は `start` が `PR#<番号>` に変えるので自分で rename しない。
3. **親の main worktree の** `.agents/skills/pr-review-fix-loop/SKILL.md` をファイルとして読み、このセッションを司令塔として従う。
4. **親の main worktree の** `loop.py` で `start` する。feature worktree の helper は使わない（`tool_root` から読むスキル・helper が PR で変更中のものになってしまう）。

   ```bash
   uv run --project "$PARENT/tools/devenv" --locked python \
     "$PARENT/.agents/skills/pr-review-fix-loop/scripts/loop.py" start "$PR_URL" \
     --controller-model "<あなたの正確なモデル ID>" --controller-runtime "<ランタイム>" \
     <loop_args の各要素>
   ```

5. サイクルは pr-review-fix-loop の「サイクル」「終わり方」「止まったとき」「監視」に従う。
6. **検証記録の再利用:** 初回で修正不要、HEAD が PR 作成時から不変なら、PR 前の正式検証を `finalize.py --checks-file` に使ってよい。同じ HEAD、検証時に未 commit の変更なし、exit 0、親の `tmp/agents/` にログあり、のすべてを確認する。確認できなければループ内で再実行する。
7. **終了:** run ID、`<review_dir>/loop/<run_id>/` の完全パス、サイクルごとの結果、ループ段階の所要時間を報告に加える。

   | ループの結果 | 報告 | マージ |
   | --- | --- | --- |
   | `no_fix_needed` | 完了。関連 Issue は finalize の結果にある更新後に観測した state を書く | した |
   | `fixes_pushed`（`ended_by: cycle_limit`） | 未完了。修正は push 済みだが上限到達。続行は `/pr-review-fix-loop <PR>` | していない |
   | `stopped` | 停止、`stop_reason` を書く。`finalize.py` が `merged: true` ならマージ済みと分けて書く | 結果のとおり |
   | エラー（`lock_held`、全レビュアー失敗、停止未確認など） | 停止。`stage`・`reason`・`next` と残した資源の完全パスを書く | していない |

## 停止時の報告

不正入力（`resolve_issue.py`・`check-options`・`implement_task.py check` の exit 2 を含む）、Task の依頼の `failed`、再試行の上限に達した `retry`・`rejected`、ループの停止・エラー、route 確認失敗、競合、未完了依存、Issue の明示承認要求、禁止・取り消せない操作、想定外の外部送信、必要なツール/API の失敗、解消できない検証失敗では停止する。停止条件は「途中で止めずに」の例外であり、省略・成功扱い・別 repo への切替で回避しない。

**理由 / 現在の状態 / 実施済み検証と終了コード / 存在する worktree・branch・Spec・Plan・ログ / 次の操作** を報告する。Task を runner の担当に依頼した run では、`cleanup` が残したもの（`retained`）と、`cleanup` を使えなかった `RUN_DIR` の完全パスも書く。ループで止まったときは `stage`・`reason`・`next`・run のディレクトリ・残した資源の完全パスも書く。作っていないパスは「未作成」と書く。通常の工程区切り、推奨案の選択、実装方式の選択は停止理由にしない。

## ランタイムごとの違い

- **Claude Code:** `/issue-to-pr <Issue> [--implement AGENT] [--merge ...]`。`ARGUMENTS` から 2 章の規則で `INPUT` を切り出す。モデルが Skill ツールで呼ぶときは `--merge` を補わない。pr-review-fix-loop は `disable-model-invocation: true` なので、5 章のとおりファイルとして読む。
- **Pi:** `/skill:issue-to-pr` の引数から同じ規則で切り出す。`/sp-brainstorm <Issue URL> [--implement AGENT] [--merge [ループ用オプション]] issue-to-pr を使い、推奨案で進めて途中で止めずに PR まで作成せよ` も同じ。利用者が書いた `--implement`・`--merge`・ループ用オプションだけを使い、1 章の入口を案内するときもそれだけを含める。
- **controller の識別:** `--controller-runtime` は Claude Code なら `claude-code`、Pi なら `pi`（他はループの「起動」）。`--controller-model` はループの「ランタイムごとの違い」に従う。Pi は pi-model-name で `PI_PROVIDER` と `PI_MODEL` を確認する。
