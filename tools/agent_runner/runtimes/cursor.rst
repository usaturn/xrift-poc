cursor ランタイムの観測記録
=============================

#566 の adapter（``tools.agent_runner.adapters.cursor``）が依拠する、
実機で観測した Cursor Agent CLI の非対話挙動を記録する。
観測は 2026-10-07 UTC、Dev Container 上の ``2026.10.01-e373342``
（ログイン済み）で行った。実行ファイルは ``agent`` / ``cursor-agent``
（同一バイナリへの symlink）。``cursor`` というコマンドは存在しない。
xrift-poc の #75 で ``writes = false`` の役割の起動を変え（移植元 usaturn/agent-workspace の #752・PR #753 と同じ変更）、
2026-10-20 UTC に ``2026.10.19-6bbf2c4`` で、Ask mode のシェル、sandbox の範囲、新しい起動の動作を確かめた
（「Ask mode のシェル（#75）」「sandbox の観測」「実機 run（#75）」）。

起動
----

argv は ``agent -p --output-format stream-json --trust -f [--sandbox disabled] --model <slug> [args...]``。
``--sandbox disabled`` は ``writes = false`` の役割だけに付け、``writes = true`` の役割は #75 の前と同じく
``-f`` だけで起動する。
prompt は stdin から渡し、argv には含めない。

- ``-p`` … 非対話 print モード。
- ``--output-format stream-json`` … 1 行 1 イベントの JSONL。
  単一 JSON の ``json`` でもよいが、init イベントで実測 model と
  session_id が早く確定し、tool イベントも観測できる stream-json を採用した。
- ``--trust`` … 新規 worktree で必須。無いと ``Workspace Trust Required``
  で停止する。adapter は常に付ける。
- ``-f`` … 利用者の allowlist（``cli-config.json`` の ``permissions.allow``）に無いシェルのコマンドも、
  確認なしで実行する。CLI のヘルプでは、明示的に拒否したもの（``permissions.deny``）は引き続き拒否される。
- ``--sandbox disabled`` … ``writes = false`` の役割で Cursor の sandbox を使わない。CLI のヘルプでは、
  利用者の設定（``sandbox.mode``）を上書きする。利用者が対話用に sandbox を有効にしていても同じ起動になり、
  利用者の設定は書き換えない（「sandbox の観測」）。
- ``--model`` … slug 指定。``--list-models`` で確認した実在値のみ使う
  （``auto``、``grok-4.7-xhigh`` など）。``model[effort=high]`` のブラケット指定も受理される。

``--mode ask``・``--mode plan`` は使わない。CLI のヘルプでは、どちらも読み取り専用のモードである。
#75 までは ``writes = false`` の役割を ``--mode ask`` で起動していたので、PR レビュー（``inspect``）の担当は
pr-review の helper（``review_run.py prepare``・``complete``）を実行できず、PR 修正ループで毎回 rejected になった
（「Ask mode のシェル（#75）」）。
``writes`` は runner の指示であって sandbox ではない（``runner.py`` が依頼の冒頭にそう書く）。
claude-code・devin・pi も、``writes`` によらずシェルを使える同じ起動にしている。

``role.skills`` があるときは prompt に ``Use the following skills: <name>.`` 行を加える。
``agent.skill_sources`` を設定した場合は、設定順で最初に実在する
``<source>/<スキル名>/SKILL.md``（source は work_dir 相対または絶対）を解決し、
その絶対パスで読み込むよう明示する行を加える。この設定パスを native discovery より優先させる。
絶対パスのスキル名、``..`` を含む名前、解決後に source ディレクトリの外へ出る symlink は
設定解決に採用せず、ネイティブ探索への依頼にフォールバックする。
実在しない source はそのまま native discovery に依頼し、バージョンを推測しない。
ファイルの複製・導入は行わない。

Ask mode のシェル（#75）
------------------------

2026-10-20 UTC、``2026.10.19-6bbf2c4`` で、#75 の前の adapter（``--mode ask``。親の main checkout の ``6cfbc40``）を
runner 経由で起動し、``writes = false`` の ``inspect`` に次のシェルのコマンドを順に依頼した。
対象は同じ ``6cfbc40`` の xrift-poc で、作業場所は runner の worktree の root。

1. ``date -u +%Y``
2. ``uv run --project tools/devenv --locked python .agents/skills/pr-review/scripts/review_run.py --help``
3. ``mktemp -d`` で作ったディレクトリへの書き込み・読み取り・削除

.. list-table::
   :header-rows: 1

   * - 担当（実測 model）
     - 結果
   * - ``cursor``（``Auto``）
     - ``date`` だけを実行した。``shellToolCall`` の引数に ``requestedSandboxPolicy``（``TYPE_WORKSPACE_READWRITE``）が付き、
       exit 0。helper と ``mktemp`` は、モデルが「Ask mode は読み取り専用のコマンドだけ」として呼び出さなかった
   * - ``cursor-grok47``（``Grok 4.7 256K Extra High``）
     - 3 つとも呼び出したが、``shellToolCall`` の結果は ``rejected``（``reason`` は空、``isReadonly: false``）。
       引数に ``requestedSandboxPolicy`` は無かった

Ask mode では、モデルが sandbox を求めなかったシェルのコマンドはランタイムが拒否し、求めたコマンドは Cursor の sandbox の中で実行される。
sandbox の中でも、helper の保存先（親の ``reviews/``）・uv のキャッシュ（HOME の下）への書き込みとネットワークは拒否される
（「sandbox の観測」）ので、どちらの形でも PR レビューは完了しない。
xrift-poc の PR #40 の PR 修正ループ（2026-10-17）で、cursor-grok47 が「シェルの実行が拒否され、pr-review の prepare を
起動できなかった」として rejected になったのも、拒否された形と同じ症状である
（PR #40 の runner の生ログは回収で消えており、tool のイベントは確かめていない）。

sandbox の観測
--------------

2026-10-20 UTC、``2026.10.19-6bbf2c4`` の ``agent sandbox run``（``workspace_readwrite`` の方針）で、モデルを呼ばずに確かめた。
cwd は、親の ``tmp/agents/`` の下に作った一時リポジトリの linked worktree、HOME は同じ場所の一時ディレクトリに隔離した。
出力された方針は、``networkPolicy.default`` が ``deny``、``readBoundary`` が ``system``、追加の読み書き・読み取りのパスは無し。
``agent sandbox run`` は引数を 1 つの文字列につないでシェルで実行するので（``sh -c '...'`` の ``-c`` は落ちる）、
コマンドは 1 つの文字列で渡した。

.. list-table::
   :header-rows: 1

   * - 操作
     - 結果
   * - cwd（worktree）の中への書き込み
     - 許可
   * - ``/tmp`` の下への書き込み（``mktemp -d``）
     - 許可
   * - worktree の外（``/tmp`` 以外）への書き込み
     - 拒否
   * - HOME の下（``/tmp`` 以外）への書き込み
     - 拒否
   * - リポジトリの共有 ``.git`` への書き込み（``git branch``、worktree の中に作る ``git worktree add`` の登録）
     - 許可
   * - worktree の外に作る ``git worktree add``
     - ディレクトリの作成で拒否（branch は作られた）
   * - worktree の外の読み取り、``uv --version``
     - 許可
   * - ネットワーク（``curl https://api.github.com/``）
     - 拒否（名前を解決できない）

確かめる場所を ``/tmp`` の下に置くと、``/tmp`` が書き込み可なので、どの書き込みも許可される。
移植元の #752 の記録（``2026.10.16-e3cb207``・``2026.10.18-ea085cb``）は、共有 ``.git`` への書き込み（``git worktree add``）を
拒否としている。この版では、共有 ``.git`` だけへの書き込みは許された。

同じ日に、runner を使わず ``agent -p`` を直接起動し（model ``auto``、cwd は同じ linked worktree）、
シェルで worktree の外（``/tmp`` 以外）のファイルを ``touch`` させた。

.. list-table::
   :header-rows: 1

   * - 起動
     - 結果
   * - ``--sandbox enabled``（``-f`` なし）
     - シェルは確認なしに sandbox の中で実行され（呼び出しに ``requestedSandboxPolicy`` が付いた）、``Permission denied`` で失敗した
   * - ``-f --sandbox enabled``
     - 呼び出しに ``requestedSandboxPolicy`` が付いたが、成功した。``-f`` のときは sandbox が掛からない
   * - ``-f --sandbox disabled``
     - 成功した

``CURSOR_CONFIG_DIR`` に ``sandbox.mode = "enabled"`` の設定を置いて ``-f --sandbox disabled`` で起動しても、
設定の ``sandbox.mode`` は ``enabled`` のままだった。``--sandbox`` は利用者の設定に残らない
（CLI は実行のたびに ``cli-config.json`` のキャッシュの項目を書き直すが、``sandbox``・``approvalMode``・``permissions`` は変えなかった）。

PR レビューの担当は、親の ``reviews/`` の保存先と uv のキャッシュ（HOME の下）に書き、DSH 以外は PR の取得にネットワークを使う
（``review_run.helper_write_exceptions``）。sandbox の中ではこれらが拒否されるので、adapter は ``writes = false`` の役割を
``--sandbox disabled`` で起動する。``-f`` が sandbox を外すことは文書に無く、版によって変わりうるので、この挙動に頼らずに明示する。
CLI は自動更新される（版ごとに ``~/.local/share/cursor-agent/versions/`` に置かれ、``agent`` の symlink が切り替わる。
移植元の #752 では確認の途中で切り替わった）。runner の担当がどの版で動くかは起動の時点で決まる。
書き込み先を足すには sandbox の方針のファイル（Cursor の文書では ``~/.cursor/sandbox.json``・``<workspace>/.cursor/sandbox.json``）が要り、
利用者の設定か、レビューする worktree を書き換えることになるので使わない。

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

実機 run（#75）
----------------

2026-10-20 UTC、``2026.10.19-6bbf2c4`` で、#75 の adapter（``-f --sandbox disabled``。feature worktree の ``7478ec4``）を
runner 経由で起動した。役割はすべて ``writes = false`` の ``inspect``。
利用者の ``cli-config.json`` は ``sandbox.mode = "disabled"``、allowlist は ``Shell(ls)`` だけで、
実行の前後で ``sandbox``・``approvalMode``・``permissions`` の値は変わらなかった。

- 読み取り専用の一般の依頼（「実機 run（実行器経由、読み取り専用）」と同じ課題）: ``cursor`` は ``success``、
  payload ``{"marker": 8642}``、所要 15 秒、実測 model ``Auto``。``cursor-grok47`` は ``success``、同じ payload、
  所要 18 秒、実測 model ``Grok 4.7 256K Extra High``。どちらもシェルを使わず、``outside_changes`` は空。
- シェル: 「Ask mode のシェル（#75）」と同じ 3 つのコマンドを依頼した。両方 ``success`` で、3 つとも ``shellToolCall`` が
  exit 0 を返した（``requestedSandboxPolicy`` は無し）。helper の ``--help`` の stdout は ``usage: review_run.py`` で始まり、
  uv は runner の worktree に ``tools/devenv/.venv`` を作った。所要は ``cursor`` 36 秒・``cursor-grok47`` 38 秒。
  ``outside_changes`` は空で、runner の worktree は ``git worktree remove`` で回収できた（変更は無視したファイルだけ）。

制約・既知の限界
----------------

- ``--trust`` を常に付けるため、trust 判断を人に委ねる運用はできない。
  新規ディレクトリで信頼済み扱いになる点を理解した上で使う。
- init の model は表示名であり API 名ではない。provider/effort/cli_version は
  この出力形式では観測できない。
- 応答が散文＋JSON の場合、adapter は最初に解釈できた JSON object を payload にする。
- ``writes = false`` は runner の指示であり、Cursor の sandbox でも OS でも保証しない。
  ``-f`` のため、担当はファイルを書き換えられる。ほかの checkout の変更は runner の ``outside_changes`` が報告し、
  runner の worktree の変更は回収のときに ``git worktree remove`` が拒否して残る（force で消さない）。
- ``-f`` でも、利用者が ``permissions.deny`` に書いたコマンドは拒否される（CLI のヘルプ）。
  helper が使うコマンド（``uv``・``git`` など）を拒否すると、PR レビューの担当は完了できない。
- ``self-review``・``fix-check`` は ``inspect`` と同じ起動だが、実機では ``inspect`` だけを確かめた。
- ``writes = true`` の役割は #75 の前と同じく ``--sandbox`` を付けないので、sandbox の扱いは利用者の設定
  （``sandbox.mode``）と ``-f`` の挙動に従う。

