"""The scoped DSH runner permits owned outputs, not neighboring host files."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "dsh_sandbox.py"
BASE = ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent"]


def invoke(
    root: Path, grants: list[Path], *, readonly: bool = False, invalid: bool = False
) -> subprocess.CompletedProcess[str]:
    work = root / "work"
    profile = BASE if readonly else [*BASE, "--tmpfs", "/tmp", "--bind", str(work), str(work)]
    if invalid:
        profile += ["--bind", "/", "/"]
    files = [
        root / name
        for name in ("work/out", "home/out", "tmp/out", "review/out", "git/lock", "neighbor", "git/protected")
    ]
    program = (
        "import json,pathlib; results=[]\nfor value in "
        + repr([str(p) for p in files])
        + ":\n"
        + (
            " try: pathlib.Path(value).write_text('changed'); results.append('written')\n"
            " except OSError: results.append('denied')\n"
            "print(json.dumps(results))\n"
        )
    )
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace",
            str(work),
            *[arg for p in grants for arg in ("--grant", str(p))],
            "--",
            *profile,
            "--",
            sys.executable,
            "-c",
            program,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def make_paths(root: Path) -> list[Path]:
    for name in ("work", "home", "tmp", "review", "git"):
        (root / name).mkdir()
    for name in ("git/lock", "neighbor", "git/protected"):
        (root / name).write_text("original")
    return [root / name for name in ("home", "tmp", "review", "git/lock")]


def test_explicit_grants_preserve_neighboring_host_files() -> None:
    # /var/tmp remains visible read-only; bwrap's per-call /tmp is ephemeral.
    with tempfile.TemporaryDirectory(prefix="dsh-sandbox-test-", dir="/var/tmp") as directory:
        root = Path(directory)
        result = invoke(root, make_paths(root))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["written"] * 5 + ["denied"] * 2
        assert (root / "neighbor").read_text() == (root / "git/protected").read_text() == "original"
        assert (root / "git/lock").read_text() == "changed"


def test_readonly_does_not_apply_additional_grants() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-sandbox-test-", dir="/var/tmp") as directory:
        root = Path(directory)
        result = invoke(root, make_paths(root), readonly=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["denied"] * 7


@pytest.mark.parametrize("case", ["symlink", "missing", "invalid_profile"])
def test_unsafe_grants_or_profile_never_run_the_command(case: str) -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-sandbox-test-", dir="/var/tmp") as directory:
        root = Path(directory)
        grants = make_paths(root)
        if case == "symlink":
            link = root / "link"
            link.symlink_to(root / "review")
            grants.append(link)
        elif case == "missing":
            grants.append(root / "absent")
        result = invoke(root, grants, invalid=case == "invalid_profile")
        assert result.returncode != 0 and result.stdout == ""
        assert "dsh-sandbox:" in result.stderr
        assert not (root / "work/out").exists()
        assert (root / "git/lock").read_text() == "original"
