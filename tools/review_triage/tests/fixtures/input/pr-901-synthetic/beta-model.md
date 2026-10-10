- 対象: [PR #901](https://example.invalid/synthetic/pull/901) / head `bbbbbbbbbbbb` / 2026-09-18 08:30 UTC

## Review history

| reviewed_at | head commit | mode |
| --- | --- | --- |
| 2026-09-18T08:30:00Z | `bbbbbbbbbbbb` | Initial |
| 2026-09-18T09:00:00Z | not-a-head | Update |

## Summary

古い HEAD を読んだ合成レビュー。

## Medium

### M-1: 設定の既定値が文書と食い違う【要設計判断】

- 検出時 HEAD: `bbbbbbbbbbbb`
- 内容: 文書は既定値を 10 とするが、コードは 20 を使う。
- 根拠: `docs/config.rst:3` と `src/config.py:7` を比較した。
- 推奨対応: どちらに合わせるか決める。

## Low

### L-1: 一時ファイルを削除していない

- 位置: `src/tmp.py:4`
- 検出時 HEAD: HEAD を記録し忘れた
- 内容: `mkstemp()` の結果を閉じた後に削除しない。
- 根拠: `src/tmp.py:4-9` を読んだ。
- 推奨対応: `finally` で削除する。
- 備考: 同じ問題が別の関数にもある。

### L-1: 同じ番号を 2 回使った指摘【検証済み・既出】

- 位置: `src/tmp.py:20`
- 検出時 HEAD: `bbbbbbbbbbbb`
- 位置: `src/tmp.py:21`
- 内容: 番号の重複を扱う合成データ。
- 根拠: 合成データ。
- 推奨対応: なし。

### M-2: Low 節に置かれた Medium の指摘

- 位置: `src/tmp.py:30`
- 検出時 HEAD: `bbbbbbbbbbbb`
- 内容: code の英字と節が食い違う合成データ。
- 根拠: 合成データ。
- 推奨対応: なし。

## Pending previous findings

### I-3: 前回の指摘を今回は再検証していない

- 位置: `src/old.py:1`
- 検出時 HEAD: `999999999999`
- 内容: 前回 HEAD の指摘を持ち越した。
- 根拠: 合成データ。
- 推奨対応: 次回再検証する。

## Merge recommendation

**Comment**
