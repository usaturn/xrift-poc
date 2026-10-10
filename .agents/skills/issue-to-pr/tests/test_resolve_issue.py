"""Exercise the read-only Issue resolver against real Git checkouts and a gh boundary double."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.test_support.repos import EnvRepo, create_env_repo, create_owned_worktree, create_product

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolve_issue.py"


WORLD_ORIGIN = "https://github.com/someone/my-world.git"


@pytest.fixture
def env(tmp_path: Path) -> EnvRepo:
    return create_env_repo(tmp_path / "workspace")


def location(env: EnvRepo, where: str) -> Path:
    if where == "env":
        return env.root
    if where == "product":
        return create_product(env.root, "worlds", "my-world", origin_url=WORLD_ORIGIN)
    if where == "uninitialized":
        return create_product(env.root, "worlds", "plain", git=False)
    if where == "no-origin":
        return create_product(env.root, "items", "local-only")
    if where == "env-worktree-product":
        worktree = create_owned_worktree(env.root, env.root / ".worktrees", name="feature")
        project = worktree / "worlds" / "w"
        project.mkdir(parents=True)
        return project
    raise AssertionError(where)


def run(argument: str, cwd: Path, env: EnvRepo) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), argument],
        cwd=cwd,
        env={**os.environ, "XRIFT_POC_ROOT": str(env.root)},
        capture_output=True,
        text=True,
    )


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
        "if repo.lower() != os.environ['EXPECTED_REPO'].lower():\n"
        "    sys.exit(65)\n"
        "if os.environ.get('GH_FAIL'):\n"
        "    print('HTTP 404: not found', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(os.environ['GH_RESPONSE'])\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    set_response(monkeypatch, "usaturn/xrift-poc")


def set_response(
    monkeypatch: pytest.MonkeyPatch,
    repo: str,
    *,
    state: str = "OPEN",
    kind: str = "issues",
    number: int = 42,
    canonical: str | None = None,
) -> None:
    monkeypatch.setenv("EXPECTED_REPO", repo)
    shown = canonical or repo
    monkeypatch.setenv(
        "GH_RESPONSE",
        json.dumps(
            {
                "number": number,
                "title": "Fix a regression",
                "body": "Keep unrelated work intact.",
                "state": state,
                "url": f"https://github.com/{shown}/{kind}/{number}",
            }
        ),
    )


@pytest.mark.parametrize(
    ("argument", "where", "expected"),
    [
        ("https://github.com/usaturn/xrift-poc/issues/42", "env", "usaturn/xrift-poc"),
        ("#42", "env", "usaturn/xrift-poc"),
        ("https://github.com/usaturn/xrift-poc/issues/42", "product", "usaturn/xrift-poc"),
        ("https://github.com/someone/my-world/issues/42", "product", "someone/my-world"),
        ("#42", "product", "someone/my-world"),
    ],
)
def test_accepted_targets(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch, argument: str, where: str, expected: str
) -> None:
    set_response(monkeypatch, expected)
    result = run(argument, location(env, where), env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["repo"], payload["number"]) == (expected, 42)


@pytest.mark.parametrize(
    ("argument", "where"),
    [
        ("https://github.com/someone/my-world/issues/42", "env"),  # 制作物の外から制作物の Issue は解決しない
        ("https://github.com/someone/other/issues/42", "product"),  # 判定した制作物と違うリポジトリ
        ("#42", "uninitialized"),  # Git 未初期化の制作物
        ("#42", "no-origin"),  # origin の無い制作物
        ("#42", "env-worktree-product"),  # 親の worktree の worlds/ の下（制作物の置き場所ではない）
        ("https://github.com/usaturn/xrift-poc/pull/42", "env"),
        ("https://github.com/usaturn/xrift-poc/issues/42?foo=bar", "env"),
        ("https://github.com.evil.test/usaturn/xrift-poc/issues/42", "env"),
    ],
)
def test_rejected_targets(env: EnvRepo, gh_stub: None, argument: str, where: str) -> None:
    result = run(argument, location(env, where), env)
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize(
    "argument", ["https://github.com/other/repo/issues/42#comment", "#0", "#-42", "42", "#42; touch injected"]
)
def test_rejects_invalid_input_before_looking_up_issue(env: EnvRepo, argument: str) -> None:
    result = run(argument, env.root, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")
    assert not (env.root / "injected").exists()


@pytest.mark.parametrize("state,kind,number", [("CLOSED", "issues", 42), ("OPEN", "pull", 42), ("OPEN", "issues", 99)])
def test_rejects_closed_pr_or_mismatched_response(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch, state: str, kind: str, number: int
) -> None:
    set_response(monkeypatch, "usaturn/xrift-poc", state=state, kind=kind, number=number)
    result = run("#42", env.root, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")


def test_merged_pr_is_reported_as_pr_not_as_malformed_issue(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_response(monkeypatch, "usaturn/xrift-poc", state="MERGED", kind="pull")
    result = run("#42", env.root, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "PR" in result.stderr


@pytest.mark.parametrize("response", ["not JSON", "[]", "{}", '{"number": "42"}'])
def test_rejects_malformed_response(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setenv("GH_RESPONSE", response)
    result = run("#42", env.root, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")


def test_reports_missing_issue_without_trying_another_repository(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_FAIL", "1")
    result = run("#42", env.root, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")
    assert "404" in result.stderr


def test_accepts_repository_name_in_a_different_case(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_response(monkeypatch, "USATURN/XRift-Poc", canonical="usaturn/xrift-poc")
    result = run("https://github.com/USATURN/XRift-Poc/issues/42", env.root, env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["repo"] == "usaturn/xrift-poc"
    assert payload["url"] == "https://github.com/usaturn/xrift-poc/issues/42"


def test_product_origin_in_lowercase_accepts_mixed_case_response(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_response(monkeypatch, "someone/my-world", canonical="Someone/My-World")
    result = run("#42", location(env, "product"), env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["repo"] == "Someone/My-World"
