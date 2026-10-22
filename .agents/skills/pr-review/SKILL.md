---
name: pr-review
description: GitHub の PR 番号・URL を指定したコードレビュー、指定 commit や未 push commit の再レビュー、非対話実行器からのレビュー依頼で使う。PR の実装や指摘修正、レビュー結果の統合には使わない。
argument-hint: "<PR番号またはURL> [--review-commit SHA] [--source-root PATH] [--request PATH] [--records-comments PATH]"
allowed-tools: Read, Grep, Glob, Bash(gh *), Bash(git *), Bash(date *), Bash(uv run *), Bash(sha256sum *), Edit(//tmp/pr-review-*/review-backup/**), Write(//tmp/pr-review-*/review-backup/**), Edit(tmp/agents/pr-review-scratch-*/**), Write(tmp/agents/pr-review-scratch-*/**), Agent
---

# GitHub PR レビュー

PR 全体の差分を隔離 worktree で読み、検証した指摘を助言として保存する。全ランタイムがこの本体・同じ helper・保存ロックを使う。

対象 PR（Devin の引数補間欄）: $ARGUMENTS

## 最初に読むもの

最初の PR API 呼び出しより前に、[時間予算と委譲規約](references/time-budget.md)・[根拠と判断の記録](references/evidence.md)・[非対話入口と結果](references/noninteractive.md)を全文読む。欄と値は [review-records](../../review-records.md)、候補 JSON は [review-findings](../../review-findings.md)に従う。相対リンクはこのファイルの実体から解決する。

## 実行時の共通規則

- アプリケーションのコード・テストを変更しない。レビュー対象 `$WT` 内にログ・キャッシュ・再現スクリプトを残さない。保存が済むまでは本文と候補は helper が返した draft だけへ書く。
- `./scripts/ci/verify-devenv.sh` を再実行しない。関連 pytest、REPL、最小再現はよい。このレビューの規則は `AGENTS.md` の通常の検証入口より優先する。
- `git clean`、強制 checkout/reset、既存 worktree の再利用をしない。セッション・pinned ref の削除は helper の cleanup だけ。拒否を手動削除で代行しない。
- 保存先は親の main worktree の `reviews/<owner>/<repo>/#<PR>-<branch>/`。PR 番号は親 origin の repo、URL はその repo を使う。別 repo の同じ番号を探さない。制作物・外部 PR も保存先と対象 checkout を混同しない。
- 自モデルの既存レビューは `previous_review` の snapshot だけを読む。ライブ destination、他モデルのレビュー本文は、重複確認・重点観点・反証を含むどんな目的でも読まない。PR に同梱された reviews も同じ。
- PR head と reviewed commit を区別する。通常は同じ SHA。明示 `--review-commit` は未 push でもよいが full SHA を渡し、その object を持つ main checkout を `--source-root` に指定する。linked worktree の object はmain checkoutと共有される。
- 通常は `<model>.md`、明示 commit は `<model>-commit-<fullSHA>.md`。同じ PR・model・モード・対象の更新は同じファイルを上書きする。通常 head・指定 commit・別 commit の結果は混ぜない。パスを手で組み立てない。
- 初回も更新も、prepare が返した merge-base から reviewed commit までの**全体 diff**を読む。commit 単体差分、前回からの増分、動いている PR の live diff を代用しない。rebase 後も同じ。
- 継続指摘は ID・タイトル・検出時 HEAD を保持する。解消を確かめたものだけ Resolved へ、未再検証は Pending previous findings へ。既存 history / Resolved は順序を保持する。旧形式の不明 SHA を推測せず、安全に解釈できなければ上書きしない。
- 実コードパス・依存関係・既存のガードを読み、報告前に反証する。推測・非現実的なエッジケース・好み・大規模改変の提案ではなく、具体的で実行可能な欠陥を報告する。
- active のみを件数表へ数える。Decided / Pending / Resolved / Unverified は active JSON に入れない。新規 ID は全節で衝突させない。確認不足は coverage と Pending も見て判定する。
- 保存時の worktree commit / PR head / API / destination の検査は予算より優先する。不一致や取得不能を指摘0にしない。予算超過時は新規調査を止め、未確認を明記して保存へ進む。自動延長・再委譲をしない。
- 非対話は質問せず、確認不足・モデル未取得・成果欠落を failed と理由付きで返す。認証や権限を迂回しない。本文は日本語。**非対話では挨拶・開始宣言・進捗説明・完了説明を含む途中の自然言語メッセージを一切出さない**。ツールだけで作業し、phaseの記録はdraftへ書く。最後に1回だけcomplete/failのJSON payloadを返す（Devinは以下のJSONコードフェンスに載せる）。
- 必要な追加スクラッチは親 main の `agent_tmp.py mkdir pr-review-scratch-<PR>` で作る。セッション名 `pr-review-<PR>-*` を追加スクラッチに流用せず、session.json を複製しない。根拠を本文へ移し、不要と確認した自身の scratch だけを空値ガード付きで削除する。残す場合は完全パスを報告する（[AGENTS.md](../../../AGENTS.md)の保存場所を参照）。
- 補助委譲は time-budget の起動・期限・停止条件を全部満たす場合だけ最大1件。非対話 worker からの再委譲は禁止。停止を確認できない worker が WT を使用中なら complete / cleanup を呼ばず、失敗・停止状態・残存パスを返す。

## ランタイムごとの違い

- Claude Code の allowed-tools は許可確認用で、独自の保存手順ではない。`.claude/skills/pr-review` はこの実体へのリンク。
- Devin headlessでは途中のassistant説明もstdoutへ連結されるため、中間messageを抑止する。禁止指示だけで漏れを保証できないので、**最終complete/fail JSONを1つの `json` コードフェンスで囲む**。JSONの外に説明を付けず、途中にJSONフェンスを出さない。既存Devin adapterがこの形式をobjectとして回収するため、共有Request/Receipt/Resultやpayloadの内容は変更しない。Devin は冒頭の対象 PR 行だけを補間する。本文の `＄ARGUMENTS` / `＄1`〜`＄9` を説明やシェルの位置引数へ半角で書かない。空欄・ドル記号始まりは PR 未指定。モデルは現在のシステム提示 `You are powered by <MODEL>` を使い、過去 transcript や要求した設定を使わない。
- Pi は自身の bash で `PI_PROVIDER` と `PI_MODEL` の両方を確認する。どちらか空なら履歴の model_change・設定にfallbackせず失敗する。
- DSH 標準 headless は `--runtime dsh` で `--model-name` を省く。helper が自身の bash の `DSH_HOME`・`DSH_SESSION_ID`・cwd に結び付いた `session.v4.jsonl` を読み、直前の解決済み request header と assistant message source の provider/model の一致から確定する。effort は同じ header、CLI 版は未観測。`PI_*` や TUI 用の `DSH_MODEL` を要求せず、親の値・起動設定・他 session のログで代用しない。欠落・不一致・途中の経路変更は failed。保存名には provider を含め、本文には正確なモデル ID を使う。
- DSH は認証済み親が作った `Request.prompt` の `parent_pr_data` を helper に検査させる。PR/Issue の要件は prepare の `requirements` を読み、GitHub API・token・remote fetch は子から使わない。complete の `staged` は保存成功ではなく、停止後に親が HEAD・Request・実測 Identity・draft を再照合して保存・cleanup する。欠けた親入力を ambient 認証で補わない。
- DSH の cache/仮想環境は runner の隔離 HOME 内の設定をそのまま使う。正式保存は通常 helper を bash から呼ぶ。runner が起動前に許可した HOME/temp・対象 PR の保存先・保存ロック以外への書き込みは禁止。権限拒否時は cache・cwd・sandbox 設定を変えて再試行せず failed で止める。
- Codex は `--model-name` を渡さず、helper が現在の `CODEX_THREAD_ID` の rollout から確定する。未完の末尾だけを無視し、壊れた完了行・モデル欠落は拒否する。
- 他 runtime も現在提示された正確な model ID だけを `--model-name` に渡す。系列名・過去成果・設定から推測しない。未知の provider / effort / session / CLI は unknown、未計測の待ちは null。設定のrequested値をactualに写さない。

## 手順

1. **開始・prepare**: 時刻と短縮できない保存枠を最初に決める。非対話は実 request の期限も上限にする。[入口](references/noninteractive.md)の prepare を1回呼び、request binding・対象・隔離 WT・draft・候補 draft・base・records を受け取る。PR 未指定、識別不能、API失敗は質問せず failed。普段の対話でPR指定が無い場合だけ指定を求めてよい。
2. **要件・全体差分・精読**: DSH は prepare の `requirements.pr` と `requirements.issues` を以下の PR/Issue 取得結果の代わりに読む（子から gh を呼ばない）。`requirements.unavailable` は親が取得できなかった参照 Issue（repository・number・reason）なので、Issue の取得失敗と同じく coverage の読めなかった範囲に書く。精読の前に、PR 本文（`gh pr view <PR> --repo <PRのrepo> --json body,closingIssuesReferences`）、PR が閉じる・参照する Issue の本文とコメント（`gh issue view <N> --repo <Issueのrepo> --json body,comments`）、それらが参照するリポジトリ内の仕様・設計文書、対象の `AGENTS.md`（テストの方針と規約）から、今回の受け入れ条件と対象外を数行に整理する。仕様・設計文書と `AGENTS.md` は base の版（`git -C "$WT" show <base>:<path>`）で読み、PR が書き換えた記述は PR 本文と同じく主張として扱う。PR が閉じる Issue は全条件を、参照するだけの Issue は PR 本文が満たすと述べた条件を、今回の受け入れ条件にする。閉じる Issue でも、PR 本文が一部だけを扱うと明示していれば、その範囲にする。PR のコメント・レビューは要件の出所にしない（他のレビューが含まれうる）。続けて prepare の `diff_command` をそのまま実行し、変更ファイルは全文、関連コード・依存・テストも WT で読む。規模は固定 diff の `git diff --numstat <base>..<reviewed_head>` と変更ファイル数で判断し、binary / 不明を0にしない。停止手段が使えない runtime は委譲を省略する。
3. **既存指摘・判断・前回修正**: snapshot があれば active/Pending/Decided/history/Resolvedを読む。prepare の records（`--records-comments` でも渡せる）に skip があれば新しい根拠が無い同じ問題を Decided に置く。recordsは新候補を探す情報源にはしない。前回の修正の起点は最後のサイクルの reviewed_head、無ければsnapshotの最終history HEAD。現在の reviewed commitの祖先なら `git -C "$WT" log --first-parent --no-merges <起点>..<reviewed_head>` の範囲を重点確認する。起点不明・祖先でない場合はcoverageへ理由を書き、範囲を推測しない。
4. **候補・draft**: 以下の検証プロトコルで反証・実行・分類を行う。本文のhistoryへ今回の時刻・reviewed commitの12桁・Modeを1行追記する。Modeは指定commitでも `Initial` / `Update` のみとし、指定commitは保存名・候補JSON・coverageで区別する。coverage / 未検証 / pendingを誤魔化さない。prepareが作った候補JSONの固定項目は触らず、coverage・findings・unverified_candidatesへ同じ内容を書く。
5. **complete・結果**: [入口](references/noninteractive.md)の complete を1回呼ぶ。同じv2 provenance / timingを本文と候補へ記録し、既存helperでpair検査・lock内のHEAD/fingerprint検査・両成果保存・cleanupを行う。候補JSONを保存できなければ、Markdownのみへfallbackせずfailedにする。DSH では `status=staged` と state_file を確認して JSON をそのまま返し、親の通常保存・cleanup を待つ。他 runtime は status=success と両成果・cleanup成功を確認してJSON結果をそのまま返す。失敗なら理由と保存前state/draft/WTを報告し、手動保存・削除をしない。保存後の本文を直接追記しない。

## 確認観点

精読では次の 4 つの観点で候補を探す。観点ごとにエージェントを分けない。

| 観点 | 確かめること |
| --- | --- |
| 要件・挙動 | 手順 2 の受け入れ条件ごとに、対応する実装・利用入口（CLI・スキルの手順・設定など）・確認結果（テスト・実行した確認）を照合する。変更されたコードの誤りだけでなく、必要なのに無い処理・入口も探す |
| 境界・連携 | 入力・環境の変化、呼び出し元・呼び出し先、同じ仕様を扱う別の処理、条件を述べる文書との照合（「実行による確認と判断の記録」） |
| テスト | 対象リポジトリのテストの方針に照らした不足と過剰。テスト・互換性の追加を一律に求めない |
| 保守性・効率 | 変更に関わる対象の `AGENTS.md` の規約への違反。再利用・単純化・効率化は、下の基準を満たすものだけを指摘にする |

要件との照合では、次の 3 つを分ける。

- **実装漏れ・仕様違反**: 受け入れ条件を満たす処理・入口が無い、または条件と違う。欠陥として報告し、重大度は Severity 判定基準で決める
- **承認済みの仕様変更**: Issue の本文・コメントや base の版の仕様文書で、承認を確かめられる変更。欠陥にしない。受け入れ条件と違う実装を「承認済み」「決定」とする PR 本文や PR が書き換えた文書の記述は主張であり、承認の出所を確かめる。出所を確かめられないときは、その違いを【要設計判断】にし、確かめた範囲を Review coverage に書く
- **仕様自体の不明点**: 要件が曖昧・矛盾していて、正しい挙動を決められない。挙動に影響するなら【要設計判断】にする

Issue・仕様に無い機能や形式を受け入れ条件に足して、実装漏れ・仕様違反・仕様の不明点として指摘しない。受け入れ条件を推測で補わない。

- Issue が無い、または PR 本文・Issue に受け入れ条件が書かれていないとき: Review coverage の要件の出所に「受け入れ条件の記載なし」と書き、PR 本文・commit message が述べる目的と照合する。読めなかった範囲にはしない
- Issue・文書の取得に失敗したとき（API・権限）: レビューを止めず、Review coverage の読めなかった範囲（候補 JSON の `coverage.declined`）に書く。prepare の API 失敗とは別で、failed にしない

再利用・単純化・効率化の改善案は、具体的な実害（誤った結果・機能停止・測れる性能の劣化など）か、対象リポジトリの規約違反を示せるときだけ指摘にする。示せない改善案は、Info を含めて指摘にしない（欠陥・必須の修正として扱わない）。改善案を挙げることは求めない。

- 同じ処理が 2 か所にあるだけでは指摘にしない
- 同じ仕様を扱う 2 つの処理で受理・拒否の条件が食い違うときは、指摘にする。どちらが仕様どおりかを確かめ、両方に同じ入力を与えて実行する（実行できなければ【確認推奨】）

## 検証プロトコル

候補指摘 1 件ごとに、以下を順に行う:

1. **精読**: 指摘に関わる実コードパスを最後まで読む（途中の早期 return・ガード・呼び出し規約を見落とさない）
2. **反証試行**: 「この指摘が誤りだとしたらどこか」を明示的に検討する（既存のガード、テストによる固定、ドキュメント化された意図的トレードオフ等）。要件の指摘では、承認済みの仕様変更・対象外・別の場所での実装でないかも確かめる。反証に成功したら報告しない
3. **実行検証**: 実行時の挙動についての Critical・High・Medium の候補は、worktree 側のコードで最小再現を実行して確かめる（関連する pytest、REPL、最小再現スクリプト）。それ以外も、挙動が不確かなら実行する。`$WT` の中にファイルを作らず、一時ファイルは追加スクラッチに置く。テストの実行方法が分からなくても、最小再現スクリプトで確かめる。`./scripts/ci/verify-devenv.sh` は実行しない。外部からの入力・新しい作業環境を扱うコードでは、入力・環境の変化も試す（「実行による確認と判断の記録」）。一時ホームが必要な場合は `env HOME="$tmpdir" ...` のワンショットだけを使い、永続シェルへ `export HOME=...` してはならない（後続の `gh` が別ホームを見て未認証になる）
4. **同種箇所**: 欠陥を確かめたら、同じパターンを PR の差分の全ファイルで検索し、該当箇所を 1 件の指摘にまとめる
5. **期限確認**: phase 境界・呼び出し復帰時・委譲開始前・新規候補や実行検証の開始前に現在時刻を確認し、調査期限と委譲期限（絶対期限・実質無進捗 5 分）を再評価する。期限超過後は新規調査を増やさず保存へ進む

検証結果に応じて、各指摘に確度ラベルを付ける:

- **【検証済み】**: コード精読または実行検証で裏付けがある。ただし実行時の挙動についての Critical・High・Medium は、実行した再現があるものに限る（コードを読んだだけなら【確認推奨】）
- **【確認推奨】**: もっともらしいが裏付けが不完全（実行時の挙動についての Critical・High・Medium で、再現を実行できなかったものを含む。足りない根拠を書く）
- **【要設計判断】**: 挙動は確認済みだが、修正すべきか仕様とすべきかはユーザの判断が必要

## 実行による確認と判断の記録

挙動についての判断は、実行した結果で示す。手順と記入例は[指摘の根拠と判断の記録の規約](references/evidence.md)、欄の定義は形式の定義元 [review-records](../../review-records.md) の 4 章にある。

- **実行した再現**: 実行時の挙動についての Critical・High・Medium は、最小再現を実行し、`再現` にコマンド・場所・条件・結果を書く。実行できなかった、または再現しなかった指摘は【確認推奨】にし、`再現: 実行しなかった（<理由>）` と足りない根拠を書く
- **入力・環境の変化**: PR が外部からの入力（エージェントの出力、利用者の文章、ファイル、環境変数）を受け付けるコードや、新しい作業環境（新しい worktree、サブプロセス、一時ディレクトリ）で動くコードを含むときは、少なくとも 1 つの入力・環境の変化を試す（例: 複数行・空・非 ASCII の文章、区切り文字を含む値、仮想環境の無い作業ディレクトリ、submodule の状態の違い、空白を含むパス）。試せないときは、Review coverage の未実施の検証に理由を書く
- **「安全か」と「動くか」**: 拒否・停止を確かめたことを、通常の利用経路で目的の操作が完了する根拠にしない。「失敗時は安全側（停止側）に倒れる」だけを理由に、実環境で確かめていない挙動を問題なしとしない。Review coverage の「実行した確認」に、入口ごとに「<条件> → 動く／止まる」を書く
- **同種箇所**: 欠陥を 1 つ確かめたら、守るべき性質を 1 文にし、PR の差分の全ファイルで同じパターンを検索する。該当箇所はすべて同じ 1 件の指摘の `同種箇所` にまとめ、実際に実行した検索のコマンドを書く（実行していないコマンドを書かない）。別の指摘に分けない
- **同じ種類の別の入力**: 欠陥を確かめた性質ごとに、同じ性質に反しうる別の入力（同じ文法の別の書き方、同じ状態の別の値、境界の値）を挙げて実行する。報告された入力の形（例: 報告された 1 つの日時の書式）だけで性質を書かない。結果は候補一覧の `variants` に、確かめなかった入力も理由を付けて書く（[規約](references/evidence.md)の 2 章）
- **条件を述べる文書との照合**: PR が、受け付ける・拒否する状態や、動く・止まる条件を変えたときは、その条件を述べている README・スキル・`interfaces.rst`・ヘルプを検索し、両方向から照合する（文書が述べる条件を実装が満たすか、実装の新しい条件を文書が正しく述べているか）。結果は候補一覧の `statement_sites` に書き、食い違いは指摘にする
- **安全性の経路**: 資格情報・秘密情報が想定していない相手へ渡る、意図した範囲の外を書き換える・削除する、権限の確認や書き込み前の検査を迂回できる、のどれかに当たる指摘は、経路（入口・入力・環境と、渡る先・書き換える先、または迂回される検査）を `内容` と候補一覧の `failure_scenario` に書く。重大度を引き上げるかどうかは決めない（review-fix-planning の安全性の再評価だけで判断する）
- **判断しなかったもの**: 読まなかった範囲、確かめなかった観点・入力は、Review coverage と候補一覧の `coverage.declined` に、対象と理由を書く。無ければ「無し」と書く
- **前回の修正の重点確認**: 前回の修正の範囲（手順 3）がある場合は、その変更が新しく足した仕組み・拒否条件・規則の写しと、「同じ規則」「同等」などの主張を重点的に確かめる。規則の写しは写し元と同じ入力で比べ、主張は実行して確かめる
- **判断済みの指摘**: 判断の記録で `skip`（対応しない）の判断と同じ問題は、新しい根拠（形式の定義元 8.2 節）が無ければ active にせず、`## Decided findings`（テンプレートどおり `## Pending previous findings` の後、`## Test suggestions` の前）に書く。新しい根拠があれば active にして `見直しの根拠` を書く。表現の違い、別のレビュアーが同じ指摘を出したこと、重大度の表記だけの引き上げは、新しい根拠に当たらない
- **D1 の材料**: 実行時の挙動についての全指摘に `利用経路` と `結果の種類` を、全指摘に `PR 起因` を書く。採否（Fix・Skip）は決めない
- **未読範囲**: 更新時も PR 全体差分を読む。読めなかった範囲が残るときは、Merge recommendation を Approve にしない

## Severity 判定基準

| Severity | 基準                                                                                         |
| -------- | -------------------------------------------------------------------------------------------- |
| Critical | データ破壊・セキュリティ侵害・主要機能の停止が、通常の利用経路で発生する                     |
| High     | 実行時バグ・セキュリティ問題・破壊的挙動変更が、現実的な条件で発生する                       |
| Medium   | 特定条件下の実挙動不具合、互換性リスク、仕様未定義の挙動（設計判断が必要なものを含む）       |
| Low      | 実害は限定的だが対応が望ましい欠陥（依存下限未指定、稀なエッジケース、性能の二次的コスト等） |
| Info     | 実害なし。ドキュメント追随漏れ、スタイル逸脱、将来の再検討事項                               |

重大度は「影響 × 発生しやすさ」で判定し、迷ったら低い方に倒す（Severity インフレを防ぐ）。

## 指摘 1 件のフォーマット

各指摘は次のテンプレートに従う:

```markdown
### <ID>: <一行タイトル>【検証済み|確認推奨|要設計判断】

- 位置: `path/to/file.py:123`（複数箇所あれば列挙）
- 検出時 HEAD: `<12文字のshort-sha>` または `不明（旧形式）`
- 内容: 何が・どの条件（入力・状態・操作列）で起きるか
- 根拠: 読んだコードパス、実行した検証とその結果
- 再現: 実行した | 実行しなかった（<理由>） | 該当なし
  - コマンド: `<実行したコマンド>`
  - 場所: <PR head の隔離 worktree か一時ディレクトリと、そこに置いたもの>
  - 条件: <入力・環境>
  - 結果: <終了コードと出力の要点>
- 利用経路: 通常 | 限定的 | 不明 | 該当なし — <入口・入力・環境>
- 結果の種類: 機能停止 | 誤った結果 | 安全性 | 性能 | 保守性 | 文書 | その他（<何が止まる・誤るか>）
- PR 起因: 持ち込み | 顕在化 | 既存 | 不明（<根拠>）
- 同種箇所: 該当なし（<理由>） | 性質「<守るべき性質>」／範囲 <検索した範囲>／検索 `<コマンド>`／結果 <該当箇所の一覧、または「他に無し」>
- 前回の修正との関係: 該当なし | 前回の修正が足した仕組み | 規則の写し | 「同じ規則」の主張 | 不明（<説明>）
- 見直しの根拠: <判断 ID と新しい根拠>
- 推奨対応: 適切な責務境界での最小の修正案（あわせて固定すべきテスト。AGENTS.md「テストの採用基準」の「書くテスト」に当たる場合だけ）
```

- ID は Severity 別の連番（Critical: C-1、High: H-1、Medium: M-1、Low: L-1、Info: I-1）。Unverified candidates は `U-1` からの連番
- `再現` から `見直しの根拠` までの欄の値と、どの指摘で必須かは、形式の定義元の 4.2 節に従う。実行時の挙動についての Critical・High・Medium には `再現` を、実行時の挙動についての全指摘には `利用経路` と `結果の種類` を、全指摘には `PR 起因` を、Critical・High・Medium の欠陥には `同種箇所` を、判断の記録に前のサイクルがある場合の全指摘には `前回の修正との関係` を書く。`見直しの根拠` は `skip` の判断を active に戻す指摘だけに書く。必須でない欄は行ごと省いてよい。文書の指摘は `再現: 該当なし`・`利用経路: 該当なし`・`結果の種類: 文書` とする
- 「内容」には具体的な発生条件を必ず書く。「〜の可能性がある」だけの指摘は書かない
- 新規指摘の検出時 HEAD は今回の `$SHORT_HEAD` を設定する。継続指摘は検出時 HEAD を維持する。旧形式から移行する指摘で過去 HEAD が無い場合は `不明（旧形式）` とし、hash を生成・推測しない

## 優先順位

1. 実行時バグ・要件の実装漏れ
2. セキュリティ問題
3. 破壊的な挙動変更
4. 依存関係の互換性リスク
5. 不足しているテスト（AGENTS.md「テストの採用基準」の「書くテスト」に当たる挙動に限る）
6. 保守性リスク

## コメントしないこと

- 無害なフォーマット差分
- 命名などの好みと、実害・規約違反を示せない改善案（「確認観点」の基準）
- 根拠のない理論上の問題
- 差分と無関係な広範なリファクタリング
- 差分外の既存コードの問題（diff が顕在化させる場合と、PR の受け入れ条件に必要な処理・入口が無い場合を除く）
- 重要な不変条件を隠していないコメント不足
- リンタ・フォーマッタで機械的に検出できる指摘だけの列挙

## 出力

以下のテンプレートを使用する。指摘が 0 件の Severity セクションは省略する。件数表は active findings のみから計算する。重大度判定は active findings、確認不足判定は coverage と pending も参照する。

```markdown
- 対象: PR #<N> / head <12文字のshort-sha> / <YYYY-MM-DD>
- レビュアー: <モデル識別子>（ランタイム: <ランタイム>）

## Review history

| Reviewed at (UTC) | HEAD                  | Mode                  |
| ----------------- | --------------------- | --------------------- |
| <RFC3339 UTC>     | `<12文字のshort-sha>` | Initial / Update |

## Summary

（総評 2〜4 文。何をレビューし、何を実行検証したかを含める）

| Severity | 件数 |
| -------- | ---- |
| Critical | n    |
| High     | n    |
| Medium   | n    |
| Low      | n    |
| Info     | n    |

## Critical

## High

## Medium

## Low

## Info

（各指摘は「指摘 1 件のフォーマット」に従う）

## Review coverage

（読んだ範囲・読めなかった範囲・実行検証・未実施の検証・委譲状態と省略・中断理由を記載する。判断の記録の集約の結果（記録なし・読めなかった理由を含む）と前回の修正の範囲も書く。要件の出所（読んだ Issue・文書。受け入れ条件の記載が無ければその旨）と照合の結果（満たさない条件は指摘 ID、確かめられない条件は理由）も書く。「実行した確認」には、入口ごとに「<条件> → 動く／止まる（<要点>）」を 1 行で書く）

## Unverified candidates

（各候補を `### U-<n>: <一行の疑い>` の見出しにし、検出時 HEAD・位置・具体的な疑い・確認済み根拠・不足根拠・次の確認方法を記載する。なければ「なし」と書く）

## Pending previous findings

（過去 ID・タイトル・元 Severity・検出時 HEAD・前回内容と根拠・今回未確認の理由を記載する。該当なしなら省略可）

## Decided findings

（判断の記録で `skip` の問題のうち、新しい根拠が無いもの。各指摘に判断 ID・位置・検出時 HEAD・新しい根拠（なし。見直す条件）を書く。件数表・重大度判定・Merge recommendation に数えない。該当なしなら省略可）

## Test suggestions

（番号付きリスト。各項目にどの指摘（ID）を固定するテストかを明記する。AGENTS.md「テストの採用基準」の「書くテスト」に当たるものだけを挙げる）

<!-- Review timing は complete が末尾へ生成する。予算・phase・委譲状態は Review coverage に書く。 -->

## Merge recommendation

（下記基準で 1 つを選び、理由を 1 文添える）

## Resolved findings

### <過去のID>: <過去の指摘タイトル>

- 検出時 HEAD: `<12文字のshort-sha>` または `不明（旧形式）`
- 解消確認時 HEAD: `<今回の12文字のshort-sha>`
- 確認根拠: <現在のコードパスまたは実行検証による根拠>
```

- Review history の時刻は `$REVIEWED_AT`（`date -u +"%Y-%m-%dT%H:%M:%SZ"`）を使う。HEAD は `$SHORT_HEAD`（12 文字）。Mode は `$PREVIOUS_REVIEW` が空なら `Initial`、snapshot があれば `Update`。明示 commit でも同じ値を使い、指定内容は保存名、候補JSONの `reviewed_head`、Review timing の `pr_head` / `review_kind`、Review coverage に残す
- 既存 Review history 行と既存 Resolved findings は削除・並べ替えず、今回分だけ追記する。同じ HEAD でも追記する
- Resolved findings が無い初回で解消も無い場合はセクションを省略してよい。更新で既存 Resolved findings がある場合は維持する
- `レビュアー` の行には、ランタイムが提示した正確なモデル識別子（`prepare` に渡した実測値。Codex では現在の session metadata から確定した値）と、ランタイムの小文字の名前（Claude Code は `claude-code`、Devin は `devin`、Codex は `codex`、pi は `pi`）を書く（形式の定義元 3 章）
- Merge recommendation は `Request changes`・`Needs manual verification`・`Approve` のどれかをそのまま先頭に書き、続けて理由を 1 文書く
- `## Review history` の見出しと 3 つの列は変えない（PR 修正ループが読む）

Merge recommendation の判定基準:

- **Request changes**: 【検証済み】の Critical / High が 1 件以上ある
- **Needs manual verification**: 【検証済み】の Critical / High がなく、active な【確認推奨】の High 以上・重要な未検証候補・未再検証の過去指摘・必要な検証の未実施・差分の実質的な未読箇所がある
- **Approve**: 上記に該当せず、【検証済み】の Critical / High が 0 件で残る指摘が Medium 以下または【要設計判断】のみ。補助委譲の省略・中断だけで本体が必要範囲を確認済みなら判定を悪化させない
- 実行時の挙動についての Critical / High は、実行した再現が無ければ【確認推奨】になるため、Request changes の根拠にならず、Needs manual verification の条件（active な【確認推奨】の High 以上）に入る。Decided findings は件数・判定に数えない

## 候補一覧と実行情報

新規JSONは `review-findings/v2`。prepare が主要skillのpath/hash、request のrun/request/agent/role、実測model/runtime/providerを固定する。未知の識別値はunknown、待ち時間はnull。completeが同じ reviewer/provenance/timingを候補と本文の `Review timing` に書く。保存・cleanupと全体の実測は返却payloadで別に報告する。

実行記録（レビューディレクトリの `runs/`）は helper が書き、手で直さない。

候補のfindingには本文の同じID・severity・confidence・位置・検出時HEADと、property / variants / statement_sites / 必要ならfailure_scenarioを記録する。再現・利用経路・結果の種類・PR起因・同種箇所・前回修正・見直しの欄は共有形式へ対応付ける。提案した修正を実行済みの根拠にしない。秘密値を記録しない。

## 出力前セルフチェック

- prepareの固定対象と全体diffを読んだか。PR headとreviewed commitを混同していないか。
- 自モデルsnapshot以外のreviewを読んでいないか。既存history/Resolvedを保持し、Modeを `Initial` / `Update` のみに保ち、指定commitを保存名・候補JSON・coverageで区別したか。
- 継続ID/検出HEAD、active/Resolved/Pending/Decidedの分類と件数、skipの新根拠を照合したか。
- 本文と候補のID/位置/coverage/未検証が同じか。未読があるのにApproveにしていないか。
- 受け入れ条件を実装・利用入口・確認結果と照合したか。受け入れ条件に無い機能や形式、実害・規約違反を示せない改善案を指摘にしていないか。
- AGENTS.mdのテスト基準に沿い、文面や運用値の写しを要求するテストを提案していないか。
- 実行時のCritical/High/Mediumの検証済みに実際の最小再現があり、「止まる」と「動く」を分けたか。
- request binding、実測識別情報、skill hash、unknown/null、同一timingを維持したか。
- DSH は complete の staged を親の保存成功と混同せず返したか。他 runtime / 親は成果2件・cleanup成功を確認したか。failed/missingを0件にしていないか。
- cleanup後のWTを成果参照に使わず、残存領域は完全パスと理由を返したか。
