# 移植の記録

## 移植元

- リポジトリ: `usaturn/maatlog-poc`
- コミット: `eac5195ce371f8ced154d6df5b2a22f35b74eac2`（2026-10-10、Issue #1 の調査と同じ）
- 実行時に maatlog-poc の checkout は要らない

## 移植したもの

| 場所 | 内容 |
|---|---|
| `.devcontainer/` | Dev Container の定義、初期化（`initialize.bash`・`install_beforehand.bash`）、シェルの設定（`zshrc.txt`・`starship.toml`）、Herdr、pi と DSH のランチャー、`agent-runner` の設定、クリーンアップ、bash のテスト |
| `scripts/` | 検証の入口（`ci/verify-devenv.sh`）、pi のセットアップ、Claude Code のオンボーディングの補助 |
| `tools/` | `devenv`（検証用の project）、`devenv_migration`（Dev Container のテスト。名前は移植元とそろえた）、`agent_runner`、`review_triage`、`test_support` |
| `.agents/` | 共通スキル 14 個、補助スクリプト、レビューの記録の形式 |
| `.claude/`・`.pi/`・`.cursor/rules/`・`.mcp.json`・`.pi-lens.json`・`.github/workflows/ci.yml` | 各エージェントの設定と CI |

## 移植しなかったもの

| 対象 | 理由 |
|---|---|
| `rst-writer`、`python-extension-docs`、`version-export` | Issue #1 で対象外と指定されている |
| `public/`、`.gitmodules`、`prepare_public_checkout`、`public/uv.lock` を使う Playwright の導入 | MaatLog のアプリ用 |
| `tools/devenv_migration/tests/test_application_checkout.py`、`test_cutover_contract.py`、`tools/devenv_migration/evidence/` | MaatLog の `public/` と cutover の記録 |
| ポート 8000 | MaatLog の sphinx-autobuild 用 |
| `opencode.json` | OpenCode はセットアップで導入していない。設定ファイルだけ置くと導入済みと誤解される |
| `.codex/.gitkeep` | 空のプレースホルダーで、設定の実体が無い |
| `scripts/add_pi_skills_setting.py` | 共有している `~/.pi/agent/settings.json` に、このリポジトリのスキルを追加してしまう。セットアップからも呼ばれていない |
| `.pi/APPEND_SYSTEM.md` の Response Persona の節 | 移植の方針として決めた（Spec 2 章） |
| 廃止済みの pi 拡張の撤去処理（`install_pi_extensions` の撤去部分、`remove_pi_mcp_adapter`、`remove_pi_distill`）と `test_pi_mcp_adapter_migration.py` | 移植の方針として決めた（Spec 2 章）。検出と警告に置き換えた |
| `.devcontainer/README.md` のうち、MaatLog の既存コンテナだけに関わる移行の節（「既存コンテナの移行」「npm への一本化と既存環境の移行」「Herdr サイドバーの設定移行」「herdr-pr-review からの移行」「pi-distill の撤去」「`public/` checkout の検査と復旧」） | 新しいリポジトリには移行元の状態が無い。共有ホームの `~/.dsh` に関わる「旧 settings.yaml からの移行」は残した |
| スキルの `evals/` のうち、移植元で実行した日付付きの記録と、MaatLog のリポジトリを入力にした評価シナリオ | MaatLog 固有で、xrift-poc では再現できない。テストや `SKILL.md` が参照しているものは、下の「移植元の記録として残したもの」に挙げた |

## 移植元の記録として残したもの

次のファイルは、テストやスキルの文書が参照しているため残した。いずれも maatlog-poc での実行の記録で、中の PR 番号・パスは移植元のものである。xrift-poc で再現した記録ではない。

- `.agents/skills/review-fix-planning/evals/2026-10-08-rework.md`: `references/porting.md`・`decision-packet.md`・`safety-packet.md` が参照している
- `.agents/skills/review-fix-planning/evals/validation.md`: `evals.json` が参照している
- `.agents/skills/review-verify/evals/validation.md`: `replay.md` が参照している
- `.agents/skills/review-verify/evals/replay.md`: `test_replay_procedure.py` が、中のコードブロックを実行している

`.agents/skills/planning-parallel-issues/evals/scenarios.json` は評価の入力として残した。参照元だった `validation.md`（2026-09-16 の移植元での実行記録）は移植しなかった。

## XRift 向けの変更

- `items/`・`worlds/` をセットアップで用意し、`.gitignore` の `/items/`・`/worlds/` で無視する
- XRift CLI（`@xrift/cli@0.25.0`）と XRift 公式スキル（`WebXR-JP/xrift-skills@41d6504`、`skills@1.7.2`）をセットアップで導入する
- `~/.xrift` の bind mount、ポート 5173・3000 の転送（5174〜5179 は `portsAttributes` のラベルだけで、開発サーバーが起動したときに VS Code が検出して転送する）
- 操作先の判定（`.agents/scripts/route_target.py`）と、制作物へのスキルのリンク（`.agents/scripts/project_link.py`・`xrift-link`）
- `cod` に `-c 'project_root_markers=[".devcontainer"]'` を加えた
- `~/.zshrc` の設定を管理ブロックにし、再実行で重複しないようにした
- Playwright は `tools/devenv` の依存から導入する
- 廃止済みの pi 拡張は撤去せず、残っていれば削除の手順を警告として表示する
- `AGENTS.md`・`repository-ownership`・各スキルの、`public/` と公開側の記述を、親と制作物の分担に書き換えた
- 次の取り込み時の変更: `tools/review_triage/tests/test_questions.py` の定数名 `KEY_A`・`KEY_B` だけを `FINDING_A`・`FINDING_B` に改名した（gitleaks の誤検出を避けるため）

### 設計の後で決めたこと（Spec からの変更）

- 制作物は親の main checkout の下で扱う。親の worktree の `items/`・`worlds/` は `stop`（そのものは `product_base`、配下は `product_in_env_worktree`）と判定する。main checkout の Git 未初期化の制作物（`product_uninitialized`。その場で編集する）とは code を分けた（最終レビューの指摘 I-1）
- `route_target` の判定の対象は、cwd ではなく、これから変更するパスにする。まだ無いパスは、実在する最も近い祖先のディレクトリで判定する。`items/`・`worlds/` の下のパスは、それらのディレクトリが無くても `env` にしない（最終レビューの指摘 I-2）
- origin が親を指すかどうかは、origin の URL の設定値の末尾（`usaturn/xrift-poc`）を大文字小文字を区別せずに照合する。`https://user@github.com/...`・SSH のホスト別名・`GitHub.com`・`http` の書き方も親とみなす（最終レビューの指摘 M-1）
- `repo-cleanup-after-merge` は、worktree を削除する前に、Git で無視したファイルも確かめる。`items/`・`worlds/` に中身がある worktree や、依存・キャッシュ以外の無視したファイルがある worktree は削除しない（`git worktree remove` は無視したファイルも消すため。最終レビューの指摘 I-1）
- `project_link` の管理ブロック（制作物の `.git/info/exclude`）は、制作物のすべての worktree にある、実在するリンクから作り直す
- `issue-to-pr` は、`gh` の応答と、大文字小文字を区別せずに照合する
- `review_triage` は親のリポジトリだけを対象にする
- `finalize` はどのリポジトリでも CI を確かめる。CI の無い制作物のリポジトリでは止まる

## 改名した識別子と、残した目印

コンテナ内で完結する識別子は `xrift-poc` に改名した。共有ホーム（bind mount している `~/.claude`・`~/.pi`・`~/.dsh` など）のファイルに書く目印は、改名すると同じ内容が 2 回書かれるため、そのまま残した。

| 識別子 | 扱い |
|---|---|
| `MAATLOG_CLEANUP_*`、`maatlog_periodic_cleanup`、`~/.cache/maatlog` | `XRIFT_POC_CLEANUP_*`、`xrift_poc_periodic_cleanup`、`~/.cache/xrift-poc` に改名 |
| `~/.local/share/maatlog/dsh-model-env.mjs`、プラグイン ID `maatlog-dsh-model-env` | `~/.local/share/xrift-poc/`、`xrift-poc-dsh-model-env` に改名（`~/.local/share` と `~/.local/bin` は mount していない） |
| `/tmp` の接頭辞 `maatlog-pr-loop-`・`maatlog-agent-runner-`・`maatlog-pr-review-scratch-` | `xrift-poc-` に改名。`cleanup-workspace.bash` と配置の検査も合わせた |
| `maatlog-devenv`（project 名）、`maatlog-review-triage/1`（User-Agent） | `xrift-poc-devenv`、`xrift-poc-review-triage/1` に改名 |
| `~/.dsh/cordis.patch.yml` の `# BEGIN maatlog managed provider config` | 残した。`~/.dsh` は maatlog-poc のコンテナと共有している。xrift-poc 側で目印を変えると、同じ provider のブロックが共有ファイルに追記される |
| コードやコメント中の `#番号` | 変えていない |

コードやコメント中の `#番号` は、特記が無い限り移植元の `usaturn/maatlog-poc` の Issue・PR を指す。

## 更新の方法

- エージェント、XRift CLI、XRift 公式スキルの更新は、README の「更新」の節に書いてある（手順を二重に書かない）。更新したら、この文書の「検証の記録」に確かめた版を追記する
- 移植元の修正の取り込み: 移植元の `git log <前回の SHA>..origin/main -- <パス>` で対象の変更を確かめ、同じパスに手で適用する。適用した移植元の SHA をこの文書に追記する

## 検証の記録（2026-10-10）

この Dev Container（Ubuntu 26.04、Node.js v24.21.0、npm 11.19.0、uv、Python 3.14）で確かめた。作業は `tmp/agents/` の一時ディレクトリで行い、制作物・fixture・ログはこのリポジトリに入れていない。エージェントの確認は、一時の `HOME`・`CODEX_HOME` を使い、モデルへは送信していない。

### XRift CLI とテンプレート

| 確かめたこと | コマンド | 結果 |
|---|---|---|
| CLI の導入 | `npm install --prefix <一時> --no-audit --no-fund @xrift/cli@0.25.0`（`-g` なし） | 52 パッケージを導入。`xrift --version` は `0.25.0` |
| ワールドの作成 | `xrift create world my-world -y` | テンプレートの取得・加工・依存の導入まで成功。依存の導入で Node.js の `DEP0190` の非推奨警告が出る（CLI が `shell` オプション付きで子プロセスを起動するため。動作に影響しない） |
| アイテムの作成 | `xrift create item my-item -y` | 同上。成功 |
| ビルド | `npm run build`（`tsc && vite build`） | ワールド・アイテムとも終了コード 0（`✓ built in 16.50s`・`15.95s`） |
| 型チェック | `npm run typecheck`（`tsc --noEmit`） | ワールド・アイテムとも終了コード 0 |
| 開発サーバー | `npm run dev -- --host 0.0.0.0`（ワールドは 5173、アイテムは `--port 5174`） | どちらも HTTP 200。vite 7.3.1 が `Network:` のアドレスも表示した。確認後に `kill -- -"$DEV_PID"` でプロセスグループごと止め、`ss -ltnp` で 5173・5174 を待ち受けるプロセスが残っていないことを確かめた |
| 描画 | Python Playwright（Chromium のヘッドレス）で `http://localhost:<port>/` のスクリーンショットを撮り、目で確かめた | ワールド: 空の背景（街並みの全天球画像）、茶色の壁、緑の床、左下に操作の説明（`WASD` で移動など）が描画された。アイテム: 灰色の床の上に、青い八面体と黒い台座（青い光の広がり付き）が描画された。ページのエラー（`pageerror`）は無し |
| セキュリティ検査 | `xrift check world --build`、`xrift check item --build` | どちらも `Security check passed`（ワールド 22 ファイル、アイテム 20 ファイルすべて `APPROVE`）。警告は出なかった。`--ignore-warnings`・`--skip-check` は使っていない |

ブラウザのコンソールには、次の警告が出た。いずれもテンプレートや three.js・ヘッドレスの描画に由来し、作成と検証の可否には影響しない。

- `THREE.Clock` の非推奨（three.js が `THREE.Timer` への移行を促している）
- `GL Driver Message ... GPU stall due to ReadPixels`（ヘッドレスのソフトウェア描画の性能の警告）
- `using deprecated parameters for the initialization function`（依存ライブラリの初期化の非推奨）
- `PCFSoftShadowMap has been deprecated`（アイテムのみ）

そのほかに分かったこと。

- Playwright のブラウザ（`chromium_headless_shell-1243`）が無かったため、`uv run --project tools/devenv --no-sync playwright install chromium` を実行した（`--with-deps` なし）。Dev Container の新規作成で `install_beforehand.bash` が行う導入は、ここでは確かめていない
- ワールドのテンプレートは、制作物の中に独自の `AGENTS.md`・`CLAUDE.md`・`skills-lock.json`・`.github/dependabot.yml`・`.gitignore`（`.agents` を無視する）を含む。アイテムのテンプレートには含まれない
- CLI は、実際の `HOME` に `~/.xrift/version-cache.json`（版の確認のキャッシュ）を書いた。認証情報（`config.json`）は書かれていない

### XRift スキルの取得

`scripts/install_xrift_skills.bash` を、`scripts/` と `.gitignore` を写した一時の Git リポジトリで実行した（`skills@1.7.2` が実際に動く）。

- 2 回続けて実行しても、どちらも終了コード 0。`xrift-sdk`・`xrift-world` が `41d65040e8c2a5313991acddf93406888fc84326` のまま（`skills-lock.json` の `ref`）
- `-a claude-code -a codex -a pi` で作られたものは、`.agents/skills/xrift-sdk`・`.agents/skills/xrift-world`（実体）、`.claude/skills/` の同名のシンボリックリンク、`skills-lock.json` だけ。`.pi/`・`.codex/` は作られなかった
- `git status --porcelain --untracked-files=all` に、取得物は出ない（コピーした `scripts/` と `.gitignore` だけが未追跡）

### エージェントの読み込み

親を写した Git リポジトリ（origin は `usaturn/xrift-poc`）の下に、Git 未初期化の制作物（`plain-world`）、`xrift-link`（`project_link.py link`）を実行した独立した Git リポジトリの制作物（`git-world`）、実行していない Git リポジトリの制作物（`unlinked-world`、対照）を置いて確かめた。親の fixture は `/workspaces/maatlog` の下にあるため、Claude Code では `/workspaces/maatlog/CLAUDE.md` も読み込まれる。この表は、fixture の親の `CLAUDE.md`・`AGENTS.md` とスキルが出るかだけを見ている。

| エージェント | `plain-world`（未初期化） | `git-world`（リンク済み） | `unlinked-world`（対照） | 確かめ方 |
|---|---|---|---|---|
| Claude Code 2.1.296 | 親の `CLAUDE.md` ✓、`repository-ownership`・`xrift-world` ✓ | 同左 ✓ | 親の `CLAUDE.md` ✓、スキル ✗ | `claude -p "/context" --output-format stream-json --verbose`（一時 HOME、`env -i`） |
| pi 1.1.0 | スキル ✓ | スキル ✓ | スキル ✗ | `pi --mode rpc --no-session --offline --no-extensions --approve` に `get_commands` を送る。指示は `resource-loader.js` の `loadProjectContextFiles` がファイルシステムのルートまで親ディレクトリをさかのぼる実装を確認（実行はしていない） |
| Codex CLI 0.162.1（`-c 'project_root_markers=[".devcontainer"]'` あり） | 親の `AGENTS.md` ✓、スキル ✓ | 同左 ✓ | 同左 ✓ | `codex debug prompt-input`（一時 `CODEX_HOME`） |
| Codex CLI（フラグなし、対照） | 親の `AGENTS.md` ✓、スキル ✓ | 親の `AGENTS.md` ✗、スキル ✓（リンク経由） | 親の `AGENTS.md` ✗、スキル ✗ | 同上 |
| Devin 3000.11.3 | スキル ✓ | スキル ✓ | スキル ✗ | `devin skills list`（モデルへ送信しない。指示の読み込みは確かめていない） |

- 制作物の中の worktree（`worlds/git-world/.worktrees/wt1`）にも `project_link.py link` を実行した。Claude Code・pi・Codex（フラグあり）・Devin のいずれも、親の指示（Devin は指示を除く）とスキルを読んだ。Codex はフラグなしだとスキルだけを読んだ（親の `AGENTS.md` は読まない）
- 親の外（`/tmp` 相当）に作った制作物の worktree への `link` は、`project_link: 制作物の配下ではない worktree には対応しない` で終了コード 2 になった。この worktree では Codex（フラグあり）も親の指示とスキルを読まなかった。`AGENTS.md` に、制作物の worktree は制作物のディレクトリの中に作るよう書いてある理由である
- リンクの管理ブロックは制作物の `.git/info/exclude` に書かれ、`git status --porcelain` に出なかった。Git 未初期化の制作物への `link` は、何も変えずに「リンクは不要」と表示して終わった
- ディレクトリ名に空白と日本語を含む制作物（`worlds/空白 ワールド`）も判定できた（次の節）

### 操作先の判定（`route_target.py`）

fixture の親を `XRIFT_POC_ROOT` にして実行した。最終レビューの修正（I-1・I-2）の後に、改訂した `route_target.py` で同じ fixture に対して再実行した結果である。

| 判定するパス | kind | code | 終了コード | repository |
|---|---|---|---|---|
| 親（main checkout）、その中のファイル | `env` | `env` | 0 | `usaturn/xrift-poc` |
| 親の worktree（`.worktrees/feat-x`）と中のファイル | `env` | `env` | 0 | `usaturn/xrift-poc` |
| 親の外に作った親の worktree | `env` | `env` | 0 | `usaturn/xrift-poc` |
| `worlds/git-world`（Git 管理、origin は `https://github.com/someone/git-world.git`） | `product` | `product` | 0 | `someone/git-world` |
| `worlds/空白 ワールド`（origin は `git@github.com:someone/space-world.git`） | `product` | `product` | 0 | `someone/space-world` |
| `worlds/git-world/.worktrees/wt1`（制作物の worktree） | `product` | `product` | 0 | `someone/git-world` |
| 親の外（`/tmp` 相当）に作った制作物の worktree | `product` | `product` | 0 | `someone/git-world` |
| `worlds/plain-world`（Git 未初期化）と、その中のディレクトリ | `stop` | `product_uninitialized` | 3 | null |
| `worlds`（`items/`・`worlds/` そのもの） | `stop` | `product_base` | 3 | null |
| 親の worktree の `worlds` | `stop` | `product_base` | 3 | null |
| 親の worktree の `worlds/in-wt-world`（Git 未初期化）、まだ無い `worlds/new-world/src/a.tsx` | `stop` | `product_in_env_worktree` | 3 | null |
| まだ無いパス: 親の `tools/new.py` | `env` | `env` | 0 | `usaturn/xrift-poc` |
| まだ無いパス: `worlds/new-world/src/a.tsx` | `stop` | `product_base` | 3 | null |
| まだ無いパス: `worlds/plain-world/src/new.tsx` | `stop` | `product_uninitialized` | 3 | null |
| まだ無いパス: `worlds/git-world/src/new.tsx` | `product` | `product` | 0 | `someone/git-world` |
| `XRIFT_POC_ROOT` が親の worktree、または制作物 | （JSON なし） | （エラー表示） | 1 | （なし） |

- 親の worktree 内のスクリプトを `XRIFT_POC_ROOT` なしで実行しても、制作物 `git-world` を `product` と判定できた
- 判定は、cwd ではなく、渡したパスで決まる（cwd が親の中でも、`worlds/git-world` は `product`）

### 未実施（このコンテナからは確かめられない）

- ホストでの Dev Container の新規作成とセットアップの完走、再構築（`${containerWorkspaceFolder}` が `XRIFT_POC_ROOT` に展開されることを含む）
- ホストのブラウザでのプレビュー
- アカウントを使う `xrift login`・`xrift whoami`・アップロード（`xrift upload`）
- Cursor CLI（`agent`）・Grok Build・DSH の読み込み: モデルに送信せずに確かめる方法が見つからなかった。`--help` に該当の確認コマンドが無い。Cursor・Grok は `.claude/skills`・`.agents/skills` を読む見込み、DSH は未確認。手動で確かめるときは、制作物の中で各エージェントを起動し、スキルの一覧（`/skills` など）に `xrift-world` と `repository-ownership` が出るかを見る
- Devin の指示（`AGENTS.md`）の読み込み
- pi の指示の読み込みの実行（実装の確認だけ）
