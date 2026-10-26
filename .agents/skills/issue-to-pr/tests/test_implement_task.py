"""Exercise implement_task.py: configuration checks on temporary TOML files and the allowed-file matcher."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "implement_task.py"

AGENTS = """
schema_version = 1

[agents.claude-opus]
runtime = "claude-code"
command = ["fake-claude"]
model = "claude-model"

[agents.codex]
runtime = "codex"
command = ["fake-codex"]
model = "codex-model"

[agents.pi-deepseek]
runtime = "pi"
command = ["fake-pi"]
model = "pi-model"
"""

IMPLEMENT_ROLE = """
[roles.implement]
writes = true
deadline_seconds = 60
result_shape = "json"
"""


def template(tmp_path: Path, assignment: str) -> Path:
    path = tmp_path / "agent-runner.toml"
    path.write_text(AGENTS + IMPLEMENT_ROLE + assignment, encoding="utf-8")
    return path


def check(tmp_path: Path, template_path: Path, *args: str, personal: Path | None = None) -> tuple[int, Any]:
    """helper を実際に起動する。個人設定は一時パスに向け、利用者の設定を読まない。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", "--template", str(template_path), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_RUNNER_CONFIG": str(personal or tmp_path / "no-personal.toml")},
    )
    return result.returncode, json.loads(result.stdout) if result.stdout else result.stderr


SELF = '\n[workflows.issue.assignments.implement]\nagents = ["self"]\n'


def test_check_implement_replaces_primary_and_keeps_fallback(tmp_path: Path) -> None:
    """--implement を設定に渡さないと、利用者が指定した担当を黙って無視し、設定の担当で実装する。"""
    path = template(
        tmp_path, '\n[workflows.issue.assignments.implement]\nagents = ["codex"]\nfallback = ["pi-deepseek"]\n'
    )

    code, payload = check(tmp_path, path, "--implement", "claude-opus")

    assert code == 0, payload
    assert payload["assignment"] == {"agents": ["claude-opus"], "fallback": ["pi-deepseek"]}
    assert payload["self"] is False


def test_check_personal_config_overrides_template(tmp_path: Path) -> None:
    """個人設定を読まないと、個人設定の担当を黙って無視し、雛形の self で実装する。"""
    personal = tmp_path / "personal.toml"
    personal.write_text('[workflows.issue.assignments.implement]\nagents = ["codex"]\n', encoding="utf-8")

    code, payload = check(tmp_path, template(tmp_path, SELF), personal=personal)

    assert code == 0, payload
    assert payload["assignment"]["agents"] == ["codex"]
    assert payload["self"] is False


def test_broken_personal_config_never_falls_back_to_template(tmp_path: Path) -> None:
    """壊れた個人設定リンクを無視すると、指定した担当やafter_runを失ったまま実装が進む。"""
    personal = tmp_path / "個人 設定.toml"
    personal.symlink_to(tmp_path / "missing.toml")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", "--template", str(template(tmp_path, SELF))],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "AGENT_RUNNER_CONFIG": str(personal),
            "XDG_CONFIG_HOME": str(tmp_path),
        },
    )
    assert result.returncode != 0, result.stdout


# --- 担当ファイルの照合 -------------------------------------------------------------------------------------------

SPEC = importlib.util.spec_from_file_location("issue_implement_task", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
mod: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def test_directory_entry_matches_by_path_segment() -> None:
    """担当ファイルの照合が文字列の前方一致に緩むと、tests_other/・a.pyc など担当外の変更を黙って取り込む。"""
    assert mod.allowed("tests/a.py", ["tests/"])
    assert mod.allowed("tests/sub/b.py", ["tests/"])
    assert not mod.allowed("tests_other/x", ["tests/"])
    assert not mod.allowed("tests", ["tests/"])
    assert mod.allowed("a.py", ["a.py"])
    assert not mod.allowed("a.pyc", ["a.py"])
    assert not mod.allowed("src/a.py/x", ["src/a.py"])
