"""Fake claude CLI: record argv/stdin, replay a fixture stream, exit."""

import json
import sys
from pathlib import Path


def _value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def main() -> int:
    args = sys.argv[1:]
    stdin = sys.stdin.read()
    capture = _value(args, "--fake-capture")
    if capture is not None:
        Path(capture).write_text(json.dumps({"argv": args, "stdin": stdin}), encoding="utf-8")
    fixture = _value(args, "--fake-stdout")
    if fixture is not None:
        sys.stdout.write(Path(fixture).read_text(encoding="utf-8"))
    err = _value(args, "--fake-stderr")
    if err is not None:
        sys.stderr.write(Path(err).read_text(encoding="utf-8"))
    code = _value(args, "--fake-exit")
    return int(code) if code is not None else 0


sys.exit(main())
