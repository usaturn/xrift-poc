"""Pi adapter: ``pi --print --mode json`` over a launcher such as piccd/pigs.

The prompt goes over stdin; stdout is NDJSON events. ``session`` reports the
session id, ``agent_end`` carries the final assistant message (model,
provider, thinkingLevel, stopReason, text parts), and ``agent_settled`` marks
clean completion. A zero exit code without a complete event stream is still a
failure. Only local parsing happens here; this module never starts a process.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.models import Identity, Payload, Receipt, ReceiptStatus, Request, ResultShape

_AUTH = re.compile(
    r"(?:not logged in|not authenticated|sign[ -]?in|log ?in|unauthorized|forbidden"
    r"|authentication|invalid api key|missing api key|no api key|api key is invalid"
    r"|oauth|\b40[13]\b|credentials? (?:missing|invalid|expired))",
    re.IGNORECASE,
)
_APPROVAL = re.compile(
    r"(?:workspace trust|trust this|awaiting approval|requires? approval"
    r"|permission (?:denied|required)|grant access|confirm(?:ed)? (?:to )?continue)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```(?:json|JSON)\b[^\n]*\n(.*?)```", re.DOTALL)
_SKILL_FILE = "SKILL.md"
_SKILL_ROOTS = (".claude/skills", ".agents/skills", ".devin/skills")
_SKILL_PATH = re.compile(r"[^\s\"'`]*SKILL\.md")
_STOP_OK = {"stop", "endturn", "end_turn", "completed", "complete", "finished", "normal"}


def _events(path: Path) -> Iterator[dict[str, object]]:
    try:
        data = path.read_bytes()
    except OSError:
        return
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield cast(dict[str, object], value)


def _text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _json_object(text: str) -> dict[str, object] | None:
    candidates = [text.strip()]
    candidates.extend(block.strip() for block in _FENCE.findall(text))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value: object = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return cast(dict[str, object], value)
    return None


def _payload(text: str, shape: ResultShape) -> tuple[ReceiptStatus, Payload]:
    if shape == "text":
        return ("success", text) if text.strip() else ("response_failed", None)
    parsed = _json_object(text)
    return ("success", parsed) if parsed is not None else ("response_failed", None)


def _failure_status(*texts: str) -> ReceiptStatus | None:
    combined = "\n".join(text for text in texts if text)
    if not combined.strip():
        return None
    if _AUTH.search(combined):
        return "awaiting_authentication"
    if _APPROVAL.search(combined):
        return "awaiting_approval"
    return None


def _receipt(request: Request, status: ReceiptStatus, payload: Payload = None) -> Receipt:
    return Receipt(
        run_id=request.run_id,
        request_id=request.request_id,
        agent_id=request.agent_id,
        role=request.role,
        status=status,
        payload=payload,
    )


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in cast(dict[str, object], node).values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in cast(list[object], node):
            yield from _strings(value)


def _skill_md(target: str, work_dir: Path, sources: Iterable[str]) -> Path | None:
    text = target.strip().strip("'\"")
    if not text:
        return None
    candidates: list[Path] = []
    direct = Path(text)
    if direct.is_absolute():
        candidates.extend((direct, direct / _SKILL_FILE))
    else:
        if direct.name == _SKILL_FILE:
            candidates.append(work_dir / direct)
        for source in sources:
            base = Path(source)
            candidates.extend((base / direct, base / direct / _SKILL_FILE, base / direct.name / _SKILL_FILE))
        candidates.extend(work_dir / root / text / _SKILL_FILE for root in _SKILL_ROOTS)
        candidates.append(work_dir / direct)
        if direct.name != _SKILL_FILE:
            candidates.append(work_dir / direct / _SKILL_FILE)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _skill_versions(targets: Iterable[str], work_dir: Path, sources: Iterable[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for target in dict.fromkeys(targets):
        path = _skill_md(target, work_dir, sources)
        if path is None:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        versions.setdefault(path.parent.name, f"sha256:{digest}")
    return versions


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
        for text in _strings(event):
            targets.extend(_SKILL_PATH.findall(text))
    return targets


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
        status, payload = _payload(_final_text(message), context.role.result_shape)
        if status != "success" and hint is not None:
            return hint, None, message
        return status, payload, message
    if "interrupt" in reason or "abort" in reason or "cancel" in reason:
        return "interrupted", None, message
    return _failure_status(_final_text(message)) or hint or "response_failed", None, message


def _identity(events: list[dict[str, object]], message: dict[str, object] | None, context: AdapterContext) -> Identity:
    empty: dict[str, object] = {}
    session = next((event for event in events if event.get("type") == "session"), empty)
    message = message or empty
    return Identity(
        model=_str(message.get("model")),
        provider=_str(message.get("provider")),
        effort=_str(message.get("thinkingLevel")),
        session_id=_str(session.get("id")),
        skill_versions=_skill_versions(
            _skill_targets(events, context.agent.skill_sources),
            context.work_dir,
            context.agent.skill_sources,
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
        events = list(_events(stdout_path))
        stderr = _text(stderr_path)
        hint = _failure_status(stderr, _text(stdout_path))
        status, payload, message = _classify(context, events, stderr, hint)
        return AdapterOutcome(
            receipt=_receipt(context.request, status, payload),
            identity=_identity(events, message, context),
        )


ADAPTER = Pi()
