"""Claude Code adapter: ``claude -p`` with ``--output-format stream-json``.

The prompt goes over stdin; stdout is NDJSON whose ``system``/``init`` event
reports the observed model, session, and CLI version, and whose ``result``
event carries the final text, error flag, and permission denials. Only local
parsing happens here; this module never starts a process.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.adapters.strict_json import decode
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
_INTERRUPT = re.compile(r"interrupt|abort|cancel", re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)\b[^\n]*\n(.*?)```", re.DOTALL)
_SKILL_FILE = "SKILL.md"
_SKILL_ROOTS = (".claude/skills", ".agents/skills", ".devin/skills")


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
            value = decode(candidate)
        except json.JSONDecodeError:
            continue
        except ValueError:
            # An invalid answer must not be rescued by a later fenced object.
            return None
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


def _skill_md(name: str, work_dir: Path, sources: Iterable[str]) -> Path | None:
    text = name.strip().strip("'\"")
    if not text:
        return None
    candidates: list[Path] = []
    direct = Path(text)
    if direct.is_absolute():
        candidates.extend((direct, direct / _SKILL_FILE))
    else:
        for source in sources:
            base = Path(source)
            candidates.extend((base / direct, base / direct / _SKILL_FILE, base / direct.name / _SKILL_FILE))
        candidates.extend(work_dir / root / text / _SKILL_FILE for root in _SKILL_ROOTS)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _skill_versions(names: Iterable[str], work_dir: Path, sources: Iterable[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in dict.fromkeys(names):
        path = _skill_md(name, work_dir, sources)
        if path is None:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        versions.setdefault(path.parent.name, f"sha256:{digest}")
    return versions


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


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _identity(events: list[dict[str, object]], names: list[str], context: AdapterContext) -> Identity:
    empty: dict[str, object] = {}
    init = next(
        (event for event in events if event.get("type") == "system" and event.get("subtype") == "init"),
        empty,
    )
    model = _str(init.get("model"))
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = _message(event)
        if message is not None and isinstance(message.get("model"), str):
            model = cast(str, message["model"])
    return Identity(
        model=model,
        session_id=_str(init.get("session_id")),
        cli_version=_str(init.get("claude_code_version")),
        skill_versions=_skill_versions(names, context.work_dir, context.agent.skill_sources),
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
        return _failure_status(stderr, _assistant_text(events)) or "response_failed", None
    if result.get("permission_denials"):
        return "awaiting_approval", None
    text = _str(result.get("result")) or ""
    if result.get("subtype") == "success" and not result.get("is_error"):
        return _payload(text, context.role.result_shape)
    if _INTERRUPT.search(str(result.get("subtype", ""))):
        return "interrupted", None
    return _failure_status(text, stderr) or "response_failed", None


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
        events = list(_events(stdout_path))
        names = _used_skills(events)
        status, payload = _classify(context, events, _text(stderr_path))
        return AdapterOutcome(
            receipt=_receipt(context.request, status, payload),
            identity=_identity(events, names, context),
        )


ADAPTER = ClaudeCode()
