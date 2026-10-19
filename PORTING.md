# 移植の記録

## 移植元

- リポジトリ: `usaturn/maatlog-poc`
- コミット: `eac5195ce371f8ced154d6df5b2a22f35b74eac2`（2026-10-10、Issue #1 の調査と同じ）
- 実行時に maatlog-poc の checkout は要らない

## 移植したもの

| 場所 | 内容 |
|---|---|
| `.devcontainer/` | Dev Container の定義、初期化（`initialize.bash`・`install_beforehand.bash`）、シェルの設定（`zshrc.txt`・`starship.toml`）、Herdr、pi と DSH のランチャー、`agent-runner` の設定、クリーンアップ、bash のテスト |
| `scripts/` | 検証の入口（`ci/verify-devenv.sh`）、pi のセットアップ、Claude Code のオンボーディングの補助 |
| `tools/` | `devenv`（検証用の project）、`devenv_migration`（Dev Container のテスト。名前は移植元とそろえた）、`agent_runner`、`review_triage`、`test_support` |
| `.agents/` | 共通スキル 14 個、補助スクリプト、レビューの記録の形式 |
| `.claude/`・`.pi/`・`.cursor/rules/`・`.mcp.json`・`.github/workflows/ci.yml` | 各エージェントの設定と CI |

## 移植しなかったもの

| 対象 | 理由 |
|---|---|
| `rst-writer`、`python-extension-docs`、`version-export` | Issue #1 で対象外と指定されている |
| `public/`、`.gitmodules`、`prepare_public_checkout`、`public/uv.lock` を使う Playwright の導入 | MaatLog のアプリ用 |
| `tools/devenv_migration/tests/test_application_checkout.py`、`test_cutover_contract.py`、`tools/devenv_migration/evidence/` | MaatLog の `public/` と cutover の記録 |
| ポート 8000 | MaatLog の sphinx-autobuild 用 |
| `opencode.json` | OpenCode はセットアップで導入していない。設定ファイルだけ置くと導入済みと誤解される |
| `.codex/.gitkeep` | 空のプレースホルダーで、設定の実体が無い |
| `scripts/add_pi_skills_setting.py` | 共有している `~/.pi/agent/settings.json` に、このリポジトリのスキルを追加してしまう。セットアップからも呼ばれていない |
| `.pi/APPEND_SYSTEM.md` の Response Persona の節 | 移植の方針として決めた（Spec 2 章） |
| 廃止済みの pi 拡張の撤去処理（`install_pi_extensions` の撤去部分、`remove_pi_mcp_adapter`、`remove_pi_distill`）と `test_pi_mcp_adapter_migration.py` | 移植の方針として決めた（Spec 2 章）。検出と警告に置き換えた |
| `.devcontainer/README.md` のうち、MaatLog の既存コンテナだけに関わる移行の節（「既存コンテナの移行」「npm への一本化と既存環境の移行」「Herdr サイドバーの設定移行」「herdr-pr-review からの移行」「pi-distill の撤去」「`public/` checkout の検査と復旧」） | 新しいリポジトリには移行元の状態が無い。共有ホームの `~/.dsh` に関わる「旧 settings.yaml からの移行」は残した |
| スキルの `evals/` のうち、移植元で実行した日付付きの記録と、MaatLog のリポジトリを入力にした評価シナリオ | MaatLog 固有で、xrift-poc では再現できない。テストや `SKILL.md` が参照しているものは、下の「移植元の記録として残したもの」に挙げた |

## 移植元の記録として残したもの

次のファイルは、テストやスキルの文書が参照しているため残した。いずれも maatlog-poc での実行の記録で、中の PR 番号・パスは移植元のものである。xrift-poc で再現した記録ではない。

- `.agents/skills/review-fix-planning/evals/2026-10-08-rework.md`: `references/porting.md`・`decision-packet.md`・`safety-packet.md` が参照している
- `.agents/skills/review-fix-planning/evals/validation.md`: `evals.json` が参照している
- `.agents/skills/review-verify/evals/validation.md`: `replay.md` が参照している
- `.agents/skills/review-verify/evals/replay.md`: `test_replay_procedure.py` が、中のコードブロックを実行している

`.agents/skills/planning-parallel-issues/evals/scenarios.json` は評価の入力として残した。参照元だった `validation.md`（2026-09-16 の移植元での実行記録）は移植しなかった。

## XRift 向けの変更

- `items/`・`worlds/` をセットアップで用意し、`.gitignore` の `/items/`・`/worlds/` で無視する
- XRift CLI（`@xrift/cli@0.25.0`）と XRift 公式スキル（`WebXR-JP/xrift-skills@41d6504`、`skills@1.7.2`）をセットアップで導入する
- `~/.xrift` の bind mount、ポート 5173・3000 の転送（5174〜5179 は `portsAttributes` のラベルだけで、開発サーバーが起動したときに VS Code が検出して転送する）
- 操作先の判定（`.agents/scripts/route_target.py`）と、制作物へのスキルのリンク（`.agents/scripts/project_link.py`・`xrift-link`）
- `cod` に `-c 'project_root_markers=[".devcontainer"]'` を加えた
- `~/.zshrc` の設定を管理ブロックにし、再実行で重複しないようにした
- Playwright は `tools/devenv` の依存から導入する
- 廃止済みの pi 拡張は撤去せず、残っていれば削除の手順を警告として表示する
- `AGENTS.md`・`repository-ownership`・各スキルの、`public/` と公開側の記述を、親と制作物の分担に書き換えた
- 次の取り込み時の変更: `tools/review_triage/tests/test_questions.py` の定数名 `KEY_A`・`KEY_B` だけを `FINDING_A`・`FINDING_B` に改名した（gitleaks の誤検出を避けるため）

### 設計の後で決めたこと（Spec からの変更）

- 制作物は親の main checkout の下で扱う。親の worktree の `items/`・`worlds/` は `stop`（そのものは `product_base`、配下は `product_in_env_worktree`）と判定する。main checkout の Git 未初期化の制作物（`product_uninitialized`。その場で編集する）とは code を分けた（最終レビューの指摘 I-1）
- `route_target` の判定の対象は、cwd ではなく、これから変更するパスにする。まだ無いパスは、実在する最も近い祖先のディレクトリで判定する。`items/`・`worlds/` の下のパスは、それらのディレクトリが無くても `env` にしない（最終レビューの指摘 I-2）
- origin が親を指すかどうかは、origin の URL の設定値の末尾（`usaturn/xrift-poc`）を大文字小文字を区別せずに照合する。`https://user@github.com/...`・SSH のホスト別名・`GitHub.com`・`http` の書き方も親とみなす（最終レビューの指摘 M-1）
- `repo-cleanup-after-merge` は、worktree を削除する前に、Git で無視したファイルも確かめる。`items/`・`worlds/` に中身がある worktree や、依存・キャッシュ以外の無視したファイルがある worktree は削除しない（`git worktree remove` は無視したファイルも消すため。最終レビューの指摘 I-1）
- `project_link` の管理ブロック（制作物の `.git/info/exclude`）は、制作物のすべての worktree にある、実在するリンクから作り直す
- `issue-to-pr` は、`gh` の応答と、大文字小文字を区別せずに照合する
- `review_triage` は親のリポジトリだけを対象にする
- `finalize` はどのリポジトリでも CI を確かめる。CI の無い制作物のリポジトリでは止まる

## 改名した識別子と、残した目印

コンテナ内で完結する識別子は `xrift-poc` に改名した。共有ホーム（bind mount している `~/.claude`・`~/.pi`・`~/.dsh` など）のファイルに書く目印は、改名すると同じ内容が 2 回書かれるため、そのまま残した。

| 識別子 | 扱い |
|---|---|
| `MAATLOG_CLEANUP_*`、`maatlog_periodic_cleanup`、`~/.cache/maatlog` | `XRIFT_POC_CLEANUP_*`、`xrift_poc_periodic_cleanup`、`~/.cache/xrift-poc` に改名 |
| `~/.local/share/maatlog/dsh-model-env.mjs`、プラグイン ID `maatlog-dsh-model-env` | `~/.local/share/xrift-poc/`、`xrift-poc-dsh-model-env` に改名（`~/.local/share` と `~/.local/bin` は mount していない） |
| `/tmp` の接頭辞 `maatlog-pr-loop-`・`maatlog-agent-runner-`・`maatlog-pr-review-scratch-` | `xrift-poc-` に改名。`cleanup-workspace.bash` と配置の検査も合わせた |
| `maatlog-devenv`（project 名）、`maatlog-review-triage/1`（User-Agent） | `xrift-poc-devenv`、`xrift-poc-review-triage/1` に改名 |
| `~/.dsh/cordis.patch.yml` の `# BEGIN maatlog managed provider config` | 残した。`~/.dsh` は maatlog-poc のコンテナと共有している。xrift-poc 側で目印を変えると、同じ provider のブロックが共有ファイルに追記される |
| コードやコメント中の `#番号` | 変えていない |

コードやコメント中の `#番号` は、特記が無い限り移植元の `usaturn/maatlog-poc` の Issue・PR を指す。

## 移植元の改善の取り込み（2026-10）

### 範囲と取り込み方

- 移植元: `usaturn/agent-workspace`（移植の時点の名前は `usaturn/maatlog-poc`。改名の後も Issue・PR の番号は変わらない）
- 範囲: 移植元の `main` の `eac5195..3bc4769`（2026-10-10〜2026-10-17）の first parent 44 件。`git log --first-parent --format='%h %ad %s' --date=short eac5195..3bc4769` で一覧にした
- 進め方: Issue #18 のボード（子 #19〜#28）で、利用者が 2026-10-17 に選んだ 9 項目と、その前提になるテストの基盤を取り込んだ。27 件を取り込み、17 件は取り込んでいない（下の「移植元の first parent 44 件」）
- 取り込み方: 移植元の merge ごとの差分（`git diff <merge>^1 <merge>`）をボードの順に当て、拒否された hunk は xrift-poc の文脈に合わせて手で直した。ファイル全体を移植元の最新版で置き換えていない。移植元が作業先の判定を `projects.toml` に置き換えた部分（`scan_before_push`、issue-to-pr の `resolve_issue.py`・`SKILL.md`）は、`route_target.py` の判定の上に書き直した
- 日付付きの評価記録（`evals/` の実行記録）と、移植元のリポジトリを入力にした評価の入力は取り込んでいない（「移植しなかったもの」と同じ方針）
- 記録の出典: 各子の完了のコメントと xrift-poc の PR。merge commit と、完了のコメントの後に足した commit は PR の情報で確かめた
- 番号: この節の「移植元の PR」の列と、「移植元の」を付けて書いた番号は、`usaturn/agent-workspace` の PR を指す。それ以外の番号（子・PR・Issue）は xrift-poc の Issue・PR を指す。このボードで取り込んだファイルのコードやコメント中の `#番号` は、上の規則と同じく、特記が無い限り移植元の Issue・PR を指す

### 子ごとの取り込み

| 子 | 内容 | xrift-poc の PR | merge commit | 当てた移植元の merge |
|---|---|---|---|---|
| #19 `test-support` | テストの基盤（`outside_runner`・pytest の `--rootdir`・負荷に強いテスト） | #33 | `157f23e` | `b3d6001`・`70d4b4e`（一部）・`f9563a0`（一部）・`200b513`・`3bc4769` |
| #20 `pi-launchers` | agent-runner の `pixg`、`pigema` ランチャー | #31 | `08bae70` | `88f3552`（`[agents.pixg]`）・`41a4cc9` |
| #21 `monitoring-budget` | `task_watch.py`、Issue の実行予算 | #30 | `7754f6f` | `05080dc` |
| #22 `scan-before-push` | push 前の秘密検査 `scan_before_push` | #32 | `7392b67` | `c3a0aca`（`scan_before_push` の部分）・`8b1947d`。どちらも書き直した |
| #23 `dsh-runner` | agent-runner の DSH の標準 headless | #35 | `30463d6` | `c0ae21e`・`346dbb6` |
| #24 `dsh-review` | PR レビューとレビュー修正ループの DSH のレビュアー | #36 | `5ab2ddc` | `ec13c3e`・`88f3552`（既定の inspect）・`a23639b`・`70d4b4e`（一部）・`f9563a0`（一部） |
| #25 `review-loop` | pr-review-fix-loop と agent-runner の修正 | #40 | `ba5c4c1` | `316ef51`・`bcfb459`・`538975a`・`d27eb20`・`0220679`・`70d4b4e`（一部）・`f9563a0`（一部） |
| #26 `write-exceptions` | writes=false の担当への書き込みの例外 | #43 | `6f2cac5` | `3fc280f`・`11cff4c` |
| #27 `issue-merge` | issue-to-pr の `--merge` | #42 | `7853c72` | `f2d2ad4`・`b2bf42b` |
| #28 `issue-implement` | issue-to-pr の `--implement` | #47 | `c384d42` | `b8dd941`・`a87369a`・`94a2aa2`・`1c379cf`・`70d4b4e`（一部） |

### 移植元の first parent 44 件

新しい順に並べた。「取り込み」の子が当てた。

| merge | 移植元の PR | 内容 | 扱い |
|---|---|---|---|
| `3bc4769` | #709 | pytest の rootdir を repo の root にする | 取り込み: #19 |
| `1c379cf` | #708 | Task の結果の形が合わないときも fallback を試す（`payload_check`） | 取り込み: #28 |
| `b2bf42b` | #706 | 統合用の branch に向ける子の PR の向き先と、`--merge` の不適用 | 取り込み: #27 |
| `200b513` | #707 | 子の起動を含むテストの短い期限を延ばす | 取り込み: #19 |
| `f9563a0` | #700 | 負荷が高いと落ちる時間依存のテストを直す | 取り込み: #19（`test_runner.py`・`test_review_regressions.py`）、#24（DSH の `test_runner.py`）、#25（ループの再実行のテスト） |
| `70d4b4e` | #699 | fixture `outside_runner` で agent-runner の depth を外す | 取り込み: #19（fixture と既存のテスト）、#24（DSH の `test_runner.py`）、#25（`test_loop_reviewer_protocol.py`）、#28（`test_implement_task_run.py`） |
| `94a2aa2` | #698 | Task の依頼ごとに `RUN_DIR` を片付ける | 取り込み: #28 |
| `a87369a` | #697 | issue-to-pr の `SKILL.md` の 1 行 | 取り込み: #28 |
| `bb419c6` | #693 | `version-export` の承認待ちと、移植元の登録先の規則 | 取り込まない: `version-export` は Issue #1 で対象外。`.agents/projects/` は移植元のワークスペース固有の登録 |
| `8b1947d` | #692 | 秘密検査で、マージ commit で入った内容も検査する | 取り込み: #22（書き直し） |
| `d08c66b` | #691 | bash のテストの検査で SIGPIPE を避ける | 取り込まない: ボードで選ばなかった小さな修正。必要になったら別の Issue にする |
| `b8dd941` | #690 | issue-to-pr の `--implement` と `implement_task.py` | 取り込み: #28 |
| `f3e22cb` | #688 | pi-lens を撤去する | 取り込まない: xrift-poc は Issue #16 で撤去済み |
| `1b796ea` | #686 | 移植元の登録先の検証と秘密検査の差し替え | 取り込まない: 移植元のワークスペース固有の登録（`projects.toml`・`.agents/projects/`） |
| `f2d2ad4` | #681 | issue-to-pr の `--merge` と `loop.py check-options` | 取り込み: #27 |
| `41a4cc9` | #682 | `pigema` ランチャー | 取り込み: #20 |
| `0220679` | #677 | ループの `start` で fix の割り当てを検査する | 取り込み: #25 |
| `5d72ed8` | #676 | 移植元の登録先の開発手順 | 取り込まない: 移植元のワークスペース固有の登録（`.agents/projects/`） |
| `a23639b` | #674 | self-review・fix-check への DSH の割り当てを拒否する | 取り込み: #24 |
| `d27eb20` | #673 | ループの `start` で Herdr のペインの名前を変える | 取り込み: #25 |
| `a6f339a` | #669 | 移植元への新しいプロジェクトの登録 | 取り込まない: 移植元のワークスペース固有の登録（`projects.toml`・`.agents/projects/`） |
| `70548d4` | #670 | 移植元の登録先の文書の修正 | 取り込まない: 移植元のワークスペース固有の登録（`.agents/projects/`） |
| `11cff4c` | #666 | self-review・fix-check に書き込みの例外を渡す | 取り込み: #26 |
| `a17d9e9` | #668 | 移植元への新しいプロジェクトの登録 | 取り込まない: 移植元のワークスペース固有の登録（`projects.toml`・`.agents/projects/`・`devcontainer.json`） |
| `2a29ba3` | #660 | pi-lens のログの置き場所 | 取り込まない: pi-lens の回避策。xrift-poc は Issue #16 で pi-lens を撤去済み（移植元も `f3e22cb` で撤去した） |
| `3fc280f` | #659 | CLI の `run --write-exception` | 取り込み: #26 |
| `88f3552` | #657 | agent-runner の `pixg` と、既定の inspect | 取り込み: #20（`[agents.pixg]`）、#24（既定の inspect） |
| `538975a` | #653 | inspect の書き込みの扱い、再実行の前の停止待ち、runner の `write_exceptions` | 取り込み: #25 |
| `a16a0aa` | #652 | worktree の一括 prune をやめる | 取り込まない: ボードで選ばなかった小さな修正。必要になったら別の Issue にする |
| `ec13c3e` | #645 | DSH のレビュアー（親が用意する入力・`finalize-parent`・sandbox） | 取り込み: #24 |
| `346dbb6` | #650 | DSH の TMPDIR に残るリンク・FIFO を片付ける | 取り込み: #23 |
| `bcfb459` | #648 | worktree の監査と、資源の回収の判定 | 取り込み: #25 |
| `316ef51` | #646 | Hold の既定を skip にする | 取り込み: #25 |
| `304dba4` | #640 | 移植元の改名と状態の移行 | 取り込まない: 移植元のリポジトリの改名に伴う移行（`projects.toml`・`tools/devenv_migration`）。ボードの対象外 |
| `88a4100` | #641 | Plannotator を Dev Container で使う | 取り込まない: xrift-poc は PR #15 で対応済み |
| `c3a0aca` | #639 | 新しいプロジェクトの登録と `scan_before_push` | 取り込み: #22（`scan_before_push` の部分だけ。書き直し）。Wabun・MaatLog の登録、`version-export`、`projects.toml`、`test_scratch_placement_contract.py` は取り込まない |
| `c0ae21e` | #638 | agent-runner の DSH の標準 headless | 取り込み: #23 |
| `068fb7a` | #636 | Issue の応答の repo を大文字小文字を区別せずに照合する | 取り込まない: 同じ問題を、移植の時点に xrift-poc の判定に合わせて直していた（`5011c60`。「設計の後で決めたこと」） |
| `ed3fc85` | #635 | submodule から `projects/` の clone へ移す | 取り込まない: `projects/`・`tools/workspace` への移行。ボードの対象外 |
| `b3d6001` | #634 | `offline_uv_env` が、別の venv の `bin` も PATH から外す | 取り込み: #19 |
| `50a61f9` | #631 | `projects.toml` と `tools/workspace` の導入 | 取り込まない: ボードの対象外 |
| `4a6624d` | #626 | `python-extension-docs` の持ち越し | 取り込まない: `python-extension-docs` は Issue #1 で対象外 |
| `05080dc` | #625 | `task_watch.py` と Issue の実行予算 | 取り込み: #21 |
| `8a0fd18` | #624 | gitlink・submodule の fixture を共有にする | 取り込まない: MaatLog の `public/` submodule の fixture。xrift-poc は `public/` を移植せず、`tools/test_support` を `create_env_repo`・`create_product` に作り替えた（`99822a7`） |

### xrift-poc に合わせた点と、移植元と違う直し方

- **#19**: `create_parent_repo`（MaatLog の `public/` submodule を持つ親）を使うテストを `create_env_repo` に置き換えた（`b3d6001`）。fixture `outside_runner` と README は xrift-poc の fixture 名で手で当て、文脈の import 行が無いテストには import の直後に足した（`70d4b4e`）。新しいテストの置き場所の `tools/workspace/tests` は `tools/review_triage/tests` にした（`3bc4769`）。`test_checkout_preparation_is_supervised` は、フックの停止を `/proc` から観測するように直した（`2c75b26`）。移植元の `f9563a0` のままでは、フックの SIGTERM のハンドラが、runner が固定 0.3 秒の猶予の後に送る SIGKILL に間に合わず、負荷の高いホストで落ちる。範囲の終点 `3bc4769` の時点では移植元も同じコードだった。移植元は `715d981`（移植元の PR #719）で同じ直しを入れた（`5e8df86` の変更行は `2c75b26` と同じ）
- **#20**: 手直しなしで当たった
- **#21**: `monitoring-long-tasks/SKILL.md` の手順 3〜5 は、xrift-poc の `verify-devenv.sh` 向けの文面を残した。`AGENTS.md` には「実行予算」の行と controller の停止の例外だけを手で当てた。例の `verify.sh` を `verify-devenv.sh`・`xrift check` にした。空白を含む state dir のテスト（`test_state_dir_with_spaces_round_trip`）を足した
- **#22**: 差分は当てずに書き直した（移植元の関数は `assert_working_root`・`route_show`・`projects.toml` に依存する）。場所の判定は `route_target.py` の JSON、base は親が `origin/main`・制作物が origin の既定ブランチにした。base に `.gitleaks.toml` が無ければ Gitleaks の既定の規則を使う（ボードの R5）。CI に固定版の Gitleaks を入れた。push の `-u` を外し（送り元が SHA なので上流ブランチを設定しない）、PR は `--head` で作る。空のリモートへの初めての push は履歴全体を検査する。移植元に無い対策として、Gitleaks が読む `git log -p` の出力をオプションで固定し、同じ `git log` を先に実行して失敗を止める（`014709c`）。呼び出し元の IFS を既定に戻す（`d94759d`、PR のレビュー修正）
- **#23**: テストの fixture を、移植元の `create_workspace` から `create_env_repo` にした。移植元に無い修正として、DSH の `tool_result` のエラーの文に認証らしい文字列があっても、完了した試行を認証待ちにしない（`8cb4093`、PR のレビュー修正）
- **#24**: `execution.md` の DSH の段落は「### 回数と受け取り」の直前に置き、まだ無かった `fix_check_write_exceptions` に触れない言い回しにした。`test_run_roles.py` の新しいテストから、移植元の write-exceptions が足した fixture `parent` を外した。雛形の inspect のコメントと README の「DSH の標準 headless」を、既定のレビュアーの構成（ボードの R3）に合わせた。移植元に無い変更として、DSH のレビュアーに書き込みの許可を渡せるのは `run_workflow` の `sandbox_write_paths` だけで、CLI の `run` からは失敗することを README と `noninteractive.md` に書いた（`131be78`）。`/tmp` の下の venv でもテストが動くように、DSH の sandbox の中では base のインタープリターを使う（`6120b3d`、PR のレビュー修正）
- **#25**: 拒否された hunk は、xrift-poc の `TARGET_REPO_ROOT` の文脈、現在の一時ディレクトリの接頭辞、DSH の import に合わせて手で当てた。移植元の旧い接頭辞の回収の許可は足していない。`.devcontainer/README.md` には、worktree の監査の説明だけを足した
- **#26**: 新しいテストの repository 名を `usaturn/xrift-poc` にした。CLI の不正な `--write-exception` は、Issue の受け入れに合わせて、値を含まない固定の理由を stderr に出す（移植元は stderr が空）。helper の Git の許可の行は DSH 以外の担当に限ると書いた
- **#27**: Issue の解決は `route_target.py` の判定（URL と `#N`）のまま残し、`parse_input` と出力の `merge` だけを足した（移植元の `<name>#N` の表記は足していない）。CI の確認は、どのリポジトリでも行う。統合用の branch の指定が無ければ、`BASE` を親は `main`、制作物は既定ブランチにする。ループの起動例で、単独で起動するときと issue-to-pr から起動するときの `TOOL_ROOT` を分けた
- **#28**: `route_target.py` の判定、`--merge` の出力、親の CI の確認を残して手で当てた。`SKILL.md` の 5 章の push の項目は変えていない（#34 の担当）。テストの要否の文面は `AGENTS.md` の「テストの採用基準」を参照する形にした（`a87369a`）。depth=1 の shallow clone で、依頼から片付けまでを通すテストを足した。マージの前に、先に入った #46（#34）との `SKILL.md` の競合を解消した（`eeef4aa`）

### 取り込んだ merge のうち、取り込まなかった差分

- `05080dc`（#21）: `monitoring-long-tasks/evals/2026-10-10-issue-598.md`（日付付きの評価記録）
- `316ef51`（#25）: `pr-review-fix-loop/evals/2026-10-14-hold-default-skip.md`（日付付きの評価記録）
- `bcfb459`（#25）: `.devcontainer/README.md` の、移植元のワークスペースの改名と移行の節の 2 hunk
- `c3a0aca`・`8b1947d`（#22）: `projects.toml`・`.agents/projects/`・`assert_working_root`・`project_clone`・`route_show` など、移植元のワークスペースの登録を前提にした部分と、`test_scratch_placement_contract.py` の変更
- `f2d2ad4`・`b2bf42b`（#27）: `projects.toml` による Issue の解決（`<name>#N`・`project_clone`）、登録による CI の免除、`issue-to-pr/evals/`
- `b8dd941`・`a87369a`・`94a2aa2`・`1c379cf`（#28）: `projects.toml`・`tools.workspace` への移行、`issue-to-pr/evals/`
- `3fc280f`（#26）: CLI が値を検査せず stderr を空にする挙動（上の #26）
- `11cff4c`（#26）: Git の許可を DSH の担当にも無条件に広げる文面
- `88f3552`・`70d4b4e`・`f9563a0`: 担当の子に分けて当て、どの子も自分の範囲の外のファイルを当てていない（上の表）

### 後続の Issue

取り込んだコードの、移植元と同じ挙動に由来する課題を Issue にした。

- #37: 応答の記録が無い DSH の試行で、認証不足の分類が `response_failed` に上書きされる（#24 の PR で見つかった）。xrift-poc で直した: Identity の読み取りに失敗したとき `response_failed` にするのは `success` の試行だけで、ほかの状態はそのまま残す。移植元（`715d981`）は上書きのままなので、移植元の `dsh.py` の `collect` を取り込むときはこの差を残す
- #38: DSH のレビューが `reviews/` に作る作業領域（`.staging`・`parent-pr-data`）の置き場所と後片付け
- #52: codex をレビュアー（inspect）にすると、writes=false の役割を `codex exec -s read-only` で起動するため pr-review の helper が書けず、毎回 rejected になる（PR #50 の loop で見つかった）。xrift-poc で直した: adapter が `Capabilities.enforces_read_only` を宣言し、loop の `check-options`・`start`、`run_roles.py`、runner（書き込みの例外のある run）が、その担当を inspect・self-review・fix-check に割り当てた設定を起動の前に止める。移植元（`a47b206`）の `codex.py` も `-s read-only` で、この検査は無いので、移植元の agent_runner・loop・run_roles を取り込むときはこの差を残す

## 移植漏れの補完（2026-10、Issue #53）

- 移植元: `usaturn/agent-workspace` の PR #719（Issue #718）、commit `0ddc82f0b85220e4f91f93937b7ce2c7d63d98af`。`tools/agent_runner/tests/test_process.py` の `alive()` が、`/proc/<pid>/stat` を開いた後、読む前に PID が回収された場合の `ProcessLookupError`（ESRCH）を停止として扱う差分（+2 −1）を取り込んだ
- #19（PR #33）の `2c75b26` では、同じ PR の `test_runner.py` 側だけを先に取り込んでいた。今回、そのとき漏れた `test_process.py` 側を補い、読み取り時に例外が発生する回帰テストを追加した。上の first parent 44 件の集計は、その当時の範囲の記録として変えていない
- xrift-poc での追加修正: `test_runner.py` の子の停止確認と、`tests/adapters/dsh/test_runner.py` の `_assert_stopped()` も `alive()` を再利用する。存在確認と読み取りの間の同じ競合を避け、Z/X を停止として扱う。権限エラーなど、消滅以外の読み取り失敗は隠さない
- 取り込みはこの差分だけで、移植元のファイル全体による置き換えや、実装本体の停止処理の変更はしていない

## 範囲の後の移植元の修正の取り込み（2026-10、Issue #48）

- 移植元: `usaturn/agent-workspace` の PR #712（Issue #710）、commit `98159dd9484f0053a57c8b1fe11058c29c5601ac`（2026-10-17）。ボード #18 の範囲の終点 `3bc4769` の後にマージされたので、上の first parent 44 件には入っていない（表は範囲の記録として変えていない）。PR #47 の持ち越し候補の再調査で見つけた
- 取り込んだ差分: issue-to-pr の `SKILL.md` の 2 章 4。既存の open PR で止まるときは、PR の URL と base を報告に書き、base が既定 branch のときだけ `/pr-review-fix-loop <PR の URL>` を、それ以外（統合用の branch など）では `/pr-review <PR の URL>`（マージしない）を案内する
- xrift-poc に合わせた点: 既定 branch を、2 章 5 の `BASE` と同じく「対象 repository の既定 branch（親は `main`、制作物は `repository-ownership` で確認した既定 branch）」にした。移植元が番号のまま残した 1 章と 5 章の表の `/pr-review-fix-loop <PR>` も PR の URL にした。xrift-poc の issue-to-pr は制作物の PR も案内し、番号だけではループが親の origin の PR として解決するため
- 取り込まなかったもの: ループ・`finalize.py` に base の検査を足すこと（移植元にも無い。Issue #48 の対象外）

## 更新の方法

- エージェント、XRift CLI、XRift 公式スキルの更新は、README の「更新」の節に書いてある（手順を二重に書かない）。更新したら、この文書の「検証の記録」に確かめた版を追記する
- 移植元の修正の取り込み:
  - 移植元は `usaturn/agent-workspace`（移植の時点の名前は `usaturn/maatlog-poc`。private）。セッションのスクラッチ（`tmp/agents/`）に読み取り専用で clone し、xrift-poc の worktree に移植元の remote を足さない
  - 対象は `git log --first-parent --format='%h %ad %s' --date=short <前回の終点>..origin/main` で一覧にする。前回の終点は、直近の取り込みの節の範囲の終点である（「移植元の改善の取り込み（2026-10）」では `3bc4769`）
  - 当てるのは merge ごとの差分（`git diff <merge>^1 <merge> -- <paths>`）で、ファイル全体を移植元の最新版で置き換えない。拒否された hunk は xrift-poc の文脈に合わせて手で直す
  - 記録は、この文書に「移植元の改善の取り込み（YYYY-MM）」の節を足して書く。first parent の全件について、取り込んだ PR か取り込まなかった理由を書き、xrift-poc に合わせた点と、取り込んだ merge のうち取り込まなかった差分も書く
  - 複数の担当で並行して取り込むときは、この文書を編集する担当を 1 人に決める。ほかの担当は、当てた merge・拒否された hunk と直し方・合わせた点・取り込まなかった差分を完了のコメントに書く

## CI をマージの条件にする（未実施）

Spec 2 章の決定（PR のマージには CI の green を必須にする）は、GitHub 側の設定がまだ無い。2026-10-10 の時点で `gh api repos/usaturn/xrift-poc/branches/main/protection` は 404（Branch not protected）、`gh api repos/usaturn/xrift-poc/rulesets` は `[]` だった。リポジトリの設定は人間が行うので、初回の CI の実行の後に、利用者が次の手順で設定する。

1. PR で `.github/workflows/ci.yml` が実行され、job `devenv` の check が表示されることを確かめる
2. GitHub の Settings → Rules → Rulesets（またはブランチ保護）で、`main` に「Require status checks to pass」を設定し、`devenv` を必須の check にする
3. `gh api repos/usaturn/xrift-poc/rulesets`（ブランチ保護なら `gh api repos/usaturn/xrift-poc/branches/main/protection`）で設定を確かめる

設定するまでは、README・`AGENTS.md` の手順どおり CI の green を確かめてからマージする。review-fix-planning の finalize は、GitHub 側の設定にかかわらず、どのリポジトリでも CI の結果を確かめる。

## 検証の記録（2026-10-10）

この Dev Container（Ubuntu 26.04、Node.js v24.21.0、npm 11.19.0、uv、Python 3.14）で確かめた。作業は `tmp/agents/` の一時ディレクトリで行い、制作物・fixture・ログはこのリポジトリに入れていない。エージェントの確認は、一時の `HOME`・`CODEX_HOME` を使い、モデルへは送信していない。

### XRift CLI とテンプレート

| 確かめたこと | コマンド | 結果 |
|---|---|---|
| CLI の導入 | `npm install --prefix <一時> --no-audit --no-fund @xrift/cli@0.25.0`（`-g` なし） | 52 パッケージを導入。`xrift --version` は `0.25.0` |
| ワールドの作成 | `xrift create world my-world -y` | テンプレートの取得・加工・依存の導入まで成功。依存の導入で Node.js の `DEP0190` の非推奨警告が出る（CLI が `shell` オプション付きで子プロセスを起動するため。動作に影響しない） |
| アイテムの作成 | `xrift create item my-item -y` | 同上。成功 |
| ビルド | `npm run build`（`tsc && vite build`） | ワールド・アイテムとも終了コード 0（`✓ built in 16.50s`・`15.95s`） |
| 型チェック | `npm run typecheck`（`tsc --noEmit`） | ワールド・アイテムとも終了コード 0 |
| 開発サーバー | `npm run dev -- --host 0.0.0.0`（ワールドは 5173、アイテムは `--port 5174`） | どちらも HTTP 200。vite 7.3.1 が `Network:` のアドレスも表示した。確認後に `kill -- -"$DEV_PID"` でプロセスグループごと止め、`ss -ltnp` で 5173・5174 を待ち受けるプロセスが残っていないことを確かめた |
| 描画 | Python Playwright（Chromium のヘッドレス）で `http://localhost:<port>/` のスクリーンショットを撮り、目で確かめた | ワールド: 空の背景（街並みの全天球画像）、茶色の壁、緑の床、左下に操作の説明（`WASD` で移動など）が描画された。アイテム: 灰色の床の上に、青い八面体と黒い台座（青い光の広がり付き）が描画された。ページのエラー（`pageerror`）は無し |
| セキュリティ検査 | `xrift check world --build`、`xrift check item --build` | どちらも `Security check passed`（ワールド 22 ファイル、アイテム 20 ファイルすべて `APPROVE`）。警告は出なかった。`--ignore-warnings`・`--skip-check` は使っていない。0.25.0 の種類を書く形は `--build` を付けてもビルドしない（#57）ので、この結果は既にあった `dist/`（上の「ビルド」の行の成果物）を検査したもので、`--build` によるビルドの確認ではない |

ブラウザのコンソールには、次の警告が出た。いずれもテンプレートや three.js・ヘッドレスの描画に由来し、作成と検証の可否には影響しない。

- `THREE.Clock` の非推奨（three.js が `THREE.Timer` への移行を促している）
- `GL Driver Message ... GPU stall due to ReadPixels`（ヘッドレスのソフトウェア描画の性能の警告）
- `using deprecated parameters for the initialization function`（依存ライブラリの初期化の非推奨）
- `PCFSoftShadowMap has been deprecated`（アイテムのみ）

そのほかに分かったこと。

- Playwright のブラウザ（`chromium_headless_shell-1243`）が無かったため、`uv run --project tools/devenv --no-sync playwright install chromium` を実行した（`--with-deps` なし）。Dev Container の新規作成で `install_beforehand.bash` が行う導入は、ここでは確かめていない
- ワールドのテンプレートは、制作物の中に独自の `AGENTS.md`・`CLAUDE.md`・`skills-lock.json`・`.github/dependabot.yml`・`.gitignore`（`.agents` を無視する）を含む。アイテムのテンプレートには含まれない
- CLI は、実際の `HOME` に `~/.xrift/version-cache.json`（版の確認のキャッシュ）を書いた。認証情報（`config.json`）は書かれていない

### XRift スキルの取得

`scripts/install_xrift_skills.bash` を、`scripts/` と `.gitignore` を写した一時の Git リポジトリで実行した（`skills@1.7.2` が実際に動く）。

- 2 回続けて実行しても、どちらも終了コード 0。`xrift-sdk`・`xrift-world` が `41d65040e8c2a5313991acddf93406888fc84326` のまま（`skills-lock.json` の `ref`）
- `-a claude-code -a codex -a pi` で作られたものは、`.agents/skills/xrift-sdk`・`.agents/skills/xrift-world`（実体）、`.claude/skills/` の同名のシンボリックリンク、`skills-lock.json` だけ。`.pi/`・`.codex/` は作られなかった
- `git status --porcelain --untracked-files=all` に、取得物は出ない（コピーした `scripts/` と `.gitignore` だけが未追跡）

### エージェントの読み込み

親を写した Git リポジトリ（origin は `usaturn/xrift-poc`）の下に、Git 未初期化の制作物（`plain-world`）、`xrift-link`（`project_link.py link`）を実行した独立した Git リポジトリの制作物（`git-world`）、実行していない Git リポジトリの制作物（`unlinked-world`、対照）を置いて確かめた。親の fixture は `/workspaces/maatlog` の下にあるため、Claude Code では `/workspaces/maatlog/CLAUDE.md` も読み込まれる。この表は、fixture の親の `CLAUDE.md`・`AGENTS.md` とスキルが出るかだけを見ている。

| エージェント | `plain-world`（未初期化） | `git-world`（リンク済み） | `unlinked-world`（対照） | 確かめ方 |
|---|---|---|---|---|
| Claude Code 2.1.296 | 親の `CLAUDE.md` ✓、`repository-ownership`・`xrift-world` ✓ | 同左 ✓ | 親の `CLAUDE.md` ✓、スキル ✗ | `claude -p "/context" --output-format stream-json --verbose`（一時 HOME、`env -i`） |
| pi 1.1.0 | スキル ✓ | スキル ✓ | スキル ✗ | `pi --mode rpc --no-session --offline --no-extensions --approve` に `get_commands` を送る。指示は `resource-loader.js` の `loadProjectContextFiles` がファイルシステムのルートまで親ディレクトリをさかのぼる実装を確認（実行はしていない） |
| Codex CLI 0.162.1（`-c 'project_root_markers=[".devcontainer"]'` あり） | 親の `AGENTS.md` ✓、スキル ✓ | 同左 ✓ | 同左 ✓ | `codex debug prompt-input`（一時 `CODEX_HOME`） |
| Codex CLI（フラグなし、対照） | 親の `AGENTS.md` ✓、スキル ✓ | 親の `AGENTS.md` ✗、スキル ✓（リンク経由） | 親の `AGENTS.md` ✗、スキル ✗ | 同上 |
| Devin 3000.11.3 | スキル ✓ | スキル ✓ | スキル ✗ | `devin skills list`（モデルへ送信しない。指示の読み込みは確かめていない） |

- 制作物の中の worktree（`worlds/git-world/.worktrees/wt1`）にも `project_link.py link` を実行した。Claude Code・pi・Codex（フラグあり）・Devin のいずれも、親の指示（Devin は指示を除く）とスキルを読んだ。Codex はフラグなしだとスキルだけを読んだ（親の `AGENTS.md` は読まない）
- 親の外（`/tmp` 相当）に作った制作物の worktree への `link` は、`project_link: 制作物の配下ではない worktree には対応しない` で終了コード 2 になった。この worktree では Codex（フラグあり）も親の指示とスキルを読まなかった。`AGENTS.md` に、制作物の worktree は制作物のディレクトリの中に作るよう書いてある理由である
- リンクの管理ブロックは制作物の `.git/info/exclude` に書かれ、`git status --porcelain` に出なかった。Git 未初期化の制作物への `link` は、何も変えずに「リンクは不要」と表示して終わった
- ディレクトリ名に空白と日本語を含む制作物（`worlds/空白 ワールド`）も判定できた（次の節）

### Git で無視した制作物の検索と編集

`/items/`・`/worlds/` を無視した一時の Git リポジトリに、`worlds/空白 ワールド/src/scene.tsx` と `items/only/a.txt` を置いて確かめた（ripgrep 15.1.0）。

- 親のルートからパスを付けずに `rg -n <文字列>` を実行すると、制作物の中は検索されず、終了コード 1（一致なし）になった。親に `items/`・`worlds/` のほかにファイルが無いときは、`rg: No files were searched` と表示して終了コード 2 になった
- 親のルートから `rg -n <文字列> worlds` とパスを明示すると、`worlds/空白 ワールド/src/scene.tsx` の行が見つかった
- 制作物のディレクトリ（`worlds/空白 ワールド`）の中から `rg -n <文字列>` を実行しても見つかった
- 制作物のファイルに追記しても、親の `git status --porcelain` は空のままだった（`--ignored` を付けると `items/`・`worlds/` だけが `!!` で出る）

この結果から、`AGENTS.md` に、親のルートからはパスを明示するか、制作物の中から検索することを書いた。

### 秘密情報の検査（gitleaks）

Gitleaks 8.30.1（`install_beforehand.bash` と同じ版）で、`6926b44`（最終レビューの修正の途中）の時点の worktree を検査した。

- `gitleaks git . --no-banner`: 32 commit を検査し、検出なし（`no leaks found`）
- `gitleaks dir . --no-banner`: 2 件を検出し、終了コード 1。どちらも `tools/review_triage/tests/__pycache__/test_state.cpython-314-pytest-9.1.1.pyc`（`aws-access-token`）で、検証の実行で作られた、追跡していない `.pyc` だった。元の `test_state.py` はテスト用の偽の値を `"AKIA" + "..."` のように分けて書いており、Python がコンパイル時に連結した値を `.pyc` に書いたものである。`.pyc` は `.gitignore` の `__pycache__/` で無視され、追跡している `.pyc` は無い（`git ls-files` に 0 件）
- `gitleaks dir` を CI などで使う場合は、検証の前に実行するか、`__pycache__` を除いて実行する

### 操作先の判定（`route_target.py`）

fixture の親を `XRIFT_POC_ROOT` にして実行した。最終レビューの修正（I-1・I-2）の後に、改訂した `route_target.py` で同じ fixture に対して再実行した結果である。

| 判定するパス | kind | code | 終了コード | repository |
|---|---|---|---|---|
| 親（main checkout）、その中のファイル | `env` | `env` | 0 | `usaturn/xrift-poc` |
| 親の worktree（`.worktrees/feat-x`）と中のファイル | `env` | `env` | 0 | `usaturn/xrift-poc` |
| 親の外に作った親の worktree | `env` | `env` | 0 | `usaturn/xrift-poc` |
| `worlds/git-world`（Git 管理、origin は `https://github.com/someone/git-world.git`） | `product` | `product` | 0 | `someone/git-world` |
| `worlds/空白 ワールド`（origin は `git@github.com:someone/space-world.git`） | `product` | `product` | 0 | `someone/space-world` |
| `worlds/git-world/.worktrees/wt1`（制作物の worktree） | `product` | `product` | 0 | `someone/git-world` |
| 親の外（`/tmp` 相当）に作った制作物の worktree | `product` | `product` | 0 | `someone/git-world` |
| `worlds/plain-world`（Git 未初期化）と、その中のディレクトリ | `stop` | `product_uninitialized` | 3 | null |
| `worlds`（`items/`・`worlds/` そのもの） | `stop` | `product_base` | 3 | null |
| 親の worktree の `worlds` | `stop` | `product_base` | 3 | null |
| 親の worktree の `worlds/in-wt-world`（Git 未初期化）、まだ無い `worlds/new-world/src/a.tsx` | `stop` | `product_in_env_worktree` | 3 | null |
| まだ無いパス: 親の `tools/new.py` | `env` | `env` | 0 | `usaturn/xrift-poc` |
| まだ無いパス: `worlds/new-world/src/a.tsx` | `stop` | `product_base` | 3 | null |
| まだ無いパス: `worlds/plain-world/src/new.tsx` | `stop` | `product_uninitialized` | 3 | null |
| まだ無いパス: `worlds/git-world/src/new.tsx` | `product` | `product` | 0 | `someone/git-world` |
| `XRIFT_POC_ROOT` が親の worktree、または制作物 | （JSON なし） | （エラー表示） | 1 | （なし） |

- 親の worktree 内のスクリプトを `XRIFT_POC_ROOT` なしで実行しても、制作物 `git-world` を `product` と判定できた
- 判定は、cwd ではなく、渡したパスで決まる（cwd が親の中でも、`worlds/git-world` は `product`）

### 未実施（このコンテナからは確かめられない）

- ホストでの Dev Container の新規作成とセットアップの完走、再構築（`${containerWorkspaceFolder}` が `XRIFT_POC_ROOT` に展開されることを含む）
- ホストのブラウザでのプレビュー
- アカウントを使う `xrift login`・`xrift whoami`・アップロード（`xrift upload`）
- `main` で CI の `devenv` をマージの必須条件にする設定（上の「CI をマージの条件にする」。初回の CI の後に利用者が行う）
- Cursor CLI（`agent`）・Grok Build・DSH の読み込み: モデルに送信せずに確かめる方法が見つからなかった。`--help` に該当の確認コマンドが無い。Cursor・Grok は `.claude/skills`・`.agents/skills` を読む見込み、DSH は未確認。手動で確かめるときは、制作物の中で各エージェントを起動し、スキルの一覧（`/skills` など）に `xrift-world` と `repository-ownership` が出るかを見る
- Devin の指示（`AGENTS.md`）の読み込み
- pi の指示の読み込みの実行（実装の確認だけ）

## Issue #8 の追加確認（2026-10-18）

上の 2026-10-10 の記録を置き換えるものではない。利用者による確認と、今回この Dev Container で実行した確認を分けて記録する。

### Dev Container の新規作成

利用者から、Issue #8 の条件1（空の checkout から新規作成でき、ログイン待ちで止まらずに `install_beforehand.bash` が完走する）は「OK」と報告された。エージェントがホストで実行したものではなく、詳細な実行ログは受領していない。

### ホストでのプレビューの準備

XRift CLI 0.25.0 で、親の main checkout の `worlds/issue-8-preview` と `items/issue-8-preview` に未改造のテンプレートを新規作成した。どちらも Git 未初期化で、親に commit する対象ではない。既存の制作物は変更していない。

- 両方の `npm run typecheck` は終了コード0。
- `xrift check --build`（種類を自動判定）は両方でビルドと検査に成功した。続けて `xrift check world --build` / `xrift check item --build` も終了コード0（ワールド22ファイル、アイテム20ファイル、すべて APPROVE）。警告を無視するオプションは使っていない。
- 新規ワールドへの初回の `xrift check world --build` は、ビルドせず `Dist directory not found` で終了コード1になった。同版の引数解析では `--build` が親の `check` に消費され、子の `world` の options へ渡らないことを実行して確認した。対応は #57 に分けた。
- `npm run dev -- --host 0.0.0.0 --port 5173 --strictPort`（ワールド）と同じコマンドの `--port 5174`（アイテム）で起動し、コンテナ内から両方の HTTP 200 を確認した。
- Python Playwright / Chromium で両方の 1280×800 の canvas とスクリーンショットを確認した。ワールドは街並み・茶色の壁・緑の床・操作説明、アイテムは灰色の床・青い八面体・黒い台座を描画し、`pageerror` は両方0件だった。
- 確認用サーバーは終了し、5173・5174 のポートが解放されたことを確かめた。その後、利用者がホストで確認し、ワールド（5173）は OK、アイテム（5174）は NG と報告した。5174 が Dev Containers で転送されていないためで、描画の成功をホストからの表示の成功とは扱っていない。
- `devcontainer.json` の `forwardPorts` に5173はあるが、5174〜5179は無い。`portsAttributes` のラベルだけでは転送は保証されない。現在のコンテナでは VS Code の「ポート」→「ポートの転送」で5174を追加できるが、追加後のホスト表示は未確認。恒久対応は #59 に分けた。

### Cursor CLI・Grok Build・DSH の読み込み

| 対象 | 版と確認方法 | 結果と限界 |
|---|---|---|
| Cursor CLI | `2026.10.16-e3cb207`、利用者の信頼承認後に `agent --print --trust --mode ask --output-format stream-json`。fixtureの未初期化・リンク済み独立Git・worktree・リンクなし対照と、実際の親・未初期化アイテムで確認 | 親の指示は、プロンプトに書いていないfixtureの親AGENTS.mdのマーカーを、ファイルを読まず正しく返したことで確認。親・リンク済み独立Git・そのworktreeでは両スキルのread成功イベントを観測した。未初期化の制作物とリンクなし対照では、モデルがネイティブの一覧に両スキルが無いと報告し、readも無かった。対応は #60 に分けた |
| Grok Build | `1.0.62 (1474f9cbdd91)`、`grok inspect --json`。利用者の信頼承認後、一時HOMEの `trusted_folders.toml` で親・fixture・独立Git・worktreeを個別に信頼（共有ホームは不変更） | 全ケース終了コード0、`projectTrusted: true`。親と未初期化の制作物では親の指示・対象スキルを検出した。リンク済み独立Gitとそのworktreeはスキルあり・親指示なし。リンクなし対照では両方なし。独立Gitで親指示を渡す扱いは #56 の追加確認として記録した。inspectによる検出結果で、モデルが指示に従うことの確認ではない |
| DSH | `0.2.0-rc.2` の導入済み `discoverBaselineInstructionFiles` / `loadBaselineInstructions` と `FileSystemSkillProvider.list/get` を実行。指示は既定の `.git` 境界・65536バイト上限。グローバル指示・スキルは一時ディレクトリへ隔離 | 親と Git 未初期化の制作物では親の指示・対象スキルを実際に読めた（指示の省略・切り詰めなし）。リンク済み独立Gitとそのworktreeでは対象スキルの本文は読めたが、親の指示は探索対象外。リンクなしの独立Git（対照）では両方読めない。対応は #56 に分けた |

DSH の対象スキルは `repository-ownership` と `xrift-world`。指示は `AGENTS.md` / `CLAUDE.md` を確認した（同じ内容のファイルをローダーがまとめる場合を含む）。この確認は導入済みローダーの実行であり、ソースを読んだだけの確認ではない。一方、DSH のセッション全体やモデルへの注入・指示に従うことを検証したものでもない。

Cursor の初回は信頼未承認で終了コード1だったが、利用者の許可後に上記の範囲で再実行した。Cursor のread成功イベントには全行範囲と `contentBlobId` がある一方、本文は含まれず、生バイトやハッシュは照合していない。ネイティブの一覧自体のdumpも取得していないので、missingの原因は断定していない。未初期化fixtureの親に実際と同じ `.claude/skills` のリンクを置いた後もmissingで、実際の未初期化アイテムでも同じ結果だった。

信頼後の最初のCursor終了時に残ったworker/LSPは、元のプロセスグループ・cwd・コマンドを照合し、事前宣言したSIGINTで停止を確認した。以後の試行も子プロセスの終了まで確認した。信頼未承認の状態を成功にはしていない。

今回確かめたのは受け入れ条件の結果と残る制約であり、すべての条件が成功したという記録ではない。ホストの追加ポートは #59、独立Gitでの親指示は #56、Cursorの未初期化制作物でのスキルは #60、CLIのビルド引数は #57 で扱う。これらの修正や修正後の受け入れ確認は今回行っていない。
