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
) -> Path:
    path = home / "sessions" / "project" / session_id / "session.v4.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {"provider": provider, "model": model}
    if effort is not None:
        config["reasoningEffort"] = effort
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
        {
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
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path
