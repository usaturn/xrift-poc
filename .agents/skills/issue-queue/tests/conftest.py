from pathlib import Path

import pytest

from tools.test_support.issue_queue import QueueEnv, make_queue_env


@pytest.fixture
def queue_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> QueueEnv:
    # 実端末で動くpytestも、Herdrの無いCIと同じ入力からfakeを作る。
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    return make_queue_env(tmp_path)
