"""Claude Code adapter: ``claude -p`` with ``--output-format stream-json``.

The prompt goes over stdin; stdout is NDJSON whose ``system``/``init`` event
reports the observed model, session, and CLI version, and whose ``result``
event carries the final text, error flag, and permission denials. Only local
parsing happens here; this module never starts a process.
"""

import re
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.adapters.output import (
    answer_payload,
    failure_status,
    ndjson_events,
    optional_str,
    read_text,
    receipt_for,
)
from tools.agent_runner.adapters.skills import located_versions
from tools.agent_runner.models import Identity, Payload, ReceiptStatus

_INTERRUPT = re.compile(r"interrupt|abort|cancel", re.IGNORECASE)


def _message(event: dict[str, object]) -> dict[str, object] | None:
    message = event.get("message")
    return cast(dict[str, object], message) if isinstance(message, dict) else None


def _used_skills(events: list[dict[str, object]]) -> list[str]:
    """Skill names actually invoked through a Skill tool_use block."""
    names: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = _message(event)
        if message is None:
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in cast(list[object], content):
            if not isinstance(block, dict):
                continue
            block_map = cast(dict[str, object], block)
            if block_map.get("type") != "tool_use" or block_map.get("name") != "Skill":
                continue
            inputs = block_map.get("input")
            if isinstance(inputs, dict) and isinstance(cast(dict[str, object], inputs).get("skill"), str):
                names.append(cast(str, cast(dict[str, object], inputs)["skill"]))
    return names


def _identity(events: list[dict[str, object]], names: list[str], context: AdapterContext) -> Identity:
    empty: dict[str, object] = {}
    init = next(
        (event for event in events if event.get("type") == "system" and event.get("subtype") == "init"),
        empty,
    )
    model = optional_str(init.get("model"))
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = _message(event)
        if message is not None and isinstance(message.get("model"), str):
            model = cast(str, message["model"])
    return Identity(
        model=model,
        session_id=optional_str(init.get("session_id")),
        cli_version=optional_str(init.get("claude_code_version")),
        skill_versions=located_versions(names, context.work_dir, context.agent.skill_sources, from_work_dir=False),
    )


def _assistant_text(events: list[dict[str, object]]) -> str:
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = _message(event)
        if message is None:
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(
            cast(str, block_map["text"])
            for block in cast(list[object], content)
            if isinstance(block, dict)
            and (block_map := cast(dict[str, object], block)).get("type") == "text"
            and isinstance(block_map.get("text"), str)
        )
        if text.strip():
            return text
    return ""


def _classify(context: AdapterContext, events: list[dict[str, object]], stderr: str) -> tuple[ReceiptStatus, Payload]:
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if result is None:
        return failure_status(stderr, _assistant_text(events)) or "response_failed", None
    if result.get("permission_denials"):
        return "awaiting_approval", None
    text = optional_str(result.get("result")) or ""
    if result.get("subtype") == "success" and not result.get("is_error"):
        return answer_payload(text, context.role.result_shape)
    if _INTERRUPT.search(str(result.get("subtype", ""))):
        return "interrupted", None
    return failure_status(text, stderr) or "response_failed", None


class ClaudeCode:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        agent = context.agent
        argv = [
            *agent.command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            agent.model,
        ]
        if agent.effort is not None:
            argv.extend(("--effort", agent.effort))
        argv.extend(("--dangerously-skip-permissions", *agent.args))
        return LaunchSpec(argv=tuple(argv), stdin=context.request.prompt.encode("utf-8"))

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        events = list(ndjson_events(stdout_path))
        names = _used_skills(events)
        status, payload = _classify(context, events, read_text(stderr_path))
        return AdapterOutcome(
            receipt=receipt_for(context.request, status, payload),
            identity=_identity(events, names, context),
        )


ADAPTER = ClaudeCode()
