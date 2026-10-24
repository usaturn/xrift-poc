"""usaturn/xrift-poc#1: route_target.py が、作業するパスの Git・Issue・PR の操作先を判定する仕様。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from dataclasses import asdict
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


def test_work_directories_themselves_stop(env: EnvRepo) -> None:
    """`items/`・`worlds/` そのものと、その直下のまだ無い名前を止めないと、
    祖先の親の Git が見つかって env と黙って判定し、制作物の作業を親に起票・commit する。"""
    assert summary(env.root / "worlds", env)[:2] == ("stop", "product_base")
    assert summary(env.root / "worlds" / "new-world" / "src" / "a.tsx", env)[:2] == ("stop", "product_base")


def test_uninitialized_product_stops_instead_of_using_the_ancestor_git(env: EnvRepo) -> None:
    """Git 未初期化の制作物で祖先の親の Git を使うと、env と黙って判定し、制作物を親に commit・起票する。"""
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


def test_product_with_a_github_origin(env: EnvRepo) -> None:
    """制作物の repository・git_root・project_root を誤ると、別のリポジトリへ黙って起票・push する。"""
    project = create_product(env.root, "worlds", "my-world", origin_url=WORLD_ORIGIN)
    # 制作物の public/ は XRift のアセットディレクトリで、特別に扱わない。
    (project / "public").mkdir()
    for path in (project, project / "public"):
        result = route_target.route(path, env.root)
        assert (result.kind, result.code, result.repository) == ("product", "product", "someone/my-world")
        assert (result.git_root, result.project_root) == (str(project.resolve()), str(project.resolve()))


def test_product_origin_credentials_are_not_reported(env: EnvRepo) -> None:
    """判定の JSON に origin の URL が載ると、認証情報がログ・PR 本文に黙って出る。"""
    project = create_product(
        env.root, "worlds", "my-world", origin_url="https://user:s3cret@github.com/someone/my-world"
    )
    result = route_target.route(project, env.root)
    assert result.repository == "someone/my-world"
    assert "s3cret" not in json.dumps(asdict(result))


# 別ホストの URL（github.com をユーザ情報に書いただけのもの）を GitHub と読まない。
def test_product_origin_that_is_not_a_github_url_has_no_repository(env: EnvRepo) -> None:
    """別ホストの URL を GitHub と読むと、scan_before_push が制作物の push を通し、別ホストへ黙って送る。"""
    project = create_product(env.root, "items", "my-item", origin_url="https://github.com@evil.test/someone/my-item")
    result = route_target.route(project, env.root)
    assert (result.kind, result.repository) == ("product", None)


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:USATURN/XRIFT-POC.git",  # 大文字小文字と SSH の `:`・`.git` 付き
        "http://github.com/usaturn/xrift-poc/",  # 末尾の `/`・`.git` 無し・http
    ],
)
def test_product_whose_origin_is_the_env_repository_stops(env: EnvRepo, origin: str) -> None:
    """親を指す origin の書き方を見逃すと、
    制作物を親（公開）の repository として扱い、制作物の commit を親へ黙って push・起票する。"""
    project = create_product(env.root, "items", "my-item", origin_url=origin)
    assert summary(project, env) == ("stop", "product_origin_is_env", None)


def test_missing_paths_under_missing_work_directories_are_not_env(env: EnvRepo, tmp_path: Path) -> None:
    """`items/`・`worlds/` の無い checkout（feature worktree など）で無いパスを祖先で判定すると、
    env と黙って判定し、制作物を worktree の中に作る（片付けで消える）。"""
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


def test_products_in_env_worktrees_stop_with_their_own_code(env: EnvRepo, tmp_path: Path) -> None:
    """親の feature worktree の `items/`・`worlds/` の配下を制作物と判定すると、
    そこで編集・commit し、worktree の片付けで黙って消える。"""
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
