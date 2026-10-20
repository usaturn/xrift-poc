"""Observed DSH routes from this attempt's standard headless v4 session log.

Request headers describe the resolved call; assistant sources prove that route
produced a response. Neither the launch patch nor the parent's PI_* is evidence.
"""

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from tools.agent_runner.models import Identity


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate DSH session key")
        result[key] = value
    return result


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("invalid DSH session object")
    return cast(dict[str, Any], value)


def _constant(value: str) -> Any:
    raise ValueError("nonfinite DSH session value")


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("DSH identity field is unavailable")
    return value


def read_session_identity(dsh_home: Path, work_dir: Path, session_id: str, *, live: bool = False) -> Identity:
    """Read a bound regular log. Only a live writer may have an unfinished tail."""
    try:
        return _read_session(dsh_home, work_dir, session_id, live=live)
    except (KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("invalid DSH identity record") from error


def _open_session(dsh_home: Path, session_id: str) -> TextIO:
    """Open the attempt's one v4 log through real directories, never following a link."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", session_id) is None:
        raise ValueError("invalid DSH session id")
    root = dsh_home.absolute()
    # Standard headless uses one unseeded session. Never select by mtime, model,
    # or a shared ~/.dsh. The session id comes from bash or stdout respectively.
    paths = list((root / "sessions").glob(f"*/{session_id}/session.v4.jsonl"))
    if not paths:
        raise FileNotFoundError("this DSH session has no v4 log")
    if len(paths) != 1:
        raise ValueError("ambiguous DSH session log")
    path = paths[0]
    current = root
    for part in path.relative_to(root).parts[:-1]:
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("unsafe DSH session directory")
        current /= part
    if not stat.S_ISDIR(current.lstat().st_mode):
        raise ValueError("unsafe DSH session directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, encoding="utf-8")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError("DSH session log is not a regular file")
    return stream


def _bound(header: dict[str, Any], work_dir: Path, session_id: str) -> bool:
    return (
        header.get("type") == "session"
        and type(header.get("version")) is int
        and header["version"] == 4
        and header.get("id") == session_id
        and header.get("cwd") == str(work_dir.resolve(strict=True))
        and header.get("isSeeded") is False
    )


def read_session_tail(dsh_home: Path, work_dir: Path, session_id: str) -> tuple[str, str | None] | None:
    """The type and UTC time of a stopped attempt's last complete record (#96), or None.

    Only the record type and its millisecond ``time`` are read; an unfinished
    tail or a broken line ends the scan at the last complete record.
    """
    try:
        with _open_session(dsh_home, session_id) as stream:
            header: dict[str, Any] | None = None
            last: tuple[str, str | None] | None = None
            for line in stream:
                if not line.endswith("\n"):
                    break
                try:
                    value = _mapping(json.loads(line, object_pairs_hook=_object, parse_constant=_constant))
                except ValueError, TypeError, RecursionError:
                    break
                if header is None:
                    header = value
                    if not _bound(header, work_dir, session_id):
                        return None
                    continue
                kind, time = value.get("type"), value.get("time")
                if not isinstance(kind, str):
                    break
                at = None
                # Records carry Date.now() milliseconds; bool is an int subclass, so compare types.
                if (type(time) is int or type(time) is float) and time >= 0:
                    moment = datetime.fromtimestamp(time / 1000, UTC)
                    at = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                last = (kind, at)
            return last
    except OSError, ValueError, OverflowError:
        return None


def _read_session(dsh_home: Path, work_dir: Path, session_id: str, *, live: bool) -> Identity:
    with _open_session(dsh_home, session_id) as stream:
        header: dict[str, Any] | None = None
        route: dict[str, Any] | None = None
        observed: Identity | None = None
        sequence = 0
        for line in stream:
            if not line.endswith("\n"):
                if not live:
                    raise ValueError("unfinished stopped DSH session record")
                break
            value = _mapping(json.loads(line, object_pairs_hook=_object, parse_constant=_constant))
            if header is None:
                header = value
                if not _bound(header, work_dir, session_id):
                    raise ValueError("DSH session is not bound to this invocation")
                continue
            if type(value.get("seq")) is not int or value["seq"] != sequence:
                raise ValueError("noncontiguous DSH session records")
            sequence += 1
            kind = value.get("type")
            if kind == "request/header":
                route = _mapping(value["data"]["header"]["config"])
            elif kind == "assistant/message":
                source = _mapping(value["data"]["message"]["source"])
                if value["data"].get("interrupted") or source.get("kind") != "model":
                    continue
                if route is None or (source.get("provider"), source.get("model")) != (
                    route.get("provider"),
                    route.get("model"),
                ):
                    raise ValueError("DSH assistant and resolved request route differ")
                observed = Identity(
                    provider=_text(source.get("provider")),
                    model=_text(source.get("model")),
                    effort=_text(route["reasoningEffort"]) if "reasoningEffort" in route else None,
                    session_id=session_id,
                )
        if observed is None:
            raise ValueError("DSH session has no observed assistant route")
        if route is None or (route.get("provider"), route.get("model"), route.get("reasoningEffort")) != (
            observed.provider,
            observed.model,
            observed.effort,
        ):
            raise ValueError("DSH route changed without an observed response")
        return observed
