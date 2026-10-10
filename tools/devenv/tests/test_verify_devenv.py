import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[3]


class UvCall(TypedDict):
    argv: list[str]
    cwd: str
    stage: str


def make_minimal_parent_fixture(tmp_path: Path, failure: str | None = None) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "parent"
    for part in (
        "scripts/ci",
        "tools/devenv/tests",
        "tools/devenv_migration/tests",
        "tools/review_triage/tests",
        "tools/agent_runner/tests",
        "tools/test_support/tests",
        ".agents",
    ):
        (root / part).mkdir(parents=True)
    shutil.copy2(SOURCE_ROOT / "scripts/ci/verify-devenv.sh", root / "scripts/ci/verify-devenv.sh")
    shutil.copy2(SOURCE_ROOT / "tools/devenv/pyproject.toml", root / "tools/devenv/pyproject.toml")
    shutil.copy2(SOURCE_ROOT / "tools/devenv/uv.lock", root / "tools/devenv/uv.lock")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-calls.jsonl"
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"log = {str(log)!r}\n"
        f"failure = {failure!r}\n"
        "args = sys.argv[1:]\n"
        "command = args[args.index('--no-sync') + 1:] if '--no-sync' in args else []\n"
        "stage = 'sync' if args[0] == 'sync' else ('ruff-' + command[1] if command[0] == 'ruff' else command[0])\n"
        "with open(log, 'a') as stream: stream.write("
        "json.dumps({'argv': args, 'cwd': os.getcwd(), 'stage': stage}) + '\\n')\n"
        "sys.exit(23 if stage == failure else 0)\n"
    )
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return root, log, env


def run_runner(
    root: Path, env: dict[str, str], cwd: Path, args: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts/ci/verify-devenv.sh"), *(args or [])],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def read_calls(log: Path) -> list[UvCall]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("failure", [None, "sync", "ruff-check", "ruff-format", "pyright", "pytest"])
def test_runner_only_uses_devenv_and_propagates_failure(tmp_path: Path, failure: str | None) -> None:
    root, log, env = make_minimal_parent_fixture(tmp_path, failure=failure)
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_runner(root, env=env, cwd=outside)
    calls = read_calls(log)
    assert (result.returncode == 0) is (failure is None)
    assert calls[0]["argv"][:4] == ["sync", "--project", str(root / "tools/devenv"), "--locked"]
    assert all(call["cwd"] == str(root) for call in calls)
    assert not any("public" in " ".join(call["argv"]) for call in calls)
    assert not any(str(root / "tests") in call["argv"] for call in calls)
    for call in calls:
        if call["stage"] in {"ruff-check", "ruff-format"}:
            assert "tools/agent_runner" in call["argv"]
        elif call["stage"] == "pytest":
            assert "tools/agent_runner/tests" in call["argv"]


def test_runner_rejects_argument_and_missing_lock(tmp_path: Path) -> None:
    root, log, env = make_minimal_parent_fixture(tmp_path)
    assert run_runner(root, env=env, cwd=tmp_path, args=["full"]).returncode != 0
    (root / "tools/devenv/uv.lock").unlink()
    assert run_runner(root, env=env, cwd=tmp_path).returncode != 0
    assert read_calls(log) == []
