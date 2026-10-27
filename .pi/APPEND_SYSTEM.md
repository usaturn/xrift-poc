# 基本ルール

- 明示的に開始した Superpowers のスキルが委譲を求める場合は、独立した調査・検証を積極的に subagent に委譲すること。委譲のためだけに通常の依頼を Superpowers へ切り替えない
- メインコンテキストには要約結果だけを戻し、メイン Agent は設計・実装・統合判断に集中すること
- `HERDR_ENV=1` のときは、タスクに着手した直後と完了したときに
  `herdr-agent-task set "<タスク名>"` / `herdr-agent-task set "<タスク名>" --state completed`
  を実行し、Herdr 左サイドバーへ担当タスクを反映すること。
  詳細は `.agents/skills/herdr-agent-task/SKILL.md` を参照すること

## リポジトリの分担

- 開発環境（`.devcontainer/`・`.agents/`・`.claude/`・`.pi/`・`.cursor/`・`scripts/`・`tools/` 等）の Issue・worktree・検証・PR は `usaturn/xrift-poc` が操作先。worktree は `origin/main` から作る
- `items/<name>/`・`worlds/<name>/` の制作物は、それぞれの Git リポジトリが操作先。Git 未初期化や origin が無い制作物では GitHub の操作をせず、代わりに `xrift-poc` へ commit・起票しない
- 操作の前に `uv run --no-project python "$XRIFT_POC_ROOT/.agents/scripts/route_target.py" <path>` で判定する。`<path>` は cwd ではなく、これから変更するファイルかディレクトリ（まだ無いパスでもよい）で、作業対象が変わるたびに判定し直す。制作物は main checkout の `items/`・`worlds/` 側で扱い、feature worktree の `items/`・`worlds/` の配下（`stop`・`product_in_env_worktree`）では作業しない。手順は `.agents/skills/repository-ownership/SKILL.md` に定義する

## サブエージェントの起動

- `subagent` は、利用者が `/sp-*` または `/skill:*` で明示的に開始した Superpowers の中で、選択したスキルが委譲を求める場合だけ使う。子の bounded role からさらに委譲しない
- 導入済みの `@teelicht/pi-superagents` が公開するツール定義と、現在のコマンドの設定に従う。`action`・`capabilities`・`async` は受け付けないので渡さない。一覧操作や同期フラグを起動条件にしない
- 単発は `agent` と `task`、独立した複数 Task の並列委譲は `tasks` を使う。ロールはツール定義にある `sp-recon`・`sp-research`・`sp-implementer`・`sp-review`・`sp-debug` から選ぶ。`useTestDrivenDevelopment` は開始時に解決した値を各呼び出しへ明示し、`model` は利用者が単発の上書きを指定した場合だけ渡す
- 単発・並列とも同期実行であり、ツールは全ての子が終了するまで戻らない。同期／非同期と直列／並列を混同しない。バックグラウンド起動・status 待機・cancel の操作は無い
- 同じ worktree に書く子は同時に1件までとする。依存する Task、同じファイルに触れる Task、担当範囲が曖昧な Task は直列に回す。並列に書く場合は `worktrees.enabled` が有効であることを確認し、Task ごとに別の worktree へ隔離する。無効な場合は調整役の作業場所を共有するため、並列の書き込みを始めない
- worktree 隔離の起動前に、親と各 Task の `git status --porcelain` が空であることを確認する。worktree は親の HEAD を基準にするため、未 commit の変更が子へ渡るとは扱わず、利用者の変更を勝手に commit・stash しない
- 通常の並列呼び出しでは、拡張が一時 worktree を作り、差分を artifact の patch に保存して片付ける。並列 SDD では、調整役が Task ごとの永続 worktree を先に作り、全ての `tasks[].cwd` に異なる絶対パスを渡す。同じ repository に属し、親 HEAD を祖先に持つ clean な worktree を使い、レビュー・修正が終わり commit を取り込んでから片付ける。作成に失敗した場合は報告して該当 Task を直列で進める。作業領域とログは `.agents/sdd-workspace.md` に従う
- `sp-review` は既定では読み取り専用で bash を持たない。テスト実行の裏取りは、実際のツール設定で bash を使える `sp-implementer` などか調整役が引き受け、レビュー子には差分照合を頼む。PR レビューの補助委譲は `.agents/monitoring-runtimes.md` の起動条件を満たさないため省略し、同期委譲で代用しない
- 予約 ref・Issue 更新・PR は調整役の一元管理とし、子には触らせない
- サブエージェントが /simplify を使いたいときは @.agents/skills/simplify-in-subagent/SKILL.md を使う

## モデル名の抽出

スキル: pr-review の `MODEL_NAME` は `.agents/skills/pi-model-name/SKILL.md` の手順で決める。

1. pi の bash ツール内で `PI_PROVIDER` と `PI_MODEL` の両方を確認する。スクリプトを使う場合は、同スキルに記載された親 checkout からの絶対パス取得手順に従う。
2. 片方でも空なら停止し、ユーザに正確なモデル識別子を確認する。セッション履歴の `model_change` を正式な `MODEL_NAME` の根拠にしない。
