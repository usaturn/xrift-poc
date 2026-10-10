"""Fake `grok`: reads --prompt-file, emits one JSON result, honors --fail-*."""

import json
import sys

SESSION = "6ff894db-5660-4000-8000-000000000003"


def main() -> int:
    prompt_file = sys.argv[sys.argv.index("--prompt-file") + 1]
    with open(prompt_file, encoding="utf-8") as stream:
        stream.read()
    if "--fail-auth" in sys.argv:
        print("Error: not logged in. Run 'grok login' first.", file=sys.stderr)
        return 1
    if "--fail-bad" in sys.argv:
        print("panic: something broke")
        return 0
    print(
        json.dumps(
            {
                "text": json.dumps({"answer": 42}),
                "stopReason": "end_turn",
                "sessionId": SESSION,
                "modelUsage": {"fake-grok-model": {"inputTokens": 1}},
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
