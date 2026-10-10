"""Fake `codex exec`: reads stdin, emits JSONL events, honors --fail-auth/--fail-bad."""

import json
import os
import sys
from pathlib import Path

SESSION = "f0f0f0f0-5660-4000-8000-000000000001"


def _write_rollout() -> None:
    """Write a session rollout into the runtime home this process inherited."""
    home = os.environ.get("CODEX_HOME")
    if not home:
        return
    directory = Path(home) / "sessions" / "2026" / "10" / "07"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "turn_context",
        "payload": {
            "cli_version": "0.160.1",
            "model_provider": "openai",
            "model": "gpt-6.1-sol",
            "effort": "xhigh",
        },
    }
    name = f"rollout-2026-10-07T00-00-00-{SESSION}.jsonl"
    (directory / name).write_text(json.dumps(record) + "\n", encoding="utf-8")


def main() -> int:
    sys.stdin.read()
    if "--fail-auth" in sys.argv:
        print("Error: not logged in. Run 'codex login' first.", file=sys.stderr)
        return 1
    print(json.dumps({"type": "thread.started", "thread_id": SESSION}))
    _write_rollout()
    if "--fail-bad" in sys.argv:
        print(json.dumps({"type": "error", "message": "turn failed"}))
        return 0
    text = json.dumps({"answer": 42})
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
