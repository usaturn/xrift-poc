"""手順 3 の調べる commit の固定と後片付けを、SKILL.md のブロックのまま bash で実行する。

origin は GitHub の URL のまま置き、``url.<bare>.insteadOf`` でローカルの bare に向ける（ネットワークを使わない）。
bare の既定ブランチは ``main`` で、``release/1.x`` と ``refs/pull/<N>/head`` を持つ。
``gh pr view`` の出力（``pr.json``）と ``resolve`` の出力（``target.json``）は、同じ形の JSON を書いて渡す。
zsh に固有の誤り（``$VAR:r`` などの修飾子）は、無効な refspec・パスになり ``git fetch`` と ``: "${…:?}"`` で止まる。
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
SHELL = "bash"
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

    ``merged_into`` は ``main``・``release/1.x``・None（未マージ。PR の base は ``release/1.x``）。
    ``release/1.x`` へのマージは ``main`` に入らない。
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
    branch = "main" if merged_into == "main" else "release/1.x"
    refspecs = [f"base:refs/heads/{branch}", f"{pr_head}:refs/pull/{PR_NUMBER}/head"]
    if branch != "main":
        refspecs.append("main:refs/heads/main")
    git(work, "push", "-q", "origin", *refspecs)
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


def run_script(skill_shell: Any, case: Case, script: str, *, pr_number: int = PR_NUMBER) -> Any:
    config_key, config_value = case.insteadof
    return skill_shell.run(
        SHELL,
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


def run_pin_and_cleanup(skill_shell: Any, case: Case) -> dict[str, str]:
    """固定のブロックの後に後片付けのブロックを同じシェルで実行し、表示した値を返す。

    clone が消える前に、対象の Git root・調査 SHA の commit 解決と worktree の HEAD を採取する。
    """
    script = "\n".join(
        (
            skill_shell.block("pin the investigated commit"),
            'printf \'TARGET_TOPLEVEL=%s\\n\' "$(git -C "$TARGET_ROOT" rev-parse --show-toplevel)"',
            "printf 'TARGET_COMMIT=%s\\n' "
            '"$(git -C "$TARGET_ROOT" rev-parse --verify "${INVESTIGATED_SHA}^{commit}")"',
            'printf \'WORKTREE_HEAD=%s\\n\' "$(git -C "$REPRO_ROOT/worktree" rev-parse HEAD)"',
            skill_shell.block("clean up the investigation"),
            "",
        )
    )
    result = run_script(skill_shell, case, script)
    assert result.returncode == 0, result.stderr
    observations = ("WORKTREE_HEAD", "TARGET_TOPLEVEL", "TARGET_COMMIT")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, _, value = line.partition("=")
        if name in (*OUTPUTS, *observations):
            values[name] = value
    assert sorted(values) == sorted((*OUTPUTS, *observations)), result.stdout
    return values


@pytest.mark.parametrize(
    ("product", "merged_into", "state", "investigated", "in_default"),
    [
        pytest.param(False, "release/1.x", "MERGED", "main", "1", id="parent-release-merged"),
        pytest.param(False, "main", "MERGED", "main", "0", id="parent-main-merged"),
        pytest.param(False, None, "OPEN", "pr", "", id="parent-open"),
        pytest.param(False, None, "CLOSED", "main", "", id="parent-closed"),
        pytest.param(True, "release/1.x", "MERGED", "main", "1", id="product-release-merged"),
    ],
)
def test_pin_selects_the_default_branch_or_the_pr_head(
    skill_shell: Any,
    tmp_path: Path,
    product: bool,
    merged_into: str | None,
    state: str,
    investigated: str,
    in_default: str,
) -> None:
    """調べる commit やマージ先の判定を取り違えると誤った結論を公開し、後片付けが消しすぎると親の checkout を失う。"""
    case = make_case(tmp_path, product=product, merged_into=merged_into, state=state)
    parent_head = git(case.parent, "rev-parse", "HEAD")
    parent_root = Path(git(case.parent, "rev-parse", "--show-toplevel"))
    values = run_pin_and_cleanup(skill_shell, case)
    expected = case.remote.main if investigated == "main" else case.remote.pr_head
    assert (values["DEFAULT_BRANCH"], values["INVESTIGATED_SHA"]) == ("main", expected)
    assert values["PR_HEAD_SHA"] == case.remote.pr_head
    assert values["MERGED_INTO_DEFAULT"] == in_default
    assert values["WORKTREE_HEAD"] == expected
    assert values["TARGET_COMMIT"] == values["INVESTIGATED_SHA"] == expected
    if product:
        # target_root が null の PR は、REPRO_ROOT の下の一時 clone を調べる（親のリポジトリを調べない）
        target_root = Path(values["TARGET_ROOT"])
        assert Path(values["REPRO_ROOT"]) in target_root.parents
        assert target_root == Path(values["TARGET_TOPLEVEL"])
        assert target_root != parent_root
    else:
        assert values["TARGET_ROOT"] == str(case.parent)
    # 後片付けは自分の作ったものだけを消す（親の checkout と TMPDIR は残る）
    assert git(case.parent, "rev-parse", "HEAD") == parent_head
    assert case.tmpdir.is_dir()


def test_pin_stops_when_the_latest_commits_cannot_be_fetched(skill_shell: Any, tmp_path: Path) -> None:
    """fetch の失敗を見逃すと、手元に残る古い origin/main・PR の head を黙って調べ、正しく見える値を表示する。

    remote には届き、PR の ref だけが無い（1 つの refspec の失敗で fetch 全体が失敗する）。remote に届かない場合は、
    fetch より前の ``ls-remote`` が失敗して ``: "${DEFAULT_BRANCH:?…}"`` で止まる。
    """
    case = make_case(tmp_path, product=False, merged_into="release/1.x", state="MERGED")
    missing = PR_NUMBER + 1
    # 親には、以前に取得した古い origin/main と、後片付けの前に止まった実行の PR の ref が残っている
    stale = git(case.parent, "rev-parse", "refs/remotes/origin/main")
    assert stale != case.remote.main
    git(case.parent, "update-ref", f"refs/carry-over/{missing}", stale)
    result = run_script(skill_shell, case, skill_shell.block("pin the investigated commit"), pr_number=missing)
    assert result.returncode != 0
    assert "INVESTIGATED_SHA=" not in result.stdout
