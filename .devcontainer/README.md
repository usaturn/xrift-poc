# 個人開発環境

このディレクトリとルートのエージェント設定は、xrift-poc で実ファイルとして管理する。
XRift のワールドとアイテムを作る開発環境であり、制作物（`items/<name>/`・`worlds/<name>/`）は
管理しない。制作物は親の `.gitignore` で無視し、セットアップは作らず、clone もしない。
開発環境は `usaturn/maatlog-poc` から移植した。移植元のコミットと変更点は、ルートの `PORTING.md` に記録する。

`install_beforehand.bash` は、親の `tools/devenv` の locked な依存を使って Playwright を導入する。

`postCreateCommand` は `/bin/bash .devcontainer/install_beforehand.bash` である。
`.devcontainer/` を変更した場合は Dev Containers: Rebuild Container を実行する。
Herdr の復元手順は `.devcontainer/docs/herdr-session-cwd-restore.md` を参照する。

## ホームの bind mount と共有

`devcontainer.json` は、ホストの次のディレクトリとファイルをコンテナにそのまま bind mount する。`initialize.bash` がホスト側で、無いものだけを作る（既存のものは変えない）。

`~/.codex`・`~/.claude`・`~/.claude.json`・`~/.pi`・`~/.local/share/devin`・`~/.config/devin`・`~/.local/share/cursor-agent`・`~/.config/gh`・`~/.cursor`・`~/.config/cursor`・`~/.config/gcloud`・`~/.xrift`

- これらは maatlog-poc など、同じホームを mount する他のコンテナと共有になる。セットアップ（`install_beforehand.bash`）は共有のディレクトリに対して、次の 3 種類の操作をする
  - 無いときだけ置く: `~/.pi/agent/models.json`、`~/.pi/agent/extensions/subagent.d/<launcher>/config.json`（`setup_pi_superagents.zsh --if-absent`）。既存の利用者の編集は書き換えない
  - 導入・更新する（再実行のたびに実行し、共有のディレクトリを書き換える）:
    - Claude Code のマーケットプレイス `dashed/claude-marketplace` の追加と、プラグイン 3 種（`example-skills`・`web-quality-skills`・`ripgrep`）の導入（`~/.claude`）
    - `npx skills add ... -g` による `herdr`・`find-skills` のグローバルスキルの導入（`~/.claude`・`~/.pi`）。XRift スキルは `-g` を付けず、親のリポジトリ内（`.agents/skills`・`.claude/skills`）に導入するので、共有のディレクトリは書き換えない
    - pi の拡張の導入（`@teelicht/pi-superagents` と、`git:github.com/obra/superpowers`・`npm:@plannotator/pi-extension` など。`~/.pi`）
    - Devin のプラグイン `obra/superpowers` の導入（ログイン済みのときだけ。`~/.local/share/devin`・`~/.config/devin`）
    - Claude Code・Cursor CLI・Devin CLI・Codex CLI のインストーラの実行（各ツールの本体や設定が、mount 先に書かれることがある）
  - 所有権を直す: bind mount 先が root 所有で作られていたとき、そのディレクトリ自身だけを現在のユーザーに `chown` する（再帰しない）
- `~/.config` そのものは bind mount していない（mount するのは上記の `~/.config/devin`・`~/.config/gh`・`~/.config/cursor`・`~/.config/gcloud` だけ）。次の設定はコンテナの中にだけあり、他のコンテナとは共有しない。リビルドすると消え、`postCreateCommand` の `install_beforehand.bash` がもう一度置く
  - 無いときだけ置く: `~/.config/agent-runner/config.toml`（`XDG_CONFIG_HOME` が絶対パスならその下）、`~/.config/herdr/config.toml`。同じコンテナで再実行しても、既存の編集は書き換えない
  - 毎回上書きする: `~/.config/starship.toml`。設定の元は `.devcontainer/starship.toml` で、コンテナの中で編集しても、セットアップの再実行やリビルドで雛形の内容に戻る。変更を残すときは `.devcontainer/starship.toml` を編集する
- `~/.xrift` は XRift CLI の認証情報（`config.json`）の保存先。`initialize.bash` が新しく作るときは権限を 700 にする。既にあるディレクトリの権限と中身は変えない。ディレクトリ以外のもの（シンボリックリンクを含む）がある場合は、理由を表示して止まる
- `containerEnv` の `XRIFT_POC_ROOT` に、親の main checkout のパス（`${containerWorkspaceFolder}`）を設定する。`route_target.py`・`project_link.py`・`xrift-link` が使う

## Grok Build 撤去後の移行（#162）

Grok Build（`grok` コマンド）は導入・更新・mount の対象から外した。
Pi の `pixg`、Cursor CLI の `cursor-grok47` と alias `cur`、
Grok のモデル名は引き続き使える。

1. **マージ前でも実施できる個人設定の移行:** 有効な agent-runner の個人設定
   （`AGENT_RUNNER_CONFIG` → 絶対パスの
   `XDG_CONFIG_HOME/agent-runner/config.toml` → `~/.config/agent-runner/config.toml`
   の順）をバックアップして編集する。雛形の全文コピーが残っている場合は
   `[agents.grok-build]` のテーブル全体を削除する。
   別名でも `runtime = "grok-build"` の agent があれば削除し、
   workflow の `agents`・`fallback` にあるその agent への参照も外す。
   Pi / Cursor の Grok の担当は削除しない。
   古い定義が1件でも残ると、撤去後は未割り当てでも設定の検証に失敗し、
   runner のすべてのコマンドが `{"error": "invalid_value"}` で止まる。
   導入処理の再実行では既存の個人設定は更新されない。
2. **コンテナは再作成を推奨:** `Dev Containers: Rebuild Container` を利用者が実行する。
   これで古い `~/.grok` の mount とコンテナ内の `~/.local/bin/grok` のリンクが無くなり、
   新しい postCreate は Grok Build を導入しない。
   ホストの `~/.grok`（bin・認証情報・グローバルスキルを含む）は削除しない。
3. **再作成しない場合:** 古い mount は残る。コンテナ内で
   `ls -l "$HOME/.local/bin/grok"` と `readlink "$HOME/.local/bin/grok"` を確認し、
   次のように想定したリンクだけを削除する。リンク先のファイルや
   `~/.grok` の中身には触れない。別の実体・リンク先なら削除せず確認する。

   ```bash
   if [ -L "$HOME/.local/bin/grok" ] &&
      [ "$(readlink "$HOME/.local/bin/grok")" = "$HOME/.grok/bin/grok" ]; then
       rm -- "${HOME:?}/.local/bin/grok"
   fi
   ```

再作成後は新しいシェルで `command -v grok` が何も返さず、
postCreate が成功したことを確かめる。個人設定の移行後、親の root で
`uv run --project tools/devenv python -m tools.agent_runner check` と
`uv run --project tools/devenv python -m tools.agent_runner list` を実行する。
続けて実際の PR 修正ループの `review` が動くことを確認する。
これらのホスト操作・実サービスの確認は利用者が行う。

## コンテナの権限と認証情報

- `runArgs` の `--cap-add=SYS_ADMIN`・`seccomp=unconfined`・`apparmor=unconfined` は、Codex CLI の Linux sandbox（bubblewrap・user namespace）を Dev Container と WSL2 で動かすために付けている。コンテナに与える権限が増えることを理解したうえで使う
- `ccd`・`cod`・`cur`・`dev` などのランチャーは、各エージェントの権限の確認を省略して起動する。移植元と同じ設定のまま移植した
- bind mount したホームのファイルは、コンテナ内のどのプロセスからも読み書きできる。コンテナに入れただけでは認証情報は隔離されない
- `userEnvProbe: loginShell` は、`~/.zshrc` の `herdrstart` が環境の取得中に Herdr を起動してハングし、Herdr の作業ディレクトリを汚すのを避けるために指定している

## ポート

| ポート | 用途 |
|---|---|
| 5173 | XRift のプレビュー（Vite）。`npm run dev -- --host 0.0.0.0`。`forwardPorts` に明示 |
| 5174〜5179 | 複数のプロジェクトを同時に動かすときの Vite（`--port 5174 --strictPort` など）。全ポートを `forwardPorts` に明示 |
| 3000 | `xrift login` のコールバック（CLI が `localhost:3000/callback` に固定している）。`requireLocalPort` により、ホスト側で使われていると転送が失敗する。使っているプロセスを止めてからやり直す |
| 19432〜19441 | Pi / Superpowers の Plannotator レビュー。空きポートを使う。全ポートを `forwardPorts` に明示 |

`forwardPorts` はコンテナからホストへ転送するポート、`portsAttributes` は転送されたポートのラベルなどの設定です。ラベルだけでは転送を保証しません（[Dev Container 仕様](https://containers.dev/implementors/json_reference/#general-properties)）。プレビューのホスト側ポートが使用中なら別のポートへ転送されることがあるため、VS Code の「ポート」の転送先を確認します。

### 追加プレビューの転送とホスト確認

設定ファイルを編集しただけで、既存の接続も直ったとは扱いません。現在のコンテナで追加分が見えない場合は、VS Code の「ポート」→「ポートの転送」で、使うコンテナ側ポート（5174 など）を追加します。新しい設定を使うには、利用者が `Dev Containers: Rebuild Container` を実行してください。エージェントはホストの再作成を代行しません。

転送を追加した後、または再作成した後は、次の順でワールドとアイテムの両方を確認します。

1. コンテナでターミナルを2つ開きます。`<world>` と `<item>` は、親の main checkout の下にある実際の制作物名へ置き換えます。

   ```bash
   # ターミナル1: ワールド
   cd "$XRIFT_POC_ROOT/worlds/<world>"
   npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
   ```

   ```bash
   # ターミナル2: アイテム
   cd "$XRIFT_POC_ROOT/items/<item>"
   npm run dev -- --host 0.0.0.0 --port 5174 --strictPort
   ```

   `--strictPort` は、コンテナ側で指定ポートが使用中のときに別ポートへ移るのを防ぎます。起動に失敗したら先にその原因を確認し、両サーバーが起動した状態を保ちます。
2. VS Code の「ポート」で、コンテナ側の 5173 と 5174、およびそれぞれのホスト側の転送先を確認します。行が無ければ手動で転送を追加します。
3. 両サーバーを起動したまま、ホストのブラウザで実際の転送先 URL を開き、ワールドとアイテムの描画を確認します。同じポートへ転送されていれば `http://localhost:5173` と `http://localhost:5174` です。コンテナ内 HTTP 200 や Playwright の描画だけでは、ホストから表示できたことにはしません。
4. [Issue #59](https://github.com/usaturn/xrift-poc/issues/59) に、手動転送か再作成後か、両制作物の実ディレクトリ、実際の起動コマンドとポート、VS Code に表示されたコンテナ側／ホスト側ポート、両 URL と表示結果を記録します。未確認の結果は成功と書きません。
5. ホスト確認が終わってから、各ターミナルで `Ctrl+C` を押して確認用サーバーを止めます。

## XRift の導入と作業ディレクトリ

`install_beforehand.bash` は次を行う。再実行しても、既存の `items/`・`worlds/` の中身は変えない。

- `items/` と `worlds/` を、無ければ作る。シンボリックリンクや通常のファイルがあるときは、理由を表示して止まる（Git の `/items/` は、ディレクトリにだけ一致するため）
- XRift CLI（`@xrift/cli`）を 0.25.0 に固定して導入し、`xrift --version` が一致することを確かめる
- `scripts/install_xrift_skills.bash` で、XRift 公式スキルを固定した版（`skills` CLI 1.7.2、`WebXR-JP/xrift-skills` のコミット 41d6504）で取得する。取得したスキルが固定したコミットと違う場合、または Git で無視されない場合は失敗する
- `scripts/write_shell_block.py` で `zshrc.txt` を `~/.zshrc` に書く。書く範囲は `# >>> xrift-poc shell setup >>>` と `# <<< xrift-poc shell setup <<<` の間だけで、再実行しても 1 つのブロックのままになる。ブロックの外の利用者の設定は変えない
- Playwright の chromium を、`tools/devenv` の locked な依存で導入する

`zshrc.txt` が定義する主な補助は次のとおり。

- `cod`: Codex を `project_root_markers=[".devcontainer"]` つきで起動し、制作物の中からでも親の `AGENTS.md` を読ませる
- `xrift-link`: 制作物に、親のスキル（独立した Git リポジトリの制作物だけ）と Pi の設定（`.pi/` の `APPEND_SYSTEM.md`・`mcp.json`・`settings.json`）へのリンクを置く・外す（`xrift-link [link|unlink] [dir]`）。Pi は起動したディレクトリの `.pi/` しか読まないため、Git 未初期化の制作物でも Pi を使うなら実行する（ルートの README.md の「エージェントを制作物から起動する」）
- `xrift_poc_periodic_cleanup`: シェルの起動時に、`cleanup-workspace.bash` を 24 時間に 1 回の間隔で実行する。状態は `~/.cache/xrift-poc` に置く（`XRIFT_POC_CLEANUP_STATE_DIR` で変えられる）。`items/`・`worlds/` は片付けの対象にしない。一時 worktree の `items/`・`worlds/` にデータがあれば、その worktree を残す。`/tmp/review-fix-planning.*` は、中のどれかが 24 時間以内に更新されたものと、clone・submodule などの自分の履歴を持つリポジトリや、どの ref にも含まれない detached HEAD の commit を含むものも残す

XRift 認証（`xrift login`）は利用者が手動で行う。ポートは前節を参照する。

## Context7 CLI（ctx7）

Context7 のドキュメントの取得は、`ctx7` CLI と `find-docs` スキル（`.agents/skills/find-docs`）で行う。Pi・Claude Code・Codex は同じ `find-docs` を読む。Context7 の MCP Server と Pi の Context7 拡張（`npm:@upstash/context7-pi`）は、xrift-poc では使わない（#49）。2026-10-18 に、npx で起動する Context7 の MCP Server が `~/.npm/_npx` の壊れた展開物（`safer-buffer` の `MODULE_NOT_FOUND`）で Pi・Claude Code とも接続できなくなったため、npx と MCP Server の起動に依存しない形にした。

### 導入

- `install_beforehand.bash` は、Context7 CLI（`ctx7`）を 0.8.0 に固定して導入し、`ctx7 --version` が一致することを確かめる。導入の失敗・PATH に無い・版の不一致は、セットアップの失敗として止まる
- 実体は npm のグローバルの `node_modules` の下に入る。`~/.npm/_npx` の一時展開物は使わない
- `ctx7 setup` は実行しない。ctx7 0.8.0 の `ctx7 setup` は、`~/.claude`・`~/.claude.json`・`~/.codex/config.toml`・`~/.codex/AGENTS.md`・`~/.agents/skills` などに MCP 設定やスキルを書き込む。その多くはホストから bind mount した共有の場所で、ホストや他のコンテナにも影響する。生成されるスキルは `find-docs` とも重複する
- zsh の `update` では更新しない

### 更新

1. `npm view ctx7 version engines` で新しい版と、必要な Node.js の版を確かめる
2. `install_beforehand.bash` の `CTX7_CLI_VERSION` を変える
3. 新しい版に入れ替える。`npm install -g ctx7@<新しい版>` を実行するか、Rebuild Container する。PATH 上の `ctx7` は、入れ替えるまで古い版のままである
4. `ctx7 --version` が新しい版と一致し、下の「動作確認」が通ることを確かめる
5. PR で取り込む

`ctx7` は新しい版があると、対話的な端末で `Update available` と表示し、`ctx7 upgrade` や `npm install -g ctx7@latest` を勧める。この案内には従わず、上の手順で版を変える。

### MCP と Pi の拡張からの移行

リポジトリの設定から、次を外した。

- `.mcp.json`（Claude Code）と `.pi/mcp.json`（Pi）の `context7`。`google-dev-knowledge` は残す
- `.claude/settings.local.json` の `enabledMcpjsonServers`（`context7` だけだった）。`enableAllProjectMcpServers: true` は `google-dev-knowledge` のために残す
- `install_beforehand.bash` が導入する Pi の拡張の一覧の `npm:@upstash/context7-pi`

リポジトリの外の Context7 の設定は、セットアップでは変えない。外すかどうかは利用者が決める。

| 設定 | 場所 | 外し方 |
|---|---|---|
| Codex の MCP | `~/.codex/config.toml` の `mcp_servers.context7` | その表を消す |
| Claude Code のユーザースコープの MCP | `~/.claude.json` の `mcpServers` | `claude mcp remove context7 -s user` |
| claude.ai の Context7 コネクタ（`mcp__claude_ai_Context7__*`） | claude.ai のアカウントの設定 | claude.ai のコネクタの設定で外す。アカウント単位の設定で、他のプロジェクトにも効く |
| Pi の Context7 拡張 | 共有の `~/.pi/agent/settings.json` | `pi remove npm:@upstash/context7-pi`。`~/.pi` を共有する他のコンテナの Pi にも効くので、先にそれらの Pi を止める。共有の `~/.pi` からは 2026-10-20 に外した（次の節） |

### Pi の Context7 拡張（共有の `~/.pi` から外した後）

共有の `~/.pi` に `npm:@upstash/context7-pi` を入れていたセットアップ（usaturn/agent-workspace）は usaturn/agent-workspace#725 で入れなくなり、共有の `~/.pi` からも 2026-10-20 に外した。これに合わせて、#49 で `.pi/settings.json` の `packages` に置いた、読み込みを止める項目（`autoload: false` と `"!**"`）も外した（#54）。いまの xrift-poc の Pi は、context7-pi を導入も読み込みもしない。

項目を残さなかったのは、Pi 1.3.0 が次のように動くためである。

- `autoload: false` を付けた project の項目は、ユーザー設定の同じパッケージに対する差分としてだけ働く（Pi 同梱の `docs/packages.md` の「Understand scope and identity」）
- ユーザー設定に同じパッケージが無いと差分の土台が無く、Pi は起動のたびに、まだ導入していない checkout の project の `.pi/npm` に context7-pi を導入する（読み込みはしない）。npm registry に届かないと導入に失敗し、Pi が起動しない（`npm error code ECONNREFUSED`）。`--offline` か `PI_OFFLINE=1` を付けると導入しない
- trust 済みの checkout では、zsh の `update`（`pi update --extensions`）も、ユーザー設定にあるかどうかによらず `.pi/npm` に導入する
- Pi が `.pi/npm` に置く `.gitignore`（`*` と `!.gitignore`）が未追跡のファイルとして残り、agent-runner の監査（`outside_changes`）と `git worktree remove`（`--force` なし）を止める

同じ形（project の差分の項目）で共有の `~/.pi` のパッケージを止めるときは、共有の `~/.pi` から外す前に（遅くとも同時に）project の項目を外す。項目を先に外すと、共有の登録が残る間はそのリポジトリでも読み込むが、起動は registry に依存しない。xrift-poc では共有の `~/.pi` から先に外れたので、#54 までの間は上の導入が起きた。なお、項目で止めるには `"!**"` が要る。空配列（`"extensions": []` など）は差分の「変更なし」として扱われ、ユーザー設定のとおりにすべて読み込む（Pi 1.3.0、2026-10-18 に確認）。

#54 より前の commit から作った worktree・branch では `.pi/settings.json` に項目が残っており、上の導入が起きる。`origin/main` を取り込んで項目を外す。取り込むまでの間に、registry に届かない環境で Pi を起動するときは、`--offline` か `PI_OFFLINE=1` を付ける。

項目が残っていた間に Pi を起動した checkout には、`.pi/npm/` が未追跡のまま残る。項目を外した後の Pi は、この `.pi/npm/` を読み込まず、変えもしない。Git では無視しない（`.gitignore` に足すと、repo-cleanup-after-merge が照合できないまま残り続ける。生成の原因の項目を外して直した）。`.pi/npm/package.json` の `dependencies` が `@upstash/context7-pi` だけであることを確かめてから、`.pi/npm/` を消す。

読み込んでいないことと、`.pi/npm` を作らないことは、モデルを呼ばずに RPC モードで確かめられる。親の main checkout（trust 済み）で実行する。trust していない worktree では `--approve` を付ける。`.pi/npm/` が残っている checkout では、上の手順で消してから実行する。`context7-pi is not loaded` と `.pi/npm is absent` だけが出れば、読み込まず、導入もしていない。`pi did not start` が出たときは Pi が起動していないので、`context7-pi is not loaded` が出ても読み込んでいないことの確認にならない。

```bash
pi_out="$(printf '{"type":"get_commands"}\n' | pi --mode rpc --no-session --no-mcp)" || echo "pi did not start"
grep -oE 'c7-docs|skill:context7-docs' <<<"$pi_out" || echo "context7-pi is not loaded"
test -e .pi/npm && echo ".pi/npm exists" || echo ".pi/npm is absent"
```

### 認証

- 環境変数 `CONTEXT7_API_KEY` を基本とする。`ctx7` はこの変数があれば、`ctx7 login` の認証情報より優先して使う。`devcontainer.json` では渡していないので、利用者がシェルの設定などで export する。キーの値をリポジトリ・Issue・PR・ログに書かない
- エージェントが実行する `ctx7` に変数が継承されていることは、値を表示せずに確かめる（下の「動作確認」の `node -e`）
- `cod` は、`CONTEXT7_API_KEY` が無いと入力を求める。Codex から `ctx7` を呼ぶときにも使う
- `ctx7 login` の認証情報は `~/.config/context7/credentials.json`（`XDG_CONFIG_HOME` が絶対パスならその下）に保存される。`~/.config` は bind mount していないので、再ビルドで消える。更新確認の状態も `~/.local/state/context7/` にあり、同じく消える
- `ctx7 whoami` は `ctx7 login` の状態だけを表示する。`CONTEXT7_API_KEY` を使っていても `Not logged in.` と出る
- 認証なしでも使えるが、レート制限が低い。quota を超えると `Monthly quota reached` などで失敗する。`find-docs` の手順で利用者に伝え、`CONTEXT7_API_KEY` の設定か `ctx7 login` を案内する

### 動作確認

```bash
node --version
command -v ctx7
readlink -f "$(command -v ctx7)"   # .../lib/node_modules/ctx7/dist/index.js（~/.npm/_npx ではない）
ctx7 --version                     # CTX7_CLI_VERSION と一致する
node -e 'console.log(process.env.CONTEXT7_API_KEY ? "CONTEXT7_API_KEY: set" : "CONTEXT7_API_KEY: not set")'
ctx7 library react "How to clean up useEffect with async operations"
ctx7 docs /react/react "useEffect cleanup"
```

加えて、Pi・Claude Code で `find-docs` を使った取得を試す。Claude Code の `/mcp`・`claude mcp list` と Pi の `pi mcp list` に Context7 が無く、`google-dev-knowledge` が接続できることを確かめる。

### トラブルシュート

| 症状 | 対応 |
|---|---|
| `ctx7: command not found` | Rebuild Container するか、`npm install -g "ctx7@<CTX7_CLI_VERSION の値>"` を実行する。`npx ctx7` で代えない |
| セットアップが `Unexpected ctx7 version` で止まる | `command -v ctx7` で、PATH の前のほうに別の `ctx7` が無いかを確かめる |
| `⚠ Library has been redirected` と出て終了コード 1 | 表示された新しい ID でやり直す（例: `/facebook/react` → `/react/react`） |
| `Monthly quota reached`・`quota exceeded` | `CONTEXT7_API_KEY` を設定するか、`ctx7 login` を実行する |
| `Update available` と表示される | 従わない。「更新」の手順で版を変える |
| `context7` が `Failed to reconnect`・`CONNECTION_CLOSED` のまま一覧に残る | 古い設定を読んでいる。親の main checkout を `origin/main` に更新し、エージェントを起動し直す。親の中に置いた worktree（`.claude/worktrees/` など）では、Claude Code が祖先の main checkout の `.mcp.json` も読む。ユーザースコープの設定（前の表）も確かめる |
| xrift-poc の Pi に `resolve-library-id`・`/c7-docs` が出る | ユーザー設定（`~/.pi/agent/settings.json`）に `npm:@upstash/context7-pi` が残っている。xrift-poc は project の設定で止めていない（#54）。外し方は前の表 |
| Pi が起動しない（`npm error code ECONNREFUSED`）、または checkout に `.pi/npm/` ができる | その checkout の `.pi/settings.json` に context7-pi の項目が残っている（#54 より前の commit）。`origin/main` を取り込む。急ぐときは `--offline` か `PI_OFFLINE=1` を付けて起動する。できた `.pi/npm/` の扱いは「Pi の Context7 拡張（共有の `~/.pi` から外した後）」 |

## 廃止した pi 拡張の警告

共有の `~/.pi` に、廃止済みの拡張（`npm:pi-subagents`・`npm:pi-mcp-adapter`・`npm:pi-distill`・`npm:pi-extensions-tool-display`・`npm:pi-extensions-i18n`・`npm:pi-lens`）が残っていると、`install_beforehand.bash` は警告を表示する。警告には削除のコマンド（`pi remove npm:<パッケージ名>`）を添える。セットアップはこれらを削除しない。`~/.pi` は他のコンテナと共有のため、削除は利用者が自分で行う。削除すると、`~/.pi` を共有する他のコンテナで動いている Pi からも拡張のファイルが消える。`pi remove` の前に、それらの Pi を止める（警告にも添える）。

`npm:pi-subagents` が残っていると `@teelicht/pi-superagents` と `subagent` ツール名が衝突し、`pigo` などのランチャーが起動できない。また `~/.pi/agent/settings.json` の `extensions` に `-builtin:mcp` が残っている場合も、削除を促す警告を表示する。

## uv の Python 実行環境（#561）

通常の開発・検証では、uv で使う・入れる Python を **3.14 以上**に揃える。
`tools/devenv/pyproject.toml` の要件も
`requires-python = ">=3.14"` である。PR レビュー helper は Python 3.14 の
構文を使うため、旧版で起動すると処理を始める前に SyntaxError になる。

uv は既定では uv 管理の Python をシステムの Python より優先する。
旧版を一度導入すると、上位に `.venv` が無い `/tmp` の worktree などで
`uv run --no-project python` が旧版を選ぶことがある。これを防ぐため、
`devcontainer.json` の `containerEnv` に次を設定している。

```json
{
  "UV_PYTHON_PREFERENCE": "system",
  "UV_PYTHON_DOWNLOADS": "manual"
}
```

- `system`: uv 管理の旧版があっても、通常の探索ではシステムの Python を優先する。
  このコンテナの `/usr/bin/python3` は 3.14 以上であることが前提。
- `manual`: 必要な版が見つからないとき、`uv run` などが自動で Python を
  ダウンロードしない。明示的な `uv python install` は許可する。

`containerEnv` を使うので、cwd に関係なく、Herdr のペインや各エージェントを含む
コンテナ内のプロセスに同じ設定が渡る。ユーザー別の `uv.toml` や
プロジェクトの設定、`install_beforehand.bash` に同じ設定を重複して置かない。
Dev Container 外の環境や CI の Python 選択は変えない。

これは版番号の強制フィルターではない。`--python` / `UV_PYTHON` による明示指定、
有効な仮想環境や探索された `.venv`、システム側の別の Python は別途確認する。
既に 3.12 が入っていれば `--python 3.12` は使えるため、通常環境では
旧版を使う・入れる操作をしない。設定だけでは既存の旧版 venv も更新されない。
uv の仕様は [Python versions](https://docs.astral.sh/uv/concepts/python-versions/) を参照する。

### 3.14 未満で互換性を確認する場合

必要な場合に限り、一意な一時ディレクトリへ interpreter とキャッシュを隔離する。
次は 3.12 の確認例。`--no-bin` により通常の `~/.local/bin` に executable を残さず、
終了時（失敗時を含む）に一時領域を削除する。必要なパッケージを `--with` で
追加する場合も、同じ `UV_CACHE_DIR` を使う。

```bash
(
    set -eu
    PYTHON_CHECK_DIR="$(mktemp -d)"
    trap 'rm -rf -- "${PYTHON_CHECK_DIR:?}"' EXIT
    cd -- "$PYTHON_CHECK_DIR"
    env -u UV_PYTHON -u VIRTUAL_ENV \
        UV_PYTHON_INSTALL_DIR="$PYTHON_CHECK_DIR/python" \
        UV_CACHE_DIR="$PYTHON_CHECK_DIR/cache" \
        UV_PYTHON_DOWNLOADS=manual \
        uv python install --no-bin 3.12
    env -u UV_PYTHON -u VIRTUAL_ENV \
        UV_PYTHON_INSTALL_DIR="$PYTHON_CHECK_DIR/python" \
        UV_CACHE_DIR="$PYTHON_CHECK_DIR/cache" \
        UV_PYTHON_DOWNLOADS=manual \
        uv run --no-project --python 3.12 python -c 'import sys; print(sys.version)'
)
```

これらの変数を通常のシェルへ export しない。互換性確認に使う旧版で
リポジトリの helper を実行せず、確認が済んだら通常の 3.14 以上へ戻る。

## Codex で `CLAUDE.md` を指示ファイルとして読む

Codex は標準では `CLAUDE.md` を自動で読まない。
`~/.codex/config.toml` の最初の `[projects...]` などのテーブルより前に、次のトップレベル設定を追加する。
既存の設定は残し、同じキーがある場合はその配列に `CLAUDE.md` を加える。

```toml
project_doc_fallback_filenames = ["CLAUDE.md"]
```

保存後に Codex を再起動し、新しいセッションで「読み込んだ指示ファイルを列挙して」と尋ね、リポジトリ直下の `CLAUDE.md` が含まれることを確認する。
同じディレクトリに `AGENTS.override.md` または `AGENTS.md` がある場合はそちらが優先される。
この設定は Dev Container に bind mount された `~/.codex` に保存される。
詳しくは [Codex の指示ファイルの説明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)を参照する。

## 汎用の非対話エージェント実行器（agent-runner）

端末 UI を操作せず、agent（起動方法）・role（仕事）・workflow（割り当て）の
3 層設定から CLI を起動し、同期で結果を回収する。
共通仕様は [tools/agent_runner/interfaces.rst](../tools/agent_runner/interfaces.rst) に定義する。
#560 は基盤だけを提供し、五ランタイムの具体的な adapter と agent 定義は
#565 / #566、PR レビューの接続は #568 が追加する。基盤だけの状態では
五ランタイムは「未対応」と表示される。別モデルや Herdr への暗黙の代替はしない。

### 設定と確認

個人設定は `AGENT_RUNNER_CONFIG`、絶対パスの
`XDG_CONFIG_HOME/agent-runner/config.toml`、`~/.config/agent-runner/config.toml` の順。
導入処理は後二者の場所に設定が無いときだけ
`.devcontainer/agent-runner.toml` をコピーする。既存設定は更新しない。
個人設定が無い場合は雛形だけで動く。壊れたリンクやディレクトリを指すリンクは、
設定が無いとは扱わず、CLI・各スキルの入口とも読み込みエラーで止まる。
雛形に定義された agent・role・workflow は、雛形のファイルで確認する。

設定は雛形 < 個人設定 < `--assign role=agent1,agent2` の順で上書きする。
table は統合、配列は置換。割り当てに `self` だけを指定すると外部起動しない。
同じ役割のモデル重複、未知キー・版、不正な参照を拒否する。
fallback は設定に列挙した場合だけ試し、役割内で一つの候補を複数の担当に重複使用しない。
候補切れは fallback_exhausted と元の失敗を返す。権限設定は非対話実行だけに限定する。
資格情報の値は書かず、`env` に変数の名前を指定する。

親リポジトリの作業対象 root で実行する。

```bash
uv run --project tools/devenv python -m tools.agent_runner check
uv run --project tools/devenv python -m tools.agent_runner list
uv run --project tools/devenv python -m tools.agent_runner \
  --config /path/to/personal.toml run --workflow audit \
  --repository /path/to/repository --revision HEAD \
  --prompt-file /path/to/prompt.txt --concurrency 2 --assign inspect=agent-a
```

`--template` / `--config` はサブコマンドの前に置く。
未対応でも妥当な設定なら check/list は成功するが、run は unsupported で返す。
プロンプトはファイルから読み、adapter が stdin または依頼ファイルで渡す。
コマンド引数へ本文を埋め込まない。

### レビュアーの選び方

PR レビュー（workflow `review` の role `inspect`）のレビュアーは固定しない。
雛形の `[workflows.review.assignments.inspect]` は、指定が無いときの既定値であり、
性能を評価して決めた順位ではない。

- 依頼ごとに選ぶ: `run --workflow review` に `--assign inspect=claude-opus,pi-gpt` を付ける
- いつも使う構成を変える: 個人設定の同じ節の `agents` を書き換える。
  個人設定を新しく書く場合は、この節だけ書けば雛形に重なる（配列は置換）

```toml
[workflows.review.assignments.inspect]
agents = ["claude-opus", "pi-gpt"]
```

runner は親の main checkout と共有の Git 管理領域を担当から読み取り専用にする。CLI の `run --workflow review` で
pr-review の helper を使わせるときは、`--write-exception` の作業範囲の指示とは別に、helper の書き込み先を
`--writable-path`（ignored の既存ディレクトリか保存ロック）と `--private-review-refs` で渡す。
例外の行のパスは mount の許可にならない。`tools/devenv/.venv` が無ければその指定を外す。
PR 修正ループと `run_roles.py` は同じ許可を自分で渡す。

```bash
mkdir -p "$PARENT/reviews/<owner>/<repo>" "$PARENT/tmp/agents"
uv run --project tools/devenv python -m tools.agent_runner run --workflow review \
  --repository "$PARENT" --revision HEAD --prompt-file /path/to/prompt.txt \
  --assign inspect=claude-opus,pi-gpt \
  --write-exception "$PARENT/reviews: the pr-review helper's saved review" \
  --writable-path "$PARENT/reviews/<owner>/<repo>" --writable-path "$PARENT/tmp/agents" \
  --writable-path "$PARENT/.git/pr-review-save.lock" --writable-path "$PARENT/tools/devenv/.venv" \
  --private-review-refs
```

同じ役割に、同じ `model` の agent は並べられない。例えば `pi-gpt` と同じモデルの担当を
個人設定で別名にしても、`pi-gpt` と同時には割り当てられない。

`codex` は、レビュアー（inspect）にも、レビュー修正の self-review・fix-check にも割り当てられない（#52）。
codex の adapter は writes=false の役割を `codex exec -s read-only` で起動し、OS の sandbox がすべての書き込みを拒否する。
PR レビューの helper は writes=false でも保存・uv のキャッシュ・Git の worktree の登録に書くので、codex では動かない。
PR 修正ループの `check-options`・`start` と `run_roles.py` は、この割り当て（fallback を含む）を終了コード 2 で止める。
runner も、`--write-exception` を付けた `run` を何も作らずに `invalid_input` で止める。
`--write-exception` を付けない `run` は止まらず、レビューが失敗する。
GPT をレビュアーにするときは `pi-gpt` を使う。codex は implement（writes=true の役割）には割り当てられる。
理由と、書き込みを許可しない判断は `tools/agent_runner/runtimes/codex.rst` に記録した。
雛形に無い agent は、個人設定の `[agents.<name>]` に定義してから指定する。
雛形の既定値を変えても、既存の個人設定には反映されない。

runtime が `cursor` の担当（雛形の `cursor`・`cursor-grok47`）は、writes=false の役割
（`inspect`・`self-review`・`fix-check`）も `-f --sandbox disabled` で起動する（#75。writes=true の役割は今までどおり `-f` だけ）。
シェルを使えるので、PR レビューの役割でも pr-review・review-verify の helper を実行できる。
writes=false は claude-code・devin・pi と同じく runner の指示であり、sandbox ではない。
開始前から登録済みで保護できる checkout は、どの runtime でも runner が読み取り専用にし、変化は `outside_changes` として報告する。
Ask mode では pr-review の helper を実行できないことと、Cursor の sandbox の範囲・実機の確認は `tools/agent_runner/runtimes/cursor.rst` に記録した。

### レビューの後処理

雛形の workflow `review` は、run の終了後に後処理（`after_run`）として
pr-review の `review_run.py attach-runner --result <result.json>` を呼ぶ。
各レビュアーの実行記録（`reviews/<owner>/<repo>/#<PR>-<branch>/runs/`）に、
ランナーが観測した model・effort・session・CLI の版と試行の結果を書く（#601）。
後処理が失敗・期限切れになっても run の終了コード・プロセスの実測 state は変わらず、
標準出力の JSON summary の `after_run` に結果とログのパスが出る。
観測値の追記を止めるときは個人設定に次を書く。

```toml
[workflows.review]
after_run = []
```

### 完了・停止・保持

run は各担当の独立した detached worktree を確定済み commit から作り、
同時実行数を制限して終了まで待つ。担当は bubblewrap の mount 名前空間で起動し、
開始前から登録済みで保護できる checkout・その Git 管理領域を読み取り専用にし、祖先のディレクトリを rename できない mountpoint にする。
許可した保存先の内側にある登録済み checkout も、保護できるものは読み取り専用のまま残る。
保護できない登録（開始前から監査できない、起動時に無いなど）があっても担当は起動し、`checkout_isolated` を false にする。
監査できる既存 checkout の内容の前後比較も行い、変化は警告として返して止めない。ignored 資源や Git 外の任意のパスは監査範囲外。

結果は `result.json`、stdout/stderr は担当別ログに保存する。
標準出力は run ID・状態・実際の保存先の JSON summary。
終了コードは成功/self=0、失敗/部分成功=1、設定/引数=2、SIGINT=130。
終了コードだけでなく、現在の依頼の識別子と receipt の payload を検証する。

試行ごとの監督プロセスが独立 process group の所有を保持し、CLI が即時終了しても
通常の子孫の停止処理が終わるまで残る。結果の終了コードは元の CLI の実測値であり、
標準出力とは別の制御 channel で受け取る。
期限切れ・中断・異常終了だけでなく、通常終了でも残る所有子孫を止めて確認する。
`stop_confirmed=false` なら成功にせず、fallback も起動しない。
停止確認後は receipt ファイルも秘密値を redact する。
停止未確認・書き換え失敗で秘密が残る可能性のあるファイルは sensitive_paths に報告する。
その内容は転載せず、停止確認と安全な回収を優先する。
ログと worktree は成功時を含め自動で削除しない。
停止確認と成果保存の後、対象 repository の `git worktree remove <work_dir>` で
登録を外し、自分が所有する成果物領域だけを回収する。変更が残る worktree や
停止未確認の領域を勝手に force で消さない。
待ち方は [.agents/monitoring-runtimes.md](../.agents/monitoring-runtimes.md) を参照する。

role/fix-check の入口が `category=environment` と `checkout_audit` を返した場合は、
全登録の接続を確認する。`unavailable_before` は開始前からの監査不能、`unavailable_after` は実行中の
監査不能、`content_changed` は内容の変化、`replaced` は checkout か祖先のディレクトリが同じパスの別のものに
置き換わったことを示す。いずれも worker が変更したという帰属ではない。
`locked`/`prunable` は観測した登録の情報であり、ロック理由や Git stderr は出力しない。

- 本体が移動済みなら、所有者が対象 run の所有・停止を確認し、移動先の `.git`、管理側の
  `.git/worktrees/<管理名>/gitdir`、`commondir` の対応を照合して、対象だけを
  `git worktree repair <移動後のworktreeパス>` で修復する。HEAD と tracked/untracked/ignored の
  内容・モード・symlink を前後で比較し、dirty な資源も失わないことを確認する。
- `prunable` は登録パスに接続できないという情報であり、本体の消失や削除許可の証明ではない。
  移動先に本体が残る場合も表示されるため、repair 前に一括 `git worktree prune` をしない。
- 親ディレクトリが消失していても Git 登録が残る場合は回収済みとしない。loop は残存登録を
  `retained` として報告する。保存済み run/result と実プロセスの停止、登録と所有資源の対応を確認し、
  所有者が対象パスだけの回収を判断する。ディレクトリ不在や `prunable` だけを根拠に削除しない。
- locked、権限不足、壊れた gitdir、未知の所有、別 run の登録は保護する。監査範囲や sandbox を
  狭めて進めず、照合できない対象は残存パスと理由を記録する。

復旧後の**新しい snapshot**で、監査対象の repository の全 checkout を読み取れることを確認する。
診断は親の checkout の root（取り込み前はこの手順を含む feature worktree）で実行する。
監査対象は、`category=environment` を返した role/fix-check の依頼の `repository` である。
制作物ならそのリポジトリの絶対パスを `TARGET_REPOSITORY` に設定する。親自身を調べる場合も同様。
パスは引用して渡すので、空白や改行を含んでいても使える。診断は修復・回収を行わない。

```bash
uv run --project tools/devenv --locked python - "${TARGET_REPOSITORY:?}" <<'PY'
import json
import sys
from pathlib import Path

from tools.agent_runner.workspace import CheckoutAuditError, require_auditable_checkouts

try:
    snapshot = require_auditable_checkouts(Path(sys.argv[1]))
except CheckoutAuditError as error:
    print(json.dumps({"category": "environment", "checkout_audit": [item.model_dump() for item in error.findings]},
                     ensure_ascii=False))
    raise SystemExit(1)
print(json.dumps({"auditable_checkouts": len(snapshot)}))
PY
```

成功した場合だけ、新しい run で role/fix-check を再実行する。開始時に監査不能だった古い run の
警告は、途中で接続が回復しても無変更として追認せず、停止記録や消費済みのレビュー予約を上書きしない。
汎用 runner の子の成功と監査警告は別であり、厳格な受け取り入口は監査不能も拒否する。
例外は `checkout_isolated` が true の run の `content_changed` と、`unavailable_after` の `checkout_missing`
（別のセッションの変更・後片付け）だけで、`replaced` と停止未確認は拒否する。
外部担当の `checkout_isolated` が false なら、`outside_changes` と `checkout_audit` が空でも厳格な入口は拒否する。

## エージェント向けツールの導入

`install_beforehand.bash` は Claude Code のプラグインと Playwright chromium を
導入する。Dev Container を使う限り手動実行は不要で、リビルド時も自動で再実行される。

Dev Container を使わないローカル環境のみ、以下を手動で順に実行する。

1. `claude plugin marketplace add dashed/claude-marketplace`
2. `claude plugin install example-skills@anthropic-agent-skills`
3. `claude plugin install web-quality-skills@addy-web-quality-skills`
4. `claude plugin install ripgrep@alberto-marketplace`
5. `uv run --project tools/devenv --locked playwright install --with-deps chromium`
6. `mkdir -p ~/.pi/agent && cp .devcontainer/pi-models.json ~/.pi/agent/models.json`
7. `mkdir -p ~/.local/bin && install -m 0755 .devcontainer/pigo ~/.local/bin/pigo`
8. `install -m 0755 .devcontainer/pigod ~/.local/bin/pigod`
9. `install -m 0755 .devcontainer/piccg ~/.local/bin/piccg`
10. `install -m 0755 .devcontainer/piccd ~/.local/bin/piccd`
11. `install -m 0755 .devcontainer/piga ~/.local/bin/piga`
12. `install -m 0755 .devcontainer/pigs ~/.local/bin/pigs`
13. `install -m 0755 .devcontainer/pigl ~/.local/bin/pigl`
14. `install -m 0755 .devcontainer/pixg ~/.local/bin/pixg`
15. `install -m 0755 .devcontainer/pigemf ~/.local/bin/pigemf`
16. `install -m 0755 .devcontainer/pigemp ~/.local/bin/pigemp`
17. `install -m 0755 .devcontainer/pigema ~/.local/bin/pigema`
18. `zsh scripts/setup_pi_superagents.zsh`

`--with-deps` は sudo を使うため、エージェントではなく人間が実行する。
OS 依存を省くと `libglib-2.0.so.0` が見つからず起動に失敗する
（Dev Container では `libglib2.0-0t64` を apt で導入済み）。

プラグインの有効化は `.claude/settings.json` の `extraKnownMarketplaces` と
`enabledPlugins` で行う。設定の反映には Claude Code の再起動が必要である。

## pi のモデル定義とランチャー

`~/.pi` は `devcontainer.json` で bind mount している。認証情報
（`~/.pi/agent/auth.json`）とモデル定義（`~/.pi/agent/models.json`）が
ホスト側に残るため、リビルド後に `scripts/setup_pi.zsh`、
`scripts/setup_pi_opencode_go.zsh`、`scripts/setup_pi_commandcode_goat.zsh`
を実行し直す必要はない。

`install_beforehand.bash` は次のものを配置する。挙動が異なるのは、
models.json だけが bind mount の内側で、他が外側にあるためである。

- `.devcontainer/pi-models.json` → `~/.pi/agent/models.json`。
  bind mount 側を正とし、ファイルが**無い場合のみ**配置する。毎回上書きすると、
  ホスト側で加えたモデル定義の変更がリビルドで失われる
- `.devcontainer/pigo` → `~/.local/bin/pigo`。`~/.local/bin` は bind mount の
  対象外でリビルドのたびに失われるため、毎回上書きで配置する
- `.devcontainer/pigod` → `~/.local/bin/pigod`。pigo と同じ理由で毎回上書き
- `.devcontainer/piccg` → `~/.local/bin/piccg`。pigo と同じ理由で毎回上書き
- `.devcontainer/piccd` → `~/.local/bin/piccd`。pigo と同じ理由で毎回上書き
- `.devcontainer/piga`・`pigs`・`pigl` → `~/.local/bin/`。同様に毎回上書き
- `.devcontainer/pixg` → `~/.local/bin/pixg`。同様に毎回上書き
- `.devcontainer/pigemf`・`pigemp`・`pigema` → `~/.local/bin/`。同様に毎回上書き
- `.devcontainer/pi-superagents/<launcher>.json` → `~/.pi/agent/extensions/subagent.d/<launcher>/config.json`、
  `.devcontainer/pi-superagents-exec.zsh` → `~/.local/bin/pi-superagents-exec.zsh`。
  `install_beforehand.bash` は `setup_pi_superagents.zsh` を呼んでこの 2 種を配置する
  （詳細は後述の「pi-superagents のランチャー別 model tier」を参照）

`~/.local/bin` への PATH は `zshrc.txt` が通すため、別途の設定は不要である。

### モデル定義の正

独自のモデル定義は `.devcontainer/pi-models.json` を単一の情報源とする。
`install_beforehand.bash` も 3 つの setup スクリプトも、定義を埋め込まずこの
ファイルから読む。定義を変更するときはこのファイルを編集してコミットする。
`openai-codex` の GPT-6 モデル、`xai` の Grok 4.7、`google-vertex` の
Gemini 3.8 Flash / Gemini 3.1 Pro Preview は Pi の組み込みカタログを使うため、
このファイルには追加しない。

`pigo` は pi を `opencode-go` プロバイダの `muse-spark-1.3-contributor` で
起動するラッパーである。このモデルは pi の組み込みカタログ
（`~/.pi/agent/models-store.json`）に無いため `pi-models.json` で定義する。
定義には `baseUrl`（`https://opencode.ai/zen/go/v1`）が必須で、省略すると
リクエストが opencode-go のエンドポイントに解決されず 404 になる。
また `reasoning.effort` に `max` は指定できない（`none` / `minimal` / `low` /
`medium` / `high` / `xhigh` のみ）ため、`thinkingLevelMap` で `max` を `xhigh` に
対応付けている。

`pigod` は同じ `opencode-go` プロバイダを、既定モデル `deepseek-v4.1-flash` で
起動するラッパーである。`pigo` と違い `pi-models.json` には何も足していない。
`deepseek-v4.1-flash` は pi のモデルカタログ（`~/.pi/agent/models-store.json`）
に `baseUrl`（`https://opencode.ai/zen/go/v1`）付きで存在するため、
`--provider opencode-go` の指定だけでエンドポイントに解決される。カタログは
`pi update --models` で更新されるので、写しを `pi-models.json` に置かずに追随
できる。

カタログは pi.dev 側で更新されるため、モデル ID が改名されることがある。
ID は表示名と一致しないので、OpenCode Go ランチャーの ID は `pi --list-models` で
確認する。Vertex ランチャーは認証を調べない bundled catalog の確認を使う（後述）。
2026-09-11 に `deepseek-flash` が `deepseek-v4.1-flash` へ改名され、
旧 ID を残していた `pigod` は次の警告を出したうえで `opencode-go` の既定モデル
（`kimi-k2.6`）の定義を複製したモデルで起動していた:

```console
Warning: Model "deepseek-flash" not found for provider "opencode-go". Using custom model id.
```

この状態でもリクエスト自体は通るため気づきにくいが、kimi-k2.6 の定義が
使われる結果、context window は 262,144（本来は 1,000,000）、max output は
65,536（本来は 384,000）になり、`supportsReasoningEffort` が false のため
`--thinking max` の `reasoning_effort` も送られない。
`scripts/setup_pi_opencode_go.zsh` の手順 6 は各ランチャーのモデル ID を
`pi --list-models` で検証するので、この種の改名は setup の時点で失敗する。

ID と表示名が一致しないモデルなので注意する。カタログ上の表示名は
`DeepSeek V4.1 Flash` で、ID は `deepseek-v4.1-flash` である。
`deepseek-v4-flash`（表示名 `DeepSeek V4 Flash`、テキスト入力のみ）と
`deepseek-v4-pro` は別のモデルである。

`--thinking max` はこのモデルでも有効である。pi は `thinkingLevelMap` の
`max -> "max"` をそのまま `reasoning_effort` として送り、opencode-go はそれを
受理する。このエンドポイントが受け付ける `reasoning_effort` は `none` /
`minimal` / `low` / `medium` / `high` / `xhigh` / `max` の文字列だけで、数値は
受け付けない（`50` や `100` を送ると 400 になる）。許容値は不正値を送ったときに
エンドポイント自身が返す検証エラーで確認できる。

`piccg` は Muse Spark 1.3 を Command Code GOAT 経由
（`commandcode` プロバイダ、既定モデル `meta/muse-spark-1.3`）で
起動するラッパーである。`meta/muse-spark-1.3-contributor` も定義済みで、
対話中の `/model`（または Ctrl+L）や Ctrl+P で切り替えできる。commandcode は pi の built-in provider では
ない（`opencode-go` と異なる）ため、deepinfra と同じく provider を完全定義
する: `baseUrl`（`https://api.commandcode.ai/provider/v1`）、`apiKey`
（`$COMMANDCODE_API_KEY` を実行時に環境変数から解決）、`api`
（`openai-completions`）、`authHeader`（Bearer 認証）。auth.json は使わない。
`thinkingLevelMap` は opencode-go 版の定義を踏襲し、`pigo` と同じく
`--thinking max` で起動する。GOAT プランはサブスクリプション制で、
リクエスト上限は 5 時間 $14・週 $35・月 $70 の使用量までである。

`piccd` は同じ `commandcode` プロバイダを共有し、既定モデルだけを
`deepseek/deepseek-v4.1-flash` に替えたラッパーである。`--models` はこの
1 件だけなので、Ctrl+P の切替候補も DeepSeek のみになる。reasoning は
`low` / `medium` / `high` / `xhigh` / `max` を受け付ける一方 `minimal` は
拒否するため、`thinkingLevelMap` は `minimal` を `low` に寄せ、他は恒等に
対応付けている。`max` をそのまま送れるので、piccg のような `xhigh` への
丸めは不要である。画像入力には対応せず `input` は `["text"]` としている
（画像は別モデル `deepseek/deepseek-v4-flash-vision-exp` が担う）。

既存の `~/.pi/agent/models.json` がある環境では、初回だけ
`zsh scripts/setup_pi_commandcode_goat.zsh` を実行して `commandcode`
プロバイダを反映する必要がある。`install_beforehand.bash` は `piccg` と
`piccd` を毎回配置する一方 models.json は既存ファイルを上書きしないため、
この一度の実行を省くと `piccg` が pi の `Unknown provider "commandcode"` で
落ちる（`piccg` 自身がこれを検出して setup スクリプトの実行を案内する）。

`piccd` は provider の有無だけでなくモデルの有無も見る。commandcode の
プロバイダ定義はあるが DeepSeek のモデル定義だけが無い環境（piccg を一度
実行済みの環境がこれに当たる）では、pi が
`No models matching "deepseek/deepseek-v4.1-flash"` で落ちるためである。
`piccd` はこの場合も setup スクリプトの実行を案内する。

### ChatGPT Pro の GPT-6 ランチャー

| launcher | Provider / Model |
|---|---|
| `piga` | `openai-codex/gpt-6-astra` |
| `pigs` | `openai-codex/gpt-6.1-sol` |
| `pigl` | `openai-codex/gpt-6-luna` |

3 つとも親 Pi は ChatGPT Pro サブスクリプションの Codex OAuth 認証を使う。
`OPENAI_API_KEY` は不要で、OpenAI API のキー課金用 provider には接続しない。
初回だけ素の `pi` を起動して `/login openai-codex` を実行し、ログインする。
OAuth 情報は `~/.pi/agent/auth.json` に保存される。`~/.pi` は Dev Container に bind mount
されているため、リビルド後もログイン状態を保持する。認証は対話操作が必要で、
`postCreateCommand` から自動ログインしない。未認証のままランチャーを起動した
場合は、このログイン手順を表示して終了する。
`pigs` の `cheap` tier の子 Pi は OpenCode Go の `deepseek-v4.1-flash` を
使う。子 Pi が使う OpenCode Go の資格情報は `setup_pi_opencode_go.zsh` が
`auth.json` に書いた api_key または `OPENCODE_API_KEY` で解決され、`pixg` と
同じく起動前の確認は行わない。`auth.json` に資格情報が無い環境では `cheap`
tier の子 Pi が provider 認証エラーになるため、必要なら `OPENCODE_API_KEY` を
設定して `zsh scripts/setup_pi_opencode_go.zsh` を実行する。

GPT-6.1 Sol のため Pi 0.99.1 以上が必要（GPT-6 Astra / Luna もこの版に含まれる）。利用前に `pi --version` を確認し、
ログイン後に各モデルが `openai-codex` 行にあることを確認する。

```bash
pi --version
pi --list-models gpt-6-astra
pi --list-models gpt-6.1-sol
pi --list-models gpt-6-luna
```

モデル行が無い場合は Pi を更新する。`pi --list-models` は利用可能なモデルを
表示するため、未認証時には Pi がモデル定義を持っていても `openai-codex` 行は
表示されない。3 ランチャーは `--thinking xhigh` で起動する。子 Pi の
`cheap` / `balanced` / `max` tier はランチャーごとに GPT-6 Luna / GPT-6.1
Sol / GPT-6 Astra を割り当てる（`pigs` の `cheap` だけは OpenCode Go の
`deepseek-v4.1-flash`。下表を参照）。

### SuperGrok / X Premium の Grok 4.7 ランチャー

| launcher | Provider / Model |
|---|---|
| `pixg` | `xai/grok-4.7` |

`pixg` の親 Pi と `max` tier は SuperGrok または X Premium
サブスクリプションの xAI OAuth 認証を使い、`cheap` / `balanced` tier の
子 Pi は OpenCode Go の `deepseek-v4.1-flash` を使う。`XAI_API_KEY` は
不要で、xAI API のキー課金には接続しない。
初回だけ素の `pi` を起動して `/login xai` を実行し、
`Sign in with SuperGrok or X Premium` からログインする。OAuth 情報は
`~/.pi/agent/auth.json` に保存される。`~/.pi` は Dev Container に bind mount
されているため、リビルド後もログイン状態を保持する。認証は対話操作が必要で、
`postCreateCommand` から自動ログインしない。xAI が未認証なら、
`pixg` はログイン手順を表示して起動前に終了する。
子 Pi が使う OpenCode Go の資格情報は `setup_pi_opencode_go.zsh` が
`auth.json` に書いた api_key を共有し、`pigo` / `pigod` と同じく起動前の
確認は行わない。

Grok 4.7 と DeepSeek V4.1 Flash は Pi の組み込みカタログにあるため、
`pi-models.json` への定義追加は不要である。ログイン後にそれぞれのモデルを
確認する。

```bash
pi --list-models xai
pi --list-models deepseek-v4.1-flash
```

モデル行が無い場合は Pi を更新する。`pi --list-models` は利用可能なモデルを
表示するため、未認証時には Pi がモデル定義を持っていても該当する
プロバイダの行は表示されない。`pixg` は `--thinking xhigh` で起動する。
Grok 4.7 の thinking level は `low` / `medium` / `high` / `xhigh` で、
`xhigh` が実際の最大値である。子 Pi は `cheap` / `balanced` が
`opencode-go/deepseek-v4.1-flash` の `max`、`max` が `xai/grok-4.7` の
`xhigh` を使う。未認証時に別モデルへ自動フォールバックはしない。

### Vertex AI の Gemini ランチャー（#578・#680）

| launcher | Provider / Model | thinking |
|---|---|---|
| `pigemf` | `google-vertex/gemini-3.8-flash` | `high` |
| `pigemp` | `google-vertex/gemini-3.1-pro-preview` | `high` |
| `pigema` | `google-vertex/gemini-4-argon` | `high` |

3 ランチャーとも Pi の native Google Vertex AI adapter と Application Default
Credentials（ADC）を使う。Gemini API 用の `google` provider ではない。
3 モデルとも Pi 1.3.0 の bundled catalog にあり、独自のモデル定義は追加しない。
thinking level はどれも `low` / `medium` / `high` で、`high` が実際の最大値である。

対応する location と課金の方式はモデルによって異なる（Google の公式情報、2026-10-15 更新）。

- `gemini-3.8-flash`・`gemini-3.1-pro-preview`:
  [Flex PayGo 対応モデル一覧](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/flex-paygo)
  で `global` endpoint の対象
- `gemini-4-argon`:
  [モデルのページ](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-argon)
  で GA。location は `global` と multi-region の `us`・`eu`、課金の方式は
  Standard PayGo・Priority PayGo・Provisioned Throughput。Flex PayGo の一覧には載っていない

location を変更する場合は、利用するモデルの対応先を確認する。
`gemini-4-argon` の単価は、Pi 1.3.0 の catalog で 1M トークンあたり input $2・output $10 と、
`gemini-3.8-flash`（input $0.75・output $3.75）より高い。Pi のシステムプロンプトだけで
入力が約 1 万トークンあるため、#680 の起票時の実測では 1 回の呼び出しで約 $0.02 かかった。

起動時の環境変数は次の順で決める（空文字も未設定として扱う）。

- project: `GOOGLE_CLOUD_PROJECT` → `GCLOUD_PROJECT` → `hobby-372711`
- location: `GOOGLE_CLOUD_LOCATION` → `global`
- ADC パス: 非空の `GOOGLE_APPLICATION_CREDENTIALS` →
  `auth.json` の `google-vertex.env.GOOGLE_APPLICATION_CREDENTIALS`。
  どちらも無ければ Pi に通常の ADC 解決を任せる。他の環境変数はそのまま引き継ぐ
- `GOOGLE_CLOUD_API_KEY` だけをランチャーのプロセスツリーから外す。
  呼び出し元のシェル、素の `pi`、他のランチャーには影響しない

ADC は利用者が事前に用意する。ランチャーは credential ファイルの存在・有効性を
確認せず、ログイン、gcloud の実行、トークンの取得・保存を行わない。
ADC が無い状態でも Pi まで起動し、認証の解決とエラー表示は Pi に任せる。
Pi の `/login google-vertex` で保存したサービスアカウントの ADC パスも利用する。
保存済みパスは同じ snapshot から取り出して、このプロセスツリーの環境へ渡す。
パスをコマンドとして実行したり、資格情報ファイルを調べたりはしない。
必要なら使うシェルで project / location や別の ADC パスを指定する。
明示した非空の ADC パスが優先され、保存済み project / location は引き継がない。

```bash
export GOOGLE_CLOUD_PROJECT=your-project
export GOOGLE_CLOUD_LOCATION=global
# サービスアカウントの ADC ファイルを使う場合だけ指定する:
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
pigemf
pigemp --session 'session with spaces.jsonl'
pigema
```

環境変数の API key を外すだけでは、Pi が優先する `auth.json` の保存済み key や
`models.json` の `apiKey` が使われ得る。そのため、この 3 ランチャーだけは、
起動時に `auth.json` を設定として解析し、全 provider の `type: "oauth"` の entry を
除外する。他 provider の非 OAuth entry は保持し、`google-vertex` を
`{type: "api_key", key: "gcp-vertex-credentials"}` に置き換えた読み取り専用の overlay を、
既存の bwrap namespace 内へ重ねる。保存済み Vertex env から残すのは ADC パスだけで、
marker entry の `env` に保持する。この文字列は key / token ではなく、Pi native adapter が
API key として送信せず ADC を使うための marker である。marker の key 分岐では saved env が
SDK に渡らないため、ADC パスは前述の実効環境へも渡す。
`!command` などの保存済み Vertex key は実行しない。
既存の `auth.json` を上書きせず、内容や overlay をログ・argv・namespace 外のディスクへ
出さない。`auth.json` が未作成の場合だけ、mount 先として空の JSON オブジェクト `{}` を
mode `0600` で作る。同時起動時も既存ファイルは置き換えず、資格情報は書き込まない。
解析に失敗した場合は秘密を含まない案内で起動を止める。

子 Pi も同じ namespace と overlay を継承する。auth の重ね先は
`PI_CODING_AGENT_DIR`（`~/` と相対パスも対応）に従い、子の cwd が変わっても
同じ場所を読むようプロセス内で絶対パスにする。tier config の重ね先は、
拡張の読み先である HOME 基準の共有パスのままである。

**読み取り専用 overlay の注意:** Pi は起動時やモデル切替時にも OAuth を更新する。
読み取り専用の snapshot では更新後の token を保存できず、refresh token が差し替わると
元の認証が使えなくなる可能性があるため、期限内の OAuth entry も含めて除外する。
この 3 ランチャー内では保存済み OAuth credential を使えない。それを使う provider は
素の `pi` または既存の対応ランチャーで利用する。`auth.json` は全 provider で読み取り専用
なので、`/login`・`/logout` も素の `pi` で行う。
重ねるのは `auth.json` だけで、settings・sessions とそれらの lock は通常の保存先を使う。
初回の設定・会話も永続化し、素の `pi` や別ランチャーとの
設定更新は同じ lock を使う。これは認証先の既定を選ぶ仕組みであり、sandbox ではない。
追加引数はそのまま渡す
ため、明示的な `--api-key`・provider 変更や拡張による認証の変更までは禁止しない。

起動前には npm でインストールした Pi の実体にある bundled catalog を、
**メタデータだけ**読んで必要な exact ID を確認する。`pi --list-models` や
availability API は認証を確認するため、このガードでは使わない。古い Pi、catalog の
配置変更、必要な ID の欠落では起動を止め、fuzzy match や別モデルのメタデータを
複製するフォールバックを防ぐ。catalog の cache や独自定義で欠落を補わず、Pi 本体を
更新する。dry-run は argv だけを表示し、auth の読み取りや作成を行わない。
確かめる ID は、親 Pi のモデルと、そのランチャーの tier が使う Vertex のモデルである
（例: `pigema` は `gemini-4-argon` と `cheap` の `gemini-3.8-flash`）。

Vertex ランチャーの一覧と確かめる ID は、`pi-superagents-exec.zsh` の
`pi_google_vertex_models` だけで定義する。auth の overlay・catalog の確認・ADC の受け渡しは、
この一覧に載ったランチャーにだけ適用される。Vertex ランチャーを足すときは、ランチャー本体と
tier の設定に加えてこの一覧に登録し、確かめる ID を tier に合わせる。

コンテナ作成／リビルド時は実行ファイルを毎回配置する。リビルドしない既存環境では、
更新済みの親リポジトリのルートで次を実行する。認証設定やモデル定義は変更しない。

```bash
mkdir -p ~/.local/bin
install -m 0755 .devcontainer/pigemf ~/.local/bin/pigemf
install -m 0755 .devcontainer/pigemp ~/.local/bin/pigemp
install -m 0755 .devcontainer/pigema ~/.local/bin/pigema
zsh scripts/setup_pi_superagents.zsh --if-absent
```

`--if-absent` は共有 config と既存ランチャーの tier を保持し、新規 tier を配置する。
新規ランチャーの tier を後からリポジトリの定義に揃えたい場合は、下の更新手順を使う。

### setup スクリプトを使う場面

`scripts/setup_pi.zsh`（deepinfra）、`scripts/setup_pi_opencode_go.zsh`
（opencode-go）、`scripts/setup_pi_commandcode_goat.zsh`（commandcode）は、
リビルドのたびに実行するものではない。次の場面で使う。

- API キーをローテーションしたとき（`setup_pi_opencode_go.zsh` が
  `auth.json` を書き換える。commandcode は環境変数参照なので
  キーのローテーションで setup スクリプトの実行は不要）
- ホスト側の `~/.pi` を作り直したとき
- `pi-models.json` を編集し、既存の `~/.pi/agent/models.json` へ反映したいとき
  （`install_beforehand.bash` は既存ファイルを上書きしないため）

いずれも冪等で、`providers` のうち対象プロバイダのエントリだけを置き換える。
`setup_pi_opencode_go.zsh` は `.devcontainer/pigo` と `.devcontainer/pigod` を、
`setup_pi_commandcode_goat.zsh` は
`.devcontainer/piccg` と `.devcontainer/piccd` をそのまま配置するため、
実行してもランチャの内容は `install_beforehand.bash` の配置結果と一致する。

commandcode の 2 つのランチャを `setup_pi_commandcode_goat.zsh` が同居して
受け持つのは、両者が同じ `providers.commandcode` エントリを共有するためで
ある。スクリプトを分けると 2 本が同じエントリを全置換し合い、片方だけを
実行した時点で他方のモデル定義が `models.json` から消える。

### API トークン

`OPENCODE_API_KEY`、`DEEPINFRA_TOKEN`、`COMMANDCODE_API_KEY` は
永続化せず、必要なときに手動で export する。

- `OPENCODE_API_KEY` は `pigo`・`pigod` の実行には不要である。bind mount した
  `auth.json` に資格情報が残るため、pi はそこから読む
  （`pi auth check --provider opencode-go` が `ready` を返す）。
  `setup_pi_opencode_go.zsh` でキーを更新するときだけ必要になる
- `DEEPINFRA_TOKEN` は `models.json` の `apiKey` が `$DEEPINFRA_TOKEN` を
  参照するため、deepinfra を使うシェルで export しておく必要がある。
  `ccdd` / `ccddg`（Claude Code を DeepInfra の Anthropic エンドポイントへ
  向けるラッパー）もこの値を読む
- `COMMANDCODE_API_KEY` は `DEEPINFRA_TOKEN` と同じ方式である。`models.json`
  の `apiKey` が `$COMMANDCODE_API_KEY` を参照するため、`piccg` と `piccd` を
  使うシェルで export しておく必要がある。未設定のまま実行すると
  ランチャがエラーメッセージを出して終了する。キーは
  `https://commandcode.ai/settings/keys` で作成する

### Plannotator のブラウザーレビュー

[Plannotator](https://plannotator.ai/) は、Pi / Superpowers の Markdown の Plan を
ブラウザーで確認・注釈し、承認／修正要求を Pi へ返すために使う。
[agent-workspace の PR #641](https://github.com/usaturn/agent-workspace/pull/641) と同じく、
`install_beforehand.bash` の既存の Pi 拡張導入処理で `npm:@plannotator/pi-extension` を登録する。
既存の Node.js 24 で動くため、Plannotator CLI や Bun の追加導入は不要である。

`devcontainer.json` は `PLANNOTATOR_REMOTE=1` と `PLANNOTATOR_PORT=19432-19441` を設定し、
19432〜19441 の 10 ポートを個別に転送する。拡張は範囲内の空きポートを選ぶ。
既存の XRift プレビュー（5173）、ログインのコールバック（3000）、
`~/.pi` の bind mount とランチャー別 model tier の設定は変更しない。

#### 既存コンテナへの反映

反映作業は利用者が行う。推奨手順は **Dev Containers: Rebuild Container** を実行し、
Pi（使っているランチャー）を起動し直すことである。
設定ファイルの保存や Pi の再起動だけでは、既存コンテナ全体に `containerEnv` は反映されない。

リビルドしない場合は、起動済みの Pi を終了し、次を実行する。
環境変数はこの起動だけに渡す。普段のランチャーを使う場合は、末尾の `pi` を
その実行ファイル名（例: `pigs`）に置き換える。既存の認証・モデル設定は置き換えない。

```bash
pi install --approve npm:@plannotator/pi-extension
env PLANNOTATOR_REMOTE=1 PLANNOTATOR_PORT=19432-19441 pi
```

#### ホストのブラウザーでレビューする

1. Spec のレビューは `/sp-brainstorm`、Plan のレビューは `/sp-plan` で依頼する。
   pi-superagents は、コマンドごとの設定 `superagents.commands.<コマンド>.usePlannotator` が
   `true` のときに Plannotator を使う。この 2 つのコマンドでは拡張の既定が `true` である。
   このリポジトリが配置するランチャー別 config は model tier だけを指定し、この設定を上書きしないため、
   有効にするための設定の追加は不要である。config で `false` を指定している場合は、その指定が優先される。
   レビューの受付は承認ではない。
2. Pi の端末に表示されたレビュー URL から、実際に使われたポートを確認する。
   リビルドしない場合は、VS Code の **Ports** 画面でそのポートを追加転送する。
3. Windows ホストのブラウザーで、Ports 画面の **Local Address** を開く。
   レビュー URL にパスやクエリがある場合は、その部分も付ける。
   ホスト側のポートが変わった場合は、コンテナ側の番号ではなく転送先の番号を使う。
4. ブラウザーで Plan を確認し、承認または注釈付きの修正要求を返す。
   Pi 側へ結果が戻ったことまで確認する。

`Plannotator review-result did not arrive before timeout` は、レビューが開始された後に
承認／修正要求が戻らなかったことを示す。pi-superagents 1.14.4 の待機時間は 10 分である。
URL の転送先を確認してその時間内に判断を返す。タイムアウト後は古いタブで承認しても
元の待機には戻らないため、Pi 側でレビューを依頼し直す。
この設定はブラウザーからの到達経路を用意するもので、待機時間を延長するものではない。

`Plannotator did not respond to plan-review request before timeout` は、結果待ちではなく
レビュー開始の受付に応答がなかったことを示す。`pi list` に拡張が登録されているか、
導入後に Pi を起動し直したかを確認する。レビュー機能が無効なら、共有 config と実際に使う
`~/.pi/agent/extensions/subagent.d/<launcher>/config.json` の `usePlannotator` の上書きを確認する。
設定を変更する場合も、既存の model tier や他のキーは残す。
19432〜19441 がすべて使用中なら新しいレビューを開始できない。
自分の不要なレビューを終了して空きを作る。無制限の同時レビューには対応しない。

remote mode はコンテナ内で `0.0.0.0` に bind する。この構成は VS Code のローカル転送を使い、
Docker の公開ポートや外部共有・トンネルは追加しない。レビュー URL を外部へ共有しない。
ブラウザーの自動起動は保証しないため、Windows の実ブラウザーと VS Code 転送を通した
承認／修正要求の往復は利用者が手動で確認する。

### pi の TUI モードと Herdr のスクロール履歴

Pi 1.0.0 から、対話 TUI の既定モードは `fullscreen` になった。このモードは
代替画面を使って会話履歴を Pi 内で管理するため、Herdr の通常のスクロール履歴に
過去の出力が残らず、コピーモードでさかのぼれない。

このリポジトリが管理する `pigo`・`pigod`・`piccg`・`piccd`・`piga`・
`pigs`・`pigl`・`pixg`・`pigemf`・`pigemp`・`pigema` は、共通ヘルパ `pi-superagents-exec.zsh` から
`pi --tui-mode regular` を起動する。regular は通常画面と端末のスクロール履歴を
使うため、画面高を超えた出力も Herdr のコピーモードで読める。
ユーザー／プロジェクトの `settings.json` は書き換えず、起動引数で指定する。
provider・model・thinking・認証確認・ランチャー別設定の分離は従来どおりで、
`--resume`・`--session`・メッセージなどの追加引数もそのまま転送する。

Dev Container の初期化／リビルドでは `install_beforehand.bash` が全ラッパーを
配置し、`setup_pi_superagents.zsh --if-absent` が共通ヘルパを更新する。
リビルドしない既存環境では、更新済みの親リポジトリのルートで次を実行し、
ラッパーを起動し直す。

```bash
zsh scripts/setup_pi_superagents.zsh --if-absent
```

`--if-absent` は既存のランチャー別 tier 設定を保持するが、共通ヘルパは毎回
更新する。各 provider の setup スクリプトが配置するラッパーも同じヘルパを使う。
この指定はラッパーの既定値なので、追加引数に `--tui-mode fullscreen` を指定した
場合はその指定が優先される。素の `pi` の既定モードには影響しない。

### pi-superagents のランチャー別 model tier

`@teelicht/pi-superagents` は Superpowers の Plan を Pi 自身の子プロセスで実行する拡張で、子 Pi のモデルを `cheap` / `balanced` / `max` の 3 tier で選ぶ。tier の定義は拡張が `~/.pi/agent/extensions/subagent/config.json` から読むが、このパスは拡張のソースに直接書かれており、環境変数による切り替えができない。

そのためランチャーは、自分専用の設定ディレクトリを `bwrap` の mount namespace でこの共有パスへ重ねて Pi を起動する。

```
~/.pi/agent/extensions/subagent.d/pigo/config.json  →  ~/.pi/agent/extensions/subagent/config.json
```

`HOME` は変えないので git / ssh の設定の見え方には影響せず、共有ファイルを書き換えないので複数のランチャーを同時に起動しても設定が混線しない。子 Pi は namespace を継承するため、親子で同じ tier を見る。

ただし `bwrap` は `no_new_privs` を立てるため、ランチャー配下では `sudo` / `su` が使えない。sudo が要る作業は、ランチャーを通さない素の `pi` か別のターミナルで行う。

| launcher | cheap | balanced | max |
|---|---|---|---|
| `pigo` | `opencode-go/muse-spark-1.3-contributor` | `opencode-go/muse-spark-1.3-contributor` | `opencode-go/deepseek-v4.1-flash` |
| `pigod` | `opencode-go/muse-spark-1.3-contributor` | `opencode-go/deepseek-v4.1-flash` | `opencode-go/deepseek-v4.1-flash` |
| `piccg` | `commandcode/meta/muse-spark-1.3-contributor` | `commandcode/meta/muse-spark-1.3-contributor` | `commandcode/deepseek/deepseek-v4.1-flash` |
| `piccd` | `commandcode/meta/muse-spark-1.3-contributor` | `commandcode/deepseek/deepseek-v4.1-flash` | `commandcode/deepseek/deepseek-v4.1-flash` |
| `piga` | `openai-codex/gpt-6-luna` | `openai-codex/gpt-6.1-sol` | `openai-codex/gpt-6-astra` |
| `pigs` | `opencode-go/deepseek-v4.1-flash` | `openai-codex/gpt-6.1-sol` | `openai-codex/gpt-6.1-sol` |
| `pigl` | `openai-codex/gpt-6-luna` | `openai-codex/gpt-6.1-sol` | `openai-codex/gpt-6.1-sol` |
| `pixg` | `opencode-go/deepseek-v4.1-flash` | `opencode-go/deepseek-v4.1-flash` | `xai/grok-4.7` |
| `pigemf` | `google-vertex/gemini-3.8-flash` | `google-vertex/gemini-3.8-flash` | `google-vertex/gemini-3.8-flash` |
| `pigemp` | `google-vertex/gemini-3.8-flash` | `google-vertex/gemini-3.1-pro-preview` | `google-vertex/gemini-3.1-pro-preview` |
| `pigema` | `google-vertex/gemini-3.8-flash` | `google-vertex/gemini-4-argon` | `google-vertex/gemini-4-argon` |

`piga` の子 Pi は `cheap` / `balanced` が `xhigh`、`max` が `max`。
`pigs` の子 Pi は `cheap` が `max`、`balanced` が `xhigh`、`max` が `max`。
`pigl` の子 Pi は全 tier が `xhigh`。`pixg` の子 Pi は DeepSeek の
`cheap` / `balanced` が `max`、Grok の `max` が `xhigh`。
`pigemf`・`pigemp`・`pigema` は全 tier が `high`。`pigema` は Gemini 4 Argon の単価が高いため、
軽い子タスクの `cheap` だけを Gemini 3.8 Flash にしている。
tier を変更するときは、リポジトリの `.devcontainer/pi-superagents/<launcher>.json` を編集してから次を実行する。`install_beforehand.bash` は既存の設定を保持するため、rebuild だけでは反映されない。

```bash
zsh scripts/setup_pi_superagents.zsh
```

`bwrap` が無い、またはランチャー用の設定が無い場合、ランチャーは起動せずに案内を出す。共有設定へ暗黙にフォールバックすると tier が警告なく混線するため。

#### Superpowers スキルの探索パス

pi-superagents v1.14.3 以降は、git で導入したパッケージの `package.json` にある
`pi.skills` を探索するため、Superpowers の探索パスを手動登録する必要はない。
[upstream の修正 #72](https://github.com/teelicht/pi-superagents/pull/72) は
[v1.14.3](https://github.com/teelicht/pi-superagents/releases/tag/v1.14.3) に含まれる。

##### 既存環境の更新

古い環境では、回避設定を整理する前に pi-superagents を v1.14.3 以降へ更新する。
`pi --version` は Pi 本体の版なので、拡張の版は別に確認する。
`pi list` で `npm:@teelicht/pi-superagents` の登録先・配置先を確認する。

```bash
pi --version
pi list
PI_SUPERAGENTS_ROOT="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/npm/node_modules/@teelicht/pi-superagents"
node -e 'const p = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8")); console.log(p.name, p.version)' "$PI_SUPERAGENTS_ROOT/package.json"
```

このパスは標準のユーザー配置を想定している。project/local 配置を利用している場合は、
`pi list` が示した対象の配置先を `PI_SUPERAGENTS_ROOT` に指定する。
期待する出力は `@teelicht/pi-superagents 1.14.3` またはそれ以降の版。
未更新のユーザー配置は、[Pi の package 管理](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md)に従い、対象を指定して更新する。

```bash
pi update npm:@teelicht/pi-superagents
```

版指定で古い npm パッケージを固定している場合は、通常の更新では pin が進まない。
その場合は `pi install npm:@teelicht/pi-superagents@1.14.3` で確認済み基準版へ
指定を変更できる。既に v1.14.3 以降なら下げる必要はない。更新後に同じ版確認を行う。

次に、最新のセットアップへ更新してから、下の任意整理に進む。
新しい `scripts/setup_pi_superagents.zsh` は `~/.pi/agent/settings.json` を
作成・変更しない。旧セットアップを残したままエントリだけ削除すると、再実行で追加される。

##### 旧エントリの任意整理

既存の `git/github.com/obra/superpowers/skills` は残してもよい。
v1.14.3 の隔離検証では、自動探索と同じ絶対パスに解決され、探索リストで重複排除された。
整理したい場合だけ `~/.pi/agent/settings.json` の `skills` 配列からこの要素を削除する。
他の skills、`packages`、`theme` などは保持し、設定全体を置き換えない。
自動削除の移行処理は行わない。

##### 検証範囲

移植元 maatlog-poc の[隔離検証 #409](https://github.com/usaturn/maatlog-poc/issues/409#issuecomment-5773449902)では、
pi-superagents 1.14.3 / Pi loader 0.85.1 / Superpowers 6.4.1 について、
旧エントリあり・なしと `includeProject` true・false の4条件で、
`using-superpowers`、`brainstorming`、`writing-plans`、
`subagent-driven-development`、`executing-plans` の解決と探索パスの重複排除を確認した。
これは探索 API の検証であり、実 trust 設定の変更や対話 Pi の `/sp-*` 実行確認ではない。

ランチャーの tier 設定の重ね方・資格情報の扱い・利用者の設定の保持などの回帰テスト（`.devcontainer/test-*.bash`）は、
`./scripts/ci/verify-devenv.sh` が `tools/devenv_migration/tests/test_devcontainer_bash_suites.py` から実行する。
単独で実行する場合は、リポジトリのルートで次を実行する。

```bash
bash .devcontainer/test-pi-superagents.bash
```

## DSH 撤去後の移行（#123）

DeepSeek Harness（DSH）の導入・ランチャー・runner runtime は撤去した。
Pi の DeepSeek 担当（`pi-deepseek`・`pigod`）とランチャー（`piccd`・`pigod`）、
Claude Code の `ccdd`・`ccddg` は引き続き使える。既定の inspect は
`devin-swe`・`pi-deepseek`・`claude-opus`・`pi-gpt` の 4 人である。

### 個人設定の更新

セットアップは既存の個人設定を上書きしない。コンテナを作り直さず使う場合は、
`~/.config/agent-runner/config.toml`（`XDG_CONFIG_HOME` を絶対パスで設定している場合は
その下の `agent-runner/config.toml`）を利用者が編集する。

1. `[agents.dsh-ccd]`・`[agents.dsh-go]` と、個人で定義した `runtime = "dsh"` の担当の節を削除する。
2. workflow の `agents`・`fallback` などにある DSH 担当の割り当てを削除する。
   同じモデルを使う場合は `dsh-ccd` を `pi-deepseek` に置き換えられる。
3. `[workflows.review]` の `after_run` から `review_run.py finalize-parent` を呼ぶコマンドだけを外し、
   `attach-runner` は残す。配列は雛形とマージせず丸ごと置き換わるため、古い配列を残すと
   撤去済みのコマンドを呼び続ける。雛形と同じ後処理を使うなら、個人設定の `after_run` 自体を外す。

担当のテーブルは雛形と再帰的にマージするため、割り当てだけを外しても DSH の担当の定義は残る。
DSH の runtime が 1 つでも定義されていると `check`・`list`・`run` はすべて
`{"error":"invalid_value"}`（終了コード 2）で止まる。PR 修正ループなども同じ設定を読む。
エラーには担当名が出ないので、上の手順で個人設定全体を確認する。
この個人設定の整理は、撤去前の版を使っている間にも行える。

移行後は、親のルートで次を実行する。

```bash
uv run --project tools/devenv python -m tools.agent_runner check
uv run --project tools/devenv python -m tools.agent_runner list
```

### 既存コンテナの更新

利用者による **Dev Containers: Rebuild Container** を推奨する。
個人設定に残したい変更は事前に控え、再作成後の設定へ必要な節だけ戻す。
新しいコンテナでは DSH と pnpm を導入せず、`~/.dsh` の bind mount とポート 3080 の転送も行わない。
`install_beforehand.bash` を再実行するだけでは既存の DSH は削除されない。

作り直さない場合は、このコンテナ内で動いている DSH を終了してから、利用者が次を実行する。
コンテナ内に配置した DSH 本体と専用ランチャーだけが対象である。

```bash
npm uninstall -g @deepseek-ai/dsh
rm -f -- "${HOME:?}/.local/bin/dshccd" "${HOME:?}/.local/bin/dshgo" \
  "${HOME:?}/.local/bin/dsh-launcher.bash" \
  "${HOME:?}/.local/bin/dshccd.patch.yml" "${HOME:?}/.local/bin/dshgo.patch.yml" \
  "${HOME:?}/.local/bin/dsh-model-env.patch.yml" \
  "${HOME:?}/.local/share/xrift-poc/dsh-model-env.mjs"
command -v dsh dshccd dshgo
```

最後のコマンドは何も表示せず終了コード 1 になるのが期待値である。表示された場合はその配置先を確認する。
この手動撤去では現在のコンテナの mount とポート転送は変わらない。取り除くにはコンテナの再作成が必要である。

ホストの `~/.dsh` は maatlog-poc などと共有している。中身、`cordis.patch.yml` の管理ブロック、
`~/.dsh-backup-*`・`~/.dsh-before-restore-*` のバックアップは削除・編集しない。

### 過去のレビュー記録

既存の `reviews/` は今回削除しない。`runtime: "dsh"` や `runner_run_id` を含む記録は引き続き読み取れる。
残っている `.staging/`・`parent-pr-data` も自動では削除しない。
片付ける場合は、利用者が対象を確認し、その資源を使った PR 修正ループの run が終了して
`finish` または `recover` でロックを解放済みであることを確かめる。
削除前には起点がシンボリックリンクでないことと、対象の所有者・保存先を確認し、
過去の記録や他のセッションの資源をまとめて削除しない。
