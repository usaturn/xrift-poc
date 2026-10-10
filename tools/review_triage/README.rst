.. _review_triage:

======================================
review_triage の CLI（トリアージ助言）
======================================

.. _review_triage_overview:

概要
====

``tools/review_triage`` は、複数モデルのレビュー（ ``reviews/<PR>/`` ）の指摘を TypeSafe System One（Jev）に問い、指摘ごとの扱いの助言ドラフトを作る親リポジトリ専用の PoC である。
対象は開発環境のリポジトリ ``usaturn/xrift-poc`` だけで、制作物（ ``items/`` ・ ``worlds/`` ）のリポジトリは対象にしない。
CLI（ ``python -m tools.review_triage`` ）は、入力の確認・任意の API 評価・助言の保存を1つのコマンドで行う。
出力は助言であり、Fix / Skip / Hold / Resolved の確定や検証済みマークの付与は既存の review-fix-planning 手順で行う

.. _review_triage_prerequisites:

前提条件
========

- 親（ ``usaturn/xrift-poc`` ）の checkout の root で実行する
- ``tools/devenv`` の Python 環境を ``uv sync --project tools/devenv --locked`` で同期してある
- レビューの対象 HEAD の commit を、committed 内容を読む git の作業ツリー（ ``--source-repo`` 、既定は ``.`` ）が持っている
- 実送信（ ``--live`` ）だけが API キー ``TYPESAFE_API_KEY`` を使う。
  キーの用意と疎通確認は :ref:`access` に従う
- 実データを送る前に、送る state の内容ハッシュの許可を得る（Issue #391 のゲート）

.. _review_triage_flow:

手順の流れ
==========

#. dry-run で送信候補の state（送る本文）と内容ハッシュを書き出す。
   通信はしない
#. ``states.md`` で本文を確認し、送ってよい state の ``content_sha256`` を許可ファイルに書く
#. ``--live`` と許可ファイルで、許可した state だけを Jev へ送る
#. 保存した応答（ ``outcomes.json`` ）は ``--response-file`` でオフラインに再生できる
#. ``report.md`` の助言と ``human_review`` の指摘を、既存の review-fix-planning 手順で判断する

.. _review_triage_procedure:

手順
====

.. _review_triage_dry_run:

dry-run で送信候補を確かめる
----------------------------

#. 出力先に、存在しないディレクトリを指定して dry-run を実行する ::

       uv run --project tools/devenv --no-sync python -m tools.review_triage --review-dir 'reviews/usaturn/xrift-poc/#123-feat-example' --repository usaturn/xrift-poc --reviewed-head 0123abc --output-dir tmp/triage-123-dry

#. 標準エラーの要約で、state の件数（ ``sendable`` ・ ``awaiting_approval`` ・ ``blocked`` ・ ``withheld`` ）を確かめる

.. _review_triage_approve:

送信を許可する
--------------

#. ``tmp/triage-123-dry/states.md`` を開き、許可待ちの state の本文を読む。
   秘密の疑いがある state は本文を表示せず、送信もできない
#. 送ってよい state の ``content_sha256`` を許可ファイルに1行ずつ書く。
   ``#`` で始まる行と、値の後ろの ``#`` から行末まではコメントになる ::

       # PR #123 で送信を許可した state
       <content_sha256 の 64 桁>  # alpha-model/active/M-1

.. _review_triage_live:

許可した state だけを送る
-------------------------

#. キーを export したシェルで、 ``--live`` と許可ファイルを付けて実行する ::

       uv run --project tools/devenv --no-sync python -m tools.review_triage --review-dir 'reviews/usaturn/xrift-poc/#123-feat-example' --repository usaturn/xrift-poc --reviewed-head 0123abc --output-dir tmp/triage-123-live --live --allow-file tmp/triage-123-allow.txt

#. 送る request の数が ``--max-calls`` （既定 50）を超えるときは、何も送らずに終了コード 2 で止まる

.. _review_triage_replay:

保存した応答を再生する
----------------------

#. live の ``outcomes.json`` と同じ許可ファイルを渡して再生する。
   通信とキーは使わない ::

       uv run --project tools/devenv --no-sync python -m tools.review_triage --review-dir 'reviews/usaturn/xrift-poc/#123-feat-example' --repository usaturn/xrift-poc --reviewed-head 0123abc --output-dir tmp/triage-123-replay --response-file tmp/triage-123-live/outcomes.json --allow-file tmp/triage-123-allow.txt

#. 今回の request と ``request_sha256`` が一致しない outcome は使わない。
   件数は標準エラーの ``unmatched`` と ``run.json`` に出る

.. _review_triage_results:

結果確認
--------

#. 終了コードが ``0`` であることを確かめる ::

       echo $?

#. ``report.md`` の status と、 ``human_review`` の指摘を確かめる

.. _review_triage_options:

オプション
==========

.. list-table:: オプション
   :header-rows: 1
   :widths: 3 2 6

   * - オプション
     - 必須
     - 意味
   * - ``--review-dir DIR``
     - 必須
     - レビューディレクトリ。直下の ``*.md`` だけを読む
   * - ``--repository REPO``
     - 必須
     - ``usaturn/xrift-poc`` だけを受け付ける。ディレクトリ名から推定しない
   * - ``--reviewed-head SHA``
     - 必須
     - 対象 HEAD（7〜40 桁の小文字 hex）。 ``--source-repo`` で 40 桁に解決する
   * - ``--output-dir OUT``
     - 必須
     - 出力先。存在しないパスを指定する（親ディレクトリは必要）。レビューディレクトリの内側は指定できない
   * - ``--source-repo PATH``
     - 任意
     - committed 内容を読む git の作業ツリー。既定は ``.``
   * - ``--diff-base SHA``
     - 任意
     - 差分の基準 commit。指定したときだけ state に ``diff`` 節を足す
   * - ``--allow-file FILE``
     - live・再生で必須
     - 送信を許可する ``content_sha256`` の一覧。UTF-8（BOM 可）、改行は LF か CRLF
   * - ``--live``
     - 任意
     - 許可した state を Jev へ送る。 ``--response-file`` と同時に指定できない
   * - ``--response-file FILE``
     - 任意
     - 保存した ``outcomes.json`` を再生する
   * - ``--thresholds FILE``
     - 任意
     - ``Thresholds`` の JSON。省略時は ``jev-latest`` の暫定値
   * - ``--max-calls N``
     - 任意
     - live の送信回数の上限。既定は 50

.. _review_triage_outputs:

出力ファイル
============

.. list-table:: 出力ファイル
   :header-rows: 1
   :widths: 3 2 6

   * - ファイル
     - モード
     - 内容
   * - ``states.json`` ・ ``states.md``
     - すべて
     - 送信候補の state・内容ハッシュ・送れない理由。秘密の疑いがある state は本文を書かない
   * - ``outcomes.json``
     - live・再生
     - client の outcome（ ``ClientOutcome`` の配列）。再生と評価（#391）の入力になる
   * - ``report.json`` ・ ``report.md``
     - すべて
     - ``TriageReport`` と、その Markdown 表現（助言ドラフト）
   * - ``run.json``
     - すべて
     - 実行の記録（mode・対象 HEAD・入力ファイルの SHA-256・件数・anomaly・evidence の解決結果）

.. _review_triage_exit:

終了コードと人間確認
====================

終了コードは ``0`` （レポートを生成した）と ``2`` （入力・利用方法のエラー）である。
想定外の例外（不具合）は Python の既定どおり ``1`` で終わる。
``2`` のときは出力ディレクトリを作らない。
標準エラーの形式は、エラーの種類で異なる

- 引数の誤り（必須オプションの欠落、 ``--allow-file`` の無い ``--live`` など）では、 ``argparse`` が ``usage:`` で始まる使い方の説明と ``python -m tools.review_triage: error:`` で始まる行を出す
- 入力の検証エラー（ ``--reviewed-head`` の形式・解決、出力先、許可ファイルなど）では、 ``review_triage: error:`` で始まる理由を1行出す

次の場合もレポートを生成して ``0`` で終わり、該当する指摘を ``human_review`` に回す。
標準エラーは、既存の review-fix-planning 手順へ戻るよう案内する

.. list-table:: 人間確認に回る場合
   :header-rows: 1
   :widths: 4 5

   * - 場合
     - レポート
   * - dry-run、または許可した state が1件も無い
     - ``fallback`` 。全件 ``human_review``
   * - キーが無い（ ``no_key`` ）、ネットワーク障害・429・5xx などの失敗
     - ``fallback`` 。全件 ``human_review``
   * - 許可していない state、根拠の抜粋を作れない指摘（ ``missing_evidence`` ）、秘密の疑い
     - その指摘は送らず、 ``missing_outcome`` で ``human_review``
   * - 解釈できないブロック（未対応の文書）
     - ``report.md`` に未解釈の件数を出す。元のレビューを確認する
   * - 閾値の model と request の model が異なる
     - ``uncalibrated`` 。全件 ``human_review``

.. _review_triage_limits:

制約
====

- 根拠の抜粋は、 ``- 位置:`` のバッククォートで囲んだ ``path:行`` （ ``path:a-b`` ・ ``:N`` を含む）だけから作る。
  前後5行を足し、1指摘あたり6件までとする。
  行番号の無い参照、 ``#`` を含むパス、symlink、UTF-8 でないファイルは使わない
- 省略されたパス（ ``SKILL.md:29`` など）は、同じ ``- 位置:`` で既に解決したパスとの後方一致で1件に決まる場合だけ解決する
- 重複候補の組は、別のレビューファイルの指摘で、同じパスの参照の間が5行以内のものに限る
- ``## Unverified candidates`` 節は input が読み飛ばすため、トリアージしない
- ``--review-dir`` の祖先ディレクトリの差し替えは、ディレクトリを開くまでの間は防げない
- git は ``rev-parse`` ・ ``ls-tree`` ・ ``cat-file`` ・ ``diff`` だけを使い、作業ツリー・index・refs へ書き込まない。
  bare リポジトリは対象外である
- 通常の検証（ ``./scripts/ci/verify-devenv.sh`` ）は、通信と実キーを使わない

.. _review_triage_references:

関連資料
========

- :ref:`interfaces` （共通型・署名・エラー種別の定義元）
- :ref:`access` （API キーと疎通確認）
- `親 Issue #330 <https://github.com/usaturn/maatlog-poc/issues/330>`__
- `cli Issue #389 <https://github.com/usaturn/maatlog-poc/issues/389>`__
- `evaluation Issue #391 <https://github.com/usaturn/maatlog-poc/issues/391>`__

以上
