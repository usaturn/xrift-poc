.. _review_eval_readme:

==================================================
review_eval の運用手順（レビュー評価データの収集）
==================================================

.. _review_eval_readme_overview:

概要
====

``tools/review_eval`` は、レビューの意味判定の精度を継続して改善するために、4 経路（pr-review、pr-review-fix-loop、review-fix-planning、review-verify）が保存するレビュー記録を、上書き・削除される前に出典付きで私有の保存先へ写す。
その上で、確認待ち台帳に、主張の分割・系列・許可・分割（calibration と held-out）・独立した正解と盲検の予測の受け渡しを記録する。
CLI は ``python -m tools.review_eval`` である。

4 経路が実際に何を保存しているかは :ref:`review_eval_sources` に書く。

収集は、設定ファイルが無い限り何もしない。
有効にするには、設定ファイルに ``enabled = true`` と承認の記録（ ``approved_by`` ・ ``approved_on`` ）を書き、保存先を ``store init`` で初期化する。
このPRは仕組みだけを作る。実データの収集を有効にすることと、過去の記録の調査（90 分の調査）は、このPRの範囲に入れない。

.. _review_eval_readme_prerequisites:

前提条件
========

- 親リポジトリ（ ``$XRIFT_POC_ROOT`` の main checkout）の root から実行する。 ``uv sync --project tools/devenv --locked`` で環境を同期してある
- 制作物の clone、モデル、API キーは要らない。 ``reviews/`` と、親・制作物の Git の origin（ ``route_target.py`` の判定）だけを読む
- 秘密検査に gitleaks を使う（既定）。Dev Container の承認済みの版を使い、再導入しない

コマンドは次の形で実行する。
以降は ``review-eval`` と略して書く。

.. code-block:: bash

   uv run --project tools/devenv --no-sync python -m tools.review_eval <サブコマンド> ...

.. _review_eval_readme_config:

設定
====

設定ファイルのパスは、環境変数 ``REVIEW_EVAL_CONFIG`` で決める。
空文字を設定すると、明示的に無効になる。
未設定のときは、 ``XDG_CONFIG_HOME`` が絶対パスなら ``$XDG_CONFIG_HOME/xrift-poc/review-eval.toml`` を、それ以外（未設定・空・相対パス）なら ``~/.config/xrift-poc/review-eval.toml`` を読む。
ファイルが無ければ無効で、何も書かない。

保存先（ ``store`` ）は、設定で絶対パスを明示する必須の項目である。
コードの既定値は無い。
下の例の ``$XRIFT_POC_ROOT/private/review-eval/collected`` （親の main checkout はホストの bind mount なので、Dev Container を再構築しても残る）は運用の推奨であり、既定値ではない。
``private/`` は親の ``.gitignore`` で無視している。
``store`` は ``workspace_root/reviews`` ・ ``workspace_root/items`` ・ ``workspace_root/worlds`` の中には置けない（レビュー記録の置き場所と、制作物の Git リポジトリを避けるため。置くと設定の誤りになる）。
``tmp/`` や ``/tmp`` に置く設定は拒否しないが、使い捨ての領域なので勧めない。

.. code-block:: toml

   schema_version = 1
   enabled = true
   approved_by = "利用者またはデータ管理担当の名前"
   approved_on = 2026-10-30
   store = "/workspaces/xrift-poc/private/review-eval/collected"
   workspace_root = "/workspaces/xrift-poc"
   max_store_bytes = 1073741824
   # 以下は省略できる（既定値あり）
   # max_artifact_bytes = 4194304
   # max_sweep_files = 2000
   # scan_timeout_seconds = 60.0
   # lock_timeout_seconds = 10.0
   # secret_scanner = ["gitleaks", "dir", ...]  # {source} と {report} を置換する argv

   [[sources]]
   repo = "usaturn/xrift-poc"
   routes = ["pr-review", "pr-review-fix-loop", "review-fix-planning", "review-verify"]
   since = 2026-09-01
   note = "取得の理由"

- ``enabled = true`` のときは、 ``approved_by`` ・ ``approved_on`` ・ ``sources`` が必須である
- ``sources`` の ``repo`` は、 ``route_target.py`` の判定で次のどちらかの ``repository`` と一致する（大文字・小文字を区別しない）ものだけ書ける。どちらでもなければ設定の誤り（終了コード 2）になる

  - ``workspace_root`` の判定が ``env`` のときの ``repository`` （親の ``usaturn/xrift-poc`` ）
  - ``workspace_root/items/<name>`` ・ ``workspace_root/worlds/<name>`` の判定が ``product`` で、 ``repository`` が null でないときの ``repository`` （Git 管理され、origin が GitHub の制作物）

  Git 未初期化の制作物と origin の無い制作物（判定が ``stop`` ）は PR が無く、対象にできない。判定に合う repo でも、 ``sources`` に書かない限り収集の対象は広がらない
- ``routes`` は 4 経路の部分集合である。 ``since`` ・ ``until`` はファイルの更新時刻（UTC の日付）で判定する。仮想記録は取得時刻で判定する
- 設定を確認するには ``review-eval config check`` を使う。 ``enabled`` と ``sources`` を JSON で出す。誤りがあれば終了コード 2 になる

.. _review_eval_readme_rebuild:

Dev Container の再構築と個人設定
========================================

設定ファイルは ``~/.config/xrift-poc/review-eval.toml`` に置く個人設定で、 ``~/.config`` は Dev Container が bind mount していない。
Dev Container を再構築すると設定ファイルは消え、収集は無効（設定が無い状態）に戻る。
再構築後に収集を続けるには、上の例から設定ファイルを書き直し、 ``review-eval config check`` で確かめる。
``store`` を推奨の ``$XRIFT_POC_ROOT/private/review-eval/collected`` に置いていれば、収集済みのデータは再構築後も残る。
個人設定を自動で作らず、リポジトリに雛形も置かない（承認は利用者ごとのデータ管理の判断であり、リポジトリに置くと承認なしに有効な状態を配りうるため）。

.. _review_eval_readme_scope:

保存するもの、しないもの
========================

保存するのは、各経路が ``reviews/`` に既に保存しているレビューの記録ファイル（ ``<model>.md`` ・ ``findings/*.json`` ・ ``runs/*.json`` ・ ``verified/*`` ・ ``loop/<run_id>/`` の記録）と、判断の記録・finalize の結果の仮想記録だけである。
本文は秘密検査（gitleaks）を通ったものだけを保存し、検出したものは hash と大きさだけを残す（ ``secret_withheld`` ）。

次は保存しない。

- 会話ログ、生ログ（ ``*.log`` ）、runner の応答本文（stdout・transcript）
- 環境変数の値、認証情報
- symlink の先（symlink をたどらない）
- 許可パターンの外のファイル

.. _review_eval_readme_enable:

有効にする前の確認
==================

実データの収集を有効にする前と、過去の記録の調査（90 分の調査）を始める前に、利用者（またはデータ管理担当）が次を確認する。
この確認が済むまでは ``enabled = true`` にしない。

- 出典（どの repo の記録か）
- 期間（ ``since`` ・ ``until`` ）
- 範囲（どの経路か。 ``routes`` ）
- 保存先（ ``store`` の絶対パス。私有の場所であること）
- 容量と時間の上限（ ``max_store_bytes`` ・ 各 timeout）
- 利用条件（ローカルでの利用だけか。モデルへの提示や外部送信は別の許可で、収集の許可に含めない）
- 確認した人の名前と日付を ``approved_by`` ・ ``approved_on`` に書く

.. _review_eval_readme_start:

初期化、起動、停止
==================

#. 設定ファイルを書く（上の例）。まだ ``enabled`` を ``false`` にしておいてもよい。
#. ``review-eval config check`` で設定を確認する。
#. ``review-eval store init --store <絶対パス>`` で保存先を初期化する。初期化していない保存先には収集しない。
#. ``enabled = true`` にする。これで 4 経路の保存点から自動で収集する。
#. 停止するには、 ``enabled = false`` にするか、設定ファイルを移す・消す。 ``REVIEW_EVAL_CONFIG=`` （空文字）でも止まる。保存済みのデータは消えない。

``review-eval status`` で、有効か無効か、保存先、observation の数、blob の合計バイト数、未処理の failure を確認できる。
設定ファイルが無ければ ``{"enabled": false}`` を出して終了コード 0 になる。
未処理の failure があれば終了コード 1 になる。
有効なのに保存先を初期化していなければ、終了コード 2 になる。

保存先の形は ``review-eval-store/v1`` で、ディレクトリは 0700、ファイルは 0600 である。
blob と observation は一時ファイルと ``os.link`` で作り、既存を上書きしない。

.. _review_eval_readme_capture:

自動取得の接続点
================

収集は、4 経路が既存の保存 helper の中から呼ぶ。SKILL.md の文面は変えていない。接続点は ``.agents/`` の下の helper である。

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - 経路
     - 接続点
     - 取るもの
   * - pr-review
     - ``review_run.py`` の ``main()`` の最後（ ``prepare`` ・ ``complete`` ・ ``fail`` ・ ``attach-runner`` ）
     - ``<model>.md`` 、 ``findings/*.json`` 、 ``runs/*.json`` 。 ``prepare`` は上書き前の版を保存する。runner を使ったレビューでは、レビュアー（runner の子）の ``prepare`` ・ ``complete`` ・ ``fail`` は収集せず、runner の after_run が起動する ``attach-runner`` が実行記録と review_dir の sweep で取る
   * - pr-review-fix-loop
     - ``loop.py`` の ``review`` ・ ``cycle_end`` ・ ``finish`` ・ ``recover`` の最後
     - ``loop/<run_id>/`` の記録、判断の記録と finalize の結果の仮想記録。 ``finish`` ・ ``recover`` で失われた版を検出する
   * - review-fix-planning
     - ``upsert_pr_comment.py`` の投稿成功後、 ``finalize.py`` の結果保存後、 ``run_roles.py`` の最後
     - 判断の記録（ ``@pr-comment/round-<n>/record.json`` 。 ``upsert_pr_comment.py`` と、投稿した ``finalize.py`` の両方で取る。同じ内容は 1 つの observation になる）、finalize の結果、 ``planning.json``
   * - review-verify
     - ``review_findings.py validate`` の成功後（subprocess）、 ``dispatch.py`` の ``accept`` の後
     - fix-check の JSON と statement-candidates。 ``validate`` は置き換え前の今の版を保存する

- 収集は例外を呼び出し元に出さない。呼び出し元の終了コードと標準出力の JSON は変わらない。標準エラーには、失敗または保留のときだけ ``review-eval: <status>: <reason>`` の 1 行を出す。収集の呼び出しで想定外の例外（設定の誤り、保存先の未初期化、 ``tools.review_eval`` の import の失敗など）が出たときは、 ``review-eval: hook failed: <例外の型名>`` の 1 行だけを出す。収集が黙って動かないまま「導入済み」に見えないようにするためで、本文・秘密の値・パスは出さない
- ``finalize.py`` は ``tools.review_eval`` を import する前にリポジトリの root を ``sys.path`` に入れる。本番の起動形（ ``uv run --project tools/devenv --locked python .../finalize.py`` ）は root を ``sys.path`` に入れず、pytest の ``pythonpath`` も無いためである
- agent-runner の子プロセス（環境変数 ``AGENT_RUNNER_DEPTH`` が空でない）では収集しない。私有の保存先に触れるのは、司令塔側のプロセスだけにする。子が保存した成果は、司令塔側の保存点（loop の ``review`` の後、 ``run_roles`` の後、pr-review の ``attach-runner`` ）の sweep か、 ``reconcile`` で取る。 ``attach-runner`` は runner の after_run（設定の信頼された argv）が起動し、モデルが動くプロセスではないので、環境に ``AGENT_RUNNER_DEPTH`` があっても収集する
- 呼び出しのたびに、同じ review_dir の許可パターンのうち未観測のファイルを取る（sweep）。 ``max_sweep_files`` を超えると ``sweep_limit`` で保留する
- 取るのは許可された経路・パターンの記録だけである。会話ログ、生ログ（ ``*.log`` ）、環境変数、認証情報は取らない。symlink をたどらない

.. _review_eval_readme_manual:

手動の取り込み、reconcile、再開
===============================

過去の記録、取りこぼし、中断した収集は、手動の経路で取る。
手動の経路は自動取得と同じ関数を通る（ ``trigger`` は ``manual`` ）。

``review-eval ingest --review-dir <dir>`` または ``review-eval ingest --repo <owner>/<repo> [--pr <N>]``
  review_dir を取り込む。結果を JSON で出す。全件が ``captured`` ・ ``disabled`` ・ ``out_of_scope`` なら終了コード 0、保留や失敗を含めば 1。設定ファイルが無い・誤りは 2。

``review-eval reconcile [--repo <owner>/<repo>] [--apply]``
  設定した ``sources`` の review_dir を走査して、次を出す。

  - ``unobserved`` ：今の内容がまだ観測されていないファイル
  - ``pending_failures`` ：未処理の failure
  - ``lost_revisions`` ：loop の記録が参照するが、観測も今の内容も無い版（復元不能）
  - ``counts`` ：期間外（ ``out_of_period`` ）、未分類（ ``unclassified`` ）、旧配置（ ``legacy_layout`` ）の件数

  ``--apply`` を付けると ``unobserved`` を取り込み、 ``lost_revisions`` を failure として記録する。
  終了コードは、 ``unobserved`` 、 ``pending_failures`` 、 ``lost_revisions`` のどれかが（ ``--apply`` の後も）残れば 1、全部空なら 0 である。
  期間外・未分類・旧配置の件数だけでは 1 にならない。

``review-eval inventory --repo <owner>/<repo> --pr <N>``
  observation を path ごとの revision の列で出し、loop のサイクル、pr-review の実行、仮想記録、未処理の failure、保存しなかった本文の数を対応付ける。足りない情報は推測で埋めない。

中断したら、同じコマンドをもう一度実行する。
同じ ``(場所, 内容)`` は 1 つの observation にしかならないので、再取得しても二重に登録しない。
並行して取得しても同じである。

.. _review_eval_readme_restore:

保存、復元、照合
================

``review-eval verify``
  全 blob を再 hash し、observation が参照する blob の存在を確かめる。問題があれば ``problems`` に出して終了コード 1。

``review-eval restore --dest <dir> (--repo <owner>/<repo> [--pr <N>] | --observation <ID>)``
  blob を元の相対パスへ書き戻し、書いた後に sha256 を照合する。
  ``--dest`` は存在しないか空のディレクトリにする。空でなければ終了コード 2 で止まる。
  結果は path ごとの ``status`` で出し、全部が ``restored`` なら終了コード 0、そうでなければ 1 である。
  ``withheld_secret`` の本文は保存していないので、復元できない（hash と大きさだけが残る）。

.. _review_eval_readme_failures:

failure の確認と ack
====================

収集の失敗・保留は、保存先の ``failures/`` に追記する。
本文・秘密の値は書かない。
未処理の failure は ``status`` と ``reconcile`` に出る。

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - kind
     - 意味
   * - ``store_not_initialized``
     - 保存先が未初期化。 ``store init`` を実行する
   * - ``identity_mismatch``
     - 場所の repo・PR と、文書の ``pr.repository`` ・ ``pr.number`` が食い違う。保存しない（同じ番号の別 repo を混ぜない）
   * - ``legacy_layout``
     - 旧配置（ ``reviews/#<N>-*`` ）。自動では取らず保留する（次の節）
   * - ``out_of_period``
     - ``since`` ・ ``until`` の期間外
   * - ``secret_withheld``
     - 秘密検査に引っかかったので、本文を保存していない
   * - ``too_large``
     - ``max_artifact_bytes`` を超えたので、本文を保存していない。observation は作らないので、上限を上げた後に同じ内容を取れば保存し、failure は解消する
   * - ``scan_failed``
     - scanner が無い、timeout、想定外の終了コード、レポートを読めない。本文を保存していない。observation は作らないので、scanner を直した後に同じ内容を取れば保存し、failure は解消する（ファイルは次の sweep か ``reconcile --apply`` 、仮想記録は同じ bytes を渡す次の収集で取る）
   * - ``capacity_held``
     - ``max_store_bytes`` を超えるので保存していない。既存のデータは消さない
   * - ``sweep_limit``
     - sweep が ``max_sweep_files`` に達した。続きは ``reconcile --apply`` で取る
   * - ``lock_timeout``
     - lock を ``lock_timeout_seconds`` 内に取れなかった。再実行する
   * - ``lost_revision``
     - loop の記録が参照するが、観測も今の内容も無い版。復元できない
   * - ``unexpected_error``
     - 想定外の例外。原因を調べる

人が内容を確認したら、 ``review-eval failures ack <ID> --by <名前> --reason <理由>`` で未処理から外す。
failure は消えない（ack を記録するだけ）。
ID は ``status`` の ``pending_failures`` に出る。

秘密検査の誤検出は、設計として保存できない。
``secret_withheld`` は hash と大きさだけを残し、ack はできるが、本文を確認して保存する経路は作っていない（持ち越し）。
必要になったら別の変更として扱う。

.. _review_eval_readme_legacy:

旧形式、旧配置の制約
====================

- 現行の形式（ ``review-findings/v2`` ・ ``review-records/v2`` など）と新配置（ ``reviews/<owner>/<repo>/#<N>-<branch>/`` ）は、 ``ingest`` と ``reconcile`` で取れる。形式の版が未知の文書は ``format.known = false`` として取り込み、読み取りで推測しない
- 旧配置 ``reviews/#<N>-*`` は ``legacy_layout`` として保留する。 ``reconcile`` は件数だけ出す。取り込むには、利用者が repo を確かめた上で、ディレクトリを ``reviews/<owner>/<repo>/#<N>-<branch>/`` の下へ複製して（元は移さない）、新配置の場所に対して ``ingest`` する。repo の決定は人が行う。文書に ``pr.repository`` があれば、場所との照合で食い違いを検出できる
- ``public/reviews/`` は、今は作られない配置で、 ``reviews/`` の外にあるため範囲外である。取り込まない
- 収集を有効にする前に失われた版は取れない（ :ref:`review_eval_sources_unrecoverable` ）

.. _review_eval_readme_ledger:

確認待ち台帳の手順
==================

台帳は ``<store>/ledger/journal.jsonl`` に op を 1 行ずつ追記する（ ``review-eval-ledger/v1`` ）。
過去の op は書き換えない。
台帳のコマンドは、すべて ``--by <識別子>`` が必須である。

識別子の規則
------------

人・担当の識別は、 ``--by`` と packet の ``--worker`` で同じ空間として照合する。
同じ人は、どの操作でも同じ識別子を使う。
識別子の空白の違いと大文字・小文字は区別しない。

- 正解担当（g1・g2・adjudicator の packet の worker または回答の worker）だった識別子と、正解の回答を取り込んだ操作者（ ``packet import`` の ``--by`` ）、adjudicator の packet を書き出した操作者、対象の系列に露出（ ``expose`` ）がある識別子は、evaluator になれない。evaluator の packet の書き出しと、その回答の取り込みの両方で検査する。取り込みでは、回答する packet の書き出しが記録した ``seen`` の露出だけを理由にしない
- 逆に、予測を見た識別子（evaluator の packet の worker、evaluator の回答の worker、evaluator の回答を取り込んだ ``--by`` ）は、正解担当になれない

手順
----

#. ``ledger sync --by <識別子>`` ：観測済みの findings の ``findings[]`` ・ ``unverified_candidates[]`` と、verified の新規の問題（ ``sources`` が空）を候補として登録する。何度実行しても同じ結果になる。同じ HEAD の再レビューで同じ ID が別の内容を指せば、別の候補になる。JSON の無い Markdown だけのレビューは ``markdown_only`` の候補 1 件になる。
#. 候補の状態：``ledger candidates [--repo R] [--status S]`` で一覧を見て、 ``ledger candidate-status <ID> --status <状態>`` で ``pending`` ・ ``selected`` ・ ``not_selected`` ・ ``no_permission`` ・ ``missing`` ・ ``failed`` ・ ``held`` にする。 ``pending`` 以外は ``--reason`` が必須である。 ``ledger show <ID>`` で候補の内容を確認できる。
#. 主張：``ledger claim create --candidates <ID...> --after-sha <40桁> --language <言語> --state-file <JSON>`` で単一主張の leaf を作る（ ``--before-sha`` ・ ``--issue`` は任意）。 ``selected`` の候補（同じ repo・PR）だけを親にできる。state は ``{claim, diff, evidence[]}`` で、evidence は ``{at: before または after, path, start_line, end_line, text}`` である。匿名化（パス・名前の置き換え）は、人が state を書くときに行う。state が 16,384 bytes を超えれば拒否する。1 件の指摘に複数の主張があれば、主張ごとに別の leaf を作る。修正は ``ledger claim revise <ID> --state-file <JSON>`` で新しい revision を作る（以前の確認・回答は引き継がない）。
#. 確認：``ledger claim confirm <ID>`` で、その revision の分割・内容を人が確認した記録を付ける。
#. 関係・系列：``ledger relate <左> <右> --relation <種類>`` で ``same_defect`` ・ ``before_after`` ・ ``near_duplicate`` ・ ``same_pr_issue`` ・ ``template_derived`` を付ける。同じ repo・PR の主張には ``same_pr_issue`` が自動で付く。別 repo であることは独立の根拠にしない。系列は関係でつながった主張の集まりで、 ``ledger family confirm <主張ID>`` で構成を確認する。主張や辺が増えて構成が変わると、確認は失効する。
#. 許可・露出：``ledger permit --subject <主張ID または repo:<owner/repo>> --scope <範囲> (--grant または --deny)`` で ``local_use`` ・ ``runtime_presentation`` （ ``--runtime`` が必須）・ ``publication`` ・ ``external_send`` を別々に付ける。 ``ledger expose <主張ID> --who <識別子> --kind <pilot・tuning・known・seen>`` で人・担当の露出を記録する。
#. 分割：``ledger split <主張ID> --split <calibration・held_out・excluded> --reason <理由>`` を系列ごとに割り当てる。 ``held_out`` は系列の確認済みで、かつ ``pilot`` ・ ``tuning`` の露出が無いことが必要である。後から ``tuning`` が付いた held_out は無効になる。
#. packet：``ledger`` ではなく ``packet export --role <g1・g2・adjudicator・evaluator> --worker <識別子> --runtime <名前> --claims <主張ID...> --out <新しいディレクトリ> --by <識別子>`` で、役割別の ``packet.json`` を書く。主張 ID・候補の参照・正解・他の担当の回答・最終採否・予測・担当名は入らない。正解担当の item には、同じ SHA で根拠を照合するための ``target`` が付く。evaluator の item は state だけで、repo と SHA を含まない。 ``local_use`` と ``runtime_presentation`` の許可、g2 は g1 と別の担当、adjudicator は G1・G2 と別で不一致の item だけ、evaluator は split の割り当て済みが必要である。同じ役割の回答は revision ごとに 1 つだけで、回答が既にあれば同じ役割の packet の書き出しと回答の取り込みを拒否する。このPRはモデルを実行せず、packet の受け渡しは人が行う。
#. 回答：担当が ``review-eval-answers/v1`` （ ``packet_id`` ・ ``worker`` ・ ``answers[{item_id, label, rationale, evidence_refs}]`` ）を書き、 ``packet import --answers <file> --by <識別子>`` で取り込む。ラベルは ``supports`` ・ ``contradicts`` ・ ``not_addressed`` ・ ``insufficient_context`` のどれかである。item の state が今の revision と違えば stale として拒否する（旧 revision の回答を転用しない）。
#. 整備時間：``ledger time --stage <selection・anonymization・annotation・adjudication・storage・other> --seconds <秒> [--claims ...]`` で、データ整備にかかった時間を記録する。レビューの工数とは分ける。
#. 集計：``ledger stats [--repo R]`` で、候補の状態、主張（下書き・確認済み）、系列、split、言語、正解ラベル、欠損、整備時間、適格件数、未確定の件数を出す。

適格は計算で決め、自動で採用しない。
現 revision が確認済み、系列が確認済み、 ``local_use`` の許可あり、正解が完了（G1・G2 が一致、または不一致を adjudicator が裁定）、split が ``calibration`` か有効な ``held_out`` のすべてを満たしたときだけ適格になる。
``ledger show <主張ID>`` で ``eligible`` と、満たしていない理由（ ``reasons`` ）を確認できる。
拒否された操作は、理由を標準エラーに出して終了コード 2 になる。

.. _review_eval_readme_notes:

収集担当向けの注意
==================

- held-out は採点しない。 ``ledger stats`` は予測と正解を突き合わせる処理を持たず、held-out は件数だけを出し、ケース別には出さない
- モデルや runtime への提示は、収集の許可とは別の許可（ ``runtime_presentation`` ）が必要である。許可なしに packet を書き出せない
- 実データ（収集した記録・台帳・正解・予測）は commit しない。保存先は ``private/`` などの私有の場所に置く。テストと fixture は ``private/`` に書かない
- 最終的な採否（Fix または Skip）と verified の判定を、正解ラベルへ変換しない
- 実データでの収集の有効化と過去記録の調査は、 :ref:`review_eval_readme_enable` の確認（出典・期間・範囲・保存先・上限・利用条件）を利用者またはデータ管理担当から得てから行う。このPRには含まれない

.. _review_eval_readme_extend:

経路・保存点を足す手順
======================

新しい経路や保存点を足すときは、次の 3 点を行う。

#. ``capture.py`` の ``classify`` に、その経路の許可パターンを足す。パターンに入らないファイルは取らない。
#. 保存点の helper から ``capture.collect`` を呼ぶ。 ``uv run --no-project`` で起動される helper は pydantic を使えないので、 ``tools/review_eval/shim.py`` をファイルパスで読み込み、subprocess で ``hook`` を起動する（ ``review_findings.py`` と ``upsert_pr_comment.py`` が例）。
#. :ref:`review_eval_sources` に行を足し、分類（1 から 5）と、改修の要否・理由を書く。

足すときの注意を次に挙げる。

- 収集は、呼び出し元の結果・終了コード・標準出力を変えてはいけない。 ``tools.review_eval`` の import は関数の中で行い（lazy import）、呼び出しは ``except Exception`` で囲む。
- agent-runner の子（ ``AGENT_RUNNER_DEPTH`` が空でない）では収集は動かない。子の成果は、司令塔側の保存点の sweep か ``reconcile`` で取る。
- テストは ``REVIEW_EVAL_CONFIG`` を ``monkeypatch.setenv`` で一時の設定に向ける。全体では isolation plugin が無効にしている（次の節）。

.. _review_eval_readme_tests:

テストの隔離
============

テストは、保存先と設定を ``tmp_path`` に置き、 ``monkeypatch.setenv("REVIEW_EVAL_CONFIG", <一時の設定>)`` で向ける。
``$XRIFT_POC_ROOT/private/`` と実の ``~/.config`` は読まない・書かない。
``tools/test_support/isolation.py`` のプラグインが、テストの起動時に ``REVIEW_EVAL_CONFIG`` を空文字（明示的に無効）にし、利用者の設定を読ませない。
収集を有効にして確かめるテストは、テストの中で一時の設定に上書きする。
補助関数は ``tools/test_support/review_eval.py`` にある（親の main checkout と制作物を作る ``make_collection_workspace`` ・設定を書く ``write_collection_config`` ）。

検証は、親 worktree の root で ``./scripts/ci/verify-devenv.sh`` を引数なしで実行する。
