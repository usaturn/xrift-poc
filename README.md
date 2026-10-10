# xrift-poc

[XRift](https://xrift.net) のワールドとアイテムを作成・プレビュー・検証するための開発環境です。Dev Container・コーディングエージェント・共通スキル・開発支援ツールを管理します。

**このリポジトリで管理するのは開発環境だけです。** ワールド・アイテムのソースコード、アセット、依存パッケージ、ビルド成果物は管理しません。

## 状況

このリポジトリは最小の初期化だけを済ませた状態です。Dev Container・エージェント・スキル・ツールの移植と XRift 向けの設定は [Issue #1](https://github.com/usaturn/xrift-poc/issues/1) で進めています。

## 構成

```text
xrift-poc/
├── AGENTS.md    # エージェント向けの共通指示
├── CLAUDE.md    # Claude Code 用（AGENTS.md を読み込む）
├── README.md
├── .gitignore
├── items/       # Git 管理対象外。各アイテムの作業場所
└── worlds/      # Git 管理対象外。各ワールドの作業場所
```

`items/` と `worlds/` は `.gitignore` でルートから無視しています。中のディレクトリはサブモジュールにせず、このリポジトリには登録しません。各制作物は、必要に応じて `items/<name>/`・`worlds/<name>/` の中で独立した Git リポジトリとして管理します。Git を初期化していないローカルのプロジェクトでも構いません。

## 変更の進め方

- 最初の commit だけは、空のリポジトリに `main` を作るために直接 push しました。
- 以降の開発環境の変更は、最新の `origin/main` から feature branch（worktree）を作り、PR で取り込みます。`main` には直接 commit しません。
- 制作物の変更は、その制作物自身のリポジトリで行います。このリポジトリに commit したり Issue を作ったりしません。
