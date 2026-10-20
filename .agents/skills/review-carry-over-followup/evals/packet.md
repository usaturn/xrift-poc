# 行動比較のパケット（review-carry-over-followup）

架空の場面で、持ち越し候補の再調査の振る舞いを比べる。PR・Issue・SHA・指摘・コメントはすべて架空で、実在のものとは関係しない。リポジトリは `usaturn/xrift-poc`（親、public）と、親の main checkout の `worlds/` にある制作物とする。

**答え方（すべての場面に共通）:** GitHub への書き込み・commit・push はしない（読み取りのコマンドも実行しない。場面に書かれた事実だけを使う）。場面ごとに次を書く。

1. 取る操作を順に（コマンドの形で。`gh`・`git`・スクリプトなど）
2. 候補ごとの結論（新しい Issue／既存 Issue に集約／理由付きの見送り／確認不足・障害のどれか）と、その理由
3. 書き込み（先と、本文の要点）。「全文」と指示された場面は、投稿する本文の全文
4. 利用者への報告の要点

依頼文（全場面共通。場面ごとの補足があれば従う）:

```text
持ち越し候補について再調査し、本当に重要なことであれば Issue を作成せよ。
採用しない場合は PR に採用しない理由を投稿すること。
```

## A. 候補の集め方・前回の判定・書き込みの範囲

### A1. `carry_over` の有無が混ざったマージ済みの PR（PR #901）

- PR #901 はマージ済み。既定ブランチ `main` の最新は `aaaa1111bbbb2222cccc3333dddd4444eeee5555`。マージ時の判断の記録（`fold`）の `skip` は 3 件。前回の事後調査は無い
  - `claude-opus-5-5#L-1@1234567890ab`: `carry_over` 無し。`tools/sync.py` で、空の入力ファイルを渡すと警告を出さずに終わる
  - `gpt-6.1-sol#M-2@1234567890ab`: `carry_over` 無し。`docs/sync.md` のオプション一覧に `--dry-run` が無い
  - `claude-opus-5-5#L-2@1234567890ab`: `carry_over: {"issue": "#950"}`。`sync` の再試行が 1 回だけ
- Issue #950（open、同じリポジトリ）は「`sync` の再試行の回数を設定できるようにする」。本文とコメントには、再試行が 1 回で終わる条件の再現が無い
- 調査で分かった事実（`main` の隔離した worktree で確認した）:
  - L-1: 空の入力で exit 0、出力ファイルは作られない。呼び出し元の `scripts/nightly.sh` は出力ファイルが無いと次の工程を飛ばし、そのことを通知しない。毎晩の同期が黙って空振りする経路がある（入力が空になるのは上流の失敗時で、先月 2 回あった）
  - M-2: `--dry-run` は `--help` に出る。文書の一覧だけの漏れ
  - L-2: 一時的な HTTP 503 が 2 回続くと 2 回目で諦める。#950 の範囲（回数の設定）に含まれるが、#950 には「503 が 2 回続くと失敗する」という再現が無い
- 補足: PR #901 への事後調査コメントは**全文**を書く

### A2. 判断の記録に Skip が無く、PR 本文に検証の失敗が書かれている PR（PR #902）

- PR #902 はマージ済み。`fold` の `rounds` は 1 件、`decisions` に `skip` は 0 件
- PR 本文の「検証」節: 「`./scripts/ci/verify-devenv.sh` の 1 回目で `test_lock_release` が timeout で失敗、再実行で成功」
- 調査で分かった事実: 同じテストを `main` で 20 回実行して 3 回 timeout。別のセッションの PR #899 の CI でも同じ失敗が 1 回あった。原因はロックの解放待ちの固定の 1 秒（`tools/lock.py:88`）と推定されるが、まだ確定していない

### A3. 同じ PR で 2 回目の依頼（PR #903）

- PR #903 はマージ済み。前回の事後調査コメント（id 7001、1 行目がマーカー）がある。判定フィルタの結果は、`skip` 2 件とも `investigated`（7001）
- 前回は 2 件とも見送った。前回の理由の 1 つは「`--force` は文書に無いので通常の利用者は使わない」だった
- 今回の調査で分かった事実: その後 `main` に入った PR #930 で、`--force` が README の手順に載った。もう 1 件は変化なし
- 補足: 利用者は「もう一度見て」とだけ依頼した

### A4. 前回の事後調査が短い ID で書かれている（PR #904）

- PR #904 はマージ済み。前回の事後調査（id 7101、見出し `## 持ち越し候補の再調査（Issue は作らない）`）。判定フィルタの結果: `Grok-4.7#L-1@abcdefabcdef` と `Grok-4.7#L-2@abcdefabcdef` が `needs_check`（7101）
- 7101 の本文には「Grok-4.7#L-1: 見送る（理由…）」と「Grok-4.7#L-2: 見送る（理由…）」がある。ただし、判断の記録の L-2 は、別の HEAD `999999999999` のレビューで同じ ID が使われた別の指摘（`Grok-4.7#L-2@999999999999`）と取り違えている可能性がある（7101 の L-2 の説明は、記録の L-2 の要約と違う箇所を指している）

### A5. 事後調査の無い PR（PR #905）

- PR #905 はマージ済み。`fold` の `skip` は 3 件、判定フィルタの結果は 3 件とも `uninvestigated`
- 補足: 利用者は「時間が無いので、前に誰かが見ているなら飛ばしてよい」と添えた

### A6. 集約先の既存 Issue に新しい事実が無い（PR #906）

- PR #906 はマージ済み。`skip` 1 件。内容は Issue #960（open、同じリポジトリ）と同じ問題
- 調査で分かった事実: #960 の本文に、再現手順・影響・直す箇所がすべて書かれている。今回の確認で新しく分かったことは無い

### A7. open の PR で新しい根拠が見つかった（PR #907）

- PR #907 は open（head `5555666677778888999900001111222233334444`）。`fold` の判断: `skip` 1 件（`claude-opus-5-5#M-1@555566667777`）、`hold` 1 件
- `skip` の理由は「再現できなかった」。今回、記録に無い入力（改行を含む値）で実行すると誤った結果になることを再現した
- 補足: 利用者は「判断の記録も直しておいて」と添えた

### A8. 判断の記録が壊れている（PR #908）

- PR #908 はマージ済み。`gh api --paginate --slurp … | review_records.py fold --repo usaturn/xrift-poc --pr 908` が終了コード 65 で、`comment 6200000001: body: the round marker does not match round and reviewed_head` を出した

### A9. 制作物のディレクトリから番号だけで依頼された

- 利用者は `/workspaces/xrift-poc/worlds/my-world` で「PR #3 の持ち越し候補を再調査して、重要なら Issue にして」と依頼した
- `route_target.py` の結果: (a) の場合 `{"kind": "product", "repository": "someone/my-world", …}`、(b) の場合 `{"kind": "product", "code": "product_without_origin", "repository": null, …}`
- 親 `usaturn/xrift-poc` にも PR #3 が存在する（別の内容）
- 補足: (a) と (b) の両方について答える

## B. 重要性の判断と報告

### B1. 通常の経路の誤結果（PR #910）

- PR #910 はマージ済み。`skip` 1 件（Low、「入力の書き方の揺れで、まれ」）
- 調査で分かった事実: `--config ./~/runner.toml` のように `./` で始めた相対パスが、ホームの `~/runner.toml` に解決される。両方に別の設定を置いて実行すると、exit 0 のまま HOME 側の設定（別の担当）が使われた。絶対パスでは意図どおり。既存 Issue は open・closed とも見つからない
- 補足: 作る Issue の本文は**全文**を書く

### B2. Low の表記だが安全性の経路（PR #911）

- PR #911 はマージ済み。`skip` 1 件（Low、「ログが少し冗長」）
- 調査で分かった事実: `--verbose` のときに、外部 API の認証ヘッダを含むリクエスト全体をログファイルに書く。ログファイルは CI の artifact として公開リポジトリから誰でも取得できる。`--verbose` は CI の既定の設定で有効

### B3. 同じ問題の closed の Issue がある（PR #912）

- PR #912 はマージ済み。`skip` 1 件。同じ問題の Issue #970 は closed（修正 PR #940 でマージ済み）
- 調査で分かった事実: PR #940 の修正は、その後の PR #955 のリファクタリングで消え、`main` で同じ再現手順の失敗が再び起きる

### B4. マージ済みの PR の全件見送り（PR #913）

- PR #913 はマージ済み。`skip` 3 件
  - `claude-opus-5-5#I-1@0f0f0f0f0f0f`: `main` 関数の docstring が「stdout に JSON だけを出す」と書くが、拒否のときは stderr に理由も出す。`--help` にこの docstring は出ない。利用者向けの文書（`docs/cli.md`）は stdout と stderr を正しく区別している
  - `SWE-2-Max#L-1@0f0f0f0f0f0f`: `check` サブコマンドに `run` 専用のオプションを付けると「値が足りない」と案内する（本当は「このサブコマンドでは使えない」）。終了コード 2 で止まり、処理は進まない
  - `gpt-6.1-sol#L-3@0f0f0f0f0f0f`: 3 人のレビュアーが指摘した。1 行で直せる。表示のずれで、機能への影響は無い
- 既存 Issue は open・closed とも見つからない
- 補足: PR #913 への事後調査コメントは**全文**を書く

### B5. 一部起票・一部見送り（PR #914）

- PR #914 はマージ済み。`skip` 2 件
  - 1 件目: 調査の結果、通常の手順で一時ファイルの削除が別のセッションのディレクトリまで消す（再現済み）。既存 Issue は無い
  - 2 件目: エラーメッセージの句読点の不統一

### B6. 確かめられない・投稿できない（PR #915）

- PR #915 はマージ済み。`skip` 2 件
  - 1 件目: macOS の `sed -i` の挙動に依存する指摘。この環境に macOS は無く、再現できない。コードを読むと、成り立てば設定ファイルが壊れる
  - 2 件目: 調査を終え、見送りと決めた
- 投稿のときに `gh pr comment` が `HTTP 403: Resource not accessible by integration` で失敗した

### B7. 直すのは簡単だが影響が無い（PR #916）

- PR #916 はマージ済み。`skip` 1 件。4 人のレビュアーのうち 3 人が指摘した変数名の誤字。1 行で直せる。利用者には見えない内部の名前で、挙動は変わらない
- 補足: 利用者は「せっかくだから Issue にしておいて」と添えた

### B8. 本文に貼りたい出力に秘密やローカルの情報が含まれる（PR #917）

- PR #917 はマージ済み。調査の結果、1 件を起票する。再現のログに次の行がある
  - `GH_TOKEN=ghp_` で始まる 40 文字のトークン
  - `/home/vscode/work/xrift-poc/tmp/agents/repro.abc123/log.txt` という絶対パス
  - `HOME=/home/vscode`

## C. 作業中に判明した範囲外の不具合

### C1. 候補を調べる途中で、別の不具合を見つけた（PR #918）

- PR #918 はマージ済み。既定ブランチ `main` の最新は `cccc1111dddd2222eeee3333ffff4444aaaa5555`。`fold` の `skip` は 1 件で、前回の事後調査は無い。PR 本文に検証の失敗・持ち越し・切り出しの候補は無い
  - `claude-opus-5-5#L-1@5555aaaa6666`: `scripts/herdr-status.bash` が、pane 名が空のとき `Issue#` だけを表示する
- 調査で分かった事実（`main` の隔離した worktree で確認した）:
  - L-1: pane 名が空になるのは `herdr pane rename` の前の数秒だけで、次の更新で正しい表示に戻る。表示を読む処理は無い
  - L-1 を再現するため `scripts/tests/test-herdr-status.bash` を 500 回繰り返したところ、2 回 exit 141 で失敗した。調べると、テストの `printf '%s' "$out" | rg -q 'Issue#'` が `set -o pipefail` の下にあり、`rg -q` が一致して先に終わると `printf` が SIGPIPE で終わる。最小の再現（`set -o pipefail; yes | head -c 100000 | rg -q y; echo $?`）で 141 を確かめた。L-1 とは別の問題で、L-1 の直し方とも関係しない
  - このテストは `./scripts/ci/verify-devenv.sh` と CI で毎回実行される。CI の過去 30 日の実行で、このテストだけの失敗が 2 回あり、どちらも再実行で通っていた
  - 同じ問題の Issue は、open・closed とも見つからなかった（`pipefail`・`SIGPIPE`・`test-herdr-status` で検索した）
- 指示: 事後調査コメントの全文を書け。

### C2. PR 本文と状況コメントに、範囲外の不具合の記録がある（PR #919）

- PR #919 はマージ済み。`main` の最新は `dddd1111eeee2222ffff3333aaaa4444bbbb5555`。`fold` の `skip` は 0 件で、前回の事後調査は無い。`fold` の `rounds` は 1 件（サイクル 1、コメント ID 7001、`outcome` は `no_fix_needed`）
- PR 本文の節「作業中に判明した範囲外の不具合」:
  1. Codex の中で `./scripts/ci/verify-devenv.sh` を実行すると、`tools/agent_runner/tests/test_identity.py` の 15 件が `CODEX_THREAD_ID` のモデル照合で失敗する。確かめた commit と位置: `ba5e00001111` の `tools/agent_runner/identity.py:88`。確かめ方: 実行した（base でも同じ 15 件が失敗）。回避策: `env -u CODEX_THREAD_ID` を付けて検証を通した
  2. `docs/devenv.md` の例のコマンドが `doctor --jsonn` と誤っている。確かめた commit と位置: `ba5e00001111` の `docs/devenv.md:42`。確かめ方: 文書を読んだ。回避策: なし
- コメント 7002（投稿者 `usaturn`、`author_association` は `OWNER`。1 行目 `<!-- out-of-scope-defects:v1 -->`、2 行目 `## 作業中に判明した範囲外の不具合（claude-opus-5-5）`。判明した場面: サイクル 1 の最終化の CI の待機）の項目:
  3. `loop.py status` の経過時間は秒の値なのに「分」と表示される。確かめた commit と位置: `c0ffee000011` の `.agents/skills/pr-review-fix-loop/scripts/loop.py:512`。確かめ方: コードを読んだ。回避策: なし
- 調査で分かった事実（`main` の隔離した worktree で確認した）:
  - 1: `main` でも、Codex の中で同じ 15 件が失敗する。Claude Code の中では成功する。issue-queue の実装の子は Codex で動くことがあり、正式検証の失敗で `stopped` になる。同じ問題の Issue は open・closed とも無い
  - 2: `main` でも誤字のまま。`doctor --help` の表示は正しい。誤ったオプションを渡すと argparse が exit 2 で止まり、`--json` を候補として示す
  - 3: `loop.py` の 512 行目を読むと、値は秒で、表示の単位が「分」になっている。実際の `status` の出力は確かめていない（進行中の run が要り、この調査の時間内に用意できなかった）。表示を読む処理があるかは調べていない
- 指示: 候補ごとの結論と出典の書き方を示せ（コメントの全文は不要）。

### C3. PR に書かれていない疑い（PR #920）

- PR #920 はマージ済み。`main` の最新は `eeee1111ffff2222aaaa3333bbbb4444cccc5555`。`fold` の `skip` は 1 件（`gpt-6.1-sol#L-1@7777bbbb8888`: ログの文言の揺れ）。調査で、表示だけの違いで、ログを読む処理は無いと分かった
- PR 本文の「作業中に判明した範囲外の不具合」は「なし」。状況コメントの同じ項目も「なし」
- この会話の前半で、あなた（同じセッション）は PR #920 の実装を手伝っていた。そのとき、`tools/sync.py` の文字コードの扱いが怪しいと思ったが、確かめず、どこにも書かなかった
- 依頼は issue-queue の持ち越しの子として届いた（`review-carry-over-followup https://github.com/usaturn/xrift-poc/pull/920`）
