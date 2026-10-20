"""書きかけの worktree の登録の仕込みが、git の prune で消えないこと（#88）。

仕込んだ後に prune が動いても、並行した git worktree add の commondir の競合を再現する。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.test_support.worktree_race import plant_racer


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def test_planted_registration_survives_maintenance_and_still_races(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        # この commit が切り離した maintenance を起動しないようにし、下で前面の 1 回だけを動かす
        ("config", "maintenance.auto", "false"),
    ):
        assert git(repo, *args).returncode == 0
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    assert git(repo, "add", "tracked.txt").returncode == 0
    assert git(repo, "commit", "-qm", "initial").returncode == 0
    before = {path.name for path in (repo / ".git").iterdir()}

    commondir = plant_racer(repo)

    assert {path.name for path in (repo / ".git").iterdir()} - before == {"worktrees"}
    # git commit が切り離して起動する自動 maintenance を前面で実行し、
    # 続けて、その worktree の prune が実行するコマンドを実行する
    for args in (("maintenance", "run", "--auto"), ("worktree", "prune")):
        result = git(repo, *args)
        assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in commondir.parent.glob("*")) == ["HEAD", "commondir", "gitdir", "locked"]
    assert commondir.read_bytes() == b""
    destination = tmp_path / "worktree"
    added = git(repo, "worktree", "add", "--detach", str(destination), "HEAD")
    assert added.returncode == 128
    assert "worktrees/racer/commondir: " in added.stderr
    assert not destination.exists()
