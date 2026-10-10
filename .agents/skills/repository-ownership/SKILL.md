---
name: repository-ownership
description: xrift-poc で作業を始めるときに使う。Issue・worktree・commit・PR を作る前や検証を実行する前に、変更が開発環境（usaturn/xrift-poc）と、items/・worlds/ の制作物（それぞれ独立した Git リポジトリ）のどちらに属するか、それとも操作を止めるべきかを決める。
---

# 操作先の分担

xrift-poc（以下「親」）は開発環境だけを管理する。制作物は `items/<name>/`・`worlds/<name>/` の、それぞれの Git リポジトリで管理する。振り分けを誤ると、制作物を親に commit したり（`items/`・`worlds/` は無視しているので `git add -f` が要り、公開リポジトリの親に制作物が載る）、制作物の Issue・PR を親に作ったりする。

| `kind` | 意味 | 操作先 |
|---|---|---|
| `env` | 親の checkout か、親の worktree | `usaturn/xrift-poc`。worktree は親の `origin/main` から作る |
| `product` | 制作物の Git リポジトリか、その worktree | `repository`（制作物の origin）。null なら GitHub の操作（Issue・PR）をしない |
| `stop` | Git 未初期化の制作物、`items/`・`worlds/` そのもの、判定できない場所 | 操作しない。`reason` と必要な設定を利用者に説明する。代わりに親へ commit・起票しない |

## 作業の前に判定する

`XRIFT_POC_ROOT` は Dev Container が親の main checkout のパスに設定する。設定されていない場所では、親の checkout のパスを設定してから実行する。

```bash
# repository-ownership route check
: "${XRIFT_POC_ROOT:?XRIFT_POC_ROOT を xrift-poc の main checkout のパスに設定する}"
uv run --no-project python "$XRIFT_POC_ROOT/.agents/scripts/route_target.py" "${ROUTE_PATH:-$PWD}"
```

出力は JSON で、`kind`・`code`・`repository`・`git_root`・`project_root`・`reason` を持つ。終了コードは `env`・`product` で 0、`stop` で 3、判定の前提が壊れているとき（親のルートが main checkout でない、origin が `usaturn/xrift-poc` でない、パスが無い）は 1。1 のときは停止し、代わりのルートを推測しない。

`git rev-parse --show-toplevel` の成功だけで判定しない。Git 未初期化の `worlds/<name>/` では、祖先の親が見つかる。

## 開発環境（env）

- **worktree:** 親の `origin/main` から作る。置き場所は `.claude/worktrees/<name>` か `.worktrees/<name>`（どちらも親で無視している）、または `/tmp` の一意のディレクトリ

  ```bash
  git -C "$XRIFT_POC_ROOT" fetch origin main
  git -C "$XRIFT_POC_ROOT" worktree add "$XRIFT_POC_ROOT/.claude/worktrees/<name>" -b <branch> origin/main
  ```

- **Issue と PR:** `gh --repo usaturn/xrift-poc ...`。`--repo` を毎回付ける
- **検証:** worktree の root で `./scripts/ci/verify-devenv.sh`。PR は CI の green を確かめる
- **stage:** パスを明示して stage する。`items/`・`worlds/` を `git add -f` しない

## 制作物（product）

- **Git の操作:** `git_root` で行う。制作物の既定ブランチから branch を作る
- **worktree:** 制作物のディレクトリの中（例: `worlds/<name>/.worktrees/<branch>`）に作り、`.worktrees/` を制作物の `.git/info/exclude` に加える。親の外に作ると、共通の指示とスキルを読めない。worktree でも `xrift-link` でスキルへのリンクを置く
- **Issue と PR:** `gh --repo <repository> ...`。`repository` が null なら GitHub の操作をせず、`git remote add origin <URL>` が必要なことを説明する
- **検証:** 制作物の `package.json` のスクリプトと `xrift check world --build`・`xrift check item --build`。`xrift upload` は利用者が明示的に指示したときだけ実行する
- 制作物の中の `public/` は XRift のアセットディレクトリであり、特別に扱わない

## 止める（stop）

`code` ごとに利用者へ説明する。

| `code` | 説明すること |
|---|---|
| `product_uninitialized` | 制作物で `git init` し、必要なら `git remote add origin <URL>` する。それまで commit・起票しない |
| `product_base` | `items/<name>` か `worlds/<name>` のディレクトリで作業する |
| `product_origin_is_env` | 制作物の origin が親を指している。制作物自身のリポジトリを origin に設定する |
| `not_git`・`unknown`・`env_origin_mismatch` | 作業する場所を確かめる。親か制作物の checkout に移る |

## 振り分けを誤ったときの症状

| 症状 | 原因 |
|---|---|
| 制作物の Issue・PR が `usaturn/xrift-poc` に作られた | `--repo` を付けずに `gh` を実行した、または Git 未初期化の制作物で祖先の親を操作先と判定した |
| 親の `git status` に `items/` や `worlds/` の中身が出る | `git add -f` した、または `.gitignore` の `/items/`・`/worlds/` が変わった |
| 制作物の worktree で共通のスキルが見えない | worktree を親の外に作った、または `xrift-link` を実行していない |
