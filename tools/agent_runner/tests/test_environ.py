"""The literal exec environment helper."""

import os
from pathlib import Path

import pytest

from tools.agent_runner import environ


def test_literal_environ_parses_proc_and_skips_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    def proc_bytes(_self: Path) -> bytes:
        return b"A=1\0NOEQUALS\0B=x=y\0EMPTY=\0RAW=\xff\xfe\0"

    monkeypatch.setattr(environ, "_literal", None)
    monkeypatch.setattr(Path, "read_bytes", proc_bytes)
    assert environ.literal_environ() == {
        "A": "1",
        "B": "x=y",
        "EMPTY": "",
        "RAW": os.fsdecode(b"\xff\xfe"),
    }


def test_literal_environ_falls_back_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environ, "_literal", None)

    def unavailable(_self: Path) -> bytes:
        raise OSError("no proc")

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    monkeypatch.setenv("FALLBACK_MARKER", "runtime-only")
    assert environ.literal_environ()["FALLBACK_MARKER"] == "runtime-only"


def test_literal_environ_caches_and_returns_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environ, "_literal", {"CACHED": "yes"})
    env = environ.literal_environ()
    env["CACHED"] = "mutated"
    assert environ.literal_environ()["CACHED"] == "yes"


def test_literal_environ_matches_proc_self_environ() -> None:
    expected = {
        os.fsdecode(name): os.fsdecode(value)
        for entry in Path("/proc/self/environ").read_bytes().split(b"\0")
        if entry and b"=" in entry
        for name, value in [entry.split(b"=", 1)]
    }
    assert environ.literal_environ() == expected
