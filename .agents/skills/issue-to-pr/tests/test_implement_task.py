"""Exercise implement_task.py: configuration checks on temporary TOML files and the request/result models."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tools.test_support.texts import FINDING_CONTENT

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "implement_task.py"
REAL_TEMPLATE = Path(__file__).resolve().parents[4] / ".devcontainer" / "agent-runner.toml"

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


def template(tmp_path: Path, assignment: str, *, role: str = IMPLEMENT_ROLE) -> Path:
    path = tmp_path / "agent-runner.toml"
    path.write_text(AGENTS + role + assignment, encoding="utf-8")
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


def test_check_accepts_template_default_self(tmp_path: Path) -> None:
    code, payload = check(tmp_path, template(tmp_path, SELF))

    assert code == 0, payload
    assert payload["status"] == "ok"
    assert payload["assignment"] == {"agents": ["self"], "fallback": []}
    assert payload["self"] is True


def test_check_implement_replaces_primary_and_keeps_fallback(tmp_path: Path) -> None:
    path = template(
        tmp_path, '\n[workflows.issue.assignments.implement]\nagents = ["codex"]\nfallback = ["pi-deepseek"]\n'
    )

    code, payload = check(tmp_path, path, "--implement", "claude-opus")
    assert code == 0, payload
    assert payload["assignment"] == {"agents": ["claude-opus"], "fallback": ["pi-deepseek"]}
    assert payload["self"] is False

    code, payload = check(tmp_path, path, "--implement", "self")
    assert code == 0, payload
    assert payload["assignment"]["agents"] == ["self"]
    assert payload["self"] is True


def test_check_personal_config_overrides_template(tmp_path: Path) -> None:
    personal = tmp_path / "personal.toml"
    personal.write_text('[workflows.issue.assignments.implement]\nagents = ["codex"]\n', encoding="utf-8")

    code, payload = check(tmp_path, template(tmp_path, SELF), personal=personal)

    assert code == 0, payload
    assert payload["assignment"]["agents"] == ["codex"]
    assert payload["self"] is False


@pytest.mark.parametrize(
    ("assignment", "role", "personal", "args"),
    [
        pytest.param(SELF, IMPLEMENT_ROLE, None, ["--implement", "nobody"], id="unknown-agent"),
        pytest.param(
            SELF,
            IMPLEMENT_ROLE,
            '[workflows.issue.assignments.implement]\nagents = ["codex", "claude-opus"]\n',
            [],
            id="two-primaries",
        ),
        pytest.param(SELF, IMPLEMENT_ROLE, "[roles.implement]\nwrites = false\n", [], id="writes-false"),
        pytest.param(SELF, IMPLEMENT_ROLE, '[roles.implement]\nresult_shape = "text"\n', [], id="text-result"),
        pytest.param(
            '\n[workflows.fix.assignments.implement]\nagents = ["codex"]\n', IMPLEMENT_ROLE, None, [], id="no-issue"
        ),
        pytest.param(SELF, "", None, [], id="no-implement-role"),
    ],
)
def test_check_rejects_invalid_assignment(
    tmp_path: Path, assignment: str, role: str, personal: str | None, args: list[str]
) -> None:
    personal_path = None
    if personal is not None:
        personal_path = tmp_path / "personal.toml"
        personal_path.write_text(personal, encoding="utf-8")

    code, payload = check(tmp_path, template(tmp_path, assignment, role=role), *args, personal=personal_path)

    assert code == 2
    assert payload["status"] == "error"
    assert payload["reason"]


def test_check_reads_the_real_template(tmp_path: Path) -> None:
    code, payload = check(tmp_path, REAL_TEMPLATE)

    assert code == 0, payload
    assert payload["self"] is True


# --- 依頼・結果の JSON の形 -----------------------------------------------------------------------------------------

SPEC = importlib.util.spec_from_file_location("issue_implement_task", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
mod: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

SHA = "0123456789abcdef0123456789abcdef01234567"


def result(**overrides: object) -> dict[str, object]:
    return {
        "status": "DONE",
        "commit": SHA,
        "changed_files": ["src/a.py"],
        "commands": [{"argv": ["uv", "run", "pytest"], "exit_code": 0}],
        "concerns": [],
        "incomplete": [],
        "retained_paths": [],
        **overrides,
    }


def request(**overrides: object) -> dict[str, object]:
    return {
        "repository": "/abs/feature",
        "revision": SHA,
        "task": "Task 2: 取り込みの前の確認",
        "allowed_files": ["src/a.py", "tests/"],
        "commit_message": "feat: 実装する\n\nCo-Authored-By: Fixture <fixture@example.invalid>",
        "prompt": FINDING_CONTENT,
        **overrides,
    }


@pytest.mark.parametrize("status", ["DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT"])
@pytest.mark.parametrize("commit", [SHA, "a" * 64, None])
def test_task_result_accepts_each_status_and_null_commit(status: str, commit: str | None) -> None:
    parsed = mod.TaskResult.model_validate(result(status=status, commit=commit))

    assert parsed.status == status
    assert parsed.commit == commit


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"commit": SHA[:12]},
        {"commit": SHA.upper()},
        {"commit": "HEAD"},
        {"status": "done"},
        {"commands": [{"argv": list[str](), "exit_code": 0}]},
        {"commands": [{"argv": ["pytest"], "exit_code": "0"}]},
        {"commands": [{"argv": ["pytest"], "exit_code": True}]},
        {"changed_files": "src/a.py"},
    ],
)
def test_task_result_rejects_unknown_key_bad_sha_and_empty_argv(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        mod.TaskResult.model_validate(result(**overrides))


def test_task_result_requires_every_key() -> None:
    payload = result()
    del payload["retained_paths"]

    with pytest.raises(ValidationError):
        mod.TaskResult.model_validate(payload)


def test_task_request_accepts_multiline_prompt_and_non_ascii_paths() -> None:
    parsed = mod.TaskRequest.model_validate(request(allowed_files=["文書/説明 ファイル.md", "tests/"]))

    assert parsed.prompt == FINDING_CONTENT
    assert parsed.allowed_files == ["文書/説明 ファイル.md", "tests/"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_files": []},
        {"allowed_files": ["/abs/a.py"]},
        {"allowed_files": ["../a.py"]},
        {"allowed_files": ["src/../a.py"]},
        {"allowed_files": ["./a.py"]},
        {"allowed_files": ["src//a.py"]},
        {"allowed_files": ["src/a.py\nother.py"]},
        {"allowed_files": [" src/a.py"]},
        {"allowed_files": ["/"]},
        {"repository": "relative/feature"},
        {"revision": "HEAD"},
        {"task": "Task 2\n二行目"},
        {"task": ""},
        {"prompt": "  \n"},
        {"commit_message": ""},
        {"extra": "x"},
    ],
)
def test_task_request_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        mod.TaskRequest.model_validate(request(**overrides))


def test_directory_entry_matches_by_path_segment() -> None:
    assert mod.allowed("tests/a.py", ["tests/"])
    assert mod.allowed("tests/sub/b.py", ["tests/"])
    assert not mod.allowed("tests_other/x", ["tests/"])
    assert not mod.allowed("tests", ["tests/"])
    assert mod.allowed("a.py", ["a.py"])
    assert not mod.allowed("a.pyc", ["a.py"])
    assert not mod.allowed("src/a.py/x", ["src/a.py"])


def test_exit_code_for_verdicts() -> None:
    assert [mod.exit_code_for(verdict) for verdict in ("importable", "no_changes")] == [0, 0]
    assert [mod.exit_code_for(verdict) for verdict in ("retry", "rejected", "failed")] == [1, 1, 1]
