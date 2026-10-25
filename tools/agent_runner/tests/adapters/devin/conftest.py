"""Fixtures for the devin adapter tests."""

from pathlib import Path

import pytest

from tools.agent_runner.adapters import AdapterContext
from tools.agent_runner.models import Agent, Request, Role


@pytest.fixture
def context(tmp_path: Path) -> AdapterContext:
    work_dir = tmp_path / "worktree"
    work_dir.mkdir()
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    request = Request(
        run_id="run-1",
        request_id="req-1",
        agent_id="agent-a",
        role="inspect",
        work_dir=work_dir,
        prompt="THE PROMPT",
        deadline_seconds=60.0,
        result_shape="json",
    )
    agent = Agent(
        runtime="devin",
        command=["devin"],
        model="swe-2-max",
    )
    role = Role(writes=False, deadline_seconds=60.0, result_shape="json")
    return AdapterContext(request, agent, role, work_dir, attempt / "receipt.json")
