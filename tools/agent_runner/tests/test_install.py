"""Install the common config in a one-shot temporary HOME, never the real one."""

import os
import subprocess
from pathlib import Path

import pytest

from tools.agent_runner.config import resolve_config_path

ROOT = Path(__file__).resolve().parents[3]


def install(home: Path, xdg: str) -> subprocess.CompletedProcess[str]:
    source = (ROOT / ".devcontainer/install_beforehand.bash").read_text()
    start = source.index("place_agent_runner_config() {")
    function = source[start : source.index("\n}\n", start) + 3]
    return subprocess.run(
        ["bash", "-c", function + "\nplace_agent_runner_config"],
        cwd=ROOT,
        env={**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": xdg},
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_template_only_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """導入が既存の個人設定を上書きする・runner が読まない場所に置くと、利用者の設定が黙って失われる・効かない。"""
    home = tmp_path / "home"
    home.mkdir()
    # Dev Container と同じく XDG_CONFIG_HOME は未設定。導入先は runner が読む場所から求める
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AGENT_RUNNER_CONFIG", raising=False)
    target = resolve_config_path()
    result = install(home, "")
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == (ROOT / ".devcontainer/agent-runner.toml").read_bytes()
    target.write_text("user customization")
    assert install(home, "").returncode == 0
    assert target.read_text() == "user customization"


def test_install_preserves_broken_personal_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dotfiles へのリンク（リンク先が未作成）を雛形で置き換える・リンク先へ書き込むと、利用者の設定を黙って壊す。"""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AGENT_RUNNER_CONFIG", raising=False)
    target = resolve_config_path()
    target.parent.mkdir(parents=True)
    missing = home / "not-created"
    target.symlink_to(missing)
    assert install(home, "").returncode == 0
    assert target.is_symlink() and target.readlink() == missing
    assert not missing.exists()
