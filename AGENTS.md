# AGENTS.md

## リポジトリ概要

xrift-poc は、XRift のワールドとアイテムを作るための開発環境（Dev Container・コーディングエージェント・共通スキル・開発支援ツール）を管理する。ワールド・アイテムの制作物（ソースコード・アセット・依存パッケージ・ビルド成果物）は管理しない。

開発環境は `usaturn/maatlog-poc` から移植した。移植元のコミット・除外したもの・変更点は `PORTING.md` に記録する。以下、xrift-poc を「親」、`items/<name>/`・`worlds/<name>/` を「制作物」と呼ぶ。

## 操作先の分担

| 作業対象 | Git・Issue・PR の操作先 |
|---|---|
| 開発環境（`.devcontainer/`・`.agents/`・`.claude/`・`.pi/`・`.cursor/`・`scripts/`・`tools/`・`.github/`・ルートの文書と設定） | `usaturn/xrift-poc`（親） |
| 制作物 | その制作物自身の Git リポジトリ。origin を確認してから操作する |
| Git 未初期化、または操作先を確認できない制作物 | GitHub の操作を止め、必要な設定（`git init`・`git remote add origin <URL>`、origin を GitHub の URL として読めないときは `git remote set-url origin https://github.com/<owner>/<repo>.git`）を利用者に説明する。代わりに親へ commit・起票しない |

- 操作の前に `uv run --no-project python "$XRIFT_POC_ROOT/.agents/scripts/route_target.py" <path>` で判定する。`kind` が `env` なら親、`product` なら制作物、`stop` なら操作しない。`product` でも `repository` が null なら Issue・PR を作らない。手順は `.agents/skills/repository-ownership/SKILL.md`（Claude は `.claude/skills/repository-ownership`）に定義する
- 判定に渡す `<path>` は、これから変更するファイルまたはディレクトリであり、cwd ではない。これから作るファイルのように、まだ無いパスも渡せる（実在する最も近い祖先のディレクトリで判定する）。変更の対象のパスが変わるたびに判定し直す
- 制作物（`items/<name>`・`worlds/<name>`）は親の main checkout（`$XRIFT_POC_ROOT`）の下で扱う。親の feature worktree 自身の `items/`・`worlds/` は `route_target.py` が `stop`（そのものは `product_base`、配下は `product_in_env_worktree`）と判定する。ここで制作物を編集・commit しない（worktree を片付けると中身も消える）
- Git 未初期化の `worlds/<name>/` で `git rev-parse --show-toplevel` を実行すると、祖先の親が見つかる。コマンドが成功しただけで「制作物が Git 管理されている」と判定しない
- `items/`・`worlds/` は `.gitignore` でルートから無視している。配下を `git add -f` しない。サブモジュール・subtree・gitlink として登録しない
- 制作物の中の `public/` は XRift のアセットディレクトリである。削除・無視の対象にしない
- `XRIFT_POC_ROOT` は Dev Container が親の main checkout のパスに設定する

## 基本方針

- ユーザの指示が曖昧な場合は AskUserQuestion を使う
- 実装速度よりコード品質、保守性、テスト容易性を優先する
- 計画や設計が必要な変更は、方針を決めてから実装する
- 開発環境の Python では pydantic、pytest、Pyright、uv を使用する
- 制作物の依存（React・Three.js・React Three Fiber・Vite・`@xrift/world-components` など）は、各制作物の `package.json` と lockfile で管理する。親を npm workspaces・モノレポにしない

### テストの採用基準

テストは挙動を守るために書き、文面や値を写すためには書かない。superpowers の TDD で進める場合も、テストを書くかどうかはこの基準で決める。

- テストで守る挙動が無い変更（文書・スキルの文面・設定値・一覧の更新など）は、失敗するテストを先に書かずに進めてよい。代わりの確認（実行、lint、構造の検査、writing-skills の行動比較）を Plan に書く
- 書くテスト
  - 実際に実行して挙動を確かめるテスト（fake のコマンドを置いた一時 HOME で関数を動かす、後片付けの安全条件を実ファイルで確かめる など）
  - 実際に起きた不具合の再現テスト
  - 外部の CLI の出力やファイル形式を解析するコードのテスト
  - 構造の検査: frontmatter・JSON・TOML の形、参照するパスの実在、文書中のコードブロックの実行
- 書かないテスト
  - 文言の照合: `SKILL.md`・README・`AGENTS.md` の文を `in` / `not in` で照合する
  - 消えたことの確認: 撤去した文言・旧パス・旧ファイルが無いこと。移行時に一度確かめれば足りる
  - 運用で変わる値の写し: ランチャー一覧・手順名・バージョン・モデル名を固定値で並べる。確かめるなら構造や性質（重複が無い、参照先が実在する など）を確かめる
  - 実行できる処理の本文の照合: スクリプトや関数の本文に特定の文字列があるかを確かめる。挙動は実行して確かめる
  - 同じ実体を複数のパスで検査: `.claude/skills/<name>` と `.agents/skills/<name>` を並べる、版同士のバイト一致を確かめる
  - 別の仕組みが保証することの再検査: pydantic のモデル定義・Pyright・ruff・他のテストが既に確かめていること
- 値を固定すること自体に理由がある場合（unset する環境変数の範囲など）は、固定値のテストを残してよい。何を守るのかと、値を変えるときはテストも合わせて更新することをコメントに書く
- テストを消すときは、その検査が守っていた挙動を別のテストが確かめていること、またはその挙動がもう無いことを確かめる

## 日本語表現

コードレビューや説明、PR や Issue への起票では、不自然な直訳表現を避け、日本人エンジニアに一般的な表現を使うこと。

- `contract` を「契約」と訳さず、文脈に応じて「仕様」「API仕様」「インターフェース仕様」「既存の挙動」などと表現する。
- `atomic` / `atomically` を「原子的」「原子的に」と訳さず、文脈に応じて「アトミック」「一括で」「途中状態を残さず」「不可分な操作として」などと表現する。
- 単なる言い換えではなく、可能な限り具体的に何が保証されるか、何が問題になるかを説明する。
- `product` / `production` を機械的に「製品」と訳さない。「製品」「製品実装」は原則として使用せず、文脈に応じて自然な日本語に言い換えること。

  - 「製品」→「システム」「アプリケーション」「機能」「実装」「対象」など
  - 「製品実装」→「実装作業」「本実装」「機能実装」など

- 作業終了を表す際に「着地」「着地完了」は使用しない。

  - 文脈に応じて「完了」「作業完了」「対応完了」「実装完了」「修正完了」を使用する。

- 「正本」は使用しない。「採用条件は `page-types.md` にのみ定義する」のようにファイル名と関係を具体的に書く。
   抽象語が必要な場合は、仕様・ルールを定義するファイルを文脈に応じて「定義元」「正式版」「基準となる文書・設定」「正式な保存先」「一元管理する場所」「〜を基準とする」など、日本人エンジニアに自然で分かりやすい表現に言い換えること。
   単純な置換ではなく、何を基準にするのかが伝わる文章にする。

- レビュー修正ループの `round` を「巡」、`seat` を「席」と訳さない。説明・レビュー・PR・Issue・引き継ぎ書では、次のように書く。

  - 「巡」→ 繰り返しの単位は「サイクル」、順番は「1 回目」「2 回目」、数は「サイクル数」「レビュー修正の回数」など
  - 「席」→ レビューを担当するモデル・エージェントは「レビュアー」、その組み合わせは「レビュアーの構成」など
  - 既存のスキル・`.agents/review-records.md` の形式・テストに残る「巡」「席」（JSON のキー `round`・`seats` を含む）は、引用するときだけ原文のまま書く。
    用語そのものを置き換えるときは、形式の定義と、文言を検査するテストを含めて別の変更として行う。

- 「実行器」のような、日本人ITエンジニアが一般的に使わない直訳表現は避ける。文脈に応じて「ランナー」「実行ツール」「実行処理」などの自然な用語を使い、対象が明確な場合は具体的なツール名を使用する。

## ファイルとドラフト

- `docs_draft/` は、タスクで指定された場合だけ読む
- Superpowers の Spec と Plan は Markdown で、親の main checkout の `docs_draft/superpowers/` に置く。gitignored のまま commit しない
- ユーザから作成を依頼された文書は `docs_draft/` に置く

### コミットしないファイルの置き場所

コミット予定のないファイルは、用途ごとに次の場所へ置く。表の相対パスは、すべて親の main checkout（`uv run --no-project python .agents/scripts/agent_tmp.py root` が出力するディレクトリ）が基準である。feature worktree・レビュー用 worktree・制作物の中には作らない。

| 用途 | 置き場所 |
|---|---|
| セッション後も参照し得るスクラッチ・検証ログ・下書き | `tmp/agents/<topic>.XXXXXX/`（`uv run --no-project python .agents/scripts/agent_tmp.py mkdir <topic>` で作る） |
| エージェント間の引き継ぎ | `tmp/docs_draft/claude_handoffs/` |
| Superpowers の Spec / Plan | `docs_draft/superpowers/` |
| PR レビュー結果 | `reviews/<owner>/<repo>/#<PR>-<branch>/`（親の PR も制作物の PR も親の main checkout に置く。保存先は pr-review helper が決める。PR 修正ループも同じ場所に保存し、ループ自身の記録は `loop/` に置く） |
| 使い捨ての git worktree とその作業領域 | `/tmp`（pr-review helper のセッション、review-fix-planning の `mktemp -d`、PR 修正ループの runner の出力と所有 worktree `${TMPDIR:-/tmp}/xrift-poc-pr-loop-XXXXXX/`） |
| 消えても困らない中間出力 | `/tmp`、Claude Code の scratchpad |

- `/tmp` 直下に固定名のファイルを作らない。`/tmp` を使う場合も `mktemp -d` などで一意のディレクトリを作り、その中に置く
- `tmp/agents/` のセッションディレクトリは、不要と確認できた時点で作成したスキル自身が消す。消せなかったものは最終報告に完全パスを書く。`tmp/agents/` 以外の `tmp/` 直下（人の置いたファイルを含む）には触らない
- `/tmp` の既存残骸は `.devcontainer/cleanup-workspace.bash` が安全条件付きで片付ける。`items/`・`worlds/` は片付けの対象にしない。`git clean -fdx` のように未追跡のファイルを一括で消すコマンドを使わない

## Git・PR

- ローカルに `main` を作らず、`origin/main` を正とする
- 親の変更は、最新の `origin/main` から feature branch の worktree を作り、PR で取り込む。`main` へ commit しない
- 制作物の変更は、その制作物のリポジトリの既定ブランチから branch を作る。制作物は親の main checkout の下で扱う（親の feature worktree の中では扱わない）。worktree は制作物のディレクトリの中（例: `worlds/<name>/.worktrees/<branch>`）に作り、`.worktrees/` を制作物の `.git/info/exclude` に加える。制作物の外（親の外）に作った worktree は、共有の指示とスキルを読めない
- ユーザの指示なしに commit しない。ただし `.claude/worktrees/`・`.worktrees/`・`/tmp/` 配下の worktree branch では commit と PR 作成を許可する
- `superpowers:executing-plans` または `superpowers:subagent-driven-development` で更新するときは、worktree で commit し、PR まで作成する。ただし Git 未初期化の制作物には当てはめない（下記「編集前の確認」を参照）
- 新しい commit を push するとき（親の feature worktree、`repository` が null でない制作物の Git の root と制作物の中の worktree）は、repository-ownership の「公開前の秘密検査」に従い、`scan_before_push` で検査した commit の SHA を push する。検査が失敗したり止まったりしたら push しない。ref の削除（claim の解放、マージ後の branch の削除）は対象外である
- 親の PR は CI（`.github/workflows/ci.yml`）の green を確認してからマージする。review-fix-planning の finalize はどのリポジトリでも CI の結果を確認する。CI の無い制作物のリポジトリでは停止して報告する
- デプロイとプロビジョニングは人間が行う。XRift への公開（`xrift upload world`・`xrift upload item`）は、利用者が明示的に指示したときだけ実行する。セキュリティチェックを省略するオプションを標準の手順にしない

## 開発と検証

- リポジトリ内の Python ツールは `uv` で実行する
- uv で使う・入れる Python は 3.14 以上にする。3.14 未満で互換性を確かめる必要がある場合だけ、`.devcontainer/README.md` の「3.14 未満で互換性を確認する場合」に従い、`UV_PYTHON_INSTALL_DIR` と `UV_CACHE_DIR` を一時ディレクトリに隔離してワンショットで実行し、確認後に削除する
- 永続シェルで `export HOME=...` しない。一時ホームが必要な検証は `env HOME="$tmpdir" ...` のワンショットに限る
- Gitleaks は `.devcontainer/install_beforehand.bash` と同じ approved pin を使う。Dev Container では再導入しない

### 親の検証

- worktree の root で、`route_target.py` が `env` と判定することを確かめてから、`./scripts/ci/verify-devenv.sh` を引数なしで実行する。`tools/devenv` project を使い、`scripts`・`tools/devenv`・`tools/devenv_migration`・`tools/review_triage`・`tools/agent_runner`・`tools/test_support`・`.agents` の lint・型検査・テストを実行する
- 収集対象や件数の確認には、同じ root で次を使う。件数は固定しない

```bash
uv sync --project tools/devenv --locked
uv run --project tools/devenv --no-sync pytest -c tools/devenv/pyproject.toml tools/devenv/tests tools/devenv_migration/tests tools/review_triage/tests tools/agent_runner/tests tools/test_support/tests .agents --collect-only -q
```

## 一時ファイル・ディレクトリの削除

rm / rmdir の削除対象にシェル変数を使う場合は、各変数に ${VAR:?} 形式の空値ガードを付け、パス全体を引用すること。

例: rm -rf -- "${T:?}/${n:?}"

この規則はサブエージェントが実行するコマンドにも適用すること。

## 編集前の確認（必須）

- `git status --porcelain` と `git worktree list` を確認する
- 親の追跡ファイル（開発環境）は、main checkout（`$XRIFT_POC_ROOT`）では編集しない。親の変更は worktree で行う。制作物（`items/<name>`・`worlds/<name>`）は main checkout の下で扱う
- 親の修正は、最新の `origin/main` から superpowers:using-git-worktrees で worktree を作り、PR まで行う（ユーザの指示なしに commit しないの **例外** である）
- Git 管理された制作物（`route_target.py` が `product`）の修正は、その制作物の最新の既定ブランチから、制作物のディレクトリの中（例: `worlds/<name>/.worktrees/<branch>`）に worktree を作り、その制作物のリポジトリの PR まで行う
- 親の main checkout の Git 未初期化の制作物（`stop`・`product_uninitialized`）はその場で編集し、commit・worktree・PR を作らない。Git の設定（`git init`・`git remote add origin <URL>`）を利用者に説明する。親の feature worktree の `items/`・`worlds/` の配下（`stop`・`product_in_env_worktree`）では編集しない

## エージェント運用

- 長時間コマンド、検証、バックグラウンド処理、サブエージェントの完了・生存・進捗を監視するときは `monitoring-long-tasks` を使う。監視手順はこのスキルの SKILL.md に定義する。各ランタイムでどのツールを使うかは `.agents/monitoring-runtimes.md` を参照する

### SDD 実行運用

- Plan に contingency 節を設ける。最初の RED で実環境の出力形式や API 挙動が前提と異なる場合は、仕様の意味を保つ最小限の修正を ledger に記録して継続する。仕様の意味を変える場合だけ Ruling を求める
- 開発中は変更に関連するテストと静的チェック（ruff / pyright）を実行する。最終 Task では、親の変更は worktree の `./scripts/ci/verify-devenv.sh`、制作物の変更は制作物の検証（`package.json` のスクリプトと `xrift check <world|item> --build`）を1回実行する
- 実装サブエージェントはテスト、lint、回帰を同期実行し、完了を待ってからターンを終える。`monitoring-long-tasks` の監視ルールは controller が長時間タスクを監視するためのものであり、実装サブエージェント内部の待ちをバックグラウンド化する根拠にしない
- controller は `monitoring-long-tasks` の終了条件が成立するまでターンを保持し、stall ポーリングを担当する。完了通知は harness の終了コード付きプロセス終了通知を指し、成果物やサブエージェントの結果通知とは区別する。これは対象停止の判定材料であり、対象の実行中にターンを終える根拠にしない。ただし事前宣言した読み取り専用の PR レビュー委譲の期限・無進捗打ち切りに限り、停止確認不能のときは委譲対象・停止状態・残存パスを報告してターンを終えてよい。`TaskOutput(block=true)` の raw dump は読まず、途中経過は出力ファイルを必要な範囲だけ読む
- 各担当は着手から成果提出までの所要時間と主要 Task 別の内訳を作業報告に記録する
- Plan の各 Task に、テストの要否と理由を書く。要否は「テストの採用基準」で決め、テストを書かない Task には代わりの確認を書く
- Plan に「受け入れ基準」節を設け、動作環境・受け付けるべき実入力・防御範囲の 3 つを書く。動作環境の例は Git 未初期化の制作物と、独立した Git リポジトリの制作物、`.venv` の無い worktree、`.devcontainer/agent-runner.toml` の既定の役割割り当て。実入力の例は改行や非 ASCII を含む複数行の自由記述。防御範囲には、対象にする条件と、対象にしない反例・設定の両方を書く。実行して確かめられる条件は実装の前にテストにし、実環境の形をした共有 fixture は `tools/test_support` を使う
- 実装担当とタスクレビュー担当の brief には同じ受け入れ基準を写し、レビュー担当にだけ計画全体や Spec を追加で渡して基準をずらさない。タスクレビューは、実装が brief の指定どおりかだけでなく、指定された文言・条件そのものが受け入れ基準の意図に合っているかも照合する
- 防御範囲の外の反例や Plan の文面と食い違う指摘は、修正ループに入れる前に controller が Plan の文面と突き合わせて裁定し、採否と理由を ledger に記録する
- controller はレビュアーの指摘を原文のまま実装担当へ渡し、要約・言い換え・修正手段の指定を書き足さない
- レビュー指摘への修正は欠陥クラス単位で行う。指摘された欠陥と同じ規則・関数・パターンを使う箇所をすべて列挙し、まとめて直すか、直さない箇所と理由を記録する
- 最後の全体レビューでは、具体的な疑いのある指摘に限り、範囲を絞ったテストや最小再現の実行をレビュー担当に認める。レビュー担当を分けるときは、指摘箇所ではなく入力 → 保存 → 検証の流れを通して追う担当で割る
- Plan が完成したら、連続実行の指示がある場合でも、受け入れ基準・防御範囲・動作環境の 3 点を利用者に提示して確認を得てから実装に進む。ただし `issue-to-pr` スキルで着手した run ではこの確認を省略し、同スキルの停止条件だけで止まる

## XRift の制作

### 作成からプレビュー・検証まで

```bash
(cd "$XRIFT_POC_ROOT/worlds" && xrift create world my-world -y)
cd "$XRIFT_POC_ROOT/worlds/my-world"
npm run dev -- --host 0.0.0.0
# 開発サーバーを止めてから検査する
xrift check world --build
```

```bash
(cd "$XRIFT_POC_ROOT/items" && xrift create item my-item -y)
cd "$XRIFT_POC_ROOT/items/my-item"
npm run dev -- --host 0.0.0.0
xrift check item --build
```

- `xrift create` は依存の導入まで行う。制作物の Git は利用者が必要に応じて初期化する（セットアップは制作物を作らない・clone しない）
- プレビューはホストのブラウザで `http://localhost:5173` を開く。2 つ目以降のプロジェクトは `npm run dev -- --host 0.0.0.0 --port 5174` のように 5174〜5179 を使う
- コンテナ内でのブラウザの確認は Python Playwright（`uv run --project "$XRIFT_POC_ROOT/tools/devenv" python <script>`）で行う
- 制作物は親の `.gitignore` で無視しているため、親のルートからパスを付けずに `rg` で検索すると、制作物の中は検索されない。親のルートからは `rg <pattern> worlds/<name>` のようにパスを明示するか、制作物のディレクトリの中から検索する
- 認証: `xrift login` は利用者が手動で行う。エージェントはログインを求めて待たない。状態は `xrift whoami` で確かめる
- XRift スキル: `xrift-world`（ワールド制作のガイド）、`xrift-sdk`（`@xrift/sdk` でのアップロード API のガイド。アイテムの描画実装のガイドではない）。セットアップが固定した版を取得する（`scripts/install_xrift_skills.bash`）
- アイテム制作: 公式のアイテム開発ガイド（<https://docs.xrift.net/item/create-first-item>）と `xrift create item`・`xrift check item --build` を使う。公式の `xrift-item` スキルがあることを前提にしない
- 公式ドキュメント: <https://docs.xrift.net>、CLI: <https://docs.xrift.net/cli/commands>

### 制作物からエージェントを起動する

- Git 未初期化の制作物から起動すると、Claude Code・pi・Codex は親の指示とスキルを読む
- 独立した Git リポジトリの制作物では、最初に一度 `xrift-link`（`project_link.py link`）を実行し、親のスキルへのリンクを置く。制作物の既存のファイル・スキル・設定は上書きしない。外すときは `xrift-link unlink`。制作物の worktree も制作物のディレクトリの中（例: `worlds/<name>/.worktrees/<branch>`）に作り、そこでも `xrift-link` を実行する。親の外に作った制作物の worktree は、共有の指示とスキルを読めない
- Codex は `cod` で起動する（親の `AGENTS.md` を読むよう `project_root_markers` を指定している）

## スキルの置き場所

- スキルの本体は `.agents/skills/<name>/` に 1 つだけ置き、日本語で書く（frontmatter の `description` を含む）。コードブロック・識別子・コマンド・パスは原文のまま書く
- `.claude/skills/<name>` は `../../.agents/skills/<name>` への相対シンボリックリンクにする。Claude Code は `.claude/skills/` しか読まない
- `.devin/skills` などランタイム専用の置き場所に写しを置かない。Pi・Codex・Devin は `.agents/skills` を読む。Devin・Cursor CLI・Grok Build は `.claude/skills` も読むが、リンクなので同じ実体である。Devin はリンクを 1 件にまとめ、`/agents:<name>` のような接頭辞を付けない
- `.pi/settings.json` の `skills` に、このリポジトリのスキルを足さない
- ランタイムによって違う部分は、同じ `SKILL.md` の中で「ランタイムごとの違い」節に分けて書く。frontmatter はランタイム固有のキー（Devin の `subagent` など）を併記し、その意味を同じ節に書く。`allowed-tools` は Claude Code と Devin が読むが、どちらも一覧のツールを許可の確認なしで実行するだけで、使えるツールを制限しない
- スキルのテストは `.agents/skills/<name>/` の実体のパスだけを読む。置き場所は `.agents/tests/test_skill_layout.py` がすべてのスキルについて検査する。一本化していないスキルは同ファイルの `PENDING` に載っており、一本化する変更で外す
- eval 用の架空のプロジェクトは `evals/fixtures/` に置く。外してあるのは pyright の対象からだけで、ruff と pytest の収集の対象には残るため、`test_*.py` や ruff を通らない Python を置かない

- セットアップで取得する XRift 公式スキル（`.agents/skills/xrift-*`・`.claude/skills/xrift-*`）は Git で無視し、編集しない。版の更新は `scripts/install_xrift_skills.bash` の定数を変えて行う。スキルの配置の検査（`.agents/tests/test_skill_layout.py`）は追跡しているスキルだけを対象にする

## 環境

Windows 11 の WSL2（Gentoo Linux）上で Docker を動かし、Ubuntu 26.04 LTS の Dev Container を使用する。

## 最新ドキュメントの取得

- ライブラリ、フレームワーク、SDK、API、CLI、クラウドサービスの仕様は Context7 で確認する。リファクタリング、ビジネスロジック、コードレビューには使わない
- ユーザが `/org/project` 形式の ID を示していない場合は、`npx ctx7@latest library <name> "<query>"` で ID を解決してから、`npx ctx7@latest docs <id> "<query>"` を実行する
- ユーザの質問を具体的な query にし、機密情報を含めない。1件につき3回までとし、指定バージョンがある場合は返された version ID を使う
- quota error の場合はその旨を伝え、`npx ctx7@latest login` または `CONTEXT7_API_KEY` を案内する。黙って別情報へ切り替えない

- XRift の仕様は <https://docs.xrift.net> を基準にする
