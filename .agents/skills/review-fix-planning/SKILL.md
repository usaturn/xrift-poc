---
name: review-fix-planning
description: "レビュー結果ディレクトリ・PR番号・PR URLを指定して、指摘の採否や修正計画、複数レビューの統合、レビュー後の実装・反映を依頼されたときに使う。全件確認して既存PRへの修正push、修正不要の最終化、具体的な障害による未完了まで進める。新規PRのレビューや、親Issueから子を選ぶ作業には使わない。"
---

# レビュー後の判断・修正・最終化

1. 全指摘を現行のコミット済みHEADで確認し、妥当性とこのPRでの採用を分ける。
2. 推奨案で続行する。通常の設計判断・Spec/Plan完成は確認待ちの理由にしない。未実施の人間の承認を記録しない。
3. H・D1の必須修正、名指しN、明示選択されたHoldだけを採用する。包括的委任はNやHold選択ではない。
4. 自己レビューの呼出は1回だけ。未確認を成功にせず、権限・環境・外部操作の具体的な障害だけを未完了として報告する。

明示された「計画だけ」「マージ禁止」「編集禁止」は優先する。その制約により終点へ進めない場合は、何の認可が足りないかを `stopped` に残す。通常判断の承認待ちを新しい終点として増やさない。

## 通常入口

親checkoutから、第1引数（前後の空白を除いた値）を `ARG` にする。入力はレビューディレクトリ、親originのPR番号（`#<number>` / `<number>`）、任意の `usaturn` repositoryのPR URL。

```bash
TOPLEVEL="$(git rev-parse --show-toplevel)"
TARGET_JSON=$(uv run --no-project python "$TOPLEVEL/.agents/scripts/review_target.py" review-dir "$ARG" --for-planning)
```

- JSONの `.review_dir` → `REVIEW_DIR`、`.repository` → `PR_REPO`、`.number` → `PR_NUMBER`、`.url` → `PR_URL`、`.target_root` → `TARGET_REPO_ROOT`。repoをディレクトリ名から独自に推測しない。helperは親の `TOPLEVEL` から使う。
- 番号/URLは親main rootの `reviews/<owner>/<repo>/#<number>-*` だけを探索する。1件なら採用。無し（exit 8）は保存レビュー無し、複数（9）は完全パス一覧を報告し対象指定が不足したpermission障害とする。新規レビュー・修正を始めない。
- レビューディレクトリのパスの識別・head branch不一致はexit 11で停止。
- exit 10は対象外owner、3〜7は存在・アクセス・認証・API・親rootの障害。stderrと具体的な不足を報告する。API失敗を「PRが存在しない」にしない。
- `TARGET_REPO_ROOT` は親のPRなら親のcheckout、制作物や他repoのPRならnull。nullなら下の再現・実装の前に一時cloneを作って使い、`items/<name>`・`worlds/<name>` のcheckoutは使わない。対象repositoryのroot/remoteを確認してから検証する。入力helperの成功はcommit/push/mergeの認可ではない。
- 任意オプション: `hold-action=fix|skip`（通常入口ではHoldだけへの利用者の明示選択）、runnerのtemplate/configと `implement` / `self-review` / `fix-check` の割当。空欄は設定どおり、未指定を名指し・選択に作り替えない。役割CLIは `references/execution.md`。
- pr-review-fix-loop から呼ばれた場合は、helper が返した `planning.hold_action` をその run の実効方針として Hold にだけ適用する。ループの既定 `skip` を利用者の明示選択と記録しない。保存値が null の旧 run は未選択のまま扱う。単独の通常入口では、未指定の Hold を `skip` に補わない。

## 1. 入力を全件読む

runtimeのTask/Todo、無ければPlanのチェックボックスに作業順を置く。

1. `REVIEW_DIR` 直下の全 `<model>.md` を読む。`findings/`（候補）と `verified/`（確認済み）はレビュアーではない。レビュー成果はgitignored/未commitでも、このディレクトリ内なら有効な入力。
2. ファイル名、レビュアー行のmodel/runtime、最後のReview historyのHEAD/Mode、Merge recommendationを表にする。旧形式はファイル名・runtime=unknown・理由を記録。HEADの違う版を黙って混ぜない。
3. PRの現行HEADとbaseをGit/ghで確認し、前の判断を読む。

```bash
gh api --paginate --slurp "repos/$PR_REPO/issues/$PR_NUMBER/comments" \
  | uv run --no-project python "$TOPLEVEL/.agents/scripts/review_records.py" fold --repo "$PR_REPO" --pr "$PR_NUMBER"
```

壊れた記録はコメントIDと問題を報告する。認可無しで削除・除外・無記録投稿に切り替えない。次のサイクルは最大 `round` + 1、無しなら1。古いApproveは現行HEADを承認しない。古いレビュアーの判断済み指摘は継承、未判断だけ現行HEADで確認する。

4. `.agents/review-findings.md` の一覧を検査する。現行 `verified/<HEAD12>.json` が無い、不正、inputsが今回の再実行レビュアーと不一致、またはレビューより古い場合は、司令塔が `review-verify` の通常cycle入口を実行して作り直す。調査→確認→判断を別agentへ順に丸投げしない。
5. `confirmed` は採否へ、`not_reproduced` は根拠付きResolved、`unverified` は採否が不足事実に依存するときUnverified、それ以外はSkip。reviewerの推奨文より一覧の確認結果を使う。一覧が扱わない必要事実は自分で補う。
6. **全指摘、全変更ファイルの全文、関連する呼出元・呼出先・テスト**を読む。差分行だけで条件・例外・通常経路を推測しない。範囲、実行コマンド、結果、未読/未実施を記録する。`Needs manual verification` の理由も全件判断に対応付ける。

## 2. コミット済みコードを根拠にする

対象source/testは `git show <HEAD>:<path>` で読む。他worktreeの未コミット差分を解消済み・仕様・再現の根拠にしない。baseとの比較SHA、introduce/surfaceの根拠、変更の種類も記録する。レビュー成果を読む規則とは別。

実行が必要なら、現在の確認HEADを固定した自分の一意なworktreeで行う。rootがnullなら `REPRO_ROOT` 内へ先にcloneする。from/toを引数として分ける。

```bash
REPRO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/review-fix-planning.XXXXXX")"
WT="$REPRO_ROOT/worktree"
# TARGET_REPO_ROOT が null の場合だけ:
# uv run --no-project python "$TOPLEVEL/.agents/scripts/review_target.py" clone --target "$TARGET_JSON" --dest "$REPRO_ROOT/repo"
# TARGET_REPO_ROOT="$REPRO_ROOT/repo"
git -C "$TARGET_REPO_ROOT" worktree add --detach "$WT" "$REVIEWED_HEAD"
test "$(git -C "$WT" rev-parse HEAD)" = "$(git -C "$TARGET_REPO_ROOT" rev-parse "$REVIEWED_HEAD^{commit}")"
```

SHA不一致を証拠に使わない。検証ログ/中間ファイルは全部 `REPRO_ROOT` 内に置き、/tmp直下の固定名や独自のハイフン名を使わない。認証/環境の例はfakeコマンドとワンショットの一時HOME、Pythonは3.14以上。未実行・タイムアウト・失敗は「再現しない」ではない。

全必要確認が終わったら、Spec待ちにせず登録解除→親ディレクトリ削除の順で片付ける。後で再確認するなら新しいmktempを使う。

```bash
git -C "$TARGET_REPO_ROOT" worktree remove --force "$WT" && rm -rf -- "${REPRO_ROOT:?}"
```

worktree remove失敗時は親を消さない。登録だけが残る削除に切り替えず、WT/REPRO_ROOTの完全パスを報告して残す。第三者のworktree/ログは消さない。残す証拠はSpecや親main rootのagent scratchへ移す。

## 3. 採否を決める

全件を `references/decisions.md` で分類する。最低限の境界:

| 状態 | 採用条件 |
| --- | --- |
| Fix（必須H） | 現行で成立、PR起因、Critical/High。安全性3項目の根拠ある再評価も含む |
| Fix（必須D1） | 実行再現、通常の入口/入力/環境、停止/誤結果、PR起因の4条件全部 |
| Fix（追加N） | 利用者がこの指摘を名指し。必須と分ける |
| Hold | PR起因、コミット済みコードで確定した数行の潜在バグだけ。明示hold-actionを適用 |
| Unverified | 成立すればH/D1だが、必要な確認がまだ不足 |
| Resolved | 現行で解消したことを確認済み |
| Skip | それ以外。理由・見直す条件・必要なら持ち越しを残す |

**Skipが既定。** 小さい修正、文書、追加テスト、票数、時間、包括的委任は採用理由ではない。意図する挙動の質問はSkip+Specの一行で、Holdや質問待ちにしない。既存Critical/Highは影響・持ち越し・人の要確認をコメントと会話に残すが、このPR起因のFixに変えない。

Unverifiedは必要確認を自力で続ける。Skip/Resolved/Holdへ隠さない。明示Hold選択が無い場合は、通常判断の質問待ちではなく `permission: <ID> のhold-action選択が無い` と未完了に残す。Hold=skipなら理由付きSkip、Hold=fixなら追加Fix（`Hold:`）で、Skip/Unverified等には適用しない。

## 4. SpecとPlanを完成させる

`superpowers:brainstorming` で経路を分類し、推奨案を選ぶ。すべての経路にSpecを残す。利用者から委任された通常判断は自分で決めて続ける。仕様・範囲・名指し採用の認可を捏造しない。

親main rootは `uv run --no-project python "$TOPLEVEL/.agents/scripts/agent_tmp.py" root` で得る。Spec/Planはその `docs_draft/superpowers/` に保存し、commitしない。feature worktree、制作物、他repoのclone、`docs/superpowers/`には置かない。

Spec: `YYYY-MM-DD-<topic>-review-fixes-design.md`。全指摘と出典→判断対応、現行/base SHA、変更種類、重大度と再評価、状態/基準/理由、継承/変更/新根拠、未確認、Hold選択、必須と追加Fix、持ち越し、全読/実行範囲を残す。各Fixに原因/性質/同種範囲/検索結果、手法、採用しない変更、全violates変種・inconsistent記述の対象/受け入れ基準を書く。uncheckedなら不足と理由を書く。使用一覧、verdict件数、レビュアー表、plannerの正確なモデル、fix-checkの回数/ファイル/指摘、状況コメントURL、結果/停止理由も追記する。

`superpowers:writing-plans` でTask別の担当ファイル・操作・受け入れコマンド・テスト要否/理由を書く。テストの採否は **AGENTS.md「テストの採用基準」**を参照し、文書の文言照合や運用値の写しを足さない。テストを省くTaskは実行/構造/行動比較等の代替確認を書く。追加の拒否には、受け入れ続ける実入力と挙動テストを挙げる。既存の仕組みの削除/統合と、新しい分岐追加を比較する。

推奨案・理由・代案・委任内で決めた範囲・停止条件を記録する。Spec/Planを出しただけで人間が承認したとは書かない。実装済みの再開でも採否・HEAD・必要確認を照合し、未確認を飛ばさない。

## 5. 設定役割で実装し、1回レビューする

`references/execution.md` を読み、既存runnerの `fix.implement` を通常requestで起動する。割当は設定・明示上書きのまま。selfなら司令塔が同じ完全依頼を実行する。外部担当は独立worktreeに限定し、元指摘/性質/Spec/Plan/担当ファイル/受け入れ条件を渡す。採否・統合・pushは司令塔が担当する。

1. 対象PRがopen、計画HEADに基づくことを再確認。移動範囲は再確認し、証拠を再利用できなければ完了扱いにしない。新しいreview-fixes branch/PRを作らない。
2. 採用したFixだけを実装。挙動を守るテストはRED→GREEN、文面変更は代替確認。問題は原因を調べて最小修正し、関連検証とrepository規則の正式入口を実行する。落ちたままcommit/pushしない。
3. fetchして対象branchの先端を照合し、既存PRのhead branchにローカルcommit。基本は1サイクル1commit。自己確認で必要な追加commitは許容するが、commitの書換え・force pushで減らさない。
4. `run_roles.py --phase self-review` **1回**でPR全体のローカルcommit再レビュー（pr-reviewの通常指定commit入口）と修正確認（review-verifyのfix_check）をconcurrency=2で動かす。再レビューは初回検出base→local HEAD、修正確認はpush済みHEAD→local HEADで、範囲が異なる。全入力/全変更ファイル全文/関連コード/関連テストを確認する。設定がself/混合なら未実行の依頼を司令塔が実行する。
5. 起動前に司令塔がv2 planningの仮の版を `REVIEW_DIR/verified/<source HEAD12>.planning.json` に作る。必須キーを全部入れ、`outcome` は仮に `stopped`、`comment_id` はnull、`activities` は実施済みの工程だけ（`pre_push_review` は入れない）。`stage=pre_push_review` の呼出枠1件は `run_roles.py` が起動前に予約する。手で書き足さない（予約済みとして拒否され、その枠は消せない）。この件数を回数として記録する。失敗/中断でも消さず、別モデル・別HEAD・別phase名で2回目を起動しない。サイクルの最後に同じファイルを更新し、予約を消さない。fix-checkの `review_dir` は `references/execution.md` の独立staging（REVIEW_DIRではない）。
6. receiptを読み、実測identity/role/from/to、stop_confirmed、outside_changes、保存先監査、JSON/pair、全残件を確認する。runnerのsuccessは修正合格ではない。fix-checkの既存acceptを通し、selfの結果も正式validatorで確認する。未停止を成功とせず、他担当の変更を削除しない。
7. 指摘を同じ採否規則で判定。H/D1必須Fixは必ず対処し、名指しN/明示Holdも維持する。Skipまで直すループにしない。問題は司令塔が自力で診断・関連検証まで進める。修正でレビューの根拠が失われて最終HEADを確認できなくなる場合は、2回目で埋めず `permission: 1回枠を消費済みで追加レビューの認可が無い` と未完了にする。
8. 全条件が同じ最終HEADで揃ったら、通常pushで既存PRへ反映しremote HEADを読み戻す。拒否は原因を調べる。rebase/mergeがレビューHEADを変えるなら無条件再pushしない。open PRが無ければPRを増やさず、状況投稿もせず障害を報告する。

`fixes_pushed` はレビュー/修正確認を完了してpushし、状況記録も残した結果。これをマージ完了と呼ばない。自己レビュー無し・2回目・未確認のままpushは禁止。finishing-a-development-branchのメニューや2度目のcommit/push確認は不要。

## 6. 3結果で終了する

| `outcome` | 終点 |
| --- | --- |
| `fixes_pushed` | 採用した修正、関連/必須検証、1回のPR全体レビューと修正確認を完了し、既存PRへpush、状況投稿を確認 |
| `no_fix_needed` | Fix/Hold/Unverified無し、全件精読/関連確認/正式検証、最終状況投稿、同HEADのマージ確認、関連Issue更新まで完了 |
| `stopped` | 自力で解消できないpermission/environment/externalの具体的障害。未完了項目・現在HEAD・local commit・未push/マージ状況・再開条件を報告 |

修正不要なら短いSpec/Planを保存し、空commit・新機能・新レビューを作らない。正式検証のhead付き実行記録を作り `scripts/finalize.py` を使う。PRの全コメントに照合した状況投稿後、CI green・draft/mergeable/HEADを再確認し、match-head-commitでマージ、MERGED/head読戻し、同repoのclosingIssueを更新してCLOSEDを確認する。admin/auto mergeやbranch削除はしない。すでに同HEADでマージ済みなら、このサイクルの自分のno_fix_needed記録が投稿済みのときだけ二重マージせずIssue更新を復旧する。記録が無い・食い違うまま手動マージされた場合はIssueを更新せず `merged: true` の `stopped`（マージ後は投稿できない。PR上の記録が無い・stoppedなら再実行でも解消しない）。結果が `wait_for`（`ci`: CIの実行中・待機中、`mergeable`: GitHubがマージ可否を計算中）付きの `stopped` なら、`monitoring-long-tasks` で期限を決めて待ち、同じ記録・本文・実証ファイルで再実行する。期限を過ぎても解消しなければ `stopped` として報告する。操作要求/CLI exit0だけでは終了しない。マージ後のIssue更新失敗も `stopped`（マージ済みであることは別に報告）。

新しい記録の停止は `stop_reason: "permission|environment|external: <具体的障害>"`。権限不足/明示認可不足、実行環境/正式検証不能、認証/通信/API/競合状態等を区別する。単なる通常判断・時間を使ったこと・計画完成は障害ではない。古いawaiting_approval/stopped_*記録はparserで読み、履歴を変換しないが、新実行の終点として書かない。

## 作業終了前のPR投稿

`references/round-records.md` と `.agents/review-records.md` に従い、**新規はreview-records/v2**、planning/確認済み一覧もv2で実測provenanceを残す。不明はunknown、selfを他担当の成功にしない。識別情報の測定は既存runner/helperの観測を使う。

- 全結果で状況投稿が必要。fixes_pushedはpush後、no_fix_neededは最終化前の採否/検証状況、stoppedは判明内容と未実施を記す。PRがopenでない等helperが拒否する場合は投稿済みとせず、ローカル記録/失敗理由を報告する。
- 本文は日本語の3見出し、Skip全件のID・理由・見直す条件を省略しない。「今回の方針で追加修正する項目はない」と書き、Skipまで解消済みにしない。次欄にはレビュー/マージ/Issue等の実状態を記す。
- `MODEL_NAME` はruntimeの正確な識別子。Piではpi-model-nameでPI_PROVIDER/PI_MODELを両方確認。片方でも無ければpermission障害として確認を求め、猜測で投稿しない。

```bash
TOPLEVEL="$(git rev-parse --show-toplevel)"
HELPER="$TOPLEVEL/.agents/skills/review-fix-planning/scripts/upsert_pr_comment.py"
uv run --no-project python "$HELPER" --pr "$PR_NUMBER" --repo "$PR_REPO" --model-name "$MODEL_NAME" \
  --records-file "$ROUND_JSON" <<EOF
## review-fix-planning（$MODEL_NAME）

対象 PR: #$PR_NUMBER（\`$HEAD_BRANCH\`） / サイクル: $ROUND / レビュー対象 HEAD: \`$REVIEWED_HEAD\` / 現行 HEAD: \`$HEAD_SHA\`

### レビュー状況
...
### 作業判断
...
### 作業結果
...
EOF
```

helperは記録/全コメント/対象PR/open/head/自身のサイクルを照合する。拒否後にrecords無し・手書きgh投稿へ切り替えない。`--edit-last`、別agent/別サイクルの上書き、同サイクルの重複は禁止。正しい記録で再検査できる障害なら自力で直し、権限/API/競合等なら具体的な停止を報告する。

planning.jsonへ判定/実装/自己レビュー/修正確認/待機の実時間、活動担当、fix-checkファイルと件数、コメントID、最終結果を残す。call枠の件数と実作業時間は別物。helper終了後に残すSpecにはURL/担当/回数を写す。

## ランタイムごとの違い・移植

本体は `.agents/skills/review-fix-planning/` 1件。Claudeは `../../.agents/skills/review-fix-planning` への相対リンク、Pi/Codex/Devinも同じ日本語本文を読む。別ランタイムに版を複製しない。Task機能/モデルの測定/runnerの設定だけをruntimeに合わせ、採否・3結果・1回制限は共通。移植の依存は `references/porting.md`。
