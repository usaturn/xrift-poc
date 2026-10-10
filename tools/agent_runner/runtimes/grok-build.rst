grok-build ランタイムの観測記録
=================================

#566 の adapter（``tools.agent_runner.adapters.grok_build``）が依拠する、
実機で観測した Grok Build CLI の非対話挙動を記録する。
観測は 2026-10-07 UTC、Dev Container 上の ``grok 1.0.46 (2765805b9442) [stable]``
（grok.com ログイン済み）で行った。

起動
----

argv は ``grok --prompt-file <path> --output-format json --permission-mode <mode> -m <model> [--reasoning-effort <effort>] [args...]``。
prompt は ``--prompt-file`` が指す prompt.txt に書き出し、argv と stdin の双方に
直接は載せない。

``role.skills`` があるときは prompt.txt に ``Use the following skills: <name>.`` 行を加える。
``agent.skill_sources`` を設定した場合は、設定順で最初に実在する
``<source>/<スキル名>/SKILL.md``（source は work_dir 相対または絶対）を解決し、
その絶対パスで読み込むよう明示する行を加える。この設定パスを native discovery より優先させる。
絶対パスのスキル名、``..`` を含む名前、解決後に source ディレクトリの外へ出る symlink は
設定解決に採用せず、ネイティブ探索への依頼にフォールバックする。
実在しない source はそのまま native discovery に依頼し、バージョンを推測しない。
ファイルの複製・導入は行わない。

- ``--prompt-file`` … 必須。``-p`` はプロンプト文字列を引数に取り、
  stdin の ``-`` はリテラル文字として扱われ読み取られない（実機で確認）。
- ``--output-format json`` … 終了時に単一の JSON object を stdout に出す。
  ``streaming-json`` / ``streaming-messages-json`` もあるが、単一 JSON の方が
  結果の取り出しと照合が単純なため採用した。
- ``--permission-mode`` … ``default / acceptEdits / auto / dontAsk /
  bypassPermissions / plan``。書き込み不可ロールは ``plan``、
  書き込みロールは ``bypassPermissions``。
- ``-m`` … 設定 model。実測の既定は ``grok-4.7``（解決先は ``grok-4.7-build``）。
- ``--reasoning-effort`` … effort 設定時のみ付ける。

出力形式
--------

stdout は 1 つの JSON object。

- ``text`` … 最終応答文字列。
- ``stopReason`` … ``end_turn`` が正常終了。それ以外は response_failed。
- ``sessionId`` … session_id の取り出し元。
- ``modelUsage`` … ``{<解決後の model 名>: {...}}``。キーが 1 つのときだけ
  そのキーを実測 model に使う。複数キーは判別不能のため null。

identity
--------

- ``session_id`` … ``sessionId``。
- ``model`` … ``modelUsage`` の唯一キー。
- ``provider`` / ``effort`` / ``cli_version`` … 出力に出ないため常に null。
  CLI 版は ``grok --version`` 系でしか取れず、adapter は推測しない。
- ``skill_versions`` … 単一 JSON の結果 document には読み込み成功を
  確認できる tool trace が無いため常に空 map。``text`` や、任意の
  ``command`` / ``input`` / ``arguments`` / ``readToolCall`` キーに
  スキルのパスがあっても、読み込みの観測として扱わない。

認証と失敗
----------

- 未ログインは ``not logged in`` / login 系文言 → ``awaiting_authentication``。
- 文言判定は、stderr・結果 document の前後の非 JSON 部分・結果 document が
  取れない場合の stdout 全体・``text`` を持たない失敗形状の document にだけ
  適用する。結果 document の ``text`` の文言では失敗扱いしない。
- ``--permission-mode plan`` でも、選択されたツールが権限確認を必要とすれば
  非対話実行が ``stopReason: cancelled`` で終了し得る。adapter は成功にせず
  ``response_failed`` と分類する。読み取り質問が ``end_turn`` で終了した観測はあるが、
  すべての質問・ツールの完走は保証しない。

実機 run（実行器経由、読み取り専用）
------------------------------------

- 対象: 一時 git リポジトリの ``TARGET.txt`` を読み ``{"marker": 8642}`` を返す inspect 役割。
- 結果: ``success``、payload ``{"marker": 8642}``、所要 8.4 秒、exit 0。
- 実測: model ``grok-4.7-build``（設定 ``grok-4.7`` の解決先）、
  session_id ``01a11260-ad47-7ec1-9e35-bb05af9dcba9``。
- スキル読み込みは観測されず、skill_versions は空 map。

追加の実機確認（指定スキル・書き込み）
--------------------------------------

2026-10-07 UTC、adapter の修正 commit ``78b6201`` を隔離した checkout で実行した。
一時リポジトリの ``.agents/skills/audit-native`` と、ネイティブ探索外の
``agent.skill_sources`` の ``audit-external`` を ``role.skills`` に指定した。

- 最初の読み取り run は18.5秒、exit 0だが ``stopReason: cancelled`` で終了し、
  正しく ``response_failed`` に分類された。権限確認を必要とするツールを選んだ場合、
  headless 実行は確認要求をキャンセルする。この run を成功とは数えない。
- 同じ adapter・設定で、端末コマンドや広範囲検索でなく組み込みの読み取りを使い、
  計画を作らない質問だと明示した1回の追加確認は、結果の取得として成功した。
  所要63.7秒、exit 0。ただし保存された ``thought`` に期待値ログも参照した記述がある。
  この結果のマーカー一致だけでは、指定スキルの読み込み成功を証明できない。
- そのため独立した fixture に新しいランダム値を生成し、実行中に見える場所では
  2つの SKILL.md にだけ値を置いた確認を1回追加した。設定sourceはwork_dir相対の
  ``.agents/skills`` と、ネイティブ探索外の絶対ディレクトリ。期待値のログは停止後に
  保存した。両値が最終 JSON と一致し、所要9.8秒、exit 0、``end_turn`` を観測。
  この確認でも read trace やスキルの版までは観測できない。
- 書き込みロールでは ``RESULT.txt`` の期待した生バイトと ``written: true`` を確認。
  所要8.3秒、exit 0。全 run で終了確認済み、他 checkout の変更なし。
- 単一結果 document に成功 read trace は出ないため、版は引き続き空 map。

制約・既知の限界
----------------

- ``--permission-mode plan`` は計画提示のみで応答しない挙動を取り得る。
  今回の読み取り専用プロンプトでは直接応答を観測したが、タスクによっては
  plan 出力になり、その場合は要求 JSON 形状を満たさず response_failed に分類される。
- ``modelUsage`` が複数キーを返す場合は実測 model を特定できず null。
- ``-p``・stdin 経路は実機で使えないため ``--prompt-file`` 必須。

