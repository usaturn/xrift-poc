---
name: issue-to-pr
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

## 2. 入力・所有者・競合の確認

1. **必須:** `repository-ownership` と作業対象に適用される AGENTS.md を読む。`repository-ownership` の判定（`route_target.py`）を実行する。Issue の内容による担当と入力先が一致しなければ停止し、別 repo の同番号を推測しない。
2. スキルの実ディレクトリを `SKILL_DIR`、親 main worktree を `PARENT` とする（`$XRIFT_POC_ROOT`。無ければ親 checkout の `.agents/scripts/agent_tmp.py root`）。現在の checkout を cwd として、読み取り専用 helper を実行する。

   ```bash
   uv run --project "$PARENT/tools/devenv" --locked python "$SKILL_DIR/scripts/resolve_issue.py" "$INPUT"
   ```

   - URL: `https://github.com/usaturn/xrift-poc/issues/N`、または現在の場所を `product` と判定した制作物の Issue の URL。
   - `#N`: 現在の場所の判定（`env` なら `usaturn/xrift-poc`、`product` ならその `repository`）で解決する。`stop` や `repository` が null なら停止する。
   - 成功: exit 0、`repo/number/url/title/body` の JSON。失敗: exit 2、stderr。失敗時は副作用に進まない。
   - `gh issue view` の成功だけでは Issue と証明できない。helper は返された URL の `/pull/`・不一致・closed を拒否する。
   - 成功 JSON の `repo` から `repository-ownership` で **TARGET_ROOT（対象 repository の checkout）** を確定する。以降の ref・branch・worktree・fetch 検査は `git -C "$TARGET_ROOT" ...` で行い、起動元の checkout の一覧で別のリポジトリを検査したことにしない。
3. 本文、全ページのコメント、依存関係を読む。Issue 本文が利用者の承認・判断を求めていれば、推奨案での委任では代用せず停止する。
4. **worktree 作成より前に**次を検査する。どれかあれば、新規作業を作らず状態を報告する。古い claim も横取りしない。
   - 同じ Issue に対応する open PR（linked Issue、本文、branch を確認。最初のページだけで判定しない）
   - 他セッションの `refs/heads/agent-claims/issue-N`、コメントに記録された未解放の担当
   - 対象 repository の既存 worktree、ローカル・remote branch（`issue-N` は番号の境界で照合し、N=58 と N=580 を混同しない）
   - 未完了の先行 Issue・必要な未マージ PR
   worktree 作成直前と、編集・push・PR 作成前に最新の Issue 状態と競合を再確認する。新規競合や closed 化があれば停止し、自分がこの run で作った worktree/branch と他者の作業を区別する。
5. `<!-- planning-parallel-issues board:... -->` がある子は **`planning-parallel-issues` 必須**。claim・先行条件・成果公開・所有権の再確認・解放はそちらに従う。このスキルにプロトコルを写さない。親からの子の選択は担当外。

`gh issue/pr` には毎回 `--repo "$REPO"` を付ける。`gh api` は `--repo` を受け付けないため、`repos/<owner>/<repo>/...` の完全な endpoint で対象を固定し、一覧は `--paginate` で取得する。helper は入力検証のみで、競合・承認・claim の確認済み証拠にはならない。

## 3. Herdr と作業場所

着手条件がそろったら、`HERDR_ENV=1` の場合に `herdr-agent-task` と `herdr` を読む。**現在の pane** をリネームし、担当タスクを設定する。他 pane や focus を変更しない。

```bash
herdr pane rename "${HERDR_PANE_ID:?}" "Issue#${NUMBER:?}"
herdr-agent-task set "Issue #$NUMBER 実装・PR作成"
```

現在 pane が特定できない・rename が失敗した場合は、別 pane を推測せず停止する。

**必須:** `superpowers:using-git-worktrees` と `repository-ownership` に従い、status・worktree 一覧・ignore を確認して、**対象 repository の最新の既定ブランチ（親は `origin/main`）** から新しい worktree を作る。名前は `issue-N-<要約>`、branch は `feat/issue-N-<要約>` または `fix/issue-N-<要約>`。URL と #番号で作成方法を分けない。制作物の worktree は制作物のディレクトリの中に作る。親の detached HEAD 直下では編集しない。

## 4. 推奨案 → Spec → Plan → 実装

1. **`superpowers:brainstorming` 必須。** 意図・受け入れ条件・編集禁止を整理し、推奨案を選ぶ。Spec は規模にかかわらず、親 main worktree の `docs_draft/superpowers/` に Markdown で保存する。`docs/superpowers/` に置かず、commit しない。
2. Spec に **決定記録** を設け、各項目に「選択した案 / 検討した別案 / 選択理由 / 委任指示または停止条件」を残す。依頼にある質問・承認要求と通常の設計確認を区別する。
3. **`superpowers:writing-plans` 必須。** 同じ保存先に Plan を作り、実在する Spec パス、Task、担当ファイル、検証入口、実行方式・理由、contingency を記録する。AGENTS.md の SDD 実行運用に従う。
4. 実行方式は次で決める。ランタイムのツール仕様を確認し、存在しない一覧操作や同期フラグを渡さない。リポジトリが要求する起動条件を満たせなければ inline を選ぶ。

   | 条件 | 選ぶ方式 |
   |---|---|
   | Task が独立し、文脈を子に渡しきれ、同期委譲を使える | `superpowers:subagent-driven-development` |
   | 同じファイルの逐次変更、1–2 Task、委譲不可 | `superpowers:executing-plans` |

   Pi の `/sp-implement` は直列実行、`/sp-implement-parallel` は依存を満たした独立 Task を別 worktree で並列実行する入口。SDD なら常に parallel という対応ではない。同じファイルを同時に書かせない。run 中は選んだスキルで続け、コマンド再入力待ちにしない。
5. 選んだスキルの実装・TDD・レビューに従う。スキルを作る場合は `superpowers:writing-skills` の無し/ありの振る舞い比較を行う。固定文言の検査テストで代用しない。検証の失敗は `systematic-debugging` で調べ、解消できなければ停止する。

## 5. 検証・PR・完了

- **`verification-before-completion` と `finishing-a-development-branch` 必須。** 終了方法は PR 作成を選び、改めて選択肢を尋ねない。作業対象 root と remote を確認し、AGENTS.md の正式入口で検証する。
  - 親: `./scripts/ci/verify-devenv.sh`
  - 制作物: `package.json` の検証スクリプトと `xrift check <world|item> --build`
- 長時間の検証・子・CI は `monitoring-long-tasks` 必須。同期の終了を待ち、終了コードと実ログを確認する。
- worktree で明示パスのみ stage、commit、push、`gh pr create --repo "$REPO" --base main`。親では `items/`・`worlds/` を stage しない。既存 PR が見つかったら重複作成しない。
- PR 本文: 同 repo の `Closes #N`、変更概要、**Spec の決定と理由、実行方式と理由**、実行した検証・結果、作業時間と主要 Task 別内訳。未実行の確認を成功と書かない。
- 親の PR は **CI green を確認してから完了**。制作物の PR は、その制作物に CI があれば green を確認する。マージ・タグ・リリース・デプロイ・プロビジョニングは行わない。
- ボード子の成果公開・claim 解放は `planning-parallel-issues` に従う。自身の所有物だけを扱う。
- 完了時は Herdr task を completed にする。PR URL、検証結果、worktree/branch、実在する Spec/Plan・残存ログの絶対パス、所要時間を報告する。

## 停止時の報告

不正入力、route 確認失敗、競合、未完了依存、Issue の明示承認要求、禁止・取り消せない操作、想定外の外部送信、必要なツール/API の失敗、解消できない検証失敗では停止する。停止条件は「途中で止めずに」の例外であり、省略・成功扱い・別 repo への切替で回避しない。

**理由 / 現在の状態 / 実施済み検証と終了コード / 存在する worktree・branch・Spec・Plan・ログ / 次の操作** を報告する。作っていないパスは「未作成」と書く。通常の工程区切り、推奨案の選択、実装方式の選択は停止理由にしない。
