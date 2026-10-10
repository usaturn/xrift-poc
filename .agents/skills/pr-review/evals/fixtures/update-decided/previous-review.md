- 対象: PR #7 / head {{H1:12}} / 2026-06-01

## Review history

| Reviewed at (UTC)    | HEAD             | Mode    |
| -------------------- | ---------------- | ------- |
| 2026-06-01T00:10:00Z | `{{H1:12}}` | Initial |

## Summary

計画の JSON を読み込む `load_plan` と、ログを追記する `append_log` を足す PR をレビューした。worktree の外を指す変更対象を計画の段階で受け付け、適用が途中で止まることを、一時ディレクトリで実行して確かめた。

| Severity | 件数 |
| -------- | ---- |
| Critical | 0    |
| High     | 0    |
| Medium   | 1    |
| Low      | 1    |
| Info     | 0    |

## Medium

### M-1: load_plan が worktree の外を指す変更対象を受け付ける【検証済み】

- 位置: `syncer/plan.py:9`
- 検出時 HEAD: `{{H1:12}}`
- 内容: 計画の `target` が `../outside/x` のように worktree の外を指しても、`load_plan` は受け付ける。`apply_changes` はその変更対象で `ValueError` を出して止まるが、それまでに書いたファイルは残る
- 根拠: `load_plan` は `target` を検査しない。一時ディレクトリで、1 件目が `docs/a.md`、2 件目が `../outside/x` の計画を `load_plan` で読み、`apply_changes` に渡すと、`docs/a.md` を書いた後に `ValueError: target escapes the worktree: ../outside/x` で止まった
- 推奨対応: 計画の段階で worktree の外を指す変更対象を拒否する（`load_plan` のテストを足す）

## Low

### L-1: append_log がログを書いた後に fsync しない【検証済み】

- 位置: `syncer/log.py:8`
- 検出時 HEAD: `{{H1:12}}`
- 内容: プロセスが異常終了すると、最後に追記したログの行が失われうる
- 根拠: `append_log` は `write` の後に `flush`・`os.fsync` を呼ばない
- 推奨対応: `stream.flush()` と `os.fsync(stream.fileno())` を呼ぶ

## Review coverage

- 読んだ範囲: PR の差分のすべて（`syncer/plan.py`・`syncer/log.py`・`tests/test_plan.py`）と、呼び出し先の `syncer/apply.py`
- 読めなかった範囲: なし
- 実行検証: 一時ディレクトリで `load_plan` と `apply_changes` に 2 件の計画を渡した
- 未実施の検証: なし
- 委譲: 委譲なし

## Unverified candidates

なし

## Test suggestions

1. M-1: worktree の外を指す変更対象を `load_plan` が拒否するテスト

## Review timing

- 開始 2026-06-01T00:00:00Z、全体予算 45 分、調査期限 40 分、draft 時点の経過 10 分、委譲なし、超過なし

## Merge recommendation

Approve — 【検証済み】の Critical / High は無く、残る指摘は Medium 以下である
