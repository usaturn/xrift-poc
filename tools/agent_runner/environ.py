"""The environment the runner process itself was started with."""

import os
import re
from pathlib import Path

_literal: dict[str, str] | None = None


def literal_environ() -> dict[str, str]:
    """Return the process environment as it existed at interpreter startup.

    ``os.environ`` may already differ from the executed environment: CPython's
    locale coercion rewrites or injects values (``LC_CTYPE`` becomes
    ``C.UTF-8`` under a C/POSIX locale) before runner code runs. Inherited
    values are reconstructed from ``/proc/self/environ``, matching what
    ``_supervisor.py`` passes to supervised commands; where ``/proc`` is
    unavailable ``os.environ`` is the best remaining source.
    """
    global _literal
    if _literal is None:
        try:
            entries = Path("/proc/self/environ").read_bytes().split(b"\0")
        except OSError:
            _literal = dict(os.environ)
        else:
            _literal = {
                os.fsdecode(name): os.fsdecode(value)
                for entry in entries
                if entry and b"=" in entry
                for name, value in [entry.split(b"=", 1)]
            }
    return dict(_literal)


def secret_values(env: dict[str, str]) -> set[str]:
    """Values of variables whose names mark them as credentials; they are redacted from retained output."""
    return {
        value
        for name, value in env.items()
        if value and re.search(r"SECRET|TOKEN|CREDENTIAL|PASSWORD|API_KEY", name, re.IGNORECASE)
    }
