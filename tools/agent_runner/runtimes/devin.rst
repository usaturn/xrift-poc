devin ランタイム（Devin CLI）
===============================

``runtime = "devin"`` の adapter（``tools.agent_runner.adapters.devin``）の
観測記録。実機は Devin CLI 3000.11.3 で確認した。

起動
----

``devin -p`` は stdin からプロンプトを読まない（stdin だけ渡すと
「needs a prompt」で失敗する）。そのため ``prepare`` は attempt 領域に
``prompt.txt`` を書き、``--prompt-file`` でパスを渡す。argv にプロンプト
本文は含めない。

.. code-block:: bash

   devin -p --prompt-file <attempt>/prompt.txt \
     --export <attempt>/export.json \
     --model <agent.model> --permission-mode dangerous \
     --respect-workspace-trust false <agent.args...>

``--respect-workspace-trust false`` は、信頼設定の無い worktree で
対話確認を出さずに実行するために必須。stdin としては空を渡す。

出力形式
--------

stdout
  最終回答のテキストのみ（本文）。
``<attempt>/export.json``
  ATIF-v1.7 の transcript。``schema_version``、``session_id``、
  ``agent``（``name`` / ``version`` / ``model_name`` / ``tool_definitions``）、
  ``steps``（``source`` が ``system`` / ``user`` / ``agent`` の
  ``message`` 列と ``tool_calls``）を持つ。

成功・失敗の判定
----------------

- stdout が非空白、かつ export が parse でき、かつ最後の
  ``source == "agent"`` step に非空白の ``message`` がある → stdout を
  result_shape に従って payload にする。
- 上記のどれかが欠ける → ``response_failed``。stderr に認証・承認の
  パターン（not logged in、workspace trust など）があれば
  ``awaiting_authentication`` / ``awaiting_approval`` を優先する。
- export が無い・壊れている場合も同じ経路で失敗に分類する。

export.json の保持と redact
---------------------------

- ``<attempt>/export.json`` を receipt と同じ管理対象の保持物として扱う。
  ``Attempt`` に専用の path 項目は設けず、receipt_path と同じ attempt 領域の
  固定パスから導く。
- 停止確認後は receipt と同様に ``redact_file`` で、SECRET / TOKEN /
  CREDENTIAL / PASSWORD / API_KEY を含む名前の環境値（runner が観測したもの）
  を ``[REDACTED]`` に置き換えてから ``collect`` に読ませる。
- export ファイルが存在しない場合は redact 失敗とみなさず、
  ``response_failed`` など既定の収集判定にそのまま従う。
- symlink・特殊ファイル・置き換え失敗など安全に redact できない場合は
  ``export.json`` を ``sensitive_paths`` に報告し、その export を収集せず、
  成功に分類しない。
- 停止未確認の場合は export を読み書きせず、receipt とともに
  ``sensitive_paths`` に報告する。fallback も開始しない。
- 通常の動作は変えない: 秘密を含まない export は内容がそのまま残り、
  実測 identity の解析も従来どおり行う。

identity の取り出し
-------------------

- ``model``: export の ``agent.model_name``。
- ``session_id``: export の ``session_id``。
- ``cli_version``: export の ``agent.version``。
- ``skill_versions``: export の ``tool_calls`` 内に現れた ``SKILL.md``
  パス（関数名に skill を含む呼び出しの引数も含む）を実ファイルへ解決して
  ``sha256:<hex>`` で記録する。
- export が得られない場合、設定の値から推測せず全項目を null / 空 map にする。

実機完走の観測
--------------

run_workflow 経由（雛形 ``workflows.review`` の inspect を
``--assign inspect=devin-swe`` で単一実行、対象は読み取り専用の指示を
受けた detached worktree）で完走した。

- agent: ``devin-swe``（command ``devin``、設定 model ``swe-2-max``）
- 実測 model: ``SWE-2 Max``、cli_version ``3000.11.3``
- session_id: ``frill-slug``
- 結果: success（exit_code 0、stop_confirmed true、elapsed 約 15.7 秒）
- payload: 依頼した JSON object が返った。認証・承認待ちは発生しなかった。

既知の制限
----------

- stdin はプロンプト輸送に使えないため、attempt 領域の ``prompt.txt`` に
  書き出す。prompt 内容は資格情報を含めないこと。
- ATIF の ``tool_calls`` は export の実構造に依存する。形式が変われば
  skill_versions の観測は空になる。
- CLI の version を stdout に出す経路は無く、cli_version は export 由来。
