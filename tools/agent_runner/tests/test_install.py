"""Install the common config in a one-shot temporary HOME, never the real one."""

import os
import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("xdg", ["", "relative", "absolute"])
def test_install_template_only_when_missing(tmp_path: Path, xdg: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "xdg" if xdg == "absolute" else home / ".config"
    value = str(config_home) if xdg == "absolute" else xdg
    target = config_home / "agent-runner/config.toml"
    result = install(home, value)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == (ROOT / ".devcontainer/agent-runner.toml").read_bytes()
    target.write_text("user customization")
    assert install(home, value).returncode == 0
    assert target.read_text() == "user customization"


def test_install_preserves_broken_personal_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".config/agent-runner/config.toml"
    target.parent.mkdir(parents=True)
    missing = home / "not-created"
    target.symlink_to(missing)
    assert install(home, "").returncode == 0
    assert target.is_symlink() and target.readlink() == missing
    assert not missing.exists()


def test_install_propagates_unwritable_destination(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".config").write_text("not a directory")
    assert install(home, "").returncode != 0
