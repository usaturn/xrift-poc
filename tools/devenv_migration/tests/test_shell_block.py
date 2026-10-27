"""usaturn/xrift-poc#1: ~/.zshrc の管理ブロックの書き込みが、利用者の設定・symlink・権限を壊さない仕様。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "write_shell_block.py"
SHELL_BLOCK_SPEC = importlib.util.spec_from_file_location("write_shell_block", SCRIPT)
assert SHELL_BLOCK_SPEC is not None and SHELL_BLOCK_SPEC.loader is not None
shell_block = importlib.util.module_from_spec(SHELL_BLOCK_SPEC)
SHELL_BLOCK_SPEC.loader.exec_module(shell_block)
BEGIN = shell_block.BLOCK_BEGIN
END = shell_block.BLOCK_END


def run(source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--target", str(target)],
        capture_output=True,
        text=True,
    )


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_updated_source_replaces_only_the_block(tmp_path: Path) -> None:
    """ブロックの置き換えが範囲を誤ると、ブロックの前の利用者の設定や、後ろにインストーラが追記した行を黙って消す。"""
    target = write(tmp_path / ".zshrc", f"# before\n{BEGIN}\nold\n{END}\n# after: installer PATH line\n")
    source = write(tmp_path / "zshrc.txt", "new\n")
    assert run(source, target).returncode == 0
    assert target.read_text(encoding="utf-8") == f"# before\n{BEGIN}\nnew\n{END}\n# after: installer PATH line\n"


def test_broken_markers_leave_the_file_unchanged(tmp_path: Path) -> None:
    """BEGIN の重複を数で検査しないと、最初の BEGIN から END までを置き換え、間の利用者の設定を黙って消す。"""
    source = write(tmp_path / "zshrc.txt", "x\n")
    broken = f'{BEGIN}\n# 利用者の設定\nexport PATH="$HOME/my tools/ビン:$PATH"\n{BEGIN}\nold\n{END}\n'
    target = write(tmp_path / ".zshrc", broken)
    assert run(source, target).returncode != 0
    assert target.read_text(encoding="utf-8") == broken


def test_symlinked_target_and_mode_are_kept(tmp_path: Path) -> None:
    """symlink の ~/.zshrc を通常のファイルに置き換えると dotfiles へのリンクが黙って切れ、0600 の権限も広がる。"""
    source = write(tmp_path / "zshrc.txt", "x\n")
    real = write(tmp_path / "dotfiles-zshrc", "# 日本語のコメント\n")
    real.chmod(0o600)
    target = tmp_path / ".zshrc"
    target.symlink_to(real)
    assert run(source, target).returncode == 0
    assert target.is_symlink()
    assert real.read_text(encoding="utf-8") == f"# 日本語のコメント\n{BEGIN}\nx\n{END}\n"
    assert real.stat().st_mode & 0o777 == 0o600
    assert os.readlink(target) == str(real)
