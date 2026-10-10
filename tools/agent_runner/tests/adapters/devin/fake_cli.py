"""Fake devin CLI: record argv, echo --prompt-file content, write --export."""

import json
import sys
from pathlib import Path


def _value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def main() -> int:
    args = sys.argv[1:]
    sys.stdin.read()
    prompt_file = _value(args, "--prompt-file")
    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    capture = _value(args, "--fake-capture")
    if capture is not None:
        Path(capture).write_text(json.dumps({"argv": args, "prompt": prompt}), encoding="utf-8")
    export = _value(args, "--export")
    fixture = _value(args, "--fake-export")
    if export is not None and fixture is not None:
        Path(export).write_text(Path(fixture).read_text(encoding="utf-8"), encoding="utf-8")
    out = _value(args, "--fake-stdout")
    if out is not None:
        sys.stdout.write(Path(out).read_text(encoding="utf-8"))
    err = _value(args, "--fake-stderr")
    if err is not None:
        sys.stderr.write(Path(err).read_text(encoding="utf-8"))
    code = _value(args, "--fake-exit")
    return int(code) if code is not None else 0


sys.exit(main())
