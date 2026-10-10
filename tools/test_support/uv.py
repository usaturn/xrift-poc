"""実 uv をオフラインで実行するための補助。

venv の無い worktree で検証コマンド（`uv run --project tools/devenv ...`）
を実際に起動したときの挙動を、ネットワークに依存せずに再現する。

- `UV_OFFLINE=1`: ネットワーク不使用を uv 側で強制する
- `UV_CACHE_DIR`: 呼び出しごとの隔離キャッシュで環境依存を除く
- `UV_PYTHON`: interpreter 解決をテスト実行中の Python に固定し、
  managed Python の download 分岐を排除する
- `PATH`: テストを動かしている venv の `bin`（`sys.executable` の親）を除く。
  除去しないと、空の `.venv` しか無い worktree で `uv run` が spawn する
  コマンドが、漏れた venv bin から解決されてしまい「venv 無し環境」の
  失敗（C-M1 相当）を再現できない
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def offline_uv_env(*, cache_dir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """実 uv をネットワーク無しで実行するための環境変数を返す。`base` は破壊しない。"""
    env = dict(os.environ if base is None else base)
    env["UV_OFFLINE"] = "1"
    env["UV_CACHE_DIR"] = str(cache_dir)
    env["UV_PYTHON"] = sys.executable
    venv_bin = str(Path(sys.executable).parent)
    env["PATH"] = os.pathsep.join(entry for entry in env.get("PATH", "").split(os.pathsep) if entry != venv_bin)
    return env


def run_uv_offline(argv: Sequence[str], *, cwd: Path, cache_dir: Path) -> subprocess.CompletedProcess[str]:
    """`uv` に `argv` を渡して `cwd` でオフライン実行し、結果をそのまま返す。"""
    return subprocess.run(
        ["uv", *argv],
        cwd=cwd,
        env=offline_uv_env(cache_dir=cache_dir),
        check=False,
        capture_output=True,
        text=True,
    )
