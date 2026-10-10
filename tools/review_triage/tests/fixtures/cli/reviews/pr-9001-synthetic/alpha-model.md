- 対象: PR #9001 / head `{{HEAD}}` / 2026-09-28

## Review history

| Reviewed at (UTC) | HEAD | Mode |
| ----------------- | ---- | ---- |
| 2026-09-28T01:00:00Z | `{{HEAD}}` | Initial |

## Summary

合成データのレビュー（#389 cli の試験用）。M-1・L-1・I-1 が active、I-2 が resolved。

## Critical

なし

## Medium

### M-1: `clamp()` が上限を超えた値に下限を返す【検証済み】

- 位置: `src/calc.js:13-14`
- 検出時 HEAD: `{{HEAD}}`
- 内容: `value > high` の分岐が `high` ではなく `low` を返す。
- 根拠: `src/calc.js:14` を読み、`clamp(10, 0, 5)` が 0 になることを確かめた。
- 推奨対応: 分岐で `high` を返す。

## Low

### L-1: `ratio()` が 0 件で Infinity を返す

- 位置: `src/calc.js:5`
- 検出時 HEAD: `{{HEAD}}`
- 内容: `count` が 0 のとき `total / count` が Infinity になる。
- 根拠: `src/calc.js:5` に 0 の検査が無い。
- 推奨対応: `count === 0` のとき 0 を返す。

## Info

### I-1: モジュール全体に単体テストが無い

- 位置: `src/calc.js`（ファイル全体）
- 検出時 HEAD: `{{HEAD}}`
- 内容: どの関数にも単体テストが無い。
- 根拠: リポジトリにテストのディレクトリが無い。
- 推奨対応: 関数ごとのテストを足す。

## Resolved findings

### I-2: README の例が古い

- 検出時 HEAD: `{{HEAD}}`
- 解消確認時 HEAD: `{{HEAD}}`
- 確認根拠: README の例が現行の関数名に更新された。

## Merge recommendation

**Request changes** — M-1 が残っている。
