# 通常requestによる役割実行と最終化

`TOPLEVEL` は読んだskillのsource checkout root、`TARGET_REPO_ROOT` は対象commitを持つcheckout。helperを制作物や外部repoから探さない。Python 3.14+、既存tools/devenv projectを使う。設定loaderの優先順はtemplate→personal→明示assign、fallbackは維持する。存在しないagentや投影できない同モデル制約をselfへ黙って変更しない。

## 1. 修正の依頼

採否は司令塔が決め、通常JSONを作る。以下の5項目以外は拒否される。

```json
{
  "repository": "/absolute/target-checkout",
  "source_head": "1111111111111111111111111111111111111111",
  "revision": "1111111111111111111111111111111111111111",
  "pr": {"repository": "usaturn/xrift-poc", "number": 900},
  "prompt": {
    "implement": "採用したFixの全文、守る性質、Spec/Plan、担当ファイル、受け入れコマンド、境界、結果をここに置く",
    "self-review": "同じPRのローカルcommitを通常pr-reviewで全体再レビューし、保存/cleanupまで確認する完全依頼",
    "fix-check": "この修正の主張・全文ファイル・関連経路/テストを確認する完全依頼"
  }
}
```

`repository` は絶対パス、両SHAは完全なコミット。実装時のrevisionは着手するcommit。自己レビュー時は全ローカル修正をcommit済みにしてrevisionをそのHEADにする。source_headは元のpush済みHEADで、途中で別HEADへ置き換えて呼出回数をリセットしない。promptは選択するroleだけが必須。各依頼を自己完結させる:

- 原指摘の参照/全文、妥当性と採用の根拠、守る性質と具体的な不具合入力。
- Spec/Planの採用Fix節、同原因範囲/検索結果、violates/holds/inconsistent、変更対象としない対象。
- 実行する関連/正式検証コマンドと、Taskごとのテスト要否/理由。AGENTS.mdを定義元として参照する。
- Request.work_dir内だけの対象編集、他worktree/未commitの使用禁止、nested agent/予約ref/Issue/PR更新/push/merge禁止。
- 結果JSONに実行した操作、実commit、検証argv/HEAD/exit、未完了、残存パスを返す。実測できないidentityはunknown。実装者が司令塔の成功receiptを自作しない。

```bash
uv run --project "$TOPLEVEL/tools/devenv" --locked python \
  "$TOPLEVEL/.agents/skills/review-fix-planning/scripts/run_roles.py" \
  --phase implement --request "$ROLE_INPUT" --output-dir "$NEW_IMPLEMENT_RUN"
```

template/configを指定できる。例えば `--assign implement=self`。selfなら外部process/worktreeを起動せず、roles.jsonのself_prompts.implementを司令塔が実行する。外部成果は実Request/Resultのrole/agent/commit/work_dirと照合し、停止確認後に対象差分だけを確認して既存PR branchへ統合する。応答successをテスト合格/commit成功とみなさない。既存PR以外のbranch/PRを作ってpushしない。

## 2. 一度だけのレビュー・修正確認

実装と関連検証・ローカルcommit後、2役割を1回のrun_workflowへ渡す（concurrency=2）。個別のfix-check dispatcherを追加起動しない。JSONに自己レビューの完全依頼とfix-checkの完全依頼を入れる。

### PR全体の自己レビュー

pr-reviewの `prepare` → 固定差分/全変更ファイル全文/関連コード・テスト → `complete` を使う。現在のPRのmerge-base→local revisionの全体差分であり、source_head..revisionの修正だけのレビューではない。`--review-commit` と必要な `--source-root` で未pushオブジェクトを指定する。`--from` / `--to` というpr-review引数を作らない。

```bash
uv run --project "$TOPLEVEL/tools/devenv" --locked python \
  "$TOPLEVEL/.agents/skills/pr-review/scripts/review_run.py" prepare \
  --pr "$PR_URL" --runtime "$RUNTIME" \
  --request "$REQUEST" --review-commit "$LOCAL_HEAD" --source-root "$SOURCE_ROOT" \
  --records-comments "$COMMENTS_JSON"
```

Requestはそのattemptの実request.json、source_rootはcommitを持ちoriginがPR repoと一致するcheckout。外部担当はrunner worktreeを対象codeのrootにし、旧別attemptを探さない。正確なmodel指定はpr-reviewのruntime手順どおり（Piの実PI_PROVIDER/PI_MODEL、Codexは省略）。selfではrequestを省略し、取得不能をunknownにする。prepareのdiff_command/base/HEAD、destination/state/draftをそのまま使い、live gh pr diffを混ぜない。prepareだけのprepared objectを「指摘なし」にしない。

自己レビュー担当にはpr-reviewの本体と `.agents/skills/pr-review/references/noninteractive.md` を読み、complete/fail/cleanupを通常入口で行うことまで明示する。出力はcompleteの実payload。司令塔がrunnerのstate/exit/stopとpayload.status、run/request/actual_agent/role、repo/PR/local commit、成果2件のschema/pair、active数、cleanup/残存pathを照合する。保存後の成果を直接修正せず、worker未観測値を設定から補わない。

### 修正確認の固定入力

`FIX_INPUT` は #562 の7項目だけ。repositoryはtoを持つcheckout、from=source_head、to=local revision、fix_idsは採用した元ID、fix_sectionsは採用変更/検証を含むSpec Fix節の全文。verified_fileは確認済み一覧の絶対パス、review_dirは専用保存先の絶対パス。既存parserでv1/v2を検査し、一覧のpr/source HEADをrequestと照合してから1回枠を予約する。request・fix_input・確認済み一覧・planningの取り違えや不正では、起動も枠の消費もしない。review_dirの状態、`--output-dir` の親、入れ子の起動は予約前に確かめないので、下の独立stagingの手順に従い、出力先は既存の作業ディレクトリの中に新しく作る。

PR再レビューの保存とfix-check監査が混ざらないよう、司令塔が自分の一意なscratch内に **独立したfix-check stagingレビュー領域**を作る。必要な確認済み入力だけをコピーし、input/保存先のsymlinkを使わない。stagingはreview_dirとして渡す正式な保存先で、外部担当はそこへ直接書かない。変更が観測されたら消去せず、acceptedに見せない。accept/validator後に、司令塔が成果2件を同じ内容でREVIEW_DIR/verifiedへ取り込み、要約mdを作る。既存成果と同名で異なる内容なら無断上書きせず止める。

```bash
uv run --project "$TOPLEVEL/tools/devenv" --locked python \
  "$TOPLEVEL/.agents/skills/review-fix-planning/scripts/run_roles.py" \
  --phase self-review --request "$ROLE_INPUT" --output-dir "$NEW_REVIEW_RUN" \
  --planning-file "$PLANNING_JSON" --fix-input "$FIX_INPUT"
```

任意: `--template <path>`、`--config <path>`、`--assign self-review=<agent|self>`、`--assign fix-check=<agent|self>`。役割は各1担当で明示fallbackだけ。review rolesのwrites=False/result_shape=jsonは必須。境界を保って投影できない設定はエラーで、同モデル除外を捨てたりfallbackを追加したりしない。

self-review・fix-checkには、runtimeが `dsh` の担当（`dsh-ccd`・`dsh-go` など）を一次担当にもfallbackにも割り当てられない。一次担当がselfでもfallbackを見る。loopはDSHのレビュアーに親側の準備をするが、run_rolesはしないので、DSHの担当は完了できずに1回枠だけを消費する。

- DSHの子はHOMEを隔離していてGitHubの資格情報を持たない。pr-reviewの `--runtime dsh` は、親が事前に取ったPRの情報とGitのbundleの記述子（`parent_pr_data`。`pr-review/scripts/review_run.py` の `prepare_parent` が作り、loopが呼ぶ）をpromptに入れたrequestを必須にし、completeは保存せずに `staged` を返す。保存は停止確認後に親が行う（同じファイルの `finalize_parent_batch`。loopが呼ぶ）
- 承認なしのDSHはbubblewrapの中で動き、書けるのは隔離したHOME・TMPDIR、runnerのworktree、`sandbox_write_paths` だけ。保存先・保存ロック（loopの `review_write_paths`）も、fix-checkが隔離したworktreeを作るときの共有の `.git` へのworktreeの登録も許可されない

run_rolesは投影の直後、requestの検査とGitの照会より前にexit 2で止まり、planningの予約、`--output-dir`、起動のどれも行わない（stderrに理由を出す）。DSHをself-review・fix-checkに使うには、run_rolesにloopと同じ親側の準備（prepare_parent・書き込みの許可・stagedの保存）を足す別の変更が要る。implementは対象外（runnerのworktreeの中に書く役割で、pr-reviewのDSHの手順を使わない）。

### 回数と受け取り

- PLANNING_JSONは司令塔が起動前に作る仮のv2 planning（SKILL.md 5節の手順5）。形は `.agents/review-findings.md` 6章のv2例と同じで、`outcome` は仮の `stopped`、`comment_id` はnull、`activities` に `pre_push_review` を入れない。共有定義の「サイクルの最後に書く」は、最後に同じファイルを更新することで満たす（共有定義は変えない）。
- run_roles.pyがpr/source_headを照合し、起動前に排他lockと一括置換で `activities.stage=pre_push_review` を1枠予約する。司令塔は手で予約しない。**この件数が自己レビュー呼出数**。同時/2回目は拒否。失敗/中断でも枠を戻さず、時間は未計測nullから始める。最後の更新でも予約の行を消さない。
- roles.json: request、実batch、self_prompts、review_changes。stdoutはstate/artifact_dir/pending_self/outside_changes/review_changes。exit 0は依頼の回収（selfを含む）、1は失敗/未停止/領域変更、2は入力/設定/起動失敗（run_roles自身の入力の誤りはstderrに理由を出す）。exit 0はレビュー合格でない。
- all-self/混合のself_promptsは司令塔が実行。実行していないものを完了にしない。レビューと修正確認の両方を回収する。
- `fix-check-receipt/dispatch.json` は既存dispatcherの固定request/verified/skill/time_limits/snapshot/verifierと、同じbatchからfix-checkだけを抽出した実結果。誰かが停止未確認ならaccept可能なsuccessを作らない。外部結果は次で受け取る。

```bash
uv run --project "$TOPLEVEL/tools/devenv" --locked python \
  "$TOPLEVEL/.agents/skills/review-verify/scripts/dispatch.py" accept \
  --run-dir "$REVIEW_ARTIFACT_DIR/fix-check-receipt"
```

acceptはstate/全attempt stop/outside changes/保存先監査、payloadがverified/inventoryだけ、道具の全候補/claims/termsの保持、fix_ids、schema/judgmentを確かめて成果2件を保存する。担当の「passed」だけを取り込まない。selfのdispatchはacceptへsuccessに変換せず、司令塔がpromptを実行し、既存review_findings.pyのvalidate（verified-fileとstatement-candidates）を通して保存する。自分のmodel/runtime/provenance、実時間を記録し、外部担当のidentityを捏造しない。

fix-check自身の指摘（sources空）は `fix-check#V-<n>@<to HEAD12>` として判断する。全候補をH/D1/N/Hold/Unverified/Skipで扱う。Skipだけならpushを妨げないが必要確認不足は成功にならない。必須Fixは司令塔が修正する。修正がレビューの根拠を失わせる場合、1回制限を守った最終HEAD確認ができず、追加レビュー認可不足としてstopped。2回目のfix-check/別model/別名phaseで上限を回避しない。

## 3. 修正不要の最終化

Fix/Hold/Unverified無しを全コメントに照合し、repository規則の正式検証を同じheadで実行する。issue-to-pr の `--merge` の run では、同じ run がPRの作成前に実行した正式検証の記録を使ってよい。条件は、PRのHEADが記録のheadと同じ、検証のときに作業ツリーに未commitの変更が無い、終了コード0、ログを親の `tmp/agents/` に残してある、のすべて。どれかを確かめられなければ、ここで実行し直す。実証ファイルの形:

```json
{"head":"1111111111111111111111111111111111111111","commands":[{"argv":["./scripts/ci/verify-devenv.sh"],"exit_code":0}]}
```

commandsは実行したargv/exitをそのまま記録する。helperはhead/形式/成功を照合するだけで、ファイルを作れば検証したことになるわけではない。既存no_fix_neededの記録JSONと、**マージ前の採否/検証状況**の日本語3見出し本文を用意する。マージ済み/Issue完了を先に書かない。

```bash
uv run --project "$TOPLEVEL/tools/devenv" --locked python \
  "$TOPLEVEL/.agents/skills/review-fix-planning/scripts/finalize.py" \
  --record-file "$ROUND_JSON" --body-file "$STATUS_BODY" --checks-file "$CHECKS_JSON" \
  --result-file "$FINAL_RESULT"
```

helperの順序: 全コメントの照合→既存投稿helper→HEAD/draft/mergeable/CI再確認→match-head-commit付きの通常のmerge commit（`--merge`。squash/rebaseは使わない）→MERGED/mergedAt/head確認→同repoのclosingIssuesReferencesだけclose/読戻し。どのrepoのPRもCI成功が必須（CI免除は無い）。admin/auto/delete-branchは使わない。対象が同headで既にMERGEDなら、このサイクルの自分のコメントの記録が渡したno_fix_needed記録と一致するときだけ、投稿/mergeを重複せずIssue更新の復旧だけを行う。コメントが無い・記録が食い違う（stoppedに置き換えた等）ならIssueを更新せず `merged: true` のstopped。

外部操作前に結果の一時保存先を確保し、結果ファイルのdirectory/symlinkは拒否する。既存の通常ファイルへの結果更新は一括置換する。result_fileのoutcomeがno_fix_needed、exit0で初めて最終化完了。exit1はstopped/stop_reason、mergedフラグと更新済みissuesも残す。待てば進む停止はstop_reasonがexternalのまま `wait_for` を持つ。`ci`（CIのCheckRunが未完了、StatusContextがPENDING/EXPECTED。失敗が混ざればwait_for無しですぐ止まる）は `gh pr checks <PR> --repo <repo> --watch` を `monitoring-long-tasks` で監視して待ち、`mergeable`（UNKNOWN）は少し間を置き、どちらも同じ記録・本文・実証ファイルで再実行する。投稿後に止まった場合も、再実行は同じサイクルのコメントを更新して続きから進む。期限内に解消しなければstoppedとして報告する。状況本文の不備やPR上の過去の記録との照合失敗は `environment:`（照合失敗は判断IDを添える）、gh/APIの失敗は `external:`。exit2は入力/結果ファイルの障害。副作用後の保存失敗はstdoutのstoppedにも実際のmerged/issuesを残すので、exit2だけから未操作と推測しない。同HEADのMERGEDを観測済みなら後続エラーでもその事実を維持する。Issue更新失敗時はマージ済みを隠さず未完了と報告し、同HEADの同記録で復旧する。moving HEAD、投稿拒否、CI不足、未停止/欠損成果、保存/cleanup失敗を「指摘なし」にしない。別repo/URL不一致のIssueを閉じない。

## 4. 既存PRだけに反映・後片付け

`TARGET_REPO_ROOT=null`なら実装の直前に一意cloneを作る。入力は通常review-targetを再解決し、old approval waitのシェル変数を信用しない。

```bash
LAND_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/review-fix-planning.XXXXXX")"
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_target.py" clone \
  --target "$TARGET_JSON" --dest "$LAND_ROOT/clone" --checkout-branch
```

fork拒否（exit12）を別のpush先で回避しない。修正commit→1回レビュー/修正確認→関連/必須検証→ `git -C "$LAND_ROOT/clone" push origin HEAD`、同じPRの状況投稿/読戻し。成功後だけ `rm -rf -- "${LAND_ROOT:?}"`。失敗時はlocal commit/worktree/cloneの完全パスを残す。review-fix用の第2PR、main直push、force push、勝手なbranch削除はしない。

全結果でSpec/Plan/planning/コメント/runner artifacts/未削除のscratchの絶対パスと実時間を報告する。第三者の資源を片付けず、消してよいと確認した自分のscratchだけ削除する。
