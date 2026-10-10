"""usaturn/xrift-poc#1: project_link.py が制作物に xrift-poc のスキルへのリンクを置く仕様。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.test_support.repos import EnvRepo, create_env_repo, create_owned_worktree, create_product

SCRIPT = Path(__file__).resolve().parents[1] / "project_link.py"
SPEC = importlib.util.spec_from_file_location("project_link", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
project_link = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = project_link
SPEC.loader.exec_module(project_link)

ORIGIN = "https://github.com/someone/my-world.git"
SKILLS = ("alpha", "xrift-world")


@pytest.fixture
def env(tmp_path: Path) -> EnvRepo:
    repo = create_env_repo(tmp_path / "workspace")
    for name in SKILLS:
        skill = repo.root / ".agents" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    (repo.root / ".agents" / "skills" / "not-a-skill").mkdir()
    return repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def links(project: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for parent in (".agents/skills", ".claude/skills"):
        directory = project / parent
        if directory.is_dir():
            for entry in directory.iterdir():
                if entry.is_symlink():
                    found[f"{parent}/{entry.name}"] = os.readlink(entry)
    return found


def exclude_text(project: Path) -> str:
    path = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_skill_names_lists_directories_with_skill_md(env: EnvRepo) -> None:
    assert project_link.skill_names(env.root) == list(SKILLS)


def test_link_places_absolute_links_and_excludes_them(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    report = project_link.link(project, env.root)
    expected = {
        f"{parent}/{name}": str(env.root.resolve() / ".agents" / "skills" / name)
        for parent in (".agents/skills", ".claude/skills")
        for name in SKILLS
    }
    assert links(project) == expected
    assert sorted(report.created) == sorted(expected)
    assert git(project, "status", "--porcelain") == ""
    text = exclude_text(project)
    assert text.count(project_link.BLOCK_BEGIN) == 1
    for relative in expected:
        assert f"/{relative}\n" in text


def test_rerun_does_not_duplicate_entries(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    first = exclude_text(project)
    report = project_link.link(project, env.root)
    assert report.created == []
    assert exclude_text(project) == first


def test_link_keeps_existing_entries(env: EnvRepo, tmp_path: Path) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    own_file = project / ".agents" / "skills" / "alpha"
    own_file.parent.mkdir(parents=True)
    own_file.write_text("利用者のファイル\n", encoding="utf-8")
    own_dir = project / ".claude" / "skills" / "alpha"
    own_dir.mkdir(parents=True)
    (own_dir / "SKILL.md").write_text("own\n", encoding="utf-8")
    broken = project / ".claude" / "skills" / "xrift-world"
    broken.symlink_to(tmp_path / "missing")
    (project / "AGENTS.md").write_text("制作物の指示\n", encoding="utf-8")

    report = project_link.link(project, env.root)

    assert own_file.read_text(encoding="utf-8") == "利用者のファイル\n"
    assert (own_dir / "SKILL.md").read_text(encoding="utf-8") == "own\n"
    assert os.readlink(broken) == str(tmp_path / "missing")
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "制作物の指示\n"
    assert sorted(report.created) == [".agents/skills/xrift-world"]
    assert len(report.kept) == 3


def test_link_does_not_write_through_a_symlinked_or_file_component(env: EnvRepo, tmp_path: Path) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    (project / ".claude").write_text("not a directory\n", encoding="utf-8")
    report = project_link.link(project, env.root)
    assert report.created == []
    assert list(outside.iterdir()) == []
    assert (project / ".claude").read_text(encoding="utf-8") == "not a directory\n"


def test_uninitialized_product_needs_no_links(env: EnvRepo) -> None:
    project = create_product(env.root, "items", "my-item", git=False)
    report = project_link.link(project, env.root)
    assert report.created == []
    assert report.note is not None
    assert not (project / ".agents").exists()


def test_link_rejects_paths_that_are_not_a_product_git_root(env: EnvRepo, tmp_path: Path) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    (project / "src").mkdir()
    outside = create_owned_worktree(project, tmp_path / "elsewhere")
    inside_env = create_owned_worktree(project, env.root / "tmp", name="wt")
    plain = create_product(env.root, "items", "plain", git=False)
    # 親の worktree の items/・worlds/ は、Git 未初期化の制作物（リンク不要）として扱わない。
    env_worktree = create_owned_worktree(env.root, tmp_path / "env-wt", name="feature")
    (env_worktree / "worlds" / "w").mkdir(parents=True)
    wrongs = (
        env.root,
        env.root / "worlds",
        project / "src",
        outside,
        inside_env,
        env_worktree / "worlds" / "w",
        project / "missing",
        plain / "missing",
        env.root / "worlds" / "missing",
    )
    for wrong in wrongs:
        for action in (project_link.link, project_link.unlink):
            with pytest.raises(project_link.LinkError) as caught:
                action(wrong, env.root)
            assert caught.value.code == 2, wrong
    assert not (env_worktree / "worlds" / "w" / ".agents").exists()


def test_link_works_in_a_product_worktree_inside_the_project(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    report = project_link.link(worktree, env.root)
    assert len(report.created) == 2 * len(SKILLS)
    assert git(worktree, "status", "--porcelain") == ""


def test_product_names_with_spaces_and_japanese(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "空白 ワールド", origin_url=ORIGIN)
    report = project_link.link(project, env.root)
    assert len(report.created) == 2 * len(SKILLS)


def test_unlink_removes_only_its_own_links(env: EnvRepo, tmp_path: Path) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    exclude.write_text(exclude.read_text(encoding="utf-8") + "/notes/\n", encoding="utf-8")
    project_link.link(project, env.root)
    replaced = project / ".claude" / "skills" / "alpha"
    replaced.unlink()
    replaced.symlink_to(tmp_path)

    report = project_link.unlink(project, env.root)

    assert links(project) == {".claude/skills/alpha": str(tmp_path)}
    assert ".claude/skills/alpha" not in report.removed
    text = exclude_text(project)
    assert project_link.BLOCK_BEGIN not in text
    assert "/notes/\n" in text


def test_broken_markers_stop_before_any_change(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    exclude.write_text(f"{project_link.BLOCK_BEGIN}\n/.agents/skills/alpha\n", encoding="utf-8")
    with pytest.raises(project_link.LinkError) as caught:
        project_link.link(project, env.root)
    assert caught.value.code == 1
    assert not (project / ".agents" / "skills").exists()


def test_unlink_in_a_worktree_keeps_the_exclusion_of_other_checkouts(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(project, env.root)
    project_link.link(worktree, env.root)

    project_link.unlink(worktree, env.root)

    assert git(project, "status", "--porcelain", "--", ".agents", ".claude") == ""
    assert links(worktree) == {}
    assert len(links(project)) == 2 * len(SKILLS)
    project_link.unlink(project, env.root)
    assert project_link.BLOCK_BEGIN not in exclude_text(project)


def test_replaced_link_is_no_longer_excluded_after_relink(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    replaced = project / ".agents" / "skills" / "alpha"
    replaced.unlink()
    replaced.mkdir()
    (replaced / "SKILL.md").write_text("own\n", encoding="utf-8")

    project_link.link(project, env.root)

    assert "/.agents/skills/alpha\n" not in exclude_text(project)
    assert "alpha/SKILL.md" in git(project, "status", "--porcelain", "-uall")


def test_relink_reports_user_files_hidden_by_another_worktrees_link(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    own = worktree / ".agents" / "skills" / "alpha"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("own\n", encoding="utf-8")
    project_link.link(project, env.root)

    report = project_link.link(worktree, env.root)

    assert any(".agents/skills/alpha" in note and "他の worktree" in note for note in report.kept)
