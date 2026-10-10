claude-code ランタイム（Claude Code）
======================================

``runtime = "claude-code"`` の adapter（``tools.agent_runner.adapters.claude_code``）の
観測記録。実機は Claude Code 2.1.291 で確認した。

起動
----

``prepare`` は次の argv を組み立て、プロンプトは stdin へ渡す。argv に含めない。

.. code-block:: bash

   claude -p --output-format stream-json --verbose \
     --model <agent.model> [--effort <agent.effort>] \
     --dangerously-skip-permissions <agent.args...>

``-p`` は stdin からプロンプトを読み、応答し終わると終了する非対話モード。
``--dangerously-skip-permissions`` を常に付け、承認プロンプトを出さない。
``agent.effort`` は設定時だけ ``--effort`` に変換する。

出力形式
--------

``--output-format stream-json --verbose`` で stdout は NDJSON になる。
主なイベント:

``system`` / ``subtype=init``
  ``claude_code_version``、``model``、``session_id``、tools、slash_commands を持つ。
``assistant``
  ``message.model`` と content block（``type=text`` の本文、``type=tool_use`` の
  ツール呼び出し）を持つ。
``result``
  ``subtype``（success / error_*）、``is_error``、``result``（最終テキスト）、
  ``num_turns``、``permission_denials`` を持つ。

各行は独立した JSON object。JSON として解釈できない行は無視する。

成功・失敗の判定
----------------

- ``result`` イベントが無い → ``response_failed``（stderr・assistant 本文の
  認証・承認パターンがあれば ``awaiting_authentication`` / ``awaiting_approval``）。
- ``result.permission_denials`` が非空 → ``awaiting_approval``。
- ``subtype == "success"`` かつ ``is_error`` が偽 → 最終テキストを
  result_shape に従って payload にする（json は全文または単一の
  `` ```json `` フェンスを object として parse、text は非空白文字列）。
  解釈できなければ ``response_failed``。
- ``subtype`` が interrupt / abort / cancel 系 → ``interrupted``。
- それ以外の ``is_error`` / 非 success subtype → 本文・stderr の
  認証・承認パターンを優先し、無ければ ``response_failed``。

identity の取り出し
-------------------

- ``model``: init イベントの ``model`` を基に、後続の assistant イベントの
  ``message.model`` で上書きした実測値。
- ``session_id``: init イベントの ``session_id``。
- ``cli_version``: init イベントの ``claude_code_version``。
- ``skill_versions``: assistant イベントの tool_use で ``name == "Skill"`` の
  ``input.skill`` に現れた名前だけを、``SKILL.md`` の実ファイルへ解決して
  ``sha256:<hex>`` で記録する。init の slash_commands 一覧は「利用可能」で
  あり読み込みの証拠にしない。読み込みが無ければ空 map。

実機完走の観測
--------------

run_workflow 経由（雛形 ``workflows.review`` の inspect を
``--assign inspect=claude-opus`` で単一実行、対象は読み取り専用の指示を
受けた detached worktree）で完走した。

- agent: ``claude-opus``（command ``claude``、設定 model ``opus``）
- 実測 model: ``claude-opus-5-5``、cli_version ``2.1.291``
- session_id: ``f8fe5007-68c0-433a-a8f4-fa6f73bb3d37``
- 結果: success（exit_code 0、stop_confirmed true、elapsed 約 8.8 秒）
- payload: 依頼した JSON object（repository / top_level_entries /
  readme_first_line）が返った。認証・承認待ちは発生しなかった。

既知の制限
----------

- ``provider`` は設定項目として保持するが、この CLI には渡さない。
- tool_use が走らない実行では skill_versions は空になる。
- 途中経過の assistant テキストは payload に使わず、最終 ``result`` の
  テキストだけを採用する。
