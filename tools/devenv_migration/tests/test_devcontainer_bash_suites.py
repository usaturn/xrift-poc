"""`.devcontainer/test-*.bash` を verify-devenv.sh の pytest から実行する。

bash のテストは、シェル設定・ランチャー・インストーラの一部を、一時 HOME と fake の
コマンドで実際に動かして確かめる。手動で実行するだけでは回帰に気づけないため、
収集対象に入れる。実環境の Pi に問い合わせる live check はここでは無効にする。

Dev Container にある bwrap・zsh・`script`（util-linux）・rg を前提にする。これらが無い
環境では飛ばさずに失敗する。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITES = sorted(path.name for path in (REPO_ROOT / ".devcontainer").glob("test-*.bash"))
LIVE_CHECKS = frozenset({"PI_CODEX_LIVE_CHECK", "PI_XAI_LIVE_CHECK"})


def test_bash_suites_are_found() -> None:
    # 0 件だと下の parametrize が skip になり、何も実行せずに通ってしまう。
    assert SUITES, "no .devcontainer/test-*.bash found"


@pytest.mark.parametrize("suite", SUITES)
def test_bash_suite_passes(suite: str, tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key not in LIVE_CHECKS}
    # 各スイートが mktemp -d で作る作業領域を、pytest の一時ディレクトリの下に置く。
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / ".devcontainer" / suite)],
        cwd=REPO_ROOT,
        env=env,
        # script -qec を使うスイートが、呼び出し元の端末の状態に左右されないようにする。
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
