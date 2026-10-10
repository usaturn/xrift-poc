# pr-review evals

pr-review の文面を変えたときに、レビュアーの行動が変わったかを確かめるための eval セットと、その実行・採点の手順である。文面の有無ではなく、架空の PR を実際にレビューさせた結果で確かめる（writing-skills の RED/GREEN）。

専用の自動 runner は無い。利用できるエージェントのサブエージェント実行で 1 件ずつ実行し、出力を採点して `results/` に記録する。

## 構成

```text
evals/
  evals.json          ケースの定義（fixture・期待する行動）
  README.md           この文書
  build_fixture.py    fixture から架空の PR を組み立てる（標準ライブラリと git だけ）
  fixtures/<名前>/
    fixture.json      リポジトリ名・PR の番号と本文・commit の手順・コメント
    trees/<名前>/     各 commit で上書きするファイル（名前が .txt で終わるものは .txt を除いて書き出す）
    *.json, *.md      判断の記録・状況コメントの本文・自モデルの前回レビューのひな形（{{ラベル}} と {{ラベル:12}} を commit の SHA に置き換える）
  results/            実行の記録
```

| fixture | 内容 | 主に確かめること |
| --- | --- | --- |
| `multiline-input` | 指摘の本文を保存する CLI に、制御文字を拒否する検査を足す PR。改行も拒否する | 実行した再現、入力の変化（複数行）、「安全か」と「動くか」、D1 の材料の欄 |
| `same-pattern` | `KEY=VALUE` を読む 3 つの関数を足す PR。どれも `split("=")` を 2 つに展開する | 同種箇所を 1 件にまとめる、検索のコマンド、実行した再現 |
| `update-decided` | 2 巡目の Update。判断の記録に `fix` と `skip` があり、修正の commit が「同じ規則」と書いた検査を足す | 判断の記録の扱い（Decided findings）、前回の修正の範囲と重点確認、PR 全体差分 |

## 実行の手順

親の main worktree か feature worktree の root で行う。出力先は scratchpad など、リポジトリの外に作る。

1. fixture を組み立てる。

   ```bash
   uv run --no-project python .agents/skills/pr-review/evals/build_fixture.py <fixture> <空の出力ディレクトリ>/fixture
   ```

   標準出力（と `build.json`）に、`worktree`・`pr_json`・`pr_diff`・`pr_comments`・`previous_review` のパスと、commit の SHA が出る。

2. 確かめる版のスキルを用意する。
   - 変更後（GREEN）: 作業中の checkout の `.agents/skills/pr-review/SKILL.md`。`TOPLEVEL` はその checkout の root。
   - 変更前（RED）: 比べる commit から、スキルと参照先を一時ディレクトリへ展開する。`TOPLEVEL` はその一時ディレクトリ。

     ```bash
     mkdir -p <出力ディレクトリ>/red-skill
     git archive <比べる commit> .agents/skills/pr-review .agents/review-records.md .agents/scripts | tar -x -C <出力ディレクトリ>/red-skill
     ```

3. 下の「指示文」の `{…}` を埋めて、読み取り専用のサブエージェントに 1 回渡す。モデルとランタイムを記録する。
4. サブエージェントが書いた `review.md` と、報告したコマンドの一覧を、`evals.json` の `expectations` に照らして 1 件ずつ採点する（下の「採点」）。
5. `results/<日付>-<目的>.md` に記録する。

### 指示文

```text
あなたは PR のレビュアーです。次のスキルの手順に従って、架空の PR を 1 件レビューしてください。

- スキル: {SKILL}（このファイルと、そこから参照される文書を読む。`.agents/skills/pr-review/evals/` 以下は読まない）
- 依頼: {PROMPT}
- これは行動確認のための実行です。GitHub の PR は存在しません。スキルの手順のうち次のものだけを読み替え、それ以外の手順と規約はスキルのとおりに行います。
  - 時間予算: 全体 30 分（利用者の指定）
  - 手順 1: `review_target.py resolve` と `gh` は実行しない。PR の情報は {PR_JSON}、PR のコメントは {PR_COMMENTS}（`gh api --paginate --slurp repos/{REPO}/issues/{NUMBER}/comments` の出力と同じ形）を読む
  - 手順 2: `create` は行わない。`$WT` は {WORKTREE}（PR の head を checkout 済み。変更しない）。`TOPLEVEL` と `PARENT_ROOT` は {TOPLEVEL}、PR のリポジトリ（`PR_REPO`。`owner/name`）は {REPO}、`PR_NUMBER` は {NUMBER}、`PR_URL` は https://github.com/{REPO}/pull/{NUMBER}、`MODEL_NAME` は eval-reviewer、ランタイムは {RUNTIME}、`$PREVIOUS_REVIEW` は {PREVIOUS_REVIEW}
  - 手順 4: PR 全体の差分は {PR_DIFF}。HEAD の一致の確認は省く
  - 手順 8・9: `save`・`cleanup` は行わない。完成したレビュー本文を {OUT}/review.md に書く
  - `gh` を使うそのほかの手順も、上のファイルで代える。GitHub に書き込まない
- 書いてよいのは {OUT} の中だけです。一時ファイルは {OUT}/scratch に置く。{WORKTREE} と、リポジトリのファイルは変更しない
- 最後に、実行したコマンドの一覧（コマンドと終了コード）と、review.md のパスを報告する
```

`{PREVIOUS_REVIEW}` は、fixture に前回レビューが無ければ「空（既存レビューなし）」と書く。

## 採点

- `expectations` の 1 件ごとに、満たす・満たさないを判定し、根拠として `review.md` の該当部分を短く引用する。実行したかどうかは、サブエージェントが報告したコマンドの一覧と、`review.md` の再現の記録の両方で確かめる。
- 期待と違う形でも、同じ目的を果たしていれば満たすと判定してよい（例: 新しい欄のラベルが無くても、同じ内容を根拠に書いている）。そのときは理由を書く。ただし、変更後の文面がラベルを求めている項目では、ラベルの有無で判定する。
- 変更前（RED）で満たした項目は、その項目については文面を足す必要が無かったことを意味する。変更後（GREEN）で満たさない項目は、文面を直して同じ fixture で再実行する。
- 1 回の実行は傾向の確認であり、統計的な保証ではない。回数・モデル・ランタイムを結果に書く。

## 読み取り専用の範囲

- リポジトリ（親の checkout・feature worktree）と、fixture の `worktree/` を変更しない。再現は一時ディレクトリで行う。
- GitHub に書き込まない。`gh` を実行しない。
- サブエージェントに `evals/` を読ませない（期待する行動が書いてあるため）。
