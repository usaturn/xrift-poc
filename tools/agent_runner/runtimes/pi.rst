pi ランタイム（pi / piccd / pigs）
===================================

``runtime = "pi"`` の adapter（``tools.agent_runner.adapters.pi``）の観測記録。
実機は pi 1.0.4、launcher として ``pigs`` で確認した。

launcher とランタイム
---------------------

``agent.command`` は pi 系 launcher（``pi`` / ``piccd`` / ``pigs``）の argv。
launcher 自身が既定の provider / model / thinking を持つ場合でも、
adapter が後置する同じオプションは「後の値が勝つ」ため、設定値が
実効値になることを実機で確認済み。

起動
----

``prepare`` は次の argv を組み立て、プロンプトは stdin へ渡す。argv に含めない。

.. code-block:: bash

   <command...> --print --mode json --no-session --no-approve \
     [--provider <agent.provider>] --model <agent.model> \
     [--thinking <agent.effort>] \
     [--skill <source>]... <agent.args...>

``--print --mode json`` が非対話の JSON イベント出力、``--no-session`` と
``--no-approve`` でセッション永続化と承認プロンプトを抑止する。
``agent.effort`` は ``--thinking`` に変換する。``agent.skill_sources`` は
各要素を ``--skill`` で渡す。

出力形式
--------

stdout は NDJSON。``{`` で始まらない行（OSC 777 の通知など、launcher が
混ぜる非 JSON 出力）は無視する。確認したイベント系列:

``session`` → ``agent_start`` → ``turn_start`` → ``message_start`` →
``message_update``* → ``message_end`` → ``turn_end`` → ``agent_end`` →
``agent_settled``

- ``session``: ``id``（session_id）。
- ``agent_end``: ``messages`` に最終 assistant message を含む。
  message は ``role`` / ``model`` / ``provider`` / ``thinkingLevel`` /
  ``stopReason``（または ``rawStopReason``）/ ``content``（``type=text``
  の部分列）を持つ。

成功・失敗の判定
----------------

- ``agent_end`` と ``agent_settled`` の両方が無い → ``response_failed``
  （終了コード 0 でも成立しない）。stderr・stdout 全体の認証・承認
  パターンがあれば ``awaiting_authentication`` / ``awaiting_approval``。
- ``agent_end`` に最終 assistant message が無い → ``response_failed``。
- ``stopReason`` が正常系（stop / endturn / completed 等）→ 最終テキストを
  result_shape に従って payload にする。
- interrupt / abort / cancel 系 → ``interrupted``。
- それ以外 → 本文・stderr の認証・承認パターンを優先し、無ければ
  ``response_failed``。

identity の取り出し
-------------------

- ``model`` / ``provider`` / ``effort``: 最終 assistant message の
  ``model`` / ``provider`` / ``thinkingLevel`` の実測値。
- ``session_id``: ``session`` イベントの ``id``。
- ``cli_version``: pi の JSON イベントには CLI 版の項目が無く null。
- ``skill_versions``: ``--skill`` で渡した読み込み元と、イベント列内の
  tool call に現れた ``SKILL.md`` パスを実ファイルへ解決して
  ``sha256:<hex>`` で記録する。

実機完走の観測
--------------

run_workflow 経由（雛形 ``workflows.review`` の inspect を
``--assign inspect=pi-gpt`` で単一実行、対象は読み取り専用の指示を受けた
detached worktree）で完走した。

- agent: ``pi-gpt``（command ``pigs``、設定 model ``gpt-6.1-sol``、
  provider ``openai-codex``、effort ``xhigh``）
- 実測 model ``gpt-6.1-sol`` / provider ``openai-codex`` /
  thinkingLevel ``xhigh``（launcher 既定値に adapter の指定が勝つことも確認）
- session_id: ``01a11281-15e7-7522-bb65-b7c060e27b74``、cli_version: null
- 結果: success（exit_code 0、stop_confirmed true、elapsed 約 14.5 秒）
- skill_versions: 実行中に ``.agents/skills/repository-ownership/SKILL.md``
  の読み込みが観測され、``sha256:22b4307a…`` が記録された。
- payload: 依頼した JSON object が返った。認証・承認待ちは発生しなかった。

既知の制限
----------

- 実効の thinking level は launcher が内部で持つ既定値と adapter が付ける
  ``--thinking`` の後者。設定に無い既定値は観測した実値として記録する。
- イベント系列の途中で出力が止まった場合は完走イベント欠落として
  ``response_failed`` に分類する。
