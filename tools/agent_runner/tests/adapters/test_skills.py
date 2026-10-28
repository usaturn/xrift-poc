"""Successful-read provenance must not block on a replaced non-regular file."""

import json
import os
import subprocess
import sys
from pathlib import Path

# 子の起動と import（pydantic を含む）は、負荷の高いホストで数秒かかる（#215 では最大 4.2 秒）。
# 止まったかどうかは子の中で呼び出しだけに期限を掛けて判定し、外側の期限は起動の遅れの安全網にする。
HANG_SECONDS = 10
STARTUP_SECONDS = 120
CHILD = """
import faulthandler, json, sys
from tools.agent_runner.adapters.skills import verified_versions

# 期限を過ぎたら止まった場所のスタックを stderr に出し、終了コード 1 で終わる
faulthandler.dump_traceback_later(int(sys.argv[2]), exit=True)
versions = verified_versions([(sys.argv[1], "observed skill")])
faulthandler.cancel_dump_traceback_later()
print(json.dumps(versions))
"""


def test_replaced_skill_fifo_is_unknown_without_blocking(tmp_path: Path) -> None:
    """読んだ後に FIFO へ置き換えられた SKILL.md を開き、収集が期限なく止まる（runner の期限は収集に効かない）。"""
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("observed skill", encoding="utf-8")
    # A task can successfully read the regular file and then replace it with a FIFO.
    skill.unlink()
    os.mkfifo(skill)
    process = subprocess.run(
        [sys.executable, "-c", CHILD, str(skill), str(HANG_SECONDS)],
        capture_output=True,
        text=True,
        check=False,
        timeout=STARTUP_SECONDS,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {}
