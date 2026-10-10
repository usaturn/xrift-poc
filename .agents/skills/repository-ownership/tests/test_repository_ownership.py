from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL = REPO_ROOT / ".agents/skills/repository-ownership/SKILL.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_route_check(skill_text: str) -> str:
    """Extract the canonical route-check bash block from the skill document."""
    pattern = re.compile(
        r"```bash\n(# repository-ownership route check\n.*?)\n```",
        re.DOTALL,
    )
    match = pattern.search(skill_text)
    assert match is not None, "route-check bash block not found in skill"
    block = match.group(1)
    lines = block.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    common = min(indents) if indents else 0
    return "\n".join(line[common:] for line in lines) + "\n"


def extract_block(skill_text: str, marker: str) -> str:
    """Extract a fenced bash block whose first line is the marker comment.

    The block may be indented inside a list item; leading whitespace before
    the marker and the closing fence is tolerated and removed by the dedent.
    """
    pattern = re.compile(r"```bash\n([ \t]*" + re.escape(marker) + r"\n.*?)\n[ \t]*```", re.DOTALL)
    match = pattern.search(skill_text)
    assert match is not None, f"{marker!r} bash block not found in skill"
    block = match.group(1)
    lines = block.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    common = min(indents) if indents else 0
    return "\n".join(line[common:] for line in lines) + "\n"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def build_parent_with_public(tmp_path: Path) -> Path:
    """Create an isolated parent repo with a registered public submodule."""
    public_src = tmp_path / "public_src"
    public_src.mkdir()
    _git(public_src, "init", "-b", "main")
    (public_src / "pyproject.toml").write_text('[project]\nname = "maatlog"\n', encoding="utf-8")
    _git(public_src, "add", "pyproject.toml")
    _git(public_src, "commit", "-m", "init public")

    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "-b", "main")
    (parent / "CLAUDE.md").write_text("# parent\n", encoding="utf-8")
    _git(parent, "add", "CLAUDE.md")
    _git(parent, "commit", "-m", "init parent")
    _git(parent, "remote", "add", "origin", "https://github.com/usaturn/maatlog-poc.git")

    # Local path submodule clones require protocol.file.allow on modern Git.
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(public_src.resolve()),
            "public",
        ],
        cwd=parent,
        check=True,
        text=True,
        capture_output=True,
    )
    _git(parent, "commit", "-m", "register public submodule")
    return parent.resolve()


def run_snippet(toplevel: Path, snippet: str, invocation: str) -> subprocess.CompletedProcess[str]:
    """Run the skill's route-check block plus one invocation in the fixture."""
    harness = f"set -uo pipefail\n{snippet}\n{invocation}\n"
    return subprocess.run(
        ["bash", "-c", harness],
        cwd=toplevel,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def ownership_fixture(tmp_path: Path) -> Path:
    return build_parent_with_public(tmp_path)


def test_app_route_uses_public_origin(ownership_fixture: Path, tmp_path: Path) -> None:
    snippet = extract_route_check(read(SKILL))
    app = run_snippet(ownership_fixture, snippet, "app_root")
    assert app.returncode == 0, app.stderr
    assert Path(app.stdout.strip().splitlines()[-1]) == ownership_fixture / "public"

    env = run_snippet(ownership_fixture, snippet, "env_root")
    assert env.returncode == 0, env.stderr
    assert Path(env.stdout.strip().splitlines()[-1]) == ownership_fixture

    # The documented app worktree command must base on public origin/main,
    # never on the parent's origin/main.
    worktree = tmp_path / "app-work"
    driver = (
        f'{snippet}\nAPP_ROOT="$(app_root)"\n'
        f'git -C "$APP_ROOT" fetch origin main\n'
        f'git -C "$APP_ROOT" worktree add "{worktree}" -b feature-app origin/main\n'
    )
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{driver}"],
        cwd=ownership_fixture,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    app_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    public_main = _git(ownership_fixture / "public", "rev-parse", "origin/main").stdout.strip()
    parent_head = _git(ownership_fixture, "rev-parse", "HEAD").stdout.strip()
    assert app_head == public_main
    assert app_head != parent_head


def test_app_worktree_path_stays_outside_public(ownership_fixture: Path) -> None:
    """The documented app worktree must not land inside the public checkout:
    that leaves `modified: public (untracked content)` in the parent and risks
    an embedded gitlink if anything blanket-stages the public repo."""
    content = read(SKILL)
    snippet = extract_route_check(content)
    block = extract_block(content, "# repository-ownership app worktree")
    driver = f"{snippet}\n{block}".replace("<name>", "app-work").replace("<branch>", "feature-app")
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{driver}"],
        cwd=ownership_fixture,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    public_status = _git(ownership_fixture / "public", "status", "--porcelain").stdout
    assert public_status.strip() == "", public_status
    parent_status = _git(ownership_fixture, "status", "--porcelain").stdout
    for line in parent_status.splitlines():
        assert "public" not in line, line
    worktree = ownership_fixture / ".claude" / "worktrees" / "app-work"
    assert worktree.is_dir()
    assert not worktree.is_relative_to(ownership_fixture / "public")


def test_wrong_public_origin_stops(ownership_fixture: Path) -> None:
    _git(
        ownership_fixture / "public",
        "remote",
        "set-url",
        "origin",
        "https://github.com/example/unrelated.git",
    )
    snippet = extract_route_check(read(SKILL))
    result = run_snippet(ownership_fixture, snippet, "app_root")
    assert result.returncode != 0
    assert "does not match" in result.stderr


def test_missing_public_checkout_stops(ownership_fixture: Path) -> None:
    dotgit = ownership_fixture / "public" / ".git"
    if dotgit.is_dir() and not dotgit.is_symlink():
        shutil.rmtree(dotgit)
    else:
        dotgit.unlink()
    snippet = extract_route_check(read(SKILL))
    result = run_snippet(ownership_fixture, snippet, "app_root")
    assert result.returncode != 0
    assert "public" in result.stderr


def test_parent_staging_guard_rejects_gitlink(ownership_fixture: Path) -> None:
    snippet = extract_route_check(read(SKILL))

    # A staged gitlink must trip the guard even alongside a legitimate file.
    # Advance the submodule HEAD first so `git add public` moves the gitlink.
    (ownership_fixture / "public" / "app.txt").write_text("app\n", encoding="utf-8")
    _git(ownership_fixture / "public", "add", "app.txt")
    _git(ownership_fixture / "public", "commit", "-m", "advance public")
    (ownership_fixture / "CLAUDE.md").write_text("# parent v2\n", encoding="utf-8")
    _git(ownership_fixture, "add", "CLAUDE.md", "public")
    staged = _git(ownership_fixture, "diff", "--cached", "--name-only").stdout
    assert "public" in staged.splitlines()
    blocked = run_snippet(ownership_fixture, snippet, "assert_no_gitlink_staged")
    assert blocked.returncode != 0
    assert "gitlink" in blocked.stderr

    _git(ownership_fixture, "reset", "-q", "--", "public")
    clean = run_snippet(ownership_fixture, snippet, "assert_no_gitlink_staged")
    assert clean.returncode == 0, clean.stderr
