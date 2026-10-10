#!/usr/bin/env python3
"""制作物の Git リポジトリに、xrift-poc のスキルへのシンボリックリンクを置く（usaturn/xrift-poc#1）。

Claude Code・pi・Codex は、独立した Git リポジトリの中から起動すると、祖先にある xrift-poc の
``.claude/skills``・``.agents/skills`` を読まない（2026-10-10 に確認）。Git 未初期化の制作物では
読むので、この補助は要らない。

- ``link [dir]``: ``<dir>/.agents/skills/<name>`` と ``<dir>/.claude/skills/<name>`` に、
  ``<root>/.agents/skills/<name>`` への絶対パスのリンクを置き、置いたリンクを ``info/exclude`` の
  管理ブロックに登録する。同じ名前のもの（壊れたリンクを含む）が既にあれば変えない
- ``unlink [dir]``: 管理ブロックに登録され、ルートのスキルを指すリンクだけを消し、管理ブロックを外す

``dir`` は ``<root>/items/<name>``・``<root>/worlds/<name>`` の配下にある Git の作業ツリーのルート
（制作物のリポジトリか、その中に作った worktree）に限る。

終了コード: 0 成功（何もしなかった場合を含む）、1 前提が壊れている、2 対象外のパス・引数の誤り。
標準ライブラリだけで書く。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import cast

BLOCK_BEGIN = "# >>> xrift-poc project_link >>>"
BLOCK_END = "# <<< xrift-poc project_link <<<"
LINK_DIRS = (Path(".agents/skills"), Path(".claude/skills"))
EXIT_ERROR = 1
EXIT_USAGE = 2


class LinkError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Report:
    created: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    kept: list[str] = field(default_factory=list[str])
    exclude_file: str | None = None
    note: str | None = None


def _load_route_target() -> ModuleType:
    path = Path(__file__).resolve().parent / "route_target.py"
    spec = importlib.util.spec_from_file_location("route_target", path)
    if spec is None or spec.loader is None:
        raise LinkError(EXIT_ERROR, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_route_target = _load_route_target()


def skill_names(env_root: Path) -> list[str]:
    """``<root>/.agents/skills`` のうち ``SKILL.md`` を持つディレクトリの名前。"""
    skills = env_root / ".agents" / "skills"
    return sorted(path.name for path in skills.iterdir() if path.is_dir() and (path / "SKILL.md").is_file())


def _target(directory: Path, env_root: Path) -> Path | None:
    """リンクを置く Git の作業ツリーのルート。Git 未初期化の制作物なら None。"""
    try:
        result = _route_target.route(directory, env_root)
    except _route_target.RouteError as error:
        raise LinkError(EXIT_USAGE, str(error)) from None
    target = directory.resolve()
    if result.kind == "stop" and result.code == "product_uninitialized":
        return None
    if result.kind != "product":
        raise LinkError(EXIT_USAGE, f"制作物の Git リポジトリではない（{result.code}: {result.reason}）")
    if not target.is_relative_to(env_root.resolve()):
        raise LinkError(EXIT_USAGE, f"xrift-poc の外にある worktree には対応しない: {target}")
    if Path(result.git_root) != target:
        raise LinkError(EXIT_USAGE, f"Git の作業ツリーのルートを指定する: {result.git_root}")
    return target


def _exclude_file(target: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LinkError(EXIT_ERROR, f"cannot find info/exclude: {result.stderr.strip()}")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else (target / path)


def _split_block(text: str, path: Path) -> tuple[list[str], list[str], list[str]]:
    """（前、ブロック内の行、後）。ブロックが無ければ（全行、[]、[]）。"""
    lines = text.splitlines()
    begins = [index for index, line in enumerate(lines) if line == BLOCK_BEGIN]
    ends = [index for index, line in enumerate(lines) if line == BLOCK_END]
    if not begins and not ends:
        return lines, [], []
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise LinkError(EXIT_ERROR, f"管理ブロックの目印が壊れている。手で直してから再実行する: {path}")
    return lines[: begins[0]], lines[begins[0] + 1 : ends[0]], lines[ends[0] + 1 :]


def _render(before: list[str], entries: list[str], after: list[str]) -> str:
    lines = [*before, *([BLOCK_BEGIN, *entries, BLOCK_END] if entries else []), *after]
    return "\n".join(lines) + "\n" if lines else ""


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _blocked_component(target: Path, link_dir: Path) -> Path | None:
    """``link_dir`` の途中にある、ディレクトリでないもの・シンボリックリンク。"""
    current = target
    for part in link_dir.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return current.relative_to(target)
    return None


def link(directory: Path, env_root: Path) -> Report:
    report = Report()
    target = _target(directory, env_root)
    if target is None:
        report.note = "Git 未初期化の制作物は、祖先の xrift-poc のスキルを読めるのでリンクは不要"
        return report
    exclude = _exclude_file(target)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    before, entries, after = _split_block(text, exclude)
    source = env_root.resolve() / ".agents" / "skills"
    for link_dir in LINK_DIRS:
        blocked = _blocked_component(target, link_dir)
        if blocked is not None:
            report.kept.append(f"{link_dir.as_posix()}: {blocked.as_posix()} がディレクトリではない")
            continue
        for name in skill_names(env_root):
            path = target / link_dir / name
            relative = (link_dir / name).as_posix()
            if path.is_symlink() or path.exists():
                report.kept.append(f"{relative}: 既にある")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(source / name, target_is_directory=True)
            report.created.append(relative)
    merged = sorted(set(entries) | {f"/{relative}" for relative in report.created})
    if merged != entries:
        _write_atomic(exclude, _render(before, merged, after))
    report.exclude_file = str(exclude)
    return report


def unlink(directory: Path, env_root: Path) -> Report:
    report = Report()
    target = _target(directory, env_root)
    if target is None:
        report.note = "Git 未初期化の制作物には、このスクリプトはリンクを置かない"
        return report
    exclude = _exclude_file(target)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    before, entries, after = _split_block(text, exclude)
    source = env_root.resolve() / ".agents" / "skills"
    for entry in entries:
        relative = Path(entry.removeprefix("/"))
        if relative.parent not in LINK_DIRS or relative.name in ("", ".", ".."):
            report.kept.append(f"{entry}: 管理ブロックの行として想定していない")
            continue
        path = target / relative
        if path.is_symlink() and Path(os.readlink(path)) == source / relative.name:
            path.unlink()
            report.removed.append(relative.as_posix())
        elif path.is_symlink() or path.exists():
            report.kept.append(f"{relative.as_posix()}: xrift-poc のスキルへのリンクではない")
    if entries:
        _write_atomic(exclude, _render(before, [], after))
    report.exclude_file = str(exclude)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="project_link.py",
        description="Link the xrift-poc skills into a product Git repository under items/ or worlds/.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("link", "unlink"):
        command = commands.add_parser(name)
        command.add_argument("directory", nargs="?", default=".")
    args = parser.parse_args(argv)
    try:
        env_root = cast(Path, _route_target.resolve_env_root())
        action = link if args.command == "link" else unlink
        report = action(Path(args.directory), env_root)
    except _route_target.RouteError as error:
        print(f"project_link: {error}", file=sys.stderr)
        return EXIT_ERROR
    except LinkError as error:
        print(f"project_link: {error}", file=sys.stderr)
        return error.code
    except OSError as error:
        print(f"project_link: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(asdict(report), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
