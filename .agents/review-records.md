# review-records v1: レビューと修正計画が共有する記録の形式

この文書は、pr-review と review-fix-planning が受け渡す情報の形式を定める（形式の定義元）。両スキルと、状況コメントを投稿する helper（`.agents/skills/review-fix-planning/scripts/upsert_pr_comment.py`）は、この文書に従う。両スキルの全体設計と決定の経緯は、Issue [#494](https://github.com/usaturn/maatlog-poc/issues/494) の Spec にある。

- 判断の記録の検査・照合・集約・描画・取り出しは、`.agents/scripts/review_records.py` が実装する。標準ライブラリだけで書かれており、`uv run --no-project python` で動く（7.6 節）。
- この文書の記入例のうち、PR・指摘・HEAD を示すもの（識別子の例を含む）は、すべて架空の PR #900 のもので、実在の PR やレビューの指摘とは関係しない。pr-review はこの文書を読むため、実在の指摘を記入例にすると、その PR を使った評価でレビュアーに答えを渡してしまう。記入例を足すときも、実在の PR の指摘を使わない。
- この文書の記入例を検査する常設のテストは無い。記入例の判断の記録（`<!-- review-records:example round-1 -->` の例の JSON と、`status-comment-round-2` の例の記録のブロックの中の JSON）を変えたら、それぞれをファイルに書き出し、`uv run --no-project python .agents/scripts/review_records.py validate --record-file <file>` で検査する。
- 形式を変える必要があるときは、10 章の手順に従う。

## 1. 用語と、誰が何を読み書きするか

- **レビュアー**: pr-review を実行するエージェント。1 つのレビューファイル `<model>.md` を書く。
- **席**: 1 つの巡でレビューしたレビュアーと、そのレビューファイルの組。
- **planner**: review-fix-planning を実行するエージェント。指摘を判断し、判断の記録を書く。
- **巡（round）**: レビューと判断の対象にした 1 つの HEAD に対する、レビューと計画のひとまとまり。1 から数える。
- **PR 修正ループ（loop）**: `.agents/skills/pr-review-fix-loop`（利用者が明示的に起動する）。レビュアーは通常の pr-review を実行し、各サイクルの計画は review-fix-planning が行うので、この文書の形式をそのまま読み書きする。loop が run の中で数えるサイクルの番号は、この文書の巡とは別のものである（対応は loop の記録の `planning.round` にある）。旧 loop（`herdr-pr-review <PR> --loop`。#571 で削除）が独自に数えた round（0 から始まる）も、巡とは別のものである。

| 情報 | 書く | 読む | 置き場所 |
| --- | --- | --- | --- |
| 指摘と根拠の項目（4 章） | レビュアー | planner | レビューファイル `<model>.md`。`reviews/` は gitignored で、ローカルにしか無い |
| 判断の記録（6 章） | planner | レビュアーと、次の巡の planner | PR の状況コメント（7 章）。`gh` で読める |
| 席構成とモデル情報（3 章） | planner | 人と、次の巡の planner | 判断の記録と状況コメント |

スキルごとに従う節は次のとおり。

- pr-review: 2 章（識別子）、3 章（モデル情報）、4 章（レビューファイルに書く項目）、5 章（D1 の判定に使う材料）、6 章（判断の記録の意味）、8.1〜8.3 節（判断の記録の読み方と扱い）、9 章（互換）
- review-fix-planning: 2〜9 章のすべて。とくに 6 章（判断の記録）、7 章（状況コメントと helper）、8.4 節（planner の扱い）

## 2. 識別子

### 2.1 指摘の参照

指摘を PR の中で一意に示すため、`<レビューファイル名>#<指摘 ID>@<検出時 HEAD>` と書く。

| 部分 | 内容 |
| --- | --- |
| `<レビューファイル名>` | レビューファイルの名前から `.md` を除いたもの（`[A-Za-z0-9][A-Za-z0-9._-]*`） |
| `<指摘 ID>` | レビューファイルの中の ID。`C-1`・`H-1`・`M-1`・`L-1`・`I-1` と、Unverified candidates の `U-1`（4.3 節） |
| `<検出時 HEAD>` | 指摘の検出時 HEAD の先頭 12 桁。`不明（旧形式）` の指摘は `unknown` |

例: `claude-opus-5-5#M-1@2222bbbb3333`

- レビュアーは、継続する指摘の ID と検出時 HEAD を維持する。そのため、Update をまたいでも参照は変わらない。ファイルを作り直して同じ ID を使った場合も、検出時 HEAD が違うので区別できる。
- **指摘 ID の無いレビューファイル**（pr-review の様式に従っていないもの）は、planner が `planner-<n>`（1 から数える）の ID を付け、検出時 HEAD にはそのファイルの対象 HEAD を使う。席の `note` に、ID を付けたことを書く。後でレビュアーが同じ指摘に ID を付けたら、その参照を同じ判断の `sources` に足す。
- **修正の確認で見つかった問題**（review-verify の `mode: fix_check` が新たに見つけ、`sources` が空のもの）は、`fix-check#V-<n>@<確かめたローカルの HEAD の先頭 12 桁>` と書く。`V-<n>` は修正の確認の一覧（`verified/<HEAD 12>.fix-check.json`）の指摘 ID。

### 2.2 判断 ID

判断が最初に記録されたときの、出典の指摘の参照を判断 ID にする。

- 1 つの指摘を複数の判断に分けるときは、末尾に `/<部分>`（小文字の英数字）を付ける。例: `Grok-4.7#L-1@1111aaaa2222/a` と `…/b`。分けてよいのは、その参照が初めて記録される巡だけである。
- 判断 ID から `/<部分>` を除いた参照は、その判断の `sources` に含める。
- 1 つの参照を複数の判断の `sources` に入れてよいのは、その参照を基にした部分どうし（`X/a` と `X/b` など）だけである。
- 後の巡で同じ問題が別の席から出たら、その指摘の参照を、同じ判断の `sources` に足す。判断 ID は変えない。
- 記録済みの参照を、別の判断に移さない。記録済みの参照を基にした新しい判断も作らない。2 つの判断が同じ問題だと後で分かったときは、一方を `skip` にし、`reason` と `revisit_when` に他方の判断 ID を書く。
- 指摘に対応する判断を探すときは、判断 ID ではなく `sources` で探す。
- 採番を調整しなくてよいので、判断 ID は planner が変わっても衝突しない。

## 3. モデル情報

誰が（どのモデルが、どのランタイムで）レビューし、判断し、再現を実行したかを、すべての記録に残す。

| 記録 | 残す値 |
| --- | --- |
| レビューファイル | 先頭の `- レビュアー:` の行（4.1 節） |
| 判断の記録 | `planner` の `model`・`runtime`・`agent_id`、`seats[]` の `review_file`・`model`・`runtime` |
| 状況コメント | 見出し `## review-fix-planning（<モデル識別子>）` と、席構成の表のモデル・ランタイムの列 |
| 集約の結果（8.1 節） | 判断の履歴の 1 件ごとの `planner_model` |
| D1 の再現（5 章） | `d1.reproduced_by`（再現した席の指摘の参照、`planner`、または review-verify の確認担当 `verifier`） |

- モデル識別子は、ランタイムが提示する正確な値を使う。推測・短縮・一般化しない。
- `planner.agent_id` は、`planner.model` の `/` と半角空白を `-` にし、連続する `-` を 1 つにまとめた値である（`review_records.py` の `agent_id_for`）。状況コメントのマーカーもこの値を使う。
- 席の `review_file` は、レビューファイルの名前そのもの（`.md` を除く）である。`agent_id` とは変換の規則が違うので、比べない。
- ランタイムは、`[a-z0-9][a-z0-9._-]*` に合う小文字の名前にする。現在の値は `claude-code`・`codex`・`devin`・`pi`・`cursor` である。
- `unknown` は、`レビュアー` の行が無い旧形式・様式外のレビューファイルの席にだけ使い、その席の `note` に理由を書く。その場合、`model` にはファイル名を書く。planner の `runtime` には `unknown` を使えない。

## 4. レビューファイルに書く項目（pr-review）

### 4.1 先頭の行

「対象」の行の次に、モデル識別子とランタイムを書く。

```markdown
- 対象: PR #900 / head 2222bbbb3333 / 2026-10-01
- レビュアー: claude-opus-5-5（ランタイム: claude-code）
```

`## Review history` の見出しと 3 つの列（Reviewed at (UTC)・HEAD・Mode）は変えない（9 章）。Merge recommendation は、`Request changes`・`Needs manual verification`・`Approve` のどれかをそのまま先頭に書く。

### 4.2 指摘の根拠の項目

既存の「位置」「検出時 HEAD」「内容」「根拠」の後、「推奨対応」の前に、次の順で書く。ラベルは変えない。

| ラベル | 値 | 書き方 |
| --- | --- | --- |
| `再現` | `実行した`・`実行しなかった（<理由>）`・`該当なし` | `実行した` のときは、下に `コマンド`・`場所`・`条件`・`結果` を書く。`場所` は、PR head の隔離 worktree か一時ディレクトリと、そこに何を置いたか。`結果` は、終了コードと出力の要点 |
| `利用経路` | `通常`・`限定的`・`不明`・`該当なし` | ` — ` の後に、入口・入力・環境を書く |
| `結果の種類` | `機能停止`・`誤った結果`・`安全性`・`性能`・`保守性`・`文書`・`その他` | 括弧の中に、何が止まるか、何が誤るかを書く |
| `PR 起因` | `持ち込み`・`顕在化`・`既存`・`不明` | 根拠を書く。問題のコードを入れた commit、base 側の状態（`git show <base>:<path>` や `git log` で確かめる） |
| `同種箇所` | `該当なし（<理由>）`、または 4 つの欄 | `性質`（守るべき性質を 1 文で）・`範囲`（PR の差分など）・`検索`（実行したコマンド）・`結果`（該当する箇所の一覧、または「他に無し」）。同じパターンの箇所は、すべてこの 1 件の指摘にまとめる |
| `前回の修正との関係` | `該当なし`・`前回の修正が足した仕組み`・`規則の写し`・`「同じ規則」の主張`・`不明` | 前回の修正の範囲（下記）にある変更かどうかを書く。規則の写しなら写し元と一致の確かめ方を、主張なら主張の位置と確かめた結果を書く。範囲を決められないときは `不明` とし、理由を Review coverage に書く |
| `見直しの根拠` | 自由記述 | 判断の記録で `skip` の問題を active に戻すときだけ書く。判断 ID と、新しい根拠（8.2 節）を書く |

**前回の修正の範囲**: 判断の記録の最後の巡の `reviewed_head` から現在の HEAD までの、first-parent の merge でない commit（`git log --first-parent --no-merges <reviewed_head>..HEAD`）の変更である。範囲を求める前に、`git merge-base --is-ancestor <reviewed_head> HEAD` が成功することを確かめる。`reviewed_head` を解決できない（shallow fetch で object が無いなど）とき、または現在の HEAD の祖先でない（巡の間に rebase して force push した場合など）ときは、範囲を求めず `不明` とし、理由を Review coverage に書く。force push の後も前回の HEAD の object が残って解決できる場合があるため、解決できることだけで範囲を求めない。巡の間に base を取り込んだ merge commit の変更は含めない。その巡の `outcome` が `fixes_pushed` なら、範囲に `commit` が含まれることも確かめる。

どの指摘で書くかは、次のとおり。

| ラベル | 書く指摘 | 書かなくてよい場合 |
| --- | --- | --- |
| `再現` | 実行時の挙動についての Critical・High・Medium | 文書や静的な指摘は `該当なし`。実行しなかったときは理由を書き、確度を【確認推奨】にする |
| `利用経路`・`結果の種類` | 実行時の挙動についての全指摘 | 文書の指摘は `結果の種類: 文書`、`利用経路: 該当なし` |
| `PR 起因` | 全指摘 | 確かめられないときは `不明` とし、理由を書く |
| `同種箇所` | Critical・High・Medium の欠陥 | 原因が繰り返しうるパターンでなければ `該当なし（<理由>）` |
| `前回の修正との関係` | 判断の記録に前の巡がある場合の全指摘 | 前の巡が無ければ書かない |
| `見直しの根拠` | `skip` の判断を active に戻す指摘 | それ以外は書かない |

実行時の挙動についての Critical・High・Medium を【検証済み】にするには、実行した再現が要る。コードを読んだだけの裏付けでは【確認推奨】にする（既存の【検証済み】の定義を、この種類の指摘についてだけ狭める）。

記入例（PR #900 の 2 巡目の `claude-opus-5-5.md` の M-1）。PR #900 は、週次レポートを CSV に書き出す CLI `example-report` を足す架空の PR で、5 章の表、6.5 節・7.3 節の記入例も同じ PR を使う。

<!-- review-records:example finding -->
```markdown
### M-1: 記録が 0 件の週を含む期間では、`example-report export` が週の平均の計算で止まる【検証済み】

- 位置: `tools/example_report/summary.py:41-48`、`tools/example_report/summary.py:57`
- 検出時 HEAD: `2222bbbb3333`
- 内容: 集計する期間に記録が 0 件の週があると、週の平均の計算が 0 で割り、`example-report export` が CSV を書き出す前に終了する
- 根拠: `weekly_average` は週ごとの合計をその週の記録の件数で割り、0 件の週を除かない。`export` はこの例外を捕まえない
- 再現: 実行した
  - コマンド: `example-report export --records "$TMP/records.jsonl" --since 2026-08-03 --until 2026-08-30 --out "$TMP/weekly.csv"`
  - 場所: PR head の隔離 worktree。記録のファイル `records.jsonl` を一時ディレクトリ `$TMP` に置いた（8 月 10〜16 日の週の記録は 0 件）
  - 条件: README の手順どおりに PR head の CLI を入れて実行した（追加の設定なし）
  - 結果: `ZeroDivisionError: division by zero` で終了コード 1。`weekly.csv` は作られない
- 利用経路: 通常 — 入口は文書化された `example-report export`、入力は記録の無い週（休暇など）を含む期間、環境は README の手順どおりの実行環境
- 結果の種類: 機能停止（記録の無い週を含む期間では、レポートを書き出せない）
- PR 起因: 持ち込み（`1a1a1a1` が週の平均の列を足した。base `0f0f0f0` に該当するコードは無い）
- 同種箇所: 性質「集計の値は、記録が 0 件の週でも計算できる」／範囲 PR の差分／検索 `rg -n '/ len\(' tools/example_report`／結果 `summary.py:41-48`（週の平均）、`summary.py:57`（達成率）
- 前回の修正との関係: 該当なし（`1111aaa..2222bbb` の first-parent の commit は、該当する箇所に触れていない）
- 推奨対応: 記録が 0 件の週は、週の平均と達成率を空欄にする。0 件の週と通常の週を含む期間で CLI を実行し、生成した CSV を確認する。この例は損害が残る前に例外で停止するため、AGENTS.md「テストの採用基準」に従い常設テストは追加しない（D1 で修正する判断とは別）
```

### 4.3 Unverified candidates

各候補に `U-<n>` の ID と検出時 HEAD を付け、見出しを `### U-1: <一行の疑い>` の形にする。既存の欄（位置・具体的な疑い・確認済みの根拠・不足している根拠・次の確認方法）は保つ。

### 4.4 Decided findings

判断の記録で `skip` になっている問題のうち、新しい根拠が無いものを、`## Pending previous findings` の後の `## Decided findings` に書く。件数表・重大度の判定・Merge recommendation には数えない。

```markdown
## Decided findings

### M-2: 集計が、取り消し済みの記録も件数に数える

- 判断: `Grok-4.7#M-2@1111aaaa2222`（対応しない。1 巡目、planner claude-opus-5-5）
- 位置: `tools/example_report/summary.py:18`
- 検出時 HEAD: `1111aaaa2222`
- 新しい根拠: なし（見直す条件: 取り消し済みの記録の数え方が仕様で決まったとき、または利用者が指定したとき）
```

- Update で、active だった自分の指摘が `skip` になっていて新しい根拠が無ければ、ID・タイトル・検出時 HEAD を保ったまま、この節へ移す。
- Initial の席は、自分の候補が `skip` の判断と同じ問題で新しい根拠が無ければ、新しい ID を付けてこの節に書く。
- 新しい ID は、active・Pending previous findings・Resolved findings・Unverified candidates・Decided findings のすべてと衝突させない。

### 4.5 実行した確認

`## Review coverage` に、実行して確かめたことを、入口ごとに 1 行で書く。「安全か」と「動くか」を分けて書くためであり、planner が「必要な検証が終わっているか」を判断する材料にもなる。

```markdown
- 実行した確認:
  - `example-report export`: 記録が 0 件の週を含む期間 → 止まる（`ZeroDivisionError`、終了コード 1。M-1）
  - `example-report export --out`: 既存のファイルを指定した（`--force` なし） → 止まる（終了コード 2。既存のファイルは変わらない。意図どおりの拒否）
  - `example-report export`: 日本語の項目名を含む記録 → 動く（UTF-8 の CSV を書き出す）
```

### 4.6 PR 修正ループのレビュアー

loop のレビュアーは、通常の pr-review（非対話の prepare → complete）を実行し、この章の項目をほかのレビュアーと同じ形で書く。loop 専用の入力形式は無い。旧 loop（herdr-pr-review の `--loop`。#571 で削除）のレビュアーは、根拠の行を Markdown の指摘と同じ形のまま sidecar の `content` に入れ、`id` に Markdown の指摘 ID と同じ値を使っていた。

## 5. D1: 重大度の表記にかかわらず Fix にする指摘

利用者が 2026-10-01 に決めた D1（案 B。[#493 のコメント](https://github.com/usaturn/maatlog-poc/issues/493#issuecomment-5926373389)）を、次の 4 つの条件で判定する。4 つすべてを満たす指摘は、重大度の表記にかかわらず Fix にする。満たすかどうかは、planner がコミット済みの現行 HEAD で確かめる。

1. **再現の記録がある。** PR のコミット済みの head で実行した再現について、コマンド・場所・条件・結果が残っている。記録は、レビュアーのもの（レビューファイルの `再現`）でも、planner のもの（判断の記録の `d1.reproduction`）でも、review-verify の確認担当のもの（確認済みの一覧の再現を、判断の記録の `d1.reproduction` に写したもの）でもよい。推論を再現として書かない。
2. **利用経路が通常である。** 入口・入力・環境のすべてが、普段の利用で起きる範囲にある。
   - 入口: 文書化された操作（CLI、スキルの手順、またはそれらから呼ばれる関数）。入口からは渡せない値で内部の関数を直接呼ぶ再現は含めない。
   - 入力: 普段の利用で生じる入力。例: エージェントや利用者が普段書く形の文章、サポートしているリポジトリの PR。
   - 環境: サポートしている環境をそのまま使う。その環境が実際に取る状態（作成した直後の作業ディレクトリ、submodule が取りうる状態、文書化された起動方法が設定する環境変数など）は含める。その環境に無い設定（利用者が個別に足した Git の設定や環境変数、手で書き換えた PATH など）は含めない。
3. **結果の種類が「機能停止」か「誤った結果」である。** 完了すべき操作が失敗する・止まる・正しい入力を拒否する、または、完了しても誤った対象・記録・結果になる。通知が遅れるだけで最終結果は正しいもの、遅いだけのもの、ログが多いだけのものは含めない。
4. **PR 起因である。** `PR 起因` が `持ち込み` か `顕在化` である。

| 指摘（PR #900） | 判定 | 理由 |
| --- | --- | --- |
| `claude-opus-5-5#M-1@2222bbbb3333`（Medium。記録が 0 件の週を含む期間で、書き出しが止まる） | 満たす | PR head の隔離 worktree で実行して再現した（`ZeroDivisionError`、終了コード 1）。入口は文書化された `example-report export`、入力は記録の無い週を含む期間、環境は README の手順どおりの実行環境である。レポートを書き出せない機能停止で、`1a1a1a1` が持ち込んだ |
| `Grok-4.7#L-1@1111aaaa2222/b`（利用者が `PYTHONIOENCODING=ascii` を設定していると、完了メッセージの表示で止まる） | 満たさない | 標準の環境に無い設定が前提である（README の手順では設定しない） |
| `claude-opus-5-5#U-1@2222bbbb3333`（集計中に記録のファイルが更新されると、週の合計が食い違いうる） | 満たさない | 実行した再現が無い |
| `SWE-2-Max#planner-1@1111aaaa2222`（完了メッセージが、CSV を閉じる前に表示される） | 満たさない | 書き出したファイルと終了コードは変わらない |

判断の記録では、D1 を適用した判断に `d1` を書く（6.2 節）。`d1` は、`state` が `fix` で、`pr_relation` が `introduced` か `surfaced` の判断にだけ書ける。D1 を満たさない理由は `reason` に書く。6.5 節の記入例（`Grok-4.7#L-1@1111aaaa2222/b`・`SWE-2-Max#planner-1@1111aaaa2222`）と、7.3 節の記入例（`claude-opus-5-5#M-1@2222bbbb3333`・`claude-opus-5-5#U-1@2222bbbb3333`）を参照する。

## 6. 判断の記録

1 つの巡で 1 人の planner が判断した内容を、1 つの JSON オブジェクト（巡の記録）にする。未知のキーは拒否する。

### 6.1 どの指摘を書くか

`decisions` には、次の指摘をすべて、どれかの判断の `sources` に含めて書く。引き継いだ判断も省かない。

- この巡の入力にある指摘。`rerun` が `true` の席の、active の節（Critical〜Info）・Pending previous findings・Unverified candidates にある指摘と、古い席の未判断の指摘
- 前の巡で `fix` だった判断。この巡で、`resolved` にするか、引き継ぐかを判断する（8.4 節）

Decided findings にある指摘は、この巡の入力に含めない（判断済みのまま続く）。それ以外の前の判断（この巡で再び挙がらなかったもの）も、書かなくてよい。集約（8.1 節）では、最後に記録した状態が続く。

### 6.2 フィールド

v2 は `planner.provenance` と全 `seats[].provenance` を必須とする。agent_id / role / provider / effort / session_id / cli_version / run_id / request_id / skills の全キー、unknown の扱い、スキルの path / SHA-256 は [review-findings 3.1節](review-findings.md#31-v2-の実行識別情報provenance) と同じ。planner の既存 agent_id はモデルから作るコメントマーカーであり、provenance.agent_id（runner の実担当または self）とは別物。model/runtime と既存の採否・停止の意味は変えない。

v2 の識別情報は render → extract → fold の間で保持する。fold の rounds に元の planner / seats を残す。v1/v2 混在の集約は v2 を返す。輸送用の HTML ブロックの v1 マーカーは維持し、中の JSON の schema で版を区別する。既存の記入例は架空の v1 として残す。

最上位:

| キー | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `schema` | 新規 `"review-records/v2"`、既存 `"review-records/v1"` | 必須 | 形式の版。v1 の読取り・検査を維持し、知らない版は拒否する |
| `pr` | `{"repository": "owner/name", "number": 整数}` | 必須 | PR が存在するリポジトリと、PR の番号（1 以上） |
| `round` | 整数（1 以上） | 必須 | 巡の番号。記録を持つ巡の通し番号で、前の巡の最大の番号 + 1 |
| `reviewed_head` | 40 桁か 64 桁の 16 進 | 必須 | この巡でレビューと判断の対象にした HEAD |
| `recorded_at` | `YYYY-MM-DDTHH:MM:SSZ` | 必須 | 記録した UTC 時刻 |
| `planner` | オブジェクト | 必須 | `model`（モデル識別子）・`runtime`（`unknown` は使えない）・`agent_id`（3 章の規則で `model` から作った値） |
| `seats` | 配列 | 必須 | 席構成。`outcome` が `stopped` または `stopped_` で始まる巡を除き、1 件以上 |
| `decisions` | 配列 | 必須 | 判断（6.1 節）。指摘が 1 件も無い巡では空 |
| `outcome` | 列挙 | 必須 | 巡の結果 |
| `commit` | 40 桁か 64 桁の 16 進 | `outcome` が `fixes_pushed` のとき必須（`reviewed_head` と違う値）。それ以外では書かない | push した commit |
| `stop_reason` | 自由記述 | `outcome` が `stopped` または `stopped_` で始まるとき必須。それ以外では書かない | `stopped` は `permission: <具体的障害>`・`environment: <具体的障害>`・`external: <具体的障害>`。旧 `stopped_*` は従来の自由記述 |

`seats[]`:

| キー | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `review_file` | レビューファイル名（`.md` なし） | 必須 | 席ごとに一意 |
| `model` | 1 行の文字列 | 必須 | レビューファイルの `レビュアー` の行のモデル識別子。旧形式はファイル名 |
| `runtime` | `[a-z0-9][a-z0-9._-]*` | 必須 | `unknown` は旧形式・様式外だけ。そのときは `note` が必須 |
| `file_head` | 12 桁の 16 進か `unknown` | 必須 | Review history の最後の行の HEAD。Review history の無い様式外のファイルは、ファイルが示す対象 HEAD |
| `mode` | `Initial`・`Update` | 必須 | 同じ行の Mode。Review history の無いファイルは `Initial` |
| `rerun` | 真偽値 | 必須 | この巡で再実行したか。`file_head` が `reviewed_head` の先頭 12 桁と同じときだけ `true` |
| `merge_recommendation` | `Request changes`・`Needs manual verification`・`Approve`・`null` | 必須 | レビューファイルの Merge recommendation。無いとき、または 3 つの値のどれでもないときは `null` にし、原文を `note` に書く（意味で読み替えない） |
| `note` | 自由記述 | 任意（`runtime` が `unknown` なら必須） | 備考 |

`outcome` が `stopped` または `stopped_` で始まる巡を除き、`rerun` が `true` の席が 1 つ以上必要である。

`decisions[]`:

| キー | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `id` | 判断 ID | 必須 | 巡の中で一意 |
| `sources` | 指摘の参照の配列（1 件以上、重複なし） | 必須 | この判断にまとめた指摘。`id` の参照の部分を含む |
| `summary` | 1 行の文字列 | 必須 | 位置と発生条件が分かる要約 |
| `locations` | 1 行の文字列の配列（1 件以上） | 必須 | `path:line` など。その巡の `reviewed_head` での位置 |
| `state` | `fix`・`hold`・`skip`・`resolved`・`unverified` | 必須 | 対応する・判断保留・対応しない・解消済み・検証未完了 |
| `change` | `new`・`inherited`・`changed` | 必須 | 新規・前の判断を引き継いだ・前の判断を変えた |
| `previous_state` | `state` と同じ列挙 | `change` が `changed` のとき必須（`state` と違う値）。それ以外では書かない | 前の状態 |
| `new_evidence` | 自由記述 | `previous_state` が `skip` のとき必須。それ以外では書かない | 見直した根拠（8.2 節） |
| `severity` | `Critical`・`High`・`Medium`・`Low`・`Info`・`null` | 必須 | 判断に使った重大度。`null` にできるのは、`sources` がすべて Unverified candidates（`U-<n>`）の判断だけ |
| `pr_relation` | `introduced`・`surfaced`・`preexisting`・`unknown` | 必須 | 持ち込み・顕在化・既存・不明 |
| `reason` | 自由記述 | 必須 | 判断の理由。Fix は採用の基準を書く |
| `revisit_when` | 自由記述 | `state` が `skip` のとき必須。それ以外では書かない | 見直す条件 |
| `unverified_reason` | 自由記述 | `state` が `unverified` のとき必須。それ以外では書かない | 足りない確認 |
| `d1` | オブジェクト | 任意（`state` が `fix` で、`pr_relation` が `introduced` か `surfaced` のときだけ） | `reproduced_by`（この判断の `sources` にある参照、`planner`、または `verifier`）・`usage_path`（入口・入力・環境）・`result_kind`（`stop` か `wrong_result`）。`reproduced_by` が `planner` か `verifier` なら、`reproduction`（`commands`（1 件以上）・`place`・`conditions`・`result`）が必須。`verifier` は review-verify の確認担当で、`reproduction` には確認済みの一覧（[review-findings](review-findings.md)）の再現を写す。判断の記録にはローカルのパスを含めないので、一覧のファイルは参照しない |
| `carry_over` | オブジェクト | 任意（`state` が `skip` のときだけ） | `issue`（`#<番号>` か Issue の URL）か、`candidate`（別 Issue の候補の一行）のどちらか一方 |

v1 の記録に無い項目（#485 が Spec に残す「変更の種類」「比較に使った base 側のコミット」など）は、planner の Spec に書き、必要なら `reason` にも書く。

`outcome`: 新しい review-fix-planning の終点は `fixes_pushed`・`no_fix_needed`・`stopped`。旧 `awaiting_approval`・`stopped_*` の記録は従来の整合条件で読み、変換しない。マージ・Issue更新等の実行完了はスキルの最終化結果で確認し、この記録の採否だけで実施済みと判断しない。

| 値 | 表示 | 整合の規則 |
| --- | --- | --- |
| `awaiting_approval` | 承認待ち | `fix` か `hold` が 1 件以上 |
| `fixes_pushed` | 修正を push | `commit` が必須で、`fix` が 1 件以上 |
| `no_fix_needed` | 追加修正なしで終了 | この巡の判断に `fix`・`hold`・`unverified` が無い。前の巡の判断を重ねても残らない（6.4 節） |
| `stopped` | 具体的な障害による未完了 | `stop_reason` が必須。`permission`・`environment`・`external` の直後に `:`、空でない具体的な理由を書く |
| `stopped_insufficient_verification` | 確認不足で停止 | `stop_reason` が必須 |
| `stopped_iteration_limit` | 上限で停止 | `stop_reason` が必須 |
| `stopped_no_progress` | 進捗なしで停止 | `stop_reason` が必須 |
| `stopped_other` | その他の停止 | `stop_reason` が必須 |

### 6.3 文字列の規則

- 1 行の欄（`summary`、`locations`、識別子、モデル識別子など）: 空白だけにしない。制御文字を含めない。
- 自由記述の欄（`reason`、`revisit_when`、`new_evidence`、`unverified_reason`、`stop_reason`、`note`、`d1` の中の文字列、`carry_over.candidate`）: 空白だけにしない。改行・タブ以外の制御文字を含めない。複数行の理由は受け付ける。
- どの文字列にも `<!--` と `-->` を含めない。コメントのマーカーを壊さないためである。
- 描画した記録は、取り出すと同じ値に戻らなければならない。`render_block` がこれを確かめる。

### 6.4 検査

記録は、投稿の前に `review_records.py` で検査する。helper（7.4 節）は、投稿の前に次の 2 つを実行する。

**記録単体の検査**（`validate_record`）: 6.2 節と 6.3 節の規則、判断 ID の重複、判断 ID の参照の部分が `sources` にあること、部分どうし以外での参照の共有が無いこと、席の `review_file` の重複、`rerun` と HEAD の一致、`outcome` との整合。

**前の状態との照合**（`check_against`）: 前の状態は、更新しようとしているコメントを除いた、ほかのすべての記録を集約したものである。

- 巡の番号: 同じ番号の巡がほかに記録されていれば拒否する（1 つの巡は 1 人の planner だけが記録する）。番号の大きい巡があれば拒否する（更新できるのは最新の巡だけ）。それ以外は、前の巡の最大の番号 + 1（記録が無ければ 1）であること。
- `pr` が前の記録と同じであること。
- `new` の判断は、前の状態に無いこと。基にした参照も、前の状態のどの判断の `sources` にも無いこと。
- `inherited` の判断は、前の状態にあり、`state` が同じで、前の状態に `d1` があれば同じ `d1` を持つこと。
- `changed` の判断は、前の状態にあり、前の `state` が `previous_state` と同じであること。
- この巡の判断の `sources` にある参照が、前の状態で別の判断（同じ参照を基にした部分を除く）に属していないこと。
- 前の状態で `state` が `fix` の判断が、すべてこの巡の `decisions` にあること（`outcome` が `stopped` または `stopped_` で始まる巡を除く）。
- `outcome` が `no_fix_needed` のとき、前の状態にこの巡の判断を重ねた結果に、`fix`・`hold`・`unverified` が残っていないこと。

手で確かめるときは、次のように実行する。一時ファイルの置き場所は、各スキルの規則に従う。

```bash
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" validate --record-file "$ROUND_JSON"
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" > "$COMMENTS_JSON"
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" validate \
  --record-file "$ROUND_JSON" --comments-file "$COMMENTS_JSON"
```

更新しようとしているコメントがあるときは、2 つ目の `validate` に `--exclude-comment <そのコメントの ID>` を付ける。終了コードは、0 が成功、65 が内容の問題、66 がファイルを読めない場合、2 が引数の誤りである。問題は、標準エラー出力に場所付きで 1 行ずつ出る。

### 6.5 記入例（PR #900 の 1 巡目）

値は架空の PR #900 のもの（4.2 節）である。この巡の入力にある指摘は、記入例の判断ですべてである（6.1 節）。

- SWE-2-Max のレビューファイルは様式外で、指摘 ID が無い。planner が `planner-1` を付けた。2 巡目には、レビュアーが同じ指摘に `I-1` を付けている（7.3 節）。
- qwen3.8-max の I-1 は、SWE-2-Max の指摘と同じ問題なので、同じ判断の `sources` にまとめた。

<!-- review-records:example round-1 -->
```json
{
  "schema": "review-records/v1",
  "pr": {
    "repository": "usaturn/xrift-poc",
    "number": 900
  },
  "round": 1,
  "reviewed_head": "1111aaaa2222bbbb3333cccc4444dddd5555eeee",
  "recorded_at": "2026-10-01T09:00:00Z",
  "planner": {
    "model": "claude-opus-5-5",
    "runtime": "claude-code",
    "agent_id": "claude-opus-5-5"
  },
  "seats": [
    {
      "review_file": "Grok-4.7",
      "model": "Grok-4.7",
      "runtime": "unknown",
      "file_head": "1111aaaa2222",
      "mode": "Initial",
      "rerun": true,
      "merge_recommendation": "Needs manual verification",
      "note": "レビュアーの行が無い旧形式のため、ランタイムは不明"
    },
    {
      "review_file": "SWE-2-Max",
      "model": "SWE-2-Max",
      "runtime": "unknown",
      "file_head": "1111aaaa2222",
      "mode": "Initial",
      "rerun": true,
      "merge_recommendation": null,
      "note": "様式外のファイル（Review history と指摘 ID が無い）。file_head は「Reviewed HEAD」の行から取り、指摘 ID は planner が planner-<n> を付けた。Merge recommendation の記載は「マージ推奨」で、3 つの値に当たらない。ランタイムは不明"
    },
    {
      "review_file": "qwen3.8-max",
      "model": "qwen3.8-max",
      "runtime": "unknown",
      "file_head": "1111aaaa2222",
      "mode": "Initial",
      "rerun": true,
      "merge_recommendation": "Approve",
      "note": "レビュアーの行が無い旧形式のため、ランタイムは不明"
    }
  ],
  "decisions": [
    {
      "id": "Grok-4.7#H-1@1111aaaa2222",
      "sources": [
        "Grok-4.7#H-1@1111aaaa2222"
      ],
      "summary": "--out に既存のファイルを渡すと、確認なしに上書きして前のレポートを失う",
      "locations": [
        "tools/example_report/export.py:22",
        "tools/example_report/cli.py:61"
      ],
      "state": "fix",
      "change": "new",
      "severity": "High",
      "pr_relation": "introduced",
      "reason": "H: PR が持ち込んだ High で、現行 HEAD で既存の weekly.csv が確認なしに上書きされることを再現した。既存のファイルがあるときは、--force が無ければ書き出す前に止める"
    },
    {
      "id": "Grok-4.7#M-2@1111aaaa2222",
      "sources": [
        "Grok-4.7#M-2@1111aaaa2222"
      ],
      "summary": "集計が、取り消し済み（status が cancelled）の記録も件数に数える",
      "locations": [
        "tools/example_report/summary.py:18"
      ],
      "state": "skip",
      "change": "new",
      "severity": "Medium",
      "pr_relation": "introduced",
      "reason": "取り消し済みの記録を数えるかは仕様に定めが無く、設計判断が要る。この PR の欠陥ではない",
      "revisit_when": "取り消し済みの記録の数え方が仕様で決まったとき、または利用者が指定したとき"
    },
    {
      "id": "Grok-4.7#L-1@1111aaaa2222/a",
      "sources": [
        "Grok-4.7#L-1@1111aaaa2222"
      ],
      "summary": "環境変数 TZ が未設定だと、週の境界がずれる",
      "locations": [
        "tools/example_report/summary.py:9"
      ],
      "state": "resolved",
      "change": "new",
      "severity": "Low",
      "pr_relation": "introduced",
      "reason": "TZ を外した環境で実行しても、週の境界は設定ファイルのタイムゾーンで決まった（PR head の隔離 worktree で確認）"
    },
    {
      "id": "Grok-4.7#L-1@1111aaaa2222/b",
      "sources": [
        "Grok-4.7#L-1@1111aaaa2222"
      ],
      "summary": "利用者が PYTHONIOENCODING=ascii を設定していると、完了メッセージの表示で止まる",
      "locations": [
        "tools/example_report/cli.py:90"
      ],
      "state": "skip",
      "change": "new",
      "severity": "Low",
      "pr_relation": "introduced",
      "reason": "利用者自身の設定が前提で、標準の環境に無い設定なので D1 の対象外。CSV は UTF-8 を指定して書き出しており、影響は完了メッセージの表示だけである",
      "revisit_when": "標準の環境で同じ問題が起きる再現が出たとき、または利用者が指定したとき"
    },
    {
      "id": "SWE-2-Max#planner-1@1111aaaa2222",
      "sources": [
        "SWE-2-Max#planner-1@1111aaaa2222",
        "qwen3.8-max#I-1@1111aaaa2222"
      ],
      "summary": "完了メッセージが、CSV を閉じる前に表示される",
      "locations": [
        "tools/example_report/cli.py:88-96",
        "tools/example_report/export.py:40-52"
      ],
      "state": "skip",
      "change": "new",
      "severity": "Info",
      "pr_relation": "introduced",
      "reason": "表示の順が前後するだけで、書き出したファイルと終了コードは変わらない。結果の種類が機能停止・誤った結果に当たらないので D1 の対象外。qwen3.8-max の I-1 は同じ指摘",
      "revisit_when": "書き出したファイルか終了コードが変わる再現が出たとき、または利用者が指定したとき"
    },
    {
      "id": "qwen3.8-max#L-1@1111aaaa2222",
      "sources": [
        "qwen3.8-max#L-1@1111aaaa2222"
      ],
      "summary": "期間の各週を 2 回集計していて、書き出しが遅い",
      "locations": [
        "tools/example_report/summary.py:30"
      ],
      "state": "skip",
      "change": "new",
      "severity": "Low",
      "pr_relation": "introduced",
      "reason": "書き出しの時間が増えるだけで、結果の正しさは変わらない（効率の問題）",
      "revisit_when": "重複した集計が結果を変える再現が出たとき、または利用者が指定したとき"
    }
  ],
  "outcome": "fixes_pushed",
  "commit": "2222bbbb3333cccc4444dddd5555eeee6666ffff"
}
```

## 7. 状況コメント

### 7.1 構造

planner は、巡ごとに新しい状況コメントを作る。前の巡のコメントは編集しない。同じ巡の中だけ、同じコメントを更新する。例えば、計画だけを投稿した後、承認・push の後に更新する。1 つの巡は 1 人の planner だけが記録する。

- 1 行目は、エージェントのマーカー `<!-- review-fix-planning:agent:<agent_id> -->`（従来どおり）。
- 2 行目は、巡のマーカー `<!-- review-fix-planning:round:<巡の番号>:<reviewed_head の全桁> -->`。
- 続けて、見出し `## review-fix-planning（<モデル識別子>）`、対象 PR の行、3 つの見出し（レビュー状況・作業判断・作業結果）を、この順で書く。
- 末尾に、空行を挟んで記録のブロックを置く。ブロックは、`<details><summary>判断の記録（review-records v1）</summary>`、空行、`<!-- review-records:v1:start -->`、` ```json `、記録の JSON（`json.dumps(記録, ensure_ascii=False, indent=2)`）、` ``` `、`<!-- review-records:v1:end -->`、空行、`</details>` の順に並べたもので、`render_block` が作る。
- 1・2 行目のマーカーと記録のブロックは helper が付ける（`compose_comment`）。planner が書くのは、見出しから作業結果までの本文である。
- 人が読む本文は、記録と食い違ってはならない。次の巡のレビュアーと planner が読むのは、記録である。
- 状況コメントは人が編集しない。補足は別のコメントに書く。編集すると記録が読まれなくなるか、helper の次の更新で上書きされる。
- 状況コメントや、この文書の記入例を、コードブロックに入れずに別のコメントへ貼らない。書き込み権限のある人のコメントは記録として読まれ、同じ巡の記録が 2 件になるか、ほかの PR の記録として集約が失敗する。

### 7.2 3 つの見出しに書く内容

| 見出し | 書く内容 |
| --- | --- |
| レビュー状況 | 巡の番号とレビュー対象 HEAD。席構成の表（レビューファイル・モデル・ランタイム・レビュー HEAD・Mode・この巡で再実行・Merge recommendation）。入力から外したファイルと理由。前の巡の判断の件数。分類の件数（5 つの状態と、そのうち引き継いだ件数）。利用者の指定で読まなかったコメントがあれば、その ID |
| 作業判断 | 対応する（判断 ID・基準・一行）。D1 を適用したものは「D1」と明記する。判断保留。対応しない（全件。判断 ID・一行の理由・見直す条件。引き継いだものは「（引き継ぎ）」と書く）。解消済み（件数と代表的な根拠）。検証未完了（件数と足りない確認）。同種箇所の確認（Fix ごとの範囲と結果） |
| 作業結果 | このターン（計画のみ・push 済み・途中停止）、Spec のパス、commit・push、巡の結果（`outcome` の表示）と理由、次 |

判断 ID は、`@<検出時 HEAD>` まで含めて全体を書く。

### 7.3 記入例（PR #900 の 2 巡目）

6.5 節の 1 巡目に続く巡である。この巡の入力にある指摘は、記入例の判断ですべてである（6.1 節）。旧形式の Grok-4.7 は Decided findings（4.4 節）を使わず、1 巡目に対応しないとした M-2 を active のまま挙げている。そのため、M-2 はこの巡の入力に含まれ、引き継ぎとして記録した。

<!-- review-records:example status-comment-round-2 -->
````markdown
<!-- review-fix-planning:agent:claude-opus-5-5 -->
<!-- review-fix-planning:round:2:2222bbbb3333cccc4444dddd5555eeee6666ffff -->
## review-fix-planning（claude-opus-5-5）

対象 PR: #900（`feat/weekly-report-export`） / 巡: 2 / レビュー対象 HEAD: `2222bbbb3333` / 現行 HEAD: `3333cccc4444`

### レビュー状況

席構成:

| レビューファイル | モデル | ランタイム | レビュー HEAD | Mode | この巡で再実行 | Merge recommendation |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-opus-5-5.md` | claude-opus-5-5 | claude-code | `2222bbbb3333` | Initial | はい | Needs manual verification |
| `Grok-4.7.md` | Grok-4.7 | unknown（旧形式） | `2222bbbb3333` | Update | はい | Approve |
| `SWE-2-Max.md` | SWE-2-Max | unknown（旧形式） | `2222bbbb3333` | Update | はい | Approve |
| `qwen3.8-max.md` | qwen3.8-max | unknown（旧形式） | `1111aaaa2222` | Initial | いいえ | Approve |

- 入力から外したファイル: `qwen3.8-max.md`（この巡で再実行されていない。指摘 2 件は 1 巡目の判断で扱い済み）
- 前の巡の判断: 6 件（1 巡目）
- 分類: 解消済み 1 / 対応する 1 / 判断保留 0 / 対応しない 3 / 検証未完了 0（計 5。うち引き継ぎ 2）

### 作業判断

- 対応する:
  - `claude-opus-5-5#M-1@2222bbbb3333` — D1 — 記録が 0 件の週を含む期間で、週の平均の計算が 0 で割り、書き出しが止まる。記録が 0 件の週は週の平均と達成率を空欄にし、0 件の週と通常の週を含む期間で CLI と生成 CSV を確認する。損害が残る前に例外で停止する例なので、採用基準に従い常設テストは追加しない
- 判断保留: なし
- 対応しない:
  - `Grok-4.7#M-2@1111aaaa2222`（引き継ぎ） — 取り消し済みの記録を数えるかは設計判断。見直す条件: 取り消し済みの記録の数え方が仕様で決まったとき、または利用者が指定したとき
  - `SWE-2-Max#planner-1@1111aaaa2222`（引き継ぎ。SWE-2-Max の I-1 と qwen3.8-max の I-1 も同じ指摘） — 表示の順が前後するだけで、書き出したファイルと終了コードは変わらない。見直す条件: 書き出したファイルか終了コードが変わる再現が出たとき、または利用者が指定したとき
  - `claude-opus-5-5#U-1@2222bbbb3333` — 未再現で、設計判断が要る。見直す条件: 集計中の更新で週の合計が食い違う再現が示されたとき、または利用者が指定したとき
- 解消済み: 1 件（`Grok-4.7#H-1@1111aaaa2222` は、2222bbb の `_open_output` で解消した）
- 検証未完了: なし
- 同種箇所の確認: `claude-opus-5-5#M-1@2222bbbb3333` — 範囲は PR の差分。`rg -n '/ len\(' tools/example_report` で、週の平均と達成率の 2 か所を確かめた。ほかには無い

### 作業結果

- このターン: push 済み
- Spec: `docs_draft/superpowers/2026-10-01-pr-900-round2-review-fixes-design.md`（gitignore 対象で、ローカルにしか無い）
- commit / push: `3333ccc`
- 巡の結果: 修正を push（次の巡のレビューで確かめる）
- 次: 次の巡のレビュー

<details><summary>判断の記録（review-records v1）</summary>

<!-- review-records:v1:start -->
```json
{
  "schema": "review-records/v1",
  "pr": {
    "repository": "usaturn/xrift-poc",
    "number": 900
  },
  "round": 2,
  "reviewed_head": "2222bbbb3333cccc4444dddd5555eeee6666ffff",
  "recorded_at": "2026-10-01T11:30:00Z",
  "planner": {
    "model": "claude-opus-5-5",
    "runtime": "claude-code",
    "agent_id": "claude-opus-5-5"
  },
  "seats": [
    {
      "review_file": "claude-opus-5-5",
      "model": "claude-opus-5-5",
      "runtime": "claude-code",
      "file_head": "2222bbbb3333",
      "mode": "Initial",
      "rerun": true,
      "merge_recommendation": "Needs manual verification"
    },
    {
      "review_file": "Grok-4.7",
      "model": "Grok-4.7",
      "runtime": "unknown",
      "file_head": "2222bbbb3333",
      "mode": "Update",
      "rerun": true,
      "merge_recommendation": "Approve",
      "note": "レビュアーの行が無い旧形式のため、ランタイムは不明"
    },
    {
      "review_file": "SWE-2-Max",
      "model": "SWE-2-Max",
      "runtime": "unknown",
      "file_head": "2222bbbb3333",
      "mode": "Update",
      "rerun": true,
      "merge_recommendation": "Approve",
      "note": "レビュアーの行が無い旧形式のため、ランタイムは不明"
    },
    {
      "review_file": "qwen3.8-max",
      "model": "qwen3.8-max",
      "runtime": "unknown",
      "file_head": "1111aaaa2222",
      "mode": "Initial",
      "rerun": false,
      "merge_recommendation": "Approve",
      "note": "この巡で再実行されていない。レビュアーの行が無い旧形式のため、ランタイムは不明"
    }
  ],
  "decisions": [
    {
      "id": "claude-opus-5-5#M-1@2222bbbb3333",
      "sources": [
        "claude-opus-5-5#M-1@2222bbbb3333"
      ],
      "summary": "記録が 0 件の週を含む期間では、example-report export が週の平均の計算で止まる",
      "locations": [
        "tools/example_report/summary.py:41-48",
        "tools/example_report/summary.py:57"
      ],
      "state": "fix",
      "change": "new",
      "severity": "Medium",
      "pr_relation": "introduced",
      "reason": "D1: 通常の利用経路で、記録の無い週を含む期間のレポートを書き出せないことを再現で確かめた。記録が 0 件の週は、週の平均と達成率を空欄にする",
      "d1": {
        "reproduced_by": "claude-opus-5-5#M-1@2222bbbb3333",
        "usage_path": "文書化された example-report export の書き出し。README の手順どおりの環境で、記録の無い週（休暇など）を含む期間を指定する",
        "result_kind": "stop"
      }
    },
    {
      "id": "Grok-4.7#H-1@1111aaaa2222",
      "sources": [
        "Grok-4.7#H-1@1111aaaa2222"
      ],
      "summary": "--out に既存のファイルを渡すと、確認なしに上書きして前のレポートを失う",
      "locations": [
        "tools/example_report/export.py:22-31"
      ],
      "state": "resolved",
      "change": "changed",
      "previous_state": "fix",
      "severity": "High",
      "pr_relation": "introduced",
      "reason": "2222bbb の _open_output が、既存のファイルがあると --force なしでは終了コード 2 で止まり、ファイルを変えないことを確かめた"
    },
    {
      "id": "Grok-4.7#M-2@1111aaaa2222",
      "sources": [
        "Grok-4.7#M-2@1111aaaa2222"
      ],
      "summary": "集計が、取り消し済み（status が cancelled）の記録も件数に数える",
      "locations": [
        "tools/example_report/summary.py:18"
      ],
      "state": "skip",
      "change": "inherited",
      "severity": "Medium",
      "pr_relation": "introduced",
      "reason": "取り消し済みの記録を数えるかは仕様に定めが無く、設計判断が要る。この PR の欠陥ではない",
      "revisit_when": "取り消し済みの記録の数え方が仕様で決まったとき、または利用者が指定したとき"
    },
    {
      "id": "SWE-2-Max#planner-1@1111aaaa2222",
      "sources": [
        "SWE-2-Max#planner-1@1111aaaa2222",
        "qwen3.8-max#I-1@1111aaaa2222",
        "SWE-2-Max#I-1@1111aaaa2222"
      ],
      "summary": "完了メッセージが、CSV を閉じる前に表示される",
      "locations": [
        "tools/example_report/cli.py:88-96",
        "tools/example_report/export.py:40-52"
      ],
      "state": "skip",
      "change": "inherited",
      "severity": "Info",
      "pr_relation": "introduced",
      "reason": "表示の順が前後するだけで、書き出したファイルと終了コードは変わらない。結果の種類が機能停止・誤った結果に当たらないので D1 の対象外。qwen3.8-max の I-1 は同じ指摘",
      "revisit_when": "書き出したファイルか終了コードが変わる再現が出たとき、または利用者が指定したとき"
    },
    {
      "id": "claude-opus-5-5#U-1@2222bbbb3333",
      "sources": [
        "claude-opus-5-5#U-1@2222bbbb3333"
      ],
      "summary": "集計中に記録のファイルが更新されると、週の合計が食い違いうる",
      "locations": [
        "tools/example_report/export.py:20-38",
        "tools/example_report/summary.py:12-30"
      ],
      "state": "skip",
      "change": "new",
      "severity": null,
      "pr_relation": "introduced",
      "reason": "未再現。集計中の更新を扱う仕様は現行に無く、設計判断が要る。実行した再現が無いので D1 の対象外",
      "revisit_when": "集計中の更新で週の合計が食い違う再現が示されたとき、または利用者が指定したとき"
    }
  ],
  "outcome": "fixes_pushed",
  "commit": "3333cccc4444dddd5555eeee6666ffff77770000"
}
```
<!-- review-records:v1:end -->

</details>
````

### 7.4 helper の規則

`upsert_pr_comment.py` は、記録付きの投稿と記録なしの投稿を持つ。どちらでも、照合の対象にするのは、書き込み権限のある投稿者（`author_association` が `OWNER`・`MEMBER`・`COLLABORATOR`）のコメントだけである。マーカーは、行全体が一致するかで比べる（部分一致にしない）。

**記録付きの投稿**（`--records-file <巡の記録の JSON>`）: review-fix-planning は、この投稿だけを使う。

1. 記録を検査する（6.4 節の記録単体の検査）。`planner.model` が `--model-name` と同じこと、`pr` が `--repo`・`--pr` で解決した PR と同じことも確かめる。
2. 投稿の時点の PR の head が、`outcome` が `fixes_pushed` なら `commit`、それ以外なら `reviewed_head` と同じことを確かめる。古い HEAD の巡を新しく投稿しない。
3. PR の全コメントを取得し、更新の対象のコメント（下記）を除いて状態を読み（`read_state`）、照合する（6.4 節）。同じ巡の番号の記録がほかのエージェントにあれば停止し、番号の大きい巡があれば停止する。
4. `compose_comment` で全文を組み立てる。本文に、エージェントのマーカー・巡のマーカー・記録のブロックの目印が含まれていれば拒否する（helper が付けるため）。65,536 文字（GitHub のコメント本文の上限）を超える場合も拒否する。超えたら、本文の「対応しない」を判断 ID と一行の理由だけに短くして組み立て直す。それでも超えるときは、投稿できない停止としてチャットで報告する。
5. 更新の対象は、1 行目が自分のエージェントのマーカーで、2 行目が同じ番号・同じ `reviewed_head` の巡のマーカーのコメントである。0 件なら作成し、1 件なら更新し、2 件以上なら停止する。
6. ほかの巡のコメント、ほかのエージェントのコメント、巡のマーカーを持たない旧形式のコメントは編集しない。
7. 記録付きの投稿が失敗しても、記録なしの投稿に切り替えない。

**記録なしの投稿**（`--records-file` を付けない場合と、旧 PR 修正ループ（herdr-pr-review の `--loop`。#571 で削除）の controller の呼び出し。新しい PR 修正ループは記録なしの投稿をしない）:

- 本文に巡のマーカーか記録のブロックの目印が含まれていれば拒否する。
- 更新の対象は、1 行目が自分のエージェントのマーカーで、2 行目が巡のマーカーでないコメントである。0 件なら作成し、1 件なら更新し、2 件以上なら停止する（従来どおり）。
- Python の呼び出し方 `upsert(pr, repo, model_name, markdown, *, runner=None)` を保つ。記録を渡す引数を足す場合は、既定値のあるキーワード専用の引数にする。
- エージェントのマーカーの ID は、3 章の規則（`agent_id_for`）で作る。同じ規則を helper に写さず、モジュールの関数を使う。

**CLI**: 記録付きの投稿は `--records-file` に、planner が Write で書いた巡の記録の JSON のパスを渡す。JSON は、親の main worktree の `tmp/agents/` に作ったセッションのディレクトリに置く。拒否したときは、問題を標準エラー出力に 1 行ずつ書き、終了コード 64 で終わる（helper の既存の終了コード）。

### 7.5 投稿する場面

利用者の決定（2026-10-01、U4）により、実装をせずに終わる巡でも投稿を必須にする。

| 巡の結果 | 状況コメントの投稿 |
| --- | --- |
| `fixes_pushed` | 必須（push の後） |
| `no_fix_needed`・`stopped`・`stopped_` で始まるもの | 必須。判断を次の巡の入力として残すため |
| `awaiting_approval` | 任意。投稿した場合は、承認・push の後に、同じ巡のコメントを更新する |

- レビュー対象の PR が open でない場合は投稿しない。
- **投稿できない停止**（記録を読めない・壊れた記録を読んだ、`gh` の失敗、本文が上限を超える）は、チャットで報告する。壊れた記録が原因なら、そのコメントの ID と問題を示す。
- **書けなかった記録**は、次の巡の前に、同じ内容で投稿し直す（同じ巡の更新として扱われ、何度実行しても同じ結果になる）。次の巡が先に記録された後は、前の巡を更新できない。その場合は報告して、次の巡の planner が、前の巡の `fix` の判断を修正後の HEAD で確かめる。

### 7.6 helper とスキルから使う公開名

| 名前 | 使う場面 |
| --- | --- |
| `validate_record(value)` | 記録単体の検査 |
| `check_against(record, prior)` | 前の状態との照合。`prior` は `read_state` の結果 |
| `read_state(comments, *, expected_pr=None, exclude_comment_ids=())` | コメントの一覧から、現在の状態と `ignored_comments` を作る |
| `comments_from_api(payload)` | `gh api` の出力を、コメントの一覧（`id`・`body`・`author`・`author_association`）に整える |
| `compose_comment(record, body)` | 記録付きの状況コメントの全文を組み立てる |
| `render_block(record)`・`extract_record(body)`・`round_marker(n, head)` | 記録のブロックと巡のマーカーの描画・取り出し |
| `agent_id_for(model)` | エージェントのマーカーの ID |
| `record_markers_in(text)` | 本文に含まれるマーカーの種類（記録なしの投稿の拒否に使う） |
| `AGENT_MARKER_PATTERN`・`ROUND_MARKER_PATTERN`・`TRUSTED_ASSOCIATIONS`・`COMMENT_BODY_LIMIT` | マーカーの行の判定、信頼する投稿者、本文の上限 |
| `RecordError` | 検査の失敗。`problems` に場所付きの問題をすべて持つ |

helper は、自分のファイルの位置から辿ってモジュールを読み込む（例: `Path(__file__).resolve().parents[4] / ".agents/scripts/review_records.py"` を `importlib` で読み込む）。cwd に頼らない。検査をスキルや helper に写さない。

## 8. 読む側の扱い

### 8.1 現在の状態の取り出し方

```bash
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" \
  | uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" fold --repo "$PR_REPO" --pr "$PR_NUMBER"
```

`$TOPLEVEL` は親リポジトリの toplevel、`$PR_REPO` は PR が存在するリポジトリ（`owner/name`）、`$PR_NUMBER` は PR の番号である。

- 記録を読むのは、書き込み権限のある投稿者のコメントのうち、1 行目がエージェントのマーカー、2 行目が巡のマーカーのものだけである。マーカーと記録の `agent_id`・`round`・`reviewed_head` が食い違えば、壊れた記録として扱う。
- マーカーを持つが読まなかったコメントは、出力の `ignored_comments` に、コメント ID・投稿者・理由とともに挙がる。信頼しない投稿者のコメントと、巡の状況コメントの形でないのに記録のブロックを持つコメントである。人のコメントが記録のマーカーを引用していても、集約は失敗しない。
- 記録のブロックを持たないコメント（旧 PR 修正ループの進捗コメント、旧形式の状況コメントなど）は無視する。`body` が null のコメントは、空の本文として扱う。
- 記録を、巡の番号・記録時刻・コメント ID の順に並べ、判断 ID ごとに最後の記載を現在の状態とする。`sources` は全巡の分をまとめる。
- 出力は `{"schema", "pr", "rounds", "decisions", "ignored_comments"}` の JSON である。`rounds` は各巡の番号・`reviewed_head`・記録時刻・`planner`・`seats`・`outcome`・`commit`・`stop_reason`・コメント ID を持つ。`decisions` は判断の現在の状態で、`history`（巡・HEAD・判断した planner のモデル・状態・変化・巡の結果・コメント ID）を持つ。
- `--repo`・`--pr` と違う PR の記録があれば、そのコメント ID を示して失敗する。読むべきコメントの記録が壊れていれば、そのコメント ID を示して失敗する（終了コード 65）。記録が 1 件も無いこと（最初の巡）は、異常ではない。
- 利用者がコメント ID を指定した場合に限り、`--exclude-comment <ID>`（複数可）で、そのコメントを読まずに続けられる。

### 8.2 新しい根拠

`skip` の判断を見直してよいのは、次のどれかの新しい根拠があるときだけである。

- 記録に無い、実行した再現
- 記録の判断と異なる利用経路・結果の種類を示す再現
- 指摘の対象のコードが、発生条件を変える形で変わった
- 利用者の指定

表現の違い、別の席が同じ指摘を出したこと（票の数）、重大度の表記だけの引き上げは、新しい根拠に当たらない。

### 8.3 pr-review

手順 2 で `TOPLEVEL` と PR のリポジトリ（PR の URL の `owner/name`）を確定した後に、8.1 節のコマンドで判断の記録を集約する。

| 現在の状態 | 扱い |
| --- | --- |
| `skip` | 同じ問題で新しい根拠が無ければ active にせず、Decided findings に書く（4.4 節）。新しい根拠があれば active にして、`見直しの根拠` を書く |
| `resolved` | 現行 HEAD で再現したら、回帰として active にし、判断 ID を書く |
| `fix` | 前回の修正を確かめる対象にする。解消していなければ、active のまま続ける。`fix` が修正済みであることを示すのは、`history` の最後の `outcome` が `fixes_pushed` のときだけである |
| `hold`・`unverified` | 通常どおり扱う。足りない確認を補えたら、根拠に書く |

- 自分の指摘に対応する判断は、`sources` で探す（2.2 節）。同じ問題かどうか迷うときは、active のまま「判断 `<ID>` と同じ可能性がある」と書き、planner に任せる。
- 記録を、新しい指摘の候補を探す情報源にしない。前回の修正の範囲（4.2 節）を知るために使う。
- 他モデルのレビューファイルを読まないという独立性の規則は変わらない。PR のコメントは、従来どおり手順 1 で読む。
- 記録を読めないとき（`gh` の失敗、壊れた記録）は、記録なしで続け、Review coverage に理由を書く。

### 8.4 review-fix-planning

- 判断の前に、8.1 節のコマンドで前の状態を読む。記録を読めないときは、判断を引き継げないので停止し、チャットで報告する（7.5 節）。復旧は、利用者が壊れたコメントを直すか消すか、読まないコメントの ID を指定して行う。
- `skip` の判断は引き継ぐ（`inherited`）。新しい根拠があるときだけ再評価し、変えるときは `changed` と `new_evidence` を書く。
- `resolved` の問題が再現したら、`changed`（回帰）にする。
- 前の巡で `fix` だった判断は、出典の席にかかわらず、この巡で修正後の HEAD で確かめる。解消していれば `resolved`（`changed`）、解消していなければ `fix` を引き継ぐ（`inherited`。前の `d1` はそのまま書く）。
- 古い席（`rerun` が `false` になるファイル）の指摘のうち、判断済みのものは引き継ぎで済ませ、再検証しない（上の `fix` を除く）。未判断のものだけを、通常どおり検証して分類する。
- 追加修正なしで終える（`no_fix_needed`）前に、前の巡の `hold`・`unverified` を片付ける。残っていると照合で拒否される。
- 席構成（モデル・ランタイムを含む）を作り、古い席を入力と区別する。Spec（ローカル）にも、同じ席構成の表、planner のモデル、投稿した記録（またはそのコメントの URL）を残す。
- 旧形式の状況コメントしか無い PR で記録を始めるときは、巡の番号を 1 から数える。旧形式のコメントで「対応しない」とした指摘がこの巡の入力にあれば、`new` として記録し、`reason` に旧形式のコメントの ID を書く。

## 9. 互換

| 対象 | 保つこと |
| --- | --- |
| `tools/herdr_pr_review/artifacts.py`（#571 で削除） | レビューディレクトリの直下には `<model>.md` 以外のファイルを置かない。機械向けの記録は `findings/`・`verified/` の下に置く（形式は [review-findings](review-findings.md)）。新しい PR 修正ループの記録は `loop/` の下に置く。レビューファイル名は `<model>.md` のまま |
| 旧 PR 修正ループ（#571 で削除）の `_preserve_review_history` | `## Review history` の見出しと 3 つの列を変えない。足す行と節（レビュアーの行、根拠の項目、Decided findings、`U-<n>` の ID）は、表の外に置く |
| 旧 PR 修正ループ（#571 で削除）のレビュアー・planner の入力 | JSON のキーを足さない。根拠は `content` の本文に、判断の理由は `reason`・`decision_reason` の本文に書く |
| 旧 PR 修正ループ（#571 で削除）の進捗コメント | 記録なしの投稿の規則を保ち、`upsert()` の呼び出し方を変えない |
| #483 の保存先 | 記録は、PR を `owner/name` と番号で、レビューをファイル名で識別し、ローカルのパスを含めない。`reviews/#<N>-…` と `reviews/<owner>-<repo>/#<N>-…` のどちらでも、同じ記録を使える |
| 旧形式のコメント | 記録付きの投稿は、巡のマーカーを持たないコメントを編集しない。記録なしの投稿は、従来どおり自分のコメントを更新する。読む側は、記録の無いコメントとして扱う |
| 移植 | `review_records.py` は標準ライブラリだけで書かれている。`agent_tmp.py` と同じく、スキルの外にある依存として扱う |

新しい PR 修正ループ（#570）は、レビュアーに PR の全コメントを渡し（8.3 節）、各サイクルの計画を review-fix-planning で行う（8.4 節）。loop の helper は、各サイクルの記録を前の状態と照合（`check_against`。6.4 節）してから次へ進むので、巡をまたいでも `skip` の判断を引き継ぐ。旧 loop（#571 で削除）は判断の記録を読み書きしなかった。

## 10. 形式を変えるとき

- 形式を変える必要に気づいたら、変更を扱う Issue を起票し、関連に #493・#494 を書く。その Issue で #494 の Spec を改訂し、改訂版を remote から参照・復元できる形で公開してから、この文書と、必要な場合は `review_records.py` を変える。
- 互換の無い変更は、`review-records/v2` にする。`review_records.py` は、知らない版の記録を拒否する。
- 記入例を変えたら、冒頭の手順（`review_records.py validate --record-file`）で記入例の記録を検査する。
