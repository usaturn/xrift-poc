# サイクルの判断の記録と投稿

review-fix-planning の planner が、新規は review-records/v2 で判断を記録し、状況コメントとして投稿する。旧 v1 と旧結果は読み取り互換を保つ。形式・検査の規則は `.agents/review-records.md` が定義元で、この文書はそれを planner の作業の順に並べたものである。規則の内容が食い違って見えるときは、`.agents/review-records.md` に従う。

変数は SKILL.md の通常入口と投稿手順で決めたもの（`TOPLEVEL`・`PR_REPO`・`PR_NUMBER`・`MODEL_NAME`）を使う。`TOPLEVEL` は親リポジトリの checkout（helper とモジュールがある場所）である。

## 1. 前の状態を読む（手順 2）

```bash
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" \
  | uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" fold --repo "$PR_REPO" --pr "$PR_NUMBER"
```

- 終了コード 0: 出力の `rounds` が記録済みの巡、`decisions` が判断の現在の状態、`ignored_comments` が読まなかったマーカー付きのコメントである。
- この巡の番号は、`rounds` の最大の `round` + 1（記録が無ければ 1）。
- 終了コード 65（壊れた記録・ほかの PR の記録）や `gh` の失敗: 判断を引き継げないので、ここで停止し、コメント ID と問題をチャットで報告する。復旧は、利用者が壊れたコメントを直すか消すか、読まないコメントの ID を指定して行う（指定されたら `--exclude-comment <ID>` を付ける）。
- 旧形式の状況コメント（巡のマーカーが無いもの）しか無い PR は、記録が無いものとして巡 1 から数える。旧形式で「対応しない」とした指摘がこの巡の入力にあれば、`new` として記録し、`reason` に旧形式のコメントの ID を書く。

## 2. 席構成（`seats`）

レビューディレクトリのファイルごとに 1 件。

| キー | 値の取り方 |
| --- | --- |
| `review_file` | ファイル名から `.md` を除いたもの |
| `model`・`runtime` | 先頭の `- レビュアー: <モデル>（ランタイム: <ランタイム>）` の行。行が無い旧形式・様式外は、`model` にファイル名、`runtime` に `unknown`、`note` に理由 |
| `file_head` | `## Review history` の最後の行の HEAD（先頭 12 桁）。表の無いファイルは、ファイルが示す対象 HEAD。分からなければ `unknown` |
| `mode` | 同じ行の Mode（`Initial`・`Update`）。表の無いファイルは `Initial` |
| `rerun` | `file_head` が `reviewed_head` の先頭 12 桁と同じときだけ `true` |
| `merge_recommendation` | `Request changes`・`Needs manual verification`・`Approve` のどれかをそのまま。無い・ほかの文言なら `null` にし、原文を `note` に書く |

- `reviewed_head` は、この巡で再実行した席がレビューした HEAD（全 40 桁）である。`rerun` が `true` のレビュアーが無い場合、新しい結果は `stopped` だけ。旧 `stopped_*` はそのまま読む。
- `rerun` が `false` の席（古い席）は、状況コメントの「入力から外したファイル」に理由とともに書く。古い席の判断済みの指摘は引き継ぎで済ませ、未判断の指摘だけを通常どおり検証する。
- 指摘 ID の無いレビューファイルは、planner が `planner-<n>` を付け、その席の `note` に書く。

## 3. 判断（`decisions`）

この巡の入力にある指摘（`rerun` が `true` の席の active・Pending previous findings・Unverified candidates と、古い席の未判断の指摘）と、前の状態で `fix` の判断を、すべてどれかの判断の `sources` に含める。Decided findings の指摘は入力に含めない。

| Spec での決定 | JSON |
| --- | --- |
| 指摘の参照 `<ファイル名>#<ID>@<検出時 HEAD 12 桁>` | `id`（最初の出典の参照。分けるときだけ `/<部分>`）と `sources`（まとめた参照すべて） |
| 位置と発生条件 | `summary`（1 行）、`locations`（`reviewed_head` での `path:line`） |
| 状態 | `state`: 対応する `fix`・判断保留 `hold`・対応しない `skip`・解消済み `resolved`・検証未完了 `unverified` |
| 前の判断との関係 | `change`: `new`・`inherited`・`changed`。`changed` は `previous_state`、前が `skip` なら `new_evidence` も |
| 重要度 | `severity`: 判断に使った値。再評価の経緯は Spec と `reason` に書く。`sources` がすべて `U-<n>` のときだけ `null` |
| PR との関係 | `pr_relation`: 持ち込み `introduced`・顕在化 `surfaced`・既存 `preexisting`・不明 `unknown` |
| 採否の理由 | `reason`。`fix` は先頭に基準ラベル（`H:`・`D1:`・`N:`）。利用者が選んだ Hold を実装するなら `Hold:` |
| 見直す条件 | `revisit_when`（`skip` のとき必須） |
| 足りない確認 | `unverified_reason`（`unverified` のとき必須） |
| D1 | `d1`: `reproduced_by`（`sources` の参照、`planner`、または review-verify の確認担当 `verifier`）・`usage_path`・`result_kind`（`stop`・`wrong_result`）。planner か確認担当が再現したら `reproduction`（`commands`・`place`・`conditions`・`result`。確認担当の再現は確認済みの一覧から写す） |
| 持ち越し | `carry_over`（`skip` のときだけ）: 既存の Issue があれば `issue`、無ければ `candidate`（一行） |

- 前の巡で `fix` の判断は、修正後の HEAD で確かめ、解消していれば `resolved`（`changed`、`previous_state: fix`）、していなければ `fix`（`inherited`、前の `d1` をそのまま）にする。
- `skip` を見直すのは新しい根拠（`.agents/review-records.md` 8.2 節）があるときだけで、変えるなら `changed`・`previous_state: skip`・`new_evidence` を書く。
- 記録済みの参照を別の判断に移さない。同じ問題だと後で分かった 2 つの判断は、一方を `skip` にし、`reason` と `revisit_when` に他方の判断 ID を書く。
- 変更の種類・比較に使った base 側のコミットは v1 の記録に無い。Spec に書き、必要なら `reason` にも書く。

## 4. 結果（`outcome`）

| 状態 | `outcome` | 追加のキー |
| --- | --- | --- |
| 採用修正・検証・1回の自己レビュー/修正確認を完了し既存PRへpush | `fixes_pushed` | `commit`（pushした完全SHA） |
| 必要確認を終え、追加Fix/Hold/Unverified無し | `no_fix_needed` | ―（最終化の実完了はfinalize結果で別に確認） |
| 具体的障害により続行できない | `stopped` | `stop_reason`: `permission: ...` / `environment: ...` / `external: ...` |

旧 `awaiting_approval`・`stopped_*` を新しい終点として書かない。前の記録は変換せず読む。通常判断やSpec/Plan完成を停止理由にしない。

`no_fix_needed` は、前の状態にこの巡の判断を重ねた結果に `fix`・`hold`・`unverified` が残らないときだけ通る。前の巡の Hold・検証未完了は、この巡で判断し直してから終える。

v2のplanner/各seatsに、共有形式のprovenance（agent/role/provider/effort/session/CLI/run/request/skills）を残す。既存runner/helperの実測を使い、unknown/nullを設定値や別attemptの成功で埋めない。hyphenの実Request役割と、共有形式のself_review/fix_checkを混同しない。識別情報の共通形式そのものは変えない。

planning.jsonはreview-findings/v2。自己レビューの前に司令塔が仮の版を作り（SKILL.md 5節の手順5）、activitiesのstage=pre_push_review枠はrun_roles.pyが起動前に1件予約して、失敗でも残す。この件数と、自己レビュー/修正確認/待機の実時間は別に記録する。未実施を実測0にしない。

## 5. ファイルを作り、検査する

```bash
ROUND_DIR="$(uv run --no-project python "$TOPLEVEL/.agents/scripts/agent_tmp.py" mkdir "review-fix-planning-pr$PR_NUMBER")"
ROUND_JSON="$ROUND_DIR/round.json"
COMMENTS_JSON="$ROUND_DIR/comments.json"
```

- 記録は Write で `$ROUND_JSON` に書く（`recorded_at` は書いた時刻の UTC）。本文（見出しから作業結果まで）は SKILL.md の雛形に従う。
- 投稿の前に検査する。

```bash
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" validate --record-file "$ROUND_JSON"
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" > "$COMMENTS_JSON"
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" validate \
  --record-file "$ROUND_JSON" --comments-file "$COMMENTS_JSON"
```

同じ巡のコメントを更新するときは、2 つ目に `--exclude-comment <そのコメントの ID>` を付ける。終了コード 65 は内容の問題で、標準エラー出力の 1 行ずつの問題を直してから検査し直す。

## 6. 投稿する

SKILL.md の「作業終了前のPR投稿」の雛形で、`--records-file "$ROUND_JSON"` を付けて helper を実行する。

- helper は、マーカー 2 行と記録のブロックを付け、記録単体の検査、`--model-name` と `planner.model` の一致、PR が open で `pr` と一致すること、PR の head（`fixes_pushed` なら `commit`、それ以外は `reviewed_head`）、前の状態との照合を確かめてから、同じ巡の自分のコメントを更新するか、無ければ作る。
- 終了コード 64 は拒否である。標準エラー出力の問題が記録の内容なら直して投稿し直す。それ以外（`gh` の失敗、ほかのエージェントが同じ巡を記録した、番号の大きい巡がある、同じ巡のコメントが 2 件以上）は停止して報告する。記録なしの投稿や `gh` の直接の呼び出しに切り替えない。
- 本文が 65,536 文字を超えると拒否される。作業判断の「対応しない」を判断 ID と一行の理由だけにして投稿し直す。それでも超えるなら、投稿できない停止としてチャットで報告する。
- push後は `outcome=fixes_pushed`、commitはpushしたSHA、recorded_atは更新時刻。既に同サイクルの自分のコメントがあれば、同じhelperで更新する。no_fix_neededの本文はマージ前の採否/検証状況を示し、MERGED/Issue更新を先に実施済みと書かない。finalizeの実結果もSpec/planning/最終報告へ残す。
- 投稿できなかった記録は、次の巡の前に同じ内容で投稿し直す。次の巡が先に記録された後は前の巡を更新できないので、報告し、次の巡の planner が前の巡の `fix` を修正後の HEAD で確かめる。

## 7. 後片付け

`ROUND_DIR` は最終投稿/最終化が成功し、Specへレビュアー表・plannerモデル・コメントURL・役割/回数/実時間を写した後だけ `rm -rf -- "${ROUND_DIR:?}"` で消す。失敗・復旧用の記録は残し、完全パスを最終報告へ書く。自己レビューの予約を持つplanning.jsonは回数の根拠なので、別ファイルで上限をリセットしない。
