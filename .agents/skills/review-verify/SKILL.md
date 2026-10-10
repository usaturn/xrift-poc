---
name: review-verify
description: >-
  レビューディレクトリ（親の `reviews/<owner>/<repo>/#<PR>-<ブランチ>/`。パス・PR 番号・PR の URL で指定）にある全レビュアーの候補（`findings/` の候補一覧、無ければ `<model>.md`）を、現在の HEAD のコミット済みの内容と隔離した worktree で確かめてまとめ、確認済みの一覧（`verified/<HEAD>.json`）を作るときに使う。review-fix-planning が計画の前に呼ぶほか、「レビュー結果が本当か確かめて」「指摘を再現して確認済みの一覧を作って」で使う。push 前の修正の確認（`mode: fix_check`）にも使う。採否は決めない
allowed-tools: Read, Grep, Glob, Bash(git *), Bash(gh *), Bash(mktemp *), Bash(rm *), Bash(test *), Bash(mv *), Bash(sha256sum *), Bash(date *), Bash(tar *), Bash(uv run --no-project python *review_findings.py *), Bash(uv run *), Write(reviews/*/*/*/verified/**), Edit(reviews/*/*/*/verified/**), Write(reviews/*/verified/**), Edit(reviews/*/verified/**), Write(public/reviews/*/verified/**), Edit(public/reviews/*/verified/**), Write(//tmp/review-fix-planning.*/**), Edit(//tmp/review-fix-planning.*/**)
---

# レビュー候補の確認（review-verify）

レビューの 3 つの工程のうち、2 番目の「確かめてまとめる」を行う（Issue #547）。

| 工程 | 担当 | 出すもの |
| --- | --- | --- |
| 探す | pr-review（レビュアーごと） | レビューファイル `<model>.md` と候補一覧 `findings/<model>-<HEAD>.json` |
| **確かめてまとめる** | **review-verify（このスキル）** | **確認済みの一覧 `verified/<HEAD>.json` と要約** |
| 決めて直す | review-fix-planning | Spec、修正、判断の記録 |

目的は、1 回目のサイクルのうちに、指摘を再現で確かめ、同じ種類の別の入力と、条件を述べている文書まで確かめ切ることである。後のサイクルで同じ種類の不具合が見つかるのを防ぐ。

この文書では、レビュー修正ループの 1 回分を「サイクル」、レビューを行う 1 モデル分を「レビュアー」と書く。

## 入口

- 引数はレビューディレクトリのパス、PR 番号（`#<PR>`。親リポジトリの `origin` の PR）、PR の URL のどれか。[review-fix-planning](../review-fix-planning/SKILL.md) の「Resolving the review directory」と同じ規則で、親の checkout から次で決める。

  ```bash
  TOPLEVEL="$(git rev-parse --show-toplevel)"
  TARGET_JSON=$(uv run --no-project python "$TOPLEVEL/.agents/scripts/review_target.py" review-dir "<引数>" --for-planning)
  ```

  - 番号・URL は、PR のリポジトリの `<親の main worktree>/reviews/<owner>/<repo>/` の直下で `#<PR>-` で始まるディレクトリだけを見る。別のリポジトリのディレクトリを横断しない。旧配置（`reviews/#<PR>-*`・`public/reviews/#<PR>-*`）は、明示したパスでだけ使う。
  - 終了コードが 0 以外なら、理由を報告して止まる。usaturn 以外のオーナーの PR（終了コード 10）は、保存済みレビューも読まずに「対象外」と報告して止まる。保存済みレビューが無い（8）、候補が複数（9。候補の完全パスを示してパス指定を求める）、パスから PR を決められない（11）、PR が無い・確認できない（3〜7）を区別して報告する。
  - JSON から `REVIEW_DIR`（`.review_dir`）、`PR_REPO`（`.repository`）、`PR_NUMBER`（`.number`）、`PR_URL`（`.url`）、Git 操作の対象 `TARGET_REPO_ROOT`（`.target_root`）を取り出す。`target_root` が `null`（親・登録済み submodule の checkout が無い usaturn のリポジトリ）なら、再現の節で一時 clone を作って `TARGET_REPO_ROOT` にする。ディレクトリ名からリポジトリを自分で推測しない。
- モードは 2 つ。
  - `cycle`（既定）: サイクルの確認。PR の現在の head をレビューした全レビュアーの候補を確かめる。review-fix-planning が計画の前に呼ぶ。単独でも使える。
  - `fix_check`: 修正をローカルで commit した後、push 前に行う。呼び出し側が `repository`（from/to のある対象checkoutの絶対パス）、`from`/`to`（完全SHA）、`fix_ids`、`fix_sections`（SpecのFix節全文。採用する変更・検証を含む）、`verified_file`（確認済み一覧の絶対パス）、`review_dir`（保存先の絶対パス）をJSONで渡す。未コミットの内容や、この入力に無い計画を推測しない。`to` があるcheckoutを `TARGET_REPO_ROOT` にする。
- スキルの補助スクリプトは親の toplevel（`$TOPLEVEL`）から使い、`$TARGET_REPO_ROOT` からは使わない。

## 守ること

- **読み取り専用**。PR へのコメント、push、commit、判断の記録の投稿、レビューファイルと候補一覧の書き換えをしない。書くのは、レビューディレクトリの `verified/` の下と、自分の一時ディレクトリだけ。
- **重大度の再評価、採否（Fix・Skip など）、修正の設計を書かない。** それは review-fix-planning の役割である。推奨の修正が正しいかも判定しない（確かめた事実だけを書く）。
- 根拠は、確かめる HEAD のコミット済みの内容（`git show <HEAD>:<path>`）と、隔離した worktree での実行だけにする。**作業ツリーのファイルを根拠にしない。** 作業ツリーには誰かの未コミットの変更が残っていることがあり、「すでに直っている」という誤った判定を生む（実際に起きた）。
- コマンドの失敗、環境の不足、時間切れ、読めなかったコードは、**`not_reproduced` にしない。** `unverified` にし、足りない確認を `missing_checks` に書く。
- 指摘したレビュアーの数を、正しさや重大度の根拠にしない。
- 秘密の値（トークン、鍵、資格情報）を、一覧・要約・ログに書かない。
- 他の worktree やブランチの未コミットの変更を読みに行かない。

## 隔離した worktree での再現

挙動についての主張は、確かめる HEAD（`cycle` では PR の head、`fix_check` では `to`）の隔離した worktree で再現する。worktree のパスと HEAD は別の引数にする。

- `TARGET_REPO_ROOT` が `null` のときは、下の `REPRO_ROOT=...` の行の直後（`worktree add` の前）に一時 clone を作り、それを `TARGET_REPO_ROOT` にする（PR の head は `refs/pr-review/<PR>` に固定され、`origin/<baseRefName>` もある）。後片付けの `rm -rf` で一緒に消える。

  ```bash
  uv run --no-project python "$TOPLEVEL/.agents/scripts/review_target.py" clone --target "$TARGET_JSON" --dest "$REPRO_ROOT/repo"
  TARGET_REPO_ROOT="$REPRO_ROOT/repo"
  ```

```bash
REPRO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/review-fix-planning.XXXXXX")"
WT="$REPRO_ROOT/worktree"
git -C "$TARGET_REPO_ROOT" worktree add --detach "$WT" "$VERIFY_HEAD"
test "$(git -C "$WT" rev-parse HEAD)" = "$(git -C "$TARGET_REPO_ROOT" rev-parse "$VERIFY_HEAD^{commit}")"
```

- SHA が一致しなければ止まり、その worktree を根拠に使わない。
- 作業領域の名前は、review-fix-planning と同じ `review-fix-planning.XXXXXX` にする。このスキルは review-fix-planning の検証の工程を引き継いだもので、AGENTS.md の「コミットしないファイルの置き場所」と `.devcontainer/cleanup-workspace.bash` の片付けは、この名前の作業領域だけを対象にしている。
- 再現に使う一時ファイル（スクリプト、ログ、出力）はすべて `$REPRO_ROOT` の下に置く。`$WT` の中にファイルを作らない（`__pycache__` を作らないよう、Python は `-B` か `PYTHONDONTWRITEBYTECODE=1` で動かす）。`${TMPDIR:-/tmp}` の直下に固定名で作らない。
- 後片付けは、worktree を使う確認がすべて終わった時点で行う。書き出しまで待たない。

```bash
git -C "$TARGET_REPO_ROOT" worktree remove --force "$WT" && rm -rf -- "${REPRO_ROOT:?}"
```

- `worktree remove` が失敗したら、親ディレクトリを `rm -rf` しない（git の登録が消えたパスを指したまま残る）。止まって、`$WT` と `$REPRO_ROOT` の完全パスを報告し、`.devcontainer/cleanup-workspace.bash` に任せる。
- 一覧に書く再現の `place` には、どの HEAD の隔離 worktree で、何を置いたかを書く。一時ディレクトリのパスは書かない。

## 時間

開始時刻の固定、期限を延長しないこと、打ち切りの順序、委譲の条件は、pr-review の [時間予算と委譲規約](../pr-review/references/time-budget.md) に従う。このスキルで決めるのは予算の値だけである。

| モード | 全体 | 確定・保存の予約枠 | 補助委譲 |
| --- | --- | --- | --- |
| `cycle` | 30 分 | 予約枠 5 分 | 読み取り専用の補助委譲を最大 1 件（例: 別の入力の実行を並行させる）。指摘の判定の根拠となる精読は委譲しない |
| `fix_check` | 30 分 | 予約枠 5 分 | 使わない。再委譲しない |

- `fix_check` では、主張の列挙・生成物・全変更ファイルの検査・文書候補の判定をこの予算に含める。実行器の設定期限が短ければ短い方を守る。
- `fix_check` では、dispatch が渡す `time_limits`（`started_at`・`stop_new_checks_at`・`deadline_at`）に従う。確認の区切りごとに `date -u` で時刻を確かめ、`stop_new_checks_at` を過ぎたら新しい確認を始めず、残りを `unchecked` と `coverage.remaining` にして書き出し、`deadline_at` より前に返す。
- `fix_check` の確認の順序は、一覧の固定（主張・変更ファイル・文書候補）→ 文書候補の判定 → 主張の確認 → 変更ファイルの検査。同じ段階の中では時間のかからない確認から行い、テストスイート全体のような長い実行は時間が残っているときに行う。
- 時間切れで確かめられなかった候補は `unverified` にし、足りない確認を書く。期限を過ぎたら新しい確認を始めず、書き出しへ進む。
- 開始・終了の時刻と秒数を `timing` に、確かめるのにかかった秒数を `accuracy.verify_seconds` に書く。

## サイクルの確認（`mode: cycle`）

1. **集める**
   - `gh pr view "$PR_URL" --json headRefOid,baseRefName` で PR の現在の head を確かめ、`VERIFY_HEAD` にする。base は `git -C "$TARGET_REPO_ROOT" merge-base "origin/<baseRefName>" "$VERIFY_HEAD"`。
   - レビュアーはレビューディレクトリの直下の `<model>.md` だけで数える（`findings/` と `verified/` はレビュアーではない）。各レビューファイルの `## Review history` の最後の行の HEAD が `VERIFY_HEAD` と違うレビュアーは「古いレビュアー」として区別し（`inputs[].stale`）、その候補は確かめるが、見逃しの集計には入れない。
   - `findings/<model>-<VERIFY_HEAD 先頭 12 文字>.json` があれば、それを `uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" validate <ファイル>` で検査し、`uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" check-pair <model>.md <ファイル>` でレビューファイルと一致することを確かめてから読む（`input_kind: json`）。一致しなければ、その候補一覧は使わず、レビューファイルを読む。無いレビュアーは、`uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" from-markdown <model>.md --review-file <model>.md --repo "$PR_REPO" --pr <PR> --model <モデル> --runtime <ランタイム>` で読み取る（`input_kind: markdown`。新しい項目は空なので、このスキルが補う）。`from-markdown` が読めなかった見出しや Summary の件数との食い違いを報告して止まったときは、そのレビューファイルの Markdown を直接読む: 指摘を 1 件ずつ拾い、読めなかった理由を `coverage` に書く。指摘を黙って落とさない。
   - レビュアーが Unverified candidates に挙げた候補も、確かめる対象に入れる。
2. **まとめる**
   - 守るべき性質と位置が同じ候補を 1 件にまとめる。元の指摘の参照（`<レビューファイル名から .md を除いたもの>#<ID>@<検出 HEAD>`。判断の記録と同じ書き方）をすべて `sources` に残し、元の重大度を `reported_severities` に残す。
   - レビュアーの数は根拠にしない。似ていても性質が違う候補はまとめない。
3. **確かめる**
   - 各候補を `VERIFY_HEAD` のコミット済みの内容で読み、挙動についての主張は隔離した worktree で再現する（上の節）。判定は `confirmed`（再現した。`reproduction` に、コマンド・場所・条件・結果。挙動についての主張は、挙動を実行した再現があるときだけ `confirmed` にする。コードや文書を読んだだけの確認は、文書・静的な主張にだけ使い、そのことを `place` に書く）、`not_reproduced`（再現しなかった・反証した。`basis` に根拠）、`unverified`（確認できていない。`missing_checks` に足りない確認）のどれかにする。
   - 「この指摘が誤りだとしたらどこか」を確かめる（既存のガード、テストによる固定、文書化された意図）。反証できたものは `not_reproduced`。
4. **別の入力を試す**
   - 確かめた性質ごとに、同じ性質に反しうる別の入力を挙げて実行する。レビュアーの `variants` に加えて、同じ文法の別の書き方、同じ状態の別の値、境界の値を自分で挙げる。報告された入力の形だけで性質を書かない（例: `Z` 付きの日時だけでなく、`+09:00` 付き・オフセット無し・小数秒付き）。
   - 入力ごとに `variants` へ、反する（`violates`）・反しない（`holds`）・未確認（`unchecked`）と、確かめた方法を書く。反する入力が見つかれば、指摘の範囲がその分広いことを `basis` に書く。
5. **条件を述べている箇所と照合する**
   - その性質や条件を述べている README・スキル（.agents の本体と、まだ一本化していない版）・`interfaces.rst`・ヘルプ・docstring を検索し（例: `git -C "$TARGET_REPO_ROOT" grep -n '<条件を表す語>' "$VERIFY_HEAD" -- README.md .agents .claude .devin tools`）、実装と一致するかを `statement_sites` に書く（`consistent`・`inconsistent`・`unchecked`）。実行した検索のコマンドを `basis` に書く。
   - 検索の語は、条件の文言だけにしない。その振る舞いに関わる状態・操作・コマンドの名前（例: 状態名、サブコマンド名、設定のキー）でも探す。同じ条件を別の言い回しで述べた文書は、文言では見つからない。
6. **未確認の範囲を閉じる**
   - レビュアーが判断しなかったもの（候補一覧の `coverage.declined`、レビューファイルの Review coverage の読めなかった範囲・未実施の検証、`Needs manual verification` の理由）は、このサイクルのうちに読むか確かめ、`coverage.closed` に書く。読めないものだけを、理由を付けて `coverage.remaining` に残す。次のサイクルに回さない。
7. **PR 起因を確かめる**
   - base と比べ、PR 起因（`introduced`・`surfaced`・`preexisting`・`unknown`）と根拠（原因の commit、base 側の状態。`git show <base>:<path>`、`git log -S`）を `pr_relation` に書く。「差分に悪い行がある」だけで判断しない。原因の commit が分かれば `cause_commit` に書く。
   - 判断の記録に前のサイクルがあれば、原因の commit が前のサイクルの `reviewed_head` の祖先かどうかで、その欠陥が存在していた最初のサイクルの HEAD（`first_present_head`）と、遅れた発見か（`late_detection`）を書く。決められなければ `unknown`。
   - 安全性の 3 項目（資格情報・秘密情報が想定していない相手へ渡る、意図した範囲の外を書き換える・削除する、権限の確認や書き込み前の検査を迂回できる）に当たると主張する指摘は、示された経路（入口・入力・環境と、渡る先・書き換える先、または迂回される検査）をコードを読むか実行して確かめ、`safety_path` に書く（安全性の経路）。重大度は再評価しない。
8. **書き出す**（下の「書き出すもの」）。`uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" validate <一時ファイル>` に通ってから、置き換える。

## 修正の確認（`mode: fix_check`）

関連テストを通しローカルで commit した修正を、実装時の思い込みではなく主張ごとの証拠で確かめる。確かめるのは `to` のコミット済み内容と隔離 worktree での実行だけ。

### 担当の割り当て

固定仕様は [agent_runner のインターフェース](../../../tools/agent_runner/interfaces.rst)。工程名 `fix_check` に対応する設定の役割名は **`fix-check`**。`workflows.fix.assignments.fix-check` の一次担当 1 件と明示 fallback を使う。他の役割を一緒に起動しない。

```bash
uv run --project "$TOPLEVEL/tools/devenv" python "$SKILL_DIR/scripts/dispatch.py" \
  --request "$FIX_INPUT" --output-dir "$NEW_RUN_DIR"
```

`SKILL_DIR` はこのスキル本体の絶対パス。request は入口の 7 項目を持つ JSON、NEW_RUN_DIR は既存でない一意の保存先（親 main root の agent_tmp.py で作ったセッション領域の子）。template/config は既存実行器と同じ優先順位、必要なら `--template`/`--config`、担当だけ上書きする場合は `--assign <agent_id|self>`。書き込み役・複数一次担当・不完全入力は拒否する。

dispatch の終了コードは次のとおり。出力の JSON 行は `state`・`artifact_dir`・`outside_changes`・`review_changes`。起動前に `review_dir` の全エントリの種類・権限・内容ハッシュを固定し、停止確認後に照合する。Gitignored・対象 repository 外の保存先も検査する。リンクはリンク先の文字列のハッシュだけを記録し、参照先は走査しない。保存入口の `review_dir` 本体・`verified` のリンクは起動前に拒否する。

- 0: `self`、または `success` で `outside_changes` と `review_changes` が空。
- 1: 失敗・停止未確認、または `outside_changes` / `review_changes` がある（`success` でも 1）。停止未確認では保存先を読み直さず `review_changes` は null、監査不能も無変更とはしない。
- 2: 設定・入力の誤り。stderr に、秘密を含まない分類（設定・runner のカテゴリ、request の誤った欄名、入力の誤りの種類）を出す。

`dispatch.json` の欄は、`batch`（runner の結果）・`request`（検証済みの入口）・`verified`（入力の確認済みの一覧）・`skill`（`path` と `sha256`）・`verifier`（`success` のときの実測値、ほかは null）・`time_limits`・`review_snapshot`（起動前の保存先の相対パスと観測値。内容そのものは保存しない）・`review_changes`（変更パス、停止未確認なら null）・`prompt`（`self` のときだけ）。

- `self`: 外部を起動しない。`dispatch.json` の完全入力と prompt に従い司令塔が下の確認を実行する。未実施の確認を合格としない。司令塔が自分で実行したことは、一覧の該当する `basis` と要約に書く。
- 外部: 同期終了した結果を、次の helper で検査して保存する。`$RUN_DIR` は dispatch が出力した JSON 行の `artifact_dir`（`dispatch.json` のあるディレクトリ）。失敗を暗黙に self へ置き換えない。runner の success は応答の回収成功だけで、payload の中身の正しさは保証しない。外部担当は呼び出し側の保存先に書かない。

  ```bash
  uv run --project "$TOPLEVEL/tools/devenv" python "$SKILL_DIR/scripts/dispatch.py" accept --run-dir "$RUN_DIR"
  ```

  - 終了コード 0: 受け取った。保存した 2 つのパス（`statement_candidates` と `fix_check`）を JSON で 1 行出力する。要約の `.fix-check.md` は helper が書かないので、呼び出し側が受け取った一覧から書く。
  - 終了コード 1: 受け取らない。helper は正式な成果物を追加保存せず、理由を stderr に 1 行出す。保存先が起動前と違う場合は、不正な payload でも、その検査より先に拒否し、変更した相対パスをエスケープして列挙する。外部担当や他セッションが書いたファイルは自動削除・復元しない。列挙されたファイルを以前の成功した保存とは見なさず、この run の結果として使用しない。これは観測と受け取りの検査であり sandbox や変更の復元を保証しない。
  - 終了コード 2: `dispatch.json` が読めない、またはレビューディレクトリが無い。
  - 受け取らない条件: 結果が `success` でない（`self` を含む。self は司令塔が自分で確認して validate する）、どれかの attempt の `stop_confirmed` が真でない、開始前から登録されていた checkout に変更がある（`outside_changes`）、保存先が起動前と異なる（dispatch 後の変更も再検査）・監査できない・保存入口がリンク、最後の payload が `verified` と `inventory` の 2 つの object だけでない、`inventory` が道具の出力と合わない、`inventory.fix_ids` が request の `fix_ids` と違う、validate に通らない。
  - `inventory` は、道具（`statement-candidates`）の出力をそのまま含み、担当が見つけた語・候補を後ろに足しただけのものだけを受け取る。道具の候補・変更ファイル・主張を消したり書き換えたりしたものは受け取らない。
- 外部の `verifier` は `dispatch.json` の同名フィールド（実担当の model/runtime/provenance）で全体を置き換える。helper が runner の identity の null を unknown、skill_versions を skills(path/sha256)へ変換し、run_id/request_id/actual_agent は BatchResult/Result から、runtime は actual_agent の設定から得る。requested model/provider/effort を actual にコピーしない。実測 skill_versions の `sha256:` 接頭辞は除去し、skill 名だけのキーからパスを推測せず unknown にする。不明パスに異なる hash が複数ある場合は 1 つを選ばず、v2 の unknown 行にまとめる。元の名前と値は BatchResult に全件保持する。inline で渡したこのスキルの内容は input の skill(path/sha256)で特定し、ネイティブの discovery を観測したとは書かない。self は司令塔の実観測値を使い、未観測は unknown。timing には開始/終了/秒数と waiting_seconds（未計測は null）、accuracy.verify_seconds には実測値を記録する。入力レビュアーの provenance は inputs と accuracy で同じ観測値を使う。

### 読み取り専用の対象

`writes=false` は呼び出し側 checkout と提供 worktree の変更を禁止する。**担当が作る一意の一時ディレクトリでの生成物・検査用ファイル・候補 JSON・validate ログの作成は禁止しない。** from/to のコミット済み内容をそこへ展開し、そこでビルド・lint・テストを実行する。生成物を要することを未実施の理由にしない。資格情報や環境変数の例は fake CLI と `env HOME="$TMP_HOME" ...` のワンショットだけで試す。実環境の資格情報や実サービスを使わない。uv は Python 3.14 以上で実行する。元 checkout の修正・commit・push や他者の作業領域への書き込みはしない。

### 主張と変更ファイルの一覧

**判定より先に一覧を固定する。** Spec の Fix 節の採用変更・検証、from..to の commit message、書き足した説明を読み、独立した主張を 1 件ずつ列挙する。1 つの説明が複数の性質を主張するなら分ける。`claims.json` は `{"fix_ids": [...], "claims": [{"id": "C-1", "fix_id", "claim", "source", "kind": "artifact"|"behavior"|"static"}]}`。各 Fix に 1 件以上。出典を記録し、確認しづらい主張を一覧から消さない。

- artifact: 生成して、その主張が現れる全データ/ノード/出力と意味を検査する。ビルド成功や構造の一部分だけで、他の属性・関連・名前まで満たすと判断しない。検査述語と観測値を artifact 欄に残す。
- behavior: 別の入力・境界・失敗状態も実行する。
- static: コミット済みの記述を読む。実行が必要な主張を static へ読み替えない。

変更ファイルは道具が全パスを列挙する。各ファイルに適用する lint・型検査・ビルドの規則を定義元から読む。たとえば RST は [rst-core-style-guide](../rst-writer/references/rst-core-style-guide.md) と対象プロフィール、リポジトリの検証は AGENTS.md。規則をここに写さない。削除/rename/テストも検査欄から落とさない。コマンドを `to` の隔離 worktree で実行し、規則の定義元、コマンド、HEAD、終了コード、結果を `fix_evidence.files` に残す。削除や適用する検査が無い場合だけ理由付き not_applicable。環境不足・時間切れは unchecked と coverage.remaining。lint 規則が `--fix` を使う場合は隔離コピーでだけ実行し、実行前後の hash/diff も保存する。終了 0 でも修正が発生したらコミット済みの原文は規則違反なので failed の指摘にする。自動修正後のコピーを元のコミットの合格証拠にしない。

確認を始めたら、文書候補と主張と変更ファイルを道具で列挙してから照合する。

1. 条件を述べている文書の候補を列挙する。

   ```bash
   uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" statement-candidates --root "$TARGET_REPO_ROOT" --from <from> --to <to> --claims "$CLAIMS_JSON" --output "<自分の一意の一時ディレクトリ>/<to 先頭 12 文字>.statement-candidates.json"
   ```

   外部担当は、この一時出力の内容を `inventory` として返す。呼び出し側の保存先へは書かず、保存は `accept` が行う。self では司令塔が validate して保存する。
   修正の差分から、コードの変更行と前後の行の状態名・識別子・CLI オプションと、文書の変更行（追加・削除）の識別子らしい語・コードスパン・RST の directive 名と role 名を取り出し、`to` のコミット済みの README・SKILL.md・`*.rst` などを検索した結果である。ヒットが多すぎる語は `common_terms` に件数だけが入る。文書の小文字だけの語（`body`・`deprecated` など）とコードスパンでない `.` を含む名前は取らないので、文書・テンプレートの修正では、変更行の主題の語を自分で足して検索する（`inventory` の後ろに足す）。
2. 候補を判定する。
   - 判定の単位: `consistent`・`inconsistent` は候補ごとの判定行（`ids` が 1 件）にする。`unrelated`・`unchecked` は、理由が同じ候補を語ごとにまとめてよい。
   - 読む範囲: 名前を含む前後の段落も読み、全 Fix のどの主張/変化に関係するかを確認する。同名の 1 条件だけを見て、他の Fix や別の属性・値・場所・経路の変化を見落とさない。
   - `basis`: basis には比較した具体的な変更前→変更後の値と、文書の記述を書く。`unrelated`・`unchecked` をまとめた行の `basis` には、その語がそこで何を指すか（`unchecked` なら足りない確認）を書く。
   - 候補の `text`: 長い行では一致した語の前後の抜粋なので、`consistent`・`inconsistent` にする前に `git show <to>:<path>` でその行と前後の段落を読む。
   - 関係の判断:
     - 「禁止を明記していない」「同じ工程の説明」は consistent の証拠ではない。関係がある候補は実装/実行の具体的な値と文書の値が合うか確かめる。
     - `unrelated` は同名の別物など事実として関係が無い場合だけ。「通常の利用ではない」「利用者はそう使わない」は採否なので、この値の根拠にしない。
   - 条件を狭めた修正:
     - **from では満たせて to では外れる場面**を先に列挙し、そこで文書の手順を実行して両者を比較する。
     - 境界を越える値・設定の継承や伝播を変えたなら、直接の入口だけでなく、その後段で同じ文書手順を実行する組み合わせも試す。
     - **from/to で同じ入力と前処理を保つ。** 値は入口の前で 1 回だけ用意し、その値を使う後段の手順を実行する。境界通過後に不足した値を再設定してからの成功は、失われた伝播を確かめた証拠にならない。
     - from 成功→to 失敗の事実を回避策や追加条件で消さない。文書に範囲の限定が無いのに特定の境界だけ無関係と推測しない。
     - fake の値/コマンドで確かめ、通常の利用と認めるかは判定しない。実行できなければ unchecked で残す。
   - 判定の種類: `consistent`（一致）・`inconsistent`（不一致）・`unrelated`（修正した条件と関係ない）・`unchecked`（時間切れなどで判定できない。`coverage.remaining` に残す）のどれかにする。
   - **ヒットしたのに判定しない候補を残さない。** 言い換えて書かれた記述（同じ状態や操作を、条件を付けずに述べているもの）は、語が一致した行を読んで判断する。
3. `inconsistent` の候補は、指摘（`findings[]`）の `statement_sites` に `inconsistent` で書く（同じ `path`）。
4. 判定を一覧の `statement_candidates`（`file` と `judgments`）に書く。`common_terms` の語は、修正した条件に関わるものだけ、自分で絞って検索する。
5. 列挙した主張を `fix_evidence.claims` に、各変更ファイルを `fix_evidence.files` に全件判定する。形は [review-findings の 5 章](../../review-findings.md#修正の主張と変更ファイルの検査fix_evidence)。主張は holds/violates/unchecked、ファイルは passed/failed/not_applicable/unchecked。checks に command/head/mode/exit_code/result/artifact、問題は confirmed の findings 参照を残す。unchecked は coverage.remaining の claim:C-ID/file:PATH にも残す。判定後に都合よく一覧を減らさない。
6. `validate` は文書候補・主張・変更ファイルの全件判定、出典 HEAD、実行証拠と問題の指摘参照を突き合わせる。候補の sidecar も同じディレクトリに置く。通らなければ保存せず、原因を調べて直す。自然言語の主張の完全な抽出や証拠の意味まで道具が証明するわけではない。

道具の候補は最低限のもので、検索の上限ではない。次の範囲のうち、条件を述べている箇所の照合では、候補に無い箇所も自分で探す。

- 範囲:
  - 修正の差分（`git -C "$TARGET_REPO_ROOT" diff <from>..<to>`）が、意図した性質を満たすか
  - 修正した性質についての同じ種類の別の入力（確認済みの一覧の `variants` と、新たに挙げたもの）で、修正が効くか
  - 修正が変えた条件を述べている箇所（README・スキルの本体・`interfaces.rst`・ヘルプ）が、新しい条件と一致するか。修正の差分で文書の記述を書き換えたなら、`git -C "$TARGET_REPO_ROOT" diff <from>..<to> --` で書き換える前の記述の語を取り出し、その語でほかの文書を検索する（直し忘れた文書は古い記述のまま残っている）。あわせて、変えた振る舞いに関わる状態・操作・コマンドの名前でも検索する。修正が条件を狭めた（受け付ける・続ける・成功とする場合を減らした）ときは、その振る舞いを条件なしで述べている箇所（「〜後も対応する」「〜でも動く」「必ず〜する」など）を探す。狭めた条件がその箇所に書かれていなければ `inconsistent` にする
  - 修正で書き足した説明（docstring・コメント・README・スキルの文言）が主張していることが、事実か
- **PR 全体の再レビューはしない**（push 後の次のサイクルで行う）。
- 出力は `verified/<修正後 HEAD 先頭 12 文字>.fix-check.json` と `.fix-check.md`。試すたびに別のファイルになる。新しく見つけた問題は、サイクルの確認と同じ形で書く（判定と根拠。採否は書かない）。確認担当が見つけた問題の `sources` は空の配列にし、参照は `fix-check#V-<n>@<to 先頭 12 文字>` と書く。
- 呼び出し側（review-fix-planning）は、結果の問題を自分の決め方で判断する。このスキルは push を止める・進めるを決めない。

## 書き出すもの

形式は [review-findings](../../review-findings.md) の5章に従う。新規結果は `review-findings/v2`、fix_checkは主張・変更ファイルを含む候補sidecarとfix_evidenceを必須にする。

| ファイル | 内容 |
| --- | --- |
| `verified/<HEAD 先頭 12 文字>.json` | サイクルの確認の一覧（`kind: "verified"`、`mode: "cycle"`）。同じ HEAD でやり直したときは置き換える |
| `verified/<HEAD 先頭 12 文字>.md` | その要約 |
| `verified/<修正後 HEAD 先頭 12 文字>.fix-check.json`・`.fix-check.md` | 修正の確認 |
| `verified/<修正後 HEAD 先頭 12 文字>.statement-candidates.json` | 修正の確認の文書の候補（`statement-candidates` が書く） |

- 一覧は、まず同じディレクトリの一時ファイル（`.` で始まる名前）に書き、`validate` に通ってから `mv` で置き換える（途中の状態を残さない）。通らなければ置き換えず、問題を報告する。
- `inputs` には入力にしたレビュアーとファイル、`coverage` には閉じた未確認範囲と残った未確認範囲を書く。
- 精度の改善に使う記録（`accuracy`）:
  - レビュアーごとに、候補の数、再現した数、再現しなかった数、確認できなかった数、見逃し（同じ HEAD をレビューしたほかのレビュアーが見つけて `confirmed` になった指摘のうち、このレビュアーが見つけなかったものの数。古いレビュアーは数えない）、そのレビュアーだけが見つけた確認済みの指摘（`unique`）
  - 探すのにかかった秒数（候補一覧の `timing.seconds`。無ければ null）と、確かめるのにかかった秒数
  - 遅れた発見は、指摘ごとの `late_detection` に書く
- 要約の Markdown には、判定ごとの件数、`confirmed` の一覧（性質・位置・反した別の入力・不一致だった文書）、`unverified` と足りない確認、残った未確認範囲、所要時間を書く。採否は書かない。

## 報告

呼び出し側（review-fix-planning か利用者）に、書き出したファイルのパス、判定ごとの件数、残った未確認範囲、所要時間、後片付けの結果（残したパスがあればその完全パス）を返す。

## 合理化とその実態

| 合理化 | 実態 |
| --- | --- |
| 「3 人のレビュアーが同じことを言っているので確か」 | 票の数は根拠ではない。再現か、コードを読んだ根拠で判定する |
| 「テストが失敗したので再現しなかった」 | 失敗は `not_reproduced` ではない。`unverified` にして足りない確認を書く |
| 「報告された入力で再現したので十分」 | 同じ性質に反する別の入力を試さないと、部分的な修正が残る |
| 「実装は直っている。文書は別の問題」 | 条件を述べている文書が古いままだと、利用者は誤った手順を取る。`statement_sites` に書く |
| 「重大度を上げたほうが直してもらえる」 | 重大度の再評価と採否は review-fix-planning が決める。事実だけを書く |
| 「作業ツリーで確かめたほうが早い」 | 未コミットの変更で誤った判定になる。コミット済みの内容と隔離した worktree だけを使う |
| 「通常の使い方ではないから無関係」 | 関係の有無は事実で決める。採否はplannerが決める |
| 「ビルドは通ったので生成物の主張も満たす」 | 生成物の全対象と主張する意味を検査する |

## ランタイムごとの違い

本体は `.agents/skills/review-verify/` の1つ。Claude Codeは `.claude/skills/review-verify` の相対リンクから読む。Piの明示入口は `/skill:review-verify`。自動bootstrapの設定を変更しない。frontmatterのallowed-toolsはClaude Code/Devinの許可確認を省く一覧で、使えるツールを制限する仕組みではない。どのランタイムも読み取り専用・採否を決めない規則を守る。設定のself/外部の選択はランタイムやsubagent機能の有無ではなく、fix-checkの割り当てで決まる。
