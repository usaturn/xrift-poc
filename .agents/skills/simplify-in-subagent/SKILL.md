---
name: simplify-in-subagent
description: "git diff の範囲に限定したコードの簡素化で、サブエージェントでも使える /simplify である。変更行だけを対象に、ネストの解消・命名の改善・冗長さの削減を行う。--staged / --ref=<ref> / ファイル指定を受け付ける。/simplify のスラッシュコマンドが使えないとき、親が /simplify と同等の処理を求めたとき、サブエージェント・worktree で --staged / --ref / ファイル指定の simplify をするときに使う。メインセッションの /simplify の代わりではない。Devin には組み込みの /simplify が無いため、Devin ではこのスキルが simplify 相当の経路になり、サブエージェントとして実行する。"
argument-hint: "[--staged|--ref=<ref>|<files>]"
subagent: true
allowed-tools:
  - read
  - edit
  - grep
  - glob
  - exec
---

# Simplify in Subagent

対象指定: $ARGUMENTS

変更された行だけを対象にする。ファイル全体のリファクタリングは決してしない。`git` のコマンドはすべて、親の cwd ではなく自分の cwd で実行する。

## ランタイムごとの違い

### Devin

- Devin CLI はスキル呼び出し時、本文中の `＄ARGUMENTS` と `＄1`〜`＄9`（ドル記号+数字1文字）形式のプレースホルダを呼び出し引数で全文置換する（この節では補間されないよう意図的に全角で表記している。半角で書くとこの説明文自体が引数で上書きされる）。埋め込みコードではシェルの位置パラメータ表記を使わず、入力は stdin や名前付き変数で受ける。
- 冒頭の `対象指定` 行がこの補間欄である。値が空欄、またはドル記号始まりのプレースホルダ表記のまま残っている場合は未指定（既定の `HEAD` との diff）として扱う。
- `subagent: true` により本文はサブエージェントの task として実行される。サブエージェントでは `ask_user_question` が常に使えないため、不明点や異常は質問にせず「異常時の対応」の節に従って報告に回す。`run_subagent` / `read_subagent` もサブエージェント内では無効だが、本手順はネストしたサブエージェントを必要としない。
- Devin には `/simplify` 相当の組み込みコマンドが無いため、メインセッションからの直呼び出しでも `subagent: true` によりサブエージェントで実行される。

### Claude Code

- Claude Code も、本文の `＄ARGUMENTS`（Devin の項目と同じ理由で全角で表記している）を呼び出しの引数で置き換える。冒頭の `対象指定` の行が空、またはプレースホルダのままなら、Pi・Codex と同じく、呼び出し元の依頼文が示す対象を使い、無ければ `HEAD` を使う。
- Claude Code は `subagent` のキーを使わず、このスキルを呼び出したセッションの中でそのまま実行する。メインセッションでは、このスキルではなく組み込みの `/simplify` を使う。

### 対象指定の行がプレースホルダのまま残るランタイム

- Pi と Codex は引数を補間しないので、冒頭の `対象指定` の行はプレースホルダのまま残る。Cursor CLI と Grok Build で置き換わるかは確認していない。上の節（Devin・Claude Code）に書いていないランタイムで、この行がプレースホルダのまま残っている場合は、呼び出し元の依頼文が示す対象を使い、無ければ `HEAD` を使う。呼び出し元は、対象を依頼文に書く。

### frontmatter の `allowed-tools`

- 一覧の read・edit・grep・glob・exec は Devin のツール名で、Claude Code のツール名（`Read`・`Edit` など）とは異なる。
- Devin はこの一覧を読む（`devin skills show simplify-in-subagent` の `Allowed tools` に read・edit・grep・glob・exec と出る）。Devin 3000.11.3 の同梱ドキュメント `creating-skills.mdx` によると、`allowed-tools` は、スキルをインラインで実行するときに許可の確認を省くツールの一覧で、使えるツールを制限しない。スキルをサブエージェントとして実行するとき（`subagent: true`）には適用されず、ツールはサブエージェントのプロファイルで決まる。このスキルは `subagent: true` なので、サブエージェントとして実行するときは Devin でもこの一覧は効かない。
- Claude Code も `allowed-tools` のキーを読む（Claude Code 2.1.292 は値として文字列とリストのどちらも受け付ける）。ただし、Devin の小文字のツール名を Claude Code がどう扱うかは確認していない。ほかのランタイムでの扱いも確認していない。

## 入力

- 必須のルーティング情報: 対象リポジトリと、作業する root。委譲の前に、呼び出し元が `repository-ownership` を使って決める。これらは新しいスラッシュコマンドのフラグとしてではなく、タスクの文脈として渡す。対象を、現在の cwd、スキルの置き場所、`src/` の有無から推測しない。
- 必須: ref（既定は `HEAD`）、`--staged`、明示したファイルの一覧のどれか 1 つ。親はどれを使うかを示すべきである。冒頭の `対象指定` の行に値が補間されていれば、その値を使う。値が空、またはプレースホルダのまま残っている場合の扱いは、「ランタイムごとの違い」の節に従う。
- 任意: テストのコマンド。既定は関係する既存のテスト。

## リポジトリの事前確認

ファイルを導出する前、または diff を読む前に、すべての入力モード（既定の `HEAD`、`--ref`、`--staged`、明示したファイル）について、ルーティング情報を確かめる。ルーティングの判断は `repository-ownership` に従う。対象リポジトリまたは作業する root を確定できない場合は、停止して、足りない情報を報告する。

自分の cwd で、次の読み取り専用のコマンドを実行する。

```bash
git rev-parse --show-toplevel
git remote get-url origin
```

Git の root と作業する root を、解決したファイルシステム上のパスで比べる。origin の GitHub の owner/repository を、宣言された対象と比べる。GitHub の標準の HTTPS と SSH の URL は、末尾の `.git` の有無にかかわらず、同じリポジトリを示しうる。ホスト・owner・リポジトリが異なるもの、または認識できない形式の URL は合格としない。SSH のエイリアスを推測しない。

| タスク | 対象リポジトリ | 作業する root | `origin/main` の意味 |
| --- | --- | --- | --- |
| 制作物 | 制作物の origin のリポジトリ | `items/<name>`・`worlds/<name>` の Git リポジトリ、またはその worktree | 制作物の remote-tracking ref |
| 開発環境 | `usaturn/xrift-poc` | 親の checkout、または親から作った worktree | 親の remote-tracking ref |

`--ref=origin/main` の場合は、route が一致した後でだけ、同じ cwd でその ref を解決する。

```bash
git rev-parse --verify 'origin/main^{commit}'
```

先へ進む前に、その SHA を記録する。ほかの入力モードでも route の確認は必要である。`origin/main` を使わない限り、`origin/main` の解決は必要ない。

コマンドが失敗した場合、root または origin が異なる場合、選んだ ref を解決できない場合は、ファイルを導出する前、編集する前に停止する。期待した対象と root、観測した root と origin、失敗したコマンドとその理由を報告する。失敗した事前確認を迂回するために、diff が空のときの `HEAD~1` へのフォールバックを使わない。暗黙の修復として、cwd を変える、`git -C` で別のリポジトリを使う、remote を書き換える、fetch する、のいずれもしない。呼び出し元が正しい作業の文脈を渡して、再実行しなければならない。

## 手順

1. **ファイルを導出する。** リポジトリの事前確認に合格した後、依頼に合った `name-status` のコマンドを実行する。`M→modified`、`A→added`、`R→renamed`、`C→copied` と対応づける。renamed・copied は新しいパスを使う。ほかのステータスは無視する。

   | 依頼 | 自分の cwd で実行するコマンド |
   | --- | --- |
   | 既定 | `git diff --name-status HEAD` |
   | `--staged` | `git diff --name-status --cached` |
   | `--ref=X` | 上の行の `HEAD` を `X` に置き換える |
   | `<files>` | `modified` として扱い、`name-status` は実行しない |
   | 結果が空 | `git diff --name-status HEAD~1` に一度だけフォールバックする |

2. **行を導出する。** ファイルごとに（`added` は除く）、`git diff --unified=0 --no-ext-diff <ref|--cached> -- <path>` の `+start,count` の hunk を解析して範囲にし、隣接する範囲はまとめる。`added` はファイル全体を範囲とする。範囲が 0 個なら、削除だけである。
3. **範囲の中だけに適用する。** 機能を保つ。`AGENTS.md` に従う。ネストと冗長さを減らし、名前を改善する。理由や意図を書いたコメントは残し、本当にノイズであるものだけを消す。三項演算子を入れ子にしない。凝ったコードを書かない、巨大な関数を作らない、抽象化を取り除かない。前後のコードを読んで文脈をつかむ。ファイルは 1 つずつ扱う。機能の追加、API の変更、変更されていないコードの編集はしない。
4. **確かめて報告する。** 関係するテストを実行する。ファイルごとの変更とその理由に加えて、範囲外の改善案（言及するだけで、決して編集しない）を出力する。報告には、確かめた対象リポジトリ、Git の root、origin、比較に使った ref（該当する場合は解決した SHA も）を含める。

## 確認項目

- diff を見る前、編集する前に、リポジトリの事前確認に合格している。
- 編集の前に範囲を導出している。`added` と削除だけの場合は、手順 2 のとおりに扱っている。
- すべての編集が、一覧にした範囲の中にある。周りのコードは読むだけにしている。
- 報告の前にテストが合格している。

## 異常時の対応

- route または ref の確認に失敗した → diff を見る前、編集する前に、「リポジトリの事前確認」の節の、停止して報告する手順に従う。
- 変更されたファイルが無い（フォールバックの後も含む）→ そのことを報告して停止する。
- diff が失敗した → 編集の前に `git diff` を手で調べる。範囲を推測しない。
- 自分の cwd でファイルが見つからない、またはパスが空 → 停止して食い違いを報告する。代わりのファイルを編集しない。
- 整形だけのツール（ruff/prettier）は、読みやすさを改善する見直しの代わりには決してならない。

## 例

- 制作物: 呼び出し元が、`/simplify-in-subagent --ref=origin/main` について、対象の制作物のリポジトリと、作業する root として `worlds/<name>` の worktree を宣言する。その worktree で root と origin を確かめ、そのリポジトリの `origin/main` を解決してから、diff に基づいて `src/foo.py` の 10〜18 行目を簡素化する。編集してよいのは、変更された行だけである。関係するテストを実行し、route と変更を報告する。
- 開発環境: 呼び出し元が、`/simplify-in-subagent --ref=origin/main` について、対象 `usaturn/xrift-poc` と親の worktree を宣言する。`tools/devenv/...` の変更行にそのリポジトリの `origin/main` を使う前に、その worktree で root と origin を確かめる。
- route の誤り: 対象は制作物のリポジトリだが、現在の cwd は親の checkout で、依頼は `src/foo.py` と `--ref=origin/main` を指定している。事前確認で停止し、食い違いを報告し、意図した制作物の worktree で再実行するよう呼び出し元に求める。親の diff を読まず、フォールバックも使わない。ここで「呼び出し元に求める」とは、その指示を結果の報告に書いて返すことを指し、質問のツールは必要としない。
