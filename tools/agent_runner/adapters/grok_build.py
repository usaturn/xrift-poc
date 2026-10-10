"""Grok Build (`grok`) adapter: --prompt-file launch and single-JSON collection."""

import json
import re
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.adapters.skills import prompt_lines
from tools.agent_runner.models import Identity, Receipt, State

_AUTHENTICATION = re.compile(
    r"(?i)(not logged in|login required|please log in|unauthorized|invalid api key|"
    r"authentication (?:required|failed)|token expired)"
)
_APPROVAL = re.compile(r"(?i)(approval required|permission (?:required|denied)|awaiting approval)")


def _prompt(context: AdapterContext) -> str:
    parts = [context.request.prompt]
    if context.request.result_shape == "json":
        parts.append("Answer with a single JSON object as your final message.")
    parts.extend(prompt_lines(context.work_dir, context.agent.skill_sources, context.role.skills))
    return "\n\n".join(parts)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _locate(text: str) -> tuple[dict[str, object] | None, int, int]:
    """Return the first decodable outer object and its [start, end) span."""
    stripped = text.strip()
    try:
        whole: object = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(whole, dict) and whole:
            start = text.index("{")
            return cast(dict[str, object], whole), start, start + len(stripped)
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value:
            return cast(dict[str, object], value), index, index + end
    return None, -1, -1


def _json_object(text: str) -> dict[str, object] | None:
    value, _start, _end = _locate(text)
    return value


def _diagnostics(stdout: str, stderr: str, result: dict[str, object] | None, start: int, end: int) -> str:
    """CLI-emitted diagnostics: stderr and output outside the result document."""
    if result is None:
        return f"{stderr}\n{stdout}"
    parts = [stderr, stdout[:start], stdout[end:]]
    if "text" not in result:
        parts.append(json.dumps(result, ensure_ascii=False))
    return "\n".join(parts)


class _GrokBuildAdapter:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        agent = context.agent
        prompt_path = context.receipt_path.parent / "prompt.txt"
        prompt_path.write_text(_prompt(context), encoding="utf-8")
        mode = "bypassPermissions" if context.role.writes else "plan"
        argv = [
            *agent.command,
            "--prompt-file",
            str(prompt_path),
            "--output-format",
            "json",
            "--permission-mode",
            mode,
            "-m",
            agent.model,
        ]
        if agent.effort is not None:
            argv += ["--reasoning-effort", agent.effort]
        argv += agent.args
        return LaunchSpec(tuple(argv), b"")

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        stdout = _read(stdout_path)
        stderr = _read(stderr_path)

        result, start, end = _locate(stdout)
        session_id: str | None = None
        model: str | None = None
        text: str | None = None
        stop_reason: object = None
        if result is not None:
            sid = result.get("sessionId")
            if isinstance(sid, str):
                session_id = sid
            usage = result.get("modelUsage")
            if isinstance(usage, dict):
                keys = list(cast(dict[str, object], usage))
                if len(keys) == 1:
                    model = keys[0]
            final_text = result.get("text")
            if isinstance(final_text, str):
                text = final_text
            stop_reason = result.get("stopReason")
        # The single result document has no tool execution/read trace.
        identity = Identity(model=model, session_id=session_id)

        def failed(state: State) -> AdapterOutcome:
            return AdapterOutcome(identity=identity, state=state)

        diagnostics = _diagnostics(stdout, stderr, result, start, end)
        if _AUTHENTICATION.search(diagnostics):
            return failed("awaiting_authentication")
        if _APPROVAL.search(diagnostics):
            return failed("awaiting_approval")
        if result is None or text is None or stop_reason != "end_turn":
            return failed("response_failed")
        if context.request.result_shape == "json":
            payload = _json_object(text)
            if payload is None:
                return failed("response_failed")
        else:
            if not text.strip():
                return failed("response_failed")
            payload = text
        request = context.request
        receipt = Receipt(
            run_id=request.run_id,
            request_id=request.request_id,
            agent_id=request.agent_id,
            role=request.role,
            status="success",
            payload=payload,
        )
        return AdapterOutcome(receipt=receipt, identity=identity)


ADAPTER = _GrokBuildAdapter()
