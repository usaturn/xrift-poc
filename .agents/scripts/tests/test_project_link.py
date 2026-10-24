"""usaturn/xrift-poc#1: project_link.py が制作物に xrift-poc のスキルへのリンクを置く仕様。"""

from __future__ import annotations

import importlib.util
import os
import shutil
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


def pi_expected(env: EnvRepo) -> dict[str, str]:
    return {f".pi/{name}": str(env.root.resolve() / ".pi" / name) for name in PI_CONFIG}


def pi_links(project: Path) -> dict[str, str]:
    return {relative: target for relative, target in links(project).items() if relative.startswith(".pi/")}


def make_product(env: EnvRepo, git_product: bool, name: str = "my-world") -> Path:
    return create_product(env.root, "worlds", name, git=git_product, origin_url=ORIGIN if git_product else None)


def git_init(project: Path) -> None:
    """Git 未初期化の制作物を、利用者と同じく後から Git で管理し始める（commit はしない）。"""
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "commit.gpgsign", "false"),
        ("remote", "add", "origin", ORIGIN),
    ):
        git(project, *args)


def check_ignore(repo: Path, *paths: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", *paths], capture_output=True, text=True, check=False
    )
    return result.stdout.splitlines()


def test_link_places_absolute_links_and_excludes_them(env: EnvRepo) -> None:
    """置いたリンクを除外しないと、
    絶対パスのリンクが未追跡に出て、`git add -A` で制作物（公開しうる）に黙って commit される。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    expected = {
        f"{parent}/{name}": str(env.root.resolve() / ".agents" / "skills" / name)
        for parent in (".agents/skills", ".claude/skills")
        for name in SKILLS
    }
    assert links(project) == expected
    assert git(project, "status", "--porcelain") == ""


def test_link_keeps_existing_entries(env: EnvRepo, tmp_path: Path) -> None:
    """同名の利用者のファイル・ディレクトリ・壊れたリンクを、リンクを置くときに黙って上書きする。"""
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

    project_link.link(project, env.root)

    assert own_file.read_text(encoding="utf-8") == "利用者のファイル\n"
    assert (own_dir / "SKILL.md").read_text(encoding="utf-8") == "own\n"
    assert os.readlink(broken) == str(tmp_path / "missing")
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "制作物の指示\n"


def test_link_does_not_write_through_a_symlinked_component(env: EnvRepo, tmp_path: Path) -> None:
    """途中の symlink をたどって、外のディレクトリ（共有の設定など）にリンクを黙って書き込む。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    project_link.link(project, env.root)
    assert list(outside.iterdir()) == []


def test_pi_links_of_an_uninitialized_product_stay_out_of_git_after_git_init(pi_env: EnvRepo) -> None:
    """Git 未初期化の制作物に置いた `.pi/` のリンクが、`git init` 後の `git add -A` で黙って制作物に commit される。"""
    project = create_product(pi_env.root, "worlds", "my-world", git=False)
    project_link.link(project, pi_env.root)
    assert links(project) == pi_expected(pi_env)
    git_init(project)

    git(project, "add", "-A")
    staged = git(project, "diff", "--cached", "--name-only", "-z").split("\0")

    assert "README.md" in staged
    assert not any(path.startswith(".pi/") for path in staged)
    git(project, "commit", "-qm", "first commit of the product")
    project_link.link(project, pi_env.root)
    skill_links = {f"{parent}/{name}" for parent in (".agents/skills", ".claude/skills") for name in SKILLS}
    assert skill_links <= set(links(project))
    assert git(project, "status", "--porcelain") == ""
    project_link.unlink(project, pi_env.root)
    assert links(project) == {}
    assert git(project, "status", "--porcelain") == ""


def test_link_rejects_paths_that_are_not_a_product_git_root(env: EnvRepo) -> None:
    """Git の root でない制作物の中のディレクトリを受け付けると、
    `info/exclude` の除外が効かない場所にリンクを置き、`git add -A` で黙って commit される。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    (project / "src").mkdir()

    with pytest.raises(project_link.LinkError):
        project_link.link(project / "src", env.root)

    assert not (project / "src" / ".agents").exists()


def test_link_works_in_a_product_worktree_inside_the_project(env: EnvRepo) -> None:
    """制作物の中の worktree（`worlds/<name>/.worktrees/<branch>`）で link したときに、
    Git が読まない worktree ごとの場所に除外を書くと、リンクが未追跡に出て `git add -A` で黙って commit される。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")

    project_link.link(worktree, env.root)

    assert git(worktree, "status", "--porcelain") == ""


def test_unlink_removes_only_its_own_links(env: EnvRepo, tmp_path: Path) -> None:
    """利用者が差し替えたリンクや、管理ブロックの外の利用者の除外行を、unlink が黙って消す。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    exclude.write_text(exclude.read_text(encoding="utf-8") + "/notes/\n", encoding="utf-8")
    project_link.link(project, env.root)
    # xrift-link の後に利用者が足した除外の行は、管理ブロックの後ろに入る。unlink の後も残す。
    exclude.write_text(exclude.read_text(encoding="utf-8") + "/scratch/\n", encoding="utf-8")
    replaced = project / ".claude" / "skills" / "alpha"
    replaced.unlink()
    replaced.symlink_to(tmp_path)

    project_link.unlink(project, env.root)

    assert links(project) == {".claude/skills/alpha": str(tmp_path)}
    text = exclude_text(project)
    assert project_link.BLOCK_BEGIN not in text
    assert "/notes/\n" in text
    assert "/scratch/\n" in text


def test_broken_markers_stop_before_any_change(env: EnvRepo) -> None:
    """目印が壊れたまま進むと、利用者の除外行を管理ブロックとみなして黙って書き換える。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    exclude.write_text(f"{project_link.BLOCK_BEGIN}\n/.agents/skills/alpha\n", encoding="utf-8")
    with pytest.raises(project_link.LinkError):
        project_link.link(project, env.root)
    assert not (project / ".agents" / "skills").exists()


def test_unlink_in_a_worktree_keeps_the_exclusion_of_other_checkouts(env: EnvRepo) -> None:
    """worktree で unlink すると、共有の `info/exclude` から main checkout のリンクの除外を黙って外し、
    そのリンクが `git add -A` で commit される。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(project, env.root)
    project_link.link(worktree, env.root)

    project_link.unlink(worktree, env.root)

    assert git(project, "status", "--porcelain", "--", ".agents", ".claude") == ""
    assert links(worktree) == {}
    assert len(links(project)) == 2 * len(SKILLS)


def test_user_skill_directory_stays_visible_across_worktrees(env: EnvRepo) -> None:
    """同名の利用者のスキルのディレクトリを共有の除外で隠すと、
    `git status` に出ず、worktree を消すと黙って一緒に消える。"""
    # 除外を実行時の状態だけで決めると、後から作ったディレクトリや worktree を保護できない。
    parent = ".claude/skills"
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    worktree = create_owned_worktree(project, project / ".worktrees", name="利用者 worktree")
    own = worktree / parent / "alpha" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("利用者のスキル\n", encoding="utf-8")
    other = worktree / parent / "other" / "SKILL.md"
    other.parent.mkdir()
    other.write_text("別名のスキル\n", encoding="utf-8")

    status = git(worktree, "status", "--porcelain", "-uall", "-z")
    assert f"?? {parent}/alpha/SKILL.md\0" in status
    assert f"?? {parent}/other/SKILL.md\0" in status
    assert git(project, "check-ignore", f"{parent}/alpha").strip() == f"{parent}/alpha"
    git(worktree, "add", "-A")
    staged = set(git(worktree, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
    assert staged == {f"{parent}/alpha/SKILL.md", f"{parent}/other/SKILL.md"}
    assert own.read_text(encoding="utf-8") == "利用者のスキル\n"

    project_link.link(worktree, env.root)
    assert own.read_text(encoding="utf-8") == "利用者のスキル\n"


def test_legacy_excludes_migrate_without_hiding_user_directories(env: EnvRepo) -> None:
    """管理ブロックを作り直すときに、前後の利用者の除外行を落とす（無視していたファイルが黙って commit 対象になる）か、
    旧形式の除外で利用者のディレクトリを隠したままにする。"""
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

    project_link.link(project, env.root)

    assert "?? .agents/skills/alpha/SKILL.md\0" in git(user, "status", "--porcelain", "-uall", "-z")
    assert own.read_text(encoding="utf-8") == "own\n"
    assert git(keeper, "status", "--porcelain", "--", ".agents", ".claude") == ""
    assert git(project, "check-ignore", "notes/file", "scratch/file").splitlines() == ["notes/file", "scratch/file"]


def test_hidden_user_entries_are_reported_from_every_worktree(env: EnvRepo) -> None:
    """共有の除外で隠れる、別の worktree の同名の利用者のファイルを報告しないと、
    利用者は気づかず、worktree を消すと黙って一緒に消える。"""
    parent = ".agents/skills"
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    sibling = create_owned_worktree(project, project / ".worktrees", name="sibling")
    user = create_owned_worktree(project, project / ".worktrees", name="利用者 worktree")
    project_link.link(project, env.root)
    project_link.link(sibling, env.root)
    own = user / parent / "alpha"
    own.parent.mkdir(parents=True)
    own.write_text("利用者のファイル\n", encoding="utf-8")
    directory = user / parent / "xrift-world" / "SKILL.md"
    directory.parent.mkdir()
    directory.write_text("own directory\n", encoding="utf-8")

    report = project_link.link(project, env.root)

    assert any(str(user) in note and f"{parent}/alpha" in note for note in report.kept)
    assert not any(str(user) in note and f"{parent}/xrift-world" in note for note in report.kept)
    assert git(user, "check-ignore", f"{parent}/alpha").strip() == f"{parent}/alpha"
    assert own.read_text(encoding="utf-8") == "利用者のファイル\n"
    assert directory.read_text(encoding="utf-8") == "own directory\n"
    assert git(sibling, "status", "--porcelain", "--", ".agents", ".claude") == ""

    # sibling のリンクで共有の除外は残るので、main checkout で unlink しても、利用者のファイルは隠れたまま報告される。
    report = project_link.unlink(project, env.root)

    assert any(str(user) in note and f"{parent}/alpha" in note for note in report.kept)


def test_unlink_stops_without_change_when_its_link_resolves_outside(env: EnvRepo, tmp_path: Path) -> None:
    """設定のディレクトリが共有ディレクトリへのリンクのとき、そこにある自分のリンクを消すと、
    それを使う他のプロジェクトからも黙って消える（#4）。"""
    # usaturn/xrift-poc#4: 設定ディレクトリを共有ディレクトリへのリンクに替えても、外のリンクを消さない。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, env.root)
    shared = tmp_path / "共有 設定"
    shared.mkdir()
    outside = shared / ".agents"
    shutil.move(project / ".agents", outside)
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    outside_link = outside / "skills" / "alpha"
    before = (links(project), exclude_text(project))

    with pytest.raises(project_link.LinkError):
        project_link.unlink(project, env.root)

    assert (links(project), exclude_text(project)) == before
    assert os.readlink(outside_link) == str(env.root.resolve() / ".agents" / "skills" / "alpha")


def test_unlink_in_a_worktree_keeps_links_of_the_main_checkout(env: EnvRepo) -> None:
    """worktree の `.agents` が main checkout の `.agents` を指すとき、
    worktree の unlink が main checkout のリンクと除外を黙って消し、
    main checkout のエージェントがスキルを読まなくなる。"""
    # worktree の .agents が main checkout の .agents を指しても、main checkout のリンクを残す。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(project, env.root)
    (worktree / ".agents").symlink_to(project / ".agents", target_is_directory=True)
    project_link.link(worktree, env.root)
    before = (links(project), exclude_text(project))

    project_link.unlink(worktree, env.root)

    assert (links(project), exclude_text(project)) == before


def test_unlink_in_the_main_checkout_keeps_links_of_a_nested_worktree(env: EnvRepo) -> None:
    """main checkout の `.agents` が中の worktree の `.agents` を指すとき、
    main checkout の unlink がその worktree のリンクを黙って消す（境界: まれな構成）。"""
    # main checkout の .agents が、中に作った worktree の .agents を指しても、その worktree のリンクを消さない。
    project = create_product(env.root, "worlds", "my-world", origin_url=ORIGIN)
    worktree = create_owned_worktree(project, project / ".worktrees", name="feature")
    project_link.link(worktree, env.root)
    (project / ".agents").symlink_to(worktree / ".agents", target_is_directory=True)
    project_link.link(project, env.root)
    before = (links(worktree), exclude_text(project))

    project_link.unlink(project, env.root)

    assert (links(worktree), exclude_text(project)) == before


# usaturn/xrift-poc#55: Pi は project の設定を cwd の .pi/ からだけ読む。制作物の .pi/ に親の設定へのリンクを置く。


def test_link_places_pi_links_in_a_git_product_and_ignores_them(pi_env: EnvRepo) -> None:
    """制作物に置いた `.pi/` のリンクを無視しないと、`git add -A` で絶対パスのリンクが黙って commit される。"""
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)

    project_link.link(project, pi_env.root)

    expected = pi_expected(pi_env)
    assert pi_links(project) == expected
    assert git(project, "status", "--porcelain") == ""
    paths = [*sorted(expected), ".pi/.gitignore"]
    assert check_ignore(project, *paths) == paths
    git(project, "add", "-A")
    assert git(project, "diff", "--cached", "--name-only") == ""


def test_link_keeps_existing_pi_entries(pi_env: EnvRepo, tmp_path: Path) -> None:
    """制作物自身の `.pi/` のファイル・ディレクトリ・壊れたリンクを、リンクを置くときに黙って上書きする。"""
    project = make_product(pi_env, git_product=True)
    pi = project / ".pi"
    pi.mkdir()
    (pi / "APPEND_SYSTEM.md").write_text("制作物の規則\n", encoding="utf-8")
    (pi / "mcp.json").mkdir()
    (pi / "mcp.json" / "own.txt").write_text("own\n", encoding="utf-8")
    (pi / "settings.json").symlink_to(tmp_path / "missing")

    project_link.link(project, pi_env.root)

    assert (pi / "APPEND_SYSTEM.md").read_text(encoding="utf-8") == "制作物の規則\n"
    assert (pi / "mcp.json" / "own.txt").read_text(encoding="utf-8") == "own\n"
    assert os.readlink(pi / "settings.json") == str(tmp_path / "missing")
    assert not (pi / ".gitignore").exists()
    status = git(project, "status", "--porcelain", "-uall", "-z")
    for path in (".pi/APPEND_SYSTEM.md", ".pi/mcp.json/own.txt", ".pi/settings.json"):
        assert f"?? {path}\0" in status


def test_link_does_not_write_through_a_symlinked_pi(pi_env: EnvRepo, tmp_path: Path) -> None:
    """`.pi` が外のディレクトリへの symlink のとき、その中（共有の設定など）にリンクを黙って書き込む。"""
    project = make_product(pi_env, git_product=False)
    outside = tmp_path / "outside-pi"
    outside.mkdir()
    (project / ".pi").symlink_to(outside, target_is_directory=True)

    project_link.link(project, pi_env.root)

    assert list(outside.iterdir()) == []


def test_link_leaves_pi_alone_when_the_product_has_its_own_pi_gitignore(pi_env: EnvRepo) -> None:
    """制作物自身の `.pi/.gitignore` を黙って書き換えるか、
    無視できないリンクを置いて `git add -A` で commit させる。"""
    project = make_product(pi_env, git_product=True)
    own = project / ".pi" / ".gitignore"
    own.parent.mkdir()
    own.write_text("own\n", encoding="utf-8")

    project_link.link(project, pi_env.root)

    assert pi_links(project) == {}
    assert own.read_text(encoding="utf-8") == "own\n"


def test_pi_links_do_not_hide_files_of_other_worktrees(pi_env: EnvRepo) -> None:
    """共有の `info/exclude` で `.pi/` のリンクを無視すると、
    別の worktree の制作物自身の `.pi/` のファイルまで隠れ、worktree を消すと黙って一緒に消える。"""
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
    """リンクを制作物自身のファイルに替えた後も `.pi/.gitignore` で無視し続けると、
    そのファイルが `git status` に出ず、黙って commit されない（worktree なら一緒に消える）。"""
    project = create_product(pi_env.root, "worlds", "my-world", origin_url=ORIGIN)
    project_link.link(project, pi_env.root)
    own = project / ".pi" / "mcp.json"
    own.unlink()
    own.write_text("{}\n", encoding="utf-8")

    project_link.link(project, pi_env.root)

    assert "?? .pi/mcp.json\0" in git(project, "status", "--porcelain", "-uall", "-z")
    others = sorted(set(pi_expected(pi_env)) - {".pi/mcp.json"})
    assert check_ignore(project, *others) == others


def test_unlink_removes_only_its_own_pi_links(pi_env: EnvRepo) -> None:
    """unlink が、リンクから差し替えた制作物自身の `.pi/` のファイルを黙って消す。"""
    project = make_product(pi_env, git_product=True)
    project_link.link(project, pi_env.root)
    own = project / ".pi" / "settings.json"
    own.unlink()
    own.write_text("{}\n", encoding="utf-8")

    project_link.unlink(project, pi_env.root)

    assert pi_links(project) == {}
    assert own.read_text(encoding="utf-8") == "{}\n"
    assert not (project / ".pi" / ".gitignore").exists()
    assert "?? .pi/settings.json\0" in git(project, "status", "--porcelain", "-uall", "-z")


def test_unlink_keeps_links_behind_a_symlinked_pi(pi_env: EnvRepo, tmp_path: Path) -> None:
    """`.pi` が共有ディレクトリへの symlink のとき、
    unlink がその中のリンクを消し、共有している他のプロジェクトから黙って消える。"""
    # .pi を共有ディレクトリへのリンクに替えても、その中（制作物の外かもしれない）のリンクを消さない。
    project = make_product(pi_env, git_product=True)
    project_link.link(project, pi_env.root)
    shared = tmp_path / "共有 設定"
    shared.mkdir()
    shutil.move(project / ".pi", shared / ".pi")
    (project / ".pi").symlink_to(shared / ".pi", target_is_directory=True)

    project_link.unlink(project, pi_env.root)

    for relative, source in pi_expected(pi_env).items():
        assert os.readlink(shared / relative) == source
    assert (shared / ".pi" / ".gitignore").is_file()
