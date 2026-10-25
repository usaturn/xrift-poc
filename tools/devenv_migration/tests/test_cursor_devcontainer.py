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


def test_initialize_keeps_existing_claude_and_cursor_credentials(tmp_path: Path) -> None:
    """initialize.bash が既存の ~/.claude.json や Cursor の auth.json を作り直すと、
    Claude Code・Cursor の認証と設定が黙って消える。"""
    home = tmp_path / "home"
    home.mkdir()
    claude = home / ".claude.json"
    cursor = home / ".config/cursor/auth.json"
    sentinel = b'{"synthetic":"not-a-secret"}\n'
    claude.write_bytes(sentinel)
    claude.chmod(0o600)
    cursor.parent.mkdir(parents=True)
    cursor.write_bytes(sentinel)
    cursor.chmod(0o600)

    for _ in range(2):
        result = subprocess.run(
            ["/bin/bash", str(DEVCONTAINER / "initialize.bash")],
            env={"HOME": str(home), "PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        for credential in (claude, cursor):
            assert credential.read_bytes() == sentinel
            assert credential.stat().st_mode & 0o777 == 0o600


def _cursor_mount() -> dict[str, str]:
    text = (DEVCONTAINER / "devcontainer.json").read_text()
    match = re.search(r'"mounts"\s*:\s*(\[.*?\])', text, re.DOTALL)
    assert match is not None, "mounts missing from devcontainer.json"
    mounts = cast(list[str], json.loads(match.group(1)))
    cursor_mounts = [mount for mount in mounts if "/.config/cursor" in mount]
    assert len(cursor_mounts) == 1, "Cursor authentication needs one shared mount"
    return dict(part.split("=", 1) for part in cursor_mounts[0].split(","))


def test_cursor_mount_shares_credential_writes_and_logout(tmp_path: Path) -> None:
    """auth.json そのものを bind すると、コンテナ内の logout が host の認証情報を消せず、
    ログアウトしたはずの認証情報が host に黙って残る。"""
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
    host_auth.write_text('{"synthetic":"previous-login"}\n')
    mount = _cursor_mount()
    source = Path(mount["source"].replace("${localEnv:HOME}", str(home)))
    target = container / Path(mount["target"]).relative_to("/home/vscode")
    assert source.resolve().is_relative_to(home.resolve())
    assert target.resolve().is_relative_to(container.resolve())
    if source == host_auth:
        # The old file mount binds the existing credentials file itself.
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
