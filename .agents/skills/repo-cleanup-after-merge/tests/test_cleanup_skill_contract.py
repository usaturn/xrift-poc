from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVALS = ROOT / ".agents/skills/repo-cleanup-after-merge/evals/evals.json"


def test_evals_have_unique_cases_with_expectations() -> None:
    evals = json.loads(EVALS.read_text(encoding="utf-8"))["evals"]
    assert evals
    ids = [case["id"] for case in evals]
    names = [case["name"] for case in evals]
    assert len(ids) == len(set(ids))
    assert len(names) == len(set(names))
    for case in evals:
        assert case["expectations"], case["name"]
