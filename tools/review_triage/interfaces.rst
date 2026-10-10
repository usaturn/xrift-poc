.. _interfaces:

==================================
review_triage 共通インターフェース
==================================

.. _interfaces_overview:

概要
====

``tools/review_triage`` の全レーンが共有する型・関数の署名・失敗時の意味・質問セット v1 を定める。
input・client・policy・CLI・評価の各担当は、この文書と ``models.py`` / ``questions.py`` を読めば独立に実装できる。
型と質問の実体は ``models.py`` と ``questions.py`` にあり、この文書は両ファイルの署名と意味の定義元である

.. _interfaces_scope:

目的と境界
==========

review_triage は、過去のレビュー指摘を TypeSafe System One の API へ送り、指摘ごとの扱いを助言させる PoC である。
この文書では、TypeSafe System One のモデル（既定は ``jev-latest``）を Jev と呼ぶ。
Jev は state（送信する本文）と質問の組を受け取り、質問ごとに型付きの回答を返す。
ツールは親（ ``usaturn/xrift-poc`` ）限定であり、制作物のリポジトリへ依存やデータを持ち込まない

Jev の回答は助言であり、指摘の成立を示す検証証拠ではない。
次の境界を型と質問文で固定する

- ``TriageDecision.advisory`` は常に ``True`` であり、助言を最終判断へ昇格させない
- ``applies_*`` 質問の回答は「引用した committed 内容に照らして主張が事実か」だけを表し、再現検証の代わりにしない
- 4つの提案区分（ ``fix_now`` など）は、既存スキルの Fix / Skip / Hold / Resolved とは別に管理する
- Jev はコードの実行や検証証拠の生成を行わない
- 通常の検証（ ``./scripts/ci/verify-devenv.sh`` ）は実通信と実キーを使わず、fake の transport と合成データだけで検査する

.. _interfaces_types:

共通型一覧
==========

型はすべて pydantic v2 のモデルであり、生成後に変更できない（frozen）。
次の6つの型は schema の版として ``version: Literal[1]`` を持ち、 ``version=2`` などの値を拒否する

- ``ReviewBatch``
- ``PreparedState``
- ``SystemOneRequest``
- ``ClientOutcome``
- ``Thresholds``
- ``TriageReport``

.. _interfaces_types_base:

基底設定
--------

型の基底は、本ツールが生成する型と Jev の応答に由来する型で分ける。
応答由来の型が未知のフィールドで解析に失敗すると、その応答全体が障害として扱われるためである

.. list-table:: 基底設定
   :header-rows: 1
   :widths: 1 3 3

   * - 基底
     - 用途
     - 設定
   * - ``_Strict``
     - 本ツールが生成する型
     - ``extra="forbid"`` ・ ``frozen=True`` ・ ``allow_inf_nan=False`` 。未知のフィールド・NaN・inf を拒否する
   * - ``_FromApi``
     - Jev の応答に由来する型
     - ``extra="allow"`` ・ ``frozen=True`` ・ ``allow_inf_nan=False`` 。公式仕様に無いフィールドを ``model_extra`` に保持するが、判断には使わない

.. _interfaces_types_enums:

列挙
----

列挙はすべて ``enum.StrEnum`` であり、値は JSON にそのまま書ける文字列である

.. list-table:: 列挙
   :header-rows: 1
   :widths: 1 3 3

   * - 列挙
     - 値
     - 意味
   * - ``Repository``
     - ``usaturn/xrift-poc``
     - レビュー対象のリポジトリ。ディレクトリ名や本文から推定せず、呼び出し側が明示する
   * - ``Severity``
     - ``critical`` ・ ``high`` ・ ``medium`` ・ ``low`` ・ ``info`` ・ ``unknown``
     - 元レビューの severity。判別できない場合は ``unknown``
   * - ``FindingStatus``
     - ``active`` ・ ``resolved`` ・ ``pending`` ・ ``unverified`` ・ ``decided``
     - Critical〜Info 節、 ``## Resolved findings`` 節、 ``## Pending previous findings`` 節、 ``## Unverified candidates`` 節、 ``## Decided findings`` 節に順に対応する
   * - ``Marker``
     - ``verified`` ・ ``design_judgment`` ・ ``confirm_recommended`` ・ ``none``
     - 見出しの【検証済み】・【要設計判断】・【確認推奨】・マーカなしに対応する
   * - ``Proposal``
     - ``fix_now`` ・ ``separate_issue`` ・ ``reject`` ・ ``human_review``
     - 助言としての提案区分。既存の Fix / Skip / Hold / Resolved と対応づけない
   * - ``ErrorKind``
     - 14 種
     - client の機械可読な失敗種別。 :ref:`エラー種別と意味 <interfaces_errors>` で定める
   * - ``ReportStatus``
     - ``advisory`` ・ ``fallback`` ・ ``uncalibrated``
     - ``advisory`` は助言ドラフトを生成した状態。 ``fallback`` は障害・キーなし・送信不可で全件を人間確認に回した状態。 ``uncalibrated`` は request の model 文字列が ``Thresholds.model_id`` と一致せず全件を人間確認に回した状態

.. _interfaces_types_models:

型
--

各型のフィールドと不変条件を示す。
不変条件に反する入力は ``pydantic.ValidationError`` になり、成功扱いにしない

.. list-table:: 共通型
   :header-rows: 1
   :widths: 2 1 5 5

   * - 型
     - 基底
     - フィールド
     - 不変条件・補足
   * - ``ReviewHistoryRow``
     - ``_Strict``
     - ``reviewed_at: str`` （ISO 8601 UTC）、 ``head: str`` （7〜40 桁の hex）、 ``mode: str``
     - なし
   * - ``ReviewFile``
     - ``_Strict``
     - ``source_file: str`` 、 ``model_name: str`` （拡張子を除いたファイル名）、 ``target_pr: int | None`` （1 以上）、 ``header_head: str | None`` 、 ``header_date: str | None`` 、 ``history: tuple[ReviewHistoryRow, ...]``
     - なし
   * - ``ReviewFinding``
     - ``_Strict``
     - ``finding_id: str`` （ ``<model_name>/<status>/<code>`` ）、 ``finding_key: str`` （12 桁の hex）、 ``repository: Repository`` 、 ``review_dir: str`` 、 ``source_file: str`` 、 ``model_name: str`` 、 ``line_start: int`` 、 ``line_end: int`` 、 ``reviewed_head: str | None`` 、 ``detected_head: str | None`` 、 ``severity: Severity`` 、 ``status: FindingStatus`` 、 ``code: str`` 、 ``title: str`` 、 ``marker: Marker`` 、 ``body: str`` （見出しを含む原文）、 ``location`` ・ ``content`` ・ ``evidence`` ・ ``recommendation`` （いずれも ``str | None`` ）、 ``missing: tuple[str, ...]`` （欠けている箇条名）
     - ``line_start`` と ``line_end`` は 1 以上で ``line_end >= line_start`` 。 ``code`` は ``^[A-Z]{1,2}-[0-9]+$`` 。 ``body`` は空でない。同一ファイル内で ``finding_id`` が衝突したら input が ``~2`` ・ ``~3`` を付けて ``ReviewBatch.anomalies`` に記録する
   * - ``UnparsedBlock``
     - ``_Strict``
     - ``source_file: str`` 、 ``line_start: int`` 、 ``line_end: int`` 、 ``text_sha256: str`` 、 ``reason: str``
     - 原文を持たず SHA-256 だけを持つ。送信候補にしない
   * - ``ReviewBatch``
     - ``_Strict``
     - ``version`` 、 ``repository: Repository`` 、 ``review_dir: str`` 、 ``reviewed_head: str`` （呼び出し側が指定する検証対象 HEAD）、 ``files: tuple[ReviewFile, ...]`` 、 ``findings: tuple[ReviewFinding, ...]`` 、 ``unparsed: tuple[UnparsedBlock, ...]`` 、 ``anomalies: tuple[str, ...]``
     - ``finding_id`` と ``finding_key`` は一意。各 finding の ``source_file`` は ``files`` に存在する。 ``active_findings()`` は ``status=active`` だけを返し、 ``finding(finding_id)`` は未知の ID で ``KeyError`` を送出する
   * - ``StateSource``
     - ``_Strict``
     - ``kind`` （ ``review`` ・ ``diff`` ・ ``blob`` ・ ``context`` ）、 ``path: str`` 、 ``sha256: str`` 、 ``line_start: int | None`` 、 ``line_end: int | None`` 、 ``byte_count: int`` （0 以上）
     - なし
   * - ``PreparedState``
     - ``_Strict``
     - ``version`` 、 ``purpose`` （ ``finding`` ・ ``pair`` ）、 ``finding_ids: tuple[str, ...]`` （1〜2 件）、 ``sections: dict[str, str]`` （節キー付きの本文）、 ``sources: tuple[StateSource, ...]`` 、 ``content_sha256: str`` 、 ``char_count: int`` 、 ``byte_count: int`` 、 ``allowed: bool`` 、 ``allowed_by: tuple[str, ...]`` 、 ``blocked_reasons: tuple[str, ...]``
     - ``sections`` は空でなく、節キーは ``^[a-z][a-z0-9_]*$`` 。 ``content_sha256`` ・ ``char_count`` ・ ``byte_count`` は ``state_digest(sections)`` と一致する。 ``allowed`` なら ``blocked_reasons`` は空で ``content_sha256`` が ``allowed_by`` に含まれる。 ``allowed`` でなければ ``blocked_reasons`` は空でない。 ``finding`` なら ``finding_ids`` は 1 件、 ``pair`` なら異なる 2 件
   * - ``NoulQuestion``
     - ``_Strict``
     - ``type: Literal["noul"]`` 、 ``instructions: str`` 、 ``criteria: dict[Literal["true", "false"], str] | None``
     - ``instructions`` は空でない
   * - ``ChoiceQuestion``
     - ``_Strict``
     - ``type: Literal["choice"]`` 、 ``instructions: str`` 、 ``criteria: dict[str, str | None]``
     - 選択肢は 2〜255 件
   * - ``ScoreQuestion``
     - ``_Strict``
     - ``type: Literal["score"]`` 、 ``instructions: str`` 、 ``criteria: tuple[str, ...]``
     - 段階は 2〜10 件
   * - ``Question``
     - なし
     - ``NoulQuestion | ChoiceQuestion | ScoreQuestion`` を ``type`` で判別する union
     - なし
   * - ``SystemOneRequest``
     - ``_Strict``
     - ``version`` 、 ``model: str`` 、 ``state: dict[str, str]`` 、 ``questions: dict[str, Question]`` 、 ``state_sha256: str`` 、 ``question_set_version: str`` 、 ``purpose`` （ ``finding`` ・ ``pair`` ）、 ``finding_ids: tuple[str, ...]`` （1〜2 件）
     - ``state`` と ``questions`` は空でない。節キーと質問 ID は規約に一致する。 ``finding`` なら ``finding_ids`` は 1 件、 ``pair`` なら異なる 2 件。 ``to_api_payload() -> ApiPayload`` は ``state`` ・ ``model`` ・ ``questions`` だけを返し ``criteria=None`` を省く。 ``payload_bytes() -> int`` はその compact JSON の UTF-8 バイト数を返す
   * - ``NoulAnswer``
     - ``_FromApi``
     - ``type: Literal["noul"]`` 、 ``noul: float`` （0〜1）
     - 独立した ``confidence`` を持たない
   * - ``ChoiceAnswer``
     - ``_FromApi``
     - ``type: Literal["choice"]`` 、 ``choice: str`` 、 ``probabilities: dict[str, float] | None`` 、 ``confidence: float`` （0〜1）
     - なし
   * - ``ScoreAnswer``
     - ``_FromApi``
     - ``type: Literal["score"]`` 、 ``score: float`` 、 ``legend: dict[str, str] | None`` 、 ``probabilities: dict[str, float] | None`` 、 ``confidence: float`` （0〜1）
     - なし
   * - ``Answer``
     - なし
     - ``NoulAnswer | ChoiceAnswer | ScoreAnswer`` を ``type`` で判別する union
     - なし
   * - ``Usage``
     - ``_FromApi``
     - ``input_tokens: int`` 、 ``output_tokens: int`` （いずれも 0 以上）
     - なし
   * - ``SystemOneResponse``
     - ``_FromApi``
     - ``model: str`` 、 ``answers: dict[str, Answer]`` 、 ``usage: Usage`` 、 ``request_id: str | None`` （ ``x-typesafe-request-id`` ヘッダの値。client が付ける）
     - ``check_against(request: SystemOneRequest) -> tuple[str, ...]`` が欠落 ID・未知 ID・型の不一致・Choice の選択肢外を列挙する。空のタプルなら整合
   * - ``ClientError``
     - ``_Strict``
     - ``kind: ErrorKind`` 、 ``http_status: int | None`` 、 ``message: str`` （200 文字以内）、 ``retryable: bool``
     - ``message`` はサニタイズ済みの短い理由であり、キー・state・応答本文を含めない
   * - ``ClientOutcome``
     - ``_Strict``
     - ``version`` 、 ``ok: bool`` 、 ``response: SystemOneResponse | None`` 、 ``error: ClientError | None`` 、 ``model_requested: str`` 、 ``request_sha256: str`` 、 ``purpose`` 、 ``finding_ids: tuple[str, ...]`` 、 ``elapsed_ms: int | None`` 、 ``calls_made: int``
     - ``ok`` が ``True`` であることと ``response`` があることと ``error`` が無いことは同値。 ``finding`` なら ``finding_ids`` は 1 件、 ``pair`` なら異なる 2 件。 ``purpose`` と ``finding_ids`` は request から転記し、state 本文は持たない
   * - ``Thresholds``
     - ``_Strict``
     - ``version`` 、 ``model_id: str`` 、 ``choice_min_confidence: float`` 、 ``score_min_confidence: float`` 、 ``noul_yes_min: float`` 、 ``noul_no_max: float``
     - すべて 0〜1 で ``noul_no_max < noul_yes_min`` 。 ``model_id`` は request の model 文字列と照合する
   * - ``SeveritySummary``
     - ``_Strict``
     - ``by_model: dict[str, Severity]`` 、 ``highest: Severity`` 、 ``lowest: Severity`` 、 ``agreement: bool``
     - ``by_model`` のキーは対象の指摘と重複候補の ``finding_id`` 、値は各指摘の元の severity であり、空でない。 ``highest`` ・ ``lowest`` ・ ``agreement`` は ``by_model`` と一致する。元 severity から ``summarize_severity()`` で決定論的に作り、Jev の Score は使わない
   * - ``TriageDecision``
     - ``_Strict``
     - ``finding_id: str`` 、 ``proposal: Proposal`` 、 ``duplicate_candidates: tuple[str, ...]`` 、 ``severity_summary: SeveritySummary`` 、 ``evidence_refs: tuple[str, ...]`` 、 ``escalation_reasons: tuple[str, ...]`` 、 ``answers: dict[str, Answer]`` （判断に使った回答）、 ``advisory: Literal[True]``
     - ``advisory`` は常に ``True`` 。 ``proposal=human_review`` なら ``escalation_reasons`` は空でない。 ``escalation_reasons`` の各要素の形式は :ref:`エスカレーション理由 <interfaces_escalation>` で定める
   * - ``TriageReport``
     - ``_Strict``
     - ``version`` 、 ``repository: Repository`` 、 ``review_dir: str`` 、 ``reviewed_head: str`` 、 ``model_id_requested: str`` 、 ``model_id_responded: str | None`` 、 ``question_set_version: str`` 、 ``thresholds: Thresholds`` 、 ``status: ReportStatus`` 、 ``reason: str`` 、 ``decisions: tuple[TriageDecision, ...]`` 、 ``unparsed_count: int`` 、 ``decided_count: int`` 、 ``unverified_count: int`` 、 ``generated_at: str``
     - ``decisions`` の ``finding_id`` は一意。 ``status`` が ``advisory`` 以外なら全 decision が ``human_review`` 。 ``decided_count`` と ``unverified_count`` は 0 以上で、旧 v1 JSON に無い場合は 0 とする
   * - ``HttpRequest``
     - ``_Strict``
     - ``method: Literal["POST"]`` 、 ``url: str`` 、 ``headers: dict[str, str]`` 、 ``body: bytes`` 、 ``timeout_s: float`` （0 より大きい）
     - なし
   * - ``HttpResponse``
     - ``_Strict``
     - ``status: int`` （100〜599）、 ``headers: dict[str, str]`` 、 ``body: bytes`` 、 ``final_url: str``
     - transport は redirect を追わず 3xx をそのまま返す
   * - ``Transport``
     - ``typing.Protocol``
     - ``send(self, request: HttpRequest) -> HttpResponse``
     - client が標準ライブラリによる実装と fake を用意して差し替える

レビューの節と記録の扱い
------------------------

``load_reviews`` は ``## `` に続く節名をそのまま照合する（末尾の空白だけ除く）。
``FindingStatus`` の表にある節名（Critical〜Info は各重大度の節）を指摘・記録の節として読む。
``## Unverified candidates`` の ``### U-<n>: <疑い>`` と
``## Decided findings`` の ``### M-2: <判断済みの指摘>`` なども ``ReviewBatch.findings`` に保持する。
節の外や未知の節にある指摘の見出し、既知の節にある不正な見出し・空のタイトルは、従来どおり ``unparsed`` に残す。
``Unverified candidates`` ・ ``Decided findings`` では、見出しに属さない説明文（「なし」に理由を添えた記述など）は読み飛ばす。
既存の active ・ resolved ・ pending の見出しに属さない本文の検出は変えない。

- ``unverified`` の重大度はコードの英字にかかわらず ``unknown`` とする。
  ``decided`` は ``resolved`` ・ ``pending`` と同じくコードの英字から重大度を読む。
- ``unverified`` ・ ``decided`` の ``missing`` は「位置」「検出時 HEAD」だけを対象とする。
  active の「内容」「根拠」「推奨対応」は要求せず、候補の「具体的な疑い」「確認済みの根拠」などと
  判断済み記録の「判断」「新しい根拠」は ``body`` に保つ。
- Jev への指摘・重複候補の送信と評価用の対象抽出は ``active_findings()`` が返す active だけを対象にする。
  ``unverified`` ・ ``decided`` は送信しない。これらを参照する outcome が ``triage`` に直接渡されても、助言・重複判定・レポートの状態には使わない。
- ``decided`` は判断済みなので ``decisions`` には含めず、``decided_count`` の件数だけをレポートに出す。
  ``unverified`` は ``unverified_count`` に数え、``status_unverified`` を理由とする ``human_review`` の decision を残す。
  ``resolved`` ・ ``pending`` の扱いは変えない。フォールバック時も ``decided`` を再判断の対象には戻さない。
- Markdown と JSON の両方に 2 つの件数を出す。旧 v1 JSON の読み込みは維持するが、
  新しい件数フィールドを受け付けない旧 reader に新しい JSON を渡す場合は reader の更新が必要である。

.. _interfaces_signatures:

レーン別の署名
==============

各レーンは次の署名で関数を公開する。
型は ``tools.review_triage.models`` から絶対 import で読み、共通の ``conftest.py`` や登録用の registry を作らない

.. code-block:: python

    load_reviews(review_dir: Path, *, repository: Repository, reviewed_head: str) -> ReviewBatch
    prepare_state(batch: ReviewBatch, *, finding_ids: Sequence[str], committed_context: Mapping[str, str], allowed_source_hashes: AbstractSet[str]) -> PreparedState
    build_request(state: PreparedState, *, questions: Mapping[str, Question], model: str) -> SystemOneRequest
    SystemOneClient(*, api_key: str | None, model: str, transport: Transport | None = None, timeout_s: float = 30.0, max_request_bytes: int = REQUEST_BYTE_LIMIT, max_calls: int = 50)
    SystemOneClient.evaluate(request: SystemOneRequest) -> ClientOutcome
    triage(batch: ReviewBatch, outcomes: Sequence[ClientOutcome], *, thresholds: Thresholds, generated_at: str | None = None) -> TriageReport
    render_report(report: TriageReport) -> str

CLI と評価の入口は次のとおりである。
CLI のオプション・出力・手順は ``README.rst`` に定める ::

    uv run --project tools/devenv --no-sync python -m tools.review_triage --review-dir DIR --repository REPO --reviewed-head SHA --output-dir OUT [--source-repo PATH] [--diff-base SHA] [--allow-file FILE] [--live | --response-file FILE] [--thresholds FILE] [--max-calls N]   （終了コード 0 = レポート生成、2 = 入力/利用方法エラー）
    uv run --project tools/devenv --no-sync python -m tools.review_triage.evaluate --dataset DATASET --responses RESPONSES --output-dir OUTPUT

.. list-table:: 署名の担当
   :header-rows: 1
   :widths: 2 2 5

   * - 署名
     - 担当と配置
     - 意味
   * - ``load_reviews``
     - input（#383）。 ``inputs.py``
     - レビューディレクトリを ``ReviewBatch`` に分解する。 ``repository`` はディレクトリ名や本文から推定できないため呼び出し側が明示する。解釈できないブロックは ``unparsed`` に残す
   * - ``prepare_state``
     - input（#383）。 ``state.py``
     - 1 指摘または重複候補 1 組ごとに ``PreparedState`` を1件作る。バッチ全体を1つの state にしない。 ``allowed_source_hashes`` の要素は ``PreparedState.content_sha256`` （送る節の全文 ``canonical_sections(sections)`` の SHA-256）であり、含まれない state は ``allowed=False`` にする
   * - ``build_request``
     - foundation（#381）。 ``questions.py``
     - 許可済みで上限内の state と質問から ``SystemOneRequest`` を組み立てる純粋関数
   * - ``SystemOneClient`` ・ ``evaluate``
     - client（#385）。 ``client.py``
     - request を1回だけ送り ``ClientOutcome`` を返す。自動 retry をしない
   * - ``triage``
     - policy（#387）。 ``policy.py``
     - 1 指摘・1 組ごとの outcome を ``finding_ids`` で batch に対応づけ、 ``TriageReport`` を作る。 ``generated_at`` を省略したときだけ UTC の現在時刻を読む
   * - ``render_report``
     - policy（#387）。 ``report.py``
     - ``TriageReport`` を人が読むレポート文字列にする。state 本文を含めない
   * - CLI
     - cli（#389）。 ``__main__.py`` ・ ``cli.py``
     - 入力・接続・判定を結線する。 ``--live`` と許可ファイルが無ければ通信せず、 ``allowed=False`` の state では ``build_request`` を呼ばない
   * - evaluate
     - evaluation（#391）。 ``evaluate.py``
     - 保存した応答とラベルから閾値と精度を評価する。CLI の実装は変えない

.. _interfaces_signatures_foundation:

foundation が提供する関数
-------------------------

foundation は次の関数も提供する。
後続のレーンはこれらを使い、ハッシュや ID を独自に計算しない

.. code-block:: python

    finding_key(repository: Repository, review_dir: str, finding_id: str) -> str
    canonical_sections(sections: Mapping[str, str]) -> bytes
    state_digest(sections: Mapping[str, str]) -> tuple[str, int, int]
    summarize_severity(by_model: Mapping[str, Severity]) -> SeveritySummary
    applies_question_id(key: str) -> str
    proposal_question_id(key: str) -> str
    same_question_id(key_a: str, key_b: str) -> str
    question_set_for_finding(key: str) -> dict[str, Question]
    question_set_for_pair(key_a: str, key_b: str) -> dict[str, Question]
    referenced_sections(question: Question) -> frozenset[str]

:``finding_key``:
    ``repository`` ・ ``review_dir`` ・ ``finding_id`` を改行で連結した文字列の SHA-256 の先頭 12 桁を返す
:``canonical_sections``:
    節の順序を保った compact JSON（UTF-8）を返す。
    ``content_sha256`` と ``byte_count`` の基準である
:``state_digest``:
    ``(content_sha256, char_count, byte_count)`` を返す。
    ``char_count`` は節の本文の文字数の合計である

.. _interfaces_signatures_client:

client の振る舞い
-----------------

``SystemOneClient`` は次の振る舞いを守る

- ``model`` は既定値を持たない。
  呼び出し側（CLI）が ``DEFAULT_MODEL_ID`` （ ``jev-latest`` ）を渡し、実応答の ``model`` を ``SystemOneResponse`` に残す
- 送信前に次の表の順で検査し、最初に当たった失敗を通信0回の outcome で返す
- redirect を追わず、自動 retry をしない。
  retry を有効にする引数も持たない
- ``SystemOneResponse.request_id`` は応答の ``x-typesafe-request-id`` ヘッダの値である。
  英数字と ``._:-`` からなる 128 文字以内の値でなければ ``None`` にする
- 実装とテストに実キーを要求しない。
  テストは ``Transport`` の fake で応答を再生する

.. list-table:: 送信前の検査
   :header-rows: 1
   :widths: 1 6 2

   * - 順
     - 条件
     - ErrorKind
   * - 1
     - ``api_key`` が ``None`` か空白だけ、または HTTP ヘッダ値に使えない（可視 ASCII 以外を含む、4096 文字を超える）
     - ``no_key``
   * - 2
     - state 本文または ``finding_ids`` を UTF-8 にできない（孤立サロゲートを含む）
     - ``invalid_request``
   * - 3
     - state 本文の digest が ``state_sha256`` と一致しない
     - ``not_allowed``
   * - 4
     - request の ``model`` が client の ``model`` と異なる
     - ``invalid_request``
   * - 5
     - ``payload_bytes()`` が ``max_request_bytes`` を超える
     - ``too_large``
   * - 6
     - 送信回数が ``max_calls`` に達している
     - ``call_budget``

client は次の補助の名前も公開する。
CLI はキーの読み出しや疎通確認の request を自前で作らず、これらを使う

.. code-block:: python

    api_key_from_env(environ: Mapping[str, str] | None = None) -> str | None  # TYPESAFE_API_KEY だけを読む。空なら None
    connectivity_request(*, model: str = DEFAULT_MODEL_ID) -> SystemOneRequest  # 疎通確認用の固定の合成 request
    HttpsTransport(*, max_response_bytes: int = RESPONSE_BYTE_LIMIT, connection_factory: ConnectionFactory | None = None)
    TransportError(kind: ErrorKind, safe_message: str)  # transport が失敗種別を明示するときに送出する
    API_KEY_ENV = "TYPESAFE_API_KEY"
    REQUEST_ID_HEADER = "x-typesafe-request-id"
    RESPONSE_BYTE_LIMIT = 1_000_000
    MAX_TIMEOUT_S = 300.0

.. _interfaces_errors:

エラー種別と意味
================

``ErrorKind`` は ``ClientOutcome.error.kind`` に入る失敗種別である。
policy は ``invalid_request`` 以外の失敗を障害として扱い、 ``ReportStatus.FALLBACK`` で全件を ``human_review`` に回す。
``invalid_request`` はサービス障害ではなく、送信側の不正（質問や state の組み立ての誤り）を示す

.. list-table:: ErrorKind
   :header-rows: 1
   :widths: 2 4 2 4

   * - 値
     - 条件
     - 生成元
     - policy の扱い
   * - ``no_key``
     - API キーが無い、空白だけ、または HTTP ヘッダ値に使えない。通信は0回
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``not_allowed``
     - 送信を許可されていない state。client は state 本文と ``state_sha256`` の不一致で判定する。通信は0回
     - client。CLI は ``allowed=False`` の state で ``build_request`` を呼ばず、outcome を作らない
     - 全件 ``human_review`` へフォールバックする
   * - ``too_large``
     - ``payload_bytes()`` が ``max_request_bytes`` を超えた
     - client。 ``STATE_CHAR_LIMIT`` を超える state は input が ``allowed=False`` にするため、CLI は送らない
     - 全件 ``human_review`` へフォールバックする
   * - ``call_budget``
     - 呼び出し回数が ``max_calls`` を超えた
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``timeout``
     - 応答が ``timeout_s`` 以内に返らない
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``connection``
     - 接続に失敗した。HTTP status によらず、応答本文が Content-Length に届く前に切れた場合を含む
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``tls``
     - TLS の確立や証明書の検証に失敗した
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``redirect``
     - 3xx が返った。redirect は追わない
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``auth``
     - 401 または 403 が返った
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``invalid_request``
     - 422 が返った。または送信前の検査で、本文を UTF-8 にできない、request の model が client と異なる（通信は0回）
     - client
     - 送信側の不正として実装エラー扱いにし、障害と区別して報告する。助言は出さず、該当する指摘は ``human_review`` にする
   * - ``rate_limit``
     - 429 が返った
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``service``
     - 529 を含む 5xx が返った
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``malformed_response``
     - 応答 JSON を ``SystemOneResponse`` として解析できない
     - client
     - 全件 ``human_review`` へフォールバックする
   * - ``unexpected_status``
     - 上記以外の HTTP status が返った
     - client
     - 全件 ``human_review`` へフォールバックする

解析に成功した応答でも、 ``check_against`` が空でないタプルを返した場合は成功扱いにしない。
``build_request`` は次の例外を送出し、 ``SystemOneRequest`` を作らない

:``NotAllowedStateError``:
    ``state.allowed`` が ``False`` である。
    ``ValueError`` の派生
:``StateTooLargeError``:
    ``state.char_count`` が ``STATE_CHAR_LIMIT`` を超えている。
    ``ValueError`` の派生
:``ValueError``:
    質問が空である、質問 ID が規約に合わない、または指示文が参照する節キーが state に無い

.. _interfaces_escalation:

エスカレーション理由
====================

``TriageDecision.escalation_reasons`` の各要素は ``<code>`` または ``<code>: <detail>`` の文字列である。
``<code>`` は ``policy.EscalationCode`` （ ``enum.StrEnum`` ）の25値のいずれかであり、集計は先頭のコードで行う。
``<detail>`` は finding id・質問 ID・値などの補足であり、レビュー本文や state 本文を含めない

.. list-table:: EscalationCode
   :header-rows: 1
   :widths: 3 6

   * - 値
     - 条件
   * - ``report_fallback``
     - レポートが ``fallback`` （障害・キーなし・未送信・batch に無い finding id を参照する outcome）
   * - ``report_uncalibrated``
     - request の model が ``Thresholds.model_id`` と異なる
   * - ``status_resolved``
     - 指摘が resolved。以降の検査をしない
   * - ``status_pending``
     - 指摘が pending。以降の検査をしない
   * - ``status_unverified``
     - 候補が unverified。Jev の助言を使わず人間確認に回し、以降の検査をしない
   * - ``head_unknown``
     - ``reviewed_head`` も ``detected_head`` も無い
   * - ``head_mismatch``
     - 指摘の HEAD と対象 HEAD が前方一致しない
   * - ``insufficient_evidence``
     - ``location`` または ``evidence`` が無い
   * - ``design_judgment_marker``
     - 見出しが【要設計判断】
   * - ``missing_outcome``
     - active な指摘の outcome が0件（未許可・未送信を含む）
   * - ``duplicate_outcome``
     - 同じ指摘の outcome が2件以上
   * - ``invalid_request``
     - outcome の失敗種別が ``invalid_request``
   * - ``answer_missing``
     - ``applies_*`` または ``proposal_*`` の回答が無い
   * - ``answer_type_mismatch``
     - 回答の型が質問の型と異なる
   * - ``answer_unexpected``
     - 質問に無い ID の回答がある
   * - ``choice_outside_criteria``
     - ``proposal_*`` の choice が4区分のいずれでもない
   * - ``applies_uncertain``
     - ``noul_no_max < noul < noul_yes_min``
   * - ``proposal_low_confidence``
     - ``confidence < choice_min_confidence``
   * - ``model_chose_human_review``
     - モデルが ``human_review`` を選んだ
   * - ``applies_proposal_conflict``
     - applies が yes なのに ``reject`` 、または no なのに ``fix_now`` ・ ``separate_issue``
   * - ``verified_finding_rejected``
     - 【検証済み】の指摘にモデルが ``reject`` を選んだ
   * - ``severe_not_fix_now``
     - 重複候補を含む severity に ``critical`` ・ ``high`` ・ ``unknown`` があるのに ``reject`` ・ ``separate_issue``
   * - ``pair_invalid``
     - 組の outcome を使えない（重複・ ``invalid_request`` ・回答の欠落・型の違い・想定外の回答）
   * - ``duplicate_uncertain``
     - 組の ``noul`` が ``noul_no_max`` と ``noul_yes_min`` の間
   * - ``duplicate_proposal_conflict``
     - 確信ありの重複候補と提案が異なる

.. _interfaces_questions:

質問セット v1
=============

質問セットの版は ``QUESTION_SET_VERSION = "1"`` である。
質問文は英語で固定し、state の節キーをバッククォートで参照する。
1つの質問は1つの命題だけを問い、「再現」「検証済み」などの意味を持たせない

.. _interfaces_questions_ids:

質問 ID と節キー
----------------

質問 ID は ``finding_key`` （12 桁の hex）から決定論的に作り、 ``QUESTION_ID_PATTERN`` に一致させる

.. list-table:: 質問 ID
   :header-rows: 1
   :widths: 2 1 5

   * - ID
     - 種別
     - 問う命題
   * - ``applies_<key>``
     - Noul
     - 指摘の主張が対象 HEAD の committed 内容に照らして事実か
   * - ``proposal_<key>``
     - Choice
     - 指摘をどう扱うべきか。選択肢は ``fix_now`` ・ ``separate_issue`` ・ ``reject`` ・ ``human_review`` の固定順
   * - ``same_<key_a>_<key_b>``
     - Noul
     - 2つの指摘が同じ場所の同じ問題を指すか。 ``key_a < key_b`` の順に並べ替えるため、引数の順序によらず同じ ID になる

input は state の節キーを次のとおり作る。
指示文が参照する節キーのうち ``diff`` ・ ``evidence_a`` ・ ``evidence_b`` だけは省略できる（ ``OPTIONAL_SECTION_KEYS`` ）

:``finding``:
    指摘の原文
:``evidence``:
    対象 HEAD の committed 内容の抜粋
:``diff``:
    対象 HEAD の差分の抜粋。
    任意
:``finding_a`` と ``finding_b``:
    pair の各指摘の原文
:``evidence_a`` と ``evidence_b``:
    pair の各指摘の committed 内容の抜粋。
    任意。
    v1 の input は作らず、pair に ``committed_context`` を渡すと ``ValueError`` にする

.. _interfaces_questions_text:

質問文と選択肢
--------------

``questions.py`` が定義する質問文と選択肢は次のとおりである

.. code-block:: python

    APPLIES_INSTRUCTIONS = (
        "Using only `evidence` (committed content at the target HEAD) and `diff` when present, "
        "is the claim made in `finding` factually true for that content?"
    )
    APPLIES_CRITERIA: dict[Literal["true", "false"], str] = {
        "true": "The cited code or behaviour exists at the target HEAD as the finding describes.",
        "false": "The evidence does not show it, or the evidence contradicts the finding.",
    }
    PROPOSAL_INSTRUCTIONS = "Judged only from `finding` and `evidence`, which handling does this finding deserve?"
    PROPOSAL_CRITERIA: dict[str, str | None] = {
        "fix_now": "A concrete defect or factual error at the target HEAD that the same change should fix.",
        "separate_issue": "A real but separable improvement that deserves its own issue.",
        "reject": "Not a defect: the evidence contradicts it, or it asks for behaviour the project does not want.",
        "human_review": "Cannot be settled from the evidence alone; a person must look at it.",
    }
    SAME_INSTRUCTIONS = "Do `finding_a` and `finding_b` describe the same underlying problem at the same location?"

``same_*`` の Noul は criteria を持たない。
v1 は Score の質問と rubric を持たない。
severity の一致度は Jev に問わず、元レビューの severity から ``summarize_severity()`` で計算する

.. _interfaces_limits:

上限と既定値
============

上限は 2026-09-26 時点の公式仕様（api.md・models.md）に基づく。
tokenizer を持たないため、token の上限に対して文字数とバイト数で保守的な上限を置く

.. list-table:: 定数と既定値
   :header-rows: 1
   :widths: 3 3 5

   * - 名前
     - 値
     - 根拠
   * - ``API_ENDPOINT``
     - ``https://api.typesafe.ai/v1/systemone``
     - 公式 API の送信先
   * - ``DEFAULT_MODEL_ID``
     - ``jev-latest``
     - 人間の指示（2026-09-26）で常に最新のエイリアスを使う。公式が推奨する版の固定は採用しない
   * - ``STATE_CHAR_LIMIT``
     - ``24_000``
     - 公式上限「state と最長の質問で 32k tokens」に対する文字数の上限
   * - ``REQUEST_BYTE_LIMIT``
     - ``200_000``
     - 公式上限「1 リクエスト 64k tokens」に対するバイト数の上限
   * - ``CHOICE_MAX_OPTIONS``
     - ``255``
     - 公式の Choice の選択肢の上限
   * - ``SCORE_MIN_LEVELS`` ・ ``SCORE_MAX_LEVELS``
     - ``2`` ・ ``10``
     - 公式の Score の段階数
   * - ``QUESTION_ID_PATTERN``
     - ``^[a-z][a-z0-9_]{0,63}$``
     - 質問 ID の規約
   * - ``SECTION_KEY_PATTERN``
     - ``^[a-z][a-z0-9_]*$``
     - state の節キーの規約
   * - ``QUESTION_SET_VERSION``
     - ``"1"``
     - 質問セットの版
   * - ``Thresholds`` の既定値
     - ``choice_min_confidence=0.9`` 、 ``score_min_confidence=0.9`` 、 ``noul_yes_min=0.95`` 、 ``noul_no_max=0.05``
     - 評価前の暫定値。#391 が実データで見直す
   * - ``SystemOneClient`` の既定値
     - ``timeout_s=30.0`` 、 ``max_request_bytes=REQUEST_BYTE_LIMIT`` 、 ``max_calls=50``
     - 公式上限以下の自前の既定値

公式の上限とエラーは次のとおりである

- 1 リクエストは 64k tokens まで
- state と最長の質問の合計は 32k tokens まで
- 250,000 tokens/s または 1,200 req/min を超えると 429
- 文書化されたエラーは 401・422・429・529

.. _interfaces_comparison:

比較
====

Noul の確率と Choice・Score の confidence は意味が異なるため、同じ閾値で扱わない

.. list-table:: 回答の型の比較
   :header-rows: 1
   :widths: 2 3 3 3

   * - 観点
     - Noul
     - Choice
     - Score
   * - 返す値
     - ``noul`` （yes の確率）
     - ``choice`` （選んだ選択肢）と ``probabilities``
     - ``score`` （実数）と ``legend`` ・ ``probabilities``
   * - 確からしさ
     - ``noul`` そのもの。独立した ``confidence`` は無く、要求も捏造もしない
     - ``confidence`` （0〜1）
     - ``confidence`` （0〜1）
   * - 閾値
     - ``noul_yes_min`` 以上で yes、 ``noul_no_max`` 以下で no、その間は保留
     - ``choice_min_confidence``
     - ``score_min_confidence``
   * - v1 での用途
     - ``applies_*`` と ``same_*``
     - ``proposal_*``
     - なし

.. _interfaces_change:

変更手順
========

共有の型・質問・署名は、後続レーンが並行して依存するため次の手順で変更する

#. 変更が必要になった担当は、親 Issue #330 の coordinator へ影響範囲とともに報告する
#. coordinator が影響を調整し、foundation の所有ファイルを1つの PR で変更する。
   兄弟担当は ``models.py`` ・ ``questions.py`` ・ ``interfaces.rst`` と CI の収集箇所を並行して変更しない
#. 質問文・選択肢・rubric を変えたら ``QUESTION_SET_VERSION`` を上げる。
   schema に互換性のない変更を入れたら ``version`` を上げる
#. 閾値と評価結果は、request の model 文字列・実応答の model・評価日・質問セットの版の組で管理する。
   実応答の model が変わったら #391 で再評価する

.. _interfaces_summary:

まとめ
======

共通型は生成側を厳格に、応答側を寛容に検証し、助言を最終判断へ昇格させない。
各レーンはこの文書の署名と ``ErrorKind`` の意味に従い、共有ファイルを編集せずに fake の入出力で実装できる

.. _interfaces_references:

関連資料
========

- `TypeSafe Quick start <https://docs.typesafe.ai/introduction/quickstart>`__
- `TypeSafe API reference <https://docs.typesafe.ai/api.md>`__
- `TypeSafe Models <https://docs.typesafe.ai/models.md>`__
- `TypeSafe Confidence <https://docs.typesafe.ai/confidence>`__
- `TypeSafe Noul <https://docs.typesafe.ai/primitives/noul>`__
- `親 Issue #330 <https://github.com/usaturn/maatlog-poc/issues/330>`__
- `foundation Issue #381 <https://github.com/usaturn/maatlog-poc/issues/381>`__

以上
