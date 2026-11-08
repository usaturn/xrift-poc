"""秘密検査。内容を番号名のファイルに置き、scanner（既定は gitleaks の `dir`）を 1 回起動する。"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import cast

DEFAULT_SCANNER: tuple[str, ...] = (
    "gitleaks",
    "dir",
    "{source}",
    "--no-banner",
    "--redact",
    "--exit-code",
    "99",
    "--report-format",
    "json",
    "--report-path",
    "{report}",
    "--ignore-gitleaks-allow",
    "--gitleaks-ignore-path",
    "{ignore}",
)
_LEAKS_FOUND = 99
_REMOVED_ENV = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")
_FILE_MODE = 0o600


@dataclass(frozen=True)
class ScanResult:
    """`flagged` は秘密が見つかった内容のインデックス。`failed` があれば全件を検査できなかった扱い。"""

    flagged: frozenset[int]
    failed: str | None


def _name(index: int) -> str:
    return f"{index:04d}"


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _flagged(report: Path, names: dict[str, int], returncode: int) -> ScanResult:
    try:
        items: object = json.loads(report.read_bytes())
    except OSError, ValueError:
        return ScanResult(frozenset(), "report_unreadable")
    if not isinstance(items, list):
        return ScanResult(frozenset(), "report_not_list")
    entries = cast(list[object], items)
    if returncode == 0:
        return ScanResult(frozenset(), None) if not entries else ScanResult(frozenset(), "report_mismatch")
    flagged: set[int] = set()
    for entry in entries:
        file = cast(dict[str, object], entry).get("File") if isinstance(entry, dict) else None
        index = names.get(PurePath(file).name) if isinstance(file, str) else None
        if index is None:
            return ScanResult(frozenset(), "report_mismatch")
        flagged.add(index)
    if not flagged:
        return ScanResult(frozenset(), "report_mismatch")
    return ScanResult(frozenset(flagged), None)


def scan(contents: Sequence[bytes], *, workdir: Path, argv: Sequence[str] | None, timeout: float) -> ScanResult:
    """内容を検査する。例外は出さず、起動・終了コード・レポートの異常は `failed` に理由を入れて返す。"""
    if not contents:
        return ScanResult(frozenset(), None)
    template = DEFAULT_SCANNER if argv is None else tuple(argv)
    try:
        work = Path(tempfile.mkdtemp(prefix="scan-", dir=workdir))
    except OSError as exc:
        return ScanResult(frozenset(), f"workdir: {type(exc).__name__}")
    try:
        source = work / "source"
        source.mkdir(mode=0o700)
        names: dict[str, int] = {}
        for index, data in enumerate(contents):
            names[_name(index)] = index
            _write(source / _name(index), data)
        report = work / "report.json"
        ignore = work / "gitleaksignore"
        _write(ignore, b"")
        values = {"{source}": str(source), "{report}": str(report), "{ignore}": str(ignore)}
        command = [_substitute(arg, values) for arg in template]
        env = {key: value for key, value in os.environ.items() if key not in _REMOVED_ENV}
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ScanResult(frozenset(), "timeout")
        except OSError as exc:
            return ScanResult(frozenset(), f"start: {type(exc).__name__}")
        if result.returncode not in (0, _LEAKS_FOUND):
            return ScanResult(frozenset(), f"exit {result.returncode}")
        return _flagged(report, names, result.returncode)
    except OSError as exc:
        return ScanResult(frozenset(), f"workdir: {type(exc).__name__}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _substitute(arg: str, values: dict[str, str]) -> str:
    for placeholder, value in values.items():
        arg = arg.replace(placeholder, value)
    return arg
