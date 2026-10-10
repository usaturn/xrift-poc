---
name: agent-handoff
description: エージェント間で作業を受け渡すときに使う。渡す側 —「区切りのいい所で停止して引き継ぎ資料を作って」「別のエージェントに実装させる」「この Spec を渡せば足りるか」「handoff を書いて」「hand this off」「write a handoff」と言われたとき。受け取る側 — 他のエージェント（Codex / Grok / 別セッションの Claude Code）が superpowers・brainstorming・writing-plans・review-fix-planning・subagent-driven-development・python-tdd-team で進めた作業を「引き継いで」「続きをやって」「どこまで終わったか確認して」と言われたとき。tmp/docs_draft/claude_handoffs/ の handoff アーティファクトを読み書きし、決定済みの裁定・触ってはいけない範囲・検証手順・反映先まで引き渡す
allowed-tools: Read, Grep, Glob, Skill, Bash(ls *), Bash(cat *), Bash(git *), Bash(gh *), Bash(uv run *), Write(tmp/docs_draft/claude_handoffs/**), Edit(tmp/docs_draft/claude_handoffs/**), Write(docs_draft/superpowers/**), Edit(docs_draft/superpowers/**)
---

# エージェント引き継ぎ

## 概要

handoff はエージェント間で作業を移す。引き継ぎ先のエージェントは**コールドスタート**である。持っているのはリポジトリと、こちらが書き残したものだけで、それ以外は何も無い。このスキルは受け渡しの両端を扱う。作業を止める側の **handoff を書く**手順と、続きを引き取る側の **handoff を読む**手順である。

アーティファクトは、親リポジトリの main worktree（`uv run --no-project python .agents/scripts/agent_tmp.py root` が出力するディレクトリ）の `tmp/docs_draft/claude_handoffs/` に置く Markdown と JSON の対で（linked worktree の中には置かない）、スキーマは `references/handoff-schema.md` にある（どのランタイムもこの 1 ファイルを共有し、複製を持たない）。書く前・解釈する前に必ず読む。

**引き継ぎが失敗する原因は、要約の不足ではないことがほとんどである。** 差分ならコールドなエージェントでも読み直せる。読み直せないのは「何を決めて、何を棄却したか」である。高くつく失敗は次の 4 つに集中する。決着済みの判断を蒸し返す、意図的に除外した指摘に手を出す、独自の検証を発明して実環境に対して走らせる、二本目の PR を開く。`decisions` / `out_of_scope` / `verification` / `landing` はこの 4 つのためだけに存在する。

## どちらの向きか

- ユーザがこちらを止めている、または別のエージェントが使う資料を求めている → **handoff を書く**
- ユーザが他のエージェントの作業の続きを求めている → **handoff を読む**

判別できないときは聞く。逆向きに始めると、読むべき場面で書きに行けば 1 ターン無駄になるだけだが、書くべき場面で読みに行くと決定が失われる。

## handoff を書く

1. **区切りを決めて、それを明記する。** 編集の途中で書いた handoff は、handoff が無いより悪い。作業単位を完了させるか元に戻すかを選び、どちらにしたかを `session.stop_reason` に書く。適用途中の編集を作業ツリーに残すなら、冒頭の節で必ず宣言する。書かなければ受け手は「作業ツリー＝ `git.head`」と仮定して踏む。

2. **理屈の置き場所を決める。** 長い論証は Spec / Plan 側（本リポジトリでは `docs_draft/superpowers/`）に置き、handoff は 1 行の裁定だけを持って `sources.spec` で指す。両方に書くと必ず食い違う。Spec が無いときは `sources.spec` を `null`（JSON）/ `none`（Markdown）にし、理由を書く。存在しないパスを捏造しない。

3. **対の 2 ファイルを手書きする。** main worktree の `tmp/docs_draft/claude_handoffs/<timestamp>-<session>.handoff.md` と同名の `.handoff.json` を `references/handoff-schema.md` に従って手書きする。

4. **自分にしか書けないフィールドを埋める。** 次の 5 つは自分で書く。

   - `decisions` — 決着済みの判断すべて。disposition は `fix` / `skip` / `deferred` / `resolved` と 1 行の理由。**`skip` の項目は `fix` と同じくらい重要である。** 書かなければ「対応しない」が「まだやっていない」に見える。
   - `out_of_scope` — 拘束力を持つ。自明に思えても書く。特に紛らわしいもの、つまり「同じ修正が要りそうな隣のファイル」「ついでに直せそうなリファクタ」「ここでは完了条件でないテストスイート」を名指しする。
   - `verification` — 実行するコマンドと、それに必要な隔離。`isolation` を落とすと、設定まわりのバグを再現しようとした受け手がユーザの実環境に対して再現してしまう。
   - `landing` — 反映先のブランチ、既存 PR、やってはいけないこと（新規 PR、`*-review-fixes` のような別ブランチ、ここでは適用しないゲート）、リポジトリが要求する commit trailer。
   - `git.worktree` / `git.branch` / `git.head` / `git.status` — 実際に作業がある場所と、そのチェックアウトの branch / HEAD / status。親チェックアウトが detached のときや別のブランチにあるときは、両方を書く。片方しか書かないと、受け手はこちらのコマンドを親チェックアウトで走らせて別のツリーを編集する。

5. **手書きで落としがちな項目を埋める。** 次の 3 つが定番の穴になる。穴を残したまま渡さず、埋める。

   - **todo の機能を使わなかったセッションには `tasks` が無い。** 作り話をせず、無いと書く。
   - **エディタのツールではなくシェルの heredoc で書いたファイルは file-history に残らず、`sources.plan_files` に出てこない。** 明示的に書き足す。
   - **`git` は worktree ではなく `project_dir` を記録することがある。** worktree を手で書く。

   ランタイムごとの具体的な現れ方は「ランタイムごとの違い」の節にある。

6. **渡す前に自己点検する。** `references/handoff-schema.md` の必須フィールドが全部あるか（`sources.spec` と `landing.pr` は `null` でもよい）。`null` でないパスが全部実在するか。**このアーティファクトとリポジトリだけで受け手が着手できるか。** チャットで補足質問に答える必要が残るなら、その答えは handoff に入るべき内容である。

## handoff を読む

1. **既存を探す:**

   ```bash
   ls -t "$(uv run --no-project python .agents/scripts/agent_tmp.py root)/tmp/docs_draft/claude_handoffs/"*.handoff.md 2>/dev/null | head
   ```

2. **合うものが無ければ、続きに入る前に作る。** 確認できた範囲で `references/handoff-schema.md` に従って書き起こし、埋まらない箇所は推測せずユーザに確認する。

3. **最新の `.handoff.md` を読む。** 正確なフィールドが要るときは対の `.handoff.json` も読む。`schema_version` を確認する。version 1 のアーティファクトには `decisions` / `out_of_scope` / `verification` / `landing` が無く、その不在は「空」ではなく **「不明」** を意味する。

4. **動く前に状態を報告する。** セッションとエージェント、検出したスキル、現在のフェーズ／ゲート、完了・未完了のタスク、決定済みの裁定、対象外の範囲、検証の証跡、反映先。

5. **handoff の状態からのみ続ける。** `out_of_scope` は拘束力を持つ。小さく勝てそうに見えても `skip` の判断を開け直さない。誤っていると考えるなら、そう述べて止まる。handoff が自分の記憶と食い違うときは handoff を採る。`verification.commands` を指定された隔離のもとで走らせ、自分の習慣ではなく `landing` の指定どおりに作業を完了させる。

## アーティファクトの作成

対の 2 ファイルを `references/handoff-schema.md` に従って手書きする。

## 検証

`quick_validate.py` はグローバルにインストールされた skill-creator のものを使う。`<skill-creator-path>` はランタイムごとに次のように解決する。

- Codex: `~/.codex/skills/.system/skill-creator`
- Claude Code・Devin: 有効化済みプラグイン（example-skills）同梱のコピーを使う。`find ~/.claude -name quick_validate.py` で見つかるパスの 2 階層上が skill-creator ディレクトリである
- Pi: `@howaboua/pi-skill-skill-creator` には `quick_validate.py` が無い。代わりに `~/.pi/agent/npm/node_modules/@howaboua/pi-skill-skill-creator/skills/skill-creator/scripts/skill-efficiency-check.py` を同じ引数で実行する

検証のコマンド:

- `uv run python <skill-creator-path>/scripts/quick_validate.py .agents/skills/agent-handoff`
- `uv run pytest .agents/skills/agent-handoff/tests -q` — スキルが引き継ぎの仕様を述べていること、スキルが参照するパスが実在することを検査する

## 参照

- `references/handoff-schema.md`: スキーマ本体、必須フィールド、version 1 互換
- `evals/evals.json`: 書く側・読む側の両方向の振る舞いの確認
- `evals/trigger_queries.json`: トリガーの範囲

## ランタイムごとの違い

### Claude Code

- `.claude/worktrees/` 配下で作業し、親チェックアウトは detached のことが多い。「handoff を書く」の手順 4 の `git.*` には両方を書く。
- 手書きで落としがちな項目（「handoff を書く」の手順 5）は、次の形で現れる。
  - **TodoWrite を使わなかったセッションには `tasks` が無い。** `tasks` は Claude Code の task reminder を元にするため、todo を開かずに進めたセッションでは空になる。空なら空と書き、作り話をしない。
  - **Bash の heredoc で書いたファイルは file-history に載らない。** `Write` / `Edit` を経ていないファイルはスナップショットに現れず `sources.plan_files` に出てこない。Spec をシェル経由で作ったなら手で `sources.spec` に書く。
  - **サブエージェント（Agent ツール）に委譲した作業の根拠は、このセッションのログにしか無い。** サブエージェントの最終レポートはユーザにも次のエージェントにも見えない。使った判断は `decisions` と `verification.evidence` に転記する。
  - **`git` は `project_dir` を見る。** 親チェックアウトが `origin/main` の detached HEAD であれば、実作業ブランチではなくそれが記録される。手で直す。

### frontmatter の `allowed-tools`

`allowed-tools` の値は Claude Code の許可の一覧の書式で書いている。Claude Code は、ここに挙げたツールを許可の確認なしで実行する。Devin も同じ値を読み、自分のツール名と一致する名前だけを自動で承認する（このスキルでは read・grep・glob。`devin skills show agent-handoff` で確かめた）。どちらのランタイムも、一覧に無いツールを使えなくするわけではない。ほかのランタイムでの扱いは確認していない。

### ほかのランタイム

本文の一般形に従う。
