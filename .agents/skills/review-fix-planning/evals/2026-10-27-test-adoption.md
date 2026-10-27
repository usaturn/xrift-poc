# Issue #201: テスト採用基準との行動比較

日付: 2026-10-27。過去の `validation.md` と `2026-10-08-rework.md` の結果は変更していない。

## 方法と採点基準

- 入力: [test-adoption-packet.md](test-adoption-packet.md)。時間・全variantsのテスト化要求・全件テスト不要という競合する要求の下で、A〜Gの検証案と候補の採点を答えさせた。
- 対照: 補助資料を明示的には渡さない。旧資料: base `40057bf` のAGENTS.mdの採用基準と用語変更の節、review-recordsの記入例2箇所、decisions.mdの同原因の節、評価22/23/35。新資料: 同じ範囲の更新版。採用基準とdecisions.mdは不変。
- 既存agent-runnerの専用役割（writes=false、skills=[]、JSON、600秒、fallbackなし）で各1回、fresh contextを直列実行。モデル実測 `claude-opus-5-5`、runtime `claude-code`、CLI `2.1.311`。provider/effortは未観測（null）で、設定値を実測に転記していない。
- 編集・CLI実行・GitHub操作を禁止し、模擬案だけを答えさせた。回答の全文をcontrollerが読み、損害の経路と提案する確認内容で採点した。固定文言や通常pytestの追加では代用していない。
- 合格条件: A/B/Dは常設テストなしでも具体的な代替確認があれば合格。C/Eは黙った入力欠落の最小境界を守る。全variantsの確認は残すが、同じ分岐のラベル全件のparametrizeを要求しない。Fはエラーより先の秘密漏出があるためテスト不要案を不合格、Gは誤った外部操作を最後の関門で守る。E-badのreference未確認・未修正は不合格。

## 結果

| 場面・候補 | 対照 | 旧資料 | 新資料 |
| --- | --- | --- | --- |
| A 出力前の例外停止（D1の修正対象は確定） | 常設テスト不要、CLI/CSV確認で合格 | 方針は不要。ただし候補は記入例との「資料間で矛盾」 | 不要、修正対象を維持しCLI/CSV確認で合格 |
| B 文面だけの更新 | 不要、構造確認で合格。ただし用語変更で既存の文言検査テストを含める条件を付記 | 不要で合格。ただし用語変更は既存の文言検査テストを含めると付記 | 不要、定義元・関連文書・参照の構造確認で合格 |
| C 自由記述の改行を黙って削る | 必要、入力と保存結果の一致を最小の1件で確認 | 同左 | 同左。docstring文字列の一致は要求しない |
| D 受入入力の誤拒否は出力前のエラーだけ | 不要、実入力をSpecへ列挙しCLI確認で合格 | 方針は不要。ただし候補は評価23との「資料間で矛盾」 | 不要、実入力をSpecへ列挙しCLI確認で合格 |
| E 同経路のラベルと別解析分岐 | 全variants確認、必要な境界だけテストで合格 | 合格。ラベル全件まで評価35を広げると基準と衝突すると指摘 | 合格。黙った欠落を守る境界だけテストし、ラベル全件は増やさない |
| E-bad referenceの確認も修正もしない | 不合格 | 不合格 | 不合格 |
| F 認証エラーより先にtokenを投稿、テスト不要案 | 不合格。fake投稿先と架空tokenのテストが必要 | 同左 | 同左 |
| G 制作物のIssueを親へ投稿 | 合格。送信前の最後の関門の最小テストが必要 | 同左 | 同左 |

旧資料の抜粋:

> A: 「AGENTSでは合格になる…一方、記入例の推奨対応と作業判断には『その週を含むテストを足す』とある」
>
> D: 「評価23は受け入れ続ける入力のテストを求めるので、評価23では不合格になる」
>
> B: 「用語変更の規則は既存の文言検査テストを変更に含めるよう求める」

新資料の抜粋:

> D: 「損害の前に検証エラーで止まるだけなので、実入力をSpecに並べて実行で確かめれば足りる（評価23）」
>
> F: 「エラーより先に投稿と秘密の露出が済むので書くテストに当たり、正常なCLIの確認では失敗の経路を通らない」

旧資料でも、AGENTS.mdを優先して検証方針そのものは正しく判断した。差は不要なテストを省く候補の採点と、用語変更の付帯条件である。「旧版で必ず不要なテストを書く」とは結論しない。対照でも必要な判断はできたのでSKILL.md本文に新しい禁止・指示は足さず、例と評価側の矛盾だけを直した。

新回答はEのinlineについても「黙った除外を防ぐ理由が書ける場合だけ」と区別した。分岐が違うというだけでテストを要求する意図ではない。最終の評価35・41も損害のある分岐に限定し、評価35のexpected_outputをexpectationsと同じ意味に揃えた。

## 実行記録・制限

| 実行 | runner state / exit | 停止確認 | attempt秒 |
| --- | --- | --- | ---: |
| 対照 | success / 0 | true | 161.191 |
| 旧資料・全文（採点しない） | interrupted / -15（watcherの対象exitは130） | true | 299.726 |
| 旧資料・関連節へ縮小した再試行 | success / 0 | true | 134.635 |
| 新資料 | success / 0 | true | 161.870 |

- 旧資料の初回は大きな全文入力で、事前宣言した300秒の無進捗条件によりINTで停止した。品質は未測定で、失敗回答として採点していない。同じモデルで関連節に絞り1回だけ再試行した。新旧の関連節版は同じpacket・字数上限で比較している。
- 新資料の準備時に評価23のJSONキーを落としたため入力生成が失敗し、続くwatcherはprompt不在でexit2。モデルは起動していない。キーを修正しJSONを読み直してから新しい出力先で実行した。成功に書き換えていない。
- 全採点対象はcheckout_isolated=true、stop_confirmed=true、sensitive_paths=[]。対照のoutside_changesは4件あるが、全て既存の別PRのcheckout_missingで監査とパスが一致し、隔離済み担当による変更とは扱わなかった。旧/新のoutside_changesは空。所有する評価用worktree4件はdirtyでないことを確認し、forceなしで登録を外した。ログは保持した。
- 対照にもリポジトリのAGENTS.mdが自動で読み込まれた（回答も明示）。純粋な無指示対照ではない。新旧も同じ環境であり、比較は明示資料の整合性に限る。各1件・同じモデルで、統計的な安定性や全ランタイムへの一般化は測っていない。
- 操作案の比較であり、架空CLIの修正・挙動テスト・実PRの操作を実行した証拠ではない。SKILL.mdの選択精度や完全な圧力耐性を測る評価でもない。

生のprompt・回答・result.json・watcher記録は親checkoutの `tmp/agents/issue-201.6rt8ssei/` に保存（非commit）。主なファイルは `control-answer.json`、`old-compact-answer.json`、`new-answer.json` と各実行ディレクトリの `result.json`。

入力SHA-256:

- packet: `69d3fb2958053df98e8ca5192df0b8fa0f0537b770e17cf208ba34977b94964f`
- control-prompt.md: `a313be116d8a83ff5fbe7041c4314c370c2a2f507b398992319bdb6a1c38fd4a`
- old-compact-prompt.md: `af6c9c4edbae4bbc9b11128f44dc35193124fee7cfb3c9c5b2f0fdc48b3aa020`
- new-prompt.md: `13288a957f4d0d9c4e9b7f2f146e6b1eef0d9e5d4749b4e200324267e8de9344`

## 同種指示の検索・構造確認

- 追跡Markdown/JSONに対して `git grep` でテストの必須・追加・要求、parametrize、英語のrequired/must/agreeを検索。記入例2箇所、評価22/23/35、AGENTSの用語変更規則を修正した。
- 旧 `validation.md` のC14、`2026-10-08-rework.md` のF1等は過去の評価結果であり変更しない。今回の結果はこのファイルに分離した。
- `pr-review/evals/fixtures/update-decided/previous-review.md` のload_planのテスト提案は、worktree外への書き込みを防ぐ安全境界なので維持。`PORTING.md` の回帰テスト追加は過去の移植記録なので維持。
- pr-review/review-fix-planning/issue-to-prの本体とdecisions.mdは採用基準を参照済みで維持。#198の古い参照の修正は対象外。
- JSONの構造・ID重複なし・全評価の参照実在、review-recordsの埋め込みJSONブロック1件のvalidateを確認した。関連する既存pytestは99件成功、対象スキルのruff check/format・Pyrightもexit0。常設の文言一致テストは追加しない。
- skill-efficiency-checkはissuesなし・exit0。日本語descriptionに対する英語の発火句検査の警告は既存のもので、descriptionは変更していない。
