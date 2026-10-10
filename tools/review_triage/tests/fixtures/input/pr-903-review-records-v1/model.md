- 対象: PR #999 / head `aaaaaaaaaaaa` / 2026-10-03

## Unverified candidates

### U-1: 絶対パスの検証 argv が別の checkout を検証する可能性

- 位置: `tools/example.py:10`
- 検出時 HEAD: `aaaaaaaaaaaa`
- 具体的な疑い: 検証対象の root が要求と異なる可能性がある。
- 確認済みの根拠: argv が絶対パスを受け付ける。
- 不足している根拠: 通常の利用経路での再現。
- 次の確認方法: 隔離 worktree で対象 root を確かめる。

## Decided findings

### M-2: 関連検証が worktree のコードを、呼び出し元の資格情報で実行する

- 判断: `Grok-4.7#M-2@25e76f6c513f`（対応しない。1 巡目、planner claude-opus-5-5）
- 位置: `tools/herdr_pr_review/loop_git.py:705`
- 検出時 HEAD: `25e76f6c513f`
- 新しい根拠: なし（見直す条件: 通常の利用経路での実害を示す再現が出たとき、または利用者が指定したとき）
