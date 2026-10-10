- 対象: PR #9001 / head `{{HEAD}}` / 2026-09-28

## Review history

| Reviewed at (UTC) | HEAD | Mode |
| ----------------- | ---- | ---- |
| 2026-09-28T02:00:00Z | `{{HEAD}}` | Initial |

## Summary

合成データのレビュー（#389 cli の試験用）。M-1 は alpha-model の M-1 と同じ問題を指す。

## Critical

なし

## Medium

### M-1: `clamp()` の上限の分岐が誤っている

- 位置: `src/calc.js:13`、`calc.js:14`（`clamp()` の上限の分岐）
- 検出時 HEAD: `{{HEAD}}`
- 内容: 上限を超えた値に `low` を返している。
- 根拠: `src/calc.js:14` の `return low;` を確認した。
- 推奨対応: `return high;` にする。

## Low

### L-1: `parsePort()` が 0 と数値でない文字列を受け付ける

- 位置: `src/calc.js:21`、`:20`
- 検出時 HEAD: `{{HEAD}}`
- 内容: `port < 0` の検査は 0 を通し、`Number.parseInt` の `NaN` も比較で弾かれない。
- 根拠: `src/calc.js:21` の条件式を読んだ。
- 推奨対応: `Number.isInteger(port) && port >= 1` を検査する。

## Info

### I-1: 設定例に鍵の見出しが残っている

- 位置: `src/util.js:4`
- 検出時 HEAD: `{{HEAD}}`
- 内容: 設定例のコメントに `-----BEGIN TEST PRIVATE KEY-----` の行が残っている。
- 根拠: 合成データ。秘密の検出器が state を止めることを確かめる。
- 推奨対応: 行を削除する。

## Merge recommendation

**Request changes** — M-1 と L-1 が残っている。
