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
| `stop` | Git 未初期化の制作物、`items/`・`worlds/` そのもの、親の worktree の `items/`・`worlds/` の配下、判定できない場所 | 操作しない。`reason` と必要な設定を利用者に説明する。代わりに親へ commit・起票しない |

## 作業の前に判定する

`XRIFT_POC_ROOT` は Dev Container が親の main checkout のパスに設定する。設定されていない場所では、親の checkout のパスを設定してから実行する。

```bash
# repository-ownership route check
: "${XRIFT_POC_ROOT:?XRIFT_POC_ROOT を xrift-poc の main checkout のパスに設定する}"
uv run --no-project python "$XRIFT_POC_ROOT/.agents/scripts/route_target.py" "${ROUTE_PATH:-$PWD}"
```

`ROUTE_PATH` には、これから変更するファイルかディレクトリを指定する。cwd ではない。親のルートで起動していても、`worlds/foo/` を編集するなら `ROUTE_PATH=worlds/foo` で判定する（`$PWD` のままだと `env` と判定され、親に commit・起票してしまう）。これから作るファイルのように、まだ無いパスも指定できる（実在する最も近い祖先のディレクトリで判定する。`items/`・`worlds/` の下のパスは `env` にならない）。作業対象のパスが変わるたびに判定し直す。

出力は JSON で、`kind`・`code`・`repository`・`git_root`・`project_root`・`reason` を持つ。終了コードは `env`・`product` で 0、`stop` で 3、判定の前提が壊れているとき（親のルートが main checkout でない、origin が `usaturn/xrift-poc` を指していない）は 1。1 のときは停止し、代わりのルートを推測しない。

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

- **作業場所:** 制作物の作業は、main checkout（`$XRIFT_POC_ROOT`）の `items/`・`worlds/` 側で行う。env の feature worktree 自身の `items/`・`worlds/` は制作物の置き場所ではない（`stop`・`product_in_env_worktree`）
- **Git の操作:** `git_root` で行う。制作物の既定ブランチから branch を作る
- **worktree:** 制作物のディレクトリの中（例: `worlds/<name>/.worktrees/<branch>`）に作り、`.worktrees/` を制作物の `.git/info/exclude` に加える。親の外に作ると、共通の指示とスキルを読めない。worktree でも `xrift-link` でスキルへのリンクを置く
- **Issue と PR:** `gh --repo <repository> ...`。`repository` が null なら GitHub の操作をしない。`code` が `product_without_origin`（origin が無い）なら `git remote add origin <URL>`、`product_origin_not_github`（origin を GitHub の URL として読めない。SSH のホスト別名など）なら、GitHub で管理しているときに `git remote set-url origin https://github.com/<owner>/<repo>.git` が必要なことを説明する
- **検証:** 制作物の `package.json` のスクリプトと `xrift check world --build`・`xrift check item --build`。`xrift upload` は利用者が明示的に指示したときだけ実行する
- 制作物の中の `public/` は XRift のアセットディレクトリであり、特別に扱わない

## 止める（stop）

`code` ごとに利用者へ説明する。

| `code` | 説明すること |
|---|---|
| `product_uninitialized` | main checkout の Git 未初期化の制作物。その場で編集してよいが、commit・worktree・起票はしない。Git で管理するなら、`git init` し、必要なら `git remote add origin <URL>` する |
| `product_in_env_worktree` | 親の feature worktree の `items/`・`worlds/` の配下。制作物の置き場所ではなく、worktree を片付けると中身も消える。ここでは編集も `git init` もせず、`$XRIFT_POC_ROOT/{items,worlds}/<name>` の main checkout 側で扱う |
| `product_base` | `items/<name>` か `worlds/<name>` のディレクトリで作業する。新しい制作物は `xrift create` などでディレクトリを作ってから判定し直す |
| `product_origin_is_env` | 制作物の origin が親を指している。制作物自身のリポジトリを origin に設定する |
| `not_git`・`unknown`・`env_origin_mismatch` | 作業する場所を確かめる。親か制作物の checkout に移る |

## 公開前の秘密検査

親は public で、制作物のリポジトリも公開しうる。新しい commit を push するときは、commit の後・push の前に `scan_before_push` で未 push の commit を Gitleaks で検査し、検査した commit の SHA だけを push する。マージ commit で入った内容（競合の解消や、マージのときに足した変更）も検査する。

- **実行する場所:** Git の root で、引数なしに実行する。受け付けるのは、`route_target.py` が `env` と判定する親の feature worktree の root と、`product` と判定し `repository` が null でない制作物の Git の root（main checkout か、制作物の中の worktree）である
- **止まる場所:** 親の main checkout、Git の root ではない場所（subdirectory）、`stop`、`repository` が null の制作物。`repository` が null なら、origin を GitHub の URL にしてから実行する
- **base:** 親は `origin/main`、制作物は origin の既定ブランチ。どちらも fetch してから決める。origin にブランチが 1 つも無い制作物（GitHub で作ったばかりの空のリポジトリへの初めての push）では、HEAD から辿れる commit がすべて未 push なので、すべてを検査する。ブランチがあるのに既定ブランチを読めないときは止まる
- **設定:** base の commit の `.gitleaks.toml`、無ければ Gitleaks の既定の規則（`[extend]` の `useDefault = true`）を使う。作業ツリーの `.gitleaks.toml`、環境変数 `GITLEAKS_CONFIG`・`GITLEAKS_CONFIG_TOML`、inline の `gitleaks:allow` で検査を緩めない。`.gitleaksignore` があれば（壊れたシンボリックリンクを含む）止まる
- **出力:** 成功したときだけ、検査した commit の SHA を stdout に 1 行出す。ログは stderr に出る。検出・設定の取得・scanner の失敗を成功扱いしない
- Gitleaks は Dev Container が入れる approved pin（`.devcontainer/install_beforehand.bash`）を使う。`jq` も使う

シェルの状態は Bash の呼び出しをまたいで残らない。関数の定義・`XRIFT_POC_ROOT`・push は同じ呼び出しで実行する。

```bash
# repository-ownership scan before push
# Git の root で引数なしに実行する。成功したときだけ、検査した commit の SHA を stdout に出す
# zsh では "$VAR:r" などが修飾子になるため、: の前の変数は ${VAR} で区切る。path・status を変数名に使わない
scan_before_push() (
    set -euo pipefail
    : "${XRIFT_POC_ROOT:?XRIFT_POC_ROOT を xrift-poc の main checkout のパスに設定する}"
    here="$(pwd -P)" || return 1
    # stop と、判定の前提が壊れているときは、route_target.py が 0 以外で終わる
    route="$(uv run --no-project python "$XRIFT_POC_ROOT/.agents/scripts/route_target.py" "$here")" || return 1
    kind="$(jq -er .kind <<<"$route")" || return 1
    git_root="$(jq -er .git_root <<<"$route")" || return 1
    if [[ "$git_root" != "$here" ]]; then
        printf 'scan_before_push: Git の root（%s）で実行する\n' "$git_root" >&2
        return 1
    fi
    base_branch=""
    case "$kind" in
        env)
            if [[ "$(git rev-parse --path-format=absolute --git-dir)" == "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
                printf 'scan_before_push: 親の main checkout からは push しない。feature worktree で実行する\n' >&2
                return 1
            fi
            base_branch=main
            ;;
        product)
            if ! jq -er .repository <<<"$route" >/dev/null; then
                printf 'scan_before_push: 制作物の origin を GitHub のリポジトリとして読めない。origin を GitHub の URL にする\n' >&2
                return 1
            fi
            # origin にブランチが無ければ base は無い（HEAD から辿れる commit をすべて検査する）
            heads="$(git ls-remote --heads origin)" || return 1
            if [[ -n "$heads" ]]; then
                base_branch="$(git ls-remote --symref origin HEAD | awk '$1 == "ref:" && $3 == "HEAD" { sub("^refs/heads/", "", $2); print $2 }')" || return 1
                if [[ -z "$base_branch" ]]; then
                    printf 'scan_before_push: origin の既定ブランチを読めない\n' >&2
                    return 1
                fi
            fi
            ;;
        *)
            return 1
            ;;
    esac
    if [[ -e .gitleaksignore || -L .gitleaksignore ]]; then
        printf 'scan_before_push: .gitleaksignore があると検査を緩められる。push の前に止める\n' >&2
        return 1
    fi
    scanned_sha="$(git rev-parse --verify 'HEAD^{commit}')" || return 1
    scan_dir="$(mktemp -d)" || return 1
    trap 'rm -rf -- "${scan_dir:?}"' EXIT
    printf '[extend]\nuseDefault = true\n' > "$scan_dir/config.toml" || return 1
    : > "$scan_dir/ignore" || return 1
    range="$scanned_sha"
    if [[ -n "$base_branch" ]]; then
        git fetch --no-tags origin "+refs/heads/${base_branch}:refs/remotes/origin/${base_branch}" >&2 || return 1
        base_sha="$(git rev-parse --verify "refs/remotes/origin/${base_branch}^{commit}")" || return 1
        range="${base_sha}..${scanned_sha}"
        if git cat-file -e "${base_sha}:.gitleaks.toml" 2>/dev/null; then
            git cat-file blob "${base_sha}:.gitleaks.toml" > "$scan_dir/config.toml" || return 1
        fi
    fi
    # マージ commit も各親との差分として検査する。利用者の log.diffMerges（off など）に左右されない
    env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML \
        gitleaks git --redact --ignore-gitleaks-allow --config "$scan_dir/config.toml" \
        --gitleaks-ignore-path "$scan_dir/ignore" --log-opts="--diff-merges=separate ${range}" . >&2 || return 1
    printf '%s\n' "$scanned_sha"
)
```

## 振り分けを誤ったときの症状

| 症状 | 原因 |
|---|---|
| 制作物の Issue・PR が `usaturn/xrift-poc` に作られた | `--repo` を付けずに `gh` を実行した、または Git 未初期化の制作物で祖先の親を操作先と判定した |
| 親の `git status` に `items/` や `worlds/` の中身が出る | `git add -f` した、または `.gitignore` の `/items/`・`/worlds/` が変わった |
| 制作物の worktree で共通のスキルが見えない | worktree を親の外に作った、または `xrift-link` を実行していない |
