# 移植メモ

本体の日本語手順は `.agents/skills/review-fix-planning/` 1件。Claudeは同じdirectoryへの相対リンクにし、runtime専用コピーを作らない。SKILL.mdだけのコピーでは実行できない。移植専用の設定framework/installerは追加しない。

## 依存ファイル

| ファイル | 役割 |
| --- | --- |
| SKILL.md、references/decisions.md・execution.md・round-records.md | 採否、通常役割実行、結果と記録 |
| scripts/upsert_pr_comment.py・run_roles.py・finalize.py | 安全な投稿、設定役割と1回枠、修正不要の最終化 |
| tests/・evals/ | fake CLI/adapterと実Gitの挙動、構造、fresh行動評価 |
| .agents/review-records.md、.agents/scripts/review_records.py・tests | v1/v2記録の検査・照合・集約・描画 |
| .agents/review-findings.md、.agents/scripts/review_findings.py・tests | 候補/確認済み一覧/v2 planningの検査 |
| .agents/skills/review-verify/ | cycleと固定7入力のfix_check、保存監査/accept |
| .agents/skills/pr-review/ | ローカルcommitを含むPR全体prepare→complete |
| tools/agent_runner/、.devcontainer/agent-runner.toml | 通常Request/Receipt/Result、役割割当/fallback |
| .agents/scripts/review_target.py・agent_tmp.py | 対象route/clone、親main rootのscratch/Spec保存 |

helperは位置をresolveして `parents[4]` から共通module/runnerを読む。相対構成を保つ。upsert/parserは標準library中心だがrun_roles/pr-review/review-verifyはpydantic等を使うので、既存tools/devenv projectからuvで実行する。

## 必要環境・認可

- Superpowersのbrainstorming/writing-plans/実装workflow。通常判断は自力で選ぶが、名指しN/明示Hold/編集境界の認可を捏造しない。
- Git、gh、uv/Python 3.14+。PRコメント/branch push/merge/Issue更新の権限を操作ごとに確認する。
- review-recordsの読取はOWNER/MEMBER/COLLABORATORの記録だけ。壊れた記録・別agentのコメントを手で迂回しない。
- agent-runnerの既存非対話adapter/認証と明示割当。self/外部/混合で同じ入力・成果・回数を守る。

## リポジトリに合わせる部分

採否（H/D1/N/Hold/Unverified/Skip・安全性3項目）、3結果、1回レビューの上限、同原因・継承の規則は共通。変えるのは以下だけ:

- 対象PR/レビューdirectory/cloneの解決。MaatLogではparent main rootのreviews/<owner>/<repo>、番号は親origin、他usaturn repoはtemporary clone。
- owner範囲（review_target.PLANNING_OWNERSはusaturnのみ）、fork拒否、旧レビュー配置は明示パスだけ。
- Spec/Planとscratch保存、所有repository、既存PR head branchへの反映、正式検証入口/必要CI。**親maatlog-pocのCI免除を他repositoryへ広げない。**
- root取得helperとPython project、runtimeモデル/Requestの測定。変更対象とhelper source rootを分ける。

旧loop callerの移行は #570 の作業。private文字列でこのスキルを別手順へ戻さない。共通Request/Receipt/Resultと識別情報の形式を移植のために勝手に変えない。

## 固定された先行インターフェース

- #483: review_targetによるowner/repo別の対象解決と保存先。記録はrepository/PR番号で識別するので配置変更で別判断にしない。
- #491: reviewerの再現/利用経路/結果種類/PR起因/同種箇所。旧形式でもplannerの現行確認で判断できる。
- #562: fix_checkの7入力、成果2件、保存先監査、道具inventoryを保ったaccept。詳細はreview-verifyとexecution.md。
- #568: pr-reviewの通常prepare→全体差分→complete、未push review-commit、固定保存/cleanupとbinding。独自レビュー入口を足さない。

## 移植後の確認

1. 実体のtests、共通parser testsとlayoutを対象repositoryのPython projectで実行する。fake gh/adapterと一時Gitだけで、実PRをmergeするテストにしない。
2. 記録無しPRのfoldはrounds空/exit0。旧awaiting_approval/stopped_*と新stoppedの読取、理由不足の拒否を確認する。
3. evals/decision-packet.md・safety-packet.mdとpressureの無スキル/旧版/新版本文のfresh応答を目視採点する。過去validation.mdは履歴、新3結果の採点はevals/2026-10-08-rework.md。fixed wordingを通常pytestで照合しない。
4. role実行でself/外部/混合、2回目拒否、成果欠落/停止未確認/保存先変更を確認する。finalizerでmoving HEAD、公開CI不足、手動MERGED、Issue更新失敗が誤完了にならないことをfake ghで確かめる。
5. スキルの参照実在・frontmatter/JSON・実行可能なコードblockを検査し、repositoryの正式入口を実行する。
