"""End-to-end `run_workflow` with the real codex adapter and a fake `codex` CLI."""

import subprocess
import sys
from pathlib import Path

import pytest

from tools.agent_runner import environ
from tools.agent_runner.models import Agent, Assignment, Config, Role, Workflow
from tools.agent_runner.runner import run_workflow
from tools.agent_runner.tests.adapters.codex.fake_cli import SESSION

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
            "codex-agent": Agent(
                runtime="codex",
                command=[sys.executable, _FAKE],
                model="gpt-6.1-sol",
                args=args or [],
                env=["PATH", "HOME", "CODEX_HOME"],
            )
        },
        roles={"inspect": Role(skills=[], writes=False, deadline_seconds=30.0, result_shape="json")},
        workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["codex-agent"])})},
    )


def test_success_with_real_adapter_and_fake_cli(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"CODEX_HOME": str(codex_home)})
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    batch = run_workflow(_config(), "audit", "summarize", repository, output_dir=tmp_path / "run")
    assert batch.state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.exit_code == 0 and attempt.stop_confirmed
    assert attempt.payload == {"answer": 42}
    assert attempt.identity.session_id == SESSION
    # The fake CLI wrote its own session rollout into the runtime home it inherited.
    rollouts = list((codex_home / "sessions").rglob(f"rollout-*-{SESSION}.jsonl"))
    assert len(rollouts) == 1 and rollouts[0].is_file()
    assert attempt.identity.model == "gpt-6.1-sol"
    assert attempt.identity.provider == "openai"
    assert attempt.identity.effort == "xhigh"
    assert attempt.identity.cli_version == "0.160.1"


def test_authentication_failure_maps_to_awaiting_authentication(repository: Path, tmp_path: Path) -> None:
    batch = run_workflow(_config(["--fail-auth"]), "audit", "prompt", repository, output_dir=tmp_path / "run")
    assert batch.state == "failed"
    assert batch.results[0].state == "awaiting_authentication"


def test_event_error_maps_to_response_failed(repository: Path, tmp_path: Path) -> None:
    batch = run_workflow(_config(["--fail-bad"]), "audit", "prompt", repository, output_dir=tmp_path / "run")
    assert batch.state == "failed"
    assert batch.results[0].state == "response_failed"
