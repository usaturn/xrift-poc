"""Workflow behavior against real disposable Git checkouts and fake Python CLIs."""

import contextlib
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Iterable
from pathlib import Path

import pytest

from tools.agent_runner import environ, workspace
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.config import ConfigError
from tools.agent_runner.models import (
    DIAGNOSIS_TEXT_CHARS,
    Agent,
    Assignment,
    Config,
    Diagnosis,
    Identity,
    Receipt,
    Role,
    Runtime,
    State,
    Workflow,
)
from tools.agent_runner.runner import RunnerError, run_workflow

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")

# A fake CLI: options pick its receipt, exit code, and side effects; it echoes inherited values to its logs.
_SCRIPT = """
import json, os, pathlib, sys
incoming = json.loads(sys.stdin.read())
request = incoming['request']
options = incoming['options']
if options.get('outside'):
    pathlib.Path(options['outside']).write_text('changed again')
token = os.environ.get('SERVICE_TOKEN', 'no-token')
_export_file = pathlib.Path(sys.argv[1]).parent.joinpath('export.json')
if options.get('export_link'):
    _export_file.symlink_to(options['export_link'])
elif options.get('devin_export') is not None:
    _export_file.write_text(options['devin_export'].replace('$TOKEN', token), encoding='utf-8')
print(os.environ.get('SERVICE_TOKEN', 'no-token'))
print(os.environ.get('Extra_Password', 'no-password'), file=sys.stderr)
payload = options.get('payload', {'prompt': request['prompt'], 'depth': os.environ.get('AGENT_RUNNER_DEPTH'),
    'ambient': os.environ.get('UNCONFIGURED_SECRET'), 'git': os.environ.get('GIT_DIR')})
receipt = {key: request[key] for key in ('schema_version', 'run_id', 'request_id', 'agent_id', 'role')}
receipt.update(status='success', payload=payload)
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
        diagnosis: Diagnosis | None = None,
    ) -> None:
        self.options = options or {}
        self.env = env or {}
        self.outcome_state: State | None = outcome_state
        self.identity = identity or Identity()
        self.diagnosis = diagnosis

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        return LaunchSpec(
            argv=(*context.agent.command, str(context.receipt_path)),
            stdin=json.dumps(
                {
                    "request": context.request.model_dump(mode="json"),
                    "options": self.options.get(context.request.agent_id, {}),
                }
            ).encode(),
            env=self.env,
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        receipt = (
            Receipt.model_validate_json(context.receipt_path.read_bytes()) if context.receipt_path.exists() else None
        )
        return AdapterOutcome(receipt, self.identity, self.outcome_state, self.diagnosis)


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


def configuration(agents: list[str] | None = None, fallback: list[str] | None = None) -> Config:
    return Config(
        agents={
            name: Agent(runtime="pi", command=[sys.executable, "-c", _SCRIPT], model=name) for name in ("a", "b", "c")
        },
        roles={"inspect": Role.model_validate({"writes": False, "deadline_seconds": 5.0, "result_shape": "json"})},
        workflows={
            "audit": Workflow(assignments={"inspect": Assignment(agents=agents or ["a"], fallback=fallback or [])})
        },
    )


def test_unavailable_registered_checkout_is_an_outside_change(repository: Path, tmp_path: Path) -> None:
    """監査できない登録済みの checkout が outside_changes から落ちると、呼び出し側が黙って受け入れる。"""
    external = workspace.create_worktree(repository, "HEAD", tmp_path / "unavailable")
    gitfile = external / ".git"
    original = gitfile.read_bytes()
    gitfile.write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
    try:
        batch = run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(),
        )
        assert batch.outside_changes == [str(external)]
    finally:
        gitfile.write_bytes(original)


@pytest.mark.parametrize("options", [{"payload": {}}, {"exit": 7}], ids=["empty-payload", "nonzero-exit"])
def test_exit_zero_is_not_a_substitute_for_valid_receipt(
    repository: Path, tmp_path: Path, options: dict[str, object]
) -> None:
    """空の payload・終了 7 の CLI を success とする、観測していない identity を補うと、呼び出し側が黙って誤る。"""
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": options}),
    )
    assert batch.results[0].state != "success"
    assert batch.results[0].attempts[0].identity.model is None


def test_adapter_success_cannot_bypass_missing_receipt(repository: Path, tmp_path: Path) -> None:
    """receipt が無いのに adapter の success で成功にすると、payload の無い success を黙って返す。"""
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}, outcome_state="success"),
    )
    assert batch.results[0].state != "success"


def test_partial_success_and_independent_worktrees(repository: Path, tmp_path: Path) -> None:
    """1 人の失敗を batch の success にする・並行の担当に同じ worktree を渡すと、失敗や混ざった変更を黙って通す。"""
    batch = run_workflow(
        configuration(["a", "b"]),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}),
    )
    assert batch.state != "success"
    assert len({result.attempts[0].work_dir for result in batch.results}) == 2


def test_only_explicit_fallback_is_tried_in_order(repository: Path, tmp_path: Path) -> None:
    """設定に無い担当へ切り替える・fallback の順序を取り違えると、利用者が選んでいない provider に黙って送る。"""
    fake = FakeAdapter({"a": {"missing": True}, "b": {"exit": 2}})
    batch = run_workflow(
        configuration(fallback=["b", "c"]),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: fake,
    )
    assert batch.results[0].actual_agent == "c"
    assert [attempt.agent_id for attempt in batch.results[0].attempts] == ["a", "b", "c"]
    alone = run_workflow(
        configuration(), "audit", "prompt", repository, output_dir=tmp_path / "alone", adapter_loader=lambda _: fake
    )
    assert [attempt.agent_id for attempt in alone.results[0].attempts] == ["a"]


def test_outside_change_to_already_dirty_file_is_warning_only(repository: Path, tmp_path: Path) -> None:
    """開始前から dirty な追跡ファイルを担当が書き換えても outside_changes に出ず、呼び出し側が黙って受け入れる。"""
    dirty = repository / "tracked.txt"
    dirty.write_text("already dirty")
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"outside": str(dirty)}}),
    )
    assert dirty.read_text() == "changed again"
    assert batch.outside_changes == [str(repository)]


def test_only_allowlisted_environment_and_redacted_results(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """許可しない資格情報・GIT_* が担当に渡る、ログ・payload・identity・request.json に秘密が黙って残る。"""
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
    batch = run_workflow(
        cfg, "audit", "prompt", repository, output_dir=tmp_path / "run", adapter_loader=lambda _: fake
    )
    serialized = batch.model_dump_json()
    assert "token-value" not in serialized and "password-value" not in serialized
    attempt = batch.results[0].attempts[0]
    assert attempt.stdout_path is not None and "token-value" not in attempt.stdout_path.read_text()
    assert attempt.stderr_path is not None and "password-value" not in attempt.stderr_path.read_text()
    assert attempt.request_path is not None and "password-value" not in attempt.request_path.read_text()
    environment = run_workflow(
        cfg,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "environment",
        adapter_loader=lambda _: FakeAdapter(env={"AGENT_RUNNER_DEPTH": "0"}),
    )
    payload = environment.results[0].attempts[0].payload
    assert isinstance(payload, dict)
    assert payload["depth"] == "1" and payload["ambient"] is None and payload["git"] is None


def test_diagnosis_is_redacted_before_it_is_cut(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先に切ってから伏せると、境界で切れた秘密の先頭が diagnosis に黙って残る（#96）。"""
    monkeypatch.setenv("SERVICE_TOKEN", "token-value")
    # The cut falls inside the secret: its first five characters fit below the limit.
    message = "x" * (DIAGNOSIS_TEXT_CHARS - 5) + "token-value の後\n2 行目"
    fake = FakeAdapter(
        {"a": {"exit": 1}},
        diagnosis=Diagnosis(
            ending="turn_end", reason="error", code="token-value", status=503, message=message, steps=2
        ),
    )
    batch = run_workflow(
        configuration(), "audit", "prompt", repository, output_dir=tmp_path / "run", adapter_loader=lambda _: fake
    )
    diagnosis = batch.results[0].attempts[0].diagnosis
    assert diagnosis is not None and diagnosis.message is not None
    assert "token" not in diagnosis.message
    assert "token-value" not in diagnosis.model_dump_json()


def test_nested_invocation_fails_closed(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """担当の中（AGENT_RUNNER_DEPTH=1）から Python API を呼ぶと、入れ子の担当を黙って起動する。"""
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", "1")
    with pytest.raises(ConfigError):
        run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(),
        )


def test_invalid_write_exceptions_are_rejected_before_any_output(repository: Path, tmp_path: Path) -> None:
    """ブランチ名に入りうる U+2028 を通すと、作業範囲の指示に行が足され、許可の範囲を黙って書き換える。"""
    output = tmp_path / "output"
    with pytest.raises(RunnerError):
        run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=output,
            write_exceptions=("ok", "a\u2028b"),
        )
    assert not output.exists()


def test_a_bare_string_is_not_split_into_exceptions(repository: Path, tmp_path: Path) -> None:
    """文字列を 1 つ渡すと 1 文字ずつ（- / を含む）が許可として並び、書き込みを黙って広く許す。"""
    output = tmp_path / "output"
    with pytest.raises(RunnerError):
        run_workflow(configuration(), "audit", "prompt", repository, output_dir=output, write_exceptions="abc")
    assert not output.exists()


class ReadOnlyAdapter(FakeAdapter):
    """codex の -s read-only のように、writes=false の役割をランタイム側で読み取り専用にする担当（#52）。"""

    capabilities = Capabilities(enforces_read_only=True)


def read_only_codex(runtime: str) -> FakeAdapter:
    return ReadOnlyAdapter() if runtime == "codex" else FakeAdapter()


def test_read_only_agents_are_rejected_before_any_output_when_writes_are_needed(
    repository: Path, tmp_path: Path
) -> None:
    """読み取り専用の fallback に書き込みの例外を渡して起動すると、毎回却下されてもレビュアーが黙って減る（#52）。"""
    config = configuration(["a"], ["b"])
    runtimes: dict[str, Runtime] = {"a": "pi", "b": "codex", "c": "cursor"}
    for name, agent in config.agents.items():
        agent.runtime = runtimes[name]
    output = tmp_path / "output"
    with pytest.raises(RunnerError):
        run_workflow(
            config,
            "audit",
            "prompt",
            repository,
            output_dir=output,
            adapter_loader=read_only_codex,
            write_exceptions=("/x/reviews: run records",),
        )
    assert not output.exists()


def test_existing_or_symlink_output_is_never_reused(repository: Path, tmp_path: Path) -> None:
    """既存の出力ディレクトリを再利用すると前の run の成果物を黙って上書きし、リンクなら別の場所へ書く。"""
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


def test_preparation_logs_are_redacted(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """準備（git worktree add）の hook が出した秘密を、準備のログ（worktree.*.log）に伏せずに黙って残す。"""
    hook = repository / ".git/hooks/post-checkout"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\n"
        "print('hook-ran', os.environ['SERVICE_TOKEN'], flush=True)\n"
        "print(os.environ['UNCONFIGURED_SECRET'], file=sys.stderr, flush=True)\n"
    )
    hook.chmod(0o755)
    values = {"SERVICE_TOKEN": "prep-token-value", "UNCONFIGURED_SECRET": "ambient-prep-value"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    # Preparation helpers run under the exec environment, not os.environ.
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | values)
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(),
    )
    work_dir = batch.results[0].attempts[0].work_dir
    assert work_dir is not None
    logs = "".join((work_dir.parent / name).read_text() for name in ("worktree.stdout.log", "worktree.stderr.log"))
    assert "hook-ran" in logs
    assert not any(value in logs for value in values.values())


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
    """失敗した試行の Devin の export.json（継承した秘密を含みうる）を伏せずに黙って残す。"""
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
    assert attempt.request_path is not None
    text = (attempt.request_path.parent / "export.json").read_text(encoding="utf-8")
    # The fake CLI wrote the inherited value, so the export held it before redaction.
    assert "no-token" not in text
    assert "synthetic-token-2345" not in text


def test_unsafe_devin_export_is_retained_reported_and_never_collected(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """export.json が外を指すリンクのとき、伏せ字でリンク先（利用者のファイル）を書き換える・収集して成功にする。"""
    _literal_env(monkeypatch, "synthetic-token-6789")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("synthetic-token-6789", encoding="utf-8")
    config = _as_devin(configuration())
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(
            {"a": {"devin_export": _devin_configurable_export(), "export_link": str(sentinel)}}
        ),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.state != "success" and attempt.payload is None
    assert attempt.request_path is not None
    assert attempt.sensitive_paths == [attempt.request_path.parent / "export.json"]
    assert sentinel.read_text(encoding="utf-8") == "synthetic-token-6789"
