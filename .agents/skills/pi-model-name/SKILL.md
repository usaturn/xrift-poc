---
name: pi-model-name
description: pi で現在動いている AI のモデルや provider を尋ねられたとき、現在のモデル名を表示するとき、レビューや成果物に MODEL_NAME を記録するときに使う。
---

# Pi のモデル名

実行中の provider / model は、ランタイムが bash ツールへ注入する `PI_PROVIDER` / `PI_MODEL` で確認する。保存設定・ランチャー名・セッション履歴から正式なモデル名を推測しない。

## 使う場面

- 「今のモデル名を表示して」と聞かれた
- レビューや成果物へ `MODEL_NAME` を記録する
- `PI_MODEL` が空のとき
- provider / reasoning / session を合わせて確認したい

使わない場面:

- pi 以外のランタイム（Codex / Claude / Cursor）のモデル確認
- モデルカタログの一覧や切替操作（`pi --list-models`、`--model` 起動引数、`/model` の話）
- 料金・性能比較などのモデル選定相談

## 基本のパターン

```bash
# ✅ GOOD: ランタイム提示をそのまま読む（bash ツール内で実行）
printf '%s/%s\n' "${PI_PROVIDER:-}" "${PI_MODEL:-}"
```

```bash
# ❌ BAD: 履歴を正式な MODEL_NAME の根拠にする
grep -o '"modelId":"[^"]*"' ~/.pi/agent/sessions/--*/ *.jsonl | tail -1
```

正式な `MODEL_NAME` に履歴・保存設定・ランチャー名を使わない。ランタイムの変数が無ければ履歴へフォールバックせず、停止してユーザに確認する。これは `pr-review` と同じ規則である。

## 早見表

| 変数 | ランタイム | 意味 | 備考 |
| --- | --- | --- | --- |
| `PI_PROVIDER` | pi | 現在の provider | bash / powershell ツールにのみ注入 |
| `PI_MODEL` | pi | 現在の model ID | `MODEL_NAME` に使う値 |
| `PI_SESSION_ID` | pi | 現セッション ID | ホストシェルの `!` では入らない |
| `PI_SESSION_FILE` | pi | 現セッション jsonl | ephemeral では未設定 |
| `PI_SUBAGENT_SESSION` | pi | 子エージェントのセッション jsonl | `PI_SESSION_FILE` が無いときだけ照合に使う |
| `PI_REASONING_LEVEL` | pi | reasoning 強度 | `off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` |
| `AI_AGENT=pi` / `PI_CODING_AGENT=true` | pi | pi 配下の目印 | モデル特定には使わない |

## 実装

同梱スクリプトが strict 既定で表示する。pi はランタイム変数が揃い、現在のセッションファイルがある場合に、選択値と直近の応答記録を照合する。履歴読みは情報表示の明示指定だけに使う。

親 checkout の root にいるうちにスクリプトの絶対パスを取得する。同じシェルなら制作物や worktree に移動した後も使える。

```bash
# 親 checkout の root で実行
PI_MODEL_SCRIPT="$(git rev-parse --show-toplevel)/.agents/skills/pi-model-name/scripts/print-pi-model.sh"

# 制作物の checkout に移動した後も実行できる
cd items/<name>

# 既定: provider/model を表示。pi の事実が無ければ exit 2 で停止
"$PI_MODEL_SCRIPT"

# 部分だけ要る場合
"$PI_MODEL_SCRIPT" --model
"$PI_MODEL_SCRIPT" --provider
"$PI_MODEL_SCRIPT" --json
"$PI_MODEL_SCRIPT" --session

# 自分のセッションファイルを明示して照合する場合（pi のみ）
"$PI_MODEL_SCRIPT" --session-file "$PI_SESSION_FILE" --json

# 情報表示として履歴を許す場合のみ（MODEL_NAME には使わない。pi のみ）
"$PI_MODEL_SCRIPT" --allow-session-history
```

`--allow-session-history` は stdout へ参考値を出し、stderr へ `provenance=session-history` と警告する。この経路の exit 0 は参考値を表示できたことだけを示す。`--json` での出力は `provider` / `model` / `provenance=session-history` の 3 フィールドで、ランタイム照合用の `ok` / `conflicts` / `warnings` は含まない。レビュー本文・ファイル名・`MODEL_NAME` には使わない。

ランタイム照合と明示した履歴表示では、ファイルを `--session-file`、`PI_SESSION_FILE`、`PI_SUBAGENT_SESSION` の順に選ぶ。明示するファイルは自分のセッションと確認したものに限る。cwd や更新時刻から他のセッションを推測しない。ランタイム照合では JSONL の現在の枝だけを読み、`model_change` を `selected`、直近の assistant メッセージを `observed` として比べる。`observed` がモデル切替前なら警告だけを出す。`responseModel` は参考情報で、照合条件には使わない。

値が矛盾すると exit 2 になり、通常のテキスト出力は空になる。`--json` は `ok=false` と `conflicts` を含む。照合できない自動選択ファイルは警告に留め、ランタイム変数の値を返す。明示したファイルを読めない、または別セッションと判明した場合は exit 2 になる。セッションファイルが無くても、ランタイム変数が揃えば従来どおり返す。ランタイム変数が欠けたら strict 既定では exit 2 のままで、セッションファイルから正式値を採用しない。

`--json` の `provenance` は `runtime` で、`provider` / `model` が正式値を示す。キーは（`provider` / `model` / `provenance` / `ok` / `session_file` / `selected` / `observed` / `conflicts` / `warnings`）である。`ok` は終了コード 0 と対応する。`session_file` は照合ファイルが無い場合に `null` になる。`selected` / `observed` はセッション内の証拠、`conflicts` は矛盾、`warnings` は切替前の応答や照合不可の理由を示す。

## よくある間違い

- ホストのターミナル（`!` / `!!` / 素の shell）で `env` を見て「空だから履歴で代用する」。注入対象は LLM-callable な bash / powershell だけだ。まずツール内で `printf` する。
- `~/.pi/agent/sessions` の最後の `model_change` を正式値として記録する。並行セッションや古い切替が混ざる。情報表示の参考値に留め、正式値が無ければユーザ確認で止める。
- `provider/model` を手で組み立て直す。`/`・空白を含む形式はそのまま渡す。model id は `deepseek/deepseek-v4.1-flash` のように `/` を含み得る。ファイル名化が必要な場合の置換は各スキルのヘルパーに任せる。
