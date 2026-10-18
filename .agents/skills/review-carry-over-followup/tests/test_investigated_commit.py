"""手順 3 の調べる commit の固定と後片付けを、SKILL.md のブロックのまま bash と zsh で実行する。

origin は GitHub の URL のまま置き、``url.<bare>.insteadOf`` でローカルの bare に向ける（ネットワークを使わない）。
bare の既定ブランチは ``main`` で、``release/1.x`` と ``refs/pull/<N>/head`` を持つ。
``gh pr view`` の出力（``pr.json``）と ``resolve`` の出力（``target.json``）は、同じ形の JSON を書いて渡す。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.repos import ENV_ORIGIN, create_env_repo, create_product

REPO_ROOT = Path(__file__).resolve().parents[4]
SHELLS = ("bash", "zsh")
PR_NUMBER = 7
PRODUCT_REPO = "someone/my-world"
PRODUCT_URL = f"https://github.com/{PRODUCT_REPO}.git"
# 利用者の global・system の git 設定（insteadOf など）を読まない。
ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
OUTPUTS = ("REPRO_ROOT", "TARGET_ROOT", "DEFAULT_BRANCH", "PR_HEAD_SHA", "INVESTIGATED_SHA", "MERGED_INTO_DEFAULT")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env={**os.environ, **ISOLATED_GIT},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass(frozen=True, slots=True)
class Remote:
    bare: Path
    main: str
    pr_head: str
    merge: str | None


def build_remote(base: Path, seed: Path, merged_into: str | None) -> Remote:
    """``seed`` の HEAD を root にした bare を作り、``main`` と PR の head、指定した branch へのマージを置く。

    ``merged_into`` は ``main``・``release/1.x``・``deleted``（マージ先を push した後で消す）・None（未マージ。
    PR の base は ``release/1.x``）。``main`` 以外へのマージは ``main`` に入らない。
    """
    bare = base / "remote.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(bare))
    git(seed, "push", "-q", str(bare), "HEAD:refs/heads/main")
    work = base / "work"
    git(base, "clone", "-q", str(bare), str(work))
    root = git(work, "rev-parse", "HEAD")
    git(work, "commit", "-q", "--allow-empty", "-m", "main side")
    if merged_into == "main":
        git(work, "switch", "-q", "-c", "base")
    else:
        git(work, "switch", "-q", "-c", "base", root)
        git(work, "commit", "-q", "--allow-empty", "-m", "release side")
    git(work, "switch", "-q", "-c", "pr")
    git(work, "commit", "-q", "--allow-empty", "-m", "pr change")
    pr_head = git(work, "rev-parse", "HEAD")
    merge: str | None = None
    if merged_into is not None:
        git(work, "switch", "-q", "base")
        git(work, "merge", "-q", "--no-ff", "-m", "merge the pr", "pr")
        merge = git(work, "rev-parse", "HEAD")
    branch = {"main": "main", "deleted": "integration/tmp"}.get(merged_into or "", "release/1.x")
    refspecs = [f"base:refs/heads/{branch}", f"{pr_head}:refs/pull/{PR_NUMBER}/head"]
    if branch != "main":
        refspecs.append("main:refs/heads/main")
    git(work, "push", "-q", "origin", *refspecs)
    if merged_into == "deleted":
        git(work, "push", "-q", "origin", f":refs/heads/{branch}")
    return Remote(bare, git(bare, "rev-parse", "refs/heads/main"), pr_head, merge)


def write_session(session: Path, *, repository: str, target_root: Path | None, remote: Remote, state: str) -> None:
    session.mkdir()
    target = {
        "parent_root": "<parent>",
        "repository": repository,
        "number": PR_NUMBER,
        "url": f"https://github.com/{repository}/pull/{PR_NUMBER}",
        "head_ref": "feature",
        "head_sha": remote.pr_head,
        "head_repository": repository,
        "base_ref": "release/1.x",
        "state": "closed" if state != "OPEN" else "open",
        "clone_url": f"https://github.com/{repository}.git",
        "review_dir": "<reviews>",
        "target_root": None if target_root is None else str(target_root),
    }
    pr = {
        "url": target["url"],
        "state": state,
        "mergedAt": "2026-10-17T00:00:00Z" if remote.merge else None,
        "mergeCommit": {"oid": remote.merge} if remote.merge else None,
        "headRefOid": remote.pr_head,
        "baseRefName": "release/1.x",
        "body": "",
    }
    (session / "target.json").write_text(json.dumps(target, ensure_ascii=False), encoding="utf-8")
    (session / "pr.json").write_text(json.dumps(pr, ensure_ascii=False), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Case:
    parent: Path
    session: Path
    tmpdir: Path
    remote: Remote
    insteadof: tuple[str, str]


def make_case(tmp_path: Path, *, product: bool, merged_into: str | None, state: str) -> Case:
    env = create_env_repo(
        tmp_path / "repos",
        copy_paths=(
            ".agents/scripts/route_target.py",
            ".agents/scripts/review_target.py",
            ".agents/scripts/agent_tmp.py",
        ),
    )
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    seed = create_product(env.root, "worlds", "my-world", origin_url=PRODUCT_URL) if product else env.root
    remote = build_remote(remotes, seed, merged_into)
    url = PRODUCT_URL if product else ENV_ORIGIN
    session = tmp_path / "session"
    write_session(
        session,
        repository=PRODUCT_REPO if product else "usaturn/xrift-poc",
        target_root=None if product else env.root,
        remote=remote,
        state=state,
    )
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    return Case(env.root, session, tmpdir, remote, (f"url.{remote.bare}.insteadOf", url))


def run_script(skill_shell: Any, case: Case, shell: str, script: str, *, pr_number: int = PR_NUMBER) -> Any:
    config_key, config_value = case.insteadof
    return skill_shell.run(
        shell,
        script,
        cwd=case.parent,
        env={
            **ISOLATED_GIT,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": config_key,
            "GIT_CONFIG_VALUE_0": config_value,
            "TMPDIR": str(case.tmpdir),
            "SESSION": str(case.session),
            "PARENT": str(case.parent),
            "PR_NUMBER": str(pr_number),
        },
    )


def run_pin_and_cleanup(skill_shell: Any, case: Case, shell: str) -> dict[str, str]:
    """固定のブロックの後に後片付けのブロックを同じシェルで実行し、表示した値を返す。

    間で、固定のブロックが作った worktree の HEAD を ``WORKTREE_HEAD`` として表示する。
    """
    script = "\n".join(
        (
            skill_shell.block("pin the investigated commit"),
            'printf \'WORKTREE_HEAD=%s\\n\' "$(git -C "$REPRO_ROOT/worktree" rev-parse HEAD)"',
            skill_shell.block("clean up the investigation"),
            "",
        )
    )
    result = run_script(skill_shell, case, shell, script)
    assert result.returncode == 0, result.stderr
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, _, value = line.partition("=")
        if name in (*OUTPUTS, "WORKTREE_HEAD"):
            values[name] = value
    assert sorted(values) == sorted((*OUTPUTS, "WORKTREE_HEAD")), result.stdout
    return values


@pytest.mark.parametrize(
    ("merged_into", "state", "investigated", "in_default"),
    [
        ("release/1.x", "MERGED", "main", "1"),
        ("deleted", "MERGED", "main", "1"),
        ("main", "MERGED", "main", "0"),
        (None, "OPEN", "pr", ""),
        (None, "CLOSED", "main", ""),
    ],
)
@pytest.mark.parametrize("product", [False, True], ids=["parent-pr", "product-pr"])
@pytest.mark.parametrize("shell", SHELLS)
def test_pin_selects_the_default_branch_or_the_pr_head(
    skill_shell: Any,
    tmp_path: Path,
    shell: str,
    product: bool,
    merged_into: str | None,
    state: str,
    investigated: str,
    in_default: str,
) -> None:
    case = make_case(tmp_path, product=product, merged_into=merged_into, state=state)
    values = run_pin_and_cleanup(skill_shell, case, shell)
    expected = case.remote.main if investigated == "main" else case.remote.pr_head
    assert (values["DEFAULT_BRANCH"], values["INVESTIGATED_SHA"]) == ("main", expected)
    assert values["PR_HEAD_SHA"] == case.remote.pr_head
    assert values["MERGED_INTO_DEFAULT"] == in_default
    assert values["WORKTREE_HEAD"] == expected
    # REPRO_ROOT は 1 回だけ作り、後片付けで消える
    repro_root = Path(values["REPRO_ROOT"])
    assert repro_root.parent == case.tmpdir and repro_root.name.startswith("review-fix-planning.")
    assert list(case.tmpdir.iterdir()) == []
    if product:
        # target_root が null の PR は、REPRO_ROOT の下の一時 clone を調べる（後片付けで clone も消える）
        assert values["TARGET_ROOT"] == str(repro_root / "repo")
    else:
        assert values["TARGET_ROOT"] == str(case.parent)
        assert git(case.parent, "for-each-ref", "refs/carry-over/") == ""
        assert str(repro_root) not in git(case.parent, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("failure", ["remote-unreachable", "pr-ref-missing"])
@pytest.mark.parametrize("shell", SHELLS)
def test_pin_stops_when_the_latest_commits_cannot_be_fetched(
    skill_shell: Any, tmp_path: Path, shell: str, failure: str
) -> None:
    case = make_case(tmp_path, product=False, merged_into="release/1.x", state="MERGED")
    # 親には、以前に取得した古い origin/main が残っている
    assert git(case.parent, "rev-parse", "refs/remotes/origin/main") != case.remote.main
    pr_number = PR_NUMBER
    if failure == "remote-unreachable":
        case.remote.bare.rename(case.remote.bare.with_name("moved.git"))
    else:
        pr_number += 1
    result = run_script(
        skill_shell, case, shell, skill_shell.block("pin the investigated commit"), pr_number=pr_number
    )
    assert result.returncode != 0
    assert "INVESTIGATED_SHA=" not in result.stdout
    assert str(case.tmpdir) not in git(case.parent, "worktree", "list", "--porcelain")
