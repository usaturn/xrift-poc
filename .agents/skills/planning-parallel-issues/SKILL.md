---
name: planning-parallel-issues
description: タスクに GitHub の親 Issue と並行して進める Sub Issue が必要なとき、既存の Issue ボードに依存関係や担当範囲の境界を定める必要があるとき、またはエージェントが親 Issue から着手できる子を選び、その実装に向けた handoff を準備しなければならないときに使う。
---

# 並行 Issue の計画

親 Issue を、独立したセッションの入口にする。並行して進められるかどうかは、明示した依存関係と重ならない担当範囲で決まる。着手できるかどうかは、取得し直した最新の証拠で決まる。claim した planning のタスクは、復元できる Spec・Plan・handoff の成果物で終える。

このスキルを使うことを宣言する。書く前に、リポジトリの規則、既存の Issue、コード、利用できるスキルを読む。Issue の本文と報告は、利用者の言語で書く。

## 依頼された操作を選ぶ

| 依頼 | 必要な手順 | 完了の条件 |
|---|---|---|
| タスクを分解する。親 Issue と Sub Issue を作成・更新する | [issue-board.md](references/issue-board.md) と [github-operations.md](references/github-operations.md) を読む | 公開したボード、native の関係、確認済みの開始指示 |
| 親から始める。着手できる子を 1 つ引き受ける | [session-workflow.md](references/session-workflow.md) と [github-operations.md](references/github-operations.md) を読む | 1 つの子の Spec → Plan → 対の handoff、復元の確認、claim の解放 |
| 両方 | ボードを作成・確認してから、条件を満たす子を 1 つ引き受ける | 両方の完了の条件 |

既存の Issue と有効な成果物を再利用する。ボードを作っただけでは、すべての子を claim したことにはならない。明確な操作を伴わない URL は、会話の中の依頼を引き継ぐ。「ボードを更新して」を、断りなく実装に置き換えない。

## 完了の規則

- **ボード:** 具体的な受け入れ基準、依存関係の DAG、ファイルの担当、共有インターフェースの担当者、検証、native の Sub Issue と `blocked_by`、親の中の自己完結した開始指示。参照文書が定める必須の出力項目に従う。
- **選択:** 判断の前に、子、コメントの全ページ、依存先の結果、PR、claim ref を取得し直す。コメントの 1 回分の応答だけでは証拠として不完全である。GitHub CLI では `gh api --paginate` を使う。親にある Ready の表示は snapshot である。既定の完了条件を満たすのは、`state_reason=completed` で close された先行 Issue だけである。`not_planned` と `duplicate` は条件を満たさない。duplicate の先行 Issue はボード上で重複先の Issue に置き換え、選択の前にその Issue を確かめる。マージされていない必須の PR や、別のセッションの claim も、その子の選択を妨げる。
- **所有権:** 一意の空の claim commit を作り、固定の remote ref を 1 つアトミックに新規作成する。ref を作る前に、その commit の SHA を保存する。書き込みの前に所有権を確かめ直す。解放は expected-SHA の lease で行う。コメントと assignee は状況を見せるためのもので、ロックではない。
- **planning:** 必須のサブスキルは、`superpowers:brainstorming`、次に `superpowers:writing-plans`、次に `agent-handoff` である。利用者が成果物を求めているので、範囲の限られた子でも Spec を書く経路を使う。推奨案を使い、3 つすべてを続けて進める。agent の判断を記録する。実際には無い人間の承認を作り上げて記録しない。利用者の認可は、ルーチンの承認待ちや「どの実行方式にするか」の確認による中断より優先する。
- **復元:** 準備ができたと告知する前に、成果物の正確な内容を hash とともに公開し、新しいディレクトリへ復元する。一度も公開していないローカルのパスや commit では足りない。
- **境界:** 実装の子は、`planning: ready`、`implementation: not-started` で open のままにする。コード、実装のテスト、マージ、デプロイには、それ専用の認可された実行タスクが要る。条件を満たす子が無ければ、blocker を報告し、claim を保持せずに終える。

## よくある誤り

| 誘惑 | 必要な対応 |
|---|---|
| 「コメントして assign し、読み直せば競合を防げる」 | セッション固有の SHA で、固定 ref をアトミックに新規作成する |
| 「古い claim はたぶん放置されている」 | そのまま残し、所有者と ref を報告する |
| 「ファイルはこのホストにある」 | すべての成果物について、remote との往復を確かめる |
| 「Spec はできた。Plan の前に確認しよう」 | 推奨案で handoff まで続ける |

所有権が不確かなとき、並行する子が同じ共有インターフェースを編集するとき、または handoff の成果物のどれかを復元できないときは、成功を宣言する前に止めて直す。
