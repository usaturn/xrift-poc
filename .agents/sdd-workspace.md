# superpowers の SDD の作業領域

superpowers の executing-plans と subagent-driven-development（以下 SDD）は、Plan ごとの作業領域に ledger・brief・実装担当の報告・review の差分・テストのログを置く。この文書は、その作業領域を feature worktree の中に作らずに使う手順と、片付けの時点、誤って作ったときの扱いを定める。置き場所そのものは AGENTS.md の「コミットしないファイルの置き場所」に定義する。superpowers の版やランタイムによらず、この手順に従う。

## superpowers の既定の置き場所を使わない理由

- superpowers の `sdd-workspace` は、作業領域を `git rev-parse --show-toplevel` の下の `.superpowers/sdd/<plan>/` に作る。置き場所を変える設定は無い。executing-plans の `task-start`・`task-done` と、出力先を渡さない `task-brief`・`review-package` も、内部でこれを使う
- feature worktree で実行すると、作業領域は worktree の中にできる。Plan の作業領域を消しても `.superpowers/sdd/.gitignore` が残る。repo-cleanup-after-merge の手順 4 は、再生成できる依存・キャッシュ以外の無視したファイル（`!! .superpowers/`）がある worktree を消さないので、マージ後の片付けが毎回止まる
- 独立した Git リポジトリの制作物では、`.superpowers/` を無視していなければ未追跡のファイルになる

このため、どこで実行するときも `sdd-workspace`・`task-start`・`task-done` を使わず、`task-brief`・`review-package` には出力先（最後の引数 `OUTFILE`）を渡す。

## 作業領域を作る

- Plan ごとに `uv run --no-project python .agents/scripts/agent_tmp.py mkdir <topic>` で 1 つ作る（`<topic>` は例えば `issue-<N>-sdd`）。出力された `tmp/agents/<topic>.XXXXXX/` を、そのまま Plan の作業領域にする。中に `.superpowers/sdd/<plan>/` の階層は作らない。以下、この完全パスを `$SDD` と書く
- `$SDD` の完全パスを、Plan の冒頭（最初の Task の見出しより前）に書く。compaction の後や、別の担当が引き継ぐときに、ここから ledger を探す
- ledger は `$SDD/progress.md`。1 行目は superpowers と同じく `# SDD ledger — plan: <Plan のパス>` とする。`docs_draft/superpowers/` の Plan は worktree の外にあるので、絶対パスで書く。`Ruling:`・`Final: minor (deferred):` などの行も superpowers のスキルの記述どおりに書く
- `agent_tmp.py` が失敗して作業領域を作れないときは、worktree の中に作らずに止まり、理由を報告する

## superpowers の手順との対応

| superpowers の手順 | ここでの手順 |
|---|---|
| `sdd-workspace PLAN_FILE` | 実行しない。上で作った `$SDD` を使う |
| executing-plans の `task-start PLAN_FILE N` | `task-brief PLAN_FILE N "$SDD/task-N-brief.md"` で brief を書き、`git rev-parse HEAD` の値を BASE として控える |
| executing-plans の `task-done PLAN_FILE N BASE -- <command>` | `<command>` の出力（標準エラー出力を含む）を `$SDD/task-N-tests.log` に書く。終了コードが 0 のときだけ、ledger に完了の行 `Task <N>: complete (commits <base7>..<head7>, tests: <command> → <result>)` を足す。`<base7>`・`<head7>` は BASE と HEAD の `git rev-parse --short=7`、`<result>` はログの空でない最後の行である。0 以外なら何も足さない（その Task は完了していない） |
| SDD の `task-brief PLAN_FILE N` | 出力先に `"$SDD/task-N-brief.md"` を渡す |
| SDD の実装担当の報告のファイル | dispatch に `"$SDD/task-N-report.md"` を書く |
| `review-package PLAN_FILE BASE HEAD` | 出力先に `"$SDD/review-<base7>..<head7>.diff"`（BASE と HEAD の `git rev-parse --short`）を渡す |
| 長いテストの出力をファイルに出す | `$SDD` の中に書く |
| 最後の全体レビューの後に Plan の作業領域を消す | その時点では消さない（「片付け」） |

`task-brief`・`review-package` は superpowers の `subagent-driven-development/scripts/` にある。スキルの実ディレクトリはランタイムごとに違うので、読み込んだスキルの base directory から辿る。展開の仕方によって実行ビットが無いことがあるので、`bash <スクリプトのパス> ...` で実行する。

## 再開

- Plan の冒頭のパスの ledger を読み、superpowers の手順どおり、`Task <N>: complete` の行がある Task を飛ばして再開する
- パスが無ければ、作業領域が失われている（消すのは run の終わりだけである）。superpowers の「作業領域を失ったら `git log` から再開する」に従い、新しい作業領域を作って Plan の冒頭を書き換え、ledger に `Ruling:` を書いて続ける
- issue-to-pr の引き継ぎ（同スキルの 3 章「issue-queue の引き継ぎ」）では、前の attempt の作業領域は読むだけにする。新しい作業領域を作り、Plan の冒頭のパスを書き換える

## 片付け

superpowers は、最後の全体レビューの後に Plan の作業領域を消してから `finishing-a-development-branch` に進む。この時点では消さない。PR 本文はまだ無い。issue-to-pr の `--merge` では、最後の全体レビューを PR 修正ループで代えるので、この時点そのものが来ない。

- PR を作る run: PR を作り、ledger の `Ruling:` と `minor (deferred)` の行を PR 本文に写した後に消す。PR 修正ループを回すときは、ループが終わった後に消す
- PR を作らない run: それらの行を最終報告に写した後に消す
- 消す前に、中身が自分の作ったファイルだけであることを確かめる。消すときは `rm -rf -- "${SDD:?}"` のように空値ガードを付ける
- 止まって終わる run（停止の報告をする run）では消さず、完全パスを報告する。issue-queue の子は、最終報告の `retained` に載せる。後で参照するファイル（検証のログなど）を残すときも、消さずに完全パスを最終報告に書く

## worktree の中にできてしまったとき

superpowers のスキルの手順をそのまま実行すると、Git の root（`git rev-parse --show-toplevel`。以下 `<root>`）の下に `.superpowers/sdd/` ができる。気付いたら、PR を作る前に次の順に行う。

1. `find "<root>/.superpowers" -mindepth 1` で中身を見る
2. この Plan の作業領域（`sdd/<名前>/plan-path` がこの Plan を指すディレクトリ）の中のファイルを `$SDD` に移し、そのディレクトリを消す。両方に ledger があれば、`.superpowers` 側の ledger の 2 行目以降を `$SDD/progress.md` に追記して 1 つにする
3. `sdd/` に `.gitignore`（`*` の 1 行）しか残らなければ、`<root>/.superpowers/sdd` を消す。`<root>/.superpowers` が空になれば `rmdir` で消す
4. ほかのもの（別の Plan の作業領域、brainstorming の visual companion の `brainstorm/` など）には触らず、残ったものの完全パスを最終報告に書く。repo-cleanup-after-merge は、その worktree の片付けを利用者の判断に回す

## Git 未初期化の制作物

置き場所の規則だけを適用する。制作物では commit を作らない（AGENTS.md の「Git・PR」）。`git rev-parse HEAD` は親の main checkout の HEAD を返すので、完了の行の commits の欄と `review-package` は意味を持たない。この環境向けの代わりの手順は定めない。

## 関係する規則

- issue-queue の子への依頼文（`.agents/skills/issue-queue/scripts/issue_queue/prompts.py`）は、同じ規則（親の main checkout の `tmp/agents` に置き、worktree の中に `.superpowers` を作らない）を子に伝える
- repo-cleanup-after-merge の手順 4 のゲートは緩めない。`.superpowers/` を再生成できる依存・キャッシュの一覧に足さず、`.gitignore` で黙らせない（同スキルの「毎回ここで止まるなら、直すのは片付け側ではなく生成側である」）
- 親の `.gitignore` の `.superpowers/` は、brainstorming の visual companion のために残す

## ランタイムごとの違い

- **Pi:** `/sp-implement`・`/sp-implement-parallel`（`@teelicht/pi-superagents`）の root prompt は、upstream の `subagent-driven-development` スキルが作業領域と ledger のパスを決め、それを組み立て直さないよう指示する。このリポジトリでは、AGENTS.md の規則とこの文書を優先する。`/sp-implement-parallel` は Task ごとに一時 worktree を作るが、brief・報告・review の差分は同じ `$SDD` に書く（名前が Task・範囲ごとに分かれるので重ならない）。ledger は controller だけが書く
- **superpowers の版:** 古い版の executing-plans には `task-start`・`task-done` が無い。上の対応表の手順は `task-brief`・`review-package` と git だけを使うので、版によらず使える
