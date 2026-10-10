"""Workflow after_run commands: trusted argv executed after the CLI saves result.json.

They never change the batch state or the CLI status; each outcome is only reported.
Commands run without a shell under the same supervised process handling as attempts.
"""

import asyncio
import os
import threading
from pathlib import Path

from tools.agent_runner.adapters import LaunchSpec
from tools.agent_runner.environ import literal_environ, secret_values
from tools.agent_runner.process import execute

AFTER_RUN_DEADLINE_SECONDS = 60.0
RESULT_PLACEHOLDER = "{result}"


def _state(state: str | None, exit_code: int | None, stop_confirmed: bool) -> str:
    if state is None and exit_code == 0 and stop_confirmed:
        return "success"
    if state in {None, "response_failed"} and exit_code is not None:
        return "failed"
    return state or "failed"


def run_after_run(
    commands: list[list[str]], *, result_path: Path, cwd: Path, cancel: threading.Event
) -> list[dict[str, object]]:
    """Run each command in order, replacing {result} with the saved result path.

    The environment is the runner's startup environment without Git routing, with
    AGENT_RUNNER_DEPTH=1 so a command cannot start a nested run. Logs are kept next
    to result.json; argument values are never reported.
    """
    ambient = literal_environ()
    env = {name: value for name, value in ambient.items() if not name.startswith("GIT_")}
    env.setdefault("PATH", os.defpath)
    env["AGENT_RUNNER_DEPTH"] = "1"
    secrets = tuple(secret_values(ambient))
    reports: list[dict[str, object]] = []
    for index, command in enumerate(commands):
        argv = tuple(argument.replace(RESULT_PLACEHOLDER, str(result_path)) for argument in command)
        stdout_path = result_path.parent / f"after-run-{index}.stdout.log"
        stderr_path = result_path.parent / f"after-run-{index}.stderr.log"
        try:
            outcome = asyncio.run(
                execute(
                    LaunchSpec(argv, b""),
                    cwd=cwd,
                    env=env,
                    deadline_seconds=AFTER_RUN_DEADLINE_SECONDS,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    cancel=cancel,
                    secrets=secrets,
                )
            )
            state, exit_code, stop_confirmed = (
                _state(outcome.state, outcome.exit_code, outcome.stop_confirmed),
                outcome.exit_code,
                outcome.stop_confirmed,
            )
        except Exception:
            # An unexpected supervisor error must not change the CLI status; the stop is unproven.
            state, exit_code, stop_confirmed = "response_failed", None, False
        reports.append(
            {
                "index": index,
                "state": state,
                "exit_code": exit_code,
                "stop_confirmed": stop_confirmed,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
        )
    return reports
