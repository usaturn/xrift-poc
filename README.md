# xrift-poc

[XRift](https://xrift.net) のワールドとアイテムを作成・プレビュー・検証するための開発環境です。Dev Container・コーディングエージェント・共通スキル・開発支援ツールを管理します。

**このリポジトリで管理するのは開発環境だけです。** ワールド・アイテムのソースコード、アセット、依存パッケージ、ビルド成果物は管理しません。

開発環境は [usaturn/agent-workspace](https://github.com/usaturn/agent-workspace)（移植の時点の名前は maatlog-poc）から移植しました。移植の記録は [PORTING.md](PORTING.md) にあります。

## 構成

```text
xrift-poc/
├── AGENTS.md          # エージェント向けの共通指示
├── CLAUDE.md          # Claude Code 用（AGENTS.md を読み込む）
├── README.md
├── PORTING.md         # 移植の記録と検証の記録
├── .devcontainer/     # Dev Container の定義・セットアップ・シェルの設定・クリーンアップ
├── .agents/           # 共通スキル・補助スクリプト・レビューの記録の形式
├── .claude/ .pi/ .cursor/   # 各エージェントの設定（スキルは .agents/skills へのリンク）
├── .github/workflows/ # CI（verify-devenv.sh の実行）
├── scripts/           # 検証の入口（ci/verify-devenv.sh）・XRift スキルの導入・pi のセットアップ
├── tools/             # devenv（検証用の project）・agent_runner・review_triage・test_support など
├── items/             # Git 管理対象外。各アイテムの作業場所
└── worlds/            # Git 管理対象外。各ワールドの作業場所
```

`items/` と `worlds/` は `.gitignore` でルートから無視しています。中のディレクトリはサブモジュールにせず、このリポジトリには登録しません。`.agents/skills/xrift-*` と `.claude/skills/xrift-*`（セットアップで取得する XRift 公式スキル）と `skills-lock.json` も Git で無視します。

## 必要なもの（ホスト）

- Docker と VS Code の Dev Containers 拡張（またはその互換ツール）
- WSL2（Linux・macOS でも可）の中にリポジトリを clone し、そこから VS Code で開きます。コンテナの作成前にホスト側で `initialize.bash`（bash）を実行し、`${localEnv:HOME}` のホームを mount するため、WSL を使わずに Windows 側のパスから開くと、最初の手順で失敗します
- ホストのホームにある各エージェントの設定は、コンテナと共有されます（`.devcontainer/README.md` の「ホームの bind mount と共有」）。無いディレクトリは `initialize.bash` が作ります
- `~/.xrift` は実ディレクトリにします。dotfiles 管理などでディレクトリへのシンボリックリンクにしていると、`initialize.bash` が理由を表示して止まり、コンテナが起動しません

## セットアップ

1. このリポジトリを clone し、VS Code で開いて「Reopen in Container」を選びます
2. `postCreateCommand` の `install_beforehand.bash` が、エージェント・XRift CLI・XRift スキル・Playwright などを導入します。API キーが無くても完了します
3. 各エージェントの認証は、起動後に手動で行います（`.devcontainer/README.md`）

ホストでのコンテナの新規作成とセットアップの完走は、まだ確かめていません（[PORTING.md](PORTING.md) の「未実施」）。

## ワールドとアイテム

制作物は、親の main checkout（環境変数 `XRIFT_POC_ROOT`）の `worlds/`・`items/` の下に作ります。親の worktree の下には作りません。

ワールドの作成からプレビュー・検証までは、次のとおりです。

```bash
(cd "$XRIFT_POC_ROOT/worlds" && xrift create world my-world -y)
cd "$XRIFT_POC_ROOT/worlds/my-world"
npm run dev -- --host 0.0.0.0
# 開発サーバーを止めてから検査する
xrift check --build
```

アイテムも同じ手順です。

```bash
(cd "$XRIFT_POC_ROOT/items" && xrift create item my-item -y)
cd "$XRIFT_POC_ROOT/items/my-item"
npm run dev -- --host 0.0.0.0
xrift check --build
```

- `xrift create` は、テンプレートの取得から依存の導入まで行います。`npm run build`（`tsc && vite build`）と `npm run typecheck`（`tsc --noEmit`）も使えます
- `xrift check --build` は、ワールドかアイテムかを `xrift.json` から判定し、ビルドしてから検査します。XRift CLI 0.25.0 では、種類を書く `xrift check world --build`・`xrift check item --build` はビルドしないので使いません。`dist/` の無い新規の制作物では失敗し、`dist/` があれば古い成果物を検査します（[#57](https://github.com/usaturn/xrift-poc/issues/57)）
- プレビューはホストのブラウザで `http://localhost:5173` を開きます。2 つ目以降のプロジェクトは `npm run dev -- --host 0.0.0.0 --port 5174` のように 5174〜5179 を使います
- コンテナ内でブラウザの描画を確かめるときは、Python Playwright を使います（`uv run --project "$XRIFT_POC_ROOT/tools/devenv" python <script>`。ブラウザが無いときは `uv run --project "$XRIFT_POC_ROOT/tools/devenv" playwright install chromium` で入れます）。制作物のディレクトリからも実行できるよう、`--project` は絶対パスで指定します
- `xrift check` のセキュリティ検査を省略するオプション（`--ignore-warnings`・`--skip-check`）は、標準の手順に使いません

制作物は `worlds/<name>/`・`items/<name>/` の中で、必要に応じて独立した Git リポジトリとして管理します。Git を初期化していないローカルのプロジェクトでも構いません。

## XRift へのログイン

次の手順は、この Dev Container ではまだ確かめていません（アカウントが必要なため）。

1. コンテナのターミナルで `xrift login` を実行します
2. ブラウザが自動で開かない場合は、表示された URL をホストのブラウザで開きます
3. ログインが終わると、コールバックが転送された `localhost:3000` に届きます。ホストの 3000 番が使われていると転送が失敗するので、使っているプロセスを止めてからやり直します
4. `xrift whoami` で確かめます

認証情報は `~/.xrift/config.json`（ホストと共有）に保存されます。このリポジトリにも制作物のリポジトリにも入れません。

## 公開

`xrift upload world`・`xrift upload item` は、利用者が公開すると明示的に決めたときだけ手動で実行します。セットアップ・更新・通常の検証には含めません。エージェントは、利用者の指示が無い限り実行しません。

1. `xrift whoami` で、ログイン済みであることを確かめます（上の「XRift へのログイン」）
2. 制作物のディレクトリで `xrift check --build` を実行し、ビルドとセキュリティ検査を通します
3. 同じディレクトリで、ワールドは `xrift upload world`、アイテムは `xrift upload item` を実行します。アイテムは `xrift.json` の内容を読みます

`xrift upload` はアップロードの前にセキュリティ検査を行います。検査を省略する `--skip-check` と、ワールドのバンドルの `<Item itemId>` が `xrift.json` の `world.items` に宣言されているかの確認を省略する `--skip-item-scan` は、標準の手順に使いません。

この節は、XRift CLI 0.25.0 の `xrift upload --help`・`xrift upload world --help`・`xrift upload item --help` で確かめた範囲で書いています。アカウントが必要なため、アップロードはこの Dev Container では実行していません（[PORTING.md](PORTING.md) の「未実施」）。

## エージェントを制作物から起動する

- Git 未初期化の制作物では、そのまま Claude Code・pi・Codex が親の共通の指示と XRift スキルを読みます。Pi の設定（`.pi/`）だけは、下の Pi の項目のとおり `xrift-link` が要ります。Git を初期化したら、次の項目のとおり `xrift-link` を実行します
- 独立した Git リポジトリの制作物では、最初に一度 `xrift-link`（`project_link.py link`）を実行します。親のスキルへのリンクを制作物の `.agents/skills`・`.claude/skills` に、Pi の設定へのリンクを `.pi/` に置きます。制作物の既存のファイル・スキル・設定は上書きしません。スキルのリンクは制作物の `.git/info/exclude` で、`.pi/` のリンクは `.pi/.gitignore` で無視されるので、制作物の Git の状態は変わりません。外すときは `xrift-link unlink` です。制作物の `.agents`・`.claude` などをシンボリックリンクにしている場合、リンク先が制作物の別の worktree の中なら、`xrift-link unlink` はそこにあるリンクを残します。制作物のどの worktree の中でもない場所（共有ディレクトリなど）のリンクを消すことになる場合は、何も変えずに止まり、手で外す方法を表示します
- 制作物の worktree は、制作物のディレクトリの中（例: `worlds/<name>/.worktrees/<branch>`）に作り、そこでも `xrift-link` を実行します。親の外に作った worktree は、親の指示とスキルを読めません
- `info/exclude` はすべての worktree で共有されます。`xrift-link` はリンクを無視しつつ、同名の利用者の実ディレクトリは無視しない否定行を置きます。別 worktree やリンク後に作ったスキルディレクトリも `git status` に表示され、未追跡の内容を消す `git worktree remove`（`--force` なし）や、同じパスを上書きする `git switch` は拒否されます。同名の通常ファイル・利用者のシンボリックリンク（壊れたリンクを含む）は無視されたままなので、`link`・`unlink` は全 worktree の該当する場所を報告します。これらは Git による削除・上書きからの保護対象ではありません。制作物自身の `.gitignore` などがスキルの上位ディレクトリを無視している場合も、その設定を優先し変更しません。以前の否定行の無い管理ブロックは、その制作物で次に `link`・`unlink` を実行したときに更新されます
- Pi は、project の設定（`.pi/` の `APPEND_SYSTEM.md`・`mcp.json`・`settings.json`）を起動したディレクトリの `.pi/` からだけ読み、親のディレクトリの `.pi/` を読みません（Pi 1.3.0）。そのままでは、制作物から起動した Pi に、親の `.pi/APPEND_SYSTEM.md` の Pi 向けの規則（サブエージェントの `async: false` など）、`.pi/mcp.json` の `google-dev-knowledge`、`.pi/settings.json` が効きません。`xrift-link` は、制作物の `.pi/` に、親のこの 3 つへのリンクを置きます。Git 未初期化の制作物でも実行します（`.pi/` のリンクだけを置きます）。Pi は制作物のルート（worktree ならそのルート）から起動します。サブディレクトリから起動すると `.pi/` を読みません。親を trust 済みなら、制作物も trust 済みとして扱われます（Pi は最も近い祖先の登録を使います）
- `.pi/` のリンクは、リンクを置いた worktree の `.pi/.gitignore` で無視します。`xrift-link` が作り直し、自分自身も無視します。`.pi/` の 3 つはファイルで、否定行で守れないため、共有の `info/exclude` は使いません。他の worktree にある制作物自身の `.pi/` のファイルは隠れず、Git 未初期化のときに置いたリンクも、`git init` の後の `git add -A` に入りません。制作物が自分の `.pi/<file>` を持つときはそれを残し、Pi はそのファイルを使います。リンクを消して自分のファイルに替えたときは、`xrift-link` を実行し直します（それまでは、そのファイルも `.pi/.gitignore` で無視されます）。制作物が自分の `.pi/.gitignore` を持つときは、`.pi/` のリンクを置きません
- `.pi/` のリンクは、親の main checkout のファイルを指します。制作物から起動した Pi で project の設定を変える操作（`pi mcp add -l`・`pi install -l`、`/mcp` での有効・無効の切り替えなど）は、親のファイルを書き換えます。その制作物だけの設定にするときは、そのリンクを消して自分のファイルを置き、`xrift-link` を実行し直します
- 制作物から起動した Pi に設定が効いていることは、制作物のルートで `pi mcp list` を実行し、`google-dev-knowledge` が出ることで確かめます
- Codex は `cod` で起動します。独立した Git リポジトリの制作物から起動しても、親の `AGENTS.md` を読むよう `project_root_markers=[".devcontainer"]` を指定しています。この指定は codex-cli 0.162.1 で確かめました。将来の版で `project_root_markers` の意味が変わると、親の `AGENTS.md` を読めなくなるおそれがあるので、Codex を更新したら制作物から起動して確かめ直します。制作物が自前の `.devcontainer` を持つ場合は、その制作物のディレクトリがルートになり、親の `AGENTS.md` を読めません
- Cursor CLI・Grok Build の読み込みは確かめていません。手動で確かめる手順は [PORTING.md](PORTING.md) の「未実施」にあります

どの制作物の Git・Issue・PR を操作するかは、`uv run --no-project python "$XRIFT_POC_ROOT/.agents/scripts/route_target.py" <変更するパス>` で判定します。Git 未初期化の制作物は、祖先の xrift-poc に誤って振り分けず、操作を止めます。

## 更新

### エージェント

zsh の `update` を実行します。XRift CLI と Context7 CLI（`ctx7`）は固定した版なので、`update` では更新しません。`ctx7` の更新の手順は [.devcontainer/README.md](.devcontainer/README.md) の「Context7 CLI（ctx7）」にあります。

CI（`.github/workflows/ci.yml`）は、pi（`@earendil-works/pi-coding-agent`）を確かめた版に固定して入れています。`update` で Dev Container の pi が新しくなったら、`pi --version` で版を確かめ、`ci.yml` の pi の版も同じ版に変えて、PR で検証を通します。

### XRift CLI の更新

1. `npm view @xrift/cli version` で新しい版を確かめます
2. `.devcontainer/install_beforehand.bash` の `XRIFT_CLI_VERSION` を変えます
3. 新しい版に入れ替えます。`npm install -g @xrift/cli@<新しい版>` を実行するか、Rebuild Container します。PATH 上の `xrift` は、入れ替えるまで古い版のままです
4. `xrift --version` が新しい版と一致することを確かめます
5. 「ワールドとアイテム」の手順でワールドとアイテムを新しく作り、`dist/` の無い状態で `xrift check --build` を実行します。ビルド（`Running build command`）の後に検査が通ることを確かめます。警告が出たら理由を確かめます
6. 続けて `dist/` を消し、`xrift check world --build`（アイテムは `xrift check item --build`）がビルドしてから検査するかを確かめます。0.25.0 ではビルドせずに `Dist directory not found` で失敗します（[#57](https://github.com/usaturn/xrift-poc/issues/57)）。ビルドするようになっていれば、`AGENTS.md` の「XRift の制作」の注記と、`xrift check --build` にそろえた手順を見直します
7. PR で取り込み、[PORTING.md](PORTING.md) の「検証の記録」に確かめた版を追記します

### XRift スキルの更新

1. `WebXR-JP/xrift-skills` の新しいコミットを確かめます（必要なら `skills` CLI の版も確かめます）
2. `scripts/install_xrift_skills.bash` の `XRIFT_SKILLS_REF`（必要なら `SKILLS_CLI_VERSION`）を変えます
3. `bash scripts/install_xrift_skills.bash` を実行します。固定した版と取得結果が違う、または取得物が Git で無視されていないときは、失敗します
4. 新しいスキルの名前が `xrift-` で始まらなければ、`.gitignore` に加えます
5. `.agents/skills/xrift-*` に新しいスキルが入り、`git status` に取得物が出ないことを確かめます。Claude Code・pi・Codex で、制作物から `xrift-world` が読み込まれることも確かめます
6. PR で取り込みます

移植元（usaturn/agent-workspace。移植の時点の名前は maatlog-poc）の修正の取り込み方は [PORTING.md](PORTING.md) の「更新の方法」にあります。

## 検証

```bash
./scripts/ci/verify-devenv.sh
```

PR では GitHub Actions が同じ検証を実行します。

## 変更の進め方

- 開発環境の変更は、最新の `origin/main` から feature branch（worktree）を作り、PR で取り込みます。`main` には直接 commit しません
- 制作物の変更は、その制作物自身のリポジトリで行います。このリポジトリに commit したり Issue を作ったりしません
- クリーンアップ（`.devcontainer/cleanup-workspace.bash`）は `/tmp` の作業用ファイルだけを対象にし、`items/`・`worlds/` には触りません
