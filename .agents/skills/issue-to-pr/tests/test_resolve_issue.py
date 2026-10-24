"""Exercise the read-only Issue resolver against real Git checkouts and a gh boundary double."""

from __future__ import annotations

import json
import os
import shlex
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
        [sys.executable, str(SCRIPT), "--", argument],
        cwd=cwd,
        env={**os.environ, "XRIFT_POC_ROOT": str(env.root)},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def gh_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``issue view 42`` に、EXPECTED_REPO のときだけ GH_RESPONSE で答える gh。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:3] != ['issue', 'view', '42'] or '--repo' not in args:\n"
        "    sys.exit(64)\n"
        "repo = args[args.index('--repo') + 1]\n"
        "if repo.lower() != os.environ['EXPECTED_REPO'].lower():\n"
        "    sys.exit(65)\n"
        "print(os.environ['GH_RESPONSE'])\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    set_response(monkeypatch, "usaturn/xrift-poc")


def set_response(monkeypatch: pytest.MonkeyPatch, repo: str, *, state: str = "OPEN", kind: str = "issues") -> None:
    monkeypatch.setenv("EXPECTED_REPO", repo)
    monkeypatch.setenv(
        "GH_RESPONSE",
        json.dumps(
            {
                "number": 42,
                "title": "Fix a regression",
                "body": "Keep unrelated work intact.",
                "state": state,
                "url": f"https://github.com/{repo}/{kind}/42",
            }
        ),
    )


def test_number_in_a_product_resolves_to_the_product_repository(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """制作物の #42 を親に解決する・書いていないオプションを返すと、別の作業や頼まれないマージに黙って進む。"""
    set_response(monkeypatch, "someone/my-world")

    result = run("#42", location(env, "product"), env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["repo"], payload["number"]) == ("someone/my-world", 42)
    assert payload["merge"] is None
    assert payload["implement"] is None


@pytest.mark.parametrize(
    ("argument", "where", "responder"),
    [
        # 制作物の外（親）から制作物の Issue は解決しない
        pytest.param(
            "https://github.com/someone/my-world/issues/42", "env", "someone/my-world", id="product-url-from-env"
        ),
        # Git 未初期化の制作物（stop）を親と同じに扱わない
        pytest.param("#42", "uninitialized", "usaturn/xrift-poc", id="uninitialized"),
        # repository が null の制作物を親に読み替えない
        pytest.param("#42", "no-origin", "usaturn/xrift-poc", id="no-origin"),
        # 親の feature worktree の worlds/ の下（制作物の置き場所ではない）を親と判定しない
        pytest.param("#42", "env-worktree-product", "usaturn/xrift-poc", id="env-worktree-product"),
    ],
)
def test_rejected_targets(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch, argument: str, where: str, responder: str
) -> None:
    """扱えないリポジトリの Issue を解決すると、親と制作物を取り違えて別のリポジトリで worktree・PR を黙って作る。"""
    # gh は responder の Issue に答えるので、拒否は resolve_issue 自身の判定から来る
    set_response(monkeypatch, responder)

    result = run(argument, location(env, where), env)

    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("state", "kind"),
    [pytest.param("CLOSED", "issues", id="closed"), pytest.param("OPEN", "pull", id="pull-request")],
)
def test_rejects_closed_pr_or_mismatched_response(
    env: EnvRepo, gh_stub: None, monkeypatch: pytest.MonkeyPatch, state: str, kind: str
) -> None:
    """応答の state・URL を照合しないと、closed の Issue や PR（gh は番号で返す）の本文で実装・マージまで進める。"""
    set_response(monkeypatch, "usaturn/xrift-poc", state=state, kind=kind)

    result = run("#42", env.root, env)

    assert result.returncode == 2
    assert result.stdout == ""


def test_merge_arguments_are_passed_in_the_written_order(env: EnvRepo, gh_stub: None) -> None:
    """ループ用オプションに既定値を足す・並べ替える・重複を消す・値を落とすと、違う上限や担当でループが黙って動く。"""
    result = run("#42 --merge --max-cycles 2 --max-cycles 3", env.root, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["merge"] == {"loop_args": ["--max-cycles", "2", "--max-cycles", "3"]}


@pytest.mark.parametrize(
    ("option", "value", "equals", "relative"),
    [
        # 相対パスは起動時の cwd で絶対パスにする（loop の start を別の cwd で実行しても同じファイルを読む）
        pytest.param("--template", "資料/a b.toml", True, ("資料", "a b.toml"), id="relative-template"),
        # Issue #44: ./ で始めた ~ は cwd の ~ という名前のディレクトリで、ホームではない
        pytest.param("--config", "./~/a.toml", False, ("~", "a.toml"), id="dot-tilde-config"),
    ],
)
def test_path_options_resolve_against_the_starting_directory(
    env: EnvRepo,
    gh_stub: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    option: str,
    value: str,
    equals: bool,
    relative: tuple[str, ...],
) -> None:
    """パスを起動時の cwd で絶対パスにしない・./~/ をホームと読むと、ループが別の設定ファイルを黙って読む。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    written = shlex.quote(f"{option}={value}") if equals else f"{option} {shlex.quote(value)}"

    result = run(f"#42 --merge {written}", env.root, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["merge"]["loop_args"] == [option, str(env.root.joinpath(*relative).resolve())]


def test_implement_is_returned_separately_from_loop_args(env: EnvRepo, gh_stub: None) -> None:
    """--implement を落とすと、利用者が指定した担当ではなく既定の担当で黙って実装する。"""
    result = run("#42 --merge --max-cycles 2 --implement pi-gpt", env.root, env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["implement"] == "pi-gpt"
    assert payload["merge"] == {"loop_args": ["--max-cycles", "2"]}
