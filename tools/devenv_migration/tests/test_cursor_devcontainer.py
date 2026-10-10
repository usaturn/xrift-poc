"""Cursor authentication remains usable through the Dev Container bind mount."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

DEVCONTAINER = Path(__file__).resolve().parents[3] / ".devcontainer"


# Break caught: a missing continuation, wrong filename, or missing Cursor parent
# stops initialization or changes existing credentials instead of preparing mounts.
@pytest.mark.parametrize(
    ("home_name", "claude_exists", "cursor_state"),
    (
        ("fresh", False, "absent"),
        ("claude-only", True, "absent"),
        ("cursor-parent", False, "directory"),
        ("both-existing", True, "file"),
        ("空白 日本語 home", False, "absent"),
        ("executable-auth", True, "executable"),
    ),
)
def test_initialize_prepares_mounts_without_changing_existing_credentials(
    tmp_path: Path, home_name: str, claude_exists: bool, cursor_state: str
) -> None:
    home = tmp_path / home_name
    home.mkdir()
    claude = home / ".claude.json"
    cursor = home / ".config/cursor/auth.json"
    sentinel = b'{"synthetic":"not-a-secret"}\n'
    if claude_exists:
        claude.write_bytes(sentinel)
        claude.chmod(0o600)
    if cursor_state != "absent":
        cursor.parent.mkdir(parents=True)
    if cursor_state in ("file", "executable"):
        cursor.write_bytes(sentinel)
        cursor.chmod(0o700 if cursor_state == "executable" else 0o600)

    for _ in range(2):
        result = subprocess.run(
            ["/bin/bash", str(DEVCONTAINER / "initialize.bash")],
            env={"HOME": str(home), "PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert claude.is_file()
        assert not (home / ".claude.json,").exists()
        assert cursor.parent.is_dir()
        if claude_exists:
            assert claude.read_bytes() == sentinel
            assert claude.stat().st_mode & 0o777 == 0o600
        if cursor_state in ("file", "executable"):
            assert cursor.read_bytes() == sentinel
            assert cursor.stat().st_mode & 0o777 == (0o700 if cursor_state == "executable" else 0o600)
        else:
            assert not cursor.exists()


def _cursor_mount() -> dict[str, str]:
    text = (DEVCONTAINER / "devcontainer.json").read_text()
    match = re.search(r'"mounts"\s*:\s*(\[.*?\])', text, re.DOTALL)
    assert match is not None, "mounts missing from devcontainer.json"
    mounts = cast(list[str], json.loads(match.group(1)))
    cursor_mounts = [mount for mount in mounts if "/.config/cursor" in mount]
    assert len(cursor_mounts) == 1, "Cursor authentication needs one shared mount"
    return dict(part.split("=", 1) for part in cursor_mounts[0].split(","))


# Break caught: binding auth.json itself (rather than its directory) prevents
# logout from unlinking it, even when saving credentials succeeds.
@pytest.mark.parametrize("existing_auth", (False, True), ids=("not-logged-in", "logged-in"))
def test_cursor_mount_shares_credential_writes_and_logout(tmp_path: Path, existing_auth: bool) -> None:
    for command in ("unshare", "mount", "umount"):
        if shutil.which(command) is None:
            pytest.skip(f"{command} is required for the real bind-mount check")
    probe = subprocess.run(
        ["unshare", "-Urnm", "--map-root-user", "--", "true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(f"User/mount namespaces unavailable: {probe.stderr.strip()}")

    home = tmp_path / "host home 日本語"
    container = tmp_path / "container"
    host_auth = home / ".config/cursor/auth.json"
    container_auth = container / ".config/cursor/auth.json"
    host_auth.parent.mkdir(parents=True)
    container_auth.parent.mkdir(parents=True)
    if existing_auth:
        host_auth.write_text('{"synthetic":"previous-login"}\n')
    mount = _cursor_mount()
    source = Path(mount["source"].replace("${localEnv:HOME}", str(home)))
    target = container / Path(mount["target"]).relative_to("/home/vscode")
    assert source.resolve().is_relative_to(home.resolve())
    assert target.resolve().is_relative_to(container.resolve())
    if source == host_auth:
        # The old file mount needs a source file even before the first login.
        source.touch(exist_ok=True)
        target.touch()
    else:
        source.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "unshare",
            "-Urnm",
            "--map-root-user",
            "--",
            sys.executable,
            "-B",
            "-c",
            """import subprocess, sys
from pathlib import Path
source, target, auth, host_auth = map(Path, sys.argv[1:])
subprocess.run(['mount', '--bind', str(source), str(target)], check=True)
try:
    auth.parent.chmod(0o700)
    payload = b'{"synthetic":"new-login"}\\n'
    auth.write_bytes(payload)
    auth.chmod(0o600)
    assert host_auth.read_bytes() == payload, 'credential writes were not shared'
    auth.unlink()
    assert not host_auth.exists(), 'logout left host credentials behind'
finally:
    subprocess.run(['umount', str(target)], check=True)
""",
            str(source),
            str(target),
            str(container_auth),
            str(host_auth),
        ],
        env={"HOME": str(home), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not host_auth.exists()
