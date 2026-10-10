from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = REPO_ROOT / ".agents/skills/agent-handoff"
SKILL = SKILL_DIR / "SKILL.md"
SCHEMA = SKILL_DIR / "references/handoff-schema.md"
EVALS = SKILL_DIR / "evals/evals.json"

#
# Paths a reader is told to open, and which the repository is expected to carry.
# Runtime locations are deliberately absent: `tmp/`, `docs_draft/` and
# `.claude/worktrees/` are gitignored, so a fresh checkout does not have them
# and their absence says nothing about whether the skill is broken.
#
REPO_PREFIXES = (".agents/", ".claude/skills/", "scripts/", "src/", "tests/")
SKILL_RELATIVE_PREFIXES = ("references/", "evals/", "agents/", "tests/")
CHECKED_PREFIXES = REPO_PREFIXES + SKILL_RELATIVE_PREFIXES


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def referenced_paths(content: str) -> list[str]:
    """Concrete repository paths a skill points its reader at.

    Globs and placeholders are excluded: they name a shape, not a file.
    """
    #
    # Split each backticked chunk on whitespace so that command lines are
    # covered too, not just bare paths. The reference that made the previous
    # version unusable sat inside a `uv run pytest <path>` command.
    #
    words = (word for chunk in re.findall(r"`([^`\n]+)`", content) for word in chunk.split())
    return [word for word in words if word.startswith(CHECKED_PREFIXES) and not any(char in word for char in "*<>$")]


def resolve(token: str) -> list[Path]:
    """Where a referenced path may live.

    A bare `tests/...` is ambiguous: it reads as skill-relative, which is how
    the previous version named a module that did not exist. Accept either
    reading so the check stays about existence, not about spelling.
    """
    return [REPO_ROOT / token, SKILL_DIR / token]


def test_every_referenced_path_exists() -> None:
    """A skill that points at a file which was never in this repository is broken.

    This guards the failure that made the previous version unusable: the
    Validation section named a test module that did not exist.
    """
    missing: list[str] = []
    for source in (SKILL, SCHEMA):
        for token in referenced_paths(read(source)):
            if not any(path.exists() for path in resolve(token)):
                missing.append(f"{source.name}: {token}")
    assert not missing, f"referenced paths do not exist: {missing}"


def test_evals_json_uses_expectations_not_assertions() -> None:
    payload = json.loads(read(EVALS))
    evals = payload["evals"]
    assert evals, "evals.json has no evals"
    for item in evals:
        eval_id = item.get("id")
        assert "expectations" in item, f"eval {eval_id} is missing expectations"
        assert "assertions" not in item, f"eval {eval_id} still uses assertions"
