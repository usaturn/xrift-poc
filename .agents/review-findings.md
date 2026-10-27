# review-findings v1 / v2: レビューの候補一覧・確認済みの一覧・工程の記録

レビューを「探す（pr-review）」「確かめてまとめる（review-verify）」「決めて直す（review-fix-planning）」の 3 つの工程に分けたときに、工程の間で受け渡す JSON の形式である（Issue #547）。形式の定義はこの文書にだけ書く。検査は `.agents/scripts/review_findings.py` が行う。

この文書では、レビュー修正ループの 1 回分を「サイクル」、レビューを行う 1 モデル分を「レビュアー」と書く。

## 1. 誰が何を書き、誰が読むか

| ファイル | 書く | 読む |
| --- | --- | --- |
| 候補一覧（`kind: "candidates"`） | pr-review（レビュアーごと） | review-verify |
| 確認済みの一覧（`kind: "verified"`） | review-verify | review-fix-planning、`stats`（サイクルの確認だけ） |
| 工程の記録（`kind: "planning"`） | review-fix-planning | 精度の改善の分析 |
| 文書の候補（`kind: "statement_candidates"`） | review-verify（fix_check が `statement-candidates` で書く） | review-verify（判定の突き合わせ）、`validate` |
| 実行記録（`kind: "review_run"`） | pr-review の helper（`review_run.py`。`runner` 欄だけは `attach-runner`） | `stats` |

- 指摘の欄（再現・利用経路・結果の種類・PR 起因・同種箇所・前回の修正との関係・見直しの根拠）の意味と値の決め方は、[review-records](review-records.md) 4.2 節に従う。この文書は、それを JSON で書くときの値の対応だけを定める。
- 採否（Fix・Skip など）と重大度の再評価は、どのファイルにも書かない。採否は review-fix-planning が判断の記録（review-records）に書く。
- 秘密の値（トークン、鍵、資格情報）を書かない。

## 2. 置き場所

レビューディレクトリ（親の main worktree の `reviews/<owner>/<repo>/#<PR>-<ブランチ>/`。`<owner>/<repo>` は PR が存在するリポジトリで、親・制作物・それ以外のどの PR も同じ規則。Issue #483）の中に置く。どれもコミットしない。保存先とその探し方は `.agents/scripts/review_target.py` が決める。

```text
reviews/<owner>/<repo>/#<PR>-<ブランチ>/
├── <model>.md                                  # レビューファイル（今までと同じ）
├── findings/
│   └── <model>-<HEAD 先頭 12 文字>.json          # 候補一覧
├── loop/                                       # PR 修正ループの記録（下の箇条書き）
├── runs/
│   └── <開始時刻>-<model>-<接尾辞>.json         # 実行記録（1 実行 1 ファイル。4.1 節）
└── verified/
    ├── <HEAD 先頭 12 文字>.json                  # 確認済みの一覧（サイクルの確認）
    ├── <HEAD 先頭 12 文字>.md                    # その要約
    ├── <修正後 HEAD 先頭 12 文字>.fix-check.json  # 確認済みの一覧（修正の確認）
    ├── <修正後 HEAD 先頭 12 文字>.fix-check.md
    ├── <修正後 HEAD 先頭 12 文字>.statement-candidates.json  # 文書の候補（修正の確認）
    └── <HEAD 先頭 12 文字>.planning.json         # 工程の記録
```

- 保存先のパスは、短く書くとき `findings/<model>-<HEAD 先頭 12 文字>.json`、`verified/<HEAD 先頭 12 文字>.json`、`verified/<修正後 HEAD 先頭 12 文字>.fix-check.json`、`verified/<修正後 HEAD 先頭 12 文字>.statement-candidates.json`、`verified/<HEAD 先頭 12 文字>.planning.json` と書く。
- 直下には `<model>.md` 以外のファイルを置かない。旧 PR 修正ループの成果物の監視（`tools/herdr_pr_review/artifacts.py`。#571 で削除）は直下の `*.md` だけを読むので、`findings/`・`verified/`・`runs/` はその動きを変えない。保存前の検査（`<model>.md` の fingerprint）もファイル単位なので、`runs/` が増えても保存先の変化とはみなさない。新しい PR 修正ループは、自分の記録（ロック・run・サイクル）を `loop/` の下に置く（形式は `.agents/skills/pr-review-fix-loop/references/records.md`）。
- 候補一覧はサイクルごとに別のファイルになる（HEAD が名前に入る）。レビューファイルは今までどおり上書きする。
- PR 修正ループも、通常の pr-review と同じ `reviews/<owner>/<repo>/#<PR>-<ブランチ>/` を使う。

## 3. 共通の項目と値

| キー | 値 |
| --- | --- |
| `schema` | 新規記録は `"review-findings/v2"`。既存の `"review-findings/v1"` も読む。文書の機械的候補は v1 のまま。知らない版は拒否する |
| `kind` | `"candidates"`・`"verified"`・`"planning"`・`"statement_candidates"`・`"review_run"`（v2 だけ） |
| `pr` | `{"repository": "owner/name", "number": 1}` |
| HEAD | 40 文字（SHA-256 のリポジトリでは 64 文字）の小文字の hex。短い形は 12 文字か `unknown` |
| 指摘の参照 | `<レビューファイル名から .md を除いたもの>#<指摘 ID>@<検出 HEAD の 12 文字か unknown>`。判断の記録（review-records 2.1 節）の `sources` と同じ書き方。段階2 の一覧にある `.md` 付きの参照も、`stats` は同じ指摘として数える |
| 時刻 | UTC の `YYYY-MM-DDTHH:MM:SSZ` |
| 文字列 | 空でなく、タブと改行以外の制御文字を含まない |

### 3.1 v2 の実行識別情報（`provenance`）

v2 は `reviewer`・`verifier`・`planner`・`inputs[]`・`accuracy.reviewers[]` に `provenance` を必須とする。既存の `model`・`runtime` は残す。以下のキーは全て必須、未知キーは禁止。

| キー | 値 |
| --- | --- |
| `agent_id` | 実担当の runner agent_id、司令塔自身なら `self`、未観測なら `unknown` |
| `role` | 実際に割り当てた役割名。runner の汎用 role 名をそのまま書ける |
| `provider`・`effort`・`session_id`・`cli_version` | 実測値。取れなければ `unknown` |
| `run_id`・`request_id` | runner の BatchResult / Result の値。非 runner 実行で取れなければ `unknown` |
| `skills` | 実際に読んだスキルの `[{"path", "sha256"}]`。同じ path の重複禁止。hash は64桁の小文字hexか `unknown` |

識別文字列は空白のみ・制御文字・HTMLコメントのマーカーを含めない。スキル無しは `[]`、読み込みを観測できない場合は `[{"path":"unknown","sha256":"unknown"}]`。主要 `skill` は実担当の `provenance.skills` に含める。v2 では主要 skill の path / sha256 も、未観測なら `unknown` を使える。

runner の Identity の null を `unknown`、skill_versions map を skills 配列に対応付ける（定義元: `tools/agent_runner/interfaces.rst`）。要求した設定を実測値として写さない。fallback は requested_agent ではなく actual_agent と実際の Attempt の identity を使う。非 runner 実行も推測せず未観測を明示する。

架空の実担当の例（これを各 agent 欄の `provenance` に置く）:

```json
{
  "agent_id": "self",
  "role": "decide",
  "provider": "example-provider",
  "effort": "high",
  "session_id": "example-session",
  "cli_version": "1.2.3",
  "run_id": "unknown",
  "request_id": "unknown",
  "skills": [{"path": ".agents/skills/example/SKILL.md", "sha256": "unknown"}]
}
```

v2 の `timing` は `started_at`・`finished_at`・`seconds`・`waiting_seconds`。秒数は有限の非負数か null（不明）。seconds は待ちを含む経過時間、waiting_seconds はその内数で既知なら seconds 以下。終了時刻は開始より前にしない。0秒は実測ゼロであり不明ではない。v1 の整数による秒数は従来どおり。

review-records 4.2 節のラベルとの対応:

| 欄（4.2 節） | JSON のキー | 値 |
| --- | --- | --- |
| 確度ラベル | `confidence` | 検証済み `verified`・確認推奨 `needs_check`・要設計判断 `needs_design_decision` |
| `再現` | `reproduction.status` | 実行した `executed`・実行しなかった `not_executed`・該当なし `not_applicable` |
| `利用経路` | `usage_path.value` | 通常 `normal`・限定的 `limited`・不明 `unknown`・該当なし `not_applicable` |
| `結果の種類` | `result_kind.value` | 機能停止 `stop`・誤った結果 `wrong_result`・安全性 `safety`・性能 `performance`・保守性 `maintainability`・文書 `documentation`・その他 `other` |
| `PR 起因` | `pr_relation.value` | 持ち込み `introduced`・顕在化 `surfaced`・既存 `preexisting`・不明 `unknown` |
| `同種箇所` | `same_kind` | `{"property", "range", "search", "result"}`、または `{"not_applicable": "<理由>"}` |
| `前回の修正との関係` | `prior_fix_relation.value` | 該当なし `not_applicable`・前回の修正が足した仕組み `added_mechanism`・規則の写し `rule_copy`・「同じ規則」の主張 `same_rule_claim`・不明 `unknown` |
| `見直しの根拠` | `review_basis` | 自由記述 |

## 4. 候補一覧（`kind: "candidates"`）

pr-review のレビュアーが、レビューファイルと同じ指摘を書く（`source: "reviewer"`）。`review_findings.py from-markdown` が旧形式のレビューファイルから読み取ったもの（`source: "markdown"`）も同じ種類で、そのときは下の「reviewer で必須」の項目を省ける。

| キー | 必須 | 内容 |
| --- | --- | --- |
| `source` | ○ | `"reviewer"` か `"markdown"` |
| `pr`・`reviewed_head` | ○ | 対象の PR と HEAD（`reviewer` では 40 文字） |
| `base` | reviewer で ○ | merge-base |
| `reviewer` | ○ | `{"model", "runtime", "review_file"}`。v2 は `provenance`（3.1節）も必須。`review_file` は `<model>.md` |
| `skill` | reviewer で ○ | その実行で読んだ SKILL.md の `{"path"（リポジトリ相対）, "sha256"}`。レビュアーが自分で求める |
| `timing` | reviewer で ○ | `{"started_at", "finished_at", "seconds"}`。レビューファイルの `Review timing` と同じ値 |
| `coverage` | reviewer で ○ | `{"read": [読んだ範囲], "checks": [{"command", "result"}], "declined": [{"target", "reason"}]}`。`declined` は判断しなかったもの（読まなかった・確かめなかった範囲）で、無ければ空の配列（「無し」と明示したことになる） |
| `findings` | ○ | active な指摘（レビューファイルの Critical〜Info の節）。Decided・Pending・Resolved は入れない |
| `unverified_candidates` | ○ | `[{"id": "U-1", "summary"}]`。レビューファイルの Unverified candidates と同じ |

指摘（`findings[]`）:

| キー | 必須 | 内容 |
| --- | --- | --- |
| `id`・`severity`・`title`・`locations`・`detected_head` | ○ | レビューファイルの見出し・位置・検出時 HEAD と同じ |
| `confidence` | reviewer で ○ | 3 章の対応 |
| `pr_relation` | reviewer で ○ | `{"value", "basis"}` |
| `property` | reviewer で ○ | 守るべき性質を 1 文で |
| `variants` | reviewer で ○ | 同じ種類の別の入力。`[{"input", "result": "violates"|"holds"|"unchecked", "how"}]`。`how` は確かめた方法、確かめていなければその理由 |
| `statement_sites` | reviewer で ○ | 条件を述べている箇所（README・スキル・`interfaces.rst`・ヘルプ）。`[{"path", "claim", "result": "consistent"|"inconsistent"|"unchecked"}]` |
| `failure_scenario` | reviewer で、`result_kind.value` が `stop`・`wrong_result`・`safety`・`performance` のとき ○ | `{"entry", "input", "environment", "steps": [操作の順番], "expected", "actual"}` |
| `reproduction`・`usage_path`・`result_kind`・`same_kind`・`prior_fix_relation`・`review_basis` | 任意 | 3 章の対応。どの指摘で書くかは 4.2 節に従う。`reproduction.status` が `executed` なら `commands`・`place`・`conditions`・`result`、`not_executed` なら `reason` が必須 |
| `suggested_fix` | 任意 | 推奨の修正。仮説であり、review-fix-planning が採用の前に確かめる |
| `raw` | 任意 | `from-markdown` が読み取った指摘の本文 |

<!-- review-findings:example candidates -->

```json
{
  "schema": "review-findings/v1",
  "kind": "candidates",
  "source": "reviewer",
  "pr": {
    "repository": "usaturn/xrift-poc",
    "number": 900
  },
  "reviewed_head": "9000000000000000000000000000000000000abc",
  "base": "8000000000000000000000000000000000000abc",
  "reviewer": {
    "model": "claude-opus-5-5",
    "runtime": "claude-code",
    "review_file": "claude-opus-5-5.md"
  },
  "skill": {
    "path": ".claude/skills/pr-review/SKILL.md",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "timing": {
    "started_at": "2026-10-04T01:00:00Z",
    "finished_at": "2026-10-04T01:40:00Z",
    "seconds": 2400
  },
  "coverage": {
    "read": [
      "PR の差分全体",
      "README.md の取り込みの節"
    ],
    "checks": [
      {
        "command": "uv run --no-project python <SCRATCH>/repro.py",
        "result": "+09:00 付きの日時を UTC として読む"
      }
    ],
    "declined": [
      {
        "target": "docs/usage.rst の全文",
        "reason": "時間切れ。取り込みの節だけ読んだ"
      }
    ]
  },
  "findings": [
    {
      "id": "M-1",
      "severity": "Medium",
      "confidence": "verified",
      "title": "オフセット付きの日時を UTC として読む",
      "locations": [
        "src/schedule/importer.py:41"
      ],
      "detected_head": "900000000000",
      "reproduction": {
        "status": "executed",
        "commands": [
          "uv run --no-project python <SCRATCH>/repro.py"
        ],
        "place": "PR head の隔離 worktree。repro.py は追加スクラッチに置いた",
        "conditions": "CSV の日時の列に `2026-10-05T09:00:00+09:00` を書く",
        "result": "終了コード 0。予定を 2026-10-05T09:00:00Z として取り込み、9 時間ずれる"
      },
      "usage_path": {
        "value": "normal",
        "detail": "README の手順どおり CSV を取り込む"
      },
      "result_kind": {
        "value": "wrong_result",
        "detail": "予定の時刻が 9 時間ずれる"
      },
      "pr_relation": {
        "value": "introduced",
        "basis": "この PR が追加した parse_time()。base には無い"
      },
      "same_kind": {
        "property": "日時の文字列は、タイムゾーン付きの値として解釈する",
        "range": "PR の差分",
        "search": "rg -n 'strptime\\(' src",
        "result": "src/schedule/export.py:18"
      },
      "failure_scenario": {
        "entry": "schedule import",
        "input": "日時の列が `2026-10-05T09:00:00+09:00` の CSV",
        "environment": "Dev Container",
        "steps": [
          "CSV を取り込む",
          "取り込んだ予定の時刻を表示する"
        ],
        "expected": "2026-10-05T00:00:00Z（同じ時刻）",
        "actual": "2026-10-05T09:00:00Z"
      },
      "property": "日時の文字列は、タイムゾーン付きの値として解釈する",
      "variants": [
        {
          "input": "`2026-10-05T09:00:00.500+09:00`（小数秒付き）",
          "result": "violates",
          "how": "repro.py に同じ列を渡した"
        },
        {
          "input": "`2026-10-05T09:00:00`（オフセット無し）",
          "result": "unchecked",
          "how": "時間切れ"
        }
      ],
      "statement_sites": [
        {
          "path": "README.md:120",
          "claim": "日時はオフセット付きで書ける",
          "result": "inconsistent"
        }
      ],
      "suggested_fix": "datetime.fromisoformat() で読み、オフセットを保つ"
    }
  ],
  "unverified_candidates": [
    {
      "id": "U-1",
      "summary": "書き出しでも同じずれが起きる疑い"
    }
  ]
}
```

### 4.1 実行記録（`kind: "review_run"`）

pr-review の helper（`.agents/skills/pr-review/scripts/review_run.py`）が、resolve の後の実行ごとに 1 つ書く（Issue #601）。成功した実行だけでなく、helper の拒否・モデルの `fail`・強制終了も残し、後から完走率・入力の条件・所要時間で評価するための記録である。書くのは helper だけで、モデルは書かず、手で直さない。指摘の中身は写さず、レビューファイルと候補一覧をパスと sha256 で参照する。v2 だけで、v1 は拒否する。

置き場所は `runs/<開始時刻>-<model>-<接尾辞>.json`（2 章）。開始時刻は prepare 開始の UTC を `20261008T123456Z` の形にしたもの、`<model>` はファイル名に使えるモデル名（指定 commit のレビューでも同じで、`review_kind` で区別する）、`<接尾辞>` は helper のセッションディレクトリ `pr-review-<PR>-<8 文字>` の 8 文字。

キーは常にすべて持つ。まだ分からない値と、起動の種類によって無い値は null にする。

| キー | 内容 |
| --- | --- |
| `pr`・`review_kind` | 対象の PR と、`"pr_head"`（resolve 時の PR head）か `"commit"`（指定 commit） |
| `mode` | `"Initial"`・`"Update"`。prepare が終わるまでは null |
| `pr_head`・`reviewed_head` | resolve 時の PR head と、実際にレビューした commit（完全なハッシュ） |
| `base` | merge-base。prepare が終わるまでは null |
| `reviewer` | `{"model", "runtime", "provenance"}`。候補一覧の `reviewer` から `review_file` を除いたものと同じ値 |
| `inputs` | `{"request", "previous_review", "records", "diff"}`。prepare が終わるまでは null |
| `inputs.request` | ランナー経由なら `{"sha256"（Request ファイルの値）, "deadline_seconds"（Request の残り時間）}`、それ以外は null |
| `inputs.previous_review` | 保存先に前回のレビューがあれば `{"sha256"}`、初回は null |
| `inputs.records` | `{"sha256", "decisions"}`。sha256 は prepare が読んだ判断の記録の状態を、キーの順をそろえた JSON にしたものの値。`decisions` は state（review-records の `fix`・`hold`・`skip`・`resolved`・`unverified`）ごとの件数 |
| `inputs.diff` | `{"files", "added", "deleted", "binary"}`。`git diff --numstat --find-renames <base>..<reviewed_head>` の件数 |
| `status` | `"started"`・`"success"`・`"failed"` |
| `failed_at` | 失敗した工程。`"prepare"`・`"review"`（モデルが `fail` で止めた）・`"complete"`（complete の検査が拒否した）・`"save"`・`"cleanup"`。失敗の種類は固定の値で分けず、`reason` に書く |
| `reason` | 失敗の理由 |
| `outputs` | `{"review_file", "review_sha256", "findings_file", "findings_sha256", "active_findings", "by_severity", "unverified"}`。パスはレビューディレクトリからの相対（`<model>.md`、`findings/<model>-<HEAD 先頭 12 文字>.json`）。`by_severity` は `critical`・`high`・`medium`・`low`・`info` の件数で、合計は `active_findings` |
| `timing` | `{"started_at", "finished_at", "prepare_seconds", "review_seconds", "save_seconds", "cleanup_seconds", "total_seconds"}`。秒数は有限の非負数か null（未計測） |
| `runner` | ランナーが観測した値。`attach-runner` が書くまでは null（下の「ランナーの観測値」） |

時間の意味: `prepare_seconds` は prepare の開始から終了まで、`review_seconds` は prepare の終了から complete（または `fail`）の開始まで、`save_seconds`・`cleanup_seconds` は complete の保存・後片付け、`total_seconds` は prepare の開始から記録を最後に書いた helper の処理の終わりまで。`started_at` は候補一覧の `timing.started_at` と同じ値。

状態ごとの条件（`validate` が検査する）:

| status | 条件 |
| --- | --- |
| `started` | `failed_at`・`reason`・`outputs`・`timing.finished_at`・`timing.total_seconds` は null |
| `success` | `failed_at`・`reason` は null。`mode`・`base`・`inputs`・`outputs`・`timing.finished_at` と 5 つの秒数は値を持つ |
| `failed` | `failed_at`・`reason`・`timing.finished_at`・`timing.total_seconds` は値を持つ。`failed_at` が `prepare` 以外なら `mode`・`base`・`inputs` も値を持つ。`outputs` は `failed_at` が `cleanup`（保存は済んだ）のときだけ値を持つ |

書く時点と状態の移り方:

| 時点 | 書く内容 |
| --- | --- |
| prepare の resolve の直後 | `started` で新しく作る。作れなければ prepare を失敗にする（何も残らない実行を作らない） |
| prepare の完了 | `mode`・`base`・`inputs`・`prepare_seconds` を書き足す |
| resolve の後の prepare の失敗 | `failed`（`prepare`） |
| complete | `success`、または `failed`（`complete`・`save`・`cleanup`） |
| `fail --state` | `failed`（`review`） |
| 強制終了・タイムアウト | 何も来ないので `started` のまま。集計では未完了に数える |

- `started` からはどの状態にも移れる。`failed`（`complete`）からは、complete を呼び直したときの `success`・`failed`（`complete`・`save`・`cleanup`）だけに移れる。ほかの `failed` と `success` は書き換えない（例: prepare の失敗の後の `fail --state` は `failed_at: prepare` を残す）
- 読み書きは保存と同じロック（親の git common dir の `pr-review-save.lock`）の中で行う
- 作成の後の更新に失敗しても、レビューの結果は変えない。helper の返却 JSON の `run_record_error` に理由を書く
- resolve の前の失敗（PR やモデルが決まらない）は、置き場所が決まらないので記録しない

ランナーの観測値: `review_run.py attach-runner --result <result.json>` が、ランナーの `result.json` の各試行を、`run_id`・`request_id`・`agent_id`・`role` が `reviewer.provenance` と一致する記録と突き合わせ、`runner` に `{"identity": {"model", "provider", "effort", "session_id", "cli_version"}, "attempt": {"state", "exit_code", "elapsed_seconds", "stop_confirmed"}}` を書く。Identity の null は 3.1 節と同じく `unknown` にする。`reviewer.provenance` は書き換えない（観測元ごとに別に持つ）。書き先は親の main worktree の `reviews/<owner>/<repo>/#<PR>-<ブランチ>/runs/*.json` のうち、この形式を満たす記録だけ。同じ値が既にあれば何もせず、違う値が既にあればどのファイルにも書かずに失敗する。一致する記録が無い試行（PR が決まる前の失敗など）は件数と ID を出すだけにする。タイムアウトした試行には `started` のままの記録が対応するので、集計でほかの未完了と区別できる。ランナーは、workflow `review` の後処理（`tools/agent_runner/interfaces.rst` の `after_run`）でこのコマンドを呼ぶ。

架空の記入例（成功した実行に、ランナーの観測値を結び付けた後）:

<!-- review-findings:example review_run -->

```json
{
  "schema": "review-findings/v2",
  "kind": "review_run",
  "pr": {"repository": "example/review-demo", "number": 900},
  "review_kind": "pr_head",
  "mode": "Update",
  "pr_head": "9000000000000000000000000000000000000abc",
  "reviewed_head": "9000000000000000000000000000000000000abc",
  "base": "8000000000000000000000000000000000000abc",
  "reviewer": {
    "model": "example-model", "runtime": "pi",
    "provenance": {
      "agent_id": "example-agent", "role": "inspect", "provider": "example-provider",
      "effort": "unknown", "session_id": "unknown", "cli_version": "unknown",
      "run_id": "example-run", "request_id": "example-request",
      "skills": [{"path": ".agents/skills/pr-review/SKILL.md", "sha256": "1111111111111111111111111111111111111111111111111111111111111111"}]
    }
  },
  "inputs": {
    "request": {"sha256": "2222222222222222222222222222222222222222222222222222222222222222", "deadline_seconds": 600},
    "previous_review": {"sha256": "3333333333333333333333333333333333333333333333333333333333333333"},
    "records": {"sha256": "4444444444444444444444444444444444444444444444444444444444444444", "decisions": {"fix": 2, "skip": 1}},
    "diff": {"files": 5, "added": 120, "deleted": 30, "binary": 0}
  },
  "status": "success",
  "failed_at": null,
  "reason": null,
  "outputs": {
    "review_file": "example-model.md",
    "review_sha256": "5555555555555555555555555555555555555555555555555555555555555555",
    "findings_file": "findings/example-model-900000000000.json",
    "findings_sha256": "6666666666666666666666666666666666666666666666666666666666666666",
    "active_findings": 3,
    "by_severity": {"critical": 0, "high": 1, "medium": 1, "low": 1, "info": 0},
    "unverified": 1
  },
  "timing": {
    "started_at": "2026-10-08T00:00:00Z", "finished_at": "2026-10-08T00:09:10Z",
    "prepare_seconds": 8.1, "review_seconds": 540.2, "save_seconds": 0.4,
    "cleanup_seconds": 1.2, "total_seconds": 549.9
  },
  "runner": {
    "identity": {
      "model": "example-model", "provider": "example-provider", "effort": "high",
      "session_id": "example-session", "cli_version": "1.2.3"
    },
    "attempt": {"state": "success", "exit_code": 0, "elapsed_seconds": 555.0, "stop_confirmed": true}
  }
}
```

## 5. 確認済みの一覧（`kind: "verified"`）

review-verify が書く。サイクルの確認（`mode: "cycle"`）は HEAD ごとに 1 つ、修正の確認（`mode: "fix_check"`）は試すたびに 1 つ。

| キー | 必須 | 内容 |
| --- | --- | --- |
| `mode` | ○ | `"cycle"` か `"fix_check"` |
| `pr`・`reviewed_head`・`base` | ○ | `fix_check` では `reviewed_head` は確かめたローカルの HEAD |
| `fix_check` | `fix_check` で ○ | `{"from": push 済みの PR head, "to": reviewed_head と同じ, "fix_ids": [確かめる Fix]}` |
| `statement_candidates` | `fix_check` で ○、`cycle` では書かない | 文書の候補への判定（下） |
| `fix_evidence` | v2 の `fix_check` で ○、v1 は任意、`cycle` では書かない | 列挙した修正の主張と、全変更ファイルの検査への判定（下） |
| `verifier` | ○ | `{"model", "runtime"}` |
| `skill`・`timing` | ○ | 4 章と同じ |
| `inputs` | ○ | 入力にしたレビュアー。`[{"review_file", "model", "runtime", "input_kind": "json"|"markdown", "findings_file": パスか null, "reviewed_head": 12 文字か unknown, "stale": 真偽}]`。`input_kind` が `json` なら `findings_file` が必須。`stale` は対象と違う HEAD をレビューした古いレビュアー |
| `coverage` | ○ | `{"closed": [{"target", "how"}], "remaining": [{"target", "reason"}]}`。レビュアーが判断しなかった範囲を閉じたか、残ったか |
| `findings` | ○ | まとめた後の指摘（下） |
| `accuracy` | ○ | 精度の改善に使う記録（下）。v2 の reviewers に provenance を必須追加し、同じ review_file の inputs の model/runtime/provenance と一致させる。入力ファイルは重複させない。find_seconds は入力の全ファイルを過不足なく含める |

v2 の inputs[] は provenance と waiting_seconds も必須。waiting_seconds は発見の待ち時間で、有限非負数か null、既知なら対応する accuracy.find_seconds 以下。v2 の cycle の sources は inputs にある review_file に解決されることだけを要求する。@<head> は指摘の検出時 HEAD であり、inputs の reviewed_head（その回のレビュー HEAD）とは独立して維持する。v2 の find_seconds/verify_seconds は有限非負数か null。

指摘（`findings[]`）:

| キー | 必須 | 内容 |
| --- | --- | --- |
| `id` | ○ | `V-1` からの連番 |
| `sources` | ○ | 元の指摘の参照すべて。`cycle` では 1 件以上。`fix_check` で確認担当が新たに見つけたものは空の配列。判断の記録でこの問題を参照するときは `fix-check#V-<n>@<to 先頭 12 文字>` と書く（review-records 2.1 節） |
| `property`・`locations`・`basis` | ○ | 守るべき性質、位置、判定の根拠 |
| `verdict` | ○ | `confirmed`（再現した）・`not_reproduced`（再現しなかった・反証した）・`unverified`（確認できていない）。コマンドの失敗・環境の不足・時間切れ・未読は `not_reproduced` にしない |
| `reproduction` | `confirmed` で ○、それ以外では書かない | 確認担当の再現 `{"commands", "place", "conditions", "result"}` |
| `missing_checks` | `unverified` で ○、それ以外では書かない | 足りない確認 |
| `variants`・`statement_sites` | ○ | 4 章と同じ形。確認担当が実行・照合した結果 |
| `pr_relation` | ○ | base と比べた結果と根拠 |
| `reported_severities` | ○ | `[{"source": 参照, "severity"}]`。元の指摘の重大度。再評価はしない |
| `safety_path` | 任意 | 安全性の 3 項目に当たると主張する指摘だけ。`{"item": 1|2|3, "entry", "input", "environment", "destination", "confirmed": 真偽}` |
| `cause_commit`・`first_present_head`・`late_detection` | 任意 | 原因が入った commit、その欠陥が存在していた最初のサイクルの HEAD、前のサイクルの HEAD にすでにあったのに後で見つかったか |

条件を述べる文書の候補への判定（`statement_candidates`）:

| キー | 必須 | 内容 |
| --- | --- | --- |
| `file` | ○ | 判定した候補ファイルの名前。`<reviewed_head 先頭 12 文字>.statement-candidates.json`（5.1 節）で、同じ `verified/` ディレクトリにある |
| `judgments` | ○ | `[{"ids": ["S-1", ...], "result", "basis"}]`。`ids` は 1 件以上、`basis` は判定の根拠 |

`result` の値は次の 4 つである。

| 値 | 意味 |
| --- | --- |
| `consistent` | 修正後の条件と合っている |
| `inconsistent` | 修正後の条件と食い違っている |
| `unrelated` | 事実として修正した条件と関係がない（同名の別物など）。「通常の利用ではない」は採否の判断なので、この値の根拠にしない |
| `unchecked` | 確かめられなかった |

`validate <fix-check.json>` は、同じディレクトリの候補ファイルを読んで次を突き合わせる。候補ファイルが無ければ終了コード 66 で終わる。

- 候補ファイルの全候補が、いずれかの `judgments[].ids` に 1 回ずつ現れる（判定漏れ・重複・存在しない ID を問題にする）。
- 候補ファイルの `from`・`to` が、`fix_check.from`・`fix_check.to` と一致する。
- `inconsistent` と判定した候補の `path` は、いずれかの指摘の `statement_sites` に `inconsistent` で載っている。

### 修正の主張と変更ファイルの検査（`fix_evidence`）

`statement-candidates --claims FILE` で固定した主張・変更ファイルを、次の形で全件判定する。新規の writer は v2 を使う。歴史的な v1 に新規欄が無いことは受理するが、主張・ファイル検査が済んだ証拠にはしない。

- `claims`: `[{"id": "C-1", "result": "holds"|"violates"|"unchecked", "basis", "checks": [...], "findings": ["V-1", ...]}]`。
- `files`: `[{"path", "result": "passed"|"failed"|"not_applicable"|"unchecked", "rule", "basis", "checks": [...], "findings": [...]}]`。
- `checks` の各行: `{"command", "head": to と同じ完全SHA, "mode": "executed"|"static", "exit_code": 整数かnull, "result", "artifact": 文字列かnull}`。`artifact` は生成したデータ/出力と、確かめた性質・検査述語を特定する。実行コマンドと出力を保存し、実行していない結果を補わない。

生成物 (`artifact`) と挙動 (`behavior`) の主張の `holds` には、終了コード0の `executed` 証拠が必要。生成物では `artifact` も必要で、読解やビルド成功だけで生成物の意味を満たしたことにしない。静的な主張 (`static`) はコミット済みの内容の読解で確かめられる。`holds`/`passed` の checks は空にせず、全件の終了コードを0にする。ファイルの `passed` にも実行証拠が必要。

`holds`/`passed` の `checks` は全件の終了コードが0なので、「不正な入力を0以外の終了コードで拒否する」ような主張は、期待どおりなら0で終わる検査（述語）のコマンドに包んで記録する。`head` は `to` なので、`from` 側での実行は `checks` ではなく `basis` に書く。

`rule` は適用する lint・型検査・ビルドの定義元を示す（ファイル名と節）。規則の本文を写さない。削除されたファイルや適用する検査が無い場合だけ `not_applicable` にし、何を読んでその判断をしたか `basis` に書く。検査コマンドの環境失敗、時間切れ、未読は `unchecked` とし、成功や規則違反に読み替えない。

`validate` は、固定した claims と changed_files が各1回判定され、Fix集合も一致することを検査する。未知ID/パス、重複、判定漏れ、違うHEAD、不十分な実行証拠を拒否する。`violates`/`failed` は `confirmed` の指摘に結び付け、`unchecked` は `coverage.remaining` の `claim:C-1` / `file:<path>` に残す。自然言語の主張をすべて抽出したか、証拠が意味的に十分かは確認担当が確かめる。validate の成功だけでその十分性を証明したことにしない。

条件を狭めた修正は、狭めた後に外れる場面を挙げ、文書の手順をその場面で実行して照合する。実行できなければ文書候補を `unchecked` とし残す。通常の利用か・対応するかは planner が決める。

精度の改善に使う記録（`accuracy`）:

| キー | 内容 |
| --- | --- |
| `reviewers` | レビュアーごとの `{"review_file", "model", "runtime", "skill_sha256": 文字列か null, "candidates", "confirmed", "not_reproduced", "unverified", "missed", "unique": [V-ID]}`。`missed` は、同じ HEAD をレビューしたほかのレビュアーが見つけて確認済みになった指摘のうち、このレビュアーが見つけなかったものの数。`unique` はこのレビュアーだけが見つけた確認済みの指摘 |
| `find_seconds` | `{review_file: 探すのにかかった秒数か null}` |
| `verify_seconds` | 確かめるのにかかった秒数 |

<!-- review-findings:example verified -->

```json
{
  "schema": "review-findings/v1",
  "kind": "verified",
  "mode": "cycle",
  "pr": {
    "repository": "usaturn/xrift-poc",
    "number": 900
  },
  "reviewed_head": "9000000000000000000000000000000000000abc",
  "base": "8000000000000000000000000000000000000abc",
  "verifier": {
    "model": "claude-opus-5-5",
    "runtime": "claude-code"
  },
  "skill": {
    "path": ".claude/skills/review-verify/SKILL.md",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "timing": {
    "started_at": "2026-10-04T02:00:00Z",
    "finished_at": "2026-10-04T02:25:00Z",
    "seconds": 1500
  },
  "inputs": [
    {
      "review_file": "claude-opus-5-5.md",
      "model": "claude-opus-5-5",
      "runtime": "claude-code",
      "input_kind": "json",
      "findings_file": "findings/claude-opus-5-5-900000000000.json",
      "reviewed_head": "900000000000",
      "stale": false
    },
    {
      "review_file": "gpt-6.1-sol.md",
      "model": "gpt-6.1-sol",
      "runtime": "pi",
      "input_kind": "markdown",
      "findings_file": null,
      "reviewed_head": "900000000000",
      "stale": false
    }
  ],
  "coverage": {
    "closed": [
      {
        "target": "docs/usage.rst の全文",
        "how": "読んで、日時の書式を述べる箇所を照合した"
      }
    ],
    "remaining": []
  },
  "findings": [
    {
      "id": "V-1",
      "sources": [
        "claude-opus-5-5#M-1@900000000000",
        "gpt-6.1-sol#L-2@900000000000"
      ],
      "property": "日時の文字列は、タイムゾーン付きの値として解釈する",
      "locations": [
        "src/schedule/importer.py:41",
        "src/schedule/export.py:18"
      ],
      "verdict": "confirmed",
      "basis": "隔離 worktree で、3 つの別の入力のうち 2 つでずれた",
      "reproduction": {
        "commands": [
          "uv run --no-project python <SCRATCH>/repro.py"
        ],
        "place": "9000000 の隔離 worktree。repro.py は一時ディレクトリに置いた",
        "conditions": "CSV の日時の列に `2026-10-05T09:00:00+09:00` を書く",
        "result": "終了コード 0。9 時間ずれる"
      },
      "variants": [
        {
          "input": "`2026-10-05T09:00:00.500+09:00`（小数秒付き）",
          "result": "violates",
          "how": "repro.py"
        },
        {
          "input": "`2026-10-05T09:00:00-05:00`",
          "result": "violates",
          "how": "repro.py"
        },
        {
          "input": "`2026-10-05T00:00:00Z`",
          "result": "holds",
          "how": "repro.py"
        }
      ],
      "statement_sites": [
        {
          "path": "README.md:120",
          "claim": "日時はオフセット付きで書ける",
          "result": "inconsistent"
        },
        {
          "path": "docs/usage.rst:40",
          "claim": "日時は UTC で書く",
          "result": "consistent"
        }
      ],
      "pr_relation": {
        "value": "introduced",
        "basis": "parse_time() はこの PR が追加した。merge-base には無い"
      },
      "reported_severities": [
        {
          "source": "claude-opus-5-5#M-1@900000000000",
          "severity": "Medium"
        },
        {
          "source": "gpt-6.1-sol#L-2@900000000000",
          "severity": "Low"
        }
      ],
      "cause_commit": "1a2b3c4",
      "first_present_head": "900000000000",
      "late_detection": false
    }
  ],
  "accuracy": {
    "reviewers": [
      {
        "review_file": "claude-opus-5-5.md",
        "model": "claude-opus-5-5",
        "runtime": "claude-code",
        "skill_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "candidates": 1,
        "confirmed": 1,
        "not_reproduced": 0,
        "unverified": 0,
        "missed": 0,
        "unique": []
      },
      {
        "review_file": "gpt-6.1-sol.md",
        "model": "gpt-6.1-sol",
        "runtime": "pi",
        "skill_sha256": null,
        "candidates": 1,
        "confirmed": 1,
        "not_reproduced": 0,
        "unverified": 0,
        "missed": 0,
        "unique": []
      }
    ],
    "find_seconds": {
      "claude-opus-5-5.md": 2400,
      "gpt-6.1-sol.md": null
    },
    "verify_seconds": 1500
  }
}
```

修正の確認の例（確認担当が、修正で変わった条件と食い違う文書を見つけた）:

<!-- review-findings:example fix_check -->

```json
{
  "schema": "review-findings/v1",
  "kind": "verified",
  "mode": "fix_check",
  "pr": {
    "repository": "usaturn/xrift-poc",
    "number": 900
  },
  "reviewed_head": "9100000000000000000000000000000000000abc",
  "base": "8000000000000000000000000000000000000abc",
  "fix_check": {
    "from": "9000000000000000000000000000000000000abc",
    "to": "9100000000000000000000000000000000000abc",
    "fix_ids": [
      "claude-opus-5-5#M-1@900000000000"
    ]
  },
  "verifier": {
    "model": "claude-opus-5-5",
    "runtime": "claude-code"
  },
  "skill": {
    "path": ".claude/skills/review-verify/SKILL.md",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "timing": {
    "started_at": "2026-10-04T03:00:00Z",
    "finished_at": "2026-10-04T03:12:00Z",
    "seconds": 720
  },
  "inputs": [],
  "coverage": {
    "closed": [],
    "remaining": []
  },
  "findings": [
    {
      "id": "V-1",
      "sources": [],
      "property": "取り込みの説明は、受け付ける日時の書式と一致する",
      "locations": [
        "README.md:120",
        "docs/usage.rst:40"
      ],
      "verdict": "confirmed",
      "basis": "修正はオフセット無しの日時を拒否するようにしたが、README はオフセット無しも書けると述べたまま",
      "reproduction": {
        "commands": [
          "git show 9100000:README.md | sed -n 115,125p"
        ],
        "place": "9100000 のコミット済みの内容",
        "conditions": "修正後の条件と、文書の記述を比べた",
        "result": "README.md:120 はオフセット無しも受け付けると述べる"
      },
      "variants": [],
      "statement_sites": [
        {
          "path": "README.md:120",
          "claim": "オフセット無しの日時も書ける",
          "result": "inconsistent"
        },
        {
          "path": "docs/usage.rst:40",
          "claim": "日時はオフセット付きで書く",
          "result": "consistent"
        }
      ],
      "pr_relation": {
        "value": "introduced",
        "basis": "この修正の差分で条件が変わった"
      },
      "reported_severities": []
    }
  ],
  "statement_candidates": {
    "file": "910000000000.statement-candidates.json",
    "judgments": [
      {
        "ids": [
          "S-1"
        ],
        "result": "inconsistent",
        "basis": "修正後の条件（オフセット必須）を書いていない"
      },
      {
        "ids": [
          "S-2"
        ],
        "result": "consistent",
        "basis": "オフセット付きで書く、と述べている"
      }
    ]
  },
  "accuracy": {
    "reviewers": [],
    "find_seconds": {},
    "verify_seconds": 720
  }
}
```

### 5.1 条件を述べる文書の候補（`kind: "statement_candidates"`）

review-verify の修正の確認（`fix_check`）が、`statement-candidates` コマンド（7 章）で書く。修正の差分で変わった条件の語を、修正後の文書から探して並べたものである。確認担当は、どの候補も読んで 5 章の `statement_candidates` に判定を書く。語の取り出しと検索は機械的に行い、文書が条件と合っているかの判断はしない。

| キー | 必須 | 内容 |
| --- | --- | --- |
| `schema`・`kind` | ○ | 3 章と同じ。`kind` は `"statement_candidates"` |
| `from`・`to` | ○ | 差分の両端のコミット（完全なハッシュ）。`to` は修正後の HEAD |
| `generated_at` | ○ | 書いた時刻（UTC、`YYYY-MM-DDTHH:MM:SSZ`） |
| `terms` | ○ | 差分から取り出した語 `[{"term", "origins": ["<path>:<line>", ...]}]`。`origins` は語の出どころで、追加行・文脈行は `to` の位置、削除行は `from` の位置（削除される前にあった行）を書く |
| `common_terms` | ○ | ヒットが多すぎて候補にしなかった語 `[{"term", "count"}]` |
| `candidates` | ○ | 文書の行の候補 `[{"id": "S-1", "path": "<path>:<line>", "terms": [...], "text"}]`。`id` は `S-1` からの連番。同じ行に複数の語が当たったときは 1 つにまとめる。`text` は行の本文。200 文字（制御文字を空白にして前後の空白を除いた後）を超える行は、その候補の `terms` の語ごとに最初に一致した位置の前後 80 文字を切り出し、重なる範囲をまとめて行内の順に ` … ` でつなぐ（先頭・末尾が行の端でなければ `… `・` …` を付ける）ので、どの語も `text` に現れる |

`--claims FILE` を付けると、次の3項目も必須の一組として書く。claims入力JSONは `{"fix_ids": [...], "claims": [...]}` のみ。確認担当は Spec の Fix 節（採用する変更・検証）、from..to の commit message、書き足した説明を読み、独立した主張を1件ずつ列挙してこの入力に保存してから判定する。

| キー | 内容 |
| --- | --- |
| `fix_ids` | 確かめるFix参照。結果の `fix_check.fix_ids` と集合が一致すること |
| `claims` | `[{"id": "C-1", "fix_id", "claim", "source", "kind": "artifact"|"behavior"|"static"}]`。IDは重複させず、各Fixに1件以上。sourceはSpec節・commit・追加説明の出典 |
| `changed_files` | Gitのfrom..toに含まれる全変更パス。削除・renameの旧新パス・テストも含む。`--no-renames --name-only -z` で列挙し、作業ツリーを読まない |

`--claims` が無い既存呼び出しは従来の候補だけを出す。新規v2 fix_checkはこの3項目を持つ候補ファイルでないと検査に通らない。

- 語の取り出し: `git diff -U3` の差分から取り出す。テスト・評価・fixture のディレクトリのファイルは除く。
  - コードのファイル: 変更行と前後の文脈行から状態名（大文字の識別子）・識別子の形の文字列リテラル・CLI オプションを、変更行だけから識別子らしい語（`_` を含む語、`camelCase` など大文字小文字の混じる語。`.` を含む語は、文字列リテラルとコードスパンから取る場合だけ）を取り出す。kebab-case の名前（`input-write`）も識別子らしい語に含める。最初の部分が 1 文字の名前（`C-1`）は含めない。
  - 文書のファイル（`.md`・`.rst`）: 変更行（追加行と削除行）だけから取り出し、文脈行からは取らない。識別子らしいコードスパン、RST の directive 名（`.. versionadded::` の `versionadded`）と role 名（`` :py:func:`...` `` の `py:func`）、CLI オプション、行の中の識別子らしい語（kebab-case と、`:` でつないだ名前 `rst:directive` を含む。コードスパンの中の語も取るので、`` `load_all()` `` からは `load_all` も取る）を取る。語の境界は ASCII の英数字で判定する（`MERGED後も` の `MERGED` も取る）。
  - 取らない語: 文書の小文字だけの語（`body`・`deprecated` など。directive 名・role 名は除く）と、コードスパンでない `.` を含む名前（`conf.py`）。修正の主題がこうした語で書かれているときは、確認担当が語を足して検索する（5 章の `inventory` の後ろに足す）。
- 検索の範囲: 修正後の HEAD の `.md`・`.rst` から、語が単語として現れる行（語の前後が英数字・`_` でない）を探す。テスト・評価・fixture のディレクトリの文書は除く。
- `common_terms`: 1 つの語のヒット行が `COMMON_TERM_LIMIT`（30）を超える語は、どこにでも出る語とみなして候補にせず、語と件数だけを `common_terms` に残す。候補が膨らんで判定が雑になるのを避けるための上限である。

<!-- review-findings:example statement_candidates -->

```json
{
  "schema": "review-findings/v1",
  "kind": "statement_candidates",
  "from": "9000000000000000000000000000000000000abc",
  "to": "9100000000000000000000000000000000000abc",
  "generated_at": "2026-10-05T00:00:00Z",
  "terms": [
    {
      "term": "TIMESTAMP_OFFSET",
      "origins": [
        "src/parser.py:42"
      ]
    }
  ],
  "common_terms": [
    {
      "term": "HEAD",
      "count": 40
    }
  ],
  "candidates": [
    {
      "id": "S-1",
      "path": "README.md:120",
      "terms": [
        "TIMESTAMP_OFFSET"
      ],
      "text": "TIMESTAMP_OFFSET を省くとオフセット無しの日時も書ける"
    },
    {
      "id": "S-2",
      "path": "docs/usage.rst:40",
      "terms": [
        "TIMESTAMP_OFFSET"
      ],
      "text": "TIMESTAMP_OFFSET を指定し、日時はオフセット付きで書く"
    }
  ]
}
```

## 6. 工程の記録（`kind: "planning"`）

review-fix-planning が、サイクルの最後（状況コメントの投稿の後）に書く。

| キー | 内容 |
| --- | --- |
| `pr`・`reviewed_head` | 対象の PR と、計画に使った HEAD |
| `planner` | `{"model", "runtime"}`。v2 は `provenance`（3.1節）も必須 |
| `verified_file` | 入力にした確認済みの一覧（`verified/<12 文字>.json`） |
| `stages` | `{"decide_seconds", "fix_seconds", "fix_check_seconds": [試すたびの秒数]}`。行っていない工程は null か空の配列 |
| `fix_checks` | `[{"file": 修正の確認の一覧, "blocking_fixes": push を止めた Fix の数}]` |
| `outcome` | 新しいplannerは `fixes_pushed`・`no_fix_needed`・`stopped`。旧 `awaiting_approval`・`stopped_*` も判断の記録と同じ整合条件で読む。停止の具体的理由は判断の記録の `stop_reason` に置く |
| `comment_id` | 投稿した状況コメントの ID。投稿していなければ null |

v2 は `activities` と `rework` も必須にする。既存 `stages` は互換の要約として残すが、v2 の集計は activities だけを使う。

- `activities[]`: `{"stage", "actor", "seconds", "waiting_seconds"}`。stage は find / verify / decide / implement / self_review / fix_check / pre_push_review。actor は `{"model", "runtime", "provenance"}`。実際に行った工程を担当ごと・試行ごとに書く。行っていない工程は省く。self は agent_id に書く。時間は3.1節と同じ。
- `rework[]`: `{"source", "implementation", "detected_stage"}`。source は指摘の参照、implementation は実装担当の `{"model", "runtime", "provenance"}`。detected_stage は self_review / fix_check / pre_push_review / next_cycle。因果を確認できたものだけを書く。不明な担当を推測しない。

採否は判断の記録の sources と確認済みの一覧の sources で照合する。判断の記録にも v2 の識別情報を保存する（review-records 6.2節）。

v2 の独立した架空の例（未観測値を明示し、実装工程の測定値だけを保存）:

<!-- review-provenance:example planning_v2 -->
```json
{
  "schema": "review-findings/v2",
  "kind": "planning",
  "pr": {"repository": "example/review-demo", "number": 900},
  "reviewed_head": "9000000000000000000000000000000000000abc",
  "planner": {
    "model": "example-model", "runtime": "pi",
    "provenance": {
      "agent_id": "self", "role": "decide", "provider": "unknown",
      "effort": "unknown", "session_id": "example-session", "cli_version": "unknown",
      "run_id": "unknown", "request_id": "unknown", "skills": []
    }
  },
  "verified_file": "verified/900000000000.json",
  "stages": {"decide_seconds": null, "fix_seconds": 12, "fix_check_seconds": []},
  "fix_checks": [],
  "outcome": "fixes_pushed",
  "comment_id": null,
  "activities": [{
    "stage": "implement",
    "actor": {
      "model": "example-model", "runtime": "pi",
      "provenance": {
        "agent_id": "self", "role": "implement", "provider": "unknown",
        "effort": "unknown", "session_id": "example-session", "cli_version": "unknown",
        "run_id": "unknown", "request_id": "unknown", "skills": []
      }
    },
    "seconds": 12, "waiting_seconds": 2
  }],
  "rework": []
}
```

既存 v1 の例:

<!-- review-findings:example planning -->

```json
{
  "schema": "review-findings/v1",
  "kind": "planning",
  "pr": {
    "repository": "usaturn/xrift-poc",
    "number": 900
  },
  "reviewed_head": "9000000000000000000000000000000000000abc",
  "planner": {
    "model": "claude-opus-5-5",
    "runtime": "claude-code"
  },
  "verified_file": "verified/900000000000.json",
  "stages": {
    "decide_seconds": 600,
    "fix_seconds": 1500,
    "fix_check_seconds": [
      720,
      540
    ]
  },
  "fix_checks": [
    {
      "file": "verified/910000000000.fix-check.json",
      "blocking_fixes": 1
    },
    {
      "file": "verified/920000000000.fix-check.json",
      "blocking_fixes": 0
    }
  ],
  "outcome": "fixes_pushed",
  "comment_id": 5987695309
}
```

## 7. 検査と集計のコマンド

親リポジトリの toplevel を `$TOPLEVEL` とする。どれも標準ライブラリだけで動く。問題があれば、場所付きで 1 行ずつ標準エラーに出し、終了コード 65（形式）か 66（読めない）で終わる。

```bash
# 検査
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" validate <ファイル>
# レビューファイル（Markdown）を候補一覧の形に読み取る（JSON の無いレビュアー）
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" from-markdown <model>.md \
  --review-file <model>.md --repo <owner/name> --pr <番号> --model <モデル> --runtime <ランタイム>
# レビューファイルと候補一覧の指摘（ID・重大度・位置・未確認の候補）が一致するか
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" check-pair <model>.md <候補一覧>
# 修正の確認用に、修正の差分の語で文書の候補を書く（出力先は verified/<修正後 HEAD 先頭 12 文字>.statement-candidates.json）
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" statement-candidates \
  --root <worktree> --from <PR head> --to <修正後 HEAD> --output <出力ファイル>
# PR をまたいだ集計（--records は review_records.py fold の出力）
uv run --no-project python "$TOPLEVEL/.agents/scripts/review_findings.py" stats <レビューディレクトリ>... [--records <ファイル>]
```

- `review_findings.py validate` は形式（キー・値・条件付きの必須）を確かめる。修正の確認の一覧（`fix-check.json`）だけは、同じディレクトリの候補ファイルとの突き合わせ（5 章）も行う。どの指摘で `再現` などを書くかの規則（4.2 節）は写さない。
- `review_findings.py check-pair` は、レビューファイルの Critical〜Info の節の指摘と、Unverified candidates の ID を比べる。JSON の `locations` の各値が、レビューファイルの `位置` の行に含まれることを確かめる。
- `review_findings.py stats` は各ディレクトリの `verified/*.json` の cycle と v2 planning を検査・集計する。`runs/*.json` の実行記録も検査し、`runs` キーに集計する（7.2 節。`runs/` に実行記録以外のファイルがあれば形式不正）。従来の documents / reviewers / severity_drift（誤った指摘の割合、見逃し、遅れた発見、重大度のずれ）は維持し、provenance を追加する。修正の確認・v1 planning・文書の候補・候補一覧は従来どおり検査せず読み飛ばす。`kind`・`mode` が無い・知らない値の一覧は検査で止まる。形式不正は終了コード 65。

### 7.1 識別情報による集計（`provenance`）

- `executions`: stage と model / runtime / provenance の全値で区別した行。スキル配列の順序では分けない。
- `by_dimension`: stage / role / model / effort / agent_id / runtime / cli_version / provider / skill / session_id / run_id / request_id ごとの行。各行は value と以下の指標。skill の value は `path@sha256`。複数スキルの実行は各版へ帰属するため、軸をまたいだ合計を全体時間としない。

| 指標 | 意味 |
| --- | --- |
| candidates / confirmed / not_reproduced / unverified / missed | accuracy にある発見の件数 |
| adopted | 判断の現在の状態または history に Fix がある source の採用実績。source を複数の判断に分割しても一度。resolved 後も残す |
| late_detections | late_detection が真の確認済みの指摘を発見した数 |
| rework | 実装担当へ明示して帰属させた source の数。同じ担当・PR・source の重複は一度 |
| seconds / waiting_seconds | 実測された時間の合計。seconds は待ち込み、waiting_seconds はその内数 |
| timed_activities / unknown_activities / unknown_waiting_activities | 経過時間を測った件数、不明の件数、待ち時間が不明の件数。既知の0秒と不明を区別 |

発見の時間は accuracy.find_seconds と inputs.waiting_seconds、確認の時間は verifier の timing、工程の時間は planning.activities を使う。同じ PR・reviewed_head・実担当・stage の planning があるときは、その明示時間を優先し、確認済みの一覧の時間を二重加算しない。activities の再試行は全て加算する。既存 stages は集計しない。集計の時間は担当別の経過時間であり、並行実行の実時間とは異なる。

採否は records.pr が一致する資料だけに適用し、source のファイルと検出 HEAD も照合する。v2 records では、実際に Fix を決めた履歴のサイクルの seats の識別情報まで一致させ、後の resolved や inherited のセッションへ採用を移さない。records が無い、別PRしか無い、または必要な識別情報が欠ける行・その集計では adopted は null（未評価）とする。候補の参照だけを持つ既存 v1 records は PR と参照で照合する。v1 文書の追加識別情報は unknown として出力し、入力を推測で書き換えない。records の不正な構造も終了コード65で報告する。

### 7.2 実行記録の集計（`runs`）

`stats` は、指定したディレクトリの `runs/*.json`（4.1 節）を読み、出力の `runs` に `documents`（記録の数）・`total`（全体）・`by_dimension`（軸ごとの行の配列）を書く。既存のキー（`documents`・`reviewers`・`severity_drift`・`provenance`）の値は、記録の有無で変わらない。

- 軸: 7.1 節の `model`・`runtime`・`role`・`effort`・`agent_id`・`cli_version`・`provider`・`skill`・`session_id`・`run_id`・`request_id`（発見だけなので `stage` は無い）と、ランナーが観測した値の `runner_model`・`runner_provider`・`runner_effort`・`runner_session_id`・`runner_cli_version`・`runner_state`。`runner` 欄の無い記録のランナーの軸は `unattached`。`provenance` の `effort` などの `unknown` と、ランナーの観測値は別の軸として並ぶ
- 各行は `value` と次の指標を持つ

| 指標 | 意味 |
| --- | --- |
| `runs` | 記録の数 |
| `success`・`failed` | status ごとの数 |
| `timed_out` | status が `started` で、ランナーが `timed_out` と観測した数 |
| `incomplete` | status が `started` で、`timed_out` でない数。集計の時点で実行中の記録も含む |
| `completion_rate` | `success / runs`。記録が無ければ null |
| `failed_at` | `prepare`・`review`・`complete`・`save`・`cleanup` ごとの数 |
| `seconds` | 工程（`prepare`・`review`・`save`・`cleanup`・`total`）ごとの `{"total", "measured"}`。null（未計測）の秒数は `measured` に数えない |
| `changed_lines`・`review_seconds_per_changed_line` | `inputs.diff` と `review_seconds` がそろった記録の、追加行と削除行の合計と、その 1 行あたりの `review_seconds`。合計が 0 なら null |

秒数は実行ごとの経過時間の合計であり、並行したレビュアーの実時間とは異なる。

## 8. 互換と、形式を変えるとき

- 候補一覧の無いレビュアー（旧形式、旧 PR 修正ループ、JSON を保存できなかったとき）は、review-verify が `from-markdown` で読み、`inputs[].input_kind` を `markdown` にする。
- 旧 PR 修正ループ（`LOOP_CONTEXT_V1=`。#571 で削除）は、この文書の形式のファイルを作らず、読まなかった。新しい PR 修正ループ（#570）のレビュアーは通常の pr-review で候補一覧を作り、各サイクルの計画は review-fix-planning が確認済みの一覧と工程の記録を作る。
- 新規の candidates / verified / planning は v2 にし、3.1節の識別情報を必須にする。既存 v1 は無変換で読み、検査条件も維持する。`from-markdown` は旧資料の読み取りなので v1、`statement-candidates` の出力も v1 のまま。スキルが新規項目を書く変更は #562 / #568 / #569 が担当する。知らない版は拒否する。
- 実行記録（`review_run`）は v2 だけで、v1 の形は無い。`runs/` の無い既存のレビューディレクトリも、そのまま読める。
- 記入例を検査する常設のテストは無い。記入例（`<!-- review-findings:example <kind> -->` の直後の JSON）を変えたら、その JSON をファイルに書き出し、`uv run --no-project python .agents/scripts/review_findings.py validate <file>` で検査する。`fix_check` の例は、`statement_candidates.file` の名前で `statement_candidates` の例を同じディレクトリに置いてから検査する。
