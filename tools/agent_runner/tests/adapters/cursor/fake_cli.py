"""Fake `cursor-agent`: reads stdin, emits stream-json init/result, honors --fail-*."""

import json
import sys

SESSION = "a6b41ff6-5660-4000-8000-000000000002"


def main() -> int:
    sys.stdin.read()
    if "--fail-auth" in sys.argv:
        print("Error: not logged in. Run 'agent login' first.", file=sys.stderr)
        return 1
    if "--fail-trust" in sys.argv:
        print("Workspace Trust Required: run interactively to decide.")
        return 0
    print(json.dumps({"type": "system", "subtype": "init", "session_id": SESSION, "model": "Fake Model X"}))
    if "--fail-bad" in sys.argv:
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "session_id": SESSION}))
        return 0
    result = json.dumps({"answer": 42})
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "is_error": False,
                "result": result,
                "session_id": SESSION,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
