"""usaturn/xrift-poc#1: XRift 公式スキルを固定したコミットから導入し、Git で無視されることを確かめる仕様。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.test_support.xrift import SKILLS_NPX_FAKE

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "空白 root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "install_xrift_skills.bash", root / "scripts" / "install_xrift_skills.bash")
    shutil.copy2(REPO_ROOT / ".gitignore", root / ".gitignore")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def run(root: Path, tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    npx = bin_dir / "npx"
    npx.write_text(SKILLS_NPX_FAKE, encoding="utf-8")
    npx.chmod(0o755)
    return subprocess.run(
        ["bash", str(root / "scripts" / "install_xrift_skills.bash")],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_SKILLS_MODE": mode},
        capture_output=True,
        text=True,
    )


def test_install_places_ignored_skills(root: Path, tmp_path: Path) -> None:
    result = run(root, tmp_path, "ok")
    assert result.returncode == 0, result.stderr
    assert (root / ".agents/skills/xrift-world/SKILL.md").is_file()
    assert (root / ".claude/skills/xrift-sdk").is_symlink()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        ".agents" not in status.stdout and ".claude" not in status.stdout and "skills-lock.json" not in status.stdout
    )


def test_rerun_succeeds(root: Path, tmp_path: Path) -> None:
    assert run(root, tmp_path, "ok").returncode == 0
    assert run(root, tmp_path, "ok").returncode == 0


def test_install_fails_on_ref_mismatch(root: Path, tmp_path: Path) -> None:
    result = run(root, tmp_path, "wrong-ref")
    assert result.returncode != 0
    assert "ERROR" in result.stderr


def test_install_fails_when_a_required_skill_is_missing(root: Path, tmp_path: Path) -> None:
    result = run(root, tmp_path, "missing")
    assert result.returncode != 0
    assert "xrift-sdk" in result.stderr


def test_install_fails_when_a_skill_is_not_ignored(root: Path, tmp_path: Path) -> None:
    result = run(root, tmp_path, "unignored")
    assert result.returncode != 0
    assert "other-skill" in result.stderr


def test_install_fails_when_skills_add_fails(root: Path, tmp_path: Path) -> None:
    result = run(root, tmp_path, "fail")
    assert result.returncode != 0
    assert "ERROR" in result.stderr
