from pathlib import Path

import pytest

from tools.test_support.issue_queue import QueueEnv, make_queue_env


@pytest.fixture
def queue_env(tmp_path: Path) -> QueueEnv:
    return make_queue_env(tmp_path)
