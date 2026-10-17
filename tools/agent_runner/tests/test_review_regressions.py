"""Consumer-visible regressions from the final independent branch review."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import environ, process
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, LaunchSpec
from tools.agent_runner.runner import run_workflow
from tools.agent_runner.tests.test_process import alive
from tools.agent_runner.tests.test_runner import FakeAdapter, configuration

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    (root / "file").write_text("original")
    subprocess.run(["git", "-C", str(root), "add", "file"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], capture_output=True, check=True)
    return root


@pytest.mark.parametrize("concurrency", [1, 2])
def test_one_fallback_cannot_fill_two_parallel_role_slots(repo: Path, concurrency: int) -> None:
    adapter = FakeAdapter({"a": {"missing": True}, "b": {"missing": True}})
    batch = run_workflow(
        configuration(["a", "b"], fallback=["c"]),
        "audit",
        "inspect",
        repo,
        concurrency=concurrency,
        adapter_loader=lambda _: adapter,
    )
    assert sum(attempt.agent_id == "c" for item in batch.results for attempt in item.attempts) == 1
    assert batch.state == "partial_success"
    exhausted = next(item for item in batch.results if item.state != "success")
    assert exhausted.fallback_exhausted


@pytest.mark.parametrize("malformed", [False, True])
def test_retained_receipts_are_scrubbed_even_when_invalid(
    repo: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool
) -> None:
    secret = "known-credential-value"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    options: dict[str, object] = {"payload": {"credential": secret}}
    if malformed:
        options["raw"] = '{"credential":"' + secret
    batch = run_workflow(
        configuration(), "audit", "inspect", repo, adapter_loader=lambda _: FakeAdapter({"a": options})
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.request_path is not None
    raw = (attempt.request_path.parent / "receipt.json").read_bytes()
    assert secret.encode() not in raw
    assert b"[REDACTED]" in raw
    assert attempt.sensitive_paths == []
    assert secret not in batch.model_dump_json()
    if not malformed:
        assert json.loads(raw)["payload"]["credential"] == "[REDACTED]"


def test_abnormal_exit_without_receipt_is_response_failure(repo: Path) -> None:
    batch = run_workflow(
        configuration(),
        "audit",
        "inspect",
        repo,
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True, "exit": 7}}),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.state == "response_failed"
    assert attempt.exit_code == 7


class ImmediateExitAdapter(FakeAdapter):
    def __init__(self, closed_pipes: bool) -> None:
        super().__init__()
        self.closed_pipes = closed_pipes
        self.child_path: Path | None = None

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        original = super().prepare(context)
        self.child_path = context.receipt_path.parent / "child.identity"
        pipes = ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if self.closed_pipes else ""
        code = (
            "import json,os,pathlib,subprocess,sys; incoming=json.load(sys.stdin); request=incoming['request']; "
            "receipt={key:request[key] for key in ('schema_version','run_id','request_id','agent_id','role')}; "
            "receipt.update(status='success',payload={'credential':os.environ['SERVICE_TOKEN']}); "
            f"pathlib.Path({str(context.receipt_path)!r}).write_text(json.dumps(receipt)); "
            f"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']{pipes}); "
            "start=pathlib.Path(f'/proc/{child.pid}/stat').read_bytes().rsplit(b')',1)[1].split()[19]; "
            f"pathlib.Path({str(self.child_path)!r}).write_text(str(child.pid)+' '+start.decode())"
        )
        return LaunchSpec((original.argv[0], "-c", code), original.stdin)


@pytest.mark.parametrize("closed_pipes", [False, True])
def test_success_receipt_survives_cli_immediate_exit_with_child(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_pipes: bool
) -> None:
    secret = "immediate-exit-private-token"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    # The exec environment, not os.environ, is what commands may inherit.
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"SERVICE_TOKEN": secret})
    # The deadline covers checkout, CLI startup, and receipt collection, which a
    # loaded host stretches (#694); its length is not the property. A runner that
    # waits for the 60 s child still fails here by timing out.
    cfg = configuration(deadline=10)
    cfg.agents["a"].env = ["SERVICE_TOKEN"]
    adapter = ImmediateExitAdapter(closed_pipes)
    try:
        batch = run_workflow(
            cfg, "audit", "inspect", repo, output_dir=tmp_path / "run", adapter_loader=lambda _: adapter
        )
        assert batch.state == "success"
        attempt = batch.results[0].attempts[0]
        assert attempt.exit_code == 0 and attempt.stop_confirmed
        assert attempt.sensitive_paths == []
        assert attempt.payload == {"credential": "[REDACTED]"}
        assert attempt.request_path is not None
        raw = (attempt.request_path.parent / "receipt.json").read_bytes()
        assert secret.encode() not in raw
        assert adapter.child_path is not None
        pid = int(adapter.child_path.read_text().split()[0])
        assert not alive(pid)
    finally:
        if adapter.child_path is not None and adapter.child_path.exists():
            pid, start = map(int, adapter.child_path.read_text().split())
            current = process._read_proc(pid)  # pyright: ignore[reportPrivateUsage] - fixture identity check
            if current is not None and current.starttime == start and alive(pid):
                os.kill(pid, signal.SIGKILL)


class FifoAdapter(FakeAdapter):
    collected = False

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        original = super().prepare(context)
        command = "import os; os.mkfifo(" + repr(str(context.receipt_path)) + ")"
        return type(original)((context.agent.command[0], "-c", command), b"")

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        self.collected = True
        raise AssertionError("collector must not run on a nonregular receipt")


def test_nonregular_receipt_is_rejected_before_adapter_reads_it(repo: Path) -> None:
    adapter = FifoAdapter()
    batch = run_workflow(configuration(), "audit", "inspect", repo, adapter_loader=lambda _: adapter)
    assert batch.results[0].state == "invalid_result"
    assert not adapter.collected


class NeverCollect(FakeAdapter):
    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        raise AssertionError("must not collect while process stop is unconfirmed")


def test_unconfirmed_stop_does_not_read_or_rewrite_live_receipt(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = process.execute
    calls = 0

    async def unconfirmed(*args: Any, **kwargs: Any) -> process.ProcessResult:
        nonlocal calls
        calls += 1
        result = await original(*args, **kwargs)
        return process.ProcessResult(result.state, result.exit_code, False, result.elapsed_seconds)

    # Patch the agent execute only; worktree preparation has its own real execute.
    monkeypatch.setattr("tools.agent_runner.runner.execute", unconfirmed)
    secret = "retained-live-credential"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    batch = run_workflow(
        configuration(fallback=["b"]),
        "audit",
        "inspect",
        repo,
        cancel=threading.Event(),
        adapter_loader=lambda _: NeverCollect({"a": {"payload": {"credential": secret}}}),
    )
    attempt = batch.results[0].attempts[0]
    assert calls == 1
    assert attempt.state == "response_failed" and not attempt.stop_confirmed
    assert attempt.request_path is not None
    path = attempt.request_path.parent / "receipt.json"
    assert attempt.sensitive_paths == [path]
    assert secret in path.read_text()
    assert secret not in batch.model_dump_json()


class HugeReceipt(FakeAdapter):
    collected = False

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        original = super().prepare(context)
        command = (
            f"from pathlib import Path; Path({str(context.receipt_path)!r}).write_bytes(b'x' * (17 * 1024 * 1024))"
        )
        return type(original)((context.agent.command[0], "-c", command), b"")

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        self.collected = True
        raise AssertionError("must not read unlimited receipt")


def test_oversized_receipt_rejected_before_unbounded_collection(repo: Path) -> None:
    adapter = HugeReceipt()
    batch = run_workflow(configuration(), "audit", "inspect", repo, adapter_loader=lambda _: adapter)
    assert batch.results[0].state == "invalid_result"
    assert not adapter.collected


# Runs run_workflow in a fresh interpreter whose startup locale coercion cannot
# be reproduced by monkeypatch.setenv inside this already-running process.
_LITERAL_ENV_PROBE = """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.models import Agent, Assignment, Config, Receipt, Role, Workflow
from tools.agent_runner.runner import run_workflow


class EnvAdapter:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        return LaunchSpec(("/usr/bin/env", "-0"), b"")

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        env = {
            name.decode("utf-8", "replace"): value.decode("utf-8", "replace")
            for entry in stdout_path.read_bytes().split(b"\\0")
            if entry
            for name, value in [entry.split(b"=", 1)]
        }
        return AdapterOutcome(
            Receipt(
                run_id=context.request.run_id,
                request_id=context.request.request_id,
                agent_id=context.request.agent_id,
                role=context.request.role,
                status="success",
                payload={"env": env},
            )
        )


config = Config(
    agents={"a": Agent(runtime="pi", command=["/usr/bin/env"], model="a", env=["LC_CTYPE"])},
    roles={"inspect": Role(writes=False, deadline_seconds=30, result_shape="json")},
    workflows={"audit": Workflow(assignments={"inspect": Assignment(agents=["a"])})},
)
batch = run_workflow(
    config, "audit", "prompt", Path(sys.argv[2]), output_dir=Path(sys.argv[3]), adapter_loader=lambda _: EnvAdapter()
)
assert batch.state == "success", batch.model_dump_json()
attempt = batch.results[0].attempts[0]
assert attempt.payload is not None, batch.model_dump_json()
print(json.dumps(attempt.payload["env"]))
"""


# An allowlisted variable is inherited from the environment the runner was
# started with; interpreter startup coercion must not rewrite or inject it.
@pytest.mark.parametrize(
    ("exec_env", "expected"),
    [
        ({"LC_CTYPE": "C"}, "C"),
        ({"LC_CTYPE": "POSIX"}, "POSIX"),
        ({"LANG": "C"}, None),
        ({"LC_ALL": "C", "LANG": "C"}, None),
    ],
)
def test_allowlisted_environment_is_the_literal_exec_environment(
    repo: Path, tmp_path: Path, exec_env: dict[str, str], expected: str | None
) -> None:
    probe = tmp_path / "literal_env_probe.py"
    probe.write_text(_LITERAL_ENV_PROBE)
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(probe),
            str(Path(__file__).resolve().parents[3]),
            str(repo),
            str(tmp_path / "run"),
        ],
        env={"PATH": os.defpath, "HOME": str(tmp_path / "home"), **exec_env},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    env = json.loads(completed.stdout.strip().splitlines()[-1])
    assert env.get("LC_CTYPE") == expected


# A secret-named value that exists only in the exec environment can still be
# allowlisted into a command; it must be scrubbed from retained output even
# though os.environ never held it.
def test_exec_only_secrets_are_scrubbed_from_command_output(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "exec-env-only-credential-4f2a9c"
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"SERVICE_TOKEN": secret})
    cfg = configuration()
    cfg.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(cfg, "audit", "inspect", repo, adapter_loader=lambda _: FakeAdapter())
    assert batch.state == "success" and batch.results[0].state == "success"
    attempt = batch.results[0].attempts[0]
    assert attempt.stdout_path is not None
    stdout = attempt.stdout_path.read_text()
    assert secret not in stdout and "[REDACTED]" in stdout
