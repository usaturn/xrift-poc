---
name: herdr-agent-task
description: 作業しているタスクが変わったとき、またはタスクを完了したときに、Herdr 左サイドバーの Agents 一覧へ現在のタスク名と状態を反映する場合に使う。Herdr の pane 制御や他エージェントの操作を求められた場合には使用しない。
---

# Herdr Agent Task

## 概要

Herdr 左サイドバーの Agents 一覧に、この pane が今どのタスクを担当しているかを表示する。
ユーザは複数のコーディングエージェントを並列に走らせており、pane を順に開かずに
「誰が・何を・どの状態で」作業しているかを把握したい。

エージェントの種別（claude / codex / cursor / grok / pi）と稼働状態（working / idle /
blocked）は Herdr が自動判定する。このスキルが供給するのは **タスク名** と **意味的な状態語**
だけである。

## 前提

Herdr 管理下の pane で動いているときだけ実行する。

```bash
test "${HERDR_ENV:-}" = 1
```

これが偽なら何もしない。`herdr-agent-task` は `herdr` が PATH に無ければ何もせず終了する
ため、誤って呼んでも壊れない。

## 使い方

タスクに着手したとき:

```bash
herdr-agent-task set "Implement page transition"
```

状態を明示したいとき:

```bash
herdr-agent-task set "Review Issue #67 changes" --state reviewing
herdr-agent-task set "Wait for CI" --state waiting
herdr-agent-task set "Implement page transition" --state completed
```

担当を手放すとき:

```bash
herdr-agent-task clear
```

現在の pane メタデータを確認するとき:

```bash
herdr-agent-task show
```

## 状態の選び方

| `--state` | 使う場面 |
| --- | --- |
| `working`（既定） | 実装・調査を進めている |
| `waiting` | CI・ビルド・外部処理の完了を待っている |
| `reviewing` | 他エージェントの変更やコードをレビューしている |
| `completed` | 依頼されたタスクを完了した |
| `idle` | 担当が無い状態に戻す |

`attention` は指定できない。ユーザの入力や承認を待って停止している状態は、
エージェント自身がコマンドを実行できないため、Herdr が `blocked` として自動検出する。

## タスク名の書き方

- 英語または日本語の短い名詞句にする。36 文字を超えると末尾が `…` に truncate される
- 「何をしているか」を書く。`Implement page transition` `Review Issue #67` `Investigate browser behavior`
- プロダクト名や自分の名前は書かない。エージェント名は Herdr が別に表示する
- サイドバーの 1 行に載るため、**改行・タブは空白へ潰される**。連続した空白は 1 つに詰められ、
  前後の空白は落とされる
- **空のタスク名は指定できない**。`set ""` や空白だけのタスク名は exit 64 で失敗する

## 呼ぶタイミング

- タスクに着手した直後に 1 回
- 担当タスクが切り替わったときに 1 回
- タスクを完了したときに `--state completed` で 1 回

進捗のたびに呼ばない。サイドバーがちらつき、他のエージェントの一覧性を損なう。
