"""Exercise the read-only Issue resolver against real Git checkouts and a gh boundary double."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolve_issue.py"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "https://github.com/usaturn/maatlog-poc.git"],
        check=True,
    )
    return root


@pytest.fixture
def gh_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:3] != ['issue', 'view', '42'] or '--repo' not in args:\n"
        "    sys.exit(64)\n"
        "repo = args[args.index('--repo') + 1]\n"
        "if repo != os.environ['EXPECTED_REPO']:\n"
        "    sys.exit(65)\n"
        "if os.environ.get('GH_FAIL'):\n"
        "    print('HTTP 404: not found', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(os.environ['GH_RESPONSE'])\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    set_response(monkeypatch, "usaturn/maatlog-poc")


def set_response(
    monkeypatch: pytest.MonkeyPatch,
    repo: str,
    *,
    state: str = "OPEN",
    kind: str = "issues",
    number: int = 42,
) -> None:
    monkeypatch.setenv("EXPECTED_REPO", repo)
    monkeypatch.setenv(
        "GH_RESPONSE",
        json.dumps(
            {
                "number": number,
                "title": "Fix a regression",
                "body": "Keep unrelated work intact.",
                "state": state,
                "url": f"https://github.com/{repo}/{kind}/{number}",
            }
        ),
    )


def resolve(checkout: Path, argument: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), argument], cwd=checkout, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("argument", "repo", "remote"),
    [
        ("https://github.com/usaturn/maatlog-poc/issues/42", "usaturn/maatlog-poc", None),
        ("https://github.com/usaturn/maatlog/issues/42", "usaturn/maatlog", None),
        ("#42", "usaturn/maatlog-poc", None),
        ("#42", "usaturn/maatlog", "https://github.com/usaturn/maatlog.git"),
        ("#42", "usaturn/maatlog", "git@github.com:usaturn/maatlog.git"),
    ],
)
def test_resolves_issue_in_selected_repository_without_changing_git(
    checkout: Path, gh_stub: None, monkeypatch: pytest.MonkeyPatch, argument: str, repo: str, remote: str | None
) -> None:
    if remote:
        subprocess.run(["git", "remote", "set-url", "origin", remote], cwd=checkout, check=True)
    set_response(monkeypatch, repo)
    before = subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout)
    result = resolve(checkout, argument)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["repo"] == repo
    assert payload["number"] == 42
    assert payload["url"] == f"https://github.com/{repo}/issues/42"
    assert payload["body"] == "Keep unrelated work intact."
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout) == before
    assert subprocess.check_output(["git", "for-each-ref"], cwd=checkout) == b""


@pytest.mark.parametrize(
    "argument",
    [
        "https://github.com/usaturn/maatlog-poc/pull/42",
        "https://github.com/other/repo/issues/42",
        "https://github.com/usaturn/maatlog/issues/42?foo=bar",
        "https://github.com/usaturn/maatlog/issues/42#comment",
        "https://github.com.evil.test/usaturn/maatlog/issues/42",
        "#0",
        "#-42",
        "42",
        "#42; touch injected",
    ],
)
def test_rejects_invalid_input_before_looking_up_issue(checkout: Path, argument: str) -> None:
    result = resolve(checkout, argument)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")
    assert not (checkout / "injected").exists()


@pytest.mark.parametrize("state,kind,number", [("CLOSED", "issues", 42), ("OPEN", "pull", 42), ("OPEN", "issues", 99)])
def test_rejects_closed_pr_or_mismatched_response(
    checkout: Path, gh_stub: None, monkeypatch: pytest.MonkeyPatch, state: str, kind: str, number: int
) -> None:
    set_response(monkeypatch, "usaturn/maatlog-poc", state=state, kind=kind, number=number)
    result = resolve(checkout, "#42")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")


def test_merged_pr_is_reported_as_pr_not_as_malformed_issue(
    checkout: Path, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_response(monkeypatch, "usaturn/maatlog-poc", state="MERGED", kind="pull")
    result = resolve(checkout, "#42")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "PR" in result.stderr


@pytest.mark.parametrize("response", ["not JSON", "[]", "{}", '{"number": "42"}'])
def test_rejects_malformed_response(
    checkout: Path, gh_stub: None, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setenv("GH_RESPONSE", response)
    result = resolve(checkout, "#42")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")


def test_reports_missing_issue_without_trying_another_repository(
    checkout: Path, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_FAIL", "1")
    result = resolve(checkout, "#42")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")
    assert "404" in result.stderr


def test_unknown_remote_does_not_fall_back_to_parent(checkout: Path, gh_stub: None) -> None:
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/other/repo.git"], cwd=checkout, check=True
    )
    result = resolve(checkout, "#42")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")
