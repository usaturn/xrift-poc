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
DEFAULT_RULES = "[extend]\nuseDefault = true\n"
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
        return {"feature": self.feature, "product": self.product, "product-worktree": self.product_worktree}[name]

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


def prepare_scan(
    layout: Layout, where: str, tmp_path: Path, *, base_rules: str | None = TRUSTED_RULES
) -> tuple[Path, dict[str, str], Path]:
    """base に信頼する設定を push し、ブランチ側で設定を緩めた未 push の commit を 2 つ積む。"""
    cwd = layout.where(where)
    if base_rules is not None:
        commit_file(cwd, ".gitleaks.toml", base_rules, "base rules")
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


def log_opts(record: dict[str, list[str]]) -> list[str]:
    value = next(arg for arg in record["args"] if arg.startswith("--log-opts="))
    return value.removeprefix("--log-opts=").split()


@pytest.mark.parametrize("where", ["feature", "product", "product-worktree"])
def test_scan_uses_base_rules_and_reports_the_scanned_commit(tmp_path: Path, where: str) -> None:
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log = prepare_scan(layout, where, tmp_path)
    sha = git(cwd, "rev-parse", "HEAD")
    base = git(cwd, "rev-parse", f"origin/{layout.base_branch(where)}")
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{sha}\n"
    record = json.loads(log.read_text(encoding="utf-8"))
    assert record["config"] == TRUSTED_RULES
    assert record["env"] == {"GITLEAKS_CONFIG": None, "GITLEAKS_CONFIG_TOML": None}
    assert log_opts(record) == ["--diff-merges=separate", f"{base}..{sha}"]
    assert "--redact" in record["args"]
    assert "--ignore-gitleaks-allow" in record["args"]
    assert not Path(record["path"]).exists()


def test_scan_uses_default_rules_without_base_config(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / SPACED)
    cwd, env, log = prepare_scan(layout, "feature", tmp_path, base_rules=None)
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    assert json.loads(log.read_text(encoding="utf-8"))["config"] == DEFAULT_RULES


def test_scan_uses_the_product_default_branch(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / SPACED, product_branch="trunk")
    # main には古い commit だけを置き、既定ブランチ（trunk）とずらす。
    git(layout.product, "push", "-q", "origin", "HEAD:refs/heads/main")
    stale = git(layout.product, "rev-parse", "HEAD")
    cwd, env, log = prepare_scan(layout, "product", tmp_path)
    trunk = git(cwd, "rev-parse", "origin/trunk")
    sha = git(cwd, "rev-parse", "HEAD")
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    opts = log_opts(json.loads(log.read_text(encoding="utf-8")))
    assert opts == ["--diff-merges=separate", f"{trunk}..{sha}"]
    assert stale != trunk


def test_scan_checks_whole_history_for_empty_product_remote(tmp_path: Path) -> None:
    # GitHub で作ったばかりの空のリポジトリへの初めての push。履歴のすべてが未 push である。
    layout = make_layout(tmp_path / SPACED, push_product=False)
    sha = commit_file(layout.product, "app.txt", "first\n", "first")
    env, log = install_scanner(tmp_path)
    result = run(layout, layout.product, "scan_before_push", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{sha}\n"
    record = json.loads(log.read_text(encoding="utf-8"))
    assert log_opts(record) == ["--diff-merges=separate", sha]
    assert record["config"] == DEFAULT_RULES


@pytest.mark.parametrize(
    "case", ["env-main", "subdirectory", "product-without-origin", "product-uninitialized", "no-root"]
)
def test_scan_refuses_unsafe_locations(tmp_path: Path, case: str) -> None:
    layout = make_layout(tmp_path / SPACED)
    env, log = install_scanner(tmp_path)
    cwd = layout.feature
    if case == "env-main":
        cwd = layout.env.root
    elif case == "subdirectory":
        cwd = layout.feature / "nested"
        cwd.mkdir()
    elif case == "product-without-origin":
        cwd = create_product(layout.env.root, "worlds", "no-origin")
    elif case == "product-uninitialized":
        cwd = create_product(layout.env.root, "items", "plain", git=False)
    else:
        env["XRIFT_POC_ROOT"] = ""
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert not log.exists()


@pytest.mark.parametrize(
    "case",
    [
        "failure",
        "ignore-file",
        "ignore-symlink",
        "remote-unreachable-env",
        "remote-unreachable-product",
        "product-head-unborn",
    ],
)
def test_scan_does_not_approve_unsafe_input(tmp_path: Path, case: str) -> None:
    layout = make_layout(tmp_path / SPACED)
    where = "product" if case.endswith("product") or case == "product-head-unborn" else "feature"
    cwd, env, log = prepare_scan(layout, where, tmp_path)
    if case == "failure":
        env["SCAN_EXIT"] = "1"
    elif case == "ignore-file":
        (cwd / ".gitleaksignore").write_text("ignored-fingerprint\n", encoding="utf-8")
    elif case == "ignore-symlink":
        (cwd / ".gitleaksignore").symlink_to("missing-ignore-file")
    elif case == "remote-unreachable-env":
        layout.env_bare.rename(layout.env_bare.with_name("moved.git"))
    elif case == "remote-unreachable-product":
        layout.product_bare.rename(layout.product_bare.with_name("moved.git"))
    else:
        # ブランチはあるが、既定ブランチ（HEAD）を読めない。
        git(layout.product_bare, "symbolic-ref", "HEAD", "refs/heads/missing")
    result = run(layout, cwd, "scan_before_push", env)
    assert result.returncode != 0
    assert result.stdout == ""
    if case != "failure":
        assert not log.exists()


def secret_like_token() -> str:
    # Gitleaks の既定の github-pat 規則に当たる形。リポジトリにリテラルを置かないよう、実行時に組み立てる。
    rng = random.Random(687)
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def real_gitleaks_env(diff_merges: str | None = None) -> dict[str, str]:
    if shutil.which("gitleaks") is None:
        pytest.fail("the Dev Container and CI install the approved Gitleaks pin; this test needs it on PATH")
    if diff_merges is None:
        return {}
    # git log -m がマージ commit に何を出すかは、利用者の git 設定で変わる。
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "log.diffMerges", "GIT_CONFIG_VALUE_0": diff_merges}


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


@pytest.mark.parametrize("diff_merges", [None, "off"])
@pytest.mark.parametrize("entry", ["conflict", "added"])
def test_scan_detects_secret_introduced_by_merge(tmp_path: Path, entry: str, diff_merges: str | None) -> None:
    layout = make_layout(tmp_path / SPACED)
    token = secret_like_token()
    if entry == "conflict":
        working = prepare_merge(layout, resolution=f'line = "{token}"\n', added=None)
    else:
        working = prepare_merge(layout, resolution='line = "resolved"\n', added=f'token = "{token}"\n')
    result = run(layout, working, "scan_before_push", real_gitleaks_env(diff_merges))
    assert result.returncode != 0
    assert result.stdout == ""
    assert leaks_found(result.stderr) >= 1
    assert token not in result.stderr


def test_scan_ignores_weakened_branch_settings(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / SPACED)
    working = layout.product_worktree
    token = secret_like_token()
    commit_file(working, ".gitleaks.toml", TRUSTED_RULES, "base rules")
    git(working, "push", "-q", "origin", "HEAD:refs/heads/main")
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


def test_scan_stops_on_broken_base_config(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / SPACED)
    working = layout.feature
    commit_file(working, ".gitleaks.toml", "title = \n[[[", "broken base rules")
    git(working, "push", "-q", "origin", "HEAD:refs/heads/main")
    commit_file(working, "app.txt", "change\n", "change")
    result = run(layout, working, "scan_before_push", real_gitleaks_env())
    assert result.returncode != 0
    assert result.stdout == ""


def test_scan_accepts_merge_without_secret(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / SPACED)
    working = prepare_merge(layout, resolution='line = "resolved"\n', added=None)
    result = run(layout, working, "scan_before_push", real_gitleaks_env())
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{git(working, 'rev-parse', 'HEAD')}\n"


def test_scan_accepts_nothing_to_push(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / SPACED)
    result = run(layout, layout.feature, "scan_before_push", real_gitleaks_env())
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{git(layout.feature, 'rev-parse', 'origin/main')}\n"


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


# Claude Code の Bash ツールは zsh で動く。zsh では "$VAR:r" などが修飾子になり、refspec を壊す。
@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("where", ["feature", "product-worktree"])
def test_scanned_push_sends_the_scanned_commit(tmp_path: Path, shell: str, where: str) -> None:
    layout = make_layout(tmp_path / SPACED)
    cwd, env, _ = prepare_scan(layout, where, tmp_path)
    sha = git(cwd, "rev-parse", "HEAD")
    result = run(layout, cwd, scanned_push(), env, shell=shell)
    assert result.returncode == 0, result.stderr
    bare = layout.env_bare if where == "feature" else layout.product_bare
    assert pushed(bare) == sha


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_scanned_push_ignores_head_moved_after_scan(tmp_path: Path, shell: str) -> None:
    layout = make_layout(tmp_path / SPACED)
    cwd, env, _ = prepare_scan(layout, "feature", tmp_path)
    sha = git(cwd, "rev-parse", "HEAD")
    env["SCAN_THEN_COMMIT"] = "1"
    result = run(layout, cwd, scanned_push(), env, shell=shell)
    assert result.returncode == 0, result.stderr
    assert git(cwd, "rev-parse", "HEAD") != sha
    assert pushed(layout.env_bare) == sha


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("case", ["scan-failure", "empty-output"])
@pytest.mark.parametrize("existing", [False, True], ids=["new-branch", "existing-branch"])
def test_scanned_push_does_not_push_without_a_scanned_commit(
    tmp_path: Path, shell: str, case: str, existing: bool
) -> None:
    layout = make_layout(tmp_path / SPACED)
    cwd, env, _ = prepare_scan(layout, "feature", tmp_path)
    before = None
    if existing:
        # 空の送り元の refspec は、先に push した branch を消してしまう。消えずに残ることを確かめる。
        before = git(cwd, "rev-parse", "origin/main")
        git(cwd, "push", "-q", "origin", f"{before}:refs/heads/{SCANNED_BRANCH}")
    invocation = scanned_push()
    if case == "scan-failure":
        env["SCAN_EXIT"] = "1"
    else:
        invocation = f"scan_before_push() {{ :; }}\n{invocation}"
    result = run(layout, cwd, invocation, env, shell=shell)
    assert result.returncode != 0
    assert pushed(layout.env_bare) == before
