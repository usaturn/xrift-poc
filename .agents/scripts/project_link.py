#!/usr/bin/env python3
"""制作物に、xrift-poc のスキルと Pi の設定へのシンボリックリンクを置く（usaturn/xrift-poc#1・#55）。

Claude Code・pi・Codex は、独立した Git リポジトリの中から起動すると、祖先にある xrift-poc の
``.claude/skills``・``.agents/skills`` を読まない（2026-10-10 に確認）。Git 未初期化の制作物では
読むので、スキルのリンクは独立した Git リポジトリの制作物にだけ置く。Pi 1.3.0 は project の設定
（``.pi/`` の ``APPEND_SYSTEM.md``・``mcp.json``・``settings.json``）を、起動した cwd の ``.pi/`` からだけ
読み、祖先を辿らない（2026-10-21 に確認）。``.pi/`` のリンクは Git 未初期化の制作物にも置く。
Cursor CLI の親スキルの利用は、リンク済みの独立 Git と、その中のリンク済み worktree を対応範囲とする。
未初期化の制作物での制約と版付きの観測は、親の README.md と PORTING.md「Cursor CLI の対応範囲」を参照する。

- ``link [dir]``: 独立した Git リポジトリの制作物では、``<dir>/.agents/skills/<name>`` と
  ``<dir>/.claude/skills/<name>`` に、``<root>/.agents/skills/<name>`` への絶対パスのリンクを置き、置いたリンクを
  ``info/exclude`` の管理ブロックに登録する。どちらの制作物でも、``<dir>/.pi/<file>`` に、ルートに実在する
  ``<root>/.pi/<file>`` への絶対パスのリンクを置く。同じ名前のもの（壊れたリンクを含む）が既にあれば変えない
- ``unlink [dir]``: 管理ブロックに登録され、ルートのスキルを指すリンクと、ルートの ``.pi/`` の同じ名前の
  ファイルを指す ``.pi/`` のリンクだけを消す。途中のシンボリックリンクをたどった先が同じリポジトリの別の
  worktree の中なら、そのスキルのリンクは消さずに残す。どの worktree の中でもない（共有ディレクトリなど）
  リンクを消すことになる場合は、何も変えずに止まる（usaturn/xrift-poc#4）。``.pi`` がシンボリックリンクなら、
  中のリンクは消さずに報告する
- 管理ブロックの行は ``info/exclude`` が制作物のすべての worktree で共有されるため、link・unlink のたびに
  「いずれかの worktree に実在する、ルートのスキルへのリンク」から作り直す。各除外行の直後にディレクトリだけを
  再び含める否定行を置き、別 worktree の同名の利用者ディレクトリを隠さない。空になったときだけブロックを外す
- ``.pi/`` のリンクは、置いた worktree の ``.pi/.gitignore`` で無視する。``.pi/`` の 3 つはファイルで否定行で
  守れず、共有の ``info/exclude`` で無視すると、他の worktree の制作物自身の同名のファイルまで隠れるため。
  ``.pi/.gitignore`` は 1 行目に管理ブロックの開始の印を置き、自分自身と置いたリンクを並べ、link・unlink の
  たびに作り直す（リンクが無くなれば消す）。印の無い、制作物自身の ``.pi/.gitignore`` があれば ``.pi/`` の
  リンクを置かない

``dir`` は ``<root>/items/<name>``・``<root>/worlds/<name>`` の配下にある Git の作業ツリーのルート
（制作物のリポジトリか、その中に作った worktree）か、Git 未初期化の制作物のディレクトリ（``<root>/items/<name>``・
``<root>/worlds/<name>`` そのもの）に限る。

終了コード: 0 成功（何もしなかった場合を含む）、1 前提が壊れている（unlink がどの worktree の中でもない
リンクを消すことになる場合を含む）、2 対象外のパス・引数の誤り。
標準ライブラリだけで書く。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

BLOCK_BEGIN = "# >>> xrift-poc project_link >>>"
BLOCK_END = "# <<< xrift-poc project_link <<<"
LINK_DIRS = (Path(".agents/skills"), Path(".claude/skills"))
PI_DIR = Path(".pi")
PI_FILES = ("APPEND_SYSTEM.md", "mcp.json", "settings.json")
PI_IGNORE = PI_DIR / ".gitignore"
PI_IGNORE_NOTE = "# xrift-link が置いた .pi/ のリンクを無視する。link・unlink のたびに作り直すので、手で編集しない"
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


@dataclass(frozen=True)
class _Target:
    """リンクを置くディレクトリ。``git`` が偽なら Git 未初期化の制作物のディレクトリ。"""

    path: Path
    git: bool


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


def _target(directory: Path, env_root: Path) -> _Target:
    """リンクを置く場所: 制作物の Git の作業ツリーのルートか、Git 未初期化の制作物のディレクトリ。"""
    # route() は無いパスを祖先で判定するので、無いディレクトリを Git 未初期化の制作物と取り違えないよう先に止める。
    if not directory.is_dir():
        raise LinkError(EXIT_USAGE, f"ディレクトリが無い: {directory}")
    result = _route_target.route(directory, env_root)
    target = directory.resolve()
    if result.kind == "stop" and result.code == "product_uninitialized":
        # Pi は起動した cwd の .pi/ だけを読むので、Pi を起動する制作物のディレクトリに置く。
        if result.project_root is None or Path(result.project_root) != target:
            raise LinkError(EXIT_USAGE, f"制作物のディレクトリを指定する: {result.project_root}")
        return _Target(target, git=False)
    if result.kind != "product":
        raise LinkError(EXIT_USAGE, f"制作物の Git リポジトリではない（{result.code}: {result.reason}）")
    if result.project_root is None or not target.is_relative_to(Path(result.project_root)):
        raise LinkError(EXIT_USAGE, f"制作物の配下ではない worktree には対応しない: {target}")
    if Path(result.git_root) != target:
        raise LinkError(EXIT_USAGE, f"Git の作業ツリーのルートを指定する: {result.git_root}")
    return _Target(target, git=True)


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


def _managed_relative(entry: str) -> Path | None:
    """管理ブロックの行が指す、``LINK_DIRS`` の直下のパス。想定していない行なら None。"""
    relative = Path(entry.removeprefix("/"))
    if relative.parent not in LINK_DIRS or relative.name in ("", ".", ".."):
        return None
    return relative


def _links_elsewhere(target: Path, entries: list[str], source: Path) -> dict[str, tuple[Path, Path | None]]:
    """途中のシンボリックリンクをたどると ``target`` 自身の外にある、``source`` 配下のスキルへのリンク。

    管理ブロックの行ごとに、解決したパスと、それを含む同じリポジトリの worktree（最も深いもの。どの
    worktree にも含まれなければ None）を返す。
    """
    worktrees = _worktrees(target)
    found: dict[str, tuple[Path, Path | None]] = {}
    for entry in entries:
        relative = _managed_relative(entry)
        if relative is None:
            continue
        path = target / relative
        if not (path.is_symlink() and Path(os.readlink(path)) == source / relative.name):
            continue
        parent = path.parent.resolve()
        owners = [worktree for worktree in worktrees if parent.is_relative_to(worktree)]
        owner = max(owners, key=lambda worktree: len(worktree.parts), default=None)
        if owner != target:
            found[entry] = (parent / relative.name, owner)
    return found


def _outside_message(found: list[tuple[str, Path]], exclude: Path) -> str:
    paths = " ".join(shlex.quote(str(path)) for path in dict.fromkeys(resolved for _, resolved in found))
    return "\n".join(
        [
            "制作物のどの worktree にも含まれない場所のリンクを消すことになるため、何も変えずに止めた"
            "（途中のシンボリックリンクが外へ解決される）",
            *(f"  {entry} -> {resolved}" for entry, resolved in found),
            "次のどちらかを行ってから、もう一度 unlink を実行する",
            f"  外のリンクも消す（共有している他のプロジェクトからも消える）: rm -- {paths}",
            f"  外のリンクを残す: {exclude} の管理ブロックから、上の行を消す",
        ]
    )


def _worktrees(target: Path) -> list[Path]:
    """``target`` と同じリポジトリのすべての worktree（``info/exclude`` を共有する）。"""
    result = subprocess.run(
        ["git", "-C", str(target), "worktree", "list", "--porcelain", "-z"],
        check=False,
        capture_output=True,
        text=True,
    )
    found = [target]
    if result.returncode != 0:
        return found
    for field_text in result.stdout.split("\0"):
        if field_text.startswith("worktree "):
            path = Path(field_text.removeprefix("worktree "))
            if path.is_dir() and path.resolve() not in found:
                found.append(path.resolve())
    return found


def _live_entries(target: Path, source: Path) -> list[str]:
    """実在する管理リンクを除外し、同名の実ディレクトリは再び含める行の対。"""
    entries: set[str] = set()
    for worktree in _worktrees(target):
        for link_dir in LINK_DIRS:
            if _blocked_component(worktree, link_dir) is not None or not (worktree / link_dir).is_dir():
                continue
            for path in (worktree / link_dir).iterdir():
                if path.is_symlink() and Path(os.readlink(path)) == source / path.name:
                    entries.add(f"/{(link_dir / path.name).as_posix()}")
    # info/exclude は共有される。末尾 / の否定は実ディレクトリだけに一致し、リンクは無視したままにする。
    return [line for entry in sorted(entries) for line in (entry, f"!{entry}/")]


def _is_pi_link(path: Path, root: Path) -> bool:
    """``path`` が、ルートの ``.pi/`` の同じ名前のファイルを指す、置いたリンクか。"""
    return path.is_symlink() and Path(os.readlink(path)) == root / PI_DIR / path.name


def _pi_ignore_state(target: Path) -> Literal["absent", "ours", "theirs"]:
    """``.pi/.gitignore`` が無いか、このスクリプトのもの（1 行目が印）か、制作物のものか。"""
    path = target / PI_IGNORE
    if not path.exists() and not path.is_symlink():
        return "absent"
    if path.is_symlink() or not path.is_file():
        return "theirs"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return "theirs"
    return "ours" if lines[:1] == [BLOCK_BEGIN] else "theirs"


def _sync_pi_ignore(target: Path, root: Path) -> None:
    """``target`` の ``.pi/`` にある置いたリンクと自分自身を無視する ``.pi/.gitignore`` を作り直す。

    ``info/exclude`` は全 worktree で共有され、否定行で守れるのはディレクトリだけなので、``.pi/`` の
    ファイルのリンクをそこで無視すると、他の worktree の制作物自身の同名のファイルまで隠れる。リンクを置いた
    worktree の中だけで無視し、リンクが無くなれば消す。制作物の ``.pi/.gitignore`` には触らない。
    """
    state = _pi_ignore_state(target)
    if state == "theirs":
        return
    path = target / PI_IGNORE
    names = [name for name in PI_FILES if _is_pi_link(target / PI_DIR / name, root)]
    if not names:
        if state == "ours":
            path.unlink()
        return
    text = "\n".join([BLOCK_BEGIN, PI_IGNORE_NOTE, "/.gitignore", *(f"/{name}" for name in names)]) + "\n"
    if state == "absent" or path.read_text(encoding="utf-8") != text:
        _write_atomic(path, text)


def _link_pi(target: Path, root: Path, report: Report) -> None:
    """``target/.pi/`` に、ルートの ``.pi/`` にある設定のファイルへのリンクを置く。"""
    names = [name for name in PI_FILES if (root / PI_DIR / name).is_file()]
    if not names:
        return
    if _blocked_component(target, PI_DIR) is not None:
        report.kept.append(f"{PI_DIR.as_posix()}: シンボリックリンクかディレクトリではないので、中に置かない")
        return
    if _pi_ignore_state(target) == "theirs":
        report.kept.append(f"{PI_IGNORE.as_posix()}: 制作物のファイル。リンクを Git から外せないので置かない")
        return
    for name in names:
        path = target / PI_DIR / name
        relative = (PI_DIR / name).as_posix()
        if path.is_symlink() or path.exists():
            report.kept.append(f"{relative}: 既にある")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(root / PI_DIR / name)
        report.created.append(relative)
    _sync_pi_ignore(target, root)


def _unlink_pi(target: Path, root: Path, report: Report) -> None:
    """``target/.pi/`` の、置いたリンクだけを消す。"""
    directory = target / PI_DIR
    if directory.is_symlink():
        report.kept.append(f"{PI_DIR.as_posix()}: シンボリックリンク。中（制作物の外かもしれない）のリンクは外さない")
        return
    if not directory.is_dir():
        return
    for name in PI_FILES:
        if _is_pi_link(directory / name, root):
            (directory / name).unlink()
            report.removed.append((PI_DIR / name).as_posix())
    _sync_pi_ignore(target, root)


def _hidden_notes(target: Path, entries: list[str], source: Path) -> list[str]:
    """共有の除外で隠れる利用者のファイル・リンクを、worktree の場所付きで報告する。"""
    notes: list[str] = []
    for worktree in _worktrees(target):
        for entry in entries:
            relative = _managed_relative(entry)
            if relative is None:
                continue
            path = worktree / relative
            ours = path.is_symlink() and Path(os.readlink(path)) == source / relative.name
            directory = path.is_dir() and not path.is_symlink()
            if (path.exists() or path.is_symlink()) and not ours and not directory:
                notes.append(f"{path}: 他の worktree のリンクのため除外されており、git status に表示されない")
    return notes


def link(directory: Path, env_root: Path) -> Report:
    report = Report()
    found = _target(directory, env_root)
    if not found.git:
        _link_pi(found.path, env_root.resolve(), report)
        report.note = (
            "Git 未初期化の制作物。スキルはリンクせず、.pi/ のリンクだけが対象。"
            "Claude Code・pi・Codex は祖先のスキルを読む。"
            "Cursor CLI はこの状態での親スキルの利用を対応範囲に含めない"
            "（親の README.md と PORTING.md「Cursor CLI の対応範囲」を参照）。"
            "Git を初期化して使う場合は xrift-link を再実行する。スキルのためだけの Git 初期化は不要"
        )
        return report
    target = found.path
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
    _link_pi(target, env_root.resolve(), report)
    merged = _live_entries(target, source)
    if merged != entries:
        _write_atomic(exclude, _render(before, merged, after))
    report.kept.extend(_hidden_notes(target, merged, source))
    report.exclude_file = str(exclude)
    return report


def unlink(directory: Path, env_root: Path) -> Report:
    report = Report()
    found = _target(directory, env_root)
    if not found.git:
        _unlink_pi(found.path, env_root.resolve(), report)
        return report
    target = found.path
    exclude = _exclude_file(target)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    before, entries, after = _split_block(text, exclude)
    source = env_root.resolve() / ".agents" / "skills"
    elsewhere = _links_elsewhere(target, entries, source)
    outside = [(entry, resolved) for entry, (resolved, owner) in elsewhere.items() if owner is None]
    if outside:
        raise LinkError(EXIT_ERROR, _outside_message(outside, exclude))
    for entry in entries:
        if entry.startswith("!/") and entry.endswith("/") and _managed_relative(entry[1:-1]) is not None:
            # ディレクトリの否定行は削除対象ではない。
            continue
        relative = _managed_relative(entry)
        if relative is None:
            report.kept.append(f"{entry}: 管理ブロックの行として想定していない")
            continue
        if entry in elsewhere:
            owner = elsewhere[entry][1]
            report.kept.append(f"{relative.as_posix()}: 別の worktree（{owner}）のリンク。外すならそこで unlink する")
            continue
        path = target / relative
        if path.is_symlink() and Path(os.readlink(path)) == source / relative.name:
            path.unlink()
            report.removed.append(relative.as_posix())
        elif path.is_symlink() or path.exists():
            report.kept.append(f"{relative.as_posix()}: xrift-poc のスキルへのリンクではない")
    _unlink_pi(target, env_root.resolve(), report)
    merged = _live_entries(target, source)
    if merged != entries:
        _write_atomic(exclude, _render(before, merged, after))
    report.kept.extend(_hidden_notes(target, merged, source))
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
