"""Outcomes of the after_run commands that the CLI and the PR review loop start after a run."""

import sys
import threading
from pathlib import Path

from tools.agent_runner import hooks


def test_failed_after_run_is_never_reported_as_success(tmp_path: Path) -> None:
    """失敗した後処理（attach-runner）を success と報告すると、loop が成功を見て黙って進む。"""
    reports = hooks.run_after_run(
        [[sys.executable, "-c", "raise SystemExit(3)"]],
        result_path=tmp_path / "result.json",
        cwd=tmp_path,
        cancel=threading.Event(),
    )
    assert reports[0]["stop_confirmed"] is True
    assert reports[0]["state"] != "success"
