"""usaturn/xrift-poc#1: route_target.py が、作業するパスの Git・Issue・PR の操作先を判定する仕様。"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.test_support.repos import EnvRepo, create_env_repo, create_owned_worktree, create_product

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "route_target.py"
SPEC = importlib.util.spec_from_file_location("route_target", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
route_target = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(route_target)

WORLD_ORIGIN = "https://github.com/someone/my-world.git"


@pytest.fixture
def env(tmp_path: Path) -> EnvRepo:
    return create_env_repo(tmp_path / "workspace")


def summary(path: Path, env: EnvRepo) -> tuple[str, str, str | None]:
    result = route_target.route(path, env.root)
    return result.kind, result.code, result.repository


def test_env_root_and_its_subdirectories_are_env(env: EnvRepo) -> None:
    (env.root / "tools").mkdir(exist_ok=True)
    assert summary(env.root, env) == ("env", "env", "usaturn/xrift-poc")
    assert summary(env.root / "tools", env) == ("env", "env", "usaturn/xrift-poc")


def test_env_worktrees_inside_and_outside_the_root_are_env(env: EnvRepo, tmp_path: Path) -> None:
    inside = create_owned_worktree(env.root, env.root / ".worktrees", name="feature")
    outside = create_owned_worktree(env.root, tmp_path / "elsewhere", name="feature")
    for worktree in (inside, outside):
        result = route_target.route(worktree, env.root)
        assert (result.kind, result.repository, result.git_root) == (
            "env",
            "usaturn/xrift-poc",
            str(worktree.resolve()),
        )


@pytest.mark.parametrize("kind", ["items", "worlds"])
def test_work_directories_themselves_stop(env: EnvRepo, kind: str) -> None:
    assert summary(env.root / kind, env)[:2] == ("stop", "product_base")


def test_uninitialized_product_stops_instead_of_using_the_ancestor_git(env: EnvRepo) -> None:
    project = create_product(env.root, "worlds", "my-world", git=False)
    (project / "src").mkdir()
    # 祖先の xrift-poc が Git のルートとして見つかることが、誤判定の前提になる。
    found = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    assert Path(found.stdout.strip()) == env.root.resolve()
    for path in (project, project / "src"):
        result = route_target.route(path, env.root)
        assert (result.kind, result.code, result.repository) == ("stop", "product_uninitialized", None)
        assert result.project_root == str(project.resolve())


@pytest.mark.parametrize(
    "origin",
    [WORLD_ORIGIN, "git@github.com:someone/my-world.git", "ssh://git@github.com/someone/my-world"],
)
def test_product_with_a_github_origin(env: EnvRepo, origin: str) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=origin)
    # 制作物の public/ は XRift のアセットディレクトリで、特別に扱わない。
    (project / "public").mkdir()
    for path in (project, project / "public"):
        result = route_target.route(path, env.root)
        assert (result.kind, result.code, result.repository) == ("product", "product", "someone/my-world")
        assert (result.git_root, result.project_root) == (str(project.resolve()), str(project.resolve()))


@pytest.mark.parametrize("origin", [None, "/srv/git/my-item.git", "https://gitlab.com/someone/my-item.git"])
def test_product_without_a_github_origin_has_no_repository(env: EnvRepo, origin: str | None) -> None:
    project = create_product(env.root, "items", "my-item", origin_url=origin)
    assert summary(project, env) == ("product", "product_without_github", None)


# 親を指す origin の書き方。正規形の GitHub の URL に直せないもの
# （user@・SSH のホスト別名・大文字のホスト名・http）も含める。
ENV_ORIGIN_SPELLINGS = [
    "https://github.com/usaturn/xrift-poc.git",
    "git@github.com:USATURN/XRIFT-POC.git",
    "https://user@github.com/usaturn/xrift-poc.git",
    "github-personal:usaturn/xrift-poc.git",
    "https://GitHub.com/usaturn/xrift-poc",
    "http://github.com/usaturn/xrift-poc/",
]


@pytest.mark.parametrize("origin", ENV_ORIGIN_SPELLINGS)
def test_product_whose_origin_is_the_env_repository_stops(env: EnvRepo, origin: str) -> None:
    project = create_product(env.root, "items", "my-item", origin_url=origin)
    assert summary(project, env) == ("stop", "product_origin_is_env", None)


@pytest.mark.parametrize(
    "origin", ["https://github.com/usaturn/xrift-poc-assets.git", "git@github.com:notusaturn/xrift-poc.git"]
)
def test_product_whose_origin_only_resembles_the_env_repository_is_a_product(env: EnvRepo, origin: str) -> None:
    project = create_product(env.root, "items", "my-item", origin_url=origin)
    assert summary(project, env)[:2] == ("product", "product")


@pytest.mark.parametrize("origin", ENV_ORIGIN_SPELLINGS)
def test_env_checkout_accepts_every_spelling_of_the_env_origin(tmp_path: Path, origin: str) -> None:
    env = create_env_repo(tmp_path / "workspace", origin_url=origin)
    assert route_target.resolve_env_root({"XRIFT_POC_ROOT": str(env.root)}) == env.root.resolve()
    worktree = create_owned_worktree(env.root, env.root / ".worktrees", name="feature")
    for path in (env.root, worktree):
        assert summary(path, env) == ("env", "env", "usaturn/xrift-poc")


def test_product_worktrees_inside_and_outside_the_root(env: EnvRepo, tmp_path: Path) -> None:
    project = create_product(env.root, "worlds", "my-world", origin_url=WORLD_ORIGIN)
    inside = create_owned_worktree(project, project / ".worktrees", name="feature")
    outside = create_owned_worktree(project, tmp_path / "elsewhere", name="feature")
    for worktree in (inside, outside):
        result = route_target.route(worktree, env.root)
        assert (result.kind, result.repository, result.git_root) == (
            "product",
            "someone/my-world",
            str(worktree.resolve()),
        )
        assert result.project_root == str(project.resolve())


def test_product_names_with_spaces_and_japanese(env: EnvRepo) -> None:
    project = create_product(
        env.root, "worlds", "空白 ワールド", origin_url="https://github.com/someone/space-world.git"
    )
    assert summary(project, env) == ("product", "product", "someone/space-world")


def test_unrelated_repositories_and_plain_directories_stop(env: EnvRepo, tmp_path: Path) -> None:
    nested = env.root / "tmp" / "clone"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    outside = tmp_path / "other"
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(outside)], check=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert summary(nested, env)[:2] == ("stop", "unknown")
    assert summary(outside, env)[:2] == ("stop", "unknown")
    assert summary(plain, env)[:2] == ("stop", "not_git")


def test_missing_paths_are_routed_by_the_nearest_existing_ancestor(env: EnvRepo, tmp_path: Path) -> None:
    # これから作るファイルのパスでも判定できる。実在する最も近い祖先のディレクトリで判定する。
    plain = create_product(env.root, "worlds", "plain", git=False)
    product = create_product(env.root, "worlds", "my-world", origin_url=WORLD_ORIGIN)
    expected = {
        env.root / "tools" / "new" / "a.py": ("env", "env", "usaturn/xrift-poc"),
        env.root / "worlds" / "new-world" / "src" / "a.tsx": ("stop", "product_base", None),
        plain / "src" / "a.tsx": ("stop", "product_uninitialized", None),
        product / "src" / "a.tsx": ("product", "product", "someone/my-world"),
        tmp_path / "missing" / "a.txt": ("stop", "not_git", None),
    }
    for path, outcome in expected.items():
        assert not path.exists()
        assert summary(path, env) == outcome, path
    assert route_target.route(plain / "src" / "a.tsx", env.root).project_root == str(plain.resolve())


def test_missing_paths_under_missing_work_directories_are_not_env(env: EnvRepo, tmp_path: Path) -> None:
    # items/・worlds/ が無くても、その下のパスを親（env）と判定しない。
    # 親の worktree には items/・worlds/ が無いのが普通（Git で無視しているため checkout されない）。
    (env.root / "items").rmdir()
    worktree = create_owned_worktree(env.root, tmp_path / "elsewhere", name="feature")
    assert not (worktree / "worlds").exists()
    assert summary(env.root / "items", env)[:2] == ("stop", "product_base")
    assert summary(env.root / "items" / "new-item" / "a.tsx", env)[:2] == ("stop", "product_base")
    assert summary(worktree / "worlds", env)[:2] == ("stop", "product_base")
    result = route_target.route(worktree / "worlds" / "new-world" / "src" / "a.tsx", env.root)
    assert (result.kind, result.code) == ("stop", "product_in_env_worktree")
    assert result.project_root == str((worktree / "worlds" / "new-world").resolve())


def test_resolve_env_root_accepts_only_the_env_main_checkout(env: EnvRepo, tmp_path: Path) -> None:
    assert route_target.resolve_env_root({"XRIFT_POC_ROOT": str(env.root)}) == env.root.resolve()
    product = create_product(env.root, "worlds", "w", origin_url=WORLD_ORIGIN)
    worktree = create_owned_worktree(env.root, tmp_path / "wt")
    for wrong in (product, worktree, tmp_path):
        with pytest.raises(route_target.RouteError):
            route_target.resolve_env_root({"XRIFT_POC_ROOT": str(wrong)})
    with pytest.raises(route_target.RouteError):
        route_target.resolve_env_root({"XRIFT_POC_ROOT": "relative/path"})


def _install_scripts(env: EnvRepo) -> Path:
    scripts = env.root / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("route_target.py", "review_target.py", "agent_tmp.py"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    return scripts / "route_target.py"


def run_cli(script: Path, path: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(script), str(path)], env=environ, capture_output=True, text=True)


def test_cli_prints_json_with_exit_codes(env: EnvRepo) -> None:
    script = _install_scripts(env)
    environ = {key: value for key, value in os.environ.items() if key != "XRIFT_POC_ROOT"}
    project = create_product(env.root, "worlds", "w", git=False)
    ok = run_cli(script, env.root, environ)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["kind"] == "env"
    stop = run_cli(script, project, environ)
    assert stop.returncode == 3
    assert json.loads(stop.stdout)["code"] == "product_uninitialized"
    missing = run_cli(script, env.root / "worlds" / "new-world" / "a.tsx", environ)
    assert missing.returncode == 3
    assert json.loads(missing.stdout)["code"] == "product_base"


def test_cli_uses_xrift_poc_root_and_rejects_a_wrong_one(env: EnvRepo, tmp_path: Path) -> None:
    other = create_env_repo(tmp_path / "other", origin_url="https://github.com/someone/fork.git")
    environ = {**os.environ, "XRIFT_POC_ROOT": str(other.root)}
    result = run_cli(SCRIPT, env.root, environ)
    assert (result.returncode, result.stdout) == (1, "")


def test_products_in_env_worktrees_stop_with_their_own_code(env: EnvRepo, tmp_path: Path) -> None:
    # 親の worktree の items/・worlds/ は制作物の置き場所ではない（worktree を消すと中身も消える）。
    # main checkout の Git 未初期化の制作物（その場で編集する）とは別の code で止める。
    inside = create_owned_worktree(env.root, env.root / ".worktrees", name="feature")
    outside = create_owned_worktree(env.root, tmp_path / "elsewhere", name="feature")
    product = create_product(env.root, "worlds", "my-world", origin_url=WORLD_ORIGIN)
    for worktree in (inside, outside):
        project = worktree / "worlds" / "w"
        (project / "src").mkdir(parents=True)
        assert summary(worktree / "worlds", env)[:2] == ("stop", "product_base")
        # まだ無い制作物の名前でも、worktree の worlds/ の配下は同じ code にする。
        assert summary(worktree / "worlds" / "new-world" / "a.tsx", env)[:2] == ("stop", "product_in_env_worktree")
        # worktree の worlds/ の中に独立した Git を作った場合と、制作物の worktree を置いた場合も同じ。
        nested = worktree / "worlds" / "n"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        product_worktree = create_owned_worktree(product, worktree / "items", name="pw")
        for path, project_root in (
            (project, project),
            (project / "src", project),
            (nested, nested),
            (product_worktree, product_worktree),
        ):
            result = route_target.route(path, env.root)
            assert (result.kind, result.code, result.repository) == ("stop", "product_in_env_worktree", None), path
            assert result.project_root == str(project_root.resolve())
