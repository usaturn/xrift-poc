"""Codex (`codex exec`) adapter: JSONL events plus session rollout identity."""

import os
import re
import shlex
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.adapters.output import embedded_object, json_event, read_text, receipt_for
from tools.agent_runner.adapters.skills import SKILL_MD_PATH, prompt_lines, verified_versions
from tools.agent_runner.models import Identity, State

_AUTHENTICATION = re.compile(
    r"(?i)(not logged in|login required|please log in|please run ['\"`]?\S* login|"
    r"unauthorized|invalid api key|authentication (?:required|failed)|token expired)"
)


def _prompt(context: AdapterContext) -> str:
    parts = [context.request.prompt]
    if context.request.result_shape == "json":
        parts.append("Answer with a single JSON object as your final message.")
    parts.extend(prompt_lines(context.work_dir, context.agent.skill_sources, context.role.skills, prefix="$"))
    return "\n\n".join(parts)


def _read_evidence(event: dict[str, object]) -> list[tuple[str, str]]:
    """Successful simple cat commands with their observed output, as (path, content)."""
    if event.get("type") != "item.completed":
        return []
    item = event.get("item")
    if not isinstance(item, dict):
        return []
    entry = cast(dict[str, object], item)
    if entry.get("type") != "command_execution" or entry.get("status") != "completed" or entry.get("exit_code") != 0:
        return []
    command = entry.get("command")
    if not isinstance(command, str):
        return []
    try:
        words = shlex.split(command)
        if len(words) == 3 and words[:2] == ["/bin/bash", "-lc"]:
            words = shlex.split(words[2])
    except ValueError:
        return []
    # Deliberately do not interpret compound commands, options or shell expansions.
    if len(words) != 2 or words[0] != "cat" or not SKILL_MD_PATH.fullmatch(words[1]):
        return []
    if any(character in words[1] for character in "$`*?[]{}~;|&()<>"):
        return []
    output = entry.get("aggregated_output")
    if not isinstance(output, str):
        return []
    return [(words[1], output)]


def _skill_versions(*texts: str) -> dict[str, str]:
    """Version map from reads whose observed output still equals the present bytes."""
    evidence: list[tuple[str, str]] = []
    for text in texts:
        for line in text.splitlines():
            event = json_event(line)
            if event is None:
                continue
            evidence.extend(_read_evidence(event))
    return verified_versions(evidence)


def _diagnostics(stdout: str, stderr: str) -> str:
    """CLI-emitted diagnostics: stderr, non-JSON stdout lines, and failure events."""
    parts = [stderr]
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json_event(line)
        if event is None or event.get("type") in {"error", "turn.failed"}:
            parts.append(line)
    return "\n".join(parts)


def _rollout(session_id: str | None) -> str:
    if not session_id:
        return ""
    homes: list[Path] = []
    if codex_home := os.environ.get("CODEX_HOME"):
        homes.append(Path(codex_home))
    if home := os.environ.get("HOME"):
        homes.append(Path(home) / ".codex")
    for root in homes:
        hits = sorted(root.glob(f"sessions/**/rollout-*{session_id}.jsonl"))
        if hits:
            return read_text(hits[-1])
    return ""


def _rollout_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        record = json_event(line)
        if record is None:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        record_payload = cast(dict[str, object], payload)
        for key in ("cli_version", "model_provider", "model", "effort"):
            if key not in fields:
                value = record_payload.get(key)
                if isinstance(value, str) and value:
                    fields[key] = value
    return fields


class _CodexAdapter:
    # -s read-only denies every write, including the caller's write exceptions (#52).
    capabilities = Capabilities(enforces_read_only=True)

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        agent = context.agent
        sandbox = "workspace-write" if context.role.writes else "read-only"
        argv = [*agent.command, "--json", "-s", sandbox, "-m", agent.model]
        if agent.effort is not None:
            argv += ["-c", f"model_reasoning_effort={agent.effort}"]
        argv += [*agent.args, "-"]
        return LaunchSpec(tuple(argv), _prompt(context).encode("utf-8"))

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        stdout = read_text(stdout_path)
        stderr = read_text(stderr_path)
        events = [e for e in (json_event(line) for line in stdout.splitlines()) if e is not None]

        session_id: str | None = None
        for event in events:
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str):
                    session_id = thread_id
                    break
        rollout_text = _rollout(session_id)
        fields = _rollout_fields(rollout_text)
        identity = Identity(
            model=fields.get("model"),
            provider=fields.get("model_provider"),
            effort=fields.get("effort"),
            session_id=session_id,
            cli_version=fields.get("cli_version"),
            skill_versions=_skill_versions(rollout_text, stdout),
        )

        def failed(state: State) -> AdapterOutcome:
            return AdapterOutcome(identity=identity, state=state)

        if _AUTHENTICATION.search(_diagnostics(stdout, stderr)):
            return failed("awaiting_authentication")
        if any(e.get("type") in {"error", "turn.failed"} for e in events):
            return failed("response_failed")
        text: str | None = None
        for event in reversed(events):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            if entry.get("type") != "agent_message":
                continue
            candidate = entry.get("text")
            if isinstance(candidate, str):
                text = candidate
                break
        if text is None:
            return failed("response_failed")
        if context.request.result_shape == "json":
            payload = embedded_object(text)
            if payload is None:
                return failed("response_failed")
        else:
            if not text.strip():
                return failed("response_failed")
            payload = text
        return AdapterOutcome(receipt=receipt_for(context.request, "success", payload), identity=identity)


ADAPTER = _CodexAdapter()
