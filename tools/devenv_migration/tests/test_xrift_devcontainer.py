"""usaturn/xrift-poc#1: XRift CLI の認証情報の保存先と devcontainer.json の構造。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

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
    home = tmp_path / "空白 home"
    home.mkdir()
    for _ in range(2):
        result = run_initialize(home)
        assert result.returncode == 0, result.stderr
    assert (home / ".xrift").is_dir()
    assert (home / ".xrift").stat().st_mode & 0o777 == 0o700


def test_initialize_keeps_an_existing_xrift_directory(tmp_path: Path) -> None:
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


def test_initialize_stops_when_xrift_is_not_a_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".xrift").write_text("x\n", encoding="utf-8")
    result = run_initialize(home)
    assert result.returncode != 0
    assert str(home / ".xrift") in result.stderr
    assert (home / ".xrift").read_text(encoding="utf-8") == "x\n"


def test_initialize_stops_when_xrift_is_a_symlink_and_names_the_cause(tmp_path: Path) -> None:
    # ディレクトリへのシンボリックリンク（dotfiles 管理）も拒否する。理由の表示が「ディレクトリではない」だけだと、
    # 実体がディレクトリの利用者には原因が分からないので、シンボリックリンクであることを表示に含める。
    home = tmp_path / "home"
    real = tmp_path / "dotfiles" / "xrift"
    real.mkdir(parents=True)
    (real / "config.json").write_text('{"synthetic":"not-a-secret"}\n', encoding="utf-8")
    home.mkdir()
    (home / ".xrift").symlink_to(real, target_is_directory=True)
    result = run_initialize(home)
    assert result.returncode != 0
    assert str(home / ".xrift") in result.stderr
    assert "symlink" in result.stderr
    assert os.readlink(home / ".xrift") == str(real)
    assert (real / "config.json").read_text(encoding="utf-8") == '{"synthetic":"not-a-secret"}\n'


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


def test_devcontainer_json_parses() -> None:
    assert isinstance(_devcontainer(), dict)


def test_xrift_directory_is_bind_mounted_from_the_host_home() -> None:
    mounts = [cast(str, mount) for mount in _devcontainer()["mounts"]]
    xrift = [dict(part.split("=", 1) for part in mount.split(",")) for mount in mounts if "/.xrift" in mount]
    assert xrift == [
        {
            "source": "${localEnv:HOME}/.xrift",
            "target": "/home/vscode/.xrift",
            "type": "bind",
            "consistency": "cached",
        }
    ]


def test_every_bind_mount_source_is_prepared_by_initialize(tmp_path: Path) -> None:
    # bind mount の元がホストに無いと Docker が root 所有で作るので、initialize.bash がすべて用意する。
    home = tmp_path / "home"
    home.mkdir()
    assert run_initialize(home).returncode == 0
    for mount in cast(list[str], _devcontainer()["mounts"]):
        fields = dict(part.split("=", 1) for part in mount.split(","))
        source = Path(fields["source"].replace("${localEnv:HOME}", str(home)))
        assert source.exists(), fields["source"]
