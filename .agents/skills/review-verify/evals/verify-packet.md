# 確認のパケット（架空の PR #900）

行動確認のための架空の PR である。GitHub にもリポジトリにも存在しない。下のファイルの内容が、PR の head（`9000000000000000000000000000000000000abc`、先頭 12 文字 `900000000000`）のコミット済みの内容であり、base（`8000000000000000000000000000000000000abc`）には `checker.py` と `finalize.py` が無い。再現は、一時ディレクトリにこれらのファイルを書き出して行う（隔離した worktree の代わり）。macOS の環境は無い。

## PR の内容

PR の本文の checkbox を数えて、loop の finalize を止めるかを決める。

### `checker.py`

```python
import re

# 箇条書きの checkbox。インラインのリンク（- [x](url)）は checkbox ではないので除く
CHECKBOX = re.compile(r"^- \[( |x|X)\](?!\()", re.MULTILINE)


def count_open(body: str) -> int:
    """未完了の checkbox の数。"""
    return sum(1 for match in CHECKBOX.finditer(body) if match.group(1) == " ")


def count_done(body: str) -> int:
    """完了した checkbox の数。x と X のどちらも完了として数える。"""
    return sum(1 for match in CHECKBOX.finditer(body) if match.group(1) in ("x", "X"))
```

### `finalize.py`

```python
from checker import count_open


def should_stop(body: str, labels: set[str]) -> bool:
    """strict ラベルがあり、未完了の checkbox が残るときだけ止まる。"""
    return "strict" in labels and count_open(body) > 0
```

### `README.md`（12 行目）

```markdown
finalize は、PR の本文に未完了の checkbox が 1 件でもあれば止まる。
```

### `docs/interfaces.rst`（40 行目）

```rst
``should_stop`` は、``strict`` ラベルがあり、未完了の checkbox が残るときだけ真を返す。
```

### `.agents/skills/loop/SKILL.md`（30 行目）

```markdown
- 本文の未完了の checkbox は、strict ラベルがあるときだけ finalize を止める。
```

## レビュアーの候補

### レビュアー A（`claude-opus-5-5.md`、候補一覧の JSON あり、HEAD `900000000000`）

- **M-1**（Medium、検証済み）: リンクの箇条書きを完了済みの checkbox と数える。位置 `checker.py:4`。再現の入力は `- [x](https://example.com)`。性質「リンクの箇条書きは checkbox に数えない」。`variants` は空。
- **M-2**（Medium、検証済み）: strict ラベルが無いと、未完了の checkbox があっても finalize が進む。位置 `finalize.py:5`。PR が停止条件を限定した。`statement_sites`: `docs/interfaces.rst:40`（一致）。
- 判断しなかったもの（`coverage.declined`）: `README.md` の全文（時間切れ）。

### レビュアー B（`gpt-6.1-sol.md`、Markdown だけ、HEAD `900000000000`）

- **L-1**（Low、確認推奨）: `- [x](url)` のようなリンクの行を checkbox として数える疑い。位置 `checker.py:4`。
- **H-1**（High、確認推奨）: macOS の大文字小文字を区別しないファイルシステムで、`checker.py` と `Checker.py` が衝突し、import が別のファイルを読む。位置 `finalize.py:1`。再現は実行しなかった（macOS が無い）。
- **M-3**（Medium、検証済み）: `count_done` が大文字の `[X]` を数えない。位置 `checker.py:14`。

## 修正の確認の変形（`mode: fix_check`）

サイクルの確認の後、planner が M-1 と L-1 を Fix にして、次の修正をローカルで commit した（`to` = `9100000000000000000000000000000000000abc`、`from` = PR head）。push はしていない。

```diff
-# 箇条書きの checkbox。インラインのリンク（- [x](url)）は checkbox ではないので除く
-CHECKBOX = re.compile(r"^- \[( |x|X)\](?!\()", re.MULTILINE)
+# 箇条書きの checkbox。リンク（- [x](url)・- [x][ref]）は checkbox ではないので除く。
+# count_done にも同じ規則を当てた
+CHECKBOX = re.compile(r"^- \[( |x|X)\](?![\(\[])", re.MULTILINE)
```

`README.md` と `.agents/skills/loop/SKILL.md` は変えていない。
