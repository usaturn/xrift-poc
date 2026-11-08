---
name: repo-cleanup-after-merge
description: PR がマージされた後にローカルリポジトリを片付けるときに使う。「PR が全て Merge されたことを確認して branch, worktree を整理してローカルを更新して」「マージ済みのブランチを消して」「worktree を掃除して」「origin/main に追従して」「リポジトリを整理して」といった依頼、および `git worktree list` に使い終わった作業ツリーが溜まっている、ローカルの `origin/main` が古い、レビュー用の使い捨て worktree の残骸が残っている、といった場面でトリガーする。未マージの作業のリベース、コンフリクト解消、リリース作業には使用しない。
allowed-tools: Read, Grep, Glob, AskUserQuestion, Bash(git *), Bash(gh *), Bash(ls *), Bash(diff *), Bash(cmp *), Bash(find *), Bash(rm *), Bash(uv run --no-project python *agent_tmp.py *)
---

# マージ後のリポジトリ整理

## 概要

PR がマージされた後に、ローカルの branch / worktree / 残骸を片付け、作業ツリーを `origin/main` に追従させる。

**中核原則: 整理とは「消すこと」ではなく「もう失うものが無いと確かめてから消すこと」である。**

git の操作はほぼ全て取り消せる。取り消せないのは次の 2 つだけであり、この 2 つにだけ注意を集中する。

- **ゲート A**: git 管理外ファイルの `rm` — 復元手段が無い
- **ゲート B**: リモートへの push（ブランチ削除を含む）— 他人が見る共有物への外向き操作

## 対象リポジトリの確定

整理対象の PR が属するリポジトリで root を決める。`repository-ownership` の判定（`route_target.py`）で、親（`usaturn/xrift-poc`）なら親の checkout、制作物ならその制作物の `git_root` が root である。制作物の片付けは、その制作物の checkout の中だけで行い、親の checkout から `items/`・`worlds/` の中身を消したり動かしたりしない。

`REPO` は `route_target.py` の `repository`（`owner/repo`）に決め、リポジトリ名から推測しない。`gh` には毎回 `--repo "$REPO"` を付ける。制作物で `repository` が null のときは、PR の状態を確かめられないので、このスキルを止める。origin の設定（`repository-ownership`）を利用者に説明し、代わりに親の `gh` で調べない。

## ワークフロー

### 1. 何も消す前に状態を把握する

次の read-only コマンドを実行する。

```bash
git status
git worktree list --porcelain
git branch -vv
git remote -v
git for-each-ref refs/pr-review
gh pr list --repo "$REPO" --state open --limit 1000 --json number,title,headRefName,headRefOid
gh pr list --repo "$REPO" --state all --limit 20 --json number,title,state,headRefName,mergedAt
```

`gh` が非0終了した場合、PR の状態は不明である。マージ判定を推測で進めず、エラーを報告して停止する。open の一覧の件数が `--limit` に達したときは、全件と扱わず、上限を上げて取り直す。

Open PR が 1 件以上ある場合、次は **整理対象外**である。残存 PR をユーザーへ報告し、それ以外だけを進める。

- `headRefName` に一致する branch
- その branch をチェックアウトしている worktree
- `git worktree list --porcelain` の `HEAD` が、オープン PR の `headRefOid` と一致する worktree（detached でも対象外）
- パスが `/tmp/pr-review-*` の worktree と、その親ディレクトリ。親の `session.json` の `head_branch` がオープン PR の `headRefName` である場合も含む
- オープン PR の番号に対応する `refs/pr-review/<PR番号>`（手順 9 の到達可能性判定でも残す）

Open PR の有無にかかわらず、`git worktree list --porcelain` で `prunable` または `locked` の行を持つ worktree も **整理対象外**である。パスと行の内容を控え、手順 11 で報告する。

- `prunable` は、登録パスに接続できないという情報である。本体の消失や削除の許可を示さない。本体を別の場所へ移動して `git worktree repair` をまだしていない worktree や、親ディレクトリだけが消えて所有者の照合を待っている別 run の登録にも出る
- `locked` は、所有する run などが削除を止めている印である

### 2. リモートを取り込む

```bash
git fetch --all --prune
```

**マージ判定より前に必ず実行する。** ローカルの `origin/main` は古いのが普通であり、fetch 前の「マージ済みか」の判定は誤る。

### 3. マージ済みか判定する

まず対象リポジトリのマージ済み PR を取得する。`REPO` は「対象リポジトリの確定」で決めた `owner/repo` とする。

```bash
gh pr list --repo "$REPO" --state merged --limit 1000 \
  --json number,headRefName,headRefOid,mergeCommit,isCrossRepository
```

一覧が上限件数に達した場合は全件と扱わず、上限を増やして取得し直す。手順 1 の Open PR 一覧も同様に、件数の打ち切りを解消してから判定する。取得に失敗したら停止する。`isCrossRepository` が true の PR は、同名でも対象リポジトリの branch の証拠にしない。PR が見つからない branch は削除せず残す。

branch ごとに先端の完全 SHA と次の出力を記録し、同じ `headRefName` の MERGED PR と照合する。ローカル branch は `refs/heads/<branch>`、手順 10 のリモート側は `refs/remotes/origin/<branch>` を使い、先端を混同しない。

```bash
git rev-parse --verify refs/heads/<branch>
git log --oneline origin/main..refs/heads/<branch>
```

手順 1 の Open PR 除外を優先したうえで、次のいずれかを満たせば取り込み済みとする。

- **祖先型:** `git log` が終了コード 0 で出力が空。従来どおり、対応 PR が MERGED であることも確認する。
- **PR 型（squash / rebase など）:** `git log` に出力があっても、次のすべてを満たす。
  1. 対応 PR が MERGED で、branch の先端 SHA がその PR の `headRefOid` と一致する。branch 名だけで古い PR を選ばない。
  2. その PR の `mergeCommit.oid` が空でなく、次が終了コード 0 となる。

  ```bash
  git merge-base --is-ancestor <mergeCommit.oid> origin/main
  ```

  終了コード 1 は到達不能なので残す。それ以外の非 0 は確認エラーとして停止する。

`git log` の出力があることだけで「未取り込み」と決めない。PR 型の証拠が欠け、祖先型でもない branch は worktree とともに残す。先端が PR の `headRefOid` と異なる、`mergeCommit` が無い・到達不能、PR が CLOSED で未マージの場合も、PR 型の証拠にはならない。SHA を解決できない・Git コマンドが失敗した場合を「出力が空」と扱わない。

判定に使った PR 番号・先端 SHA・`mergeCommit.oid`・判定区分を記録する。マージ後に revert されていても、マージ時点で取り込み済みとして扱う。現在の main との tree 一致や `git cherry` の結果で代替しない。削除直前にも Open PR の有無と対象の先端を確認し、変化があれば再判定する。

### 4. worktree ごとに clean を確認する

```bash
git -C <worktree の絶対パス> status --short
git -C <worktree の絶対パス> status --short --ignored -- items worlds
git -C <worktree の絶対パス> status --short --ignored=matching
```

`-C` には絶対パスを渡す。worktree 隔離セッションでは相対パスが拒否されることがある。clean とするのは、3 つとも終了コードが 0 で、1 つ目と 2 つ目の出力が空、3 つ目に再生成できる依存・キャッシュ以外の `!!` の行が無いときだけである。次のどれかに当たる worktree は **削除しない**。パスを添えてユーザーへ報告し、判断を仰ぐ。

- 3 つのどれかの終了コードが 0 以外である。登録パスが無い・移動した・読めない worktree では、出力が空のまま終了コードが 0 以外（`cannot change to` なら 128）になる。clean を確認できていないので削除せず、手順 11 で報告する
- 1 つ目に出力がある（未コミットの変更・未追跡のファイル）
- 2 つ目に出力がある。`items/`・`worlds/` の中身（Git 未初期化の制作物や、置かれた制作物の Git リポジトリ）は Git で無視されているため 1 つ目に出ないが、手順 5 の `git worktree remove` は `--force` なしでも消す。親の worktree は制作物の置き場所ではないので、親の main checkout の `items/`・`worlds/` 側へ移すかどうかを確かめる
- 3 つ目の `!!` の行に、再生成できる依存・キャッシュ（`.venv/`・`node_modules/`・`__pycache__/`・`*.py[cod]`・`.pytest_cache/`・`.ruff_cache/`）以外がある（`reviews/`・`docs_draft/`・`tmp/`・`.env` など）。`items/`・`worlds/` の行は、中身が無くても出るので 2 つ目で判定する

#### 毎回ここで止まるなら、直すのは片付け側ではなく生成側である

untracked ファイルは保存先と照合する述語を持たないため、ゲート A の対象として必ず人間の判断を要求する。同じ種類のファイルで毎回止まるのは、**worktree 内に判定不能な生成物を残す手順が上流にある**ということである。ゲートを緩めて解決してはならない。

報告時は「どのスキル・どの手順の生成物か」を添えて指摘し、生成側を次の原則へ合わせるよう促す。

**生成物は worktree の外（親の main worktree の `tmp/agents/`。`uv run --no-project python .agents/scripts/agent_tmp.py mkdir <topic>` で作る）に出す。** untracked が発生しないので手順 4 で止まらない。本スキルは worktree 直下の untracked を機械的に照合する述語を持たない。手順 6 のゲート A は `session.json` を持つ残骸ディレクトリ専用であり、worktree 内に残す限り毎回ユーザー確認になる。

`.gitignore` への追加で黙らせてはならない。gitignore 対象は本スキルのガードレールで照合なしの削除を禁じており、`git status` からも消えるため、照合する術が無いまま残り続ける。

### 5. worktree を削除する

#### pr-review セッションは helper を先に使う

手順 1 の対象外（Open PR・locked・prunable）を除き、`/tmp/pr-review-*/worktree` は先に削除せず、親の `session.json` を helper に渡す。`PR_REVIEW_HELPER` は読んだ pr-review skill を持つワークスペースの `.agents/skills/pr-review/scripts/pr_review_workspace.py` の絶対パス、`STATE_FILE` は候補直下の `session.json` の絶対パスに設定する。

```bash
python3 "$PR_REVIEW_HELPER" reap-check --allow-failed --state "$STATE_FILE" \
  && python3 "$PR_REVIEW_HELPER" cleanup --strict-reap --allow-failed --state "$STATE_FILE"
```

helper が停止証拠・候補・Git の安全性を検証し、削除直前にも保存ロック内で再判定する。成功なら WT・残骸・自セッションの ref の処理は完了であり、手順 6 の削除は不要。拒否なら理由と対象パスを報告する。state が無い・無効な残骸は保留する。

失敗・放棄したセッション、または成功・保存済みと確認できないセッションは、拒否時に **WT も含め全体を保持**し、下の通常の worktree 削除や手順 6 のゲート A へ迂回しない。draft と候補の下書き（ある場合）が保存先と一致するセッションは、helper が実行記録によらず保存済み（`retirement: saved`）として扱う。未保存の draft・未保存の候補・`previous-review.md`・編集済み候補・未知ファイルがあれば残す。findings が空でも coverage・unverified candidates・timing・固定項目・未知フィールドの編集は成果であり、名前や空配列だけで未着手 scaffold と判断しない。

停止未確認 **だけ** が拒否理由なら、実際に担当プロセスと子孫の停止を確認した人だけが、確認内容を空白だけでない自由記述の理由として次を実行できる。Claude Code では、実行の前に `AskUserQuestion` で、担当のプロセスと子孫の停止を利用者が確かめたかを尋ね、確かめたと答えた内容を理由に渡す。年齢・PID 不在・モデルの fail は確認の代わりにならず、自動で理由を作らない。この引数が代替するのは停止証拠だけで、成果・未知ファイル・Git の安全検査は迂回しない。

```bash
python3 "$PR_REVIEW_HELPER" cleanup --strict-reap --allow-failed --state "$STATE_FILE" \
  --operator-confirmed-stopped "$CONFIRMED_STOP_REASON"
```

helper の結果にある削除・保留対象と理由を報告する。通常の保存済みセッション（draft と候補の下書きが保存先と一致し helper が保存済みと判定するもの、または `run_context` の無い旧形式で保存済み本文を照合できるもの）に限り、helper が拒否しても、以下の clean 確認と手順 6 の従来の照合を使える。

#### 通常の worktree 削除

削除してよいのは、手順 4 で clean を確認した次の worktree だけである。

- 手順 3 の祖先型または PR 型で取り込み済みと判定した branch を checkout している worktree（PR 型では branch の先端と `headRefOid` の一致も確認する）
- 上の helper 判定を先に行い、通常の保存済みセッションと確認できた、オープン PR に対応しない `/tmp/pr-review-*` の worktree

それ以外の worktree は削除せず、手順 11 で報告する。手順 1 で対象外にしたもの（オープン PR に対応するもの、`prunable`・`locked` のもの）のほかに、branch を checkout していない detached な worktree（PR 修正ループや agent-runner など、別の run が作ったもの。上の `/tmp/pr-review-*` は除く）も対象外である。HEAD が `origin/main` から到達可能で clean でも、所有者が使っている作業場所かどうかは Git から分からない。`/tmp/pr-review-*` の worktree も detached だが、親の `session.json` で対応する PR を確かめられるので、手順 1 の条件で選ぶ。

```bash
git worktree remove <path>
```

`git worktree remove` は対象の登録も消すので、続けて `git worktree prune` を実行しない。対象を限定しない `git worktree prune` は、片付け対象と関係の無い `prunable` な登録をすべて消す。repair 前の移動済み worktree の登録が消えると、`git worktree repair` で戻せず、移動先の本体は HEAD・index との対応を失う。存在しないパスへの `git worktree remove`（`--force` の有無を問わない）も同じく登録を消すので、`prunable` な worktree に使わない。

`git worktree remove` は、Git で無視したファイルも含めて worktree のディレクトリごと消す。`--force` なしで拒否されるのは、未コミットの変更か未追跡のファイルがあるときだけである。無視したファイルを守るのは手順 4 の確認だけである。

通常の保存済みセッションでは、worktree 本体の削除と、手順 6 の残骸削除は **別の判断**である。worktree 本体は追跡済みの内容であり、マージ済み・clean を確認していれば削除してよい。手順 6 で保留が発生しても、worktree 本体の削除まで止める必要はない。

### 6. ゲート A: 残骸ディレクトリは照合してから消す

この節は通常の保存済みセッションだけに適用する。手順 5 で helper が拒否した失敗・放棄・成功未確認のセッションには適用せず、`previous-review.md` の例外も使わない。

`git worktree remove` が消すのは worktree のディレクトリ（Git で無視したファイルを含む）だけで、その外は消さない。親ディレクトリに残る `session.json` や `review-backup/` は **git 管理外**であり、`git status` にも `git worktree list` にも現れない。

ゲート A の照合候補にする前に、helper の `state_file` が候補直下の `session.json` であることを確かめ、その state の `worktree` が候補内の `worktree/`、`backup_dir` が実在する候補内の `review-backup/`、`draft` がその `review-backup/` 内を指すことを確認する。さらに `destination` が安全に解決できて正しいリポジトリの `reviews/` 配下に収まることを確認する（基準は `version` 7 では `parent_root`、6 では `main_root`）。`review-backup/` がない場合は **`review-backup/ 欠落`** と分類する。

`worktree/` が残っている候補は手順 4・5 の worktree 安全確認に委ね、ゲート A では削除しない。worktree 削除後にゲート A で候補を削除できるのは、直下のエントリが `session.json` と `review-backup/` のみの場合に限る。一時 clone の `repo/` が残る候補（state の `target_kind` が `clone`）は、helper の `cleanup`（`.devcontainer/cleanup-workspace.bash` の厳密な片付け）に任せ、ゲート A では削除しない。その他の未知のエントリは `未知のエントリ` としてパスとともに報告して保持し、候補を削除しない。この保留を理由に全体を停止せず、他の手順で独立して安全に確認できる操作を続行する。

`session.json` がない・読めない・有効でない、`review-backup/` が欠落またはディレクトリでない、あるいは `destination` の安全性を確認できない候補は **`対象外・保留`** とする。そのパスと不足・不一致の根拠を報告して保持し、削除しない。この分類だけを理由に全体を停止せず、他の手順で独立して安全に確認できる操作を続行する。名前や古さ、コピーされた state の `destination` だけを削除根拠にしない。

照合する保存先のパスは有効な `session.json` の `destination` から取り、branch 名の変換で推測しない。

```bash
python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["destination"])' <残骸ディレクトリ>/session.json
```

`diff` の出力は、親の main worktree の `tmp/agents/` に作ったセッションディレクトリへ書く（親 checkout の root で実行する）。セッションディレクトリは手順 6 の最初に 1 回だけ作り、表示された絶対パスを控える。

```bash
GATE_DIR="$(uv run --no-project python .agents/scripts/agent_tmp.py mkdir repo-cleanup-gate-a)" && printf 'GATE_DIR=%s\n' "$GATE_DIR"
```

シェル変数は Bash の呼び出しをまたいで残らない。以降の呼び出しでは、先頭で `GATE_DIR=<表示された絶対パス>` を設定し直す。候補ごとに番号 `<n>` を振り、判定に使う出力は `diff` と同じ呼び出しの中で表示する。

```bash
GATE_DIR=<表示された絶対パス>
diff -rq <残骸ディレクトリ>/review-backup <保存先ディレクトリ> > "${GATE_DIR:?}/diff-<n>.out" 2> "${GATE_DIR:?}/diff-<n>.err"; echo "exit=$?"
cat "$GATE_DIR/diff-<n>.out"; printf '%s\n' '--- stderr ---'; cat "$GATE_DIR/diff-<n>.err"
```

手順 6 のすべての候補の判定を終えたら、`GATE_DIR=<表示された絶対パス>` を設定し直したうえで `rm -rf -- "${GATE_DIR:?}"` で消す。`GATE_DIR` が空なら `:?` が失敗し、何も消さない。判定の途中で停止した場合は、`$GATE_DIR` の完全パスを手順 11 で報告する。

削除してよいのは、次をすべて満たすときだけである。空の `grep '^Only in .*review-backup'` だけを削除許可にしてはならない。

- `diff` の stderr（`"$GATE_DIR/diff-<n>.err"`）が空である。保存先ディレクトリ不在はここに出る（終了コード 2）。stderr があれば削除しない
- stdout に `Files ... differ` が無い
- stdout の `Only in ...review-backup` が、`previous-review.md` 以外に無い。`previous-review.md` はセッション snapshot であり、保存先へコピーせず、この判定からも除外する

どれか一つでも欠ければ削除しない。

- `Only in ...review-backup` の対象が `<model>.md` なら保存先へ `cp` する。コピーは非破壊であり、AskUserQuestion は不要。コピー後に述語を再実行する
- `previous-review.md` は保存先へコピーしない
- `Files ... differ` の `<model>.md` は保存先のファイルを上書きしない。残すか別名退避するかをユーザーに確認し、確認が終わるまで残骸は消さない

```bash
cp <残骸ディレクトリ>/review-backup/<model>.md <保存先ディレクトリ>/
```

`reviews/` は `.gitignore` 対象であり、git は一切保護しない。

### 7. 作業ツリーを origin/main へ更新する

メイン作業ツリーが dirty かどうかでは止めない。未コミットの変更を失うかどうかは checkout の成否で決まる。`--force` / `--merge` を付けない `git checkout` は、ローカルの変更が checkout 先と衝突すると何も変更せずに中断し、衝突しない変更はそのまま引き継ぐ。

checkout の前に、失敗時の比較用として HEAD と作業ツリーの状態を記録する。

```bash
git rev-parse HEAD
git status --short
```

```bash
git checkout --detach origin/main
```

- 成功（exit 0）: 引き継いだ未コミット変更を `git status --short` で記録し、手順 11 で報告する
- 失敗（exit 0 以外）: git が出力した衝突パスを添えてユーザーへ報告し、手順 7 を停止する。`git rev-parse HEAD` と `git status --short` が checkout 前の記録と一致し、HEAD と作業ツリーが変わっていないことを確認する。手順 8 以降は続ける

衝突を `--force` / `--merge` / `git stash` / `git reset` で回避しない。

`items/`・`worlds/` は親で `.gitignore` 対象であり、親の checkout の更新では触れない。制作物は、その制作物の root で同じ手順を行う。

ローカル `main` branch は作成しない（プロジェクト規約）。

### 8. マージ済みローカル branch を削除する

手順 7 のあとで、手順 3 で取り込み済みと判定したローカル branch だけに実行する。`git branch -d` の成功だけを取り込みの証拠にしない（upstream に先端があれば未取り込みでも成功し得る）。その判定はメイン作業ツリーの未コミット変更に依存しないため、dirty なだけでは止めない。手順 7 が衝突で停止しても続ける。メイン作業ツリーが feature を checkout したままだと `git branch -d` は `used by worktree` / `checked out` で失敗する。

```bash
git branch -d <branch>
```

まず `-d` を使う。`-D` を許すのは、下の `not fully merged` に対する PR 型の例外だけである。

`-d` が拒否した場合、理由で分岐する。拒否を未マージと同一視しない。

- `used by worktree` または `checked out`: 未マージではない。手順 7 をまだ実行していなければ手順 7 に戻る。手順 7 が衝突で停止した場合は、メイン作業ツリーが checkout しているこの branch を削除せず報告する。他 worktree が掴んでいるならその worktree を残し、この branch は削除しない
- `not fully merged`: 手順 3 の証拠で切り分ける
  - **PR 型の例外:** 対応 PR が今も MERGED で、ローカル branch の先端がその `headRefOid` と一致し、`mergeCommit.oid` が `origin/main` の祖先であることを削除直前に確認できた場合だけ、`git branch -D <branch>` を使ってよい。Open PR に対応せず、どの worktree もその branch を checkout していないことも再確認する。先端が変わった・証拠が欠けた場合は削除せず残す。squash 取り込みでは履歴上の祖先でないため `-d` に拒否されても、この証拠で取り込み済みと判断できる。
  - PR 型を満たさず、`git log --oneline origin/main..refs/heads/<branch>` に出力がある: 削除しない。
  - `git log` が終了コード 0 で出力が空: `origin/main` には取り込み済みである。PR 型の例外も満たさなければ `-D` は使わず、削除を保留した理由を報告する。`git branch -d` は、upstream の remote-tracking ref があれば upstream と、無ければ HEAD と比較するため、次のどちらかで拒否される
    - upstream が branch の tip を含まない（別の branch を upstream にしている、マージ後に remote branch が書き換えられた）: 手順 7 の checkout が成功していても拒否され、HEAD が tip を含むと git は `even though it is merged to HEAD` の warning を出す。upstream が tip を含まないため削除を保留したと報告する
    - upstream の remote-tracking ref が無い（remote branch の削除後に prune されたもの、upstream 未設定のもの）うえに、手順 7 が衝突で停止して HEAD が古い `origin/main` のまま: HEAD が古いため削除を保留したと報告する

detached HEAD では次の warning が出るが、**削除が成功していれば正常**である。upstream にマージ済みであることは確認できており、拒否ではない。

```
warning: deleting branch '<branch>' that has been merged to
         'refs/remotes/origin/<branch>', but not yet merged to HEAD
```

### 9. pr-review の pinned ref を削除する

pr-review スキルはレビュー対象の PR head をローカル専用 ref `refs/pr-review/<PR番号>` に pin する。正常終了時は helper の `cleanup` が自セッションの commit を指す ref だけ削除する。別セッションが pin し直した ref や、中断・失敗したセッションの ref は残り得る。

手順 1 のオープン PR の番号に対応する ref は到達可能でも削除しない。残した ref の完全名をユーザーに報告する。その他の残存 ref について、指す commit が `origin/main` から到達可能かを確認する。

```bash
git merge-base --is-ancestor <sha> origin/main
```

オープン PR に対応しない ref が到達可能なら内容は `origin/main` に取り込み済みであり、pin を消しても失うものは無い。ローカル専用 ref でありゲート B の対象外なので、ユーザー確認なしに削除してよい。

```bash
git update-ref -d refs/pr-review/<PR番号>
```

到達不能な ref は削除しない。対応する PR がオープンなら進行中・中断したレビューの可能性があるため残し、マージ済みなのに到達不能なら（force-push 等で履歴から外れた場合）ユーザーに報告する。

### 10. ゲート B: リモート branch の削除はユーザー確認を得る

手順 8 でローカル branch を消すと対象名が手元から消える。手順 3 のマージ済み PR 一覧の `headRefName` から、実在する `refs/remotes/origin/<branch>` を独立に洗い出す。`origin/HEAD`・`origin/main` と Open PR に対応する branch は候補から除外する。

各候補の remote-tracking ref の先端を取得し、手順 3 と同じ判定をその先端に対して行う。

```bash
git rev-parse --verify refs/remotes/origin/<branch>
git log --oneline origin/main..refs/remotes/origin/<branch>
git merge-base --is-ancestor <mergeCommit.oid> origin/main
```

PR 型ではリモート側の先端が PR の `headRefOid` と一致し、`mergeCommit.oid` の到達確認が終了コード 0 のものだけを候補にする。`mergeCommit` が無い場合も祖先型なら候補にできる。ローカル branch を消せたことや、branch 名だけを根拠にしない。対応 PR を照合できない、どちらの判定も満たさないものは残す。候補には **PR 番号・リモート側の先端 SHA・判定区分**を添えて提示する。

マージ済みでも、リモート branch の削除を独断で実行しない。対象を提示してユーザー確認を求め、承認を得てから実行する。承認後にも fetch と Open PR・リモート側の先端の照合をやり直し、再照合で得た完全な SHA を控える。提示した SHA と違うものは削除せず、再判定・再確認する。削除は、控えた SHA を lease に渡して行う。

```bash
git push --force-with-lease=refs/heads/<branch>:<再照合した完全な SHA> origin :refs/heads/<branch>
git fetch --prune
```

lease は、再照合と削除 push の間に別の担当が先端を変えた場合に、承認していない先端を消さないためのものである。lease が拒否した（`stale info` などで終了コードが 0 以外）ときは、その branch を残し、lease なしの削除や `--force` で回避せず、fetch からやり直して再判定・再確認する。

### 11. 最終状態を報告する

```bash
git status
git worktree list
git branch -a
```

削除した対象、保留した対象、その理由を報告する。手順 7 で引き継いだ未コミット変更（`git status --short`）と、手順 7 が衝突で停止した場合の衝突パスも報告する。

手順 1 で控えた `prunable`・`locked` な worktree と、手順 4・5 で削除しなかった worktree は、パスと理由（`prunable` や `locked` の行の内容、`git -C` の終了コード、branch を持たない別 run の worktree であること）を添えて、「照合が必要」と報告する。移動済みの worktree は、所有者が移動先を確かめて `git worktree repair <移動先>` を個別に行う。本体が無い登録は、所有する run の停止と対応を確かめた所有者が、対象を限定して回収を判断する（`.devcontainer/README.md` の「完了・停止・保持」）。このスキルでは repair も回収も行わない。

`tmp/agents/` に残っているセッションディレクトリも報告する。

```bash
ls -la --time-style=long-iso "$(uv run --no-project python .agents/scripts/agent_tmp.py root)/tmp/agents/" 2>/dev/null
```

各エントリの完全パスと最終更新日時を報告する。削除はユーザーが項目ごとに確認した場合だけ行う。`tmp/agents/` 以外の `tmp/` 直下は報告の対象外であり、触らない。

## Red Flags: 手を止めてワークフローに戻る

- 「Merge 済みなので役目は終えている」を根拠に git 管理外ファイルを消そうとしている
- 「ディレクトリ名が `stale` / `tmp` / `pr-review-*` だから」を削除の根拠にしている
- 中身を保存先と照合せずに「軽微なメモ」「レビューのメタデータ」と分類している
- helper の strict cleanup を使わず `rm -rf` を実行しようとしているが、直前に `diff` または `cmp` を実行していない
- `Files ... differ` や `diff` の失敗を無視して「固有ファイルが無いから消してよい」としている
- `review-backup/` 欠落、無効な `session.json`、未知のトップレベルエントリがある候補を削除しようとしている、またはその候補だけを理由に独立して安全な cleanup まで停止しようとしている
- 手順 5 の削除対象に当たらない worktree を、「実体が無い」「`prunable`」「HEAD が `origin/main` から到達可能」を根拠に消そうとしている
- 一括 `git worktree prune` の代わりに、`prunable` な登録を `git worktree remove` で 1 件ずつ消そうとしている
- 「branch の先端が `origin/main` の祖先でないから未取り込み」と決め、PR 型の証拠を確認していない
- PR が MERGED というだけで、先端と `headRefOid` の一致や `mergeCommit` の到達可能性を確かめずに `-D` を使おうとしている
- リモート branch を削除してから「不要なら戻せます」と報告しようとしている
- 再照合した SHA の lease なしでリモート branch を削除しようとしている
- 「時間が無い」「手早く」と言われたので確認を飛ばそうとしている

**いずれかに該当したら、対象の削除を中止する。残骸候補は手順 6 で報告・保留し、独立して安全性を確認できた操作は続行する。**

## 言い訳と現実

| 言い訳 | 現実 |
|---|---|
| 「PR は Merge 済みだから、レビュー成果物の役目も終わった」 | マージされたのは追跡対象のコードだけである。git 管理外のレビュー結果は 1 バイトもマージされていない |
| 「ディレクトリ名が stale なので消してよい」 | 名前は中身を保証しない。照合していないなら未保存データの有無を知らないだけである |
| 「中を見たら軽微なメモだった」 | 軽微だと判断したのではなく、保存先と照合していないだけである。照合は `diff -r` 1 回で済む |
| 「内容差分があっても固有ファイルが無ければ失うものはない」 | 同名の未保存稿は `Files ... differ` になる。`previous-review.md` だけをコピーして消すと新しい原稿が消える |
| 「worktree は clean だったのでデータ損失はない」 | `git status` は gitignore 対象ファイルも、worktree の外にある残骸も見ない |
| 「`prunable` で実体が無いので、登録を消しても失うものは無い」 | `prunable` は登録パスに接続できないという情報である。本体を移動しただけでも出る。親ディレクトリが消えた登録も、所有する run が照合を待っていることがある。消した登録は repair で戻せない |
| 「HEAD が `origin/main` から到達可能で clean なので、detached な worktree を消しても失うものは無い」 | 追跡内容は失わなくても、別の run が使っている作業場所と登録を奪う。branch を持たない worktree の所有者は Git から分からない。例外は、親の `session.json` で対応する PR を確かめられる `/tmp/pr-review-*` だけである |
| 「一括 prune は危ないので、消してよい登録だけを `git worktree remove` で個別に消せばよい」 | 存在しないパスへの `remove` も、prune と同じく登録を消す。どの登録を消すかは所有者が照合して決める。片付けの手順では決めない |
| 「消した後で問題があれば言ってもらえばいい」 | `rm -rf` に取り消しは無い。削除の前だけが確認である |
| 「マージ済み branch の削除は cleanup の一環」 | ローカルはそうである。リモートは他人が見る共有物であり、外向きの操作である |
| 「`origin/main..<branch>` に出力があるから未取り込み」 | squash では commit が作り直される。MERGED PR の `headRefOid` と先端が一致し、`mergeCommit` が `origin/main` の祖先なら取り込み済みと判定できる |
| 「手早くと言われた」 | 照合は数秒で終わる。失ったレビューの再生成はそれより桁違いに高い |
| 「untracked が出るのは仕方ない。毎回訊けばよい」 | 訊かれるのは、判定不能な生成物を作り続けている上流の手順があるからである。生成側を直せば訊く必要そのものが無くなる |
| 「毎回同じ確認で面倒なので `.gitignore` に入れる」 | `git status` から消えるだけで、復元不能なファイルであることは変わらない。gitignore 対象は本スキルのガードレールで照合なしの削除を禁じており、判定不能なまま残り続ける |
| 「メイン作業ツリーが dirty なので、origin/main への更新も branch 削除も止める方が安全」 | `--force` / `--merge` なしの `git checkout` は、衝突すると何も変更せずに中断する。失うかどうかは dirty かどうかではなく checkout の成否で決まる。一律に止めると、失うものが無い整理まで止まる |

## ガードレール

- ローカル `main` branch を作成しない。
- `git branch -D` は、手順 8 の `not fully merged` に対し、MERGED PR の `headRefOid` とローカル先端の一致・`mergeCommit` の到達可能性を再確認できた例外だけに使う。マージ判定や worktree の保護を握り潰すために使わない。
- clean を確認していない worktree に `--force` を付けない。
- 対象を限定しない `git worktree prune` を実行しない。
- `prunable`・`locked` な worktree と、branch を checkout していない別 run の worktree（手順 5 の `/tmp/pr-review-*` を除く）を `git worktree remove` しない。
- 手順 7 の checkout の衝突を `--force` / `--merge` / `git stash` / `git reset` で回避しない。衝突したら停止して報告する。
- `items/`・`worlds/` の中身と、制作物の未コミットの変更を片付けの対象にしない。親の worktree の `items/`・`worlds/` に中身があれば、その worktree を削除しない（手順 4）。
- ユーザー確認なしに `git push`（`--delete` を含む）を実行しない。
- リモート branch は、再照合した完全な SHA の lease（`--force-with-lease=refs/heads/<branch>:<SHA>`）を付けて削除する。lease の拒否を lease なしの削除や `--force` で回避しない。
- Open PR が残っている branch と worktree、および対応する `refs/pr-review/*` を削除しない。detached な pr-review worktree も含む。
- `reviews/` や `docs_draft/` など gitignore 対象ディレクトリの内容を、照合せずに削除しない。例外は、helper が strict cleanup で検証した停止確認済み未着手の失敗セッション、このスキルが作った `$GATE_DIR`、ユーザーが項目ごとに確認した `tmp/agents/` のエントリだけである。
- `tmp/agents/` のエントリを、ユーザーが項目ごとに確認する前に削除しない。
- コンフリクト解消、rebase、リリース作業は行わない。

## 関連スキル

- `pr-review` — `/tmp/pr-review-*/` の使い捨て worktree、`review-backup/`、`refs/pr-review/<PR番号>` の pinned ref を作る側。手順 6 の残骸と手順 9 の ref はこのスキルの生成物である

## ランタイムごとの違い

- Claude Code では、ユーザー確認（手順 6 の上書きの確認、手順 10 のゲート B、手順 11 の `tmp/agents/` の削除）に `AskUserQuestion` を使う
- frontmatter の `allowed-tools` の値は Claude Code の許可の一覧の書式で書いている。Claude Code は、ここに挙げたツールを許可の確認なしで実行する。Devin も同じ値を読み、自分のツール名と一致する名前だけを自動で承認する（このスキルでは read・grep・glob。`devin skills show repo-cleanup-after-merge` で確かめた）。どちらのランタイムも、一覧に無いツールを使えなくするわけではない。ほかのランタイムでの扱いは確認していない
