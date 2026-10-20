"""Standard DSH headless profile, isolated from user profiles and plugins.

The CLI owns profile initialization; no plugin install or subprocess runs here.
The JSONL projection supplies sessionId; the bound durable session supplies
observed provider/model/effort. Exit and descendant stop status are independently
enforced by the runner, not inferred from ``final``.
HOME is a disposable cache the runner removes after stop. State, the patch and
regular files in a private TMPDIR (DSH spills large command output there) are
retained redacted; links and special files tools leave in TMPDIR are unlinked.
"""

import json
import re
import sys
import uuid
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters import (
    AdapterContext,
    AdapterOutcome,
    Capabilities,
    LaunchSpec,
)
from tools.agent_runner.adapters.skills import prompt_lines
from tools.agent_runner.dsh_identity import read_session_identity, read_session_tail
from tools.agent_runner.environ import literal_environ
from tools.agent_runner.models import Diagnosis, DiagnosisEnding, Identity, Payload, Receipt, ReceiptStatus

_KEYS = {"commandcode": "COMMANDCODE_API_KEY", "opencode-go": "OPENCODE_API_KEY"}
_AUTH = re.compile(
    r"MISSING_CREDENTIAL|INVALID_CREDENTIAL|UNAUTHORIZED|\b40[13]\b|invalid api key",
    re.IGNORECASE,
)
_APPROVAL = re.compile(
    r"APPROVAL_REQUIRED|requires? approval|approval.*(?:reject|unavailable|cancel)"
    r"|the user rejected (?:tool|escalating)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```json\b[^\n]*\n(.*?)```", re.IGNORECASE | re.DOTALL)


def _constant(value: str) -> object:
    raise ValueError("nonfinite JSON")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode(text: str) -> object:
    return json.loads(text, parse_constant=_constant, object_pairs_hook=_object)


def _json_payload(text: str) -> dict[str, object] | None:
    # Same candidates and order as the other adapters: the whole text, then each
    # ```json fence. The first object decides; an empty one is still a failure.
    for candidate in (text, *_FENCE.findall(text)):
        try:
            value = _decode(candidate)
        except ValueError, RecursionError:
            continue
        if isinstance(value, dict):
            return cast(dict[str, object], value) or None
    return None


def _diagnostic(text: str) -> ReceiptStatus:
    if _AUTH.search(text):
        return "awaiting_authentication"
    if _APPROVAL.search(text):
        return "awaiting_approval"
    return "response_failed"


def _intermediate_event_valid(event: dict[str, object], active: int | None) -> bool:
    if active is None:
        return False
    kind = event.get("type")
    if kind in {"text", "thinking"}:
        return isinstance(event.get("text"), str)
    if kind == "status":
        step = event.get("step")
        return (
            event.get("phase") in {"step_start", "step_end"}
            and type(event.get("turn")) is int
            and event["turn"] == active
            and type(step) is int
            and step > 0
        )
    if kind in {"tool_call", "tool_result"}:
        if not isinstance(event.get("callId"), str) or not event["callId"]:
            return False
        if kind == "tool_call":
            return (
                isinstance(event.get("tool"), str)
                and bool(event["tool"])
                and ("input" in event or event.get("truncated") is True)
            )
        return event.get("status") in {"completed", "error"} and isinstance(event.get("result"), str)
    return False


def _classify(events: list[dict[str, object]], shape: str) -> tuple[ReceiptStatus, Payload]:
    # Error events and error tool results are diagnostics, never answer text.
    for event in events:
        if event.get("type") == "error":
            return _diagnostic(str(event.get("code", "")) + " " + str(event.get("message", ""))), None
        if event.get("type") == "tool_result" and event.get("status") == "error":
            # Tool errors are free text the agent can recover from (paths, line counts);
            # only an approval rejection ends the attempt. Authentication is read from
            # error events, the turn_end reason and, when the stream only reads as
            # response_failed, stderr.
            if _APPROVAL.search(str(event.get("result", ""))):
                return "awaiting_approval", None
        if event.get("type") == "status" and event.get("phase") == "turn_end":
            reason = event.get("reason")
            if not isinstance(reason, dict):
                return "response_failed", None
            reason = cast(dict[str, object], reason)
            if reason.get("kind") == "aborted":
                return "interrupted", None
            if reason.get("kind") != "completed":
                return _diagnostic(json.dumps(reason)), None
    if len(events) < 4 or events[0].get("type") != "session" or events[-1].get("type") != "final":
        return "response_failed", None
    if not isinstance(events[0].get("sessionId"), str) or not events[0]["sessionId"]:
        return "response_failed", None
    active: int | None = None
    ended: set[int] = set()
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind in {"session", "final"}:
            if index != (0 if kind == "session" else len(events) - 1) or event.get("truncated"):
                return "response_failed", None
        elif kind == "status" and event.get("phase") in {"turn_start", "turn_end"}:
            turn = event.get("turn")
            if type(turn) is not int or turn < 1 or event.get("truncated"):
                return "response_failed", None
            if event["phase"] == "turn_start":
                if active is not None or turn in ended:
                    return "response_failed", None
                active = turn
            else:
                if active != turn:
                    return "response_failed", None
                ended.add(turn)
                active = None
        elif not _intermediate_event_valid(event, active):
            return "response_failed", None
    if not ended or active is not None:
        return "response_failed", None
    text = events[-1].get("text")
    if not isinstance(text, str) or not text.strip():
        return "response_failed", None
    if shape == "text":
        return "success", text
    payload = _json_payload(text)
    return ("success", payload) if payload is not None else ("response_failed", None)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _failure(ending: DiagnosisEnding, details: dict[str, object], reason: str | None = None) -> dict[str, object]:
    status = details.get("status")
    return {
        "ending": ending,
        "reason": reason,
        "code": _text(details.get("code")),
        "status": status if type(status) is int else None,
        "message": _text(details.get("message")),
    }


def _diagnose(stdout_path: Path, stderr_path: Path, context: AdapterContext, session: str | None) -> Diagnosis:
    """How the attempt ended (#96): counts, labels and the failure's code/status/message only.

    Independent of _classify, which still decides the state. The message head is
    redacted and cut by the runner; no answer, tool or stderr text is copied.
    """
    events: list[dict[str, object]] = []
    unreadable = False
    for line in stdout_path.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            value = _decode(line)
        except ValueError, RecursionError:
            value = None
        if not isinstance(value, dict):
            unreadable = True
            break
        events.append(cast(dict[str, object], value))
    steps = tool_calls = 0
    tokens: int | None = None
    ended: dict[str, object] | None = None
    completed = False
    for event in events:
        kind, phase = event.get("type"), event.get("phase")
        if kind == "tool_call":
            tool_calls += 1
        elif kind == "status" and phase == "step_start":
            steps += 1
        elif kind == "status" and phase == "step_end" and isinstance(event.get("usage"), dict):
            count = cast(dict[str, object], event["usage"]).get("inputTokens")
            if type(count) is int and count >= 0:
                tokens = count
        if ended is not None:
            continue
        # The first failure in stream order decides, as in _classify.
        if kind == "error":
            ended = _failure("error_event", event)
        elif kind == "status" and phase == "turn_end":
            reason = event.get("reason")
            details = cast(dict[str, object], reason) if isinstance(reason, dict) else {}
            if details.get("kind") == "completed":
                completed = True
            else:
                error = details.get("error")
                ended = _failure(
                    "turn_end",
                    cast(dict[str, object], error) if isinstance(error, dict) else {},
                    _text(details.get("kind")),
                )
    if ended is None:
        finished = completed and bool(events) and events[-1].get("type") == "final"
        ended = {"ending": "unreadable" if unreadable else "completed" if finished else "incomplete"}
    last = events[-1] if events else None
    label = None if last is None else last.get("phase") if last.get("type") == "status" else last.get("type")
    try:
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr = ""
    tail = (
        read_session_tail(context.receipt_path.parent / "dsh" / "state", context.work_dir, session)
        if session
        else None
    )
    return Diagnosis.model_validate(
        {
            **ended,
            "steps": steps,
            "tool_calls": tool_calls,
            "last_event": _text(label),
            "last_input_tokens": tokens,
            "last_record": tail[0] if tail else None,
            "last_record_at": tail[1] if tail else None,
            "stderr_authentication": bool(_AUTH.search(stderr)),
            "stderr_approval": bool(_APPROVAL.search(stderr)),
        }
    )


class Dsh:
    capabilities = Capabilities()

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        agent = context.agent
        if agent.provider not in _KEYS or agent.args:
            raise ValueError("unsupported DSH launch configuration")
        provider = agent.provider
        root = context.receipt_path.parent / "dsh"
        root.mkdir(mode=0o700)
        home, dsh_home, tmp = root / "home", root / "state", root / "tmp"
        for directory in (home, dsh_home, tmp):
            directory.mkdir(mode=0o700)
        # A fresh reference prevents DSH from falling back to a project's .env
        # or a shared credential store under the original provider key name.
        key_ref = f"AGENT_RUNNER_DSH_{uuid.uuid4().hex.upper()}_API_KEY"
        key_name = _KEYS[provider]
        inherited = context.environment if context.environment is not None else literal_environ()
        key = inherited.get(key_name, "") if key_name in agent.env else ""
        model: dict[str, object] = {"id": agent.model, "contextWindow": 1000000}
        selection: dict[str, object] = {"provider": provider, "model": agent.model}
        if agent.effort is not None:
            model["reasoningEfforts"] = {agent.effort: agent.effort}
            selection["reasoningEffort"] = agent.effort
        route: dict[str, object] = {
            "apiKeyEnv": key_ref,
            "models": [model],
            "retryPolicy": {"mode": "normal", "maxRetries": 0},
        }
        if provider == "commandcode":
            route.update(
                api="openai-completions",
                baseURL="https://api.commandcode.ai/provider/v1",
                compat={"thinkingFormat": "deepseek"},
            )
        runner_command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "dsh_sandbox.py"),
            "--workspace",
            str(context.work_dir.resolve(strict=True)),
            "--read-root",
            str(context.receipt_path.parent.resolve(strict=True)),
            *[argument for path in (home, tmp, *context.sandbox_write_paths) for argument in ("--grant", str(path))],
            "--",
        ]
        patch = [
            {
                "id": "sandbox",
                "config": {
                    "runnerCommand": runner_command,
                    "runnerFailureSignatures": ["dsh-sandbox:", "bwrap:"],
                },
            },
            {"id": "llm-pi-ai", "config": {"providers": {provider: route}}},
            {"id": "agent-default-model", "config": selection},
            {"id": "approval", "config": {"policy": "never"}},
            {
                "id": "permission",
                "config": {
                    "presets": {"runner": {"sandbox": "workspace-write", "approval": "never"}},
                    "defaultPreset": "runner",
                },
            },
            {
                "id": "sandbox-policy",
                "config": {
                    "mode": "workspace-write",
                    "workspaceRoot": str(context.work_dir),
                },
            },
            {
                "id": "session-persistence-jsonl",
                "config": {"root": str(dsh_home / "sessions"), "compression": "none"},
            },
            {"id": "session-title-llm", "disabled": True},
            {"id": "plugin-package-inventory-deepseek", "config": {"enabled": False}},
        ]
        patch_path = root / "provider.patch.yml"
        patch_path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
        skill_lines = prompt_lines(context.work_dir, agent.skill_sources, context.role.skills)
        prompt = context.request.prompt
        if skill_lines:
            prompt += "\n\n" + "\n".join(skill_lines)
        return LaunchSpec(
            argv=(
                *agent.command,
                "--profile",
                "headless",
                "--patch",
                str(patch_path),
                "--json",
                "-",
            ),
            stdin=prompt.encode("utf-8"),
            env={
                "HOME": str(home),
                "DSH_HOME": str(dsh_home),
                "TMPDIR": str(tmp),
                "DSH_TELEMETRY_MODE": "DISABLED",
                "DSH_TELEMETRY_DISABLED": "1",
                "DSH_PERMISSION_MODE": "workspace-write",
                "UV_CACHE_DIR": str(home / "uv-cache"),
                "UV_PROJECT_ENVIRONMENT": str(home / "venv"),
                "UV_PYTHON_DOWNLOADS": "manual",
                "UV_PYTHON_PREFERENCE": "system",
                key_ref: key,
            },
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        events: list[dict[str, object]] = []
        try:
            for line in stdout_path.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                value = _decode(line)
                if not isinstance(value, dict):
                    raise TypeError("invalid event")
                events.append(cast(dict[str, object], value))
            status, payload = _classify(events, context.role.result_shape)
        except OSError, UnicodeError, ValueError, TypeError, RecursionError:
            status, payload = "response_failed", None
        if status == "response_failed":
            try:
                status = _diagnostic(stderr_path.read_text(encoding="utf-8"))
            except OSError, UnicodeError:
                pass
        session = events[0].get("sessionId") if events and events[0].get("type") == "session" else None
        identity = Identity(
            session_id=session if isinstance(session, str) and not events[0].get("truncated") else None
        )
        if identity.session_id:
            try:
                identity = read_session_identity(
                    context.receipt_path.parent / "dsh" / "state", context.work_dir, identity.session_id
                )
            except FileNotFoundError:
                pass  # Generic uses may have no durable model observation; review callers reject unknown.
            except OSError, ValueError, KeyError, TypeError:
                # An unproven route voids a success; a failure (a key-less attempt logs no
                # assistant message) keeps its classification and the session-only identity.
                if status == "success":
                    status, payload = "response_failed", None
        try:
            diagnosis = _diagnose(stdout_path, stderr_path, context, identity.session_id)
        except Exception:
            # The summary is best effort; it never changes the classification above.
            diagnosis = None
        return AdapterOutcome(
            receipt=Receipt(
                run_id=context.request.run_id,
                request_id=context.request.request_id,
                agent_id=context.request.agent_id,
                role=context.request.role,
                status=status,
                payload=payload,
            ),
            identity=identity,
            diagnosis=diagnosis,
        )


ADAPTER = Dsh()
