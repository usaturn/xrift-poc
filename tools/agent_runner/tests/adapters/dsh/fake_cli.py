"""Headless-shaped fake CLI; validates managed flags, writes private artifacts."""

import importlib.util
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


_PYTEST_CASE = '''"""Synthetic passing test; no credentials or external files are read."""

import os
from pathlib import Path


def test_tmp_path(tmp_path: Path) -> None:
    (tmp_path / "clean.txt").write_text("synthetic clean fixture\\n")
    if os.environ["FAKE_PYTEST_FIXTURES"] == "1":
        # Like the repository's safety fixtures: links out of tmp_path and a FIFO.
        outside = Path(os.environ["FAKE_OUTSIDE"])
        (tmp_path / "outside-file").symlink_to(outside / "README.md")
        (tmp_path / "outside-dir").symlink_to(outside)
        os.mkfifo(tmp_path / "fixture.fifo")
    assert (tmp_path / "clean.txt").is_file()
'''


def _run_pytest(fixtures: bool) -> dict[str, object]:
    # Real pytest with its default retention: pytest-current and <test>current links stay in TMPDIR.
    case = Path(tempfile.mkdtemp(prefix="pytest-input-"))
    (case / "pytest.ini").write_text("[pytest]\n")
    (case / "probe_case.py").write_text(_PYTEST_CASE)
    if fixtures:
        (Path.cwd() / "sentinel-link").symlink_to(Path.cwd() / "README.md")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(case / "pytest.ini"),
            "-p",
            "no:cacheprovider",
            str(case / "probe_case.py"),
        ],
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "FAKE_PYTEST_FIXTURES": "1" if fixtures else "0",
            "FAKE_OUTSIDE": str(Path.cwd()),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return {"exit": completed.returncode, "summary": completed.stdout.strip().splitlines()[-1:]}


def _write_unanswered_session(state: Path) -> None:
    # Like a missing credential: a bound v4 log with the attempt but no assistant message.
    # The attempt runs in a disposable checkout, so the shared fixture is loaded by path.
    spec = importlib.util.spec_from_file_location(
        "dsh_session", Path(__file__).resolve().parents[4] / "test_support" / "dsh.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_session(state, Path.cwd(), session_id="fake-observed", answered=False)


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
    if mode in {"pytest", "pytest-fixtures"}:
        capture["pytest"] = _run_pytest(mode == "pytest-fixtures")
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
    elif mode == "tmp-link":
        # TMPDIR itself now resolves into the checkout, which holds a link of its own.
        scratch = Path(os.environ["TMPDIR"])
        target = Path.cwd() / "scratch"
        target.mkdir()
        (target / "keep-link").symlink_to(Path.cwd() / "README.md")
        scratch.rmdir()
        scratch.symlink_to(target)
    elif mode == "root-link":
        # The whole runtime root now resolves into the checkout, which has its own home/ and tmp/.
        (Path.cwd() / "home").mkdir()
        (Path.cwd() / "home" / "keep.txt").write_text("keep\n")
        (Path.cwd() / "tmp").mkdir()
        (Path.cwd() / "tmp" / "keep-link").symlink_to(Path.cwd() / "README.md")
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
    if mode == "github-token":
        token = os.environ.get("GH_TOKEN")
        text = json.dumps({"github_token": token, "unlisted": os.environ.get("UNLISTED_SECRET")})
        (state / "session.jsonl").write_text(json.dumps({"tool": token}) + "\n")
    if mode == "auth":
        reason = {"kind": "error", "error": {"code": "MISSING_CREDENTIAL", "message": "missing"}}
        text = ""
        _write_unanswered_session(state)
    session_event: dict[str, object] = {"type": "session", "sessionId": "fake-observed", "cwd": str(Path.cwd())}
    # The provider echoes the attempt's key in a long diagnostic (#96).
    failure = f"provider rejected the request made with {key} " + "詳細" * 125
    if mode == "error-event":
        # A failure outside a turn: an error event, no final, and the same line on stderr.
        print(json.dumps(session_event), flush=True)
        print(json.dumps({"type": "error", "message": failure}, ensure_ascii=False), flush=True)
        print(f"dsh: {failure}", file=sys.stderr)
        return 1
    if mode == "cut":
        # The stream stops inside a step, as when the process dies mid-call.
        for event in (
            session_event,
            {"type": "status", "phase": "turn_start", "turn": 1},
            {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
            {"type": "tool_call", "callId": "call", "tool": "read", "input": {"path": "README.md"}},
        ):
            print(json.dumps(event), flush=True)
        return 1
    if mode == "turn-error":
        reason = {"kind": "error", "error": {"code": "PROVIDER_ERROR", "message": failure, "status": 503}}
        text = ""
    events: list[dict[str, object]] = [session_event, {"type": "status", "phase": "turn_start", "turn": 1}]
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
    return 1 if mode in {"auth", "nonzero", "turn-error"} else 0


if __name__ == "__main__":
    sys.exit(main())
