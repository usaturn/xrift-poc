"""Workflow behavior against real disposable Git checkouts and fake Python CLIs."""

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tools.agent_runner import environ, runner, workspace
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.config import ConfigError
from tools.agent_runner.models import Agent, Assignment, Config, Identity, Receipt, Role, State, Workflow
from tools.agent_runner.process import ProcessResult
from tools.agent_runner.runner import RunnerError, run_workflow
from tools.agent_runner.tests.test_process import alive

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")

# Removing receipt validation, cancellation checks, environment filtering, or the
# workflow semaphore must break these consumer-visible assertions.
_SCRIPT = """
import json, os, pathlib, sys, time
incoming = json.loads(sys.stdin.read())
request = incoming['request']
options = incoming['options']
if options.get('outside'):
    pathlib.Path(options['outside']).write_text('changed again')
if options.get('marker'):
    pathlib.Path(options['marker']).write_text(str(time.monotonic()))
token = os.environ.get('SERVICE_TOKEN', 'no-token')
_export_file = pathlib.Path(sys.argv[1]).parent.joinpath('export.json')
if options.get('export_link'):
    _export_file.symlink_to(options['export_link'])
elif options.get('export_fifo'):
    os.mkfifo(_export_file)
elif options.get('devin_export') is not None:
    _export_file.write_text(options['devin_export'].replace('$TOKEN', token), encoding='utf-8')
time.sleep(options.get('sleep', 0))
print(os.environ.get('SERVICE_TOKEN', 'no-token'))
print(os.environ.get('Extra_Password', 'no-password'), file=sys.stderr)
payload = options.get('payload', {'prompt': request['prompt'], 'depth': os.environ.get('AGENT_RUNNER_DEPTH'),
    'ambient': os.environ.get('UNCONFIGURED_SECRET'), 'git': os.environ.get('GIT_DIR'),
    'skills': incoming['skills'], 'sources': incoming['sources']})
receipt = {key: request[key] for key in ('schema_version', 'run_id', 'request_id', 'agent_id', 'role')}
receipt.update(status=options.get('status', 'success'), payload=payload)
receipt.update(options.get('receipt_changes', {}))
if not options.get('missing'):
    pathlib.Path(sys.argv[1]).write_text(options.get('raw', json.dumps(receipt)))
sys.exit(options.get('exit', 0))
"""


class FakeAdapter:
    capabilities = Capabilities()

    def __init__(
        self,
        options: dict[str, dict[str, object]] | None = None,
        *,
        env: dict[str, str] | None = None,
        outcome_state: State | None = None,
        identity: Identity | None = None,
    ) -> None:
        self.options = options or {}
        self.env = env or {}
        self.outcome_state: State | None = outcome_state
        self.identity = identity or Identity()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        return LaunchSpec(
            argv=(*context.agent.command, str(context.receipt_path)),
            stdin=json.dumps(
                {
                    "request": context.request.model_dump(mode="json"),
                    "options": self.options.get(context.request.agent_id, {}),
                    "skills": context.role.skills,
                    "sources": context.agent.skill_sources,
                }
            ).encode(),
            env=self.env,
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        receipt = (
            Receipt.model_validate_json(context.receipt_path.read_bytes()) if context.receipt_path.exists() else None
        )
        return AdapterOutcome(receipt, self.identity, self.outcome_state)


@contextlib.contextmanager
def set_when(event: threading.Event, ready: Callable[[], bool]) -> Generator[None]:
    """Set event from a watcher thread once ready() holds inside the with block.

    Interruption tests key cancellation to an observed point of the run, not to
    a timer started before run_workflow: preparation before the slot and inside
    the role deadline both stretch on a loaded host (#694). The event is never
    set if ready() does not hold before the block ends.
    """
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.01):
            if ready():
                event.set()
                return

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join()


def written(paths: Iterable[Path]) -> int:
    """Count files whose content has been written (a single small write)."""
    return sum(path.stat().st_size > 0 for path in paths)


@contextlib.contextmanager
def exit_times(pids: Callable[[], Iterable[int]]) -> Generator[dict[int, float]]:
    """Record when each pid from pids() is first seen not alive inside the with block.

    A stop is observed from outside rather than recorded by the stopped process:
    work a process does after SIGTERM must finish within the runner's fixed grace
    before SIGKILL, which a loaded host stretches (#694). A last sweep after the
    block records processes that stopped just before it ended.
    """
    times: dict[int, float] = {}
    finished = threading.Event()

    def watch() -> None:
        while True:
            done = finished.wait(0.01)
            for pid in pids():
                if pid not in times and not alive(pid):
                    times[pid] = time.monotonic()
            if done:
                return

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        yield times
    finally:
        finished.set()
        watcher.join()


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


def configuration(
    agents: list[str] | None = None,
    fallback: list[str] | None = None,
    *,
    deadline: float = 5.0,
    shape: str = "json",
) -> Config:
    return Config(
        agents={
            name: Agent(runtime="pi", command=[sys.executable, "-c", _SCRIPT], model=name, skill_sources=["source"])
            for name in ("a", "b", "c")
        },
        roles={
            "inspect": Role.model_validate(
                {"writes": False, "skills": ["review"], "deadline_seconds": deadline, "result_shape": shape}
            )
        },
        workflows={
            "audit": Workflow(assignments={"inspect": Assignment(agents=agents or ["a"], fallback=fallback or [])})
        },
    )


def test_unavailable_registered_checkout_does_not_prevent_cli_run(repository: Path, tmp_path: Path) -> None:
    external = workspace.create_worktree(repository, "HEAD", tmp_path / "unavailable")
    gitfile = external / ".git"
    original = gitfile.read_bytes()
    gitfile.write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
    try:
        batch = run_workflow(
            configuration(),
            "audit",
            "literal prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(),
        )
        assert batch.state == "success"
        attempt = batch.results[0].attempts[0]
        assert attempt.exit_code == 0 and attempt.stop_confirmed
        assert "literal prompt" in str(attempt.payload)
        assert batch.outside_changes == [str(external)]
        assert [item.model_dump() for item in batch.checkout_audit] == [
            {
                "path": str(external),
                "kind": "unavailable_before",
                "reason": "git_unavailable",
                "locked": False,
                "prunable": False,
            }
        ]
    finally:
        gitfile.write_bytes(original)


def test_success_has_verified_receipt_paths_and_observed_identity(repository: Path, tmp_path: Path) -> None:
    prompt = 'a quote " and a backtick `\nsecond line\n'
    batch = run_workflow(
        configuration(),
        "audit",
        prompt,
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(identity=Identity(model="observed")),
    )
    assert batch.state == "success"
    result = batch.results[0]
    assert (result.requested_agent, result.actual_agent, result.role) == ("a", "a", "inspect")
    attempt = result.attempts[0]
    assert attempt.identity.model == "observed"
    assert attempt.identity.provider is None
    assert attempt.exit_code == 0 and attempt.stop_confirmed
    assert attempt.work_dir == batch.artifact_dir / result.request_id / "attempt-1" / "worktree"
    assert attempt.work_dir is not None and attempt.work_dir.exists()
    assert attempt.request_path is not None and attempt.request_path.exists()
    assert attempt.stdout_path is not None and attempt.stdout_path.exists()
    assert attempt.stderr_path is not None and attempt.stderr_path.exists()
    payload = cast(dict[str, object], attempt.payload)
    assert prompt in str(payload["prompt"])
    assert "this worktree only" in str(payload["prompt"])
    assert "not a sandbox" in str(payload["prompt"])
    assert payload["skills"] == ["review"] and payload["sources"] == ["source"]
    assert payload["depth"] == "1"
    assert batch.outside_changes == []


@pytest.mark.parametrize(
    ("options", "state"),
    [
        ({"missing": True}, "missing_result"),
        ({"raw": "{"}, "invalid_result"),
        ({"receipt_changes": {"run_id": "old"}}, "invalid_result"),
        ({"receipt_changes": {"request_id": "old"}}, "invalid_result"),
        ({"receipt_changes": {"agent_id": "old"}}, "invalid_result"),
        ({"receipt_changes": {"role": "old"}}, "invalid_result"),
        ({"receipt_changes": {"schema_version": 2}}, "invalid_result"),
        ({"receipt_changes": {"extra": True}}, "invalid_result"),
        ({"payload": {}}, "invalid_result"),
        ({"payload": []}, "invalid_result"),
        ({"payload": "text"}, "invalid_result"),
        ({"payload": None}, "invalid_result"),
        ({"exit": 7}, "response_failed"),
        ({"status": "awaiting_authentication"}, "awaiting_authentication"),
        ({"status": "awaiting_authentication", "exit": 3}, "awaiting_authentication"),
        ({"status": "awaiting_approval"}, "awaiting_approval"),
        ({"status": "awaiting_approval", "exit": 3}, "awaiting_approval"),
        ({"status": "response_failed"}, "response_failed"),
        ({"status": "response_failed", "exit": 3}, "response_failed"),
        # A CLI receipt cannot claim a state that only the runner determines.
        ({"status": "self"}, "invalid_result"),
        ({"status": "timed_out"}, "invalid_result"),
    ],
)
def test_exit_zero_is_not_a_substitute_for_valid_receipt(
    repository: Path,
    options: dict[str, object],
    state: str,
) -> None:
    batch = run_workflow(
        configuration(), "audit", "prompt", repository, adapter_loader=lambda _: FakeAdapter({"a": options})
    )
    assert batch.state == "failed"
    assert batch.results[0].state == state
    assert batch.results[0].attempts[0].identity.model is None


@pytest.mark.parametrize(
    ("payload", "state"), [("answer", "success"), (" \n ", "invalid_result"), ({"x": 1}, "invalid_result")]
)
def test_text_receipt_requires_nonblank_text(repository: Path, payload: object, state: str) -> None:
    batch = run_workflow(
        configuration(shape="text"),
        "audit",
        "prompt",
        repository,
        adapter_loader=lambda _: FakeAdapter({"a": {"payload": payload}}),
    )
    assert batch.results[0].state == state


def test_adapter_success_cannot_bypass_missing_receipt(repository: Path) -> None:
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}, outcome_state="success"),
    )
    assert batch.results[0].state == "missing_result"


def test_partial_success_and_independent_worktrees(repository: Path) -> None:
    batch = run_workflow(
        configuration(["a", "b"]),
        "audit",
        "prompt",
        repository,
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}),
    )
    assert batch.state == "partial_success"
    assert batch.assignments == {"inspect": ["a", "b"]}
    assert [result.state for result in batch.results] == ["missing_result", "success"]
    assert len({result.request_id for result in batch.results}) == 2
    assert len({result.attempts[0].work_dir for result in batch.results}) == 2


def test_only_explicit_fallback_is_tried_in_order(repository: Path) -> None:
    fake = FakeAdapter({"a": {"missing": True}, "b": {"exit": 2}})
    batch = run_workflow(
        configuration(fallback=["b", "c"]), "audit", "prompt", repository, adapter_loader=lambda _: fake
    )
    assert batch.state == "success"
    assert batch.assignments == {"inspect": ["a"]}
    assert batch.results[0].actual_agent == "c"
    assert [attempt.agent_id for attempt in batch.results[0].attempts] == ["a", "b", "c"]
    alone = run_workflow(configuration(), "audit", "prompt", repository, adapter_loader=lambda _: fake)
    assert [attempt.agent_id for attempt in alone.results[0].attempts] == ["a"]


def test_self_has_no_process_or_worktree(repository: Path) -> None:
    batch = run_workflow(configuration(["self"]), "audit", "prompt", repository)
    assert batch.state == "self"
    assert batch.assignments == {"inspect": ["self"]}
    assert batch.results[0].state == "self"
    assert not list(batch.artifact_dir.rglob("worktree"))


def test_launch_failed_is_safe(repository: Path) -> None:
    cfg = configuration()
    cfg.agents["a"].command = ["/does/not/exist"]
    failed = run_workflow(cfg, "audit", "secret prompt", repository, adapter_loader=lambda _: FakeAdapter())
    assert failed.results[0].state == "launch_failed"
    assert "secret prompt" not in failed.model_dump_json()


@pytest.mark.parametrize("deadline", [4.0, 8.0])
def test_role_deadline_covers_fallback_attempts(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline: float
) -> None:
    now = 0.0
    budgets: list[tuple[str, float]] = []

    # Replace only runner's clock: asyncio and real process teardown keep real time.
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: now))

    async def prepare(
        repository: Path,
        commit: str,
        destination: Path,
        *,
        deadline_seconds: float,
        cancel: threading.Event,
        stdout_path: Path,
        stderr_path: Path,
        secrets: tuple[str, ...] = (),
    ) -> ProcessResult:
        nonlocal now
        budgets.append(("prepare", deadline_seconds))
        destination.mkdir()
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        now += deadline / 8
        return ProcessResult(None, 0, True, deadline / 8)

    async def execute(
        spec: LaunchSpec,
        *,
        cwd: Path,
        env: dict[str, str],
        deadline_seconds: float,
        stdout_path: Path,
        stderr_path: Path,
        cancel: threading.Event,
        secrets: tuple[str, ...] = (),
    ) -> ProcessResult:
        nonlocal now
        agent = json.loads(spec.stdin)["request"]["agent_id"]
        budgets.append((agent, deadline_seconds))
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        # a exits without a receipt; b consumes the remaining shared budget.
        elapsed = deadline / 4 if agent == "a" else deadline_seconds
        now += elapsed
        return ProcessResult(None if agent == "a" else "timed_out", 0, True, elapsed)

    monkeypatch.setattr(runner, "prepare_worktree", prepare)
    monkeypatch.setattr(runner, "execute", execute)
    batch = run_workflow(
        configuration(fallback=["b", "c"], deadline=deadline),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "artifacts",
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}),
    )
    result = batch.results[0]
    assert result.state == "timed_out"
    assert [(attempt.agent_id, attempt.state) for attempt in result.attempts] == [
        ("a", "missing_result"),
        ("b", "timed_out"),
    ]
    assert budgets == [
        ("prepare", deadline),
        ("a", deadline * 7 / 8),
        ("prepare", deadline * 5 / 8),
        ("b", deadline / 2),
    ]
    assert batch.elapsed_seconds == deadline


@pytest.mark.parametrize("locale_source", ["default", "allowlist", "adapter"])
def test_workflow_preserves_literal_environment(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supervisor_bootstrap: Callable[[str], None],
    locale_source: str,
) -> None:
    class EnvironmentAdapter(FakeAdapter):
        def prepare(self, context: AdapterContext) -> LaunchSpec:
            return LaunchSpec(("/usr/bin/env", "-0"), b"", env=self.env)

        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            payload: dict[str, object] = {
                os.fsdecode(name): os.fsdecode(value)
                for entry in stdout_path.read_bytes().split(b"\0")
                if entry
                for name, value in [entry.split(b"=", 1)]
            }
            return AdapterOutcome(
                Receipt(
                    schema_version=context.request.schema_version,
                    run_id=context.request.run_id,
                    request_id=context.request.request_id,
                    agent_id=context.request.agent_id,
                    role=context.request.role,
                    status="success",
                    payload=payload,
                )
            )

    # The exec environment, not os.environ, is what commands may inherit.
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"LC_CTYPE": "C", "LANG": "POSIX"})
    config = configuration()
    additions = {"LC_CTYPE": "C", "EMPTY": "", "TEXT": "日本語\nkey=value"} if locale_source == "adapter" else {}
    if locale_source == "allowlist":
        config.agents["a"].env = ["LC_CTYPE"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "artifacts",
        adapter_loader=lambda _: EnvironmentAdapter(env=additions),
    )
    expected = {"PATH": os.defpath, "AGENT_RUNNER_DEPTH": "1"}
    if locale_source == "allowlist":
        expected["LC_CTYPE"] = "C"
    expected.update(additions)
    assert batch.state == "success" and batch.results[0].state == "success"
    assert batch.results[0].attempts[-1].payload == expected


def test_cancel_during_run_prevents_fallback(repository: Path, tmp_path: Path) -> None:
    cancel = threading.Event()
    started = tmp_path / "a-started"
    with set_when(cancel, lambda: written(tmp_path.glob(started.name)) == 1):
        batch = run_workflow(
            configuration(fallback=["b"]),
            "audit",
            "prompt",
            repository,
            cancel=cancel,
            adapter_loader=lambda _: FakeAdapter({"a": {"sleep": 60, "marker": str(started)}}),
        )
    assert batch.results[0].state == "interrupted"
    assert len(batch.results[0].attempts) == 1
    assert batch.results[0].attempts[0].stop_confirmed


def test_preexisting_cancel_launches_nothing(repository: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    batch = run_workflow(
        configuration(["a", "b"], ["c"]),
        "audit",
        "prompt",
        repository,
        cancel=cancel,
        adapter_loader=lambda _: FakeAdapter(),
    )
    assert all(result.state == "interrupted" for result in batch.results)
    assert not list(batch.artifact_dir.rglob("worktree"))


def test_unconfirmed_stop_blocks_success_and_fallback(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Independent stop evidence is the only patched seam: execution and artifacts remain real.
    def no_stop(_: object) -> bool:
        return False

    monkeypatch.setattr("tools.agent_runner.process._stop_confirmed", no_stop)
    batch = run_workflow(
        configuration(fallback=["b"]), "audit", "prompt", repository, adapter_loader=lambda _: FakeAdapter()
    )
    result = batch.results[0]
    assert result.state != "success"
    assert len(result.attempts) == 1
    assert not result.attempts[0].stop_confirmed
    assert result.attempts[0].work_dir is not None and result.attempts[0].work_dir.exists()


def test_concurrency_bounds_whole_assignments(repository: Path, tmp_path: Path) -> None:
    markers = {name: tmp_path / name for name in ("a", "b", "c")}
    batch = run_workflow(
        configuration(["a", "b", "c"]),
        "audit",
        "prompt",
        repository,
        concurrency=2,
        adapter_loader=lambda _: FakeAdapter(
            {name: {"sleep": 0.25, "marker": str(path)} for name, path in markers.items()}
        ),
    )
    assert batch.state == "success"
    starts = sorted(float(path.read_text()) for path in markers.values())
    assert starts[1] - starts[0] < 0.2
    assert starts[2] - starts[0] >= 0.23


def test_outside_change_to_already_dirty_file_is_warning_only(repository: Path) -> None:
    dirty = repository / "tracked.txt"
    dirty.write_text("already dirty")
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        adapter_loader=lambda _: FakeAdapter({"a": {"outside": str(dirty)}}),
    )
    assert batch.state == "success"
    assert batch.outside_changes == [str(repository)]
    assert dirty.read_text() == "changed again"


def test_only_allowlisted_environment_and_redacted_results(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "token-value")
    monkeypatch.setenv("UNCONFIGURED_SECRET", "ambient-value")
    monkeypatch.setenv("GIT_DIR", "/bogus-ambient-git")
    # The same ambient values are the exec environment commands inherit from.
    monkeypatch.setattr(
        environ,
        "_literal",
        environ.literal_environ()
        | {"SERVICE_TOKEN": "token-value", "UNCONFIGURED_SECRET": "ambient-value", "GIT_DIR": "/bogus-ambient-git"},
    )
    cfg = configuration()
    cfg.agents["a"].env = ["SERVICE_TOKEN", "GIT_DIR"]
    fake = FakeAdapter(
        {"a": {"payload": {"nested": ["token-value", {"x": "password-value"}]}}},
        env={"AGENT_RUNNER_DEPTH": "0", "Extra_Password": "password-value"},
        identity=Identity(model="token-value", skill_versions={"version": "password-value"}),
    )
    batch = run_workflow(cfg, "audit", "prompt", repository, adapter_loader=lambda _: fake)
    serialized = batch.model_dump_json()
    assert "token-value" not in serialized and "password-value" not in serialized
    assert "[REDACTED]" in serialized
    attempt = batch.results[0].attempts[0]
    assert attempt.stdout_path is not None and "token-value" not in attempt.stdout_path.read_text()
    assert attempt.stderr_path is not None and "password-value" not in attempt.stderr_path.read_text()
    assert attempt.request_path is not None and "password-value" not in attempt.request_path.read_text()
    environment = run_workflow(
        cfg, "audit", "prompt", repository, adapter_loader=lambda _: FakeAdapter(env={"AGENT_RUNNER_DEPTH": "0"})
    )
    payload = cast(dict[str, object], environment.results[0].attempts[0].payload)
    assert payload["depth"] == "1" and payload["ambient"] is None and payload["git"] is None


def test_invalid_direct_config_is_rejected_before_execution(repository: Path) -> None:
    cfg = configuration()
    cfg.workflows["audit"].assignments["inspect"].agents = ["unknown"]
    with pytest.raises(ConfigError, match="invalid_reference"):
        run_workflow(cfg, "audit", "prompt", repository)
    cfg = configuration(["a", "b"])
    cfg.agents["b"].model = "a"
    with pytest.raises(ConfigError, match="duplicate_model"):
        run_workflow(cfg, "audit", "prompt", repository)
    with pytest.raises(ConfigError, match="invalid_reference"):
        run_workflow(configuration(), "unknown", "prompt", repository)


def test_nested_invocation_and_invalid_inputs_fail_closed(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", "1")
    with pytest.raises(ConfigError, match="nested_invocation"):
        run_workflow(configuration(), "audit", "prompt", repository)
    monkeypatch.delenv("AGENT_RUNNER_DEPTH")
    with pytest.raises(RunnerError):
        run_workflow(configuration(), "audit", "prompt", repository, concurrency=0)
    with pytest.raises(RunnerError):
        run_workflow(configuration(), "audit", "prompt", repository, revision="--dangerous")


def test_existing_or_symlink_output_is_never_reused(repository: Path, tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "receipt.json"
    marker.write_text("retained")
    for path in (existing, tmp_path / "broken"):
        if path != existing:
            path.symlink_to(tmp_path / "missing")
        with pytest.raises(RunnerError):
            run_workflow(configuration(), "audit", "prompt", repository, output_dir=path)
    assert marker.read_text() == "retained"


def test_default_artifact_directory_is_unique(repository: Path) -> None:
    one = run_workflow(configuration(["self"]), "audit", "prompt", repository)
    two = run_workflow(configuration(["self"]), "audit", "prompt", repository)
    assert one.artifact_dir != two.artifact_dir
    # Tripwire: disposable artifacts go to a unique, identifiable directory under
    # /tmp (AGENTS.md placement rule). Update this together with the runner prefix.
    assert str(one.artifact_dir).startswith("/tmp/xrift-poc-agent-runner-")
    assert one.artifact_dir.is_dir() and two.artifact_dir.is_dir()


def test_invalid_mutated_config_does_not_emit_serializer_values(repository: Path) -> None:
    cfg = configuration()
    cfg.agents["a"].env = cast(list[str], "private-token-value")
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        with pytest.raises(ConfigError):
            run_workflow(cfg, "audit", "prompt", repository)
    assert not emitted


def test_invalid_observed_identity_does_not_emit_serializer_values(repository: Path) -> None:
    identity = Identity()
    identity.skill_versions = cast(dict[str, str], "private-token-value")
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        batch = run_workflow(
            configuration(), "audit", "prompt", repository, adapter_loader=lambda _: FakeAdapter(identity=identity)
        )
    assert batch.results[0].state == "invalid_result"
    assert not emitted


class MissingFileAdapter(FakeAdapter):
    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        return AdapterOutcome(Receipt.model_validate_json(context.receipt_path.read_bytes()))


def test_missing_receipt_file_is_missing_not_a_parse_failure(repository: Path) -> None:
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        adapter_loader=lambda _: MissingFileAdapter({"a": {"missing": True}}),
    )
    assert batch.results[0].state == "missing_result"


class PreexistingReceiptAdapter(FakeAdapter):
    def prepare(self, context: AdapterContext) -> LaunchSpec:
        context.receipt_path.write_text(
            Receipt(
                run_id=context.request.run_id,
                request_id=context.request.request_id,
                agent_id=context.request.agent_id,
                role=context.request.role,
                status="success",
                payload={"cached": True},
            ).model_dump_json()
        )
        return super().prepare(context)


def test_receipt_created_before_launch_is_never_reused(repository: Path) -> None:
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        adapter_loader=lambda _: PreexistingReceiptAdapter({"a": {"missing": True}}),
    )
    assert batch.results[0].state == "invalid_result"


def test_unproven_capability_is_unsupported(repository: Path) -> None:
    adapter = FakeAdapter()
    adapter.capabilities = Capabilities(noninteractive=cast(bool, "not-a-guarantee"))
    batch = run_workflow(configuration(), "audit", "prompt", repository, adapter_loader=lambda _: adapter)
    assert batch.results[0].state == "unsupported"
    assert not list(batch.artifact_dir.rglob("worktree"))


# Real checkout hooks block synchronous Git; supervising preparation must keep
# other assignments progressing, bound cancellation, and stop escaped helpers.
# Cancellation follows observed events, never a timer started before
# run_workflow, and every case runs with slow preparation before the slot and
# inside the role deadline, as on a loaded host (#694).
@pytest.mark.parametrize("interruption", ["none", "cancel", "late_cancel"])
def test_checkout_preparation_is_supervised(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    markers = tmp_path / "hook-markers"
    markers.mkdir()
    hook = repository / ".git/hooks/post-checkout"
    child_code = (
        "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"open({str(markers)!r} + '/child-' + str(os.getpid()),'w').write(str(time.monotonic())); time.sleep(60)"
    )
    # The hook records its pid; exit_times observes when the runner stops it. The
    # runner decides timed_out or interrupted before it signals, so an observed
    # stop is past the deadline.
    hook.write_text(
        f"#!{sys.executable}\n"
        "import os,subprocess,sys,time\n"
        f"open({str(markers)!r} + '/hook-' + str(os.getpid()),'w').close()\n"
        "time.sleep(0.3)\n"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(os.environ['SERVICE_TOKEN'], flush=True)\n"
        "print(os.environ['UNCONFIGURED_SECRET'], file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    hook.chmod(0o755)
    real_snapshot = runner.snapshot_checkouts

    def slow_snapshot(repository: Path) -> dict[str, str | None]:
        time.sleep(0.3)
        return real_snapshot(repository)

    monkeypatch.setattr(runner, "snapshot_checkouts", slow_snapshot)
    monkeypatch.setenv("SERVICE_TOKEN", "prep-token-value")
    monkeypatch.setenv("UNCONFIGURED_SECRET", "ambient-prep-value")
    monkeypatch.setenv("GIT_DIR", "/bogus-ambient-git")
    # Preparation helpers run under the exec environment, not os.environ.
    monkeypatch.setattr(
        environ,
        "_literal",
        environ.literal_environ()
        | {
            "SERVICE_TOKEN": "prep-token-value",
            "UNCONFIGURED_SECRET": "ambient-prep-value",
            "GIT_DIR": "/bogus-ambient-git",
        },
    )
    cancel = threading.Event()
    agent_markers = {name: tmp_path / f"agent-{name}" for name in ("a", "b")}
    try:
        with exit_times(lambda: [int(path.name.removeprefix("hook-")) for path in markers.glob("hook-*")]) as stopped:
            # cancel: once both helpers run; late_cancel: once the runner stopped both hooks.
            ready = {
                "cancel": lambda: written(markers.glob("child-*")) == 2,
                "late_cancel": lambda: len(stopped) == 2,
            }.get(interruption, lambda: False)
            with set_when(cancel, ready):
                batch = run_workflow(
                    # Deadlines far above unloaded preparation (0.2 s) plus the delays above.
                    configuration(["a", "b"], ["c"], deadline=60 if interruption == "cancel" else 3),
                    "audit",
                    "prompt",
                    repository,
                    cancel=cancel,
                    concurrency=2,
                    output_dir=tmp_path / "run",
                    adapter_loader=lambda _: FakeAdapter(
                        {name: {"marker": str(path)} for name, path in agent_markers.items()}
                    ),
                )
        expected = "interrupted" if interruption == "cancel" else "timed_out"
        assert [result.state for result in batch.results] == [expected, expected]
        assert cancel.is_set() == (interruption != "none")
        # Well below the 60 s hook, helper, and cancel-case deadline: stopping waits for none of them.
        assert batch.elapsed_seconds < 30
        starts = sorted(float(path.read_text()) for path in markers.glob("child-*"))
        stops = sorted(stopped.values())
        assert len(starts) == 2 and len(stops) == 2
        # Preparations progress together: both helpers start before either hook is stopped.
        assert starts[-1] < stops[0]
        assert not any(path.exists() for path in agent_markers.values())
        for result in batch.results:
            assert len(result.attempts) == 1
            attempt = result.attempts[0]
            assert attempt.stop_confirmed
            assert attempt.work_dir is not None and attempt.work_dir.is_dir()
            assert attempt.request_path is not None and not attempt.request_path.exists()
            assert attempt.stdout_path is not None and attempt.stdout_path.name == "worktree.stdout.log"
            assert attempt.stderr_path is not None and attempt.stderr_path.name == "worktree.stderr.log"
            for path in (attempt.stdout_path, attempt.stderr_path):
                output = path.read_text()
                assert "prep-token-value" not in output and "ambient-prep-value" not in output
            assert "[REDACTED]" in attempt.stderr_path.read_text()
        for marker in markers.glob("child-*"):
            pid = int(marker.name.removeprefix("child-"))
            record = Path(f"/proc/{pid}/stat")
            assert not record.exists() or record.read_bytes().rsplit(b")", 1)[1].split()[0] in (b"Z", b"X")
    finally:
        for marker in [*markers.glob("child-*"), *markers.glob("hook-*")]:
            try:
                os.kill(int(marker.name.split("-", 1)[1]), signal.SIGKILL)
            except ProcessLookupError:
                pass


# Cancellation at checkout completion must not replace retained preparation log
# paths with agent log paths for an agent that was never launched.
def test_cancel_at_preparation_completion_retains_log_paths(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = threading.Event()

    async def cancelled(
        repository: Path,
        commit: str,
        destination: Path,
        *,
        deadline_seconds: float,
        cancel: threading.Event,
        stdout_path: Path,
        stderr_path: Path,
        secrets: tuple[str, ...] = (),
    ) -> ProcessResult:
        result = await workspace.prepare_worktree(
            repository,
            commit,
            destination,
            deadline_seconds=deadline_seconds,
            cancel=cancel,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            secrets=secrets,
        )
        cancel.set()
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(runner, "prepare_worktree", cancelled)
    marker = tmp_path / "agent-started"
    batch = run_workflow(
        configuration(fallback=["b"]),
        "audit",
        "prompt",
        repository,
        cancel=cancel,
        adapter_loader=lambda _: FakeAdapter({"a": {"marker": str(marker)}}),
    )
    assert batch.results[0].state == "interrupted"
    assert len(batch.results[0].attempts) == 1
    attempt = batch.results[0].attempts[0]
    assert not marker.exists()
    assert attempt.stdout_path is not None and attempt.stdout_path.is_file()
    assert attempt.stderr_path is not None and attempt.stderr_path.is_file()
    assert attempt.stdout_path.name == "worktree.stdout.log"
    assert attempt.stderr_path.name == "worktree.stderr.log"


# Devin leaves an ATIF export next to the receipt. Its transcript can repeat
# inherited secret values in the final answer, history, and tool arguments,
# so the runner must redact the managed export like the receipt.
def _devin_configurable_export() -> str:
    return json.dumps(
        {
            "schema_version": "ATIF-v1.7",
            "session_id": "session-1",
            "agent": {"name": "devin", "version": "3000.11.3", "model_name": "SWE-2 Max"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "run with $TOKEN"},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "answer using $TOKEN",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "run",
                                "arguments": json.dumps({"command": "curl -H 'Authorization: Bearer $TOKEN'"}),
                            },
                        }
                    ],
                },
            ],
        }
    )


def _as_devin(config: Config) -> Config:
    for agent in config.agents.values():
        agent.runtime = "devin"
    return config


def _literal_env(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", value)
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"SERVICE_TOKEN": value})


def test_devin_retained_export_is_redacted_after_nonzero_exit(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _literal_env(monkeypatch, "synthetic-token-2345")
    config = _as_devin(configuration())
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"exit": 4, "devin_export": _devin_configurable_export()}}),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "response_failed" and attempt.exit_code == 4 and attempt.stop_confirmed
    retained = (attempt.request_path or batch.artifact_dir / "x" / "request.json").parent / "export.json"
    text = retained.read_text(encoding="utf-8")
    assert "synthetic-token-2345" not in text
    assert "[REDACTED]" in text


def test_devin_retained_export_is_redacted_after_timeout(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _literal_env(monkeypatch, "synthetic-token-3456")
    # The deadline also covers checkout and CLI startup before the export exists;
    # keep it far above their unloaded 0.2 s (#694).
    config = _as_devin(configuration(deadline=3.0))
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"sleep": 60, "devin_export": _devin_configurable_export()}}),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "timed_out" and attempt.stop_confirmed
    assert attempt.request_path is not None
    retained = attempt.request_path.parent / "export.json"
    text = retained.read_text(encoding="utf-8")
    assert "synthetic-token-3456" not in text
    assert "[REDACTED]" in text


def test_devin_retained_export_is_redacted_after_interruption(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _literal_env(monkeypatch, "synthetic-token-4567")

    async def quick_prepare(
        repository: Path,
        commit: str,
        destination: Path,
        *,
        deadline_seconds: float,
        cancel: threading.Event,
        stdout_path: Path,
        stderr_path: Path,
        secrets: tuple[str, ...] = (),
    ) -> ProcessResult:
        destination.mkdir(parents=True)
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        return ProcessResult(None, 0, True, deadline_seconds / 8)

    monkeypatch.setattr(runner, "prepare_worktree", quick_prepare)
    config = _as_devin(configuration(deadline=6.0))
    config.agents["a"].env = ["SERVICE_TOKEN"]
    cancel = threading.Event()
    with set_when(cancel, lambda: written((tmp_path / "run").glob("*/attempt-1/export.json")) == 1):
        batch = run_workflow(
            config,
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            cancel=cancel,
            adapter_loader=lambda _: FakeAdapter({"a": {"sleep": 60, "devin_export": _devin_configurable_export()}}),
        )
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "interrupted" and attempt.stop_confirmed
    assert attempt.request_path is not None
    retained = attempt.request_path.parent / "export.json"
    text = retained.read_text(encoding="utf-8")
    assert "synthetic-token-4567" not in text
    assert "[REDACTED]" in text


def test_unconfirmed_stop_never_reads_or_rewrites_devin_export(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _literal_env(monkeypatch, "synthetic-token-5678")

    async def quick_prepare(
        repository: Path,
        commit: str,
        destination: Path,
        *,
        deadline_seconds: float,
        cancel: threading.Event,
        stdout_path: Path,
        stderr_path: Path,
        secrets: tuple[str, ...] = (),
    ) -> ProcessResult:
        # Only agent execution observes unconfirmed stop evidence below.
        destination.mkdir(parents=True)
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        return ProcessResult(None, 0, True, deadline_seconds / 8)

    monkeypatch.setattr(runner, "prepare_worktree", quick_prepare)

    def no_stop(_: object) -> bool:
        return False

    monkeypatch.setattr("tools.agent_runner.process._stop_confirmed", no_stop)
    config = _as_devin(configuration(fallback=["b"]))
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(
            {"a": {"devin_export": _devin_configurable_export(), "payload": {"secret": "synthetic-token-5678"}}}
        ),
    )
    result = batch.results[0]
    assert result.state == "response_failed"
    attempt = result.attempts[0]
    assert not attempt.stop_confirmed and attempt.state == "response_failed"
    assert attempt.payload is None
    assert len(result.attempts) == 1 and attempt.agent_id == "a"
    assert attempt.request_path is not None
    export = attempt.request_path.parent / "export.json"
    assert attempt.sensitive_paths == [attempt.request_path.parent / "receipt.json", export]
    receipt = attempt.request_path.parent / "receipt.json"
    assert "synthetic-token-5678" in receipt.read_text(encoding="utf-8")
    text = export.read_text(encoding="utf-8")
    # Neither the receipt nor the export was read or rewritten for a live writer.
    assert "synthetic-token-5678" in text
    assert json.loads(text) == json.loads(_devin_configurable_export().replace("$TOKEN", "synthetic-token-5678"))


@pytest.mark.parametrize("unsafe", ["symlink", "fifo"])
def test_unsafe_devin_export_is_retained_reported_and_never_collected(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    _literal_env(monkeypatch, "synthetic-token-6789")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("synthetic-token-6789", encoding="utf-8")
    options: dict[str, object] = {"devin_export": _devin_configurable_export()}
    if unsafe == "symlink":
        options["export_link"] = str(sentinel)
    else:
        options["export_fifo"] = True
    config = _as_devin(configuration())
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": options}),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "response_failed" and attempt.stop_confirmed
    assert attempt.payload is None
    assert attempt.request_path is not None
    assert attempt.sensitive_paths == [attempt.request_path.parent / "export.json"]
    if unsafe == "symlink":
        # The external sentinel was never opened or rewritten.
        assert sentinel.read_text(encoding="utf-8") == "synthetic-token-6789"
    else:
        assert (attempt.request_path.parent / "export.json").exists()


def test_devin_export_redaction_fault_never_reaches_collection(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _literal_env(monkeypatch, "synthetic-token-7890")
    original = runner.redact_file

    def faulty(path: Path, secrets: tuple[str, ...]) -> bool:
        return False if Path(path).name == "export.json" else original(path, secrets)

    monkeypatch.setattr(runner, "redact_file", faulty)
    config = _as_devin(configuration())
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"devin_export": _devin_configurable_export()}}),
    )
    attempt = batch.results[0].attempts[0]
    assert batch.state == "failed"
    assert attempt.state == "response_failed" and attempt.stop_confirmed
    assert attempt.payload is None and attempt.identity.model is None
    assert attempt.request_path is not None
    assert attempt.sensitive_paths == [attempt.request_path.parent / "export.json"]
    # The unredactable managed file still holds values; only its path is reported.
    assert "synthetic-token-7890" in (attempt.request_path.parent / "export.json").read_text(encoding="utf-8")
