# herdr セッション復元時の cwd 是正

## 背景

Dev Container を Reopen したあと、ターミナルの作業ディレクトリが `/workspaces/xrift-poc`
ではなく `/home/vscode` になる。`.devcontainer/zshrc.txt` の `herdrstart` は
`cd -- "$workspace_dir"` を実行しているにもかかわらず、その結果がペインに反映されない。

## 根本原因

herdr はセッション復元時、`~/.config/herdr/session.json` に保存済みの cwd を最優先し、
クライアントが渡す startup cwd を**明示的に無視する**。

サーバログ（`~/.config/herdr/herdr-server.log`）:

```
herdr::server::headless: restored session already has workspaces; ignoring startup cwd cwd=/workspaces/xrift-poc
```

`herdrstart` の `cd` が効くのは、herdr サーバが**新規**セッションを作る初回だけである
（このとき `created startup workspace cwd=...` が記録される）。一度ワークスペース外の
cwd で herdr サーバが起動すると、その値が `session.json` に焼き付き、以後の復元で
再現され続ける。

ペインのシェルは herdr **サーバ**の子として spawn されるため、2 回目以降にクライアントを
起動したシェルの cwd は一切反映されない。

### 汚染経路

VS Code Dev Containers の `userEnvProbe`（既定 `loginInteractiveShell`）が `docker exec`
でインタラクティブシェル `/bin/zsh -i` を起動する。このシェルは tty を持つため
`herdrstart` の tty ガード（`[ ! -t 0 ] || [ ! -t 1 ]`）を通過し、`.zshrc` 末尾の
`herdrstart` から herdr を起動してブロックしたまま残留する。

このプローブシェルはワークスペースではなくホームで開始されるため、herdr サーバの
startup workspace が `/home/vscode` となり `session.json` を汚染する。副作用として
孤児シェルが残り、`herdr-status-updater --daemon` が多重起動する。

## 検討して却下した案

### config.toml の `new_cwd` を固定パスにする

`[terminal] new_cwd` は `"follow"` `"home"` のほか固定パスを取れるが、**セッション復元時
には適用されない**。分離セッション（`herdr --session <name>`）による実測結果:

| 保存されていた cwd | `new_cwd` | 復元後のペイン cwd |
| --- | --- | --- |
| `/home/vscode` | `/workspaces/xrift-poc` | `/home/vscode` |
| `/workspaces/xrift-poc/.worktrees/xrift-mvp` | `/workspaces/xrift-poc` | `/workspaces/xrift-poc/.worktrees/xrift-mvp` |

`new_cwd` が効くのは復元ではない新規ペイン・タブ・ワークスペース作成時のみで、目的を
達成しない。

### `session.json` を毎回削除してクリーン起動する

永続セッション（ワークスペース・タブ構成、`agent_session` に記録された AI エージェントの
セッション ID）を常時失う。herdr を使う動機と衝突する。

## 設計

### 1. `.devcontainer/zshrc.txt` — 起動前に壊れた cwd だけを是正する

`herdr_repair_session_cwd()` を追加し、`herdrstart` 内の `herdr` 起動直前に呼ぶ。
引数として `herdr_select_workspace` が返したワークスペースディレクトリを渡す
（`herdr_repair_session_cwd "$workspace_dir"`）。是正時の書き換え先はこの値である。

**何もしないガード**（いずれか該当で即 return）

- `herdr status server --json` が失敗する、JSON として解釈できない、または
  `running` が boolean `false` ではない — 稼働状態を安全に確認できないため書き換えない
- `session.json` が無い
- `python3` が無い
- JSON のパースに失敗した
- `session.json` の `version` が 3 ではない — スキーマは herdr の内部形式であり、
  未知の version は構造が同じでも意味が変わり得るため書き換えない
- 修復先ワークスペースが実在しない、または進入できない — 壊れた cwd を別の
  壊れた cwd で置換することになるため書き換えない
- 修復先が `$HERDR_WORKSPACE_ROOT` 自身（末尾 `/` を除いて同一）— fzf キャンセルや
  ルート直下 0 件のフォールバックでルートが渡ると、焼き付いたあと
  `inside(root, root)` が真になり自己修復不能になるため書き換えない

**是正ロジック**

各 workspace の `identity_cwd` と各 pane の `cwd` を検査する。

- **保持**: `$HERDR_WORKSPACE_ROOT`（既定 `/workspaces`）配下に実在するディレクトリ。
  `/workspaces/xrift-poc/.worktrees/...` はここで保持される
- **是正**: それ以外（`/home/vscode`、削除済み worktree など）は引数で受け取った
  ワークスペースディレクトリへ書き換える

配下判定は `os.path.realpath()` で両者を解決したうえで `os.path.commonpath()` を用いる。
文字列の前方一致では `/workspaces-old` のような兄弟ディレクトリを誤って配下と判定するため。
`realpath` を挟むのは、ワークスペース直下がシンボリックリンクである構成
（本リポジトリの `.devcontainer` など）でも安定して判定するため。

複数の workspace が是正対象に該当する場合、修復先を既に `identity_cwd` に持つ
エントリが存在するときは 2 件目以降を書き換えない（pane を含むエントリ全体を
スキップする）。全エントリを同一の `identity_cwd` へ収束させ、herdr が同一
ワークスペースとして扱う事態を防ぐため。修復されるのは先頭から最初に該当した
1 エントリだけである。

pane の壊れた `cwd` は、当該エントリの `identity_cwd` が `$HERDR_WORKSPACE_ROOT`
配下に実在するときはその値へ是正し、identity 自体を修復したエントリでは引数の
ワークスペースディレクトリへ是正する。起動中ワークスペースへ兄弟エントリの pane を
流し込まない。

変更があったときだけ、一時ファイル + `os.replace` でアトミックに書き戻す。
`agent_session` を含む他のフィールドには一切触れない。

worktree 配下のパスを保持するのは、AI エージェントのセッションが cwd に紐づくため。
Claude Code は `~/.claude/projects/<cwd スラグ>/` で会話履歴を管理しており、cwd を
書き換えると worktree で開始したセッションを再開できなくなる。

### 2. `.devcontainer/devcontainer.json` — 汚染源を断つ

```json
"userEnvProbe": "loginShell",
```

`zsh -l`（非インタラクティブ）は `.zshrc` を読まないため `herdrstart` が走らない。
プローブシェルの残留と、それによる `session.json` の汚染がなくなる。

`.zshrc` にしか定義されていない PATH（bun、grok、opencode など）はプローブで採取できなく
なるが、現状はプローブが herdr でハングして何も採取できていないため悪化しない。

### 3. `.devcontainer/install_beforehand.bash` — 追記順序を直す

`cat .devcontainer/zshrc.txt >> ${HOME}/.zshrc` を、PATH を追記する各 CLI
インストーラの後、失敗時に `exit 1` する Herdr インストールの前へ移動する。
これにより `herdrstart` より先に CLI の PATH が評価され、Herdr の導入に失敗しても
alias・環境変数・PATH 補助は `.zshrc` に残る。

## 起動シーケンス（修正後）

```
コンテナ Reopen
 └─ VS Code ターミナル (zsh -l, interactive)
     └─ ~/.zshrc
         ├─ 各インストーラの PATH 設定        ← 変更 3 で前に移動
         └─ herdrstart
             ├─ ガード（HERDR_ENV / HERDR_SOCKET_PATH / tty）
             ├─ workspace_dir = herdr_select_workspace
             ├─ cd $workspace_dir
             ├─ herdr_repair_session_cwd      ← 変更 1 で追加
             │    └─ サーバ未起動時のみ、壊れた cwd を是正
             └─ herdr → 是正済み session.json を復元
```

## エラー処理

全経路をフェイルセーフとする。`session.json` を壊すくらいなら「直さない」を選ぶ。
JSON のスキーマは herdr の内部形式（現在 `"version": 3`）であり、将来変更されうる。
読めない構造に出会ったら黙って何もせず、herdr の起動自体は妨げない。
status が厳密に `running: false` と確認できた場合だけ修復する。

## 検証

分離セッション（`herdr --session <name>` と `HERDR_CONFIG_PATH`）を使えば、稼働中の
セッションを壊さずに検証できる。

1. `session.json` に `/home/vscode` を仕込み、復元ペインが `/workspaces/xrift-poc` に
   なること
2. `session.json` に worktree パスを仕込み、**保持される**こと（退行防止）
3. サーバ稼働中は `session.json` が書き換わらないこと
4. status コマンドの失敗、不正な status JSON、`running` キー欠落、文字列 `"false"`、
   数値 `0` のとき書き換わらないこと
5. Reopen 後の実地確認 — `pwd` がワークスペース、`ps` に `/bin/zsh -i` の残留が無い、
   `herdr-status-updater --daemon` が 1 つだけ

## 既存コンテナへの適用

`postCreateCommand` はコンテナ**作成時**にしか実行されない。Reopen では走らないため、
既存コンテナには自動適用されない。リビルドするか、`.devcontainer/README.md` に記載する
手動適用手順を実行する。

`install_beforehand.bash` は `~/.config/herdr/config.toml` が既に存在する場合は上書き
しない。ユーザ設定を保護するための意図的な挙動であり、変更しない。

## 自動テスト

Task 1 で追加した自動テストを実行する。

```
bash .devcontainer/test-herdr-migration.bash
```
