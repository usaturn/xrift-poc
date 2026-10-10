"""Real supervisor launch paths and optional startup fault injection."""

import asyncio
import os
from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture(params=["posix_spawn", "popen"])
def supervisor_bootstrap(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    if request.param == "posix_spawn" and not hasattr(os, "POSIX_SPAWN_CLOSEFROM"):
        pytest.skip("This interpreter does not provide POSIX_SPAWN_CLOSEFROM")
    real_launch = asyncio.create_subprocess_exec
    backend = "os.__dict__.pop('POSIX_SPAWN_CLOSEFROM', None); " if request.param == "popen" else ""

    def configure(startup: str = "") -> None:
        async def launch(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            code = "import os,runpy; " + backend + startup + f"\nrunpy.run_path({args[3]!r}, run_name='__main__')"
            return await real_launch(args[0], "-I", "-S", "-c", code, *args[4:], **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)

    configure()
    return configure
