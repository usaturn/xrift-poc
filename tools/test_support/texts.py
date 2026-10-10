"""レビュー指摘とその理由の、実環境の形をした自由記述サンプル。

`ReviewFinding.content` や planner の `reason` は、改行・タブ・CRLF・
非 ASCII 文字を含む複数行の文章になりうる。識別子やパスとして解釈
できる形ではない（``str.isidentifier()`` が False）。
"""

from typing import Final

FINDING_CONTENT: Final[str] = (
    "owned worktree の検証で `uv run --no-sync pytest` が失敗する。\r\n"
    "venv の無いディレクトリでは uv が空の `.venv` を作ったあと、\r\n"
    "\t- `Failed to spawn: pytest`（exit 2）で終わる\n"
    "\t- controller 側は `tools/devenv/.venv` 済みなので同じ argv が通る\n"
    "再現: `git worktree add` の結果を cwd にして同じコマンドを実行する。\n"
)

FINDING_REASON: Final[str] = (
    "実環境と同じ形の入力で起きる失敗のため採用する。\r\n"
    "\t根拠: `uv run` の spawn 失敗はテストが常に uv を成功で返していたことに由来し、\n"
    "\tFakeRunner 前提の fixture では検出できない。\n"
)
