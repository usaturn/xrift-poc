# 基本ルール

- 独立した調査・検証は積極的に subagent に委譲して並列化すること
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

- 起動前に `subagent` ツールへ `{ action: "list", capabilities: true }` を投げ、`executable` かつ `launchable` なエージェントを確認する
- 単発起動は同期実行とし、`async: false` を必ず指定する（例: `{ agent: "worker", task: "...", async: false }`）
- `async` の省略は禁止。省略時はバックグラウンド起動扱いとなる
- `async: true` のワークフロー一括起動は、この環境では使用しない。pi がスタンドアロン版で `@earendil-works/pi-server` 系が無く、child session を作成できず失敗する（2026-09-06 に maatlog-poc で確認）
- 複数タスクは同期呼び出しで直列に回す。worktree を共有する場合は同時書き込みを避け、一度に書く子は1件にする
- `reviewer` に bash は無い。テスト実行の裏取りは bash 持ち（implementer / worker / delegate）にやらせるか、調整役が引き受ける。レビュー子には差分照合だけを頼む
- 予約 ref・Issue 更新・PR は調整役の一元管理とし、子には触らせない
- サブエージェントが /simplify を使いたいときは @.agents/skills/simplify-in-subagent/SKILL.md を使う

## モデル名の抽出

スキル: pr-review の `MODEL_NAME` は `.agents/skills/pi-model-name/SKILL.md` の手順で決める。

1. pi の bash ツール内で `PI_PROVIDER` と `PI_MODEL` の両方を確認する。スクリプトを使う場合は、同スキルに記載された親 checkout からの絶対パス取得手順に従う。
2. 片方でも空なら停止し、ユーザに正確なモデル識別子を確認する。セッション履歴の `model_change` を正式な `MODEL_NAME` の根拠にしない。
