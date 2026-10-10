"""Successful-read provenance must not block on a replaced non-regular file."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_replaced_skill_fifo_is_unknown_without_blocking(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("observed skill", encoding="utf-8")
    # A task can successfully read the regular file and then replace it with a FIFO.
    skill.unlink()
    os.mkfifo(skill)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; from tools.agent_runner.adapters.skills import verified_versions; "
            "print(json.dumps(verified_versions([(sys.argv[1], 'observed skill')])) )",
            str(skill),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
    )
    assert json.loads(process.stdout) == {}
