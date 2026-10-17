"""Runner/process integration for DSH using real disposable repositories."""

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import environ, runner
from tools.agent_runner.models import Agent, Assignment, Config, Role, Workflow
from tools.agent_runner.process import ProcessResult
from tools.agent_runner.runner import run_workflow
from tools.test_support.repos import create_env_repo

FAKE = Path(__file__).with_name("fake_cli.py")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_env_repo(tmp_path / "workspace").root


@pytest.fixture
def ambient_tmp(tmp_path: Path) -> Path:
    path = tmp_path / "ambient-tmp"
    path.mkdir()
    return path


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, ambient_tmp: Path) -> Config:
    # An allowlisted ambient TMPDIR must not receive the runtime's temporary files.
    monkeypatch.setattr(
        environ,
        "_literal",
        {"COMMANDCODE_API_KEY": "private-test-credential", "PATH": os.defpath, "TMPDIR": str(ambient_tmp)},
    )
    return Config(
        agents={
            "a": Agent(
                runtime="dsh",
                command=[sys.executable, str(FAKE), "success"],
                model="fixture-model",
                provider="commandcode",
                env=["COMMANDCODE_API_KEY", "TMPDIR"],
            )
        },
        roles={"inspect": Role(writes=False, deadline_seconds=15.0, result_shape="json")},
        workflows={"test": Workflow(assignments={"inspect": Assignment(agents=["a"])})},
    )


@pytest.mark.parametrize(
    "mode,state,exit_code",
    [
        ("success", "success", 0),
        ("auth", "awaiting_authentication", 1),
        ("approval", "awaiting_approval", 0),
        ("nonzero", "response_failed", 1),
        ("malformed", "response_failed", 0),
        ("descendant", "success", 0),
    ],
)
def test_real_process_state_and_stdin(
    config: Config, repository: Path, tmp_path: Path, mode: str, state: str, exit_code: int
) -> None:
    config.agents["a"].command[-1] = mode
    prompt = "一行目\n二行目 ☃\n"
    result = run_workflow(config, "test", prompt, repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == state
    assert attempt.exit_code == exit_code
    assert attempt.stop_confirmed
    assert attempt.work_dir is not None
    assert attempt.request_path is not None
    capture = json.loads((attempt.request_path.parent / "dsh" / "state" / "capture.json").read_text())
    assert capture["prompt"].endswith(prompt)
    assert capture["depth"] == "1" and capture["telemetry"] == "DISABLED"
    assert Path(capture["home"]).is_relative_to(attempt.request_path.parent)
    assert Path(capture["tmpdir"]).is_relative_to(attempt.request_path.parent / "dsh")
    assert capture["parent_model"] is None
    if state == "success":
        assert isinstance(attempt.payload, dict)
        assert attempt.payload["secret"] == "[REDACTED]"
    if mode == "descendant":
        pid = int((attempt.work_dir / "child.pid").read_text())
        _assert_stopped(pid)


def test_private_session_artifacts_are_redacted(config: Config, repository: Path, tmp_path: Path) -> None:
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.request_path is not None
    session = attempt.request_path.parent / "dsh" / "state" / "session.jsonl"
    assert json.loads(session.read_text())["tool"] == "[REDACTED]"
    assert attempt.sensitive_paths == []
    assert "private-test-credential" not in attempt.model_dump_json()


def test_home_tool_caches_are_disposed_without_following_links(
    config: Config, repository: Path, tmp_path: Path
) -> None:
    config.agents["a"].command[-1] = "home-cache"
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == "success" and attempt.stop_confirmed
    assert attempt.sensitive_paths == []
    assert attempt.request_path is not None and attempt.work_dir is not None
    root = attempt.request_path.parent / "dsh"
    assert not os.path.lexists(root / "home")  # links, FIFO, secrets and caches are not retained
    assert (root / "state" / "capture.json").is_file()
    assert (attempt.work_dir / "README.md").read_text() == "# fixture xrift-poc\n"
    assert Path(sys.executable).exists()


def test_large_tmpdir_spill_is_kept_in_runtime_tree_and_redacted(
    config: Config, repository: Path, tmp_path: Path, ambient_tmp: Path
) -> None:
    config.agents["a"].command[-1] = "spill"
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == "success" and attempt.sensitive_paths == []
    assert attempt.request_path is not None
    root = attempt.request_path.parent / "dsh"
    capture = json.loads((root / "state" / "capture.json").read_text())
    spill = Path(capture["spill"])
    assert spill.is_relative_to(root / "tmp")
    content = spill.read_text()
    assert len(content) > 70000
    assert "private-test-credential" not in content and "API_KEY=[REDACTED]" in content
    assert list(ambient_tmp.iterdir()) == []


@pytest.mark.parametrize("mode", ["symlink", "fifo", "tmp-symlink", "tmp-fifo"])
def test_unsafe_runtime_files_are_reported_not_followed(
    config: Config, repository: Path, tmp_path: Path, mode: str
) -> None:
    config.agents["a"].command[-1] = mode
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == "response_failed"
    assert attempt.stop_confirmed
    assert attempt.sensitive_paths
    assert attempt.work_dir is not None and attempt.request_path is not None
    assert (attempt.work_dir / "README.md").read_text() == "# fixture xrift-poc\n"
    managed = attempt.request_path.parent / "dsh" / ("tmp" if mode.startswith("tmp-") else "state")
    assert all(path.is_relative_to(managed) for path in attempt.sensitive_paths)
    if mode.endswith("symlink"):
        assert attempt.sensitive_paths[0].is_symlink()


@pytest.mark.parametrize("mode,replaced", [("home-link", "dsh/home"), ("root-link", "dsh")])
def test_replaced_home_or_root_link_is_reported_and_target_kept(
    config: Config, repository: Path, tmp_path: Path, mode: str, replaced: str
) -> None:
    config.agents["a"].command[-1] = mode
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == "response_failed" and attempt.stop_confirmed
    assert attempt.request_path is not None and attempt.work_dir is not None
    link = attempt.request_path.parent / replaced
    assert link in attempt.sensitive_paths and link.is_symlink()
    assert (attempt.work_dir / "README.md").read_text() == "# fixture xrift-poc\n"
    assert (attempt.work_dir / ".git").exists()
    if mode == "root-link":
        assert (attempt.work_dir / "home" / "keep.txt").read_text() == "keep\n"


def test_home_removal_failure_is_reported_not_success(
    config: Config, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = shutil.rmtree

    def failing(path: Any, *, dir_fd: int | None = None) -> None:
        if Path(path).name == "home":
            raise PermissionError("injected removal failure")
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(shutil, "rmtree", failing)
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == "response_failed" and attempt.stop_confirmed
    assert attempt.request_path is not None
    home = attempt.request_path.parent / "dsh" / "home"
    assert home in attempt.sensitive_paths
    # A HOME that could not be disposed is still strictly redacted, not skipped.
    assert (home / ".netrc").read_text() == "password [REDACTED]\n"


def _assert_stopped(pid: int) -> None:
    path = Path(f"/proc/{pid}/stat")
    if path.exists():
        assert path.read_text().rsplit(")", 1)[1].split()[0] == "Z"


@pytest.mark.parametrize("interrupt", [False, True])
def test_timeout_and_cancellation_stop_descendants(
    config: Config, repository: Path, tmp_path: Path, interrupt: bool
) -> None:
    config.agents["a"].command[-1] = "hang"
    config.roles["inspect"].deadline_seconds = 2.0
    cancel = threading.Event()
    timer = threading.Timer(1.0, cancel.set)
    if interrupt:
        timer.start()
    try:
        result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run", cancel=cancel)
    finally:
        timer.cancel()
        if interrupt:
            timer.join()
    attempt = result.results[0].attempts[0]
    assert attempt.state == ("interrupted" if interrupt else "timed_out")
    assert attempt.stop_confirmed
    assert attempt.work_dir is not None
    _assert_stopped(int((attempt.work_dir / "child.pid").read_text()))
    assert attempt.request_path is not None
    root = attempt.request_path.parent / "dsh"
    session = root / "state" / "session.jsonl"
    assert "private-test-credential" not in session.read_text()
    spill = Path(json.loads((root / "state" / "capture.json").read_text())["spill"])
    assert spill.is_relative_to(root / "tmp")
    assert "private-test-credential" not in spill.read_text()
    assert not os.path.lexists(root / "home")


def test_unconfirmed_stop_reports_runtime_root(
    config: Config, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unconfirmed(*args: Any, **kwargs: Any) -> ProcessResult:
        return ProcessResult(None, 0, False, 0.0)

    monkeypatch.setattr(runner, "execute", unconfirmed)
    result = run_workflow(config, "test", "respond", repository, output_dir=tmp_path / "run")
    attempt = result.results[0].attempts[0]
    assert attempt.state == "response_failed" and not attempt.stop_confirmed
    assert attempt.request_path is not None
    assert attempt.request_path.parent / "dsh" in attempt.sensitive_paths
    # Without stop evidence a live writer may still use HOME; it is neither removed nor read.
    assert (attempt.request_path.parent / "dsh" / "home").is_dir()
