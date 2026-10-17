"""Headless-shaped fake CLI; validates managed flags, writes private artifacts."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _home_cache(home: Path) -> None:
    # Shapes tools leave in HOME: uv cache links (to a file, a sibling and a
    # directory outside HOME), a FIFO, and Go's read-only module cache.
    entry = home / ".cache" / "uv" / "archive-v0" / "entry"
    (entry / "lib").mkdir(parents=True)
    (entry / "bin").mkdir()
    (entry / "bin" / "python").symlink_to(sys.executable)
    (entry / "lib64").symlink_to("lib")
    (home / ".cache" / "checkout").symlink_to(Path.cwd())
    os.mkfifo(home / "agent.fifo")
    module = home / "go" / "pkg" / "mod" / "example.com" / "m@v1"
    module.mkdir(parents=True)
    (module / "go.mod").write_text("module example.com/m\n")
    (module / "go.mod").chmod(0o444)
    module.chmod(0o555)
    module.parent.chmod(0o555)


def main() -> int:
    mode = sys.argv[1]
    args = sys.argv[2:]
    assert args[:2] == ["--profile", "headless"]
    assert args[-2:] == ["--json", "-"]
    prompt = sys.stdin.read()
    patch = json.loads(Path(args[args.index("--patch") + 1]).read_text())
    rows = {row["id"]: row for row in patch}
    route = next(iter(rows["llm-pi-ai"]["config"]["providers"].values()))
    key = os.environ.get(route["apiKeyEnv"], "")
    state = Path(os.environ["DSH_HOME"])
    home = Path(os.environ["HOME"])
    capture: dict[str, object] = {
        "prompt": prompt,
        "home": str(home),
        "dsh_home": str(state),
        "tmpdir": os.environ.get("TMPDIR"),
        "telemetry": os.environ["DSH_TELEMETRY_MODE"],
        "depth": os.environ["AGENT_RUNNER_DEPTH"],
        "parent_model": os.environ.get("PI_MODEL"),
        "key_present": bool(key),
    }
    if mode in {"spill", "hang"}:
        # Like DSH's subprocess-local spill of >64 KB command output under tmpdir().
        spill = Path(tempfile.mkdtemp(prefix="dsh-subprocess-")) / "output.log"
        spill.write_text("env dump\n" + "x" * 70000 + f"\nAPI_KEY={key}\n")
        capture["spill"] = str(spill)
    (state / "capture.json").write_text(json.dumps(capture, ensure_ascii=False))
    if mode == "home-link":
        home.rmdir()
        home.symlink_to(Path.cwd())
    else:
        (home / ".netrc").write_text(f"password {key}\n")
    if mode == "home-cache":
        _home_cache(home)
    if mode == "symlink":
        (state / "foreign-link").symlink_to(Path.cwd() / "README.md")
    elif mode == "fifo":
        os.mkfifo(state / "session.fifo")
    elif mode == "tmp-symlink":
        (Path(os.environ["TMPDIR"]) / "foreign-link").symlink_to(Path.cwd() / "README.md")
    elif mode == "tmp-fifo":
        os.mkfifo(Path(os.environ["TMPDIR"]) / "spill.fifo")
    elif mode == "root-link":
        # The whole runtime root now resolves into the checkout, which has its own home/.
        (Path.cwd() / "home").mkdir()
        (Path.cwd() / "home" / "keep.txt").write_text("keep\n")
        home.parent.rename(home.parent.with_name("dsh-moved"))
        home.parent.symlink_to(Path.cwd())
    else:
        (state / "session.jsonl").write_text(json.dumps({"tool": key}) + "\n")
    if mode in {"hang", "descendant"}:
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(600)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (Path.cwd() / "child.pid").write_text(str(child.pid))
        if mode == "hang":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(600)
    reason: dict[str, object] = {"kind": "completed"}
    text = json.dumps({"ok": True, "prompt": prompt, "secret": key})
    if mode == "auth":
        reason = {"kind": "error", "error": {"code": "MISSING_CREDENTIAL", "message": "missing"}}
        text = ""
    events: list[dict[str, object]] = [
        {"type": "session", "sessionId": "fake-observed", "cwd": str(Path.cwd())},
        {"type": "status", "phase": "turn_start", "turn": 1},
    ]
    if mode == "approval":
        events.append({"type": "tool_result", "callId": "call", "status": "error", "result": "APPROVAL_REQUIRED"})
    events.extend(
        [
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": reason},
            {"type": "final", "text": text},
        ]
    )
    for event in events:
        print(json.dumps(event, ensure_ascii=False), flush=True)
    if mode == "malformed":
        print("broken {")
    return 1 if mode in {"auth", "nonzero"} else 0


if __name__ == "__main__":
    sys.exit(main())
