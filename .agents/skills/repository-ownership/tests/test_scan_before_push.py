"""repository-ownership の「公開前の秘密検査」のコードブロックを、親と制作物の fixture で実行する。

origin の URL は GitHub の URL のまま置き（route_target.py はこの設定値で判定する）、
``url.<bare>.insteadOf`` でローカルの bare リポジトリへ向けて、fetch・ls-remote・push をネットワーク無しで動かす。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.test_support.repos import EnvRepo, create_env_repo, create_product

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL = REPO_ROOT / ".agents/skills/repository-ownership/SKILL.md"
ENV_URL = "https://github.com/usaturn/xrift-poc.git"
PRODUCT_URL = "https://github.com/someone/my-world.git"
# 空白と日本語を含むパスでも、検査する範囲と push する commit が変わらないことを確かめる。
SPACED = "空白 と 日本語"
PRODUCT_NAME = "空白 ワールド"
TRUSTED_RULES = 'title = "trusted base"\n[extend]\nuseDefault = true\n'
# 利用者の global・system の git 設定（hooksPath・insteadOf・log.diffMerges など）を読まない。
ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

SCANNER = """\
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
config = pathlib.Path(args[args.index("--config") + 1])
record = {{
    "args": args,
    "config": config.read_text(encoding="utf-8"),
    "path": str(config),
    "env": {{key: os.environ.get(key) for key in ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")}},
}}
pathlib.Path({log!r}).write_text(json.dumps(record), encoding="utf-8")
if os.environ.get("SCAN_THEN_COMMIT"):
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", *identity, "commit", "--allow-empty", "-qm", "after scan"], check=True)
sys.exit(int(os.environ.get("SCAN_EXIT", "0")))
"""


def block(marker: str) -> str:
    match = re.search(
        rf"```bash\n(# repository-ownership {re.escape(marker)}\n.*?)\n```",
        SKILL.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, f"{marker} block not found in SKILL.md"
    return match.group(1)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env={**os.environ, **ISOLATED_GIT},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_file(cwd: Path, name: str, content: str, message: str) -> str:
    (cwd / name).write_text(content, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", message)
    return git(cwd, "rev-parse", "HEAD")


def create_bare(path: Path, branch: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(path.parent, "init", "-q", "--bare", "-b", branch, str(path))
    return path


@dataclass(frozen=True, slots=True)
class Layout:
    env: EnvRepo
    env_bare: Path
    feature: Path
    product: Path
    product_bare: Path
    product_branch: str
    product_worktree: Path

    def where(self, name: str) -> Path:
        return {"feature": self.feature, "product": self.product}[name]

    def base_branch(self, name: str) -> str:
        return "main" if name == "feature" else self.product_branch


def make_layout(base: Path, *, product_branch: str = "main", push_product: bool = True) -> Layout:
    """親の main checkout・feature worktree と、制作物の main checkout・中の worktree を作る。"""
    env = create_env_repo(
        base / "repos",
        copy_paths=(
            "tools/devenv/pyproject.toml",
            "tools/devenv/uv.lock",
            ".agents/scripts/route_target.py",
            ".agents/scripts/review_target.py",
        ),
    )
    env_bare = create_bare(base / "remotes" / "xrift-poc.git", "main")
    git(env.root, "config", f"url.{env_bare}.insteadOf", ENV_URL)
    git(env.root, "push", "-q", "origin", "HEAD:refs/heads/main")
    feature = env.root / ".claude" / "worktrees" / "feat"
    git(env.root, "worktree", "add", "-q", "-b", "feat/scan", str(feature), "origin/main")

    product = create_product(env.root, "worlds", PRODUCT_NAME, origin_url=PRODUCT_URL)
    product_bare = create_bare(base / "remotes" / "my-world.git", product_branch)
    git(product, "config", f"url.{product_bare}.insteadOf", PRODUCT_URL)
    if push_product:
        git(product, "push", "-q", "origin", f"HEAD:refs/heads/{product_branch}")
        git(product, "fetch", "-q", "origin")
    with (product / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write(".worktrees/\n")
    # 制作物の worktree は detached HEAD で作る（branch が無くても検査した SHA を push できる）。
    product_worktree = product / ".worktrees" / "feat"
    git(product, "worktree", "add", "-q", "--detach", str(product_worktree), "HEAD")
    return Layout(env, env_bare, feature, product, product_bare, product_branch, product_worktree)


def install_scanner(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "scan.json"
    scanner = bin_dir / "gitleaks"
    scanner.write_text(f"#!{sys.executable}\n" + SCANNER.format(log=str(log)), encoding="utf-8")
    scanner.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}, log


def prepare_scan(layout: Layout, where: str, tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """base に信頼する設定を push し、ブランチ側で設定を緩めた未 push の commit を 2 つ積む。"""
    cwd = layout.where(where)
    commit_file(cwd, ".gitleaks.toml", TRUSTED_RULES, "base rules")
    git(cwd, "push", "-q", "origin", f"HEAD:refs/heads/{layout.base_branch(where)}")
    git(cwd, "fetch", "-q", "origin")
    for index in range(2):
        commit_file(cwd, ".gitleaks.toml", f'title = "weakened branch {index}"\n', f"candidate {index}")
    env, log = install_scanner(tmp_path)
    env["GITLEAKS_CONFIG"] = str(cwd / ".gitleaks.toml")
    env["GITLEAKS_CONFIG_TOML"] = 'title = "environment bypass"'
    return cwd, env, log


def run(
    layout: Layout, cwd: Path, invocation: str, env: dict[str, str] | None = None, shell: str = "bash"
) -> subprocess.CompletedProcess[str]:
    script = f"{block('scan before push')}\n{invocation}\n"
    argv = ["zsh", "-f", "-c", script] if shell == "zsh" else ["bash", "-c", script]
    return subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, **ISOLATED_GIT, "XRIFT_POC_ROOT": str(layout.env.root), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def scanned_range(record: dict[str, list[str]]) -> str:
    """--log-opts の中の、オプションではない唯一のトークン（検査する範囲）。"""
    value = next(arg for arg in record["args"] if arg.startswith("--log-opts="))
    revisions = [token for token in value.removeprefix("--log-opts=").split() if not token.startswith("-")]
    assert len(revisions) == 1, value
    return revisions[0]


def test_scan_uses_base_rules_and_reports_the_scanned_commit(tmp_path: Path) -> None:
    """branch で緩めた設定・環境変数や base..HEAD より狭い範囲で検査し、秘密を含む commit を push する。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log = prepare_scan(layout, "feature", tmp_path)
    sha = git(cwd, "rev-parse", "HEAD")
    base = git(cwd, "rev-parse", "origin/main")
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{sha}\n"
    record = json.loads(log.read_text(encoding="utf-8"))
    assert record["config"] == TRUSTED_RULES
    assert record["env"] == {"GITLEAKS_CONFIG": None, "GITLEAKS_CONFIG_TOML": None}
    assert scanned_range(record) == f"{base}..{sha}"


def test_scan_uses_the_product_default_branch(tmp_path: Path) -> None:
    """制作物の既定ブランチ以外を base にし、信頼できない branch の設定で検査を緩める。"""
    layout = make_layout(tmp_path / SPACED, product_branch="trunk")
    # main には古い commit だけを置き、既定ブランチ（trunk）とずらす。
    git(layout.product, "push", "-q", "origin", "HEAD:refs/heads/main")
    stale = git(layout.product, "rev-parse", "HEAD")
    cwd, env, log = prepare_scan(layout, "product", tmp_path)
    trunk = git(cwd, "rev-parse", "origin/trunk")
    sha = git(cwd, "rev-parse", "HEAD")
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    assert scanned_range(json.loads(log.read_text(encoding="utf-8"))) == f"{trunk}..{sha}"
    assert stale != trunk


def test_scan_checks_whole_history_for_empty_product_remote(tmp_path: Path) -> None:
    """空のリポジトリへの初めての push で範囲を誤り、履歴の大半を検査せずに公開する。"""
    # GitHub で作ったばかりの空のリポジトリへの初めての push。履歴のすべてが未 push である。
    layout = make_layout(tmp_path / SPACED, push_product=False)
    sha = commit_file(layout.product, "app.txt", "first\n", "first")
    env, log = install_scanner(tmp_path)
    result = run(layout, layout.product, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{sha}\n"
    assert scanned_range(json.loads(log.read_text(encoding="utf-8"))) == sha


def test_scan_refuses_the_parent_main_checkout(tmp_path: Path) -> None:
    """親の main checkout から検査した SHA を出し、main checkout の作業を feature branch として公開する。"""
    layout = make_layout(tmp_path / SPACED)
    env, log = install_scanner(tmp_path)
    result = run(layout, layout.env.root, "scan_before_push", env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert not log.exists()


@pytest.mark.parametrize("case", ["failure", "ignore-file"])
def test_scan_does_not_approve_unsafe_input(tmp_path: Path, case: str) -> None:
    """scanner の失敗や .gitleaksignore の除外を検査済みとして扱い、秘密を含む commit を push する。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log = prepare_scan(layout, "feature", tmp_path)
    if case == "failure":
        env["SCAN_EXIT"] = "1"
    else:
        # Gitleaks 8.30.1 は、空の --gitleaks-ignore-path を渡しても検査する場所の .gitleaksignore を読む。
        (cwd / ".gitleaksignore").write_text("ignored-fingerprint\n", encoding="utf-8")
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode != 0
    assert result.stdout == ""
    if case != "failure":
        assert not log.exists()


def secret_like_token() -> str:
    # Gitleaks の既定の github-pat 規則に当たる形。リポジトリにリテラルを置かないよう、実行時に組み立てる。
    rng = random.Random(687)
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def real_gitleaks_env(*settings: str) -> dict[str, str]:
    """実物の Gitleaks を使う env。``settings`` は利用者の git 設定（``key=value``）として渡す。"""
    if shutil.which("gitleaks") is None:
        pytest.fail("the Dev Container and CI install the approved Gitleaks pin; this test needs it on PATH")
    env = {"GIT_CONFIG_COUNT": str(len(settings))}
    for index, setting in enumerate(settings):
        key, value = setting.split("=", 1)
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def leaks_found(stderr: str) -> int:
    found = re.search(r"leaks found: (\d+)", stderr)
    assert found is not None, stderr
    return int(found.group(1))


def prepare_merge(layout: Layout, *, resolution: str, added: str | None) -> Path:
    """競合する origin/main を未 push の commit にマージし、マージ commit に指定した内容を入れる。

    base に設定は置かない（既定の規則で検出することも確かめる）。
    """
    working = layout.feature
    fork = commit_file(working, "app.txt", 'line = "base"\n', "base")
    git(working, "push", "-q", "origin", "HEAD:refs/heads/main")
    commit_file(working, "app.txt", 'line = "main"\n', "main side")
    git(working, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(working, "checkout", "-q", "--detach", fork)
    commit_file(working, "app.txt", 'line = "feature"\n', "feature side")
    git(working, "fetch", "-q", "origin")
    merge = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "merge", "--no-edit", "origin/main"],
        cwd=working,
        env={**os.environ, **ISOLATED_GIT},
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode != 0, "the fixture must produce a conflict to resolve"
    (working / "app.txt").write_text(resolution, encoding="utf-8")
    git(working, "add", "app.txt")
    if added is not None:
        (working / "merged.txt").write_text(added, encoding="utf-8")
        git(working, "add", "merged.txt")
    git(working, "commit", "-q", "--no-edit")
    assert len(git(working, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    return working


# git log -p がマージ commit に何を出すか、色を付けるかは、利用者の git 設定で変わる。
@pytest.mark.parametrize(("setting", "entry"), [("log.diffMerges=off", "conflict"), ("color.ui=always", "added")])
def test_scan_detects_secret_introduced_by_merge(tmp_path: Path, setting: str, entry: str) -> None:
    """マージ commit で入った秘密が利用者の git 設定で git log -p から消え、検査を通って公開される。"""
    layout = make_layout(tmp_path / SPACED)
    token = secret_like_token()
    if entry == "conflict":
        working = prepare_merge(layout, resolution=f'line = "{token}"\n', added=None)
    else:
        working = prepare_merge(layout, resolution='line = "resolved"\n', added=f'token = "{token}"\n')
    result = run(layout, working, "scan_before_push", real_gitleaks_env(setting))
    assert result.returncode != 0
    assert result.stdout == ""
    assert leaks_found(result.stderr) >= 1
    assert token not in result.stderr


def test_scan_ignores_weakened_branch_settings(tmp_path: Path) -> None:
    """base に設定が無いとき branch の設定・環境変数・inline の allow で規則が緩み、秘密が検査を通る。"""
    layout = make_layout(tmp_path / SPACED)
    working = layout.product_worktree
    token = secret_like_token()
    weakened = "[extend]\nuseDefault = true\n[allowlist]\npaths = ['''.*''']\n"
    (working / ".gitleaks.toml").write_text(weakened, encoding="utf-8")
    (working / "secret.txt").write_text(f'token = "{token}" # gitleaks:allow\n', encoding="utf-8")
    git(working, "add", ".gitleaks.toml", "secret.txt")
    git(working, "commit", "-qm", "weaken and leak")
    env = {**real_gitleaks_env(), "GITLEAKS_CONFIG": str(working / ".gitleaks.toml"), "GITLEAKS_CONFIG_TOML": weakened}
    result = run(layout, working, "scan_before_push", env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert leaks_found(result.stderr) >= 1
    assert token not in result.stderr


def git_path(cwd: Path, name: str) -> Path:
    return Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-path", name))


# commit できる属性と textconv で、ファイルの中身が git log -p から消えても検出する。
@pytest.mark.parametrize("case", ["committed-diff-attr", "textconv"])
def test_scan_sees_through_attributes_and_settings_that_hide_content(tmp_path: Path, case: str) -> None:
    """属性・textconv でファイルの中身が git log -p から消え、秘密が検査を通る。"""
    layout = make_layout(tmp_path / SPACED)
    working = layout.product_worktree
    settings: list[str] = []
    if case == "committed-diff-attr":
        commit_file(working, ".gitattributes", "secret.txt -diff\n", "hide secret.txt from diffs")
    else:
        attributes = git_path(working, "info/attributes")
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text("secret.txt diff=hide\n", encoding="utf-8")
        settings = ["diff.hide.textconv=true"]
    token = secret_like_token()
    commit_file(working, "secret.txt", f'token = "{token}"\n', "leak")
    result = run(layout, working, "scan_before_push", real_gitleaks_env(*settings))
    assert result.returncode != 0
    assert result.stdout == ""
    assert leaks_found(result.stderr) >= 1
    assert token not in result.stderr


def test_scan_checks_the_root_commit_of_a_first_push(tmp_path: Path) -> None:
    """log.showRoot=false で root commit の差分が消え、初めての push の秘密が検査を通る。"""
    layout = make_layout(tmp_path / SPACED, push_product=False)
    token = secret_like_token()
    (layout.product / ".env").write_text(f'TOKEN="{token}"\n', encoding="utf-8")
    git(layout.product, "add", ".env")
    git(layout.product, "commit", "-q", "--amend", "--no-edit")
    assert git(layout.product, "rev-list", "--count", "HEAD") == "1"
    result = run(layout, layout.product, "scan_before_push", real_gitleaks_env("log.showRoot=false"))
    assert result.returncode != 0
    assert result.stdout == ""
    assert leaks_found(result.stderr) >= 1


def test_scan_stops_when_git_log_fails(tmp_path: Path) -> None:
    """Gitleaks は内部の git log の失敗でも成功を返すので、検査していない commit を検査済みとして push する。"""
    # Gitleaks 8.30.1 は、内部の git log が失敗しても「no leaks found」で 0 を返す。
    layout = make_layout(tmp_path / SPACED)
    working = layout.feature
    commit_file(working, "app.txt", "change\n", "change")
    result = run(layout, working, "scan_before_push", real_gitleaks_env("diff.algorithm=bogus"))
    assert result.returncode != 0
    assert result.stdout == ""


def test_scan_does_not_depend_on_the_caller_ifs(tmp_path: Path) -> None:
    """呼び出し元の IFS で Gitleaks の --log-opts が壊れ、0 commits のまま検査済みとして push する。"""
    # 関数は呼び出し元のシェルの IFS を引き継ぐ。--log-opts の連結が IFS に左右されると、
    # Gitleaks の内部の git log だけが失敗し、0 commits scanned のまま成功を返していた。
    layout = make_layout(tmp_path / SPACED)
    working = layout.feature
    token = secret_like_token()
    commit_file(working, "secret.txt", f'token = "{token}"\n', "leak")
    result = run(layout, working, "IFS=,\nscan_before_push", real_gitleaks_env(), shell="zsh")
    assert result.returncode != 0
    assert result.stdout == ""
    assert leaks_found(result.stderr) >= 1
    assert token not in result.stderr


def test_scan_stops_on_broken_base_config(tmp_path: Path) -> None:
    """壊れた base の設定で Gitleaks が規則なしに成功を返しても止まらず、検査していない commit を push する。"""
    layout = make_layout(tmp_path / SPACED)
    working = layout.feature
    commit_file(working, ".gitleaks.toml", "title = \n[[[", "broken base rules")
    git(working, "push", "-q", "origin", "HEAD:refs/heads/main")
    commit_file(working, "app.txt", "change\n", "change")
    result = run(layout, working, "scan_before_push", real_gitleaks_env())
    assert result.returncode != 0
    assert result.stdout == ""


SCANNED_BRANCH = "feat/issue-22-scanned"


def scanned_push(branch: str = SCANNED_BRANCH) -> str:
    return block("scanned push").replace("<branch>", branch)


def pushed(bare: Path, branch: str = SCANNED_BRANCH) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=bare,
        env={**os.environ, **ISOLATED_GIT},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


# push のブロックは、Claude Code の Bash ツールと同じ zsh で実行する（"$VAR:r" などが修飾子になるシェル）。
def test_scanned_push_ignores_head_moved_after_scan(tmp_path: Path) -> None:
    """検査の後に動いた HEAD を push し、検査していない commit を公開する。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, _ = prepare_scan(layout, "feature", tmp_path)
    sha = git(cwd, "rev-parse", "HEAD")
    env["SCAN_THEN_COMMIT"] = "1"
    result = run(layout, cwd, scanned_push(), env, shell="zsh")
    assert result.returncode == 0, result.stderr
    assert git(cwd, "rev-parse", "HEAD") != sha
    assert pushed(layout.env_bare) == sha


def test_scanned_push_does_not_push_without_a_scanned_commit(tmp_path: Path) -> None:
    """検査した SHA が空のまま push し、空の送り元の refspec で既存のリモートの branch を消す。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, _ = prepare_scan(layout, "feature", tmp_path)
    # 空の送り元の refspec は、先に push した branch を消してしまう。消えずに残ることを確かめる。
    before = git(cwd, "rev-parse", "origin/main")
    git(cwd, "push", "-q", "origin", f"{before}:refs/heads/{SCANNED_BRANCH}")
    invocation = f"scan_before_push() {{ :; }}\n{scanned_push()}"
    result = run(layout, cwd, invocation, env, shell="zsh")
    assert result.returncode != 0
    assert pushed(layout.env_bare) == before


# PR 修正は親の外の clone でも動かす。origin は設定値で照合し、転送はローカル bare に隔離する。
REVIEW_BRANCH = "fix/review"
REVIEW_BASE = "integration/world"
REVIEW_URL = "https://github.com/usaturn/my-world.git"


def prepare_review(layout: Layout, tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    """制作物の PR（head は REVIEW_BRANCH、base は REVIEW_BASE）と、未 push の修正を積んだ一時 clone を作る。"""
    source = layout.product_worktree
    bare = layout.product_bare
    git(source, "remote", "set-url", "origin", REVIEW_URL)
    git(source, "config", f"url.{bare}.insteadOf", REVIEW_URL)
    commit_file(source, ".gitleaks.toml", TRUSTED_RULES, "PR base rules")
    git(source, "push", "-q", "origin", f"HEAD:refs/heads/{REVIEW_BASE}")
    head = commit_file(source, "app.txt", "published\n", "PR head")
    git(source, "push", "-q", "origin", f"HEAD:refs/heads/{REVIEW_BRANCH}")
    cwd = tmp_path / "一時 clone 空白"
    git(tmp_path, "clone", "-q", "--branch", REVIEW_BRANCH, str(bare), str(cwd))
    git(cwd, "remote", "set-url", "origin", REVIEW_URL)
    git(cwd, "config", f"url.{bare}.insteadOf", REVIEW_URL)
    commit_file(cwd, "app.txt", "local fix\n", "unpublished fix")
    repository = "usaturn/my-world"
    snapshot = {
        "repository": repository,
        "number": 7,
        "head_ref": REVIEW_BRANCH,
        "head_sha": head,
        "head_repository": repository,
        "base_ref": REVIEW_BASE,
        "clone_url": REVIEW_URL,
    }
    response = {
        "number": 7,
        "html_url": f"https://github.com/{repository}/pull/7",
        "state": "open",
        "head": {"ref": REVIEW_BRANCH, "sha": head, "repo": {"full_name": repository}},
        "base": {"ref": REVIEW_BASE, "repo": {"full_name": repository, "clone_url": REVIEW_URL}},
    }
    env, log = install_scanner(tmp_path)
    bin_dir = tmp_path / "bin"
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "value = json.loads(os.environ['REVIEW_TARGET'])\n"
        "assert sys.argv[1:] == ['api', '-i', f'repos/{value[\"repository\"]}/pulls/{value[\"number\"]}']\n"
        "print('HTTP/2.0 200 Status\\nContent-Type: application/json\\n')\n"
        "print(os.environ['REVIEW_RESPONSE'])\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        REVIEW_TARGET=json.dumps(snapshot),
        REVIEW_RESPONSE=json.dumps(response),
    )
    return cwd, env, log, bare


def review_push() -> str:
    return scanned_push(REVIEW_BRANCH).replace(
        "$(scan_before_push)", '$(scan_before_push --review-target "$REVIEW_TARGET")'
    )


def test_review_scan_uses_pr_base_and_pushes_only_scanned_sha(tmp_path: Path) -> None:
    """PR 修正で PR の base 以外の設定・範囲で検査し、PR の作者が緩めた設定で秘密を通す。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log, bare = prepare_review(layout, tmp_path)
    sha = git(cwd, "rev-parse", "HEAD")
    base = git(bare, "rev-parse", f"refs/heads/{REVIEW_BASE}")
    env["SCAN_THEN_COMMIT"] = "1"
    result = run(layout, cwd, review_push(), env, shell="zsh")
    assert result.returncode == 0, result.stderr
    assert pushed(bare, REVIEW_BRANCH) == sha
    assert git(cwd, "rev-parse", "HEAD") != sha
    record = json.loads(log.read_text(encoding="utf-8"))
    assert record["config"] == TRUSTED_RULES
    assert scanned_range(record) == f"{base}..{sha}"


@pytest.mark.parametrize("case", ["repo-mismatch", "no-context", "base-moved"])
def test_review_scan_failure_never_pushes(tmp_path: Path, case: str) -> None:
    """origin が PR と違う clone、PR の照合を経ない clone、base の変わった PR から、照合を無視して push する。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log, bare = prepare_review(layout, tmp_path)
    before = pushed(bare, REVIEW_BRANCH)
    invocation = review_push()
    if case == "repo-mismatch":
        other = "https://github.com/usaturn/other.git"
        git(cwd, "remote", "set-url", "origin", other)
        # 照合を外したときに push がネットワークで失敗して通らないよう、転送は同じローカル bare に向ける。
        git(cwd, "config", "--add", f"url.{bare}.insteadOf", other)
    elif case == "base-moved":
        response = json.loads(env["REVIEW_RESPONSE"])
        response["base"]["ref"] = "main"
        env["REVIEW_RESPONSE"] = json.dumps(response)
    else:
        invocation = scanned_push(REVIEW_BRANCH)
    result = run(layout, cwd, invocation, env, shell="zsh")
    assert result.returncode != 0
    assert pushed(bare, REVIEW_BRANCH) == before
    assert not log.exists()


def test_review_context_does_not_bypass_location_guards(tmp_path: Path) -> None:
    """PR の引数で、unknown 以外の stop の場所（親の worktree の中の制作物の clone など）から push する。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log, bare = prepare_review(layout, tmp_path)
    misplaced = layout.feature / "worlds" / "bad"
    misplaced.parent.mkdir()
    git(tmp_path, "clone", "-q", str(cwd), str(misplaced))
    git(misplaced, "remote", "set-url", "origin", REVIEW_URL)
    # 場所の検査を外したときに fetch がネットワークで失敗して通らないよう、転送はローカル bare に向ける。
    git(misplaced, "config", f"url.{bare}.insteadOf", REVIEW_URL)
    result = run(layout, misplaced, 'scan_before_push --review-target "$REVIEW_TARGET"', env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert not log.exists()


def execution_push() -> str:
    path = REPO_ROOT / ".agents/skills/review-fix-planning/references/execution.md"
    match = re.search(r"```bash\n(# review-fix-planning scanned push\n.*?)\n```", path.read_text(), re.DOTALL)
    assert match is not None
    return match.group(1)


def test_review_fix_execution_block_pushes_only_verified_scanned_commit(tmp_path: Path) -> None:
    """review-fix-planning の push のブロックが、確認した HEAD と違う commit（確認後に動いた commit）を push する。"""
    layout = make_layout(tmp_path / SPACED)
    cwd, env, _, bare = prepare_review(layout, tmp_path)
    landing = tmp_path / "反映 空白"
    landing.mkdir()
    cwd.rename(landing / "clone")
    before = pushed(bare, REVIEW_BRANCH)
    env.update(LAND_ROOT=str(landing), TARGET_JSON=env["REVIEW_TARGET"], LOCAL_HEAD="a" * 40)
    result = run(layout, landing / "clone", execution_push(), env, shell="zsh")
    assert result.returncode != 0
    assert pushed(bare, REVIEW_BRANCH) == before
