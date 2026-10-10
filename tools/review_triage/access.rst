.. _access:

===========================
TypeSafe API キーと疎通確認
===========================

.. _access_overview:

概要
====

``tools/review_triage`` の client（ ``client.py`` ）は、TypeSafe System One（Jev）の API キーを環境変数 ``TYPESAFE_API_KEY`` だけから読む。
キーは任意であり、未設定でも Dev Container の起動と ``./scripts/ci/verify-devenv.sh`` は成功し、client は通信せずに ``no_key`` の失敗を返す。
この文書は、キーを用意する人間の作業（Phase 0）、キーの扱い、疎通の確認方法を定める

.. _access_prerequisites:

前提条件
========

- 親リポジトリ（ ``usaturn/maatlog-poc`` ）の checkout の root で作業する
- ``tools/devenv`` の Python 環境を ``uv sync --project tools/devenv --locked`` で同期してある
- 疎通確認には、Phase 0 で取得した API キーと ``api.typesafe.ai`` へ proxy なしで HTTPS 接続できるネットワークが要る。
  キーなしの確認はどちらも要らない

.. _access_flow:

手順の流れ
==========

#. 人間が Phase 0 で API キーを用意する
#. キーを使うシェルでだけ export する
#. キーなしの確認で、キーが無いと client が通信しないことを確かめる
#. 疎通確認で、固定の合成 request を1回だけ送る
#. 結果の行を確かめ、失敗したときは種別に応じて対処する

.. _access_procedure:

手順
====

.. _access_phase0:

Phase 0（人間の作業）
---------------------

次の作業は人間が行う。
エージェントは申込み・キーの作成・秘密の登録を行わない

#. TypeSafe の early access（waitlist 制）に申し込む
#. 利用を認められたら、console（ https://console.typesafe.ai/keys ）で API キーを作る
#. 作ったキーを :ref:`access_key_handling` に従って、使うシェルへ渡す
#. 実データ（過去のレビュー）を送る前に、評価 Issue #391 のゲートで送信対象の内容ハッシュの許可を得る

Phase 0 の完了・未完了は Issue #385 と #391 に記録する。
この文書はキーを取得済みであることを示さない

.. _access_key_handling:

キーの扱い
----------

キーはリポジトリ・ ``devcontainer.json`` ・シェルの設定ファイルに保存せず、使うシェルでだけ export する。
コマンド履歴に残さないため、値をコマンド行に書かず ``read`` で入力する ::

    read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY
    # ここで疎通確認や CLI を実行する
    unset TYPESAFE_API_KEY

- キーを Issue・PR・ログ・レビュー文書へ貼らない
- client はキーを ``Authorization`` ヘッダの組み立てにだけ使い、結果（ ``ClientOutcome`` ）や ``repr`` に出さない
- 使い終わったら ``unset`` する

.. _access_offline_check:

キーなしの確認
--------------

キーが無い環境で、client が通信しないことを確かめる。
親リポジトリの root で次を実行する ::

    env -u TYPESAFE_API_KEY uv run --project tools/devenv --no-sync python -c '
    from tools.review_triage.client import SystemOneClient, api_key_from_env, connectivity_request
    from tools.review_triage.models import DEFAULT_MODEL_ID
    outcome = SystemOneClient(api_key=api_key_from_env(), model=DEFAULT_MODEL_ID).evaluate(connectivity_request())
    print(outcome.error.kind if outcome.error else "sent", outcome.calls_made)
    '

``no_key 0`` と表示されれば、キーなしでは通信していない

.. _access_live_check:

疎通確認
--------

キーを export したシェルで、固定の合成 request を1回だけ送る。
送る内容は ``connectivity_request()`` が作る英文1文（ ``The sky is blue on a clear day.`` ）と Noul の質問1つだけであり、リポジトリの内容や秘密を含まない ::

    uv run --project tools/devenv --no-sync python -c '
    from tools.review_triage.client import SystemOneClient, api_key_from_env, connectivity_request
    from tools.review_triage.models import DEFAULT_MODEL_ID
    client = SystemOneClient(api_key=api_key_from_env(), model=DEFAULT_MODEL_ID, max_calls=1)
    outcome = client.evaluate(connectivity_request())
    if outcome.response is not None:
        usage = outcome.response.usage
        print("ok", outcome.response.model, usage.input_tokens, usage.output_tokens, outcome.response.request_id)
    elif outcome.error is not None:
        print("error", outcome.error.kind, outcome.error.http_status)
    '

.. _access_results:

結果確認
--------

``ok`` で始まる行が出れば疎通できている。
2列目は実際に応答した model であり、 ``jev-latest`` がどの版に解決されたかを示す。
結果の行はキーと応答本文を含まないため、Issue へ記録してよい。
``error`` で始まる行が出たときは、 :ref:`access_failures` に従って対処する

.. _access_failures:

失敗の見方
==========

``error`` で始まる行は、 ``ErrorKind`` の値と HTTP status を示す。
主な値と人間の対処は次のとおりである

.. list-table:: 失敗の種別と対処
   :header-rows: 1
   :widths: 2 4 4

   * - 種別
     - 意味
     - 対処
   * - ``no_key``
     - キーが無い、またはヘッダに使えない文字を含む。通信していない
     - ``TYPESAFE_API_KEY`` を export したか、値に空白や改行が混ざっていないか確認する
   * - ``auth``
     - 401 または 403。キーが無効か、利用が許可されていない
     - console でキーの状態を確認し、必要なら作り直す
   * - ``rate_limit``
     - 429。送信の上限を超えた
     - 時間を置いて再実行する。client は自動で再送しない
   * - ``service``
     - 529 を含む 5xx。サービス側の障害や過負荷
     - 時間を置いて再実行する
   * - ``timeout`` ・ ``connection`` ・ ``tls``
     - 応答が ``timeout_s`` 以内に無い、接続できない、または TLS を確立できない
     - ネットワーク経路と proxy の有無を確認する（ :ref:`access_limits` ）
   * - ``redirect``
     - 3xx が返った。client は redirect を追わず、キーを別の送信先へ送らない
     - 公式文書で endpoint の変更が無いか確認し、coordinator（Issue #330）へ報告する
   * - ``invalid_request``
     - 422、または request の model が client の model と異なる
     - 送信側の組み立ての誤りとして coordinator へ報告する
   * - ``malformed_response``
     - 応答を共有型として解析できない、または質問と回答が対応しない
     - 公式の応答形式の変更を疑い、coordinator へ報告する
   * - ``unexpected_status``
     - 上記以外の HTTP status
     - status を添えて coordinator へ報告する

どの種別でも、policy（#387）は該当する指摘を既存のレビュー判断（ ``human_review`` ）へ戻す

.. _access_limits:

制約と既定値
============

- 送信先は ``https://api.typesafe.ai/v1/systemone`` に固定し、変更する設定を持たない
- redirect を追わず、自動 retry をしない。
  ``rate_limit`` ・ ``service`` ・ ``timeout`` ・ ``connection`` は ``retryable=True`` を返すが、再送するかは呼び出し側が決める
- 標準ライブラリの ``http.client`` で送るため、 ``HTTPS_PROXY`` などの proxy の環境変数を使わない。
  proxy が必要な環境では疎通しない
- 名前解決は ``timeout_s`` の対象外であり、DNS が応答しないと ``timeout_s`` より長く待つことがある
- 全体の締切（送信の開始から ``timeout_s`` 後）は、応答本文を 64 KiB ずつ読む合間に確認する。
  応答ヘッダの受信中や1回の読み取りの途中は socket の無通信 timeout（ ``timeout_s`` ）だけがかかり、全体の締切は保証しない
- 既定値は ``timeout_s=30.0`` （上限 300 秒）、 ``max_request_bytes=200_000`` 、 ``max_calls=50`` （インスタンスごとの送信回数）、応答本文の上限 1,000,000 バイトである
- 既定 model は ``jev-latest`` であり、実際に応答した model を ``response.model`` に残す

.. _access_references:

関連資料
========

- :ref:`interfaces` （ ``interfaces.rst`` ）
- `TypeSafe Quick start <https://docs.typesafe.ai/introduction/quickstart>`__
- `TypeSafe API reference <https://docs.typesafe.ai/api>`__
- `client Issue #385 <https://github.com/usaturn/maatlog-poc/issues/385>`__
- `evaluation Issue #391 <https://github.com/usaturn/maatlog-poc/issues/391>`__

以上
