"""usaturn/xrift-poc#1: project_link.py が制作物に xrift-poc のスキルへのリンクを置く仕様。"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
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
# 親（xrift-poc）が追跡する .pi/ の設定のファイル。Issue #55 は、この 3 つが制作物から起動した Pi に届くことを求める。
# 親の .pi/ に設定のファイルを足したら、ここにも足す。
PI_CONFIG = ("APPEND_SYSTEM.md", "mcp.json", "settings.json")


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
    for parent in (".agents/skills", ".claude/skills", ".pi"):
        directory = project / parent
        if directory.is_dir():
            for entry in directory.iterdir():
                if entry.is_symlink():
                    found[f"{parent}/{entry.name}"] = os.readlink(entry)
    return found


def exclude_text(project: Path) -> str:
    path = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def pi_env(env: EnvRepo) -> EnvRepo:
    (env.root / ".pi").mkdir()
    for name in PI_CONFIG:
        (env.root / ".pi" / name).write_text(f"{name} of xrift-poc\n", encoding="utf-8")
    return env


def pi_expected(env: EnvRepo, names: Sequence[str] = PI_CONFIG) -> dict[str, str]:
    return {f".pi/{name}": str(env.root.resolve() / ".pi" / name) for name in names}


def pi_links(project: Path) -> dict[str, str]:
    return {relative: target for relative, target in links(project).items() if relative.startswith(".pi/")}


def make_product(env: EnvRepo, git_product: bool, name: str = "my-world") -> Path:
    return create_product(env.root, "worlds", name, git=git_product, origin_url=ORIGIN if git_product else None)


def check_ignore(repo: Path, *paths: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", *paths], capture_output=True, text=True, check=False
    )
    return result.stdout.splitlines()


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


@pytest.mark.parametrize("parent", [".agents/skills", ".claude/skills"])
@pytest.mark.parametrize("timing", ["before-link", "after-link", "new-worktree"])
def test_user_skill_directory_stays_visible_across_worktrees(env: EnvRepo, parent: str, timing: str) -> None:
    # 除外を実行時の状態だけで決めると、後から作ったディレクトリや worktree を保護できない。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    if timing == "new-worktree":
        project_link.link(project, env.root)
    worktree = create_owned_worktree(project, project / ".worktrees", name="利用者 worktree")
    if timing == "after-link":
        project_link.link(project, env.root)
    own = worktree / parent / "alpha" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("利用者のスキル\n", encoding="utf-8")
    other = worktree / parent / "other" / "SKILL.md"
    other.parent.mkdir()
    other.write_text("別名のスキル\n", encoding="utf-8")
    if timing == "before-link":
        project_link.link(project, env.root)

    status = git(worktree, "status", "--porcelain", "-uall", "-z")
    assert f"?? {parent}/alpha/SKILL.md\0" in status
    assert f"?? {parent}/other/SKILL.md\0" in status
    assert git(project, "check-ignore", f"{parent}/alpha").strip() == f"{parent}/alpha"
    git(worktree, "add", "-A")
    staged = set(git(worktree, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
    assert staged == {f"{parent}/alpha/SKILL.md", f"{parent}/other/SKILL.md"}
    assert own.read_text(encoding="utf-8") == "利用者のスキル\n"

    report = project_link.link(worktree, env.root)
    assert not any("git status" in note and f"{parent}/alpha" in note for note in report.kept)
    assert own.read_text(encoding="utf-8") == "利用者のスキル\n"


@pytest.mark.parametrize("parent", [".agents/skills", ".claude/skills"])
def test_user_skill_directory_blocks_worktree_removal(env: EnvRepo, parent: str) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    own = worktree / parent / "alpha" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("own\n", encoding="utf-8")

    result = subprocess.run(
        ["git", "-C", str(project), "worktree", "remove", str(worktree)], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, "利用者の未追跡スキルを含む worktree を削除してはいけない"
    assert own.read_text(encoding="utf-8") == "own\n"
    assert str(worktree) in git(project, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("parent", [".agents/skills", ".claude/skills"])
def test_user_skill_directory_blocks_switch(env: EnvRepo, parent: str) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    git(project, "switch", "-c", "with-skill")
    tracked = project / parent / "alpha" / "SKILL.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked\n", encoding="utf-8")
    git(project, "add", f"{parent}/alpha/SKILL.md")
    git(project, "commit", "-qm", "tracked skill")
    git(project, "switch", "main")
    project_link.link(project, env.root)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    own = worktree / parent / "alpha" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("own\n", encoding="utf-8")
    head = git(worktree, "rev-parse", "HEAD")

    result = subprocess.run(
        ["git", "-C", str(worktree), "switch", "with-skill"], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, "利用者の未追跡スキルをブランチの内容で上書きしてはいけない"
    assert own.read_text(encoding="utf-8") == "own\n"
    assert git(worktree, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("action", ["link", "unlink"])
def test_legacy_excludes_migrate_without_hiding_user_directories(env: EnvRepo, action: str) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    user = create_owned_worktree(project, project / ".worktrees", name="user")
    keeper = create_owned_worktree(project, project / ".worktrees", name="keeper")
    own = user / ".agents" / "skills" / "alpha" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("own\n", encoding="utf-8")
    project_link.link(project, env.root)
    project_link.link(keeper, env.root)
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    # 実際の旧形式を入力として与え、生成された行の照合ではなく Git で移行結果を確認する。
    old_entries = [f"/{parent}/{name}" for parent in (".agents/skills", ".claude/skills") for name in SKILLS]
    exclude.write_text(
        "\n".join(["/notes/", project_link.BLOCK_BEGIN, *old_entries, project_link.BLOCK_END, "/scratch/"]) + "\n",
        encoding="utf-8",
    )

    getattr(project_link, action)(project, env.root)

    assert "?? .agents/skills/alpha/SKILL.md\0" in git(user, "status", "--porcelain", "-uall", "-z")
    assert own.read_text(encoding="utf-8") == "own\n"
    assert git(keeper, "status", "--porcelain", "--", ".agents", ".claude") == ""
    assert git(project, "check-ignore", "notes/file", "scratch/file").splitlines() == ["notes/file", "scratch/file"]


def test_unlink_accepts_directory_reinclude_entries(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)

    report = project_link.unlink(project, env.root)

    assert report.kept == []
    assert links(project) == {}


@pytest.mark.parametrize("parent", [".agents/skills", ".claude/skills"])
def test_directory_protection_does_not_override_user_gitignore(env: EnvRepo, parent: str) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    ignore = worktree / ".gitignore"
    setting = f"/{Path(parent).parts[0]}/\n"
    ignore.write_text(setting, encoding="utf-8")
    own = worktree / parent / "alpha" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("own\n", encoding="utf-8")

    project_link.link(project, env.root)

    assert git(worktree, "status", "--porcelain", "-uall", "--", parent) == ""
    assert git(worktree, "check-ignore", f"{parent}/alpha/SKILL.md").strip() == f"{parent}/alpha/SKILL.md"
    assert ignore.read_text(encoding="utf-8") == setting
    assert own.read_text(encoding="utf-8") == "own\n"


@pytest.mark.parametrize("parent", [".agents/skills", ".claude/skills"])
@pytest.mark.parametrize("action", ["link", "unlink"])
@pytest.mark.parametrize("kind", ["file", "symlink-dir", "broken-symlink"])
@pytest.mark.parametrize("operation", ["main", "sibling"])
def test_hidden_user_entries_are_reported_from_every_worktree(
    env: EnvRepo, parent: str, action: str, kind: str, operation: str
) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    sibling = create_owned_worktree(project, project / ".worktrees", name="sibling")
    user = create_owned_worktree(project, project / ".worktrees", name="利用者 worktree")
    project_link.link(project, env.root)
    project_link.link(sibling, env.root)
    own = user / parent / "alpha"
    own.parent.mkdir(parents=True)
    destination = Path("../..") / ("my-skill" if kind == "symlink-dir" else "missing")
    if kind == "file":
        own.write_text("利用者のファイル\n", encoding="utf-8")
    else:
        if kind == "symlink-dir":
            (user / "my-skill").mkdir()
            (user / "my-skill" / "SKILL.md").write_text("利用者のスキル\n", encoding="utf-8")
        own.symlink_to(destination, target_is_directory=True)
    directory = user / parent / "xrift-world" / "SKILL.md"
    directory.parent.mkdir()
    directory.write_text("own directory\n", encoding="utf-8")
    target = project if operation == "main" else sibling

    report = getattr(project_link, action)(target, env.root)

    assert any(str(user) in note and f"{parent}/alpha" in note for note in report.kept)
    assert not any(str(user) in note and f"{parent}/xrift-world" in note for note in report.kept)
    assert git(user, "check-ignore", f"{parent}/alpha").strip() == f"{parent}/alpha"
    if kind == "file":
        assert own.read_text(encoding="utf-8") == "利用者のファイル\n"
    else:
        assert os.readlink(own) == str(destination)
        if kind == "symlink-dir":
            assert (own / "SKILL.md").read_text(encoding="utf-8") == "利用者のスキル\n"
    assert directory.read_text(encoding="utf-8") == "own directory\n"
    keeper = sibling if operation == "main" else project
    assert git(keeper, "status", "--porcelain", "--", ".agents", ".claude") == ""


def test_unlink_does_not_report_removed_exclusions(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(project, env.root)
    own = worktree / ".agents" / "skills" / "alpha"
    own.parent.mkdir(parents=True)
    own.write_text("own\n", encoding="utf-8")

    report = project_link.unlink(project, env.root)

    assert report.kept == []
    assert "?? .agents/skills/alpha\0" in git(worktree, "status", "--porcelain", "-uall", "-z")
    assert own.read_text(encoding="utf-8") == "own\n"


@pytest.mark.parametrize("component", [".agents", ".claude", ".agents/skills", ".claude/skills"])
def test_unlink_stops_without_change_when_its_link_resolves_outside(
    env: EnvRepo, tmp_path: Path, component: str
) -> None:
    # usaturn/xrift-poc#4: 設定ディレクトリを共有ディレクトリへのリンクに替えても、外のリンクを消さない。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    shared = tmp_path / "共有 設定"
    shared.mkdir()
    outside = shared / Path(component).name
    shutil.move(project / component, outside)
    (project / component).symlink_to(outside, target_is_directory=True)
    outside_link = outside / "alpha" if component.endswith("skills") else outside / "skills" / "alpha"
    before = (links(project), exclude_text(project))

    with pytest.raises(project_link.LinkError) as caught:
        project_link.unlink(project, env.root)

    assert caught.value.code == 1
    assert (links(project), exclude_text(project)) == before
    assert os.readlink(outside_link) == str(env.root.resolve() / ".agents" / "skills" / "alpha")
    assert shlex.quote(str(outside_link.parent.resolve() / "alpha")) in str(caught.value)


def test_unlink_suggests_a_command_that_clears_the_shared_links(env: EnvRepo, tmp_path: Path) -> None:
    # .agents と .claude が同じ共有ディレクトリを指すと、同じリンクが 2 行に出る。表示したコマンドはそのまま
    # 実行できる。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    shared = tmp_path / "共有 設定"
    shutil.move(project / ".agents", shared)
    shutil.rmtree(project / ".claude")
    for name in (".agents", ".claude"):
        (project / name).symlink_to(shared, target_is_directory=True)

    with pytest.raises(project_link.LinkError) as caught:
        project_link.unlink(project, env.root)
    line = next(line for line in str(caught.value).splitlines() if "rm -- " in line)
    subprocess.run(["bash", "-c", line[line.index("rm -- ") :]], check=True)

    assert os.listdir(shared / "skills") == []
    report = project_link.unlink(project, env.root)
    assert report.removed == []
    assert project_link.BLOCK_BEGIN not in exclude_text(project)


def test_unlink_in_a_worktree_keeps_links_of_the_main_checkout(env: EnvRepo) -> None:
    # worktree の .agents が main checkout の .agents を指しても、main checkout のリンクを残して自分のリンクを外す。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(project, env.root)
    (worktree / ".agents").symlink_to(project / ".agents", target_is_directory=True)
    project_link.link(worktree, env.root)
    before = (links(project), exclude_text(project))

    report = project_link.unlink(worktree, env.root)

    assert sorted(report.removed) == sorted(f".claude/skills/{name}" for name in SKILLS)
    assert all(any(f".agents/skills/{name}" in line for line in report.kept) for name in SKILLS)
    assert (links(project), exclude_text(project)) == before
    assert project_link.unlink(worktree, env.root).removed == []


def test_unlink_in_the_main_checkout_keeps_links_of_a_nested_worktree(env: EnvRepo) -> None:
    # main checkout の .agents が、中に作った worktree の .agents を指しても、その worktree のリンクを消さない。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(worktree, env.root)
    (project / ".agents").symlink_to(worktree / ".agents", target_is_directory=True)
    project_link.link(project, env.root)
    before = (links(worktree), exclude_text(project))

    report = project_link.unlink(project, env.root)

    assert sorted(report.removed) == sorted(f".claude/skills/{name}" for name in SKILLS)
    assert (links(worktree), exclude_text(project)) == before


def test_unlink_follows_a_component_linked_inside_the_worktree(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    shutil.rmtree(project / ".claude" / "skills")
    (project / ".claude" / "skills").symlink_to(Path("..") / ".agents" / "skills", target_is_directory=True)

    report = project_link.unlink(project, env.root)

    assert sorted(report.removed) == sorted(f".agents/skills/{name}" for name in SKILLS)
    assert links(project) == {}
    assert project_link.BLOCK_BEGIN not in exclude_text(project)


def test_unlink_continues_when_an_outside_component_holds_no_link_of_ours(env: EnvRepo, tmp_path: Path) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    shared = tmp_path / "共有 設定"
    (shared / "skills").mkdir(parents=True)
    own = shared / "skills" / "alpha"
    own.symlink_to(tmp_path / "利用者のスキル", target_is_directory=True)
    shutil.rmtree(project / ".agents")
    (project / ".agents").symlink_to(shared, target_is_directory=True)

    report = project_link.unlink(project, env.root)

    assert os.readlink(own) == str(tmp_path / "利用者のスキル")
    assert os.listdir(shared / "skills") == ["alpha"]
    assert sorted(report.removed) == sorted(f".claude/skills/{name}" for name in SKILLS)
    assert project_link.BLOCK_BEGIN not in exclude_text(project)


# usaturn/xrift-poc#55: Pi は project の設定を cwd の .pi/ からだけ読む。制作物の .pi/ に親の設定へのリンクを置く。


@pytest.mark.parametrize("pre_created", [False, True])
def test_link_places_pi_links_in_a_git_product_and_ignores_them(pi_env: EnvRepo, pre_created: bool) -> None:
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)
    if pre_created:
        (project / ".pi").mkdir()

    report = project_link.link(project, pi_env.root)

    expected = pi_expected(pi_env)
    assert pi_links(project) == expected
    assert set(expected) <= set(report.created)
    assert git(project, "status", "--porcelain") == ""
    paths = [*sorted(expected), ".pi/.gitignore"]
    assert check_ignore(project, *paths) == paths
    git(project, "add", "-A")
    assert git(project, "diff", "--cached", "--name-only") == ""
    assert project_link.link(project, pi_env.root).created == []


@pytest.mark.parametrize("git_product", [True])
def test_link_skips_pi_files_the_root_does_not_have(env: EnvRepo, git_product: bool) -> None:
    (env.root / ".pi").mkdir()
    (env.root / ".pi" / "APPEND_SYSTEM.md").write_text("rules\n", encoding="utf-8")
    project = make_product(env, git_product)

    project_link.link(project, env.root)

    assert pi_links(project) == pi_expected(env, ["APPEND_SYSTEM.md"])
    if git_product:
        # 置いていない名前は無視しない。後から作った制作物自身の .pi/mcp.json は git status に出る。
        (project / ".pi" / "mcp.json").write_text("{}\n", encoding="utf-8")
        assert "?? .pi/mcp.json\0" in git(project, "status", "--porcelain", "-uall", "-z")


@pytest.mark.parametrize("git_product", [True])
def test_link_keeps_existing_pi_entries(pi_env: EnvRepo, tmp_path: Path, git_product: bool) -> None:
    project = make_product(pi_env, git_product)
    pi = project / ".pi"
    pi.mkdir()
    (pi / "APPEND_SYSTEM.md").write_text("制作物の規則\n", encoding="utf-8")
    (pi / "mcp.json").mkdir()
    (pi / "mcp.json" / "own.txt").write_text("own\n", encoding="utf-8")
    (pi / "settings.json").symlink_to(tmp_path / "missing")

    report = project_link.link(project, pi_env.root)

    assert (pi / "APPEND_SYSTEM.md").read_text(encoding="utf-8") == "制作物の規則\n"
    assert (pi / "mcp.json" / "own.txt").read_text(encoding="utf-8") == "own\n"
    assert os.readlink(pi / "settings.json") == str(tmp_path / "missing")
    assert not any(entry.startswith(".pi/") for entry in report.created)
    assert sum(entry.startswith(".pi/") for entry in report.kept) == 3
    assert not (pi / ".gitignore").exists()
    if git_product:
        status = git(project, "status", "--porcelain", "-uall", "-z")
        for path in (".pi/APPEND_SYSTEM.md", ".pi/mcp.json/own.txt", ".pi/settings.json"):
            assert f"?? {path}\0" in status


def test_link_keeps_a_tracked_own_pi_file(pi_env: EnvRepo) -> None:
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)
    own = project / ".pi" / "mcp.json"
    own.parent.mkdir()
    own.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    git(project, "add", ".pi/mcp.json")
    git(project, "commit", "-qm", "own mcp.json")

    project_link.link(project, pi_env.root)

    assert own.read_text(encoding="utf-8") == '{"mcpServers": {}}\n'
    assert pi_links(project) == pi_expected(pi_env, ["APPEND_SYSTEM.md", "settings.json"])
    assert git(project, "status", "--porcelain") == ""
    own.write_text('{"mcpServers": {"own": {}}}\n', encoding="utf-8")
    assert " M .pi/mcp.json\0" in git(project, "status", "--porcelain", "-z")


@pytest.mark.parametrize("git_product", [True])
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_link_does_not_write_through_a_symlinked_or_file_pi(
    pi_env: EnvRepo, tmp_path: Path, git_product: bool, kind: str
) -> None:
    project = make_product(pi_env, git_product)
    outside = tmp_path / "outside-pi"
    outside.mkdir()
    if kind == "symlink":
        (project / ".pi").symlink_to(outside, target_is_directory=True)
    else:
        (project / ".pi").write_text("not a directory\n", encoding="utf-8")

    report = project_link.link(project, pi_env.root)

    assert not any(entry.startswith(".pi/") for entry in report.created)
    assert list(outside.iterdir()) == []
    if kind == "file":
        assert (project / ".pi").read_text(encoding="utf-8") == "not a directory\n"


@pytest.mark.parametrize("git_product", [True])
def test_link_leaves_pi_alone_when_the_product_has_its_own_pi_gitignore(pi_env: EnvRepo, git_product: bool) -> None:
    project = make_product(pi_env, git_product)
    own = project / ".pi" / ".gitignore"
    own.parent.mkdir()
    own.write_text("own\n", encoding="utf-8")

    report = project_link.link(project, pi_env.root)

    assert pi_links(project) == {}
    assert own.read_text(encoding="utf-8") == "own\n"
    assert any(entry.startswith(".pi/.gitignore") for entry in report.kept)


def test_pi_links_do_not_hide_files_of_other_worktrees(pi_env: EnvRepo) -> None:
    # info/exclude は全 worktree で共有され、ファイルは否定行で守れない。.pi/ の無視を他の worktree に広げない。
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, pi_env.root)
    user = create_owned_worktree(project, project / ".worktrees", name="利用者 worktree")
    own = [user / ".pi" / "mcp.json", user / ".pi" / "settings.json"]
    own[0].parent.mkdir()
    for path in own:
        path.write_text("{}\n", encoding="utf-8")

    status = git(user, "status", "--porcelain", "-uall", "-z")
    result = subprocess.run(
        ["git", "-C", str(project), "worktree", "remove", str(user)], capture_output=True, text=True, check=False
    )

    assert "?? .pi/mcp.json\0" in status
    assert "?? .pi/settings.json\0" in status
    assert result.returncode != 0, "制作物自身の未追跡の .pi/ のファイルを含む worktree を削除してはいけない"
    assert all(path.read_text(encoding="utf-8") == "{}\n" for path in own)


def test_relink_stops_ignoring_a_pi_link_replaced_by_an_own_file(pi_env: EnvRepo) -> None:
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, pi_env.root)
    own = project / ".pi" / "mcp.json"
    own.unlink()
    own.write_text("{}\n", encoding="utf-8")

    project_link.link(project, pi_env.root)

    assert "?? .pi/mcp.json\0" in git(project, "status", "--porcelain", "-uall", "-z")
    others = sorted(set(pi_expected(pi_env)) - {".pi/mcp.json"})
    assert check_ignore(project, *others) == others


@pytest.mark.parametrize("git_product", [True])
def test_unlink_removes_only_its_own_pi_links(pi_env: EnvRepo, git_product: bool) -> None:
    project = make_product(pi_env, git_product)
    project_link.link(project, pi_env.root)
    own = project / ".pi" / "settings.json"
    own.unlink()
    own.write_text("{}\n", encoding="utf-8")

    report = project_link.unlink(project, pi_env.root)

    assert pi_links(project) == {}
    assert own.read_text(encoding="utf-8") == "{}\n"
    removed = {entry for entry in report.removed if entry.startswith(".pi/")}
    assert removed == set(pi_expected(pi_env)) - {".pi/settings.json"}
    assert not (project / ".pi" / ".gitignore").exists()
    if git_product:
        assert "?? .pi/settings.json\0" in git(project, "status", "--porcelain", "-uall", "-z")


def test_link_and_unlink_pi_links_in_a_worktree_inside_the_product(pi_env: EnvRepo) -> None:
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(project, pi_env.root)
    project_link.link(worktree, pi_env.root)
    assert pi_links(worktree) == pi_expected(pi_env)
    assert git(worktree, "status", "--porcelain") == ""

    project_link.unlink(worktree, pi_env.root)

    assert pi_links(worktree) == {}
    assert not (worktree / ".pi" / ".gitignore").exists()
    assert pi_links(project) == pi_expected(pi_env)
    assert git(project, "status", "--porcelain", "--", ".pi") == ""


@pytest.mark.parametrize("git_product", [True])
def test_unlink_keeps_links_behind_a_symlinked_pi(pi_env: EnvRepo, tmp_path: Path, git_product: bool) -> None:
    # .pi を共有ディレクトリへのリンクに替えても、その中（制作物の外かもしれない）のリンクを消さない。
    project = make_product(pi_env, git_product)
    project_link.link(project, pi_env.root)
    shared = tmp_path / "共有 設定"
    shared.mkdir()
    shutil.move(project / ".pi", shared / ".pi")
    (project / ".pi").symlink_to(shared / ".pi", target_is_directory=True)

    report = project_link.unlink(project, pi_env.root)

    assert not any(entry.startswith(".pi/") for entry in report.removed)
    assert any(entry.startswith(".pi") for entry in report.kept)
    for relative, source in pi_expected(pi_env).items():
        assert os.readlink(shared / relative) == source
    assert (shared / ".pi" / ".gitignore").is_file()
