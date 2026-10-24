"""Git workspace audits against real repositories."""

import subprocess
from pathlib import Path

import pytest

from tools.agent_runner import workspace
from tools.agent_runner.models import CheckoutAudit
from tools.agent_runner.workspace import (
    WorkspaceError,
    blocking_checkout_audit,
    changed_checkouts,
    create_worktree,
    snapshot_checkouts,
)


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "tracked.txt").write_text("committed\n")
    (root / ".gitignore").write_text("ignored/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


@pytest.mark.parametrize("symlink", [False, True])
def test_audit_detects_existing_dirty_content_edits(repository: Path, tmp_path: Path, symlink: bool) -> None:
    """開始前からある未追跡ファイルやリンクの変更を見逃すと outside_changes に黙って出ない。"""
    external = create_worktree(repository, "HEAD", tmp_path / "external checkout\nwith newline")
    path = external / "untracked.txt"
    if symlink:
        path.symlink_to("first-target")
    else:
        path.write_text("dirty before\n")
    before = snapshot_checkouts(repository)
    assert set(before) == {str(repository), str(external)}
    assert changed_checkouts(before) == []
    if symlink:
        path.unlink()
        path.symlink_to("different-target")
    else:
        path.write_text("dirty after!\n")
    assert changed_checkouts(before) == [str(external)]


def test_audit_detects_moved_head(repository: Path) -> None:
    """HEAD を指紋に含めないと、担当が既存の checkout でファイルを変えずに commit しても黙って見逃す。"""
    before = snapshot_checkouts(repository)
    git(repository, "commit", "--allow-empty", "-qm", "advance HEAD")
    assert changed_checkouts(before) == [str(repository)]


def test_audit_reports_disappeared_checkout(repository: Path, tmp_path: Path) -> None:
    """担当が既存の checkout を移動・削除しても、変化なしとして黙って扱う。"""
    external = create_worktree(repository, "HEAD", tmp_path / "external")
    before = snapshot_checkouts(repository)
    external.rename(tmp_path / "moved")
    assert changed_checkouts(before) == [str(external)]


def test_checkout_recovered_after_snapshot_still_warns(repository: Path, tmp_path: Path) -> None:
    """開始時に監査できなかった checkout が終了時に読めると、baseline が無いのに変化なしとして黙って扱う。"""
    external = create_worktree(repository, "HEAD", tmp_path / "recovered")
    gitfile = external / ".git"
    original = gitfile.read_bytes()
    gitfile.write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
    try:
        before = snapshot_checkouts(repository)
    finally:
        gitfile.write_bytes(original)
    assert changed_checkouts(before) == [str(external)]


def test_registration_list_failure_still_stops_snapshot(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git worktree list の失敗を空の snapshot として扱うと、監査の対象が無くなり、どの変更も黙って出ない。"""

    def unavailable(*_: object) -> bytes:
        raise WorkspaceError()

    monkeypatch.setattr(workspace, "_git", unavailable)
    with pytest.raises(WorkspaceError):
        snapshot_checkouts(repository)


def test_only_isolated_content_changes_are_nonblocking() -> None:
    """隔離なし・監査不能・同じパスの置換を許すと、担当の既存 checkout 変更を黙って受け付ける。"""
    changed = CheckoutAudit(path="/checkout", kind="content_changed", reason="content_changed")
    unavailable = CheckoutAudit(path="/checkout", kind="unavailable_after", reason="filesystem_unavailable")
    replaced = CheckoutAudit(path="/checkout", kind="replaced", reason="checkout_replaced")
    assert not blocking_checkout_audit([changed.path], [changed], isolated=True)
    assert blocking_checkout_audit([changed.path], [changed], isolated=False)
    assert blocking_checkout_audit([unavailable.path], [unavailable], isolated=True)
    assert blocking_checkout_audit([replaced.path], [replaced], isolated=True)
    assert blocking_checkout_audit([changed.path, "/unaccounted"], [changed], isolated=True)


def test_incomplete_isolation_blocks_even_without_audit_findings() -> None:
    """隔離できない既存 checkout の操作を監査結果が空という理由で受け付けると、無断変更を見逃す。"""
    assert blocking_checkout_audit([], [], isolated=False)
