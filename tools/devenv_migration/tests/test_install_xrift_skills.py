"""usaturn/xrift-poc#1: XRift 公式スキルの導入が、固定したコミットと必須のスキルを skills-lock.json で照合する仕様。"""

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


def test_install_fails_on_ref_mismatch(root: Path, tmp_path: Path) -> None:
    """lock のコミットの照合が外れると、固定していない版のスキルを黙って受け入れ、その指示で制作が進む。"""
    assert run(root, tmp_path, "wrong-ref").returncode != 0


def test_install_fails_when_a_required_skill_is_missing(root: Path, tmp_path: Path) -> None:
    """必須のスキルの照合が外れると、xrift-sdk が入らなくても導入が成功として終わり、ガイド無しで進む。"""
    assert run(root, tmp_path, "missing").returncode != 0
