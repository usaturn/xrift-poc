"""`.devcontainer/test-*.bash` を verify-devenv.sh の pytest から実行する。

bash のテストは、シェル設定・ランチャー・インストーラの一部を、一時 HOME と fake の
コマンドで実際に動かして確かめる。手動で実行するだけでは回帰に気づけないため、
収集対象に入れる。

Dev Container にある bwrap・zsh・rg を前提にする。これらが無い環境では飛ばさずに失敗する。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITES = sorted(path.name for path in (REPO_ROOT / ".devcontainer").glob("test-*.bash"))


def test_bash_suites_are_found() -> None:
    """このテストが無いと、glob が 1 件も拾わないとき下の parametrize が skip になり、
    bash のスイートを 1 件も実行しないまま CI が通る。"""
    assert SUITES, "no .devcontainer/test-*.bash found"


@pytest.mark.parametrize("suite", SUITES)
def test_bash_suite_passes(suite: str, tmp_path: Path) -> None:
    """このテストが無いと、各スイートが守る挙動（tier 設定の重ね方、資格情報の受け渡し、
    利用者の設定の保持、herdr のセッションの修復）が黙って壊れても CI で気づけない。"""
    env = dict(os.environ)
    # 各スイートが mktemp -d で作る作業領域を、pytest の一時ディレクトリの下に置く。
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / ".devcontainer" / suite)],
        cwd=REPO_ROOT,
        env=env,
        # スイートが呼び出し元の端末の状態に左右されないようにする。
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"{suite} exited with {result.returncode}\n"
        f"stdout (tail):\n{result.stdout[-4000:]}\nstderr (tail):\n{result.stderr[-4000:]}"
    )
