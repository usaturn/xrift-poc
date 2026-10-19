codex ランタイムの観測記録
============================

#566 の adapter（``tools.agent_runner.adapters.codex``）が依拠する、
実機で観測した ``codex`` CLI の非対話挙動を記録する。
観測は 2026-10-07 UTC、Dev Container 上の ``codex-cli 0.160.1``
（ChatGPT ログイン済み）で行った。

起動
----

argv は ``codex exec --json -s <sandbox> -m <model> [-c model_reasoning_effort=<effort>] [args...] -``。
prompt は末尾の ``-`` 引数と組み合わせて stdin から渡し、argv には一切含めない。

``role.skills`` があるときは prompt に ``Use the following skills: $<name>.`` 行を加える。
``agent.skill_sources`` を設定した場合は、設定順で最初に実在する
``<source>/<スキル名>/SKILL.md``（source は work_dir 相対または絶対）を解決し、
その絶対パスで読み込むよう明示する行を加える。この設定パスを native discovery より優先させる。
絶対パスのスキル名、``..`` を含む名前、解決後に source ディレクトリの外へ出る symlink は
設定解決に採用せず、ネイティブ探索への依頼にフォールバックする。
実在しない source はそのまま native discovery に依頼し、バージョンを推測しない。
ファイルの複製・導入は行わない。

- ``exec`` … 非対話実行サブコマンド。TOML では ``command = ["codex", "exec"]`` とする。
- ``--json`` … JSONL イベントを stdout に出す。
- ``-s read-only`` … 書き込み不可ロール用。書き込みロールは ``workspace-write``。
  OS の sandbox がすべての書き込みを拒否するので、adapter は ``Capabilities(enforces_read_only=True)`` を宣言する。
  書き込み不可でも helper の書き込みが要る役割（PR レビューの inspect、self-review、fix-check）には割り当てられない
  （下の「書き込み不可ロールで helper を動かさない理由（#52）」）。
- ``-m`` … 設定 model をそのまま渡す。
- ``-c model_reasoning_effort=<値>`` … effort 設定時のみ付ける。
- 対話確認なしで完走する。``--dangerously-bypass-approvals-and-sandbox`` は使わない。

出力形式
--------

stdout は 1 行 1 イベントの JSONL。

- ``{"type": "thread.started", "thread_id": "..."}`` … session_id の取り出し元。
- ``{"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}`` …
  最後の agent_message の ``text`` が最終応答。
- ``{"type": "error"}`` / ``turn.failed`` … response_failed と分類する。

identity
--------

- ``session_id`` … ``thread.started.thread_id``。
- ``model`` / ``provider`` / ``effort`` / ``cli_version`` … JSONL イベントには
  入らない。``$CODEX_HOME`` または ``$HOME/.codex`` 配下の
  ``sessions/**/rollout-*<session_id>.jsonl`` を探し、レコードの
  ``payload.cli_version`` / ``model_provider`` / ``model`` / ``effort`` の
  実測値を使う。ファイルが無ければ null のまま。
- ``skill_versions`` … ``item.completed`` の ``command_execution`` が
  ``status: completed``・``exit_code: 0`` で終了した読み込みだけを記録する。
  対象は単一の絶対パスを読む ``cat <path>`` と、実機で観測した
  ``/bin/bash -lc 'cat <path>'``。``aggregated_output`` が実在するファイルの
  生バイトを UTF-8 として厳密にデコードした内容と完全一致する場合だけ
  SHA-256 を記録する。``aggregated_output`` の欠落、部分観測、読み込み後の
  内容変更・削除、非 UTF-8 のバイトは版を不明のままにする。開始イベント、
  失敗、本文や ``echo`` による言及、rollout のツール呼び出し引数・
  ``parsed_cmd`` だけでは読み込み成功と判断しない。

認証と失敗
----------

- 未ログイン時は ``not logged in`` / ``login`` 系の文言が stderr に出て
  非零終了 → ``awaiting_authentication``。
- 認証・承認系の文言判定は、stderr・JSON を解釈できない stdout 行・
  ``error``/``turn.failed`` イベントにだけ適用する。イベントとして解釈できた
  応答本文やツール出力の文言では失敗扱いしない。
- 承認待ちは ``-s read-only`` の限り発生しない。approval フラグは使わない。

実機 run（実行器経由、読み取り専用）
------------------------------------

- 対象: 一時 git リポジトリの ``TARGET.txt`` を読み ``{"marker": 8642}`` を返す inspect 役割。
- 結果: ``success``、payload ``{"marker": 8642}``、所要 14.4 秒、exit 0。
- 実測: model ``gpt-6.1-sol``、provider ``openai``、effort ``xhigh``、
  cli_version ``0.160.1``、session_id ``01a1125f-e41a-7b21-8437-4d559a86ab0e``。
- 読まれたスキル: ``using-superpowers`` = ``82c5c8866ad7f5dd4440ce66bd7806ba48a2f13771beae5cf112e53f08fe36ba``
  （出所: stdout/rollout が参照した
  ``~/.codex/plugins/cache/openai-curated-remote/superpowers/6.4.2/skills/using-superpowers/SKILL.md``。
  リポジトリの ``.agents/skills`` ではなくプラグインキャッシュ側だった）。

追加の実機確認（指定スキル・書き込み）
--------------------------------------

2026-10-07 UTC、adapter の修正 commit ``78b6201`` を隔離した checkout で実行した。
一時リポジトリの ``.agents/skills/audit-native`` と、ネイティブ探索外の
``agent.skill_sources`` の ``audit-external`` を ``role.skills`` に指定した。

- ``$audit-native`` / ``$audit-external`` を含む prompt で、両 SKILL.md の読み込み成功と
  本文にだけ記した2つのマーカーを含む最終 JSON を観測した。所要22.7秒、exit 0。
- CLI は複数ファイルをまとめた ``cat`` を使ったため、対応する単一ファイル読み込みの
  条件には該当せず、この run の ``skill_versions`` は空 map。読み込み自体の成功と
  adapter が版を記録できるかは区別する。
- 書き込みロールでは ``RESULT.txt`` の期待した生バイトと ``written: true`` を確認。
  所要31.0秒、exit 0。全 run で終了確認済み、他 checkout の変更なし。

書き込み不可ロールで helper を動かさない理由（#52）
---------------------------------------------------

PR #50 の loop（2026-10-18、codex-cli 0.163.0、``gpt-6.1-sol``）で、inspect の codex は 2 回とも rejected になった。
pr-review の helper（``uv run ... review_run.py``）が uv のキャッシュ ``~/.cache/uv`` に一時ファイルを作れず
（read-only file system、os error 30）、prepare の前に終わった。

inspect・self-review・fix-check は、writes=false でも helper がレビューディレクトリ、親の Git の共有ディレクトリ
（保存ロック、review worktree の登録・ref・オブジェクト）、``tools/devenv/.venv`` と uv のキャッシュに書き、
DSH 以外の pr-review は ``gh api`` と ``git fetch`` でネットワークも使う。``-s workspace-write`` と ``--add-dir`` で
書き込み先を足しても、codex は書き込みを許したディレクトリの中の ``.git``（ディレクトリかポインタファイル）・
``gitdir:`` の解決先・``.codex`` を読み取り専用に保ち、``.git/config`` などの metadata も明示の許可が無ければ書かせない。
ネットワークも既定で制限される（Context7 ``/openai/codex``、2026-10-19 に確認）。helper を動かすにはこれらを明示的に
許す必要があり、``.git/hooks``・``.git/config``・uv のキャッシュに書ければ sandbox の外で後から実行されるコードを置けるので、
sandbox の意味が無くなる。このため書き込みを許可せず、割り当てを止める。

- loop の ``check-options``・``start`` は、codex を review の inspect と fix の self-review・fix-check（一次担当か fallback）に
  割り当てた設定を、run を作る前に終了コード 2 で止める。``run_roles.py`` は 1 回枠の予約の前に止める
- runner は、``write_exceptions`` を渡された run で writes=false の役割に codex がいれば、何も作らずに invalid_input で止める
  （``review-verify/scripts/dispatch.py`` の fix-check、CLI の ``--write-exception``）。例外の無い読み取りの run は従来どおり
- 書き込みロール（implement）の codex は変えない
- codex を DSH と同じ経路（親が PR の情報を用意し、専用 clone、``staged`` を親が確定）に乗せれば安全に渡せるが、
  loop・``review_run.py``・runner の DSH 固有の分岐を広げる別の変更になる

制約・既知の限界
----------------

- rollout ファイルが見つからない環境（別 HOME、後片付け済み等）では
  model/provider/effort/cli_version は null になる。推測で埋めない。
- ``skill_versions`` は対応する単純な ``cat`` の成功が観測でき、観測内容が
  現在のバイトと一致する SKILL.md のみ。複合コマンド、オプション、シェル展開や
  成功不明の呼び出しは解析せず、版を記録しない。読み込み成功と内容一致の両方を
  観測できなければ空 map。
- ``-c model_reasoning_effort=...`` の受理は rollout の ``effort`` でしか確認できない。

