"""実 uv をオフラインで実行するための補助。

venv の無い worktree で検証コマンド（`uv run --project tools/devenv ...`）
を実際に起動したときの挙動を、ネットワークに依存せずに再現する。

- `UV_OFFLINE=1`: ネットワーク不使用を uv 側で強制する
- `UV_CACHE_DIR`: 呼び出しごとの隔離キャッシュで環境依存を除く
- `UV_PYTHON`: interpreter 解決をテスト実行中の Python に固定し、
  managed Python の download 分岐を排除する
- `PATH`: venv の `bin` を除く。対象は、テストを動かしている venv の `bin`
  （`sys.executable` の親）と、親ディレクトリに `pyvenv.cfg` がある絶対パスの
  エントリ（外側の `uv run --project <別の checkout>` が子の `PATH` に足す
  別の venv の `bin` など）。除去しないと、空の `.venv` しか無い worktree で
  `uv run` が spawn するコマンドが、漏れた venv bin から解決されてしまい
  「venv 無し環境」の失敗（C-M1 相当）を再現できない。相対パスと空の
  エントリは、子の cwd で指す先が変わるため判定せずに残す
- `UV_PROJECT_ENVIRONMENT`: 外す。外側の実行環境が別の venv をここに設定する場合がある。
  残っていると、uv は worktree の `.venv` の代わりにその venv を使い、venv の無い
  worktree での失敗（C-M1 相当）を再現できない（#81）
- `uv` 自体は、絞る前の呼び出し元の `PATH` で絶対パスに解決してから起動する。
  `uv` が venv の `bin` にしか無くても、上の除去で見失わない
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def _is_venv_bin(entry: str, running_venv_bin: str) -> bool:
    """`PATH` のエントリ `entry` が venv の `bin` なら真を返す。"""
    if entry == running_venv_bin:
        return True
    return os.path.isabs(entry) and (Path(entry).parent / "pyvenv.cfg").is_file()


def offline_uv_env(*, cache_dir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """実 uv をネットワーク無しで実行するための環境変数を返す。`base` は破壊しない。"""
    env = dict(os.environ if base is None else base)
    env["UV_OFFLINE"] = "1"
    env["UV_CACHE_DIR"] = str(cache_dir)
    env["UV_PYTHON"] = sys.executable
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    running_venv_bin = str(Path(sys.executable).parent)
    env["PATH"] = os.pathsep.join(
        entry for entry in env.get("PATH", "").split(os.pathsep) if not _is_venv_bin(entry, running_venv_bin)
    )
    return env


def run_uv_offline(argv: Sequence[str], *, cwd: Path, cache_dir: Path) -> subprocess.CompletedProcess[str]:
    """`uv` に `argv` を渡して `cwd` でオフライン実行し、結果をそのまま返す。

    `uv` は呼び出し元の `PATH` で解決する。見つからなければ `FileNotFoundError` を送出する。
    """
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv is not on PATH")
    return subprocess.run(
        [uv, *argv],
        cwd=cwd,
        env=offline_uv_env(cache_dir=cache_dir),
        check=False,
        capture_output=True,
        text=True,
    )
