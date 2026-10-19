"""Machine CLI behavior without invoking any installed agent runtime."""

import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import cast, get_args

import pytest

from tools.agent_runner import cli, hooks, runner
from tools.agent_runner.adapters import (
    AdapterContext,
    AdapterOutcome,
    Capabilities,
    LaunchSpec,
    runtime_supported,
)
from tools.agent_runner.models import BatchResult, BatchState, Config, Result, Runtime

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")

_TEMPLATE = """schema_version = 1
[agents.a]
runtime = "pi"
command = ["never-invoke-this-command"]
model = "model-a"
[agents.b]
runtime = "codex"
command = ["never-invoke-this-command"]
model = "model-b"
[roles.inspect]
writes = false
deadline_seconds = 5
result_shape = "json"
[workflows.audit.assignments.inspect]
agents = ["a"]
fallback = ["b"]
"""


@pytest.fixture
def template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(tmp_path / "absent-personal.toml"))
    path = tmp_path / "template.toml"
    path.write_text(_TEMPLATE)
    return path


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return repo


def command(template: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.agent_runner", "--template", str(template), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def document(output: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads(output))


@pytest.mark.parametrize("subcommand", ["check", "list"])
def test_check_list_report_support_state_without_configuration_values(template: Path, subcommand: str) -> None:
    process = command(template, subcommand)
    assert process.returncode == 0
    data = document(process.stdout)
    runtimes = cast(list[dict[str, object]], data["runtimes"])
    # Every runtime the configuration model accepts is reported exactly once.
    assert sorted(cast(str, item["runtime"]) for item in runtimes) == sorted(get_args(Runtime))
    assert all(item["supported"] == runtime_supported(cast(str, item["runtime"])) for item in runtimes)
    assert "never-invoke-this-command" not in process.stdout
    assert "model-a" not in process.stdout
    assert process.stderr == ""


@pytest.mark.parametrize(
    "args",
    [
        ["invalid-secret-command"],
        ["run", "--concurrency", "secret-value"],
        ["run", "--unknown-secret-flag"],
        ["run"],
        ["run", "--workflow", "audit", "--assign", "bad-secret-assignment"],
        # argparse は -x のような値を別の引数に書くとオプションと読む。誤りになっても値は出さない
        ["run", "--workflow", "audit", "--write-exception", "-secret-value"],
    ],
)
def test_invalid_arguments_are_sanitized(template: Path, args: list[str]) -> None:
    process = command(template, *args)
    assert process.returncode == 2
    assert "secret" not in process.stdout + process.stderr
    assert document(process.stdout)["error"] == "invalid_input"


def test_invalid_config_never_prints_raw_values(template: Path) -> None:
    template.write_text('schema_version = 1\nsecret_key = "secret-value"')
    process = command(template, "check")
    assert process.returncode == 2
    assert document(process.stdout)["error"] == "unknown_key"
    assert "secret" not in process.stdout + process.stderr


def test_explicit_missing_and_auto_broken_personal_are_errors(
    template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--template", str(template), "--config", str(tmp_path / "missing"), "check"]) == 2
    assert document(capsys.readouterr().out)["error"] == "config_io"
    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "missing")
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(broken))
    assert cli.main(["--template", str(template), "list"]) == 2
    assert document(capsys.readouterr().out)["error"] == "config_io"


@pytest.mark.parametrize(("state", "status"), [("success", 0), ("self", 0), ("partial_success", 1), ("failed", 1)])
def test_run_exit_codes_and_atomic_result(
    template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: BatchState,
    status: int,
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text('line 1\n"line 2"\n')
    artifact = tmp_path / "new-run"

    def fake_run(config: Config, workflow: str, text: str, repository: Path, **kwargs: object) -> BatchResult:
        assert text == 'line 1\n"line 2"\n'
        assert workflow == "audit" and repository == tmp_path
        assert kwargs["revision"] == "HEAD" and kwargs["concurrency"] == 3
        assert kwargs["output_dir"] == artifact
        artifact.mkdir()
        return BatchResult(
            run_id="test-run",
            state=state,
            assignments={"inspect": ["a"]},
            results=[],
            outside_changes=[],
            elapsed_seconds=0.0,
            artifact_dir=artifact,
        )

    monkeypatch.setattr(cli, "run_workflow", fake_run)
    code = cli.main(
        [
            "--template",
            str(template),
            "run",
            "--workflow",
            "audit",
            "--repository",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--concurrency",
            "3",
            "--output-dir",
            str(artifact),
        ]
    )
    assert code == status
    summary = document(capsys.readouterr().out)
    saved = document((artifact / "result.json").read_text())
    assert saved["state"] == state and summary["state"] == state
    assert summary["result_path"] == str(artifact / "result.json")
    assert not list(artifact.glob("*.tmp"))
    assert "line 1" not in json.dumps(summary) + json.dumps(saved)


def test_cli_overlay_assignment_priority_preserves_fallback(
    template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    personal = tmp_path / "personal.toml"
    personal.write_text('[workflows.audit.assignments.inspect]\nagents = ["b"]\nfallback = ["a"]\n')
    prompt = tmp_path / "prompt"
    prompt.write_text("task")
    seen: list[list[str]] = []

    def fake_run(config: Config, workflow: str, text: str, repository: Path, **kwargs: object) -> BatchResult:
        assignment = config.workflows[workflow].assignments["inspect"]
        seen.append(assignment.agents)
        assert assignment.fallback == ["a"]
        artifact = tmp_path / f"run-{len(seen)}"
        artifact.mkdir()
        return BatchResult(
            run_id=f"run-{len(seen)}",
            state="self",
            assignments={"inspect": assignment.agents},
            results=[],
            outside_changes=[],
            elapsed_seconds=0.0,
            artifact_dir=artifact,
        )

    monkeypatch.setattr(cli, "run_workflow", fake_run)
    base = [
        "--template",
        str(template),
        "--config",
        str(personal),
        "run",
        "--workflow",
        "audit",
        "--repository",
        str(tmp_path),
        "--prompt-file",
        str(prompt),
    ]
    assert cli.main(base) == 0
    assert cli.main([*base, "--assign", "inspect=self"]) == 0
    assert seen == [["b"], ["self"]]
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_sigint_saves_interrupted_batch_and_restores_handler(
    template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt = tmp_path / "prompt"
    prompt.write_text("task")
    previous = signal.getsignal(signal.SIGINT)
    artifact = tmp_path / "interrupted"

    def fake_run(config: Config, workflow: str, text: str, repository: Path, **kwargs: object) -> BatchResult:
        event = cast(threading.Event, kwargs["cancel"])
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert event.is_set()
        artifact.mkdir()
        return BatchResult(
            run_id="interrupt-run",
            state="failed",
            assignments={"inspect": ["a"]},
            results=[
                Result(
                    request_id="request",
                    role="inspect",
                    requested_agent="a",
                    actual_agent="a",
                    state="interrupted",
                    attempts=[],
                )
            ],
            outside_changes=[],
            elapsed_seconds=0.0,
            artifact_dir=artifact,
        )

    monkeypatch.setattr(cli, "run_workflow", fake_run)
    assert (
        cli.main(
            [
                "--template",
                str(template),
                "run",
                "--workflow",
                "audit",
                "--repository",
                str(tmp_path),
                "--prompt-file",
                str(prompt),
            ]
        )
        == 130
    )
    assert signal.getsignal(signal.SIGINT) == previous
    saved = document((artifact / "result.json").read_text())
    assert cast(list[dict[str, object]], saved["results"])[0]["state"] == "interrupted"
    assert document(capsys.readouterr().out)["state"] == "failed"


def test_unsupported_run_persists_result(
    template: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Stand-in modules without ADAPTER keep "unsupported" reachable in-process
    # even after every fixed runtime ships an implementation.
    for module in ("pi", "codex"):
        name = f"tools.agent_runner.adapters.{module}"
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    prompt = tmp_path / "prompt"
    prompt.write_text("task")
    output = tmp_path / "unsupported"
    assert (
        cli.main(
            [
                "--template",
                str(template),
                "run",
                "--workflow",
                "audit",
                "--repository",
                str(repository),
                "--prompt-file",
                str(prompt),
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == ""
    saved = document((output / "result.json").read_text())
    result = cast(list[dict[str, object]], saved["results"])[0]
    assert result["state"] == "unsupported"
    assert len(cast(list[object], result["attempts"])) == 2


# The shipped template is the base of every installed configuration. Loading it
# checks its TOML shape and references (defined agents and roles, distinct
# models per role); each agent it defines must name a runtime with an adapter.
def test_shipped_template_passes_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(tmp_path / "absent-personal.toml"))
    assert cli.main(["check"]) == 0
    agents = cast(list[dict[str, object]], document(capsys.readouterr().out)["agents"])
    assert agents and all(agent["supported"] is True for agent in agents)


def test_default_template_resolves_relative_to_module_not_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Characterize the path used by the real configuration reader, not a mocked loader.
    module = tmp_path / "installed" / "tools" / "agent_runner" / "cli.py"
    default = module.parents[2] / ".devcontainer" / "agent-runner.toml"
    default.parent.mkdir(parents=True)
    default.write_text(_TEMPLATE)
    monkeypatch.setattr(cli, "__file__", str(module))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(tmp_path / "absent"))
    assert cli.main(["check"]) == 0
    assert "runtimes" in document(capsys.readouterr().out)


class ListingAdapter:
    def __init__(self, capabilities: Capabilities) -> None:
        self.capabilities = capabilities

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        return LaunchSpec((), b"")

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        return AdapterOutcome()


@pytest.mark.parametrize(
    ("capabilities", "supported"),
    [
        (Capabilities(), True),
        (Capabilities(stdin=False), False),
        (Capabilities(noninteractive=cast(bool, "truthy-but-not-guaranteed")), False),
    ],
)
def test_list_requires_installed_guaranteed_capabilities(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    capabilities: Capabilities,
    supported: bool,
) -> None:
    module = ModuleType("tools.agent_runner.adapters.pi")
    module.__dict__["ADAPTER"] = ListingAdapter(capabilities)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert cli.main(["--template", str(template), "list"]) == 0
    inventory = document(capsys.readouterr().out)
    pi = next(item for item in cast(list[dict[str, object]], inventory["runtimes"]) if item["runtime"] == "pi")
    assert pi["supported"] is supported


# --- after_run（Issue #601） ---------------------------------------------------------------------------------------

_HOOK = (
    "import json, os, sys; "
    "json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'depth': os.environ.get('AGENT_RUNNER_DEPTH')}, "
    "open(sys.argv[2], 'w')); sys.exit(int(sys.argv[3]))"
)


def add_after_run(template: Path, *commands: list[str]) -> None:
    template.write_text(template.read_text() + "[workflows.audit]\nafter_run = " + json.dumps(list(commands)) + "\n")


def run_audit(template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("task")
    artifact = tmp_path / "new-run"

    def fake_run(config: Config, workflow: str, text: str, repository: Path, **kwargs: object) -> BatchResult:
        artifact.mkdir()
        return BatchResult(
            run_id="test-run",
            state="success",
            assignments={"inspect": ["a"]},
            results=[],
            outside_changes=[],
            elapsed_seconds=0.0,
            artifact_dir=artifact,
        )

    monkeypatch.setattr(cli, "run_workflow", fake_run)
    argv = ["--template", str(template), "run", "--workflow", "audit", "--repository", str(tmp_path)]
    code = cli.main([*argv, "--prompt-file", str(prompt), "--output-dir", str(artifact)])
    return code, artifact


@pytest.mark.parametrize(("hook_exit", "state"), [(0, "success"), (3, "failed")])
def test_after_run_gets_the_result_path_and_never_changes_the_exit_code(
    template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_exit: int,
    state: str,
) -> None:
    marker = tmp_path / "hook.json"
    add_after_run(template, [sys.executable, "-c", _HOOK, "{result}", str(marker), str(hook_exit)])
    code, artifact = run_audit(template, tmp_path, monkeypatch)
    assert code == 0
    summary = document(capsys.readouterr().out)
    seen = json.loads(marker.read_text())
    assert seen["argv"][0] == summary["result_path"] == str(artifact / "result.json")
    assert seen["depth"] == "1"
    assert Path(seen["cwd"]) == Path(cli.__file__).resolve().parents[2]
    reports = cast(list[dict[str, object]], summary["after_run"])
    assert reports == [
        {
            "index": 0,
            "state": state,
            "exit_code": hook_exit,
            "stop_confirmed": True,
            "stdout_path": str(artifact / "after-run-0.stdout.log"),
            "stderr_path": str(artifact / "after-run-0.stderr.log"),
        }
    ]
    assert _HOOK not in json.dumps(summary)
    assert document((artifact / "result.json").read_text())["state"] == "success"


def test_after_run_timeout_is_reported_without_changing_the_exit_code(
    template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hooks, "AFTER_RUN_DEADLINE_SECONDS", 0.5)
    add_after_run(template, [sys.executable, "-c", "import time; time.sleep(30)"])
    code, _artifact = run_audit(template, tmp_path, monkeypatch)
    assert code == 0
    report = cast(list[dict[str, object]], document(capsys.readouterr().out)["after_run"])[0]
    assert report["state"] == "timed_out" and report["stop_confirmed"] is True


def test_summary_lists_no_after_run_when_unset(
    template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_audit(template, tmp_path, monkeypatch)[0] == 0
    assert document(capsys.readouterr().out)["after_run"] == []


def test_after_run_is_not_started_after_an_interrupted_run(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    marker = tmp_path / "hook.json"
    reports = hooks.run_after_run(
        [[sys.executable, "-c", _HOOK, "{result}", str(marker), "0"]],
        result_path=tmp_path / "result.json",
        cwd=tmp_path,
        cancel=cancel,
    )
    assert reports[0]["state"] == "interrupted" and not marker.exists()


def test_after_run_internal_error_is_reported_without_changing_the_exit_code(
    template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("supervisor unavailable")

    monkeypatch.setattr(hooks, "execute", broken)
    add_after_run(template, ["never-invoke-this-command"])
    code, _artifact = run_audit(template, tmp_path, monkeypatch)
    assert code == 0
    report = cast(list[dict[str, object]], document(capsys.readouterr().out)["after_run"])[0]
    assert (report["state"], report["exit_code"], report["stop_confirmed"]) == ("response_failed", None, False)


# --- write_exceptions（Issue #656） --------------------------------------------------------------------------------

_BODY = "review the change"
# 受け取った依頼の prompt を receipt の payload に写す。実際のエージェントの CLI は起動しない
_ECHO = (
    "import json, pathlib, sys; request = json.loads(sys.stdin.read()); "
    "receipt = {key: request[key] for key in ('schema_version', 'run_id', 'request_id', 'agent_id', 'role')}; "
    "receipt.update(status='success', payload={'prompt': request['prompt']}); "
    "pathlib.Path(sys.argv[1]).write_text(json.dumps(receipt))"
)


class EchoAdapter:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        argv = (sys.executable, "-c", _ECHO, str(context.receipt_path))
        return LaunchSpec(argv, context.request.model_dump_json().encode())

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        return AdapterOutcome()


_CODEX_FALLBACK = 'fallback = ["b"]\n'


def run_echo(template: Path, repository: Path, output: Path, monkeypatch: pytest.MonkeyPatch, *options: str) -> int:
    """実際の run_workflow を通し、pi の adapter だけを EchoAdapter に差し替えて CLI の run を実行する。

    雛形の fallback の codex は writes=false の役割を読み取り専用で起動するので、--write-exception を付けた run を
    runner が止める（#52）。例外の行を確かめるため、その fallback を外した写しを使う。
    """
    module = ModuleType("tools.agent_runner.adapters.pi")
    module.__dict__["ADAPTER"] = EchoAdapter()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    text = template.read_text()
    assert _CODEX_FALLBACK in text
    echo_template = output.with_name(f"{output.name}-template.toml")
    echo_template.write_text(text.replace(_CODEX_FALLBACK, ""))
    prompt = output.with_name(f"{output.name}-prompt.txt")
    prompt.write_text(_BODY)
    argv = ["--template", str(echo_template), "run", "--workflow", "audit", "--repository", str(repository)]
    return cli.main([*argv, "--prompt-file", str(prompt), "--output-dir", str(output), *options])


def prompt_lines(output: Path) -> list[str]:
    """最後の試行が受け取った prompt を、実行ごとに違う work_dir を置き換えて行に分ける。"""
    results = cast(list[dict[str, object]], document((output / "result.json").read_text())["results"])
    attempt = cast(list[dict[str, object]], results[0]["attempts"])[-1]
    prompt = cast(dict[str, str], attempt["payload"])["prompt"]
    return prompt.replace(cast(str, attempt["work_dir"]), "<work_dir>").split("\n")


def test_write_exceptions_follow_the_work_scope_lines_in_order(
    template: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = ["/x/#7-feat-日本語\u3000名前: run records", "-x/notes", "uv cache with spaces"]
    plain_code = run_echo(template, repository, tmp_path / "plain", monkeypatch)
    capsys.readouterr()
    granted_code = run_echo(
        template,
        repository,
        tmp_path / "granted",
        monkeypatch,
        "--write-exception",
        values[0],
        f"--write-exception={values[1]}",
        "--write-exception",
        values[2],
    )
    captured = capsys.readouterr()
    summary = json.dumps(document(captured.out), ensure_ascii=False)
    assert plain_code == granted_code == 0
    assert captured.err == ""
    plain, granted = prompt_lines(tmp_path / "plain"), prompt_lines(tmp_path / "granted")
    # --write-exception を付けない呼び出しの指示は、作業範囲の 2 行と空行のまま
    assert runner.EXCEPTIONS_HEADING not in plain
    assert [line.split(":", 1)[0].split("=", 1)[0] for line in plain[:2]] == ["Work in this worktree only", "writes"]
    assert plain[2:] == ["", _BODY]
    assert granted == [*plain[:2], runner.EXCEPTIONS_HEADING, *(f"- {value}" for value in values), *plain[2:]]
    assert not any(value in summary for value in values)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        pytest.param("notes may change", 0, id="valid"),
        pytest.param("", 2, id="empty"),
        pytest.param(" a", 2, id="leading-space"),
        pytest.param("a ", 2, id="trailing-space"),
        pytest.param("\u3000a", 2, id="leading-ideographic"),
        pytest.param("a\u00a0", 2, id="trailing-nbsp"),
        pytest.param("private-value\nb", 2, id="newline"),
        pytest.param("a\rb", 2, id="carriage-return"),
        pytest.param("a\u2028b", 2, id="unicode-line-separator"),
        pytest.param("a\tb", 2, id="tab"),
        pytest.param("a\x00b", 2, id="nul"),
        pytest.param("a\u3000b\u00a0c", 0, id="internal-unicode-space"),
        pytest.param("\u200b", 0, id="format-character"),
        pytest.param("a\ue000b\u0378c", 0, id="private-unassigned"),
        # 別の引数に書いた -x は argparse がオプションと読む。値は --write-exception=-x と書く
        pytest.param("-x", 2, id="dash-separate-token"),
    ],
)
def test_invalid_write_exceptions_are_rejected_before_any_output(
    template: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
    code: int,
) -> None:
    output = tmp_path / "output"
    assert run_echo(template, repository, output, monkeypatch, "--write-exception", value) == code
    captured = capsys.readouterr()
    # 正しい値なら同じ呼び出しが成功するので、拒否は値によるもので引数の未定義によるものではない
    assert output.exists() is (code == 0)
    if code == 2:
        assert captured.out == json.dumps({"error": "invalid_input"}) + "\n"
        assert "--write-exception" in captured.err
        assert "private-value" not in captured.out + captured.err
        assert ("--write-exception=-x" if value == "-x" else "one line") in captured.err
    else:
        assert captured.err == ""


def test_write_exceptions_with_a_read_only_fallback_are_refused_before_any_output(
    template: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """雛形の fallback は実際の codex の adapter（writes=false を -s read-only で起動する）。例外を実行できないので、
    --write-exception を付けた run は何も作らずに止まる。付けなければ従来どおり動く（#52）。"""
    module = ModuleType("tools.agent_runner.adapters.pi")
    module.__dict__["ADAPTER"] = EchoAdapter()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(_BODY)
    argv = ["--template", str(template), "run", "--workflow", "audit", "--repository", str(repository)]
    argv += ["--prompt-file", str(prompt), "--output-dir"]
    refused, plain = tmp_path / "refused", tmp_path / "plain"
    assert cli.main([*argv, str(refused), "--write-exception", "/x/reviews: run records"]) == 2
    assert capsys.readouterr().out == json.dumps({"error": "invalid_input"}) + "\n"
    assert not refused.exists()
    assert cli.main([*argv, str(plain)]) == 0
    assert plain.exists()
