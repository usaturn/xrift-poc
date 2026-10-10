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

整理対象の PR が属するリポジトリで root を決める。`repository-ownership` の判定（`route_target.py`）で、親（`usaturn/xrift-poc`）なら親の checkout、制作物ならその制作物の `git_root` が root である。制作物の片付けは、その制作物の checkout の中だけで行い、親の checkout から `items/`・`worlds/` の中身を消したり動かしたりしない。`gh` には毎回 `--repo` を付ける。

## ワークフロー

### 1. 何も消す前に状態を把握する

次の read-only コマンドを実行する。

```bash
git status
git worktree list --porcelain
git branch -vv
git remote -v
git for-each-ref refs/pr-review
gh pr list --state open --json number,title,headRefName,headRefOid
gh pr list --state all --limit 20 --json number,title,state,headRefName,mergedAt
```

`gh` が非0終了した場合、PR の状態は不明である。マージ判定を推測で進めず、エラーを報告して停止する。

Open PR が 1 件以上ある場合、次は **整理対象外**である。残存 PR をユーザーへ報告し、それ以外だけを進める。

- `headRefName` に一致する branch
- その branch をチェックアウトしている worktree
- `git worktree list --porcelain` の `HEAD` が、オープン PR の `headRefOid` と一致する worktree（detached でも対象外）
- パスが `/tmp/pr-review-*` の worktree と、その親ディレクトリ。親の `session.json` の `head_branch` がオープン PR の `headRefName` である場合も含む
- オープン PR の番号に対応する `refs/pr-review/<PR番号>`（手順 9 の到達可能性判定でも残す）

### 2. リモートを取り込む

```bash
git fetch --all --prune
```

**マージ判定より前に必ず実行する。** ローカルの `origin/main` は古いのが普通であり、fetch 前の「マージ済みか」の判定は誤る。

### 3. マージ済みか判定する

branch ごとに次を実行する。

```bash
git log --oneline origin/main..<branch>
```

- 出力が空: `origin/main` に取り込み済みである。`gh pr list --state all` の対応する要素が `"state": "MERGED"` であることも確認する。
- 出力がある: 未取り込みである。この branch と対応する worktree は削除しない。

### 4. worktree ごとに clean を確認する

```bash
git -C <worktree の絶対パス> status --short
```

`-C` には絶対パスを渡す。worktree 隔離セッションでは相対パスが拒否されることがある。出力がある worktree は **削除しない**。ユーザーへ報告して判断を仰ぐ。

#### 毎回ここで止まるなら、直すのは片付け側ではなく生成側である

untracked ファイルは保存先と照合する述語を持たないため、ゲート A の対象として必ず人間の判断を要求する。同じ種類のファイルで毎回止まるのは、**worktree 内に判定不能な生成物を残す手順が上流にある**ということである。ゲートを緩めて解決してはならない。

報告時は「どのスキル・どの手順の生成物か」を添えて指摘し、生成側を次の原則へ合わせるよう促す。

**生成物は worktree の外（親の main worktree の `tmp/agents/`。`uv run --no-project python .agents/scripts/agent_tmp.py mkdir <topic>` で作る）に出す。** untracked が発生しないので手順 4 で止まらない。本スキルは worktree 直下の untracked を機械的に照合する述語を持たない。手順 6 のゲート A は `session.json` を持つ残骸ディレクトリ専用であり、worktree 内に残す限り毎回ユーザー確認になる。

`.gitignore` への追加で黙らせてはならない。gitignore 対象は本スキルのガードレールで照合なしの削除を禁じており、`git status` からも消えるため、照合する術が無いまま残り続ける。

### 5. worktree を削除する

手順 1 で対象外にした worktree（オープン PR の `headRefOid` と HEAD が一致するもの、`/tmp/pr-review-*` で親 `session.json` の `head_branch` がオープン PR と一致するもの）は削除しない。

手順 4 で clean を確認済みの worktree だけを削除する。

```bash
git worktree remove <path>
git worktree prune
```

worktree 本体の削除と、手順 6 の残骸削除は **別の判断**である。worktree 本体は追跡済みの内容であり、マージ済み・clean を確認していれば削除してよい。手順 6 で保留が発生しても、worktree 本体の削除まで止める必要はない。

### 6. ゲート A: 残骸ディレクトリは照合してから消す

`git worktree remove` は作業ツリーだけを消す。親ディレクトリに残る `session.json` や `review-backup/` は **git 管理外**であり、`git status` にも `git worktree list` にも現れない。

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

手順 7 のあとで実行する。`git branch -d` の判定はメイン作業ツリーの未コミット変更に依存しないため、dirty なだけでは止めない。手順 7 が衝突で停止しても続ける。メイン作業ツリーが feature を checkout したままだと `git branch -d` は `used by worktree` / `checked out` で失敗する。

```bash
git branch -d <branch>
```

`-D` は使わない。

`-d` が拒否した場合、理由で分岐する。拒否を未マージと同一視しない。

- `used by worktree` または `checked out`: 未マージではない。手順 7 をまだ実行していなければ手順 7 に戻る。手順 7 が衝突で停止した場合は、メイン作業ツリーが checkout しているこの branch を削除せず報告する。他 worktree が掴んでいるならその worktree を残し、この branch は削除しない
- `not fully merged`: 削除しない。`git log --oneline origin/main..<branch>` で切り分ける
  - 出力がある: 未マージである。手順 3 の判定が誤っている
  - 出力が空: `origin/main` には取り込み済みである。未マージとも手順 3 の誤りとも扱わず、削除を保留した理由を報告する。`git branch -d` は、upstream の remote-tracking ref があれば upstream と、無ければ HEAD と比較するため、次のどちらかで拒否される
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

手順 8 でローカル branch を消すと対象名が手元から消える。リモート側から独立に洗い出す。

```bash
git branch -r --merged origin/main | grep -vE 'origin/(HEAD|main)$'
```

候補が複数ある場合は、手順 1 で取得した `gh pr list --state all` の `headRefName` と突き合わせ、**どの PR 由来か**を添えて提示する。対応する PR が見つからないリモート branch は候補から外し、そのまま残す。

マージ済みでも、リモート branch の削除を独断で実行しない。対象を提示してユーザー確認を求め、承認を得てから実行する。

```bash
git push origin --delete <branch>
git fetch --prune
```

### 11. 最終状態を報告する

```bash
git status
git worktree list
git branch -a
```

削除した対象、保留した対象、その理由を報告する。手順 7 で引き継いだ未コミット変更（`git status --short`）と、手順 7 が衝突で停止した場合の衝突パスも報告する。

`tmp/agents/` に残っているセッションディレクトリも報告する。

```bash
ls -la --time-style=long-iso "$(uv run --no-project python .agents/scripts/agent_tmp.py root)/tmp/agents/" 2>/dev/null
```

各エントリの完全パスと最終更新日時を報告する。削除はユーザーが項目ごとに確認した場合だけ行う。`tmp/agents/` 以外の `tmp/` 直下は報告の対象外であり、触らない。

## Red Flags: 手を止めてワークフローに戻る

- 「Merge 済みなので役目は終えている」を根拠に git 管理外ファイルを消そうとしている
- 「ディレクトリ名が `stale` / `tmp` / `pr-review-*` だから」を削除の根拠にしている
- 中身を保存先と照合せずに「軽微なメモ」「レビューのメタデータ」と分類している
- `rm -rf` を実行しようとしているが、直前に `diff` または `cmp` を実行していない
- `Files ... differ` や `diff` の失敗を無視して「固有ファイルが無いから消してよい」としている
- `review-backup/` 欠落、無効な `session.json`、未知のトップレベルエントリがある候補を削除しようとしている、またはその候補だけを理由に独立して安全な cleanup まで停止しようとしている
- リモート branch を削除してから「不要なら戻せます」と報告しようとしている
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
| 「消した後で問題があれば言ってもらえばいい」 | `rm -rf` に取り消しは無い。削除の前だけが確認である |
| 「マージ済み branch の削除は cleanup の一環」 | ローカルはそうである。リモートは他人が見る共有物であり、外向きの操作である |
| 「手早くと言われた」 | 照合は数秒で終わる。失ったレビューの再生成はそれより桁違いに高い |
| 「untracked が出るのは仕方ない。毎回訊けばよい」 | 訊かれるのは、判定不能な生成物を作り続けている上流の手順があるからである。生成側を直せば訊く必要そのものが無くなる |
| 「毎回同じ確認で面倒なので `.gitignore` に入れる」 | `git status` から消えるだけで、復元不能なファイルであることは変わらない。gitignore 対象は本スキルのガードレールで照合なしの削除を禁じており、判定不能なまま残り続ける |
| 「メイン作業ツリーが dirty なので、origin/main への更新も branch 削除も止める方が安全」 | `--force` / `--merge` なしの `git checkout` は、衝突すると何も変更せずに中断する。失うかどうかは dirty かどうかではなく checkout の成否で決まる。一律に止めると、失うものが無い整理まで止まる |

## ガードレール

- ローカル `main` branch を作成しない。
- `git branch -D` でマージ判定を握り潰さない。
- clean を確認していない worktree に `--force` を付けない。
- 手順 7 の checkout の衝突を `--force` / `--merge` / `git stash` / `git reset` で回避しない。衝突したら停止して報告する。
- `items/`・`worlds/` の中身と、制作物の未コミットの変更を片付けの対象にしない。
- ユーザー確認なしに `git push`（`--delete` を含む）を実行しない。
- Open PR が残っている branch と worktree、および対応する `refs/pr-review/*` を削除しない。detached な pr-review worktree も含む。
- `reviews/` や `docs_draft/` など gitignore 対象ディレクトリの内容を、照合せずに削除しない。例外は、このスキルが作った `$GATE_DIR` と、ユーザーが項目ごとに確認した `tmp/agents/` のエントリだけである。
- `tmp/agents/` のエントリを、ユーザーが項目ごとに確認する前に削除しない。
- コンフリクト解消、rebase、リリース作業は行わない。

## 関連スキル

- `pr-review` — `/tmp/pr-review-*/` の使い捨て worktree、`review-backup/`、`refs/pr-review/<PR番号>` の pinned ref を作る側。手順 6 の残骸と手順 9 の ref はこのスキルの生成物である

## ランタイムごとの違い

- Claude Code では、ユーザー確認（手順 6 の上書きの確認、手順 10 のゲート B、手順 11 の `tmp/agents/` の削除）に `AskUserQuestion` を使う
- frontmatter の `allowed-tools` の値は Claude Code の許可の一覧の書式で書いている。Claude Code は、ここに挙げたツールを許可の確認なしで実行する。Devin も同じ値を読み、自分のツール名と一致する名前だけを自動で承認する（このスキルでは read・grep・glob。`devin skills show repo-cleanup-after-merge` で確かめた）。どちらのランタイムも、一覧に無いツールを使えなくするわけではない。ほかのランタイムでの扱いは確認していない
