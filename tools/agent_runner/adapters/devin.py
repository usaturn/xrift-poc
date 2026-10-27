"""Devin CLI adapter: ``devin -p`` with ``--prompt-file`` and ``--export``.

``devin -p`` does not read stdin, so ``prepare`` writes the prompt into the
attempt directory and passes it by path. The run's stdout is the final answer;
``--export`` leaves an ATIF export that reports the observed model, CLI
version, and session id. Only local parsing happens here; this module never
starts a process.
"""

import json
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.adapters.output import (
    answer_payload,
    failure_status,
    iter_strings,
    optional_str,
    read_text,
    receipt_for,
)
from tools.agent_runner.adapters.skills import SKILL_MD_MENTION, located_versions
from tools.agent_runner.models import Identity

_PROMPT_FILE = "prompt.txt"
_EXPORT_FILE = "export.json"


def _skill_targets(export: dict[str, object]) -> list[str]:
    """Skill evidence inside tool calls: SKILL.md paths and skill invocations."""
    targets: list[str] = []
    steps = export.get("steps")
    if not isinstance(steps, list):
        return targets
    for step in cast(list[object], steps):
        if not isinstance(step, dict):
            continue
        calls = cast(dict[str, object], step).get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in cast(list[object], calls):
            if not isinstance(call, dict):
                continue
            call_map = cast(dict[str, object], call)
            for text in iter_strings(call_map):
                targets.extend(SKILL_MD_MENTION.findall(text))
            function = call_map.get("function")
            name = cast(dict[str, object], function).get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and "skill" in name.lower():
                arguments = cast(dict[str, object], function).get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed: object = json.loads(arguments)
                    except ValueError:
                        parsed = arguments
                    targets.extend(iter_strings(parsed))
    return targets


def _export(path: Path) -> dict[str, object] | None:
    try:
        value: object = json.loads(path.read_bytes())
    except OSError, ValueError:
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _final_agent_message(export: dict[str, object]) -> str | None:
    steps = export.get("steps")
    if not isinstance(steps, list):
        return None
    for step in reversed(cast(list[object], steps)):
        if not isinstance(step, dict) or cast(dict[str, object], step).get("source") != "agent":
            continue
        message = cast(dict[str, object], step).get("message")
        return message if isinstance(message, str) and message.strip() else None
    return None


def _identity(export: dict[str, object], context: AdapterContext) -> Identity:
    agent = export.get("agent")
    agent_map = cast(dict[str, object], agent) if isinstance(agent, dict) else {}
    return Identity(
        model=optional_str(agent_map.get("model_name")),
        session_id=optional_str(export.get("session_id")),
        cli_version=optional_str(agent_map.get("version")),
        skill_versions=located_versions(
            _skill_targets(export), context.work_dir, context.agent.skill_sources, from_work_dir=True
        ),
    )


class Devin:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        agent = context.agent
        attempt = context.receipt_path.parent
        prompt_file = attempt / _PROMPT_FILE
        prompt_file.write_text(context.request.prompt, encoding="utf-8")
        argv = (
            *agent.command,
            "-p",
            "--prompt-file",
            str(prompt_file),
            "--export",
            str(attempt / _EXPORT_FILE),
            "--model",
            agent.model,
            "--permission-mode",
            "dangerous",
            "--respect-workspace-trust",
            "false",
            *agent.args,
        )
        return LaunchSpec(argv=argv, stdin=b"")

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        stdout = read_text(stdout_path)
        stderr = read_text(stderr_path)
        hint = failure_status(stderr)
        export = _export(context.receipt_path.parent / _EXPORT_FILE)
        if stdout.strip() and export is not None and _final_agent_message(export) is not None:
            status, payload = answer_payload(stdout, context.role.result_shape)
            if status != "success" and hint is not None:
                status, payload = hint, None
        else:
            status, payload = hint or "response_failed", None
        return AdapterOutcome(
            receipt=receipt_for(context.request, status, payload),
            identity=_identity(export, context) if export is not None else Identity(),
        )


ADAPTER = Devin()
