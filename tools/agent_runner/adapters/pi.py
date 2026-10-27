"""Pi adapter: ``pi --print --mode json`` over a launcher such as piccd/pigs.

The prompt goes over stdin; stdout is NDJSON events. ``session`` reports the
session id, ``agent_end`` carries the final assistant message (model,
provider, thinkingLevel, stopReason, text parts), and ``agent_settled`` marks
clean completion. A zero exit code without a complete event stream is still a
failure. Only local parsing happens here; this module never starts a process.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.adapters.output import (
    answer_payload,
    failure_status,
    iter_strings,
    ndjson_events,
    optional_str,
    read_text,
    receipt_for,
)
from tools.agent_runner.adapters.skills import SKILL_MD_MENTION, located_versions
from tools.agent_runner.models import Identity, Payload, ReceiptStatus

_STOP_OK = {"stop", "endturn", "end_turn", "completed", "complete", "finished", "normal"}


def _last_assistant(messages: object) -> dict[str, object] | None:
    if not isinstance(messages, list):
        return None
    for message in reversed(cast(list[object], messages)):
        if isinstance(message, dict) and cast(dict[str, object], message).get("role") == "assistant":
            return cast(dict[str, object], message)
    return None


def _final_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        cast(str, part_map["text"])
        for part in cast(list[object], content)
        if isinstance(part, dict)
        and (part_map := cast(dict[str, object], part)).get("type") == "text"
        and isinstance(part_map.get("text"), str)
    )


def _skill_targets(events: list[dict[str, object]], sources: Iterable[str]) -> list[str]:
    """``--skill`` sources plus SKILL.md paths observed inside tool calls."""
    targets = list(sources)
    for event in events:
        for text in iter_strings(event):
            targets.extend(SKILL_MD_MENTION.findall(text))
    return targets


def _classify(
    context: AdapterContext, events: list[dict[str, object]], stderr: str, hint: ReceiptStatus | None
) -> tuple[ReceiptStatus, Payload, dict[str, object] | None]:
    types = {event.get("type") for event in events}
    if "agent_end" not in types or "agent_settled" not in types:
        return hint or "response_failed", None, None
    end = next(event for event in reversed(events) if event.get("type") == "agent_end")
    message = _last_assistant(end.get("messages"))
    if message is None:
        return hint or "response_failed", None, None
    reason = str(message.get("stopReason") or message.get("rawStopReason") or "").lower()
    if reason in _STOP_OK:
        status, payload = answer_payload(_final_text(message), context.role.result_shape)
        if status != "success" and hint is not None:
            return hint, None, message
        return status, payload, message
    if "interrupt" in reason or "abort" in reason or "cancel" in reason:
        return "interrupted", None, message
    return failure_status(_final_text(message)) or hint or "response_failed", None, message


def _identity(events: list[dict[str, object]], message: dict[str, object] | None, context: AdapterContext) -> Identity:
    empty: dict[str, object] = {}
    session = next((event for event in events if event.get("type") == "session"), empty)
    message = message or empty
    return Identity(
        model=optional_str(message.get("model")),
        provider=optional_str(message.get("provider")),
        effort=optional_str(message.get("thinkingLevel")),
        session_id=optional_str(session.get("id")),
        skill_versions=located_versions(
            _skill_targets(events, context.agent.skill_sources),
            context.work_dir,
            context.agent.skill_sources,
            from_work_dir=True,
        ),
    )


class Pi:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        agent = context.agent
        argv = [*agent.command, "--print", "--mode", "json", "--no-session", "--no-approve"]
        if agent.provider is not None:
            argv.extend(("--provider", agent.provider))
        argv.extend(("--model", agent.model))
        if agent.effort is not None:
            argv.extend(("--thinking", agent.effort))
        for source in agent.skill_sources:
            argv.extend(("--skill", source))
        argv.extend(agent.args)
        return LaunchSpec(argv=tuple(argv), stdin=context.request.prompt.encode("utf-8"))

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        events = list(ndjson_events(stdout_path))
        stderr = read_text(stderr_path)
        hint = failure_status(stderr, read_text(stdout_path))
        status, payload, message = _classify(context, events, stderr, hint)
        return AdapterOutcome(
            receipt=receipt_for(context.request, status, payload),
            identity=_identity(events, message, context),
        )


ADAPTER = Pi()
