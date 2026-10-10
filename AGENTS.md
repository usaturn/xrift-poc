# AGENTS.md

## リポジトリ概要

xrift-poc は、XRift のワールドとアイテムを作るための開発環境（Dev Container・コーディングエージェント・共通スキル・開発支援ツール）を管理する。ワールド・アイテムの制作物そのものは管理しない。

現在は最小の初期化だけを済ませた状態で、開発環境の移植は Issue #1 で進めている。

## 操作先の分担

| 作業対象 | Git・Issue・PR の操作先 |
|---|---|
| 開発環境の変更 | `usaturn/xrift-poc`（このリポジトリ） |
| `items/<name>/`・`worlds/<name>/` の制作物 | 対象プロジェクト自身の独立した Git リポジトリ。リモート URL を確認してから操作する |
| Git 未初期化、または操作先が確認できない制作物 | GitHub の操作を止め、必要な設定を利用者に説明する。代わりに `xrift-poc` へ commit・起票しない |

- `items/` と `worlds/` はこのリポジトリの `.gitignore` で無視している。配下を `git add` せず、サブモジュールや gitlink として登録しない
- Git 未初期化の `worlds/<name>/` で `git rev-parse --show-toplevel` を実行すると、祖先の `xrift-poc` が見つかる。コマンドが成功しただけで「制作物が Git 管理されている」と判定しない。対象のルート・実際の Git ルート・`git remote get-url origin` を照合する
- 制作物の中の `public/` は XRift のアセットディレクトリである。削除・無視の対象にしない

## Git・PR

- ローカルに `main` を作らず、`origin/main` を基準にする
- 開発環境の変更は、最新の `origin/main` から feature branch の worktree を作り、PR で取り込む。`main` へ直接 commit しない
- ユーザの指示なしに commit・push しない
- `xrift upload world`・`xrift upload item` は、利用者が明示的に公開を指示したときだけ実行する

## 会話と文書

- 利用者との会話、Issue・PR・レビューは日本語で書く
- 不自然な直訳を避け、日本人エンジニアに一般的な表現を使う
