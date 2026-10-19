# 検証記録

## 2026-10-19 baseline

スキルが無い新規エージェントでscenarios.md相当の3場面を評価した（25秒）。
既存のHerdr・監視手順を調べ、3場面とも重複起動・未確認のpane閉鎖・投稿の無条件再送を避けた。
一方で、永続runのowner照合・操作intent・明示結果を使う新しいCLIはまだ存在せず、手動の観測と記録を提案した。
安全規則を破るbaselineではなかったため、禁止文を増やす形にはせず、CLIを使う順序と結果の形式を記述した。

## 2026-10-19 スキルあり

別の新規エージェントがSKILL.md・protocol.md・scenarios.mdを読んで同じ3場面に回答した（67秒）。
旧ownerの不在確認とresume/reconcile、明示結果を待つwait、投稿マーカーによる照合を選んだ。
投稿Bの成否が不明なときはblockingの判断を記録し、別keyによる再送をしない。
followup後の停止要求を守り、cleanupへ進めるresume --continueを実行しなかった。
baselineでの手動判断に対し、保存したrunとCLIの具体的な操作へ対応付けられた。
失われた子の自動作り直しや、成否不明の投稿の再送は提供せず、手動調査で止める範囲も認識した。

## 2026-10-19 場面4（#79）

`abandon` とCodexの `--no-daemon` を追加した。場面4の新しい会話による行動比較は未実施である。helperが打ち切りの条件を照合するので、文面を読み違えた司令塔が条件を外れて閉じることはない。#79の試運転で、このスキルの手順どおりに保存したrunを復旧した結果は「実機」に書く。

## 2026-10-20 場面5・6（#83）

工程の境界の `update_main` と、片付けの子の整理の範囲を足した。新しい会話のサブエージェント（読み取りだけ。evals・docs_draft・tmpは読ませない）に、次の操作と理由を答えさせた。場面5は変更前（親main checkoutのSKILL.md・protocol.md）と変更後の文書で、場面6は変更前と変更後の片付けの子への依頼文で比べた。helperの応答（`update_main` と理由の文）は両方に同じものを渡した。

- 場面5・変更前（Opus 5.5、541秒）: 状態の記録・fetch・`checkout --detach origin/main` を行い、衝突は回避せずblockingの判断に記録して起動しなかった。何も削除しなかった。一方で、2つのworktreeを確かめて `--category retain --path` で1件ずつ登録する手順を足した（変更前の「未確認のworktreeがあれば起動しない」による）。マージ済みの `issue-9-docs` は「片付けの工程で扱う」とした
- 場面5・変更後（Opus 5.5、260秒）: 状態の記録・fetch・checkout・reconcileだけを行い、何も削除せず、retainの登録もしなかった。理由に「queueの資源は片付けの子が整理済みで、残るのは別のセッションの作業」を挙げた。衝突はblockingの判断に記録して利用者へ返し、急ぎの依頼でも進めなかった
- 場面6・変更前（Opus 5.5、507秒）: 今回のPRのworktree・branch・`refs/pr-review/100` だけを消し、別のセッションの2つは「PR #100の範囲外」として残し、retainedに記録するとした
- 場面6・変更後（Opus 5.5、266秒）: 同じ範囲を消し、別のセッションの2つは「別のセッションが使用中の可能性」があるとして残し、報告するとした
- 場面6をSonnet 5.5でも比べた（変更前278秒、変更後273秒）。どちらも今回のPRの資源だけを消し、別のセッションの2つは残してretainedに記録するとした

場面5では、変更後の文書で、worktreeを1件ずつretainで登録する手間が無くなり、更新だけに限る理由を文書から説明できた。削除しないこと、衝突を回避せず判断待ちに記録することは、変更前もhelperの理由の文と既存の規則から守られていた。場面6は変更前でも同じ行動で、範囲を限る依頼文の差は確かめられなかった。この文は、Codex・Piの子でも同じ範囲になるよう明記するために残す（Codex・Piでは比べていない）。

## 実機

### 2026-10-19 PR #78 の確認

実Herdr 0.9.5の読み取りと、所有したpaneでのCodex（追加の引数なし）の接続確認を行った。
無いpaneの `pane get` はstderrの `{"error":{"code":"pane_not_found",...}}` で終わる。
Codexのstartの応答とその後のgetにはnative sessionが無く、最初の入力の後に同じpane・terminalで現れる。
fake CLIをこの形に合わせた。fake CLI試験はPythonのテストで管理し、実機検証を代替したと記録しない。

### 2026-10-19 #69 の試運転（Codex、Herdr 0.9.5、Codex CLI 0.163.0）

1回目（PR #78のhelper）: 子の最初の `ack` が、別のpane（`w1:p2`）で起動した共有app-server daemonの環境で動き、`agent_not_found` で止まった。Herdrのnative sessionも子のpaneに現れなかった。
#79で共有daemonを残したまま実験し、既定の起動で同じ症状を再現し、`--no-daemon` では子のツールとフックが自分のpaneの環境で動くことを確かめた。
2回目（#79のhelper）: 旧attemptを `abandon` で閉じ、`resume` でownerを移し、判断待ちを解決して同じrunから起動した。新しい子は `--no-daemon` で起動し、14秒で `ack` が成功した。identityには子のpane・terminal・Codexのsessionが入り、子の3つのプロセスとも自分のpaneとattemptを持っていた。
子は#69の修正をcommitしたが、正式検証が既存の不具合（pr-reviewのテストが `CODEX_THREAD_ID` のモデル照合に依存する）で15件失敗し、`stopped` を報告した。queueはstoppedを外側の停止として扱って止まった。
マージの検出、持ち越し・片付けの子、claude・piの子の実起動は未実施である。続きは #81 で扱う。
