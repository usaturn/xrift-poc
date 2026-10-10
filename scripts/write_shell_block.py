#!/usr/bin/env python3
"""~/.zshrc に xrift-poc のシェル設定を管理ブロックとして書き込む（usaturn/xrift-poc#1）。

``# >>> xrift-poc shell setup >>>`` と ``# <<< xrift-poc shell setup <<<`` で囲んだ範囲を
``--source`` の内容で置き換える。ブロックが無ければ末尾に追加する。ブロックの外は変えない。
目印の数や順序が合わないとき、``--source`` が目印を含むときは、書き換えずに終了コード 1 で止まる。
``--target`` がシンボリックリンクなら、リンク先のファイルを書き換える。

使い方: write_shell_block.py --source .devcontainer/zshrc.txt --target ~/.zshrc
標準ライブラリだけで書く。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

BLOCK_BEGIN = "# >>> xrift-poc shell setup >>>"
BLOCK_END = "# <<< xrift-poc shell setup <<<"


class ShellBlockError(Exception):
    """管理ブロックを安全に書き込めないときに送出する。"""


def render(current: str, body: str) -> str:
    """``current`` の管理ブロックを ``body`` に置き換えた全文を返す。"""
    if any(line.rstrip("\r") in (BLOCK_BEGIN, BLOCK_END) for line in body.splitlines()):
        raise ShellBlockError("the source must not contain the block markers")
    content = body if not body or body.endswith("\n") else body + "\n"
    block = f"{BLOCK_BEGIN}\n{content}{BLOCK_END}\n"
    lines = current.splitlines(keepends=True)
    begins = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == BLOCK_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == BLOCK_END]
    if not begins and not ends:
        prefix = current if not current or current.endswith("\n") else current + "\n"
        return prefix + block
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise ShellBlockError("the block markers are broken; fix them by hand and run again")
    return "".join(lines[: begins[0]]) + block + "".join(lines[ends[0] + 1 :])


def write(target: Path, text: str) -> None:
    real = Path(os.path.realpath(target))
    mode = real.stat().st_mode & 0o777 if real.exists() else 0o644
    real.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, real)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="write_shell_block.py", description="Write the xrift-poc shell setup block.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        body = args.source.read_text(encoding="utf-8")
        real = Path(os.path.realpath(args.target))
        current = real.read_text(encoding="utf-8") if real.exists() else ""
        write(args.target, render(current, body))
    except (ShellBlockError, OSError, UnicodeError) as error:
        print(f"write_shell_block: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
