---
name: repository-ownership
description: このリポジトリでどんな作業を始めるときにも使う。Issue・worktree・PR を作る前や検証を実行する前に、変更が公開アプリケーションリポジトリ（usaturn/maatlog。public/ に checkout する）と親の開発環境リポジトリ（usaturn/maatlog-poc）のどちらに属するかを決める。編集・テストの実行・PR をリポジトリに振り分ける前に読み込む。
---

# リポジトリの分担

MaatLog の作業は 2 つのリポジトリに分かれている。作業を誤ったリポジトリに振り分けると、作業が失われる（アプリケーションの変更を親にだけ commit した場合。親のアプリケーションのツリーは cutover で削除される）か、公開ミラーが壊れる（親の commit が `public/` の gitlink を一緒に動かした場合）。

| 対象 | リポジトリ | checkout 先 | 既定の base |
|---|---|---|---|
| アプリケーション: `src/`、`tests/`、`docs/`、`pyproject.toml`、`uv.lock`、README、配布物 | `usaturn/maatlog`（公開） | `public/` submodule、または単独の clone | 公開側の `origin/main` |
| 開発環境: `.agents/`、`.claude/`、`.devin/`、`.codex/`、`.pi/`、`.cursor/`、`tools/`、`scripts/ci/verify-devenv.sh`、`.devcontainer/` | `usaturn/maatlog-poc`（この親） | この checkout | 親の `origin/main` |

## 作業の前に振り分け先を決める

親の checkout の中で、branch・worktree・Issue・PR を作る前にこの route check を実行する。route check は親の `origin` を確かめ、さらに `git -C public remote get-url origin` が `.gitmodules` の URL と一致することを確かめる。値が `.gitmodules` と一致しない場合は submodule が別の場所を指しているので、アプリケーションの作業をする前に必ず直す。

```bash
# repository-ownership route check
TOPLEVEL="$(git rev-parse --show-toplevel)"

app_root() {
    local expected actual
    if ! expected="$(git -C "$TOPLEVEL" config -f .gitmodules --get submodule.public.url)"; then
        printf 'repository-ownership: public/ is not registered in %s/.gitmodules\n' "$TOPLEVEL" >&2
        return 1
    fi
    if ! actual="$(git -C "$TOPLEVEL/public" remote get-url origin 2>/dev/null)"; then
        printf 'repository-ownership: %s/public is not a readable checkout; run: git -C %s submodule update --init public\n' "$TOPLEVEL" "$TOPLEVEL" >&2
        return 1
    fi
    if [[ "$actual" != "$expected" ]]; then
        printf 'repository-ownership: public/ origin %s does not match .gitmodules %s; fix the submodule remote before application work\n' "$actual" "$expected" >&2
        return 1
    fi
    printf '%s\n' "$TOPLEVEL/public"
}

env_root() {
    printf '%s\n' "$TOPLEVEL"
}

assert_no_gitlink_staged() {
    if git -C "$TOPLEVEL" diff --cached --name-only | grep -qx 'public'; then
        printf 'repository-ownership: the public gitlink is staged; unstage it unless this commit is an intentional checkpoint\n' >&2
        return 1
    fi
}
```

`app_root` が失敗したら停止する。代わりの root を推測しない。`public/` の checkout が無い場合は `git submodule update --init public` で直す。`origin` が一致しない場合は submodule の remote を直す。公開側の checkout がおかしく見えるという理由で、アプリケーションの作業を親に振り替えてはならない。

## アプリケーションの振り分け先（公開側）

利用者に届くものはすべて `usaturn/maatlog` に置く。

- **worktree の base:** submodule の中で fetch した公開側の `origin/main`。worktree のパスは公開側の checkout の**外**に置く。`public/` の下に作った worktree は、親では `modified: public (untracked content)` として表示され、公開側で一括の `git add` を実行すると公開リポジトリに取り込まれるおそれがある。代わりに、親で ignore されている `.claude/worktrees/` の下に置く。

  ```bash
  # repository-ownership app worktree
  APP_ROOT="$(app_root)"
  git -C "$APP_ROOT" fetch origin main
  git -C "$APP_ROOT" worktree add "$TOPLEVEL/.claude/worktrees/<name>" -b <branch> origin/main
  ```

  アプリケーションの branch を親の `origin/main` から切らない。親の `origin/main` には cutover 後にアプリケーションのツリーが無く、現時点でも公開側の履歴から分岐している。

- **Issue と PR:** `gh --repo usaturn/maatlog ...`。親の checkout で `--repo` を付けずに `gh pr` を呼ぶと、親の `origin` に対して解決され、警告なしに誤ったリポジトリを操作する。毎回 `--repo` を指定する。

- **検証:** 公開側の checkout の中で `./scripts/ci/verify.sh <profile>` を実行する。`full`/`quick`/`static` の各プロファイルはこの検証スクリプトの仕様である（`AGENTS.md` を参照）。親の CI 免除は、ここには一切適用されない。

## 開発環境の振り分け先（親）

開発環境の作業は `usaturn/maatlog-poc` で行う。

- **worktree の base:** 親の `origin/main`。

  ```bash
  git fetch origin main
  git worktree add .claude/worktrees/<name> -b <branch> origin/main
  ```

  親の Issue が統合用の branch を指定している場合は、テストの前にその branch を feature branch に merge し、PR の向き先を `main` ではなくその branch にする。

- **Issue と PR:** `git remote get-url origin` でこの checkout が親であることを確かめてから、`gh --repo usaturn/maatlog-poc ...` を使う。

- **検証:** 親の root で `./scripts/ci/verify-devenv.sh` を実行する。

## public の gitlink は checkpoint であり、差分ではない

親の `public/` のエントリは gitlink（公開側の commit を固定して指すポインタ）である。通常の開発環境の commit で gitlink を動かしてはならない。

- 親の変更はパスを明示して stage する。親では一括の stage を禁止する。
- 親で commit する前には毎回、route check の `assert_no_gitlink_staged` を実行する。失敗したら `public` を unstage し、commit を分ける。
- `public` の gitlink を stage してよいのは、意図した checkpoint commit（タスクが明示的に求めるリリース・互換性のための固定）だけである。その場合もパスを明示して stage し、checkpoint 専用の commit にする。

## 振り分けを誤ったときの症状

| 症状 | 原因 |
|---|---|
| アプリケーションの PR が `usaturn/maatlog-poc` に作られた | 親の checkout で `--repo usaturn/maatlog` を付けずに `gh` を実行した |
| アプリケーションの worktree に `src/` が無い | 公開側ではなく親の `origin/main` から branch を切った |
| 開発環境の commit の後に `modified: public` が出る | 一括の stage で gitlink が巻き込まれた。unstage して commit し直す |
| 公開側の CI を「免除」として省いた | CI の免除は親の PR だけが対象である。公開側の PR には公開 CI の green ゲートがそのまま適用される |
