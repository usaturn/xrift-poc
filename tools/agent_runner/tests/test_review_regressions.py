"""Consumer-visible regressions from the final independent branch review."""

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import environ, process
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome
from tools.agent_runner.runner import run_workflow
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


def test_one_fallback_cannot_fill_two_parallel_role_slots(repo: Path, tmp_path: Path) -> None:
    """共有の fallback 1 人が並行の 2 つの担当を埋めると、同じ担当の結果が 2 人分として黙って数えられる。"""
    adapter = FakeAdapter({"a": {"missing": True}, "b": {"missing": True}})
    batch = run_workflow(
        configuration(["a", "b"], fallback=["c"]),
        "audit",
        "inspect",
        repo,
        concurrency=2,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: adapter,
    )
    assert sum(attempt.agent_id == "c" for item in batch.results for attempt in item.attempts) == 1


@pytest.mark.parametrize("malformed", [False, True])
def test_retained_receipts_are_scrubbed_even_when_invalid(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool
) -> None:
    """正常または不正な receipt の伏せ字を省くと、残したファイルに資格情報が黙って残る。"""
    secret = "known-credential-value"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    options: dict[str, object] = (
        {"raw": '{"credential":"' + secret} if malformed else {"payload": {"credential": secret}}
    )
    batch = run_workflow(
        configuration(),
        "audit",
        "inspect",
        repo,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": options}),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.request_path is not None
    raw = (attempt.request_path.parent / "receipt.json").read_bytes()
    if malformed:
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
    assert secret.encode() not in raw
    assert secret not in batch.model_dump_json()


class NeverCollect(FakeAdapter):
    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        raise AssertionError("must not collect while process stop is unconfirmed")


def test_unconfirmed_stop_does_not_read_or_rewrite_live_receipt(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止を確かめられない試行の receipt を読み書きし、sensitive_paths に載せず、fallback を黙って始める。"""
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
        output_dir=tmp_path / "run",
        cancel=threading.Event(),
        adapter_loader=lambda _: NeverCollect({"a": {"payload": {"credential": secret}}}),
    )
    attempt = batch.results[0].attempts[0]
    assert calls == 1
    assert attempt.state != "success" and not attempt.stop_confirmed
    assert attempt.request_path is not None
    path = attempt.request_path.parent / "receipt.json"
    assert attempt.sensitive_paths == [path]
    assert secret in path.read_text()
    assert secret not in batch.model_dump_json()


def test_exec_only_secrets_are_scrubbed_from_command_output(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動時の環境（/proc/self/environ）にだけある秘密が伏せ字の対象から外れ、ログに黙って残る。"""
    secret = "exec-env-only-credential-4f2a9c"
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"SERVICE_TOKEN": secret})
    cfg = configuration()
    cfg.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        cfg, "audit", "inspect", repo, output_dir=tmp_path / "run", adapter_loader=lambda _: FakeAdapter()
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.stdout_path is not None
    stdout = attempt.stdout_path.read_text()
    # The fake CLI printed the inherited value, so the log held it before redaction.
    assert "no-token" not in stdout
    assert secret not in stdout
