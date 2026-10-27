"""usaturn/xrift-poc#1: initialize.bash が用意する bind mount の元と、XRift CLI の認証情報の保存先。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

DEVCONTAINER = Path(__file__).resolve().parents[3] / ".devcontainer"


def run_initialize(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(DEVCONTAINER / "initialize.bash")],
        env={"HOME": str(home), "PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_initialize_creates_a_private_xrift_directory(tmp_path: Path) -> None:
    """~/.xrift を 0700 で作らないと、XRift CLI の認証情報の置き場所が他のユーザから見える権限で黙って作られる。"""
    home = tmp_path / "空白 home"
    home.mkdir()
    for _ in range(2):
        result = run_initialize(home)
        assert result.returncode == 0, result.stderr
    assert (home / ".xrift").is_dir()
    assert (home / ".xrift").stat().st_mode & 0o777 == 0o700


def test_initialize_keeps_an_existing_xrift_directory(tmp_path: Path) -> None:
    """既存の ~/.xrift と中の認証情報・権限を変えると、利用者のログイン状態と設定が黙って失われる。"""
    home = tmp_path / "home"
    config = home / ".xrift" / "config.json"
    config.parent.mkdir(parents=True)
    config.parent.chmod(0o755)
    config.write_text('{"synthetic":"not-a-secret"}\n', encoding="utf-8")
    config.chmod(0o600)
    assert run_initialize(home).returncode == 0
    assert config.read_text(encoding="utf-8") == '{"synthetic":"not-a-secret"}\n'
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_initialize_preserves_rejected_xrift_paths(tmp_path: Path, kind: str) -> None:
    """拒否時に ~/.xrift やリンク先を消すと、非0終了でも利用者の設定が黙って失われる。"""
    home = tmp_path / "空白 home 日本語\n二行目"
    xrift = home / ".xrift"
    config = xrift if kind == "file" else home / "既存の設定" / "config.json"
    payload = '{\n  "synthetic": "not-a-secret",\n  "note": "日本語の合成設定"\n}\n'.encode("utf-8")
    config.parent.mkdir(parents=True)
    config.write_bytes(payload)
    link_target = config.parent.relative_to(home)
    if kind == "symlink":
        xrift.symlink_to(link_target, target_is_directory=True)

    result = run_initialize(home)

    assert result.returncode != 0, result.stderr
    if kind == "symlink":
        assert xrift.is_symlink()
        assert xrift.readlink() == link_target
    assert config.is_file()
    assert not config.is_symlink()
    assert config.read_bytes() == payload


def _strip_jsonc(text: str) -> str:
    """文字列の中を除いて // と /* */ のコメントを取り除き、末尾のカンマを外す。"""
    out: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\":
                out.append(text[index + 1])
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index == -1:
                break
            continue
        elif text.startswith("/*", index):
            index = text.index("*/", index) + 2
            continue
        else:
            out.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _devcontainer() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_strip_jsonc((DEVCONTAINER / "devcontainer.json").read_text())))


def test_every_bind_mount_source_is_prepared_by_initialize(tmp_path: Path) -> None:
    """bind mount の元が host に無いと、Docker が root 所有のディレクトリを作り
    （~/.claude.json がディレクトリになる）、host の状態が黙って壊れる。"""
    home = tmp_path / "home"
    home.mkdir()
    assert run_initialize(home).returncode == 0
    for mount in cast(list[str], _devcontainer()["mounts"]):
        fields = dict(part.split("=", 1) for part in mount.split(","))
        source = Path(fields["source"].replace("${localEnv:HOME}", str(home)))
        assert source.exists(), fields["source"]
