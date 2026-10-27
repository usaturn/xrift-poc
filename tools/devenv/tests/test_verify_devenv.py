import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

SOURCE_ROOT = Path(__file__).resolve().parents[3]


class UvCall(TypedDict):
    argv: list[str]
    cwd: str
    stage: str


def make_fake_uv(
    tmp_path: Path, failure: str | None = None, pytest_python: str | None = None
) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-calls.jsonl"
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"log = {str(log)!r}\n"
        f"failure = {failure!r}\n"
        f"pytest_python = {pytest_python!r}\n"
        "args = sys.argv[1:]\n"
        "command = args[args.index('--no-sync') + 1:] if '--no-sync' in args else []\n"
        "stage = 'sync' if args[0] == 'sync' else ('ruff-' + command[1] if command[0] == 'ruff' else command[0])\n"
        "with open(log, 'a') as stream: stream.write("
        "json.dumps({'argv': args, 'cwd': os.getcwd(), 'stage': stage}) + '\\n')\n"
        "if stage == 'pytest' and pytest_python:\n"
        "    os.execv(pytest_python, [pytest_python, '-m', 'pytest', *command[1:]])\n"
        "sys.exit(23 if stage == failure else 0)\n"
    )
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return log, env


def make_minimal_parent_fixture(
    tmp_path: Path, failure: str | None = None, pytest_python: str | None = None
) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "parent"
    for part in (
        "scripts/ci",
        "tools/devenv/tests",
        "tools/devenv_migration/tests",
        "tools/agent_runner/tests",
        "tools/test_support/tests",
        ".agents",
    ):
        (root / part).mkdir(parents=True)
    shutil.copy2(SOURCE_ROOT / "scripts/ci/verify-devenv.sh", root / "scripts/ci/verify-devenv.sh")
    shutil.copy2(SOURCE_ROOT / "tools/devenv/pyproject.toml", root / "tools/devenv/pyproject.toml")
    shutil.copy2(SOURCE_ROOT / "tools/devenv/uv.lock", root / "tools/devenv/uv.lock")
    log, env = make_fake_uv(tmp_path, failure=failure, pytest_python=pytest_python)
    return root, log, env


def run_runner(root: Path, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts/ci/verify-devenv.sh")],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def read_calls(log: Path) -> list[UvCall]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_runner_fails_and_stops_at_any_failing_stage(tmp_path: Path) -> None:
    """どの段階（uv sync・ruff・pyright・最後の pytest）の失敗を握りつぶしても、CI が green で壊れたコードを通す。"""
    root, log, env = make_minimal_parent_fixture(tmp_path / "pass")
    passing = run_runner(root, env=env, cwd=root)
    assert passing.returncode == 0, passing.stderr
    # 段階の名前を写さず、成功した実行で呼ばれた段階を、それぞれ失敗させる。
    stages = [call["stage"] for call in read_calls(log)]
    assert stages
    for index, stage in enumerate(stages):
        root, log, env = make_minimal_parent_fixture(tmp_path / f"fail-{index}", failure=stage)
        result = run_runner(root, env=env, cwd=root)
        assert result.returncode != 0, stage
        # 失敗した段階で止まり、後の段階へ進まない。
        assert [call["stage"] for call in read_calls(log)] == stages[: index + 1], stage


def test_runner_scopes_conftest_fixtures_to_their_directory(tmp_path: Path) -> None:
    """--rootdir が外れ、conftest の autouse の fixture が別のディレクトリのテストにも効いたまま通る。"""
    root, _, env = make_minimal_parent_fixture(tmp_path, pytest_python=sys.executable)
    # 自分のディレクトリのテストにだけ効くはずの autouse の fixture。nodeid が repo の root からのパスかも確かめる
    (root / "tools/agent_runner/tests/conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def agent_runner_only(request: pytest.FixtureRequest) -> None:\n"
        "    assert request.node.nodeid.startswith('tools/agent_runner/tests/'), request.node.nodeid\n"
    )
    (root / "tools/agent_runner/tests/test_inside.py").write_text("def test_inside() -> None:\n    pass\n")
    (root / "tools/test_support/tests/test_outside.py").write_text("def test_outside() -> None:\n    pass\n")
    result = run_runner(root, env=env, cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
