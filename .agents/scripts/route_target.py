#!/usr/bin/env python3
"""作業するパスの Git・Issue・PR の操作先を判定する（usaturn/xrift-poc#1）。

xrift-poc は開発環境だけを管理し、制作物は ``items/<name>/``・``worlds/<name>/`` の
それぞれの Git リポジトリで管理する。Git 未初期化の制作物では ``git rev-parse --show-toplevel``
が祖先の xrift-poc を返すので、その成功だけで制作物の Git と判定しない。

使い方: ``route_target.py [path]``（既定は cwd）。判定結果を JSON で出力する。

- ``kind``: ``env``（開発環境）・``product``（制作物）・``stop``（操作しない）
- ``code``: 判定の理由の識別子
- ``repository``: GitHub の ``owner/repo``。GitHub の操作をしてはいけないときは null
- ``git_root``・``project_root``・``reason``

終了コード: 0 ``env``・``product``、3 ``stop``、1 環境のルートを決められない・パスが無い、2 引数の誤り。

環境のルートは、環境変数 ``XRIFT_POC_ROOT``（Dev Container が設定する）か、無ければこのスクリプトの
置き場所の main worktree とする。どちらの場合も、main worktree であることと、origin が
``usaturn/xrift-poc`` であることを確かめる。

標準ライブラリだけで書く（``uv run --no-project python`` で実行するため）。
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

ENV_REPOSITORY = "usaturn/xrift-poc"
PRODUCT_KINDS = ("items", "worlds")
EXIT_ERROR = 1
EXIT_STOP = 3

Kind = Literal["env", "product", "stop"]


class RouteError(Exception):
    """判定の前提（環境のルート・対象のパス）が壊れているときに送出する。"""


@dataclass(frozen=True, slots=True)
class Route:
    kind: Kind
    code: str
    repository: str | None
    git_root: str | None
    project_root: str | None
    reason: str


def _load_review_target() -> ModuleType:
    path = Path(__file__).resolve().parent / "review_target.py"
    spec = importlib.util.spec_from_file_location("review_target", path)
    if spec is None or spec.loader is None:
        raise RouteError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_review_target = _load_review_target()


def _git(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _toplevel(path: Path) -> Path | None:
    out = _git(path, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if out else None


def _common_dir(path: Path) -> Path | None:
    out = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).resolve() if out else None


def origin_repository(git_root: Path) -> str | None:
    """origin の設定値を ``owner/repo`` に直す。未設定・GitHub 以外は None。"""
    return cast(str | None, _review_target.origin_repository(git_root))


def _is_env_repository(repository: str | None) -> bool:
    return repository is not None and repository.lower() == ENV_REPOSITORY


def resolve_env_root(environ: Mapping[str, str] | None = None, anchor: Path | None = None) -> Path:
    """環境のルート（xrift-poc の main worktree）を決め、origin を確かめて返す。"""
    values = os.environ if environ is None else environ
    configured = values.get("XRIFT_POC_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise RouteError(f"XRIFT_POC_ROOT must be an absolute path: {configured}")
    else:
        start = (anchor or Path(__file__)).resolve().parent
        common = _common_dir(start)
        if common is None or common.name != ".git":
            raise RouteError(f"cannot find the main worktree from {start}")
        root = common.parent
    root = root.resolve()
    if _common_dir(root) != root / ".git" or _toplevel(root) != root:
        raise RouteError(f"not the main worktree of a Git repository: {root}")
    if not _is_env_repository(origin_repository(root)):
        raise RouteError(f"origin of {root} is not {ENV_REPOSITORY}")
    return root


def _stop(code: str, reason: str, *, git_root: Path | None = None, project_root: Path | None = None) -> Route:
    return Route(
        "stop",
        code,
        None,
        str(git_root) if git_root is not None else None,
        str(project_root) if project_root is not None else None,
        reason,
    )


def _product_bases(env_root: Path) -> list[Path]:
    return [(env_root / kind).resolve() for kind in PRODUCT_KINDS]


def _product_project(path: Path, env_root: Path) -> Path | None:
    """``path`` を含む制作物のディレクトリ（``<root>/<kind>/<name>`` の実体）。"""
    for base in _product_bases(env_root):
        if path != base and path.is_relative_to(base):
            return base / path.relative_to(base).parts[0]
    return None


def _product_route(git_root: Path, project_root: Path) -> Route:
    repository = origin_repository(git_root)
    if _is_env_repository(repository):
        return _stop(
            "product_origin_is_env",
            f"制作物の origin が {ENV_REPOSITORY} を指している。制作物自身のリポジトリを origin に設定する",
            git_root=git_root,
            project_root=project_root,
        )
    if repository is None:
        return Route(
            "product",
            "product_without_github",
            None,
            str(git_root),
            str(project_root),
            "制作物の Git だが、origin が無いか GitHub ではない。GitHub の操作（Issue・PR）はしない",
        )
    return Route("product", "product", repository, str(git_root), str(project_root), "制作物のリポジトリ")


def route(path: Path, env_root: Path) -> Route:
    """``path`` の操作先を判定する。``env_root`` は ``resolve_env_root`` が返した環境のルート。"""
    if not path.exists():
        raise RouteError(f"path does not exist: {path}")
    target = path.resolve()
    if not target.is_dir():
        target = target.parent
    env_root = env_root.resolve()
    if target in _product_bases(env_root):
        return _stop(
            "product_base", "items/ か worlds/ そのものは制作物ではない。items/<name> か worlds/<name> を指定する"
        )
    git_root = _toplevel(target)
    project = _product_project(target, env_root)
    if project is not None:
        if git_root is None or not git_root.is_relative_to(project):
            return _stop(
                "product_uninitialized",
                "制作物の Git が初期化されていない。祖先の xrift-poc の Git を制作物の Git と取り違えない",
                git_root=git_root,
                project_root=project,
            )
        return _product_route(git_root, project)
    common = _common_dir(target)
    if git_root is None or common is None:
        return _stop("not_git", "Git の作業ツリーではない")
    if common == env_root / ".git":
        if not _is_env_repository(origin_repository(git_root)):
            return _stop("env_origin_mismatch", f"origin が {ENV_REPOSITORY} ではない", git_root=git_root)
        return Route("env", "env", ENV_REPOSITORY, str(git_root), None, "開発環境のリポジトリ")
    owner = _product_project(common, env_root)
    if owner is not None:
        return _product_route(git_root, owner)
    return _stop("unknown", "xrift-poc とも制作物とも判定できない", git_root=git_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="route_target.py",
        description="Decide whether a path belongs to the xrift-poc environment, a product repository, or neither.",
    )
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args(argv)
    try:
        env_root = resolve_env_root()
        result = route(Path(args.path), env_root)
    except RouteError as error:
        print(f"route_target: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(asdict(result), ensure_ascii=False))
    return EXIT_STOP if result.kind == "stop" else 0


if __name__ == "__main__":
    raise SystemExit(main())
