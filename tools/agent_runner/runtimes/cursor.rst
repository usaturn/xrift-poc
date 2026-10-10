cursor ランタイムの観測記録
=============================

#566 の adapter（``tools.agent_runner.adapters.cursor``）が依拠する、
実機で観測した Cursor Agent CLI の非対話挙動を記録する。
観測は 2026-10-07 UTC、Dev Container 上の ``2026.10.01-e373342``
（ログイン済み）で行った。実行ファイルは ``agent`` / ``cursor-agent``
（同一バイナリへの symlink）。``cursor`` というコマンドは存在しない。

起動
----

argv は ``agent -p --output-format stream-json --trust (--mode ask | -f) --model <slug> [args...]``。
prompt は stdin から渡し、argv には含めない。

- ``-p`` … 非対話 print モード。
- ``--output-format stream-json`` … 1 行 1 イベントの JSONL。
  単一 JSON の ``json`` でもよいが、init イベントで実測 model と
  session_id が早く確定し、tool イベントも観測できる stream-json を採用した。
- ``--trust`` … 新規 worktree で必須。無いと ``Workspace Trust Required``
  で停止する。adapter は常に付ける。
- ``--mode ask`` … 書き込み不可ロール用（計画・質問のみ）。書き込みロールは ``-f``。
- ``--model`` … slug 指定。``--list-models`` で確認した実在値のみ使う
  （``auto``、``grok-4.7-xhigh`` など）。``model[effort=high]`` のブラケット指定も受理される。

``role.skills`` があるときは prompt に ``Use the following skills: <name>.`` 行を加える。
``agent.skill_sources`` を設定した場合は、設定順で最初に実在する
``<source>/<スキル名>/SKILL.md``（source は work_dir 相対または絶対）を解決し、
その絶対パスで読み込むよう明示する行を加える。この設定パスを native discovery より優先させる。
絶対パスのスキル名、``..`` を含む名前、解決後に source ディレクトリの外へ出る symlink は
設定解決に採用せず、ネイティブ探索への依頼にフォールバックする。
実在しない source はそのまま native discovery に依頼し、バージョンを推測しない。
ファイルの複製・導入は行わない。

出力形式
--------

- ``{"type": "system", "subtype": "init", "session_id": "...", "model": "..."}`` …
  session_id と実測 model の取り出し元。model は ``Auto`` や
  ``Grok 4.7 256K Extra High`` のような表示名。
- ``{"type": "result", "subtype": "success", "is_error": false, "result": "..."}`` …
  最終応答。``is_error: true`` または result 不在は response_failed。

identity
--------

- ``session_id`` / ``model`` … ``system.init`` イベントの値。
- ``provider`` / ``effort`` / ``cli_version`` … イベントに出ないため常に null。
  CLI 版は ``agent --version`` でしか取れず、adapter は推測しない。
- ``skill_versions`` … ``tool_call`` の ``subtype: completed`` が返した
  ``readToolCall.result.success`` を読み込み成功の観測として使う。記録には
  ``content``・``exceededLimit: false``・``isEmpty`` 無しという完了結果、
  ``startLine: 1`` かつ ``endLine`` が ``totalLines`` に一致する ``readRange``
  （部分範囲や上限超過は不明のまま）、結果が返した ``path`` 全体の
  ``**/skills/**/SKILL.md`` 一致、そして ``content`` が実在ファイルの生バイトを
  UTF-8 として厳密にデコードした内容との完全一致を要する。満たす場合だけ
  SHA-256 を記録する。開始イベント、``args.path`` だけの呼び出し、失敗結果、
  削除・内容変更・内容欠落、本文や prompt echo の構造化フィールド・shell
  コマンドへの言及では版を不明のままにする。
  対応する読み込み成功が観測できなければ空 map。

認証と失敗
----------

- 未ログインは ``not logged in`` / login 系文言 → ``awaiting_authentication``。
- ``Workspace Trust Required`` / trust 要求 → ``awaiting_approval``。
- 文言判定は、stderr・JSON を解釈できない stdout 行・``error`` イベント・
  ``is_error`` の ``result`` イベントにだけ適用する。イベントとして解釈できた
  応答本文や prompt の echo の文言では失敗扱いしない。
- モデル側の拒否（``is_error: false`` だが拒否文を返す）は成功扱いせず、
  result が要求 JSON 形状を満たさなければ response_failed。

実機 run（実行器経由、読み取り専用）
------------------------------------

対象: 一時 git リポジトリの ``TARGET.txt`` を読み ``{"marker": 8642}`` を返す inspect 役割。

- ``cursor``（model ``auto``）: ``success``、payload ``{"marker": 8642}``、
  所要 15.5 秒、実測 model ``Auto``、session_id ``3f46e063-93fd-4caf-b09a-26f8722f7fa0``。
- ``cursor-grok47``（model ``grok-4.7-xhigh``）: ``success``、payload ``{"marker": 8642}``、
  所要 17.5 秒、実測 model ``Grok 4.7 256K Extra High``、
  session_id ``33373a4b-95a0-40c6-8293-18ee64d191b6``。
- 両 run でスキル読み込みは観測されず、skill_versions は空 map。

追加の実機確認（指定スキル・書き込み）
--------------------------------------

2026-10-07 UTC、adapter の修正 commit ``78b6201`` を隔離した checkout で実行した。
一時リポジトリの ``.agents/skills/audit-native`` と、ネイティブ探索外の
``agent.skill_sources`` の ``audit-external`` を ``role.skills`` に指定した。

- 両 SKILL.md の完全な成功 read イベントと、本文にだけ記した2つのマーカーを含む
  最終 JSON を観測。``skill_versions`` は両ファイルの SHA-256 と一致した。
  所要21.4秒、exit 0、実測 model ``Auto``。
- 書き込みロールでは ``RESULT.txt`` の期待した生バイトと ``written: true`` を確認。
  CLI がネイティブスキルも自動適用したため、最終 JSON にはマーカーも含まれた。
  所要28.1秒、exit 0。全 run で終了確認済み、他 checkout の変更なし。

制約・既知の限界
----------------

- ``--trust`` を常に付けるため、trust 判断を人に委ねる運用はできない。
  新規ディレクトリで信頼済み扱いになる点を理解した上で使う。
- init の model は表示名であり API 名ではない。provider/effort/cli_version は
  この出力形式では観測できない。
- 応答が散文＋JSON の場合、adapter は最初に解釈できた JSON object を payload にする。

