"""usaturn/xrift-poc#1: ~/.zshrc の管理ブロックを、再実行しても重複させずに書き込む仕様。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "write_shell_block.py"
BEGIN = "# >>> xrift-poc shell setup >>>"
END = "# <<< xrift-poc shell setup <<<"


def run(source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--target", str(target)],
        capture_output=True,
        text=True,
    )


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_target_gets_the_block(tmp_path: Path) -> None:
    source = write(tmp_path / "zshrc.txt", "alias ll='ls -l'\n")
    target = tmp_path / ".zshrc"
    assert run(source, target).returncode == 0
    assert target.read_text(encoding="utf-8") == f"{BEGIN}\nalias ll='ls -l'\n{END}\n"


def test_block_is_appended_after_user_content(tmp_path: Path) -> None:
    source = write(tmp_path / "zshrc.txt", "export EDITOR=code")
    target = write(tmp_path / ".zshrc", '# 利用者の設定\nexport PATH="$HOME/.local/bin:$PATH"')
    assert run(source, target).returncode == 0
    assert target.read_text(encoding="utf-8") == (
        f'# 利用者の設定\nexport PATH="$HOME/.local/bin:$PATH"\n{BEGIN}\nexport EDITOR=code\n{END}\n'
    )


def test_rerun_keeps_a_single_block(tmp_path: Path) -> None:
    source = write(tmp_path / "zshrc.txt", "setopt no_beep\n")
    target = write(tmp_path / ".zshrc", "# user\n")
    assert run(source, target).returncode == 0
    first = target.read_text(encoding="utf-8")
    assert run(source, target).returncode == 0
    assert target.read_text(encoding="utf-8") == first
    assert first.count(BEGIN) == 1


def test_updated_source_replaces_only_the_block(tmp_path: Path) -> None:
    target = write(tmp_path / ".zshrc", f"# before\n{BEGIN}\nold\n{END}\n# after: installer PATH line\n")
    source = write(tmp_path / "zshrc.txt", "new\n")
    assert run(source, target).returncode == 0
    assert target.read_text(encoding="utf-8") == f"# before\n{BEGIN}\nnew\n{END}\n# after: installer PATH line\n"


def test_broken_markers_leave_the_file_unchanged(tmp_path: Path) -> None:
    source = write(tmp_path / "zshrc.txt", "x\n")
    for broken in (f"{BEGIN}\nold\n", f"{END}\n{BEGIN}\n", f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n"):
        target = write(tmp_path / ".zshrc", broken)
        result = run(source, target)
        assert result.returncode == 1
        assert target.read_text(encoding="utf-8") == broken


def test_source_containing_a_marker_is_rejected(tmp_path: Path) -> None:
    source = write(tmp_path / "zshrc.txt", f"{BEGIN}\n")
    target = write(tmp_path / ".zshrc", "# user\n")
    assert run(source, target).returncode == 1
    assert target.read_text(encoding="utf-8") == "# user\n"


def test_symlinked_target_and_mode_are_kept(tmp_path: Path) -> None:
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
