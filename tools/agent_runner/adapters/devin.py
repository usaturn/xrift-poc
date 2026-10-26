"""Devin CLI adapter: ``devin -p`` with ``--prompt-file`` and ``--export``.

``devin -p`` does not read stdin, so ``prepare`` writes the prompt into the
attempt directory and passes it by path. The run's stdout is the final answer;
``--export`` leaves an ATIF export that reports the observed model, CLI
version, and session id. Only local parsing happens here; this module never
starts a process.
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
_FENCE = re.compile(r"```(?:json|JSON)\b[^\n]*\n(.*?)```", re.DOTALL)
_SKILL_FILE = "SKILL.md"
_SKILL_ROOTS = (".claude/skills", ".agents/skills", ".devin/skills")
_SKILL_PATH = re.compile(r"[^\s\"'`]*SKILL\.md")
_PROMPT_FILE = "prompt.txt"
_EXPORT_FILE = "export.json"


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
            for text in _strings(call_map):
                targets.extend(_SKILL_PATH.findall(text))
            function = call_map.get("function")
            name = cast(dict[str, object], function).get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and "skill" in name.lower():
                arguments = cast(dict[str, object], function).get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed: object = json.loads(arguments)
                    except ValueError:
                        parsed = arguments
                    targets.extend(_strings(parsed))
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


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _identity(export: dict[str, object], context: AdapterContext) -> Identity:
    agent = export.get("agent")
    agent_map = cast(dict[str, object], agent) if isinstance(agent, dict) else {}
    return Identity(
        model=_str(agent_map.get("model_name")),
        session_id=_str(export.get("session_id")),
        cli_version=_str(agent_map.get("version")),
        skill_versions=_skill_versions(_skill_targets(export), context.work_dir, context.agent.skill_sources),
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
        stdout = _text(stdout_path)
        stderr = _text(stderr_path)
        hint = _failure_status(stderr)
        export = _export(context.receipt_path.parent / _EXPORT_FILE)
        if stdout.strip() and export is not None and _final_agent_message(export) is not None:
            status, payload = _payload(stdout, context.role.result_shape)
            if status != "success" and hint is not None:
                status, payload = hint, None
        else:
            status, payload = hint or "response_failed", None
        return AdapterOutcome(
            receipt=_receipt(context.request, status, payload),
            identity=_identity(export, context) if export is not None else Identity(),
        )


ADAPTER = Devin()
