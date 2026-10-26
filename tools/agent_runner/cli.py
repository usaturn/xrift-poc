"""JSON check/list/run commands; global --template/--config precede the command."""

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast, get_args

from tools.agent_runner import runner
from tools.agent_runner.adapters import load_adapter
from tools.agent_runner.config import ConfigError, check_depth, load_config, select_personal_config
from tools.agent_runner.hooks import run_after_run
from tools.agent_runner.models import BatchResult, Config, Runtime
from tools.agent_runner.runner import RunnerError, run_workflow

# The checkout holding the runner: the default template and the cwd of after_run commands.
_ROOT = Path(__file__).resolve().parents[2]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's normal diagnostics echo values (including secrets).
        if "--write-exception" in message:
            print(
                "--write-exception requires a value; use --write-exception=-x for values starting with '-'.",
                file=sys.stderr,
            )
        raise RunnerError("invalid_input")


class _Arguments(argparse.Namespace):
    template: Path | None
    config: Path | None
    command: str
    workflow: str
    repository: Path
    revision: str
    prompt_file: Path
    concurrency: int
    output_dir: Path | None
    assign: list[str]
    write_exception: list[str]
    writable_path: list[Path]
    private_review_refs: bool


def _write_exception(value: str) -> str:
    # Use the API's predicate rather than maintaining a second set of Unicode rules.
    if not runner._single_line(value):  # pyright: ignore[reportPrivateUsage]
        print(
            "--write-exception must be nonempty, have no surrounding whitespace, occupy one line, "
            "and contain no control characters.",
            file=sys.stderr,
        )
        raise RunnerError("invalid_input")
    return value


def _parse(argv: list[str] | None) -> _Arguments:
    parser = _Parser(description=__doc__)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("list")
    run = commands.add_parser("run")
    run.add_argument("--workflow", required=True)
    run.add_argument("--repository", type=Path, required=True)
    run.add_argument("--revision", default="HEAD")
    run.add_argument("--prompt-file", type=Path, required=True)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--assign", action="append", default=[])
    # run_workflow checks API inputs too, before anything is created.
    run.add_argument("--write-exception", action="append", type=_write_exception, default=[])
    # Mount permissions are explicit paths; a --write-exception line is never read as one.
    run.add_argument("--writable-path", action="append", type=Path, default=[])
    run.add_argument("--private-review-refs", action="store_true")
    return cast(_Arguments, parser.parse_args(argv))


def _assignments(values: list[str]) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for value in values:
        role, separator, names = value.partition("=")
        if not separator or not role or not names or role in assignments:
            raise RunnerError()
        agents = names.split(",")
        if any(not name for name in agents):
            raise RunnerError()
        assignments[role] = agents
    return assignments


def _inventory(config: Config) -> dict[str, object]:
    runtimes: list[dict[str, object]] = []
    supported: dict[str, bool] = {}
    for runtime in cast(tuple[str, ...], get_args(Runtime)):
        noninteractive = stdin = False
        try:
            adapter = load_adapter(runtime)
            noninteractive = adapter.capabilities.noninteractive is True
            stdin = adapter.capabilities.stdin is True
        except Exception:
            pass
        supported[runtime] = noninteractive and stdin
        runtimes.append(
            {"runtime": runtime, "supported": supported[runtime], "noninteractive": noninteractive, "stdin": stdin}
        )
    return {
        "schema_version": 1,
        "runtimes": runtimes,
        "agents": [
            {"agent_id": name, "runtime": agent.runtime, "supported": supported[agent.runtime]}
            for name, agent in config.agents.items()
        ],
        "roles": list(config.roles),
        "workflows": list(config.workflows),
    }


def _save(batch: BatchResult) -> Path:
    result_path = batch.artifact_dir / "result.json"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="result-", suffix=".tmp", dir=batch.artifact_dir, delete=False
        ) as stream:
            stream.write(batch.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, result_path)
    except OSError:
        raise RunnerError("output_io") from None
    return result_path


def _run(arguments: _Arguments, config: Config) -> int:
    try:
        prompt = arguments.prompt_file.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        raise RunnerError() from None
    cancel = threading.Event()

    def interrupt(signum: int, frame: FrameType | None) -> None:
        cancel.set()

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, interrupt)
    try:
        batch = run_workflow(
            config,
            arguments.workflow,
            prompt,
            arguments.repository,
            revision=arguments.revision,
            concurrency=arguments.concurrency,
            output_dir=arguments.output_dir,
            cancel=cancel,
            write_exceptions=arguments.write_exception,
            writable_paths=arguments.writable_path,
            private_review_refs=arguments.private_review_refs,
        )
        result_path = _save(batch)
        after_run = run_after_run(
            config.workflows[arguments.workflow].after_run, result_path=result_path, cwd=_ROOT, cancel=cancel
        )
        print(
            json.dumps(
                {
                    "run_id": batch.run_id,
                    "state": batch.state,
                    "artifact_dir": str(batch.artifact_dir),
                    "result_path": str(result_path),
                    "after_run": after_run,
                }
            )
        )
        if cancel.is_set():
            return 130
        return 0 if batch.state in {"success", "self"} else 1
    finally:
        signal.signal(signal.SIGINT, previous)


def main(argv: list[str] | None = None) -> int:
    """Return a CLI status, printing only JSON (apart from explicit --help)."""
    try:
        arguments = _parse(argv)
        check_depth()
        template = arguments.template or _ROOT / ".devcontainer/agent-runner.toml"
        personal = select_personal_config(arguments.config)
        is_run = arguments.command == "run"
        config = load_config(
            template,
            personal,
            _assignments(arguments.assign) if is_run else None,
            arguments.workflow if is_run else None,
        )
        if is_run:
            return _run(arguments, config)
        print(json.dumps(_inventory(config)))
        return 0
    except (ConfigError, RunnerError) as error:
        print(json.dumps({"error": error.category}))
        return 2
    except Exception:
        print(json.dumps({"error": "runner_failed"}))
        return 2
