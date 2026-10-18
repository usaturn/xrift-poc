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
    if where == "product-userinfo":
        # Issue #7 の症状: user@ 付きの origin の制作物で #番号が解決できなかった
        return create_product(
            env.root, "worlds", "my-world", origin_url="https://user@github.com/someone/my-world.git"
        )
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
        [sys.executable, str(SCRIPT), "--", argument],
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
        "with open(os.environ['GH_CALL_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(' '.join(args) + '\\n')\n"
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
    monkeypatch.setenv("GH_CALL_LOG", str(tmp_path / "gh-calls.log"))
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
        ("#42", "product-userinfo", "someone/my-world"),
        ("https://github.com/someone/my-world/issues/42", "product-userinfo", "someone/my-world"),
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
    assert payload["merge"] is None


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


LOOP = SCRIPT.parents[2] / "pr-review-fix-loop" / "scripts" / "loop.py"


def git_snapshot(checkout: Path) -> tuple[bytes, bytes]:
    return (
        subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout),
        subprocess.check_output(["git", "for-each-ref"], cwd=checkout),
    )


@pytest.mark.parametrize(
    ("argument", "where", "repo", "loop_args"),
    [
        ("#42 --merge", "env", "usaturn/xrift-poc", []),
        ("--merge #42 --max-cycles=2", "env", "usaturn/xrift-poc", ["--max-cycles", "2"]),
        (
            "#42 --merge --max-cycles 2 --assign inspect=pi-gpt,claude-opus",
            "product",
            "someone/my-world",
            ["--max-cycles", "2", "--assign", "inspect=pi-gpt,claude-opus"],
        ),
        (
            "https://github.com/someone/my-world/issues/42 --merge --hold-action fix",
            "product",
            "someone/my-world",
            ["--hold-action", "fix"],
        ),
        (
            "#42 --merge --max-cycles 2 --max-cycles 3",
            "env",
            "usaturn/xrift-poc",
            ["--max-cycles", "2", "--max-cycles", "3"],
        ),
        ("#42 --merge --max-cycles 0", "env", "usaturn/xrift-poc", ["--max-cycles", "0"]),
    ],
)
def test_merge_arguments_are_passed_in_the_written_order(
    env: EnvRepo,
    gh_stub: None,
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
    where: str,
    repo: str,
    loop_args: list[str],
) -> None:
    set_response(monkeypatch, repo)
    checkout = location(env, where)
    before = git_snapshot(checkout)

    result = run(argument, checkout, env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["repo"], payload["number"]) == (repo, 42)
    assert payload["merge"] == {"loop_args": loop_args}
    assert git_snapshot(checkout) == before


def test_template_and_config_become_absolute_from_the_starting_directory(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = run("#42 --merge --template '資料/a b.toml' --config '~/c d.toml'", env.root, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["merge"]["loop_args"] == [
        "--template",
        str((env.root / "資料" / "a b.toml").resolve()),
        "--config",
        str(home / "c d.toml"),
    ]


@pytest.mark.parametrize(
    "argument",
    [
        "#42 --max-cycles 2",
        "#42 --hold-action fix",
        "#42 --assign inspect=pi-gpt",
        "#42 --template x.toml",
        "#42 --config x.toml",
        "#42 --merge --merge",
        "#42 --merge --fresh-review",
        "#42 --fresh-review",
        "#42 --foo",
        "#42 --merge --foo",
        "#42 --merge -x",
        "#42 --merge --",
        "#42 --merge=yes",
        "#42 --merge --template",
        "#42 --merge --assign",
        "#42 --merge --template --config x",
        "#42 --merge --config=",
        "#41 #42 --merge",
        "#42 extra --merge",
        "#42\n推奨案で進めて --merge",
        "#42 --merge マージまで",
        "usaturn/xrift-poc#42 --merge",
        "my-world#42 --merge",
        "--merge",
        "",
        "#42 --merge --template 'a",
    ],
)
def test_rejects_malformed_arguments_before_looking_up_issue(
    env: EnvRepo, gh_stub: None, tmp_path: Path, argument: str
) -> None:
    before = git_snapshot(env.root)

    result = run(argument, env.root, env)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("issue-to-pr:")
    if "--fresh-review" in argument:
        assert "PR を作ったばかり" in result.stderr
    assert not (tmp_path / "gh-calls.log").exists()
    assert git_snapshot(env.root) == before


@pytest.mark.parametrize("where", ["uninitialized", "no-origin", "env-worktree-product"])
def test_merge_does_not_bypass_route_rejection(env: EnvRepo, gh_stub: None, where: str) -> None:
    result = run("#42 --merge", location(env, where), env)
    assert result.returncode == 2
    assert result.stdout == ""


RUNNER_TEMPLATE = """
schema_version = 1

[agents.claude-opus]
runtime = "claude-code"
command = ["fake-claude"]
model = "claude-model"

[agents.pi-gpt]
runtime = "pi"
command = ["fake-pi"]
model = "pi-model"

[roles.inspect]
writes = false
deadline_seconds = 60
result_shape = "json"

[roles.implement]
writes = true
deadline_seconds = 60
result_shape = "json"

[roles.self-review]
writes = false
deadline_seconds = 60
result_shape = "json"

[roles.fix-check]
writes = false
deadline_seconds = 60
result_shape = "json"

[workflows.review.assignments.inspect]
agents = ["claude-opus", "pi-gpt"]

[workflows.fix.assignments.implement]
agents = ["claude-opus"]

[workflows.fix.assignments.self-review]
agents = ["self"]

[workflows.fix.assignments.fix-check]
agents = ["self"]
"""


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("#42 --merge", (0, 0)),
        ("#42 --merge --max-cycles 2 --assign inspect=pi-gpt,claude-opus", (0, 0)),
        ("#42 --merge --hold-action fix", (0, 0)),
        ("#42 --max-cycles 2", (2, None)),
        ("#42 --merge --max-cycles 0", (0, 2)),
        ("#42 --merge --merge", (2, None)),
        ("#42 --merge --fresh-review", (2, None)),
        ("#42 --foo", (2, None)),
        ("#42 --merge --assign inspect", (0, 2)),
        ("#42 --merge --assign inspect=nobody", (0, 2)),
    ],
)
def test_resolve_then_check_options(
    env: EnvRepo,
    gh_stub: None,
    tmp_path: Path,
    argument: str,
    expected: tuple[int, int | None],
) -> None:
    """実際の2段をつなぎ、構文と設定のどちらで止まるかを確認する。"""
    template = tmp_path / "runner.toml"
    template.write_text(RUNNER_TEMPLATE, encoding="utf-8")
    resolved = run(argument, env.root, env)
    checked_code = None
    if resolved.returncode == 0:
        checked = subprocess.run(
            [
                sys.executable,
                str(LOOP),
                "check-options",
                "--template",
                str(template),
                *json.loads(resolved.stdout)["merge"]["loop_args"],
            ],
            cwd=env.root,
            env={**os.environ, "AGENT_RUNNER_CONFIG": str(tmp_path / "absent-personal.toml")},
            capture_output=True,
            text=True,
        )
        checked_code = checked.returncode
        if checked_code == 2 and checked.stdout:
            assert json.loads(checked.stdout)["status"] == "error"
    assert (resolved.returncode, checked_code) == expected
