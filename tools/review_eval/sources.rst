.. _review_eval_sources:

==========================================
review_eval の 4 経路の必要情報対応表
==========================================

.. _review_eval_sources_overview:

概要
====

レビュー評価データの収集（ :ref:`review_eval_readme` ）が必要とする情報を、4 つの経路（pr-review、pr-review-fix-loop、review-fix-planning、review-verify）が実際に何として保存しているかを対応付ける。
各行は、実際の出力を書く関数とファイルを読んで確かめた内容である。
読めなかった項目・確かめられなかった項目は、表の中で「不明」または「未確認」と書く

xrift-poc の ``.agents/`` の helper で、接続点の関数名（ ``review_run.collect_outputs`` 、 ``loop.collect_records`` 、 ``upsert_pr_comment.collect_round_record`` 、 ``finalize.collect_result`` 、 ``run_roles.collect_review_dir`` 、 ``dispatch.collect_accepted`` 、 ``review_findings._collect_before_replacement`` ）が存在することを確かめた。
各接続点は収集の想定外の例外を ``review-eval: hook failed: <例外の型名>`` の 1 行にして標準エラーへ出し、呼び出し元の結果・終了コード・標準出力を変えない。
``attach-runner`` の収集は 30 秒の期限で打ち切る

保存先はいずれも親の main worktree の ``reviews/<owner>/<repo>/#<PR>-<branch>/`` である（ ``.agents/scripts/review_target.py`` が決める。gitignore 対象）。
表の「保存場所」は、この review_dir からの相対パスで書く。
``@`` で始まるパスは、ファイルとしては存在しない仮想記録で、保存点の呼び出し元が持つ bytes をそのまま収集する

.. _review_eval_sources_classes:

分類
====

表の最後の列は、次の 5 つに分ける。

1. そのまま利用する。現行の出力に必要な情報があり、収集は bytes を写すだけでよい
2. 元の blob から決定論的に復元する。収集した exact bytes から、読み取り側が推測なしに取り出せる
3. 今後は発生時点で追加記録する。上書き・削除の前に、保存点で収集する
4. 別工程で行う。単一主張への分割、独立した正解、系列の判定は、台帳（ :ref:`review_eval_readme_ledger` ）で人が行う
5. 復元できず欠損になる。収集を有効にする前に上書き・削除された版、記録していない情報

分類が複数ある行は、現行の版と、上書きされる前の版・有効化前の版で分けて書く。
欠損は、読み取り側が推測で埋めず、 ``null`` と理由（observation の ``target.missing_reason`` など）で残す

.. _review_eval_sources_caution:

読み替えてはいけないこと
========================

- 指摘 1 件は単一主張ではない。1 件の指摘に複数の主張が含まれる。分割は台帳で人が行う
- verified は独立した正解ではない。確認結果（ ``verified/<HEAD12>.json`` ）は通常のレビュー工程の結果である。正解は台帳の G1・G2・adjudicator の回答で決める
- 1 つの PR は独立した系列ではない。系列は台帳の関係と、人による確認で決める
- hash は復元できる本文ではない。本文を保存していなければ、hash だけでは元の内容を取り出せない。記録が持つ ``review_sha256`` などは、保存した bytes との照合にだけ使う
- レビューの工数と、データ整備の工数は分ける。前者は findings の ``timing``、実行記録の秒数、planning の ``activities`` にある。後者（選択・匿名化・注釈・裁定・保存）は台帳の ``time`` に入れる
- 最終的な採否（Fix または Skip）を正解ラベルに変換しない。候補の行数、出典の数、レビューの件数を、適格な主張の件数として数えない

.. _review_eval_sources_skills:

SKILL.md を変えない理由
=======================

このPRでは SKILL.md の文面を変えていない。
4 経路の保存点は、すべて既存の helper の中にある（下表の「改修」）。
収集の呼び出しを helper の中に置いたので、エージェントが SKILL.md の手順を守ったかどうかに依存しない。
指示の文面にだけ頼る保存点は無いため、スキルの行動比較（writing-skills）の対象にもならない。

review-verify の ``verified/<HEAD12>.json`` は、エージェントが一時ファイルに書き、 ``review_findings.py validate`` の後に ``mv`` で置き換える。
コード上の書き手は無いので、置き換えの直前に呼ばれる ``validate`` が、今の版を先に収集する。
これにより、SKILL.md に保存用のコマンドを足さずに済ませた。
この方法の限界は、後述の review-verify の節に書く

.. _review_eval_sources_pr_review:

pr-review
=========

関数とファイルは ``.agents/skills/pr-review/scripts/`` の ``review_run.py``・ ``review_run_record.py``・ ``pr_review_workspace.py`` である。
収集の接続は ``review_run.collect_outputs`` で、 ``main()`` の最後（ ``prepare`` ・ ``complete`` ・ ``fail`` ・ ``attach-runner`` ）から呼ぶ。
``prepare`` は上書きの前に呼ばれるため、sweep（同じ review_dir の未観測のファイルを取る処理）だけを行い、上書きされる前の ``<model>.md`` を保存する。
runner を使ったレビューでは、レビュアーが runner の子（ ``AGENT_RUNNER_DEPTH`` が空でない）なので、 ``prepare`` ・ ``complete`` ・ ``fail`` では収集しない。
runner の after_run が起動する ``attach-runner`` は、depth が付いていても収集し、実行記録と review_dir の sweep で、子が保存した ``<model>.md`` ・findings・実行記録を取る。
同じモデルの次のレビューが上書きする前の版も、この sweep で取れる

.. list-table::
   :header-rows: 1
   :widths: 8 12 8 8 10 10 10 20 6

   * - 項目
     - 取得元・形式/版
     - 書き手
     - 保存時点
     - 保存場所
     - 保持条件
     - 不足時の状態
     - 改修要否と理由
     - 分類
   * - 指摘の本文・根拠
     - Markdown（版の欄なし、review-records v1 の 4 章）。指摘の本文は ``### <ID>:`` の節にだけある
     - ``pr_review_workspace.save`` が ``atomic_copy`` で書く。呼び出しは ``review_run.complete`` だけ
     - ``complete``
     - ``<model>.md`` 、指定 commit は ``<model>-commit-<40桁>.md``
     - 同じ PR・モデル・モードで毎回上書きする。前の版は作業 session の ``previous-review.md`` にだけ残り、session の cleanup で消える
     - 有効化前に上書きされた版は取れない
     - 要。 ``prepare`` の sweep で上書き前の版を保存し、 ``complete`` の保存後にも取る
     - 1（現行の版）、3（上書きされる版）、5（有効化前の上書き済み）
   * - 指摘の構造化項目
     - ``review-findings/v2`` 、 ``kind: candidates`` 、 ``source: reviewer``
     - ``prepare`` が雛形を作り、 ``complete`` が timing を入れて書き直し、 ``save`` が保存する
     - ``complete``
     - ``findings/<model>-<HEAD12>.json``
     - 同じ HEAD の再レビューで上書きする
     - title・locations・detected_head などはあるが、内容と根拠の文章は持たない（上の Markdown が持つ）
     - 要。 ``complete`` と ``prepare`` の sweep で取る
     - 1、3、5（上と同じ分け方）
   * - 対象の完全な SHA
     - ``reviewed_head`` ・ ``base`` ・ ``pr_head`` は 40 桁。 ``detected_head`` ・ファイル名・Review history は 12 桁
     - 同上（findings）、 ``complete`` が Markdown 末尾の ``## Review timing`` に JSON で付ける
     - ``complete``
     - findings と ``<model>.md`` の末尾
     - 上書きと同じ
     - 12 桁だけの箇所からは 40 桁へ戻せない（読み取り側は ``not_full_sha`` と記録する）
     - 不要。40 桁が findings と Markdown の末尾にある
     - 1
   * - 出典の repo・PR・Issue
     - repo と PR は review_dir の場所と、findings・実行記録の ``pr{repository, number}``
     - ``review_run`` が resolve した対象から書く
     - ``prepare`` 以降
     - review_dir のパス、 ``findings/*.json`` 、 ``runs/*.json``
     - 上書きと同じ
     - Issue 番号はどの記録にも無い
     - 不要（repo と PR）。Issue は記録が無いので、台帳の ``claim create --issue`` で人が入れる
     - 1（repo・PR）、4（Issue）
   * - 実行識別（run_id・request_id・agent・モデル）
     - ``reviewer.provenance`` （agent_id・role・provider・effort・session_id・cli_version・run_id・request_id・skills）
     - ``review_run_record`` の ``create`` と ``update`` 、runner の after_run の ``attach-runner``
     - ``prepare`` から ``complete`` ・ ``fail``
     - ``runs/<開始時刻>-<model>-<8文字>.json`` （ ``kind: review_run`` ）と findings の ``reviewer``
     - 実行ごとに別ファイル
     - Request の無い単独実行は run_id・request_id・agent_id・role が ``unknown`` になる。収集は ``unknown`` を ``null`` にする
     - 不要。区別は ``unknown`` の有無と、loop の ``review.batch.run_id`` との一致で読み取り側が導く
     - 1
   * - 時間
     - findings の ``timing.seconds`` （prepare 開始から complete 開始まで）、実行記録の ``timing`` （prepare・review・save・cleanup・total の秒数と ``started_at`` ・ ``finished_at`` ）
     - ``complete`` 、 ``record.update``
     - ``complete`` ・ ``fail``
     - findings と ``runs/*.json``
     - 同上
     - ``waiting_seconds`` は ``null`` 。findings の秒数は保存前までで、保存と cleanup は含まない
     - 不要。レビュー工数としてそのまま使う。整備工数とは混ぜない
     - 1
   * - 再利用と新規の区別
     - pr-review 自身は区別を持たない（再利用は loop の記録にある）
     - 該当なし
     - 該当なし
     - 該当なし
     - 該当なし
     - 単独実行と loop 経由の区別は、実行記録の run_id と loop の記録の参照で導く。環境変数による区別は記録されない
     - 不要。loop の節の対応で足りる
     - 2
   * - 失敗・中断
     - 実行記録の ``status`` （started・success・failed）
     - ``record.create`` ・ ``record.update`` ・ ``fail``
     - 各段階
     - ``runs/*.json``
     - 同上
     - ``started`` のまま残った実行は、中断したと推測せず ``started`` のまま扱う
     - 要。 ``fail`` と ``attach-runner`` の後にも収集する（runner の子の ``fail`` は収集せず、after_run の ``attach-runner`` の sweep で取る）
     - 1（記録のある失敗）、5（記録に出ない中断）
   * - 入力 revision
     - 実行記録の ``inputs.request.sha256`` ・ ``inputs.previous_review.sha256`` ・ ``inputs.records.sha256``
     - ``record.update``
     - ``prepare``
     - ``runs/*.json``
     - 同上
     - ``inputs.records.sha256`` は判断の記録を畳み込んだ結果の canonical JSON の hash で、ファイルの hash ではない
     - 不要
     - 1
   * - 記録の形式/版
     - ``schema`` と ``kind`` （JSON）。Markdown は版の欄が無い
     - 同上
     - 同上
     - 同上
     - 同上
     - Markdown は ``## Review timing`` の有無で既知かどうかを決める
     - 不要。observation の ``format`` に残す
     - 1
   * - 本文の exact bytes と hash
     - 実行記録の ``outputs.review_sha256`` ・ ``findings_sha256`` （保存した 2 ファイルの bytes）
     - ``record.update``
     - ``complete``
     - ``runs/*.json``
     - 同上
     - 記録した hash と、後で上書きされた実ファイルは一致しなくなる
     - 要。収集は読んだ bytes を blob に保存する。hash だけでは本文を復元できない
     - 3、5（有効化前に上書きされた版）

.. _review_eval_sources_loop:

pr-review-fix-loop
==================

関数とファイルは ``.agents/skills/pr-review-fix-loop/scripts/loop.py`` と ``references/records.md`` である。
収集の接続は ``loop.collect_records`` で、 ``review`` ・ ``cycle_end`` ・ ``finish`` ・ ``recover`` の最後に呼ぶ。
``cycle_end`` は ``--record-file`` （判断の記録）と ``--finalize-result`` の bytes を仮想記録として渡す。
``finish`` と ``recover`` は、この run のサイクルの記録が参照する ``(path, sha256)`` が観測済みか確かめ、観測も今の内容も無いものを ``lost_revision`` として記録する

.. list-table::
   :header-rows: 1
   :widths: 8 12 8 8 10 10 10 20 6

   * - 項目
     - 取得元・形式/版
     - 書き手
     - 保存時点
     - 保存場所
     - 保持条件
     - 不足時の状態
     - 改修要否と理由
     - 分類
   * - サイクルと成果の対応
     - ``pr-review-fix-loop/v1`` 、 ``kind: cycle`` 。 ``review.reviewers[]`` の ``review_file`` ・ ``review_sha256`` ・ ``findings_file`` ・ ``findings_sha256`` 、 ``planning`` の ``verified_file`` ・ ``verified_sha256`` ・ ``comment_id``
     - ``save_cycle`` （ ``write_json`` ：一時ファイル、fsync、 ``os.replace`` ）
     - ``cycle-begin`` ・ ``review`` ・ ``cycle-end``
     - ``loop/<run_id>/cycle-<n>.json``
     - サイクルごとに別ファイル。同じファイルは更新で置き換わる
     - ``review_sha256`` は記録した時点のディスクのファイルの bytes の hash。 ``<model>.md`` は後のサイクルで上書きされ、実ファイルと合わなくなる
     - 要。各接続点で記録を取り、 ``finish`` ・ ``recover`` で参照の欠落を検出する
     - 1（記録）、5（参照先がすでに上書きされた版）
   * - 再利用と新規の区別
     - ``review.mode`` （ ``run`` または ``reuse`` ）、 ``review.reused[]`` （パス・sha256・model・runtime・reviewed_head・active_findings）、 ``review.batch.run_id``
     - ``save_cycle``
     - ``cycle-begin`` と ``review``
     - ``loop/<run_id>/cycle-<n>.json``
     - 同上
     - 再利用した成果は、同じ ``(場所, 内容)`` なら二重に登録しない
     - 不要。区別は記録にある
     - 1
   * - 実行識別
     - run の ``run_id`` （uuid の hex）、 ``controller`` 、 ``assignments`` 、 ``tool_commit``
     - ``save_run``
     - ``start`` 以降
     - ``loop/<run_id>/run.json``
     - 更新で置き換わる
     - pr-review 側の ``provenance.run_id`` との一致で対応付ける
     - 不要
     - 1
   * - 失敗・中断・結果
     - run の ``state`` （running・ended・held・abandoned）と ``result{outcome, ended_by, stop_reason, merged, cycles}``
     - ``save_run`` 、 ``finish`` 、 ``recover``
     - ``finish`` ・ ``recover``
     - ``loop/<run_id>/run.json``
     - 同上
     - 終了コード 6 などのエラーは記録に残らず、標準出力だけに出る
     - 要。 ``finish`` ・ ``recover`` の後に取る。標準出力のエラーは取らない
     - 1（記録のあるもの）、5（記録に出ないエラー）
   * - 採否と理由
     - ``planning.outcome`` と ``planning.finalize`` （ ``result_file`` ・ ``round_dir`` ・中身）、 ``role_runs[]``
     - ``save_cycle``
     - ``cycle-end``
     - ``loop/<run_id>/cycle-<n>.json``
     - ``round_dir`` ・ ``result_file`` ・ ``role_runs`` はパスでしか持たない。後片付けで中身が消える
     - 判断の記録そのものは review-fix-planning の節の仮想記録で取る
     - 要。 ``cycle_end`` で ``--record-file`` と ``--finalize-result`` の bytes を取る
     - 3、5（有効化前に消えた ROUND_DIR）
   * - 確認結果への参照
     - ``planning.verified_file`` ・ ``verified_sha256``
     - ``save_cycle``
     - ``cycle-end``
     - ``loop/<run_id>/cycle-<n>.json``
     - 参照する ``verified/<HEAD12>.json`` は同じ HEAD のやり直しで置き換わる
     - 参照先が失われていれば ``lost_revision``
     - 要。参照の検査を ``finish`` ・ ``recover`` で行う
     - 1、5
   * - 付属の記録
     - ``cycle-<n>/comments.json`` （1 回目の review の PR コメントの写し）、 ``review-result.json`` ・ ``rerun-result.json`` （runner の結果）、 ``requests/<request_id>.json`` （却下したレビュアーの Request）、 ``diagnostics/<request_id>.json``
     - ``loop.py``
     - 後片付けの前
     - ``loop/<run_id>/cycle-<n>/`` の下
     - 後片付け（ ``cleanup_output`` ）は runner の出力の worktree と一時ディレクトリを消す
     - 許可パターンの外のファイルは取らない
     - 要。許可パターンに入れた JSON を取る
     - 1
   * - 取らないもの
     - ``loop/lock.json`` と、 ``loop/`` の下の ``*.log`` （ ``after-run-<i>.{stdout,stderr}.log`` は ``tools/agent_runner/hooks.py`` が runner の結果の置き場所に同じ名前で書く。 ``*.log`` は許可パターンに入らず取らない）
     - ``loop.py``
     - 該当なし
     - ``loop/`` の下
     - 該当なし
     - 生ログ・排他の状態は評価に使わない
     - 取らない（範囲外）
     - 該当なし
   * - 時間
     - 記録に timing を持つ項目（ ``review.reviewers[].timing`` など）
     - ``save_cycle``
     - ``review``
     - ``cycle-<n>.json``
     - 同上
     - サイクル全体の所要時間は、 ``resources`` と各 timing から読み取り側が足す。推測で埋めない
     - 不要
     - 1
   * - 入力 revision と形式/版
     - ``schema`` ・ ``kind`` と、 ``sha256_file`` による参照の hash
     - ``save_run`` ・ ``save_cycle``
     - 各保存点
     - 同上
     - 同上
     - 参照の hash は、記録時点のディスクのファイルの bytes の hash
     - 不要
     - 1

.. _review_eval_sources_planning:

review-fix-planning
===================

関数とファイルは ``.agents/skills/review-fix-planning/scripts/`` の ``upsert_pr_comment.py``・ ``finalize.py``・ ``run_roles.py``、 ``references/round-records.md``、 ``.agents/scripts/review_records.py`` である。
``review_records.py`` はファイルを書かない（畳み込み・検証・表示を標準出力に出すだけ）。
SKILL.md は ``uv run --no-project`` で ``upsert_pr_comment.py`` を起動するので、収集は標準ライブラリだけの shim 経由の subprocess で行う（ ``upsert_pr_comment.collect_round_record`` ）。
``finalize.collect_result`` と ``run_roles.collect_review_dir`` は in-process で収集する

.. list-table::
   :header-rows: 1
   :widths: 8 12 8 8 10 10 10 20 6

   * - 項目
     - 取得元・形式/版
     - 書き手
     - 保存時点
     - 保存場所
     - 保持条件
     - 不足時の状態
     - 改修要否と理由
     - 分類
   * - 採否と理由（判断の記録）
     - ``review-records/v2`` （v1 も読める）。 pr・round・reviewed_head（40 桁）・recorded_at・planner・seats[]・decisions[]（state は fix・hold・skip・resolved・unverified、reason など）・outcome・commit・stop_reason
     - planner が作業ファイル ``<main>/tmp/agents/review-fix-planning-pr<PR>.XXXXXX/round.json`` に書く。最終の保存先は PR の issue comment で、 ``upsert_pr_comment.upsert`` が POST または PATCH する
     - コメント投稿の成功後
     - 仮想記録 ``@pr-comment/round-<n>/record.json``
     - 同じ round の自分のコメントを PATCH で上書きする。途中の版は GitHub 上で失われる。作業ファイルは後片付けで消える
     - 投稿しなかった round は取れない
     - 要。 ``upsert_pr_comment.main`` が投稿成功後に ``--records-file`` の bytes を、 ``finalize.main`` が投稿した後に ``--record-file`` の bytes を、同じ仮想パスの仮想記録として収集する
     - 3、5（有効化前に上書きされた途中の版）
   * - 採否の結果（finalize）
     - ``{outcome, merged, issues, stop_reason?, wait_for?, comment?}`` 。形式の版は無い
     - ``finalize.main``
     - 結果の保存後
     - 仮想記録 ``@finalize/<HEAD12>/result.json`` （元の ``--result-file`` の場所は固定されない）
     - 元のファイルは後片付けで消えうる
     - 結果ファイルが無ければ取れない
     - 要。 ``finalize.main`` の後に収集する
     - 3
   * - 工程の記録
     - ``review-findings/v2`` 、 ``kind: planning`` 。stages・fix_checks・activities[]（stage・actor・seconds・waiting_seconds）・rework[]・outcome・comment_id
     - planner が Write する（仮の版、サイクルの最後に更新）。 ``run_roles.reserve_review`` が呼出枠を追記する（ ``os.replace`` ）。専用の helper は無い
     - 仮の版は planner の Write、枠の追記は ``run_roles``
     - ``verified/<HEAD12>.planning.json``
     - planner の Write が仮の版を上書きする。仮の版の中身は最終の版の部分集合
     - Write の間に保存点が無ければ仮の版は取れない
     - 要。 ``run_roles.main`` の最後に review_dir の sweep を行う。仮の版の欠落は評価への影響が小さいとして許容する
     - 1、3、5（取れなかった仮の版）
   * - 確認結果への参照
     - 判断の記録は確認結果のパスを持たない（ローカルのパスを書かない規則）。 ``planning.json`` の ``verified_file``
     - planner
     - サイクルの最後
     - ``planning.json``
     - 同上
     - 判断の記録だけからは確認結果を特定できない。loop 側の ``planning`` の参照で対応する
     - 不要
     - 2
   * - 実行識別・モデル
     - 判断の記録の ``planner`` と ``seats[].provenance``
     - planner
     - 判断の記録の作成時
     - ``@pr-comment/round-<n>/record.json``
     - 同上
     - 不明な項目は ``unknown`` の文字列。収集は変換しない
     - 不要
     - 1
   * - 時間
     - ``planning.json`` の ``activities[]`` と ``stages{decide_seconds, fix_seconds, fix_check_seconds[]}``
     - planner、 ``run_roles``
     - 同上
     - ``planning.json``
     - 同上
     - ``waiting_seconds`` が無い活動がある
     - 不要。レビュー工数として使い、整備工数とは分ける
     - 1
   * - 呼び出し元の区別
     - 記録に無い。loop から呼ばれたかは、loop 側の ``planning.round`` との対応だけで分かる
     - 該当なし
     - 該当なし
     - 該当なし
     - 該当なし
     - 単独の review-fix-planning と loop 経由は、判断の記録だけからは区別できない
     - 不要。読み取り側が loop の記録から導く
     - 2
   * - 取らないもの
     - ``run_roles`` の出力 ``<output-dir>/roles.json`` ・ ``fix-check-receipt/dispatch.json`` （版の欄なし）、 ROUND_DIR ・ LAND_ROOT ・ REPRO_ROOT の中身
     - ``run_roles``
     - 該当なし
     - 一時ディレクトリ
     - 後片付けで消える
     - 評価に必要な情報は判断の記録と planning.json にある
     - 取らない。有効化前に消えたものは復元できない
     - 5

.. _review_eval_sources_verify:

review-verify
=============

関数とファイルは ``.agents/skills/review-verify/SKILL.md``・ ``scripts/dispatch.py`` と ``.agents/scripts/review_findings.py`` である。
収集の接続は、 ``review_findings.py validate`` の成功後（ ``_collect_before_replacement`` 。 ``--no-project`` で動くため shim の subprocess）と、 ``dispatch.accept`` の後（ ``dispatch.collect_accepted`` ）である。
``validate`` の後の収集は sweep だけで、同じ HEAD をやり直す ``mv`` の前に今の版を保存する

.. list-table::
   :header-rows: 1
   :widths: 8 12 8 8 10 10 10 20 6

   * - 項目
     - 取得元・形式/版
     - 書き手
     - 保存時点
     - 保存場所
     - 保持条件
     - 不足時の状態
     - 改修要否と理由
     - 分類
   * - 確認結果（cycle）
     - ``review-findings/v2`` 、 ``kind: verified`` 、 ``mode: cycle`` と、同名の ``.md``
     - エージェントが一時ファイル（ ``.`` 始まり）に Write し、 ``review_findings.py validate`` の後に ``mv`` する。コード上の書き手は無い
     - ``mv`` の前（ ``validate`` ）
     - ``verified/<HEAD12>.json`` ・ ``.md``
     - 同じ HEAD でやり直すと置き換わり、前の版は残らない
     - ``validate`` を通さずに置き換えた版は取れない。 ``mv`` の後に置き換えが起きる版は、次の ``validate`` か reconcile で取る
     - 要。 ``validate`` の成功後に、置き換え前の今の版を sweep で保存する。SKILL.md の文面は変えない
     - 1（現行の版）、3（置き換えられる版）、5（有効化前に置き換わった版）
   * - fix_check の確認結果
     - ``kind: verified`` 、 ``mode: fix_check`` （ ``fix_evidence`` 必須）、 ``statement-candidates.json`` （v1 固定）
     - 外部担当なら ``dispatch.accept`` （ ``os.replace`` ）、self ならエージェント。 statement-candidates は ``review_findings.py statement-candidates`` が ``_write_atomically`` で書く
     - ``accept`` の後
     - ``verified/<to12>.fix-check.json`` ・ ``.fix-check.md`` ・ ``<to12>.statement-candidates.json``
     - 同じ HEAD でやり直すと置き換わる
     - self の確認結果は ``validate`` の sweep で取る
     - 要。 ``accept`` の後に収集する
     - 1、3
   * - 1 件の項目（確認結果の内容）
     - id ``V-<n>`` 、sources、property、locations、verdict（confirmed・not_reproduced・unverified）、basis、reproduction、missing_checks など。fix_check の新規の問題は sources が空
     - 同上
     - 同上
     - 同上
     - 同上
     - 再現の作業ログは ``$REPRO_ROOT`` （ ``.agents/skills/review-verify/SKILL.md`` が ``${TMPDIR:-/tmp}`` の下の ``mktemp -d`` で作る。移植元と同じ ``/tmp`` 側）にしか無く、後片付けで消える
     - 不要。項目は取れる。作業ログは永続しないので取らない
     - 1、5（作業ログ）
   * - 対象の完全な SHA
     - 文書の ``reviewed_head`` ・ ``base`` ・ ``fix_check.from`` / ``to`` は 40 桁。ファイル名は 12 桁
     - 同上
     - 同上
     - 同上
     - 同上
     - なし
     - 不要
     - 1
   * - 入力 revision
     - ``inputs[]`` はファイル名と HEAD12 だけ。入力の findings・レビュー Markdown の hash は記録されない。 ``skill.sha256`` は SKILL.md の bytes
     - 同上
     - 同上
     - 同上
     - 同上
     - 入力の版を hash では特定できない。ファイル名と HEAD12 の対応でだけ辿れる
     - 不要。収集した findings の observation と、HEAD12 で突き合わせる（hash による確証は無い）
     - 1、5（入力の hash）
   * - 実行識別
     - ``verifier.provenance`` ・ ``mode`` ・ ``dispatch.json`` の有無
     - エージェント、 ``dispatch.py``
     - 同上
     - 同上
     - 同上
     - 単独と呼び出し元（loop・review-fix-planning）の区別の欄は無い。 mode、 provenance、 dispatch.json の有無、loop の cycle の参照で判別する
     - 不要
     - 2
   * - 時間
     - 確認結果は ``timing`` （開始・終了・秒数）と ``accuracy.verify_seconds`` を持つ（ ``review_findings.py`` が検査し、SKILL.md が書くよう定める）。工程の秒数は planning の ``activities`` にもある
     - 該当なし
     - 該当なし
     - 該当なし
     - 該当なし
     - 取った確認結果に ``timing`` ・ ``accuracy.verify_seconds`` が無い版は読めない。推測で埋めない
     - 不要
     - 1

.. _review_eval_sources_unrecoverable:

復元できないもの（分類 5）
==========================

収集を有効にする前に失われた版と、記録に出ない情報は復元できない。
読み取り側は推測で埋めず、 ``null`` と理由で残す

- 有効化前に上書きされた ``<model>.md`` と findings の前の版（作業 session の ``previous-review.md`` は cleanup で消える）
- review-fix-planning の round の途中の版（PR コメントの PATCH による上書き）と、 ROUND_DIR ・ LAND_ROOT ・ REPRO_ROOT の中身
- 有効化前に同じ HEAD でやり直した ``verified/<HEAD12>.json`` の前の版と、再現の作業ログ
- loop の記録が参照する ``review_sha256`` などのうち、参照先がすでに上書きされた版（ ``lost_revision`` として検出するだけで、復元はできない）
- Issue 番号、確認の所要時間、入力の hash など、そもそもどの記録にも無い情報
- 秘密検査を通らず ``withheld_secret`` になった本文（hash と大きさだけが残る）。大きさの上限（ ``too_large`` ）・秘密検査の失敗（ ``scan_failed`` ）で保存しなかった版は、同じ内容を後で取り直せば保存できるが、取り直す前にファイルが書き換わった版は失われる
- 旧配置（ ``reviews/#<N>-*`` ）は保留として件数だけ出す。 ``public/reviews/`` は範囲外で、取り込まない（手順は :ref:`review_eval_readme_legacy` ）
