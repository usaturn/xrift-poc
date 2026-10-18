---
name: review-carry-over-followup
description: "PR（open・マージ済み・close 済み）の持ち越し候補（判断の記録で Skip にした指摘、PR 本文の検証の失敗や切り出しで残した候補）を再調査し、重要なものだけ Issue にして、採用しないものの理由を PR に残すよう依頼されたときに使う。「持ち越し候補を再調査して重要なら Issue にして」「見送る指摘には PR に理由を残して」「マージした PR の Skip を見直して」など。新規の PR レビュー、PR の指摘の採否・修正・最終化、レビュー修正ループには使わない。"
---

# 持ち越し候補の再調査（review-carry-over-followup）

PR のレビューで直さなかった候補を、今のコミット済みのコードで調べ直し、別の Issue で継続して追跡する必要があるものだけを起票する。起票しない候補にも、確かめた事実・理由・見直す条件を元の PR に残す。候補を機械的に Issue へ写さない。「この PR で直さない」と「別 Issue で追跡する必要が無い」は別の判断である。

## 書き込みの範囲

このスキルを明示した依頼で使ったことを、次の書き込みの許可とみなす（利用者への確認は要らない）。

1. 重要と判断した候補の新しい Issue（元 PR と同じリポジトリ）
2. 元 PR への事後調査コメント（新しいコメントを足す）
3. 集約先の既存 Issue へのコメント 1 件。元 PR と同じリポジトリにあり、open で、候補をその Issue に集約し、その Issue にまだ無い事実があるときだけ

- 既存 Issue へのコメントには、元 PR の事後調査コメントへのリンク、判断 ID、調査した SHA と SHA 付きの位置、確かめた事実、見直す条件を書く。範囲に含めるかはその Issue の担当が決め、含めない場合は担当の Spec に一行書く、という既定も書く。利用者の承認・判断を求める書き方、claim やボードのマーカー、担当を名乗る記述は書かない（`issue-to-pr` はそれを見て止まる）
- 既存 Issue にコメントしない場合: 新しい事実が無い（元 PR のコメントに対応だけを書く）、closed（同じ問題が再発していれば新しい Issue を作って closed の Issue へリンクする。解決済みなら見送りの根拠にする）、別のリポジトリ（書き込まず報告に残す）
- しないこと: 既存 Issue の本文・タイトル・ラベル・担当・マイルストーン・状態（close・reopen）の変更、前回の事後調査コメント・判断の記録・元のレビュー成果の編集、コード修正・commit・push・PR・マージ、`review-fix-planning`・`pr-review-fix-loop` の起動、権限・sandbox の緩和
- 利用者が特定の候補の起票を名指しで求めたら、その指定として起票し、本文と報告に重要性の評価をそのまま書く（低いなら低いと書く）

## 入口: 対象の PR と書き込み先を決める

入力は元 PR の URL か番号。会話の記憶だけで対象 PR や候補を推測しない。足りなければ対象を尋ねる。

1. 番号だけ（`#N`・`N`）なら、現在の場所を `route_target.py "$PWD"` で判定し、`env` なら `usaturn/xrift-poc`、`product` ならその `repository` の PR の URL（`https://github.com/<repository>/pull/N`）にしてから次へ進む。`stop`、または `repository` が null なら止まって URL を求める。`review_target.py` に番号をそのまま渡すと、制作物の中からでも親の PR として解決される
2. PR を解決し、状態を読む。`PARENT` は親の main checkout（`$XRIFT_POC_ROOT`）

   ```bash
   SESSION="$(uv run --no-project python "$PARENT/.agents/scripts/agent_tmp.py" mkdir "carry-over-pr<番号>")"
   uv run --no-project python "$PARENT/.agents/scripts/review_target.py" resolve "$PR_URL" > "$SESSION/target.json"
   PR_REPO="$(jq -r .repository "$SESSION/target.json")"; PR_NUMBER="$(jq -r .number "$SESSION/target.json")"
   gh pr view "$PR_NUMBER" --repo "$PR_REPO" \
     --json url,state,mergedAt,mergeCommit,headRefOid,baseRefName,body > "$SESSION/pr.json"
   ```

   `resolve` の終了コードが 0 以外なら、stderr を伝えて止まる（3〜7 は存在・アクセス・認証・API・親の root の障害で、「PR が無い」とは限らない）
3. 書き込み先を確かめる。`PR_REPO` が親の origin（`usaturn/xrift-poc`）か、親の main checkout の制作物のうち `route_target.py` が `product` と判定し `repository` が `PR_REPO` と一致するものだけに書き込む。どちらでもなければ、読み取りの調査まではしてよいが、Issue・コメントを書かずに報告する。制作物の origin を確かめられないときに、親へ代わりに起票しない

   ```bash
   for dir in "$PARENT"/items/*/ "$PARENT"/worlds/*/; do
     [ -d "$dir" ] && uv run --no-project python "$PARENT/.agents/scripts/route_target.py" "$dir"
   done | jq -s --arg repo "$PR_REPO" \
     '[.[] | select(.kind == "product" and ((.repository // "") | ascii_downcase) == ($repo | ascii_downcase))]'
   ```

## 手順

### 1. 候補を集める

```bash
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" > "$SESSION/comments.json"
uv run --no-project python "$PARENT/.agents/scripts/review_records.py" fold --repo "$PR_REPO" --pr "$PR_NUMBER" \
  < "$SESSION/comments.json" > "$SESSION/fold.json"
```

- `fold` が終了コード 65（壊れた記録・ほかの PR の記録）や `gh` の失敗で止まったら、候補を推測せずに止まる。コメント ID と問題を報告し、復旧（利用者が直す・消す・読まないコメントを指定する）を示す。自分の判断で `--exclude-comment` を付けない
- `state` が `skip` の判断はすべて候補にする。`carry_over` は任意の項目で、無いことは持ち越さなくてよいという意味ではない。`carry_over.issue` があれば、その Issue との照合から始める。`carry_over.candidate` は一行の候補文として読む
- `hold`・`unverified`: open の PR では `review-fix-planning` が判断している途中なので、調べず報告に載せるだけにする。マージ済み・close 済みの PR に残っていれば「採否未決のまま終わった候補」として、`skip` と同じ手順で扱う
- 記録の外の候補も集め、出典を書いて `記録外-<n>` と呼ぶ。PR 本文の検証の失敗、PR 本文の持ち越し・切り出しの候補、Spec（親の main checkout の `docs_draft/`。別の環境では読めないことがある）、利用者が示した一覧。保存レビュー（親の main checkout の `reviews/<owner>/<repo>/#<PR>-<branch>/`）は、`fold` の `rounds` が空のとき、保存レビューの最後の HEAD が最後に記録した `reviewed_head` より新しいとき、利用者が示したときだけ読み、どの判断の `sources` にも無い指摘だけを拾う
- 同じ原因の候補はまとめてよいが、元の参照をすべて残す

### 2. 前回の事後調査を確かめる

```bash
jq -n --slurpfile comments "$SESSION/comments.json" --slurpfile state "$SESSION/fold.json" \
  -f "$PARENT/.agents/skills/review-carry-over-followup/scripts/previous_followups.jq" > "$SESSION/previous.json"
```

`followups` が前回の事後調査（1 行目がマーカーか `## 持ち越し候補の再調査`・`## 持ち越しの候補の再調査` で、書き込み権限のある投稿者のコメント）、`candidates[].status` が判定である。判断 ID を含むだけのコメント（状況コメントなど）は前回の事後調査ではない。

| `status` | 扱い |
| --- | --- |
| `investigated`（調査済み） | 前回の結論を読み、その後に前提が変わったか（対象のコード・文書・利用経路の変更、新しい再現）だけを確かめる。変わっていなければ同じ内容を投稿し直さない |
| `needs_check`（要確認） | 前回の本文の説明を、判断の `summary`・`locations` と突き合わせる。同じ指摘だと確かめられなければ未調査として扱う |
| `uninvestigated`（未調査） | 調べる。「前に誰かが見ているはず」で飛ばさない |

記録の外の候補は判定の対象外なので、前回の本文を読んで決める。前回の結論を変えるときは、新しいコメントで前回のどの判断を補足・訂正するかを書き、前回のコメントは編集しない。すべての候補が調査済みで前提も変わっていなければ、新しいコメントは投稿せず、前回のコメントの URL を報告する。

### 3. 調べるコミットを固定して調べ直す

- 調べるコミット: open の PR は現在の PR head（`headRefOid`）、マージ済みの PR は fetch した既定ブランチの最新、未マージで close された PR は最後の PR head と既定ブランチを区別する（既定ブランチに入っていない変更を、稼働中の問題として扱わない）。完全な SHA で固定し、元のレビューの HEAD と混ぜない。対象のファイルが元のレビューの HEAD から変わったかを `git diff --stat <元の HEAD> <調べる SHA> -- <パス>` で確かめる
- 対象のコード、呼び出し元・呼び出し先、既存のガード、関係する仕様と文書、テストを読む。指示の食い違いは、抜粋ではなく実際に渡される完全なプロンプト・設定の受け渡しまで追う
- 挙動を確かめるときは隔離した worktree で再現し、通常の入力・別の形式・境界・失敗の経路を試す。`target_root` が null（制作物など）なら、先に `review_target.py clone --target "$SESSION/target.json" --dest "$REPRO_ROOT/repo"` で一時 clone を作って `TARGET_ROOT` にする

  ```bash
  REPRO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/review-carry-over-followup.XXXXXX")"
  git -C "$TARGET_ROOT" worktree add --detach "$REPRO_ROOT/worktree" "$INVESTIGATED_SHA"
  # 終わったら: git -C "$TARGET_ROOT" worktree remove --force "$REPRO_ROOT/worktree" && rm -rf -- "${REPRO_ROOT:?}"
  ```

- 確かめ方を区別して書く: コードを読んだ、fake のコマンドで実行した、実際の sandbox で実行した、実際のモデルで E2E を実行した。プロンプトを組み立てられたことは、モデルが意図どおりに動く証明ではない。未実行・テストの失敗・環境の不足を「再現しない」「影響なし」にしない

### 4. 事実と重要性を分けて判断する

候補ごとに、確かめた事実（成立する条件、通常の利用で起きるか、影響、頻度、回避策、安全性・データ損失の経路）と、Issue で追跡する重要性を分けて書く。結論は次のどれか 1 つ。判断の観点と型ごとの例は [references/judgement.md](references/judgement.md) にある。

| 結論 | 条件 |
| --- | --- |
| 新しい Issue | 通常の経路での停止・誤った結果、データ損失や具体的な安全性の問題、繰り返す運用上の支障など、継続して管理する理由を示せる。元 PR より前からの問題も対象。原因が確定していなければ、確定したバグと書かず、調査 Issue にする |
| 既存 Issue に集約 | open の同じ問題の Issue がある。書き込みは「書き込みの範囲」の条件に従う |
| 理由付きの見送り | 上に当たらない。成り立つ問題でも、見送りは解消済みではないので、未修正のまま残ることを書く |
| 確認不足・障害 | 判断に必要な事実を確かめられない。足りない確認を書く。根拠の無い見送りにしない |

- 重大度の表記（Low・Info）、元 PR で Skip だったこと、PR 修正の採用の基準（H・D1・N）を、そのまま起票・見送りの理由にしない。「小さい修正」「文書だけ」「複数のレビュアーが指摘」「直すのが簡単」だけでは起票しない
- 既存 Issue は open・closed の両方を、言い方を変えた複数の検索で探す（`gh issue list --repo "$PR_REPO" --state all --search "<語>"`）。closed という理由だけで再作成・reopen しない
- open の PR で、`skip` の判断を見直す新しい根拠（`.agents/review-records.md` 8.2 節）を得たら、判断の記録は書き換えず、PR での再判断には `review-fix-planning` の実行が必要だと報告する。利用者に求められても記録を書き換えず、自分で `review-fix-planning` を始めない
- 秘密が今も公開されている（公開の artifact・ログなど）と分かったら、投稿の前に利用者へ知らせ、無効化・公開物の削除を先に求める。公開の Issue・コメントには、取得の手がかり（どの artifact・run か）を書かない

### 5. 本文を書き、投稿の前に検査する

本文は [references/templates.md](references/templates.md) の形で `$SESSION` のファイルに書く。事後調査コメントの 1 行目はマーカー `<!-- review-carry-over-followup:v1 -->`、2 行目は `## 持ち越し候補の再調査（<モデル識別子>）` にし、判断 ID は `fold` の `id` と同じ完全な形（`<ファイル>#<ID>@<HEAD の先頭 12 桁>`）で書く。モデル識別子はランタイムが示す正確な値にする。

投稿する本文ファイルごとに、次を同じシェルで実行する。どれかが失敗したら投稿しない。

```bash
check_body() (
    set -euo pipefail
    body="$1"
    scan="$(mktemp -d)"
    trap 'rm -rf -- "${scan:?}"' EXIT
    printf '[extend]\nuseDefault = true\n' > "$scan/config.toml"
    : > "$scan/ignore"
    env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML gitleaks dir --no-banner --redact --ignore-gitleaks-allow \
        --config "$scan/config.toml" --gitleaks-ignore-path "$scan/ignore" "$body" >&2
    if rg -n -e '<!-- review-fix-planning:' -e '<!-- review-records:' \
        -e '/home/' -e '/workspaces/' -e '/tmp/' -e '/root/' -e '/Users/' "$body" >&2; then
        printf 'check_body: 記録のマーカーかローカルの絶対パスがある\n' >&2
        return 1
    fi
    [ "$(wc -m < "$body")" -le 65536 ]
)
```

環境変数の値（`HOME=…`・トークン）やログの全文を貼らない。必要な行だけを選び、パスはリポジトリからの相対パスか `$TMP` のような説明にする。

### 6. 投稿し、読み戻す

順序は、新しい Issue → 集約先の Issue へのコメント → 元 PR への事後調査コメント（作った Issue の番号を入れてから）。`gh` には毎回 `--repo "$PR_REPO"` を付ける。

```bash
gh issue create --repo "$PR_REPO" --title "<タイトル>" --body-file "$SESSION/issue-1.md"
gh issue comment <番号> --repo "$PR_REPO" --body-file "$SESSION/issue-<番号>-comment.md"
gh pr comment "$PR_NUMBER" --repo "$PR_REPO" --body-file "$SESSION/followup.md"
# 返された URL のコメント ID で読み戻し、送った本文と完全に一致することを確かめる
gh api "repos/$PR_REPO/issues/comments/<コメント ID>" | jq -e --rawfile sent "$SESSION/followup.md" '.body == $sent'
# 判断の記録の読み取りが変わっていないことを確かめる（rounds・decisions・ignored_comments）
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" \
  | uv run --no-project python "$PARENT/.agents/scripts/review_records.py" fold --repo "$PR_REPO" --pr "$PR_NUMBER" \
  | jq -S '{rounds, decisions, ignored_comments}' | diff - <(jq -S '{rounds, decisions, ignored_comments}' "$SESSION/fold.json")
```

Issue は `gh issue view <番号> --repo "$PR_REPO" --json url,state,body` で読み戻す。認証・権限・通信の失敗（例: `HTTP 403`）は、投稿済み・見送り済みと報告せず、未完了として残す。別のトークン・別の経路・別のリポジトリで投稿し直さない。投稿できなかった本文ファイルは残し、完全パスを報告する。

### 7. 報告と後片付け

報告には、候補ごとの結論、作った・参照した Issue と PR コメントの URL、調べた SHA、実行した確認、確かめていないこと、未完了の書き込みを書く。open の PR で `review-fix-planning` の再判断が要る候補もここに書く。自分で作った worktree と `$REPRO_ROOT` を片付け、`$SESSION` は報告に使い終えてから消す。消せないもの・残したものは完全パスを報告する。

## 既存スキルとの分担

| スキル | 分担 |
| --- | --- |
| `pr-review` | 指摘を探す。このスキルは PR 全体の再レビューを始めない |
| `review-verify` | コミット済みのコードで主張を確かめる。採否と GitHub への投稿は、このスキルが自分で行う |
| `review-fix-planning` | 現在の PR での採否・修正・最終化。判断の記録と状況コメントを書く。このスキルはそれを書き換えず、`upsert_pr_comment.py` も使わない |
| `pr-review-fix-loop` | このスキルを自動では組み込まない。持ち越しの事後調査は明示した依頼で行う |

## ランタイムごとの違い

本体は `.agents/skills/review-carry-over-followup/` の 1 つだけで、Claude Code は `.claude/skills/review-carry-over-followup` のリンクから同じ本文を読む。Pi・Codex・Devin は `.agents/skills` を読む。違うのはモデル識別子の取り方だけで、Pi では `pi-model-name` で `PI_PROVIDER` と `PI_MODEL` を確かめる。
