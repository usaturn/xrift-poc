汎用の非対話エージェント実行器 v1
==================================

この文書は #560 が定義する共通インターフェースを示す。
六ランタイムの具体的な起動処理は #565 / #566 が実装する。
PR レビューの保存・ロック・GitHub 操作、修正計画や loop は本実行器の責務ではない。

設定
----

TOML の ``schema_version = 1`` と以下の三層を使う。
未知のキー、版、不正な型・参照を拒否する。資格情報の値は書かない。

``agents.<agent_id>``
  ``runtime``: claude-code / devin / pi / codex / cursor / grok-build。
  ``command``: 空でない argv 配列。シェル文字列・alias ではなく実行可能なコマンド。
  ``model``: 空でないモデル値。
  ``provider`` / ``effort``: 任意の文字列。
  ``approval``: noninteractive のみ（既定）。
  ``skill_sources``: スキルの読み込み元の配列（既定は空）。
  ``args``: 追加 argv の配列（既定は空）。
  ``env``: 引き継ぐ環境変数の名前の配列（値ではない、既定は空）。

``roles.<role>``
  ``skills``: 使用するスキルの名前・パスの配列（無しも可）。
  ``writes``: worktree に書き込む仕事かを示す bool。
  ``deadline_seconds``: 有限の正数。
  ``result_shape``: json（空でない object）または text（空白だけでない string）。

``workflows.<workflow>.assignments.<role>``
  ``agents``: 並行担当の agent_id の配列、または ``["self"]`` のみ。
  ``fallback``: 明示した順序の代替候補の配列（既定は空、self は不可）。
  未対応・失敗を理由に別モデルや対話方式へ暗黙に切り替えない。
  fallback は役割内の共有候補で、一つの候補を複数の並行担当へ割り当てない。
  使用済み候補しか残らなければ、元の失敗と fallback_exhausted を返す。

``workflows.<workflow>.exclude_same_model_as``
  任意の role → 他の role の table。
  両役割で同じモデルを使わない制約を明示する。
  指定しなければ役割間で同じモデルを使ってよい。

``workflows.<workflow>.after_run``
  CLI が result.json を保存した後に順に起動する argv の配列（既定は空）。
  各要素は空でない argv の配列で、先頭は空白だけでない文字列。詳細は「後処理」の節。

同じ役割の agents と fallback の全候補で ``model`` 値が重複すると拒否する。
command や provider が違っても同じモデル値なら重複とする。
比較はモデル文字列の完全一致。別名が同じモデルを指すかまでは解決しない。
``self`` は予約名で、agent として定義できない。

上書きの優先順位は、雛形 < 個人設定 < CLI/API の割り当て。
table は再帰的に統合、配列は置換する。``--assign role=a,b`` は agents を置換し、
その役割の明示済み fallback は保持する。最終形の参照・重複を検証する。

個人設定の場所は、``AGENT_RUNNER_CONFIG``、絶対パスの
``XDG_CONFIG_HOME/agent-runner/config.toml``、
``~/.config/agent-runner/config.toml`` の順。
CLI は既存の個人設定を雛形に重ねる。明示された ``--config`` が読めなければエラー。
雛形をコピーした後も、既存個人設定を導入処理で更新しない。

``after_run`` も配列なので、個人設定の ``after_run = []`` で雛形の後処理を止められる。

Python の ``load_config(template, personal=None, assignments=None, workflow=None)``
は、personal=None のとき個人設定を自動では読まない。
``validate_config(config, workflow=None)`` は直接 API に渡す設定も同じルールで検証する。
``ConfigError.category`` は schema_version / unknown_key / invalid_type /
invalid_value / invalid_reference / duplicate_model / config_io / invalid_toml /
nested_invocation / invalid_config。原文・値・Git 診断は返さない。

ランタイム別モジュール
----------------------

以下の固定モジュールが ``ADAPTER`` を公開する。共有の登録表は編集しない。
基盤 PR には具体的な六モジュールを含めず、未導入は unsupported と報告する。

.. list-table::
   :header-rows: 1

   * - ランタイム
     - モジュール
   * - claude-code
     - tools.agent_runner.adapters.claude_code
   * - devin
     - tools.agent_runner.adapters.devin
   * - pi
     - tools.agent_runner.adapters.pi
   * - codex
     - tools.agent_runner.adapters.codex
   * - cursor
     - tools.agent_runner.adapters.cursor
   * - grok-build
     - tools.agent_runner.adapters.grok_build

型は ``tools.agent_runner.adapters`` で定義する。

.. code-block:: python

   class Adapter(Protocol):
       capabilities: Capabilities

       def prepare(self, context: AdapterContext) -> LaunchSpec: ...
       def collect(self, context: AdapterContext,
                   stdout_path: Path, stderr_path: Path) -> AdapterOutcome: ...

``Capabilities(noninteractive=True, stdin=True, enforces_read_only=False)``
  noninteractive と stdin の両方が literal True の場合だけ起動する。
  機能宣言はランタイム担当が実機で確かめる。対話への代替は不可。
  ``enforces_read_only`` は起動の条件ではない。writes=false の役割を、ランタイム自身が書き込みを拒否する状態で
  起動する adapter が True を宣言する（codex の ``-s read-only``。#52）。その担当は、呼び出し側が許可した書き込み
  （``write_exceptions``）を実行できない。``read_only_assignments(config, workflow, adapter_loader=load_adapter)``
  は、workflow の writes=false の役割に割り当てた担当（一次担当と fallback、self を除く）のうち、宣言した担当を
  ``(role, agent)`` の一覧で返す。adapter を読み込めない担当は含めない（起動の時点で unsupported になる）。

``AdapterContext(request, agent, role, work_dir, receipt_path)``
  request は現在の識別情報と依頼、agent/role は有効な設定。
  receipt_path は attempt 領域の新規ファイル。
  スキル・追加引数・モデル・effort・承認設定を実際の CLI に適用するのは adapter の責務。

``LaunchSpec(argv: tuple[str, ...], stdin: bytes, env: dict[str, str] = {})``
  プロンプトは stdin または request ファイルで渡す。argv に埋め込まない。
  シェルは使用しない。env は明示的な追加だけ。
  入れ子防止の AGENT_RUNNER_DEPTH=1 と Git routing の除外は runner が強制する。

``AdapterOutcome(receipt=None, identity=Identity(), state=None, diagnosis=None)``
  collect は CLI イベント・終了情報を解釈し、receipt と実測の identity を返す。
  receipt ファイルを使わず、CLI 出力から抽出した Receipt を返すことも可能。
  状態だけの success は受け入れない。
  認証・承認待ちを state として返せるため、receipt が無い失敗も分類できる。
  receipt ファイルがある場合は、その内容も runner が独立に検証する。
  diagnosis は試行の終わり方の要約（「診断の要約」の節）。返せない adapter は null のままでよい。

``Identity``
  model / provider / effort / session_id / cli_version は実測値または null。
  skill_versions は実際に読み込んだスキルの版の map。
  要求した設定を実測値としてコピーしない。未観測の項目は null / 空 map。
  collect はローカルの出力解析だけを行い、プロセスを起動したり無期限に待ったりしない。

request と receipt
------------------

``tools.agent_runner.models`` の Pydantic モデルを使う。
全モデルは未知キー禁止、型の暗黙変換禁止、非有限数禁止。

Request の JSON フィールド:

- schema_version: 1
- run_id / request_id / agent_id / role: 識別子
- work_dir: 専用 worktree の絶対パス
- prompt: 作業範囲の指示（呼び出し側が許可した例外の列挙を含む）を加えた依頼
- deadline_seconds: 残り時間
- result_shape: json または text

Receipt の JSON フィールド:

- schema_version: 1
- run_id / request_id / agent_id / role: Request と全一致する値
- status: success / awaiting_authentication / awaiting_approval / response_failed / interrupted
- payload: JSON object または string または null

成功時は role の result_shape と payload の型・非空を確認する。
Python API に ``payload_check`` を渡すと、この確認に通った payload をその関数でも検査し、
``True`` 以外を返すか例外を出したら invalid_result とする（明示 fallback の次の候補に進む。例外の文は出さない）。
途中 JSON、古い ID、他の agent/role、未知版・キー、不正な payload は invalid_result。
ファイルは準備時に存在してはいけない。symlink の receipt も拒否する。
receipt は停止確認後に読む。リンク・特殊ファイル・16 MiB を超えるファイルを
adapter が読む前に拒否する。停止未確認なら収集・書き換え自体を行わない。
終了コード 0 だけでは成功にしない。非ゼロ終了で success receipt があっても失敗。
非ゼロ終了に receipt が無い場合も response_failed とし、明示的な認証/承認待ちの診断を優先する。
内容の意味（例えばレビュー指摘の正しさ）の検証は各 workflow の利用側が担う。

結果
----

Python API は BatchResult を返す。CLI は result.json と短い JSON summary を保存・表示する。
summary は run_id / state / artifact_dir / result_path / after_run。
after_run は後処理ごとの index / state / exit_code / stop_confirmed / stdout_path / stderr_path の配列で、
後処理が無ければ空。引数の値は出さない。

BatchResult
  schema_version / run_id / state / assignments / results / outside_changes /
  elapsed_seconds / artifact_dir。
  assignments は self を含む有効な一次担当。成功・失敗混在は partial_success。
  state は success / self / partial_success / failed。

Result
  request_id / role / requested_agent / actual_agent / state / attempts / fallback_exhausted。
  attempts には明示 fallback を含む実際の起動順が残る。

Attempt
  agent_id / state / identity / diagnosis / exit_code / elapsed_seconds / stop_confirmed /
  work_dir / stdout_path / stderr_path / request_path / sensitive_paths / payload。
  diagnosis は collect が返した診断の要約（「診断の要約」の節）。collect が例外を出した試行、停止未確認の試行、
  起動の前に終わった試行、診断を返さない adapter の試行では null。
  sensitive_paths は、停止未確認または安全な redact ができず、秘密が残る可能性のある
  receipt やランタイムの既知保持物（Devin の ``export.json`` など）のパス。
  プロセス作成前の unsupported などでは path が null の場合がある。

Attempt / Result の state は success / self / unsupported / launch_failed /
awaiting_authentication / awaiting_approval / response_failed / missing_result /
invalid_result / timed_out / interrupted。
``stop_confirmed = false`` は停止未確認を示す。成功にせず、fallback も開始しない。
ログ・worktree は残し、パスを報告する。

診断の要約
----------

``Diagnosis`` は、誰も見ていなかった失敗の原因の型（認証・接続、provider の一時的なエラー、context の上限、
長い作業に固有の問題）を、後から記録だけで見分けるための要約である（#96）。数・固定の語・provider の code と
HTTP status・伏せ字の後の message の先頭だけを持ち、回答の本文・ツールの入力と結果・stdout/stderr の本文・
環境の値は持たない。成功した試行にも付く（adapter は runner の最終の state を知らない）。

.. list-table::
   :header-rows: 1

   * - キー
     - 値
   * - ending
     - completed（ターンが completed で終わり、最後まで出た）/ error_event（エラーのイベント）/
       turn_end（completed 以外のターンの終わり方）/ incomplete（イベント列が最後まで出なかった）/
       unreadable（読めない行があった）
   * - reason
     - turn_end のときの理由の種類（error / aborted / max-tokens など）。ほかは null
   * - code / status / message
     - provider の失敗の code（文字列）、HTTP status（整数）、message の先頭。取れなければ null
   * - steps / tool_calls
     - 始まったステップの数とツールの呼び出しの数
   * - last_event
     - 最後のイベントの種類
   * - last_input_tokens
     - 最後に終わったステップの入力 token の数（context の大きさ）。取れなければ null
   * - last_record / last_record_at
     - ランタイムの記録の最後の記録の種類と時刻（UTC）。取れなければ null
   * - stderr_authentication / stderr_approval
     - stderr が認証・承認の語に当たったか。本文は持たない

runner はバッチの終わりに、identity・payload と同じ秘密の値で diagnosis を伏せ字にし、その後で文字列の値を
すべて先頭 200 文字（``tools.agent_runner.models.DIAGNOSIS_TEXT_CHARS``）に切る。切ってから伏せ字にすると、
境界で切れた秘密の先頭が残りうるのでこの順にする。stdout/stderr は実行中にも伏せ字にされている。

期限と保持
----------

同時実行数は assignment 単位。role の期限は実行枠を得た時点から数え、
worktree の準備と明示 fallback を含む。準備の git checkout と hook/filter も
非同期の監督下で実行し、期限・中断で停止する。準備ログは worktree.stdout.log /
worktree.stderr.log とし、不完全な worktree も保持する。
同じ repository への並行した ``git worktree add`` が、別の登録の書きかけの
commondir を読んで何も作らずに止まったとき（終了コード 128、停止を確認済み、
作成先なし、stderr に ``worktrees/<id>/commondir:``）だけ、準備を最大 3 回まで
やり直す（#68）。期限は準備全体で数え、やり直す前の待ちで中断・期限切れに
なったら、やり直さずにその回の失敗を返す。やり直すときは、失敗した回のログを
worktree.stdout.log.<n> / worktree.stderr.log.<n>（n は失敗した回の番号）に
名前を変えて残す。
プロセスの停止確認に必要な猶予は期限を超えることがある。
trusted adapter の prepare/collect は同期処理なので、短いローカル処理だけに限定する。
SIGINT は共有 cancel event をセットし、所有するプロセスを停止して結果を保存する。

試行ごとの監督プロセスが Linux の独立 process group を保持し、元の CLI の
終了状態を標準出力とは別の制御 channel で返す。終了コードは監督プロセスではなく
元の CLI の実測値である。監督プロセスは子孫の停止処理が終わるまで残る。
/proc の PID/starttime により、通常の子孫と観測済みの setsid 子孫を
TERM → KILL で止め、停止を確認する。
数値の PGID だけで所有とみなさず、元の leader / 観測済み group member の
PID/starttime が一致する場合だけ追加の group member を観測する。
所有を確定できないときは無関係な PID へ signal を送らず、停止未確認とする。
終了した leader が残した子孫も停止する。zombie は実行中として数えない。
これは sandbox ではなく、観測を逃れる悪意ある二重 fork/setsid の封じ込めは保証しない。
停止未確認・失敗・成功を問わず、ログと worktree を自動では削除しない。

監査は開始前から登録されていた checkout の HEAD・index・tracked ファイル・
非 ignored untracked ファイルの内容を比較する。initialized submodule も含む。
変更は outside_changes と checkout_audit に警告として返し、処理自体は止めない。
外部の担当は bubblewrap の mount 名前空間で起動し、開始前から登録済みの checkout と
その Git 管理領域を読み取り専用にする。担当用の detached worktree とその Git 管理領域、
新しい Git object・worktree の保存先、runner の保存先は書き込み可能にする。
呼び出し側が明示した ignored の保存先も検査して書き込み可能にできる。
保護を構成できず起動に失敗した試行は成功にしない。
BatchResult.checkout_isolated は、全試行をこの保護の下で実行した場合だけ true となる。
受け取り側はこの値が true で、checkout_audit の全件が content_changed かつ
outside_changes のパスと一致する場合に限り、内容変更を拒否条件から外せる。
監査不能な checkout と停止未確認の試行は引き続き拒否する。
Gitignored 資源、symlink の参照先、Git 外の任意のディレクトリは監査範囲外。
外部セッションによる変更と委譲先による変更の因果までは断定しない。

環境変数は agent.env の名前だけ引き継ぎ、PATH が無ければ os.defpath を使う。
引き継ぐ値は runner プロセスの起動時環境から読み、
起動後に os.environ へ加えた変更は渡さない。
SECRET / TOKEN / CREDENTIAL / PASSWORD / API_KEY を含む名前の環境値を
stdout/stderr・返す payload/identity から redact する。
チャンク境界をまたぐ値も対象。停止確認後に retained receipt も JSON の成否によらず
ストリーミングで redact して置き換える。同様に、receipt と同じ attempt 領域に
ランタイムが書く既知の保持物（例: Devin の ``export.json``）も、収集の前に
同じ redact 経路で置き換える。安全に置き換えられない場合、または停止未確認の
あいだはそのファイルを読み書きせず sensitive_paths に残し、成功として返さない。
そのパスの内容をログやレビュー資料へ転載しない。
意図的に保存する prompt は資格情報を含めないこと。
値の変形・エンコード、名前から秘密と判別できない値までは自動で検出できない。

後処理
------

``after_run`` は、CLI の run が result.json を保存した後に、設定の順に起動する。
Python API の run_workflow は起動しない（result.json の保存が CLI の責務のため）。

- 各引数の中の ``{result}`` を result.json の絶対パスに置き換える。ほかの文字は置き換えない
- シェルを介さず、試行と同じ監督プロセス（独立 process group、期限、停止確認、ログの redact）で動かす。
  期限は 1 件 60 秒（``tools.agent_runner.hooks.AFTER_RUN_DEADLINE_SECONDS``）
- cwd は runner のある checkout の root（既定の雛形と同じ基準）
- 環境変数は runner の起動時の環境から ``GIT_*`` を除いたもの。``AGENT_RUNNER_DEPTH=1`` を付け、
  後処理から runner を入れ子に起動させない
- ログは artifact_dir の ``after-run-<番号>.stdout.log`` / ``after-run-<番号>.stderr.log``
- 失敗・期限切れでも、CLI の終了コードと result.json の state は変えない。結果は summary の after_run にだけ出す。
  state は success / failed / timed_out / interrupted / launch_failed / response_failed
- SIGINT で中断した run では起動せず、interrupted と報告する
- runner は後処理の中身（例えば pr-review の実行記録の形式）を知らない。
  雛形の workflow ``review`` は pr-review の ``review_run.py attach-runner --result {result}`` を呼ぶ

CLI と API
----------

.. code-block:: bash

   uv run --project tools/devenv python -m tools.agent_runner check
   uv run --project tools/devenv python -m tools.agent_runner list
   uv run --project tools/devenv python -m tools.agent_runner \
     --config /path/to/personal.toml run --workflow audit \
     --repository /path/to/repository --revision HEAD \
     --prompt-file /path/to/prompt.txt --concurrency 2 \
     --assign inspect=agent-a,agent-b --output-dir /tmp/unique-new-directory \
     --write-exception '/path/to/notes: the notes this run may write'

グローバルの --template / --config はサブコマンドの前に置く。
output-dir は既存 directory/symlink を拒否する。省略時は /tmp 内の一意の directory。
``--write-exception`` は API の ``write_exceptions`` の 1 件にあたる。繰り返し指定でき、指定した順に並ぶ。
workflow のすべての役割に同じ例外が付く（役割ごとには分けない）。
値の条件と拒否は下の API の段落に従う。CLI は API と同じ検査を使い、値を含まない拒否理由を stderr に出す。
stdout のエラー JSON と終了コード 2 は維持する。API の入力は run_workflow が検査する。
``-`` で始まる値は、argparse が別の引数をオプションと読むことがある（``--write-exception -x`` は invalid_input になる）ので、
``--write-exception=-x`` のように ``=`` でつないで書く。
値は CLI が標準出力に出す JSON（結果の要約・エラー）には出さないが、request.json のプロンプトには残る。
check/list はコマンドや環境変数の値を表示せず、設定と導入状態を JSON で返す。
六ランタイムが未導入でも、設定自体が妥当なら check/list は終了コード 0。
run は同期で全依頼の終了・停止確認まで待つ。
終了コードは success/self=0、失敗/部分成功=1、設定/入力=2、SIGINT=130。

.. code-block:: python

   from tools.agent_runner import load_config, run_workflow

   config = load_config(template, personal, assignments={"inspect": ["agent-a"]}, workflow="audit")
   result = run_workflow(config, "audit", prompt, repository,
                         revision="HEAD", concurrency=2, output_dir=None, cancel=None)

API は既存 asyncio event loop 内からは呼ばない。
``write_exceptions`` は、呼び出し側が作業範囲の指示（``Work in this worktree only`` と ``writes=...``）への
例外として許可するものの列。runner は空でなければ、その 2 行の直後に見出しと ``- <値>`` の行を順に置くだけで、
内容を解釈・強制しない（sandbox ではない）。
ただし、例外を実行できない担当（``read_only_assignments`` が返す担当）が writes=false の役割にいれば、
何も作らずに invalid_input で止まる（CLI の ``--write-exception`` も同じ）。例外の無い run では止めない。
例えば PR レビューの loop と review-fix-planning の self-review は、pr-review の helper が書く実行記録・保存先・保存ロックなどを、
review-verify の fix-check は一時ディレクトリ・再現用 worktree の登録などを渡す。
見出しは ``Caller-authorized exceptions to the rules above (only these; everything else stays forbidden):`` で、
一覧に無い書き込みは禁止と読まれる。プロンプトの本文にだけ書いた許可も禁止と読まれうるので、
例外を渡すときは、許可する書き込みをすべてこの一覧に並べる。

各値は、次の条件をすべて満たす文字列とする（実装は ``runner.py`` の ``_single_line``）。

- 空でない
- 前後に空白が無い。ここでの空白は ``str.strip()`` が取り除く Unicode の空白で、
  全角空白（U+3000）・NBSP（U+00A0）を含み、ゼロ幅文字などの Cf（U+200B など）は含まない
- ``str.splitlines()`` で 1 行になる（LF・CR・U+2028 などの改行類を含まない）
- Unicode の Cc（制御文字。タブ・NUL を含む）を含まない

行の途中の全角空白・NBSP と、Cf・Co（私用領域）・Cn（未割り当て）の文字は受け付ける。
Cf は前後にあっても受け付けるので、ゼロ幅文字 1 文字だけの値も通る（指示に空に見える行が 1 行増える）。
条件に合わない値と、列の代わりに文字列を 1 つそのまま渡した場合は、出力ディレクトリなど何も作る前に
RunnerError で拒否する（CLI では ``{"error": "invalid_input"}``、終了コード 2）。
列が空なら（CLI で ``--write-exception`` を付けなければ）、指示は従来と同じ文字列になる。

``payload_check`` は、成功した試行の payload が呼び出し側の期待する形かを確かめる関数（既定は ``None`` で、検査しない）。
例えば issue-to-pr の ``implement_task.py`` は、担当の結果が ``TaskResult`` に合うかを渡す。
合わない結果は invalid_result になり、役割の期限の中で明示 fallback の次の候補を試す。
停止未確認・timed_out・interrupted の試行では呼ばれず、fallback も始めない（上の扱いのまま）。
CLI からは指定できない。関数には redact の前の payload が渡るので、関数の中で値をログなどに出さない。
試行を進める event loop の中で同期に呼ぶので、短いローカル処理に限る。

``adapter_loader`` はテスト用に差し替えられる。既定は固定モジュールの loader。
run_workflow は外部 checkout の警告を含む完全な結果を返すが、自動保存は CLI の責務。

停止を確認し、成果物を保存した後の回収は、個々の worktree を対象 repository から
``git worktree remove <work_dir>`` で行う。変更がある worktree を勝手に force で消さない。
登録を外した後に、所有する artifact directory だけを削除する。
停止未確認の領域は回収しない。他の run の領域や既存の /tmp 残骸には触らない。
