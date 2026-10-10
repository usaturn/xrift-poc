"""End-to-end `run_workflow` with the real grok-build adapter and a fake `grok` CLI."""

import subprocess
import sys
from pathlib import Path

import pytest

from tools.agent_runner.models import Agent, Assignment, Config, Role, Workflow
from tools.agent_runner.runner import run_workflow
from tools.agent_runner.tests.adapters.grok_build.fake_cli import SESSION

_FAKE = str(Path(__file__).with_name("fake_cli.py"))


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True, capture_output=True)
    return repo


def _config(args: list[str] | None = None) -> Config:
    return Config(
        agents={
            "grok-agent": Agent(
                runtime="grok-build",
                command=[sys.executable, _FAKE],
                model="grok-4.7",
                args=args or [],
                env=["PATH", "HOME"],
            )
        },
        roles={"inspect": Role(skills=[], writes=False, deadline_seconds=30.0, result_shape="json")},
        workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["grok-agent"])})},
    )


def test_success_with_real_adapter_and_fake_cli(repository: Path, tmp_path: Path) -> None:
    batch = run_workflow(_config(), "audit", "summarize", repository, output_dir=tmp_path / "run")
    assert batch.state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.exit_code == 0 and attempt.stop_confirmed
    assert attempt.payload == {"answer": 42}
    assert attempt.identity.session_id == SESSION
    assert attempt.identity.model == "fake-grok-model"


def test_authentication_failure_maps_to_awaiting_authentication(repository: Path, tmp_path: Path) -> None:
    batch = run_workflow(_config(["--fail-auth"]), "audit", "prompt", repository, output_dir=tmp_path / "run")
    assert batch.state == "failed"
    assert batch.results[0].state == "awaiting_authentication"


def test_unparseable_output_maps_to_response_failed(repository: Path, tmp_path: Path) -> None:
    batch = run_workflow(_config(["--fail-bad"]), "audit", "prompt", repository, output_dir=tmp_path / "run")
    assert batch.state == "failed"
    assert batch.results[0].state == "response_failed"
