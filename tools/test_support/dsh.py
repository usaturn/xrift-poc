"""Small v4 DSH log with the complete identity-bearing record shapes."""

import json
from pathlib import Path
from typing import Any


def write_session(
    home: Path,
    cwd: Path,
    *,
    session_id: str = "session-test",
    provider: str = "observed-provider",
    model: str = "observed-model",
    effort: str | None = "high",
    answered: bool = True,
) -> Path:
    """answered=False is a credential failure: the attempt is logged, no assistant message is."""
    path = home / "sessions" / "project" / session_id / "session.v4.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {"provider": provider, "model": model}
    if effort is not None:
        config["reasoningEffort"] = effort
    reply: dict[str, Any]
    if answered:
        reply = {
            "type": "assistant/message",
            "seq": 1,
            "time": 2,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "id": "message-test",
                    "content": [],
                    "source": {"kind": "model", "provider": provider, "model": model},
                },
            },
        }
    else:
        failure = {"kind": "error", "failure": {"code": "MISSING_CREDENTIAL", "message": "missing"}}
        reply = {
            "type": "assistant/attempt",
            "seq": 1,
            "time": 2,
            "data": {
                "turn": 1,
                "step": 1,
                "stream": [{"type": "chunk", "time": 2, "chunk": {"type": "finish", "reason": failure}}],
            },
        }
    rows: list[dict[str, Any]] = [
        {
            "type": "session",
            "version": 4,
            "id": session_id,
            "cwd": str(cwd),
            "createdAt": 1,
            "isSeeded": False,
            "delegationDepth": 0,
        },
        {"type": "request/header", "seq": 0, "time": 1, "data": {"header": {"config": config}, "reason": "initial"}},
        reply,
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path
