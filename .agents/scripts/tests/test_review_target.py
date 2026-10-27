"""Issue #483: review_target.py が PR・保存先・レビューディレクトリ・push 先を取り違えないこと。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "review_target.py"
SPEC = importlib.util.spec_from_file_location("review_target", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
target = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(target)

PARENT_ORIGIN = "https://github.com/usaturn/xrift-poc.git"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    return path.resolve()


def make_parent(path: Path, origin: str | None = PARENT_ORIGIN) -> Path:
    parent = make_repo(path)
    (parent / ".gitignore").write_text("/reviews/\n", encoding="utf-8")
    git(parent, "add", ".gitignore")
    git(parent, "commit", "-m", "parent")
    if origin is not None:
        git(parent, "remote", "add", "origin", origin)
    return parent


def make_remote(tmp_path: Path, *, number: int = 3, branch: str = "hogehoge") -> tuple[Path, str]:
    """bare remote に main と head ブランチを置き、refs/pull/<number>/head を head に向ける。"""
    work = make_repo(tmp_path / "remote-work")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "base")
    git(work, "switch", "-c", branch)
    (work / "change.txt").write_text("change\n", encoding="utf-8")
    git(work, "add", "change.txt")
    git(work, "commit", "-m", "change")
    head = git(work, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    git(work, "push", "-q", str(remote), "main", branch, f"{branch}:refs/pull/{number}/head")
    return remote.resolve(), head


def http(status: int, body: object | None = None) -> str:
    text = json.dumps(body) if body is not None else ""
    return f"HTTP/2.0 {status} Status\nContent-Type: application/json\n\n{text}"


def pull(
    repository: str,
    number: int,
    head_ref: str,
    head_sha: str = "a" * 40,
    head_repository: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/{repository}/pull/{number}",
        "state": "open",
        "head": {
            "ref": head_ref,
            "sha": head_sha,
            "repo": {"full_name": head_repository or repository},
        },
        "base": {
            "ref": "main",
            "repo": {"full_name": repository, "clone_url": f"https://github.com/{repository}.git"},
        },
    }


class FakeGh:
    """gh api -i の応答を API のパスごとに返す。"""

    def __init__(self, responses: dict[str, tuple[str, str]]) -> None:
        self.responses = responses

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(argv[:3]) == ["gh", "api", "-i"], argv
        stdout, stderr = self.responses.get(argv[3], (http(404, {"message": "Not Found"}), ""))
        status = 0 if stdout.startswith("HTTP/2.0 2") else 1
        return subprocess.CompletedProcess(list(argv), status, stdout, stderr)


def gh_for(*pulls: dict[str, Any]) -> FakeGh:
    responses: dict[str, tuple[str, str]] = {}
    for item in pulls:
        repository = item["base"]["repo"]["full_name"]
        responses[f"repos/{repository}/pulls/{item['number']}"] = (http(200, item), "")
        responses[f"repos/{repository}"] = (http(200, {"full_name": repository}), "")
    return FakeGh(responses)


def error_code(excinfo: pytest.ExceptionInfo[Exception]) -> int:
    error: Any = excinfo.value
    return error.code


# 指定の解釈と URL の読み取り


def test_parse_number_url_and_reject_other() -> None:
    """照合が部分一致に緩むと、別リポジトリの指定を親の同じ番号の PR と黙って読み、別の PR を修正する。"""
    assert target.parse_pull_request_argument(
        "https://github.com/usaturn/wabun-rst-ulint/pull/3/files"
    ) == target.PullRequestArgument("usaturn/wabun-rst-ulint", 3)
    for value in ("owner/repo#3", "https://gitlab.com/a/b/pull/3"):
        with pytest.raises(target.TargetError) as excinfo:
            target.parse_pull_request_argument(value)
        assert error_code(excinfo) == target.EXIT_USAGE


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com@evil.test/someone/my-world",
        "https://github.com.evil.test/someone/my-world",
    ],
)
def test_github_repository_ignores_lookalike_hosts(url: str) -> None:
    """別ホストを GitHub と読むと、scan_context の送信先の照合が通り、修正を別ホストへ push する。"""
    assert target.github_repository(url) is None


# GitHub への照会


def test_fetch_pull_request_success() -> None:
    """head と base・head 側のリポジトリを読み違えると、fork の PR を同じリポジトリの PR と黙って扱う。"""
    gh = gh_for(pull("usaturn/xrift-sample-world", 12, "feat/x", head_repository="someone/xrift-sample-world"))

    pr = target.fetch_pull_request("usaturn/xrift-sample-world", 12, runner=gh)

    assert pr.repository == "usaturn/xrift-sample-world"
    assert pr.head_ref == "feat/x"
    assert pr.head_repository == "someone/xrift-sample-world"
    assert pr.clone_url == "https://github.com/usaturn/xrift-sample-world.git"


# resolve（pr-review）


def test_resolve_uses_canonical_repository_name(tmp_path: Path) -> None:
    """URL の綴りのまま保存先を作ると、同じ PR のレビューが分かれ、review-dir の探索が一部を黙って見落とす。"""
    parent = make_parent(tmp_path / "parent")
    gh = FakeGh(
        {"repos/Usaturn/Xrift-Sample-World/pulls/3": (http(200, pull("usaturn/xrift-sample-world", 3, "feat/x")), "")}
    )

    result = target.resolve("https://github.com/Usaturn/Xrift-Sample-World/pull/3", parent_root=parent, runner=gh)

    assert result["repository"] == "usaturn/xrift-sample-world"
    assert result["review_dir"] == str(target.review_directory(parent, "usaturn/xrift-sample-world", 3, "feat/x"))


def test_require_parent_root_rejects_linked_worktree(tmp_path: Path) -> None:
    """feature worktree を親として受け付けると、レビューを worktree に保存し、片付けで黙って消える。"""
    parent = make_parent(tmp_path / "parent")
    linked = tmp_path / "linked"
    git(parent, "worktree", "add", "-q", "--detach", str(linked))

    with pytest.raises(target.TargetError) as excinfo:
        target.require_parent_root(linked)

    assert error_code(excinfo) == target.EXIT_PARENT


# レビューディレクトリの探索（review-fix-planning・review-verify）


def review_dir(parent: Path, *parts: str) -> Path:
    directory = parent.joinpath("reviews", *parts)
    directory.mkdir(parents=True)
    (directory / "model.md").write_text("review\n", encoding="utf-8")
    return directory


def test_find_review_directory_does_not_cross_repositories(tmp_path: Path) -> None:
    """保存先の owner の階層を落とす・探索がリポジトリを越えると、別のリポジトリのレビューを黙って使い修正する。"""
    parent = make_parent(tmp_path / "parent")
    target.review_directory(parent, "someone/xrift-poc", 4, "feat/x").mkdir(parents=True)
    gh = gh_for(pull("usaturn/xrift-poc", 4, "feat/x"))

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory("#4", parent_root=parent, for_planning=False, runner=gh)
    assert error_code(excinfo) == target.EXIT_NO_REVIEW

    own = target.review_directory(parent, "usaturn/xrift-poc", 4, "feat/x")
    own.mkdir(parents=True)
    result = target.find_review_directory("#4", parent_root=parent, for_planning=False, runner=gh)
    assert result["review_dir"] == str(own)


def test_find_review_directory_lists_multiple_candidates(tmp_path: Path) -> None:
    """候補が複数のとき 1 つを黙って選ぶと、もう片方のレビューの指摘が判断から欠ける。"""
    parent = make_parent(tmp_path / "parent")
    review_dir(parent, "usaturn", "xrift-poc", "#4-a")
    review_dir(parent, "usaturn", "xrift-poc", "#4-b")

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            "#4", parent_root=parent, for_planning=False, runner=gh_for(pull("usaturn/xrift-poc", 4, "b"))
        )

    assert error_code(excinfo) == target.EXIT_AMBIGUOUS


def test_find_review_directory_rejects_other_owner_before_api(tmp_path: Path) -> None:
    """オーナーの制限（#483）が外れると、review-fix-planning・review-verify が他人の PR を修正・検証する。"""
    parent = make_parent(tmp_path / "parent")

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            "https://github.com/someone/x/pull/1",
            parent_root=parent,
            for_planning=True,
            runner=gh_for(pull("someone/x", 1, "y")),
        )

    assert error_code(excinfo) == target.EXIT_OWNER


# 既存の checkout への fetch


def test_fetch_pull_head_preserves_tracking_refs_tags_and_fetch_head(tmp_path: Path) -> None:
    """fetch の指定が外れると、pr-review が既存の checkout の追跡 ref・tag・FETCH_HEAD を黙って書き換える。"""
    remote, head = make_remote(tmp_path)
    checkout = tmp_path / "source"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", "--no-tags", str(remote), str(checkout)],
        check=True,
        capture_output=True,
    )
    git(checkout, "config", "--add", "remote.origin.fetch", "+refs/pull/*/head:refs/remotes/origin/pr-*")
    git(checkout, "config", "remote.origin.tagOpt", "--tags")
    git(remote, "update-ref", "refs/tags/published-after-clone", head)
    fetch_head = checkout / ".git" / "FETCH_HEAD"
    fetch_head.write_text("preserve caller fetch result\n", encoding="utf-8")
    before = git(
        checkout, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes", "refs/tags"
    )

    ref = target.fetch_pull_head(checkout, 3, head)

    assert git(checkout, "rev-parse", ref) == head
    assert (
        git(checkout, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes", "refs/tags")
        == before
    )
    assert fetch_head.read_text(encoding="utf-8") == "preserve caller fetch result\n"


# push 前の PR・checkout の照合（scan_before_push の PR 修正の入口の最後の関門）


def scan_checkout(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    repo = "usaturn/xrift-sample-world"
    root = make_parent(tmp_path / "空白 と 日本語", origin=f"https://github.com/{repo}.git")
    pr = pull(repo, 7, "fix/world", head_sha=git(root, "rev-parse", "HEAD"))
    pr["base"]["ref"] = "integration/world"
    value = {
        "repository": repo,
        "number": 7,
        "head_ref": "fix/world",
        "head_sha": pr["head"]["sha"],
        "head_repository": repo,
        "clone_url": f"https://github.com/{repo}.git",
        "base_ref": "integration/world",
    }
    return root, value, pr


def test_scan_context_accepts_descendant_and_uses_pr_base(tmp_path: Path) -> None:
    """返す base_ref が PR の base でなくなると、Gitleaks が誤った範囲を検査し、検査していない commit を push する。"""
    root, value, pr = scan_checkout(tmp_path)
    git(root, "commit", "--allow-empty", "-m", "fix")
    git(
        root,
        "config",
        "remote.origin.pushurl",
        "git@github.com:Usaturn/Xrift-Sample-World.git",
    )

    result = target.scan_context(value, root, runner=gh_for(pr))

    assert result == {
        "repository": "usaturn/xrift-sample-world",
        "base_ref": "integration/world",
        "head_ref": "fix/world",
    }


@pytest.mark.parametrize("case", ["fork", "head-ref", "closed"])
def test_scan_context_rejects_changed_or_unpushable_pr(tmp_path: Path, case: str) -> None:
    """fork・head変更・途中でclosedとなったPRを通すと、対象外のブランチへ修正を黙ってpushする。"""
    root, value, pr = scan_checkout(tmp_path)
    target.scan_context(value, root, runner=gh_for(pr))
    if case == "fork":
        pr["head"]["repo"]["full_name"] = "someone/fork"
    elif case == "head-ref":
        pr["head"]["ref"] = "other"
    else:
        pr["state"] = "closed"
    gh = FakeGh({"repos/usaturn/xrift-sample-world/pulls/7": (http(200, pr), "")})

    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=gh)
    assert error_code(excinfo) == target.EXIT_CLONE


@pytest.mark.parametrize("case", ["many-origins", "pushurl"])
def test_scan_context_rejects_wrong_checkout_or_destination(tmp_path: Path, case: str) -> None:
    """push 先が PR と別のリポジトリ・複数の URL でも通すと、修正と秘密を別のリポジトリへ黙って push する。"""
    root, value, pr = scan_checkout(tmp_path)
    key = "remote.origin.url" if case == "many-origins" else "remote.origin.pushurl"
    git(root, "config", "--add", key, "https://github.com/usaturn/other.git")

    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=gh_for(pr))
    assert error_code(excinfo) == target.EXIT_CLONE


def test_scan_context_owner_and_api_failures_are_not_approval(tmp_path: Path) -> None:
    """照会の失敗やオーナーの制限を通すと、PR を確かめないまま検査済みとして push する。"""
    root, value, _pr = scan_checkout(tmp_path)
    gh = FakeGh({"repos/usaturn/xrift-sample-world/pulls/7": (http(403, {}), "")})
    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=gh)
    assert error_code(excinfo) == target.EXIT_AUTH
    value.update(
        repository="someone/world",
        head_repository="someone/world",
        clone_url="https://github.com/someone/world.git",
    )
    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=FakeGh({}))
    assert error_code(excinfo) == target.EXIT_OWNER


def isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """利用者の global・system の git 設定（insteadOf・pushInsteadOf）で、push 先の展開が変わらないようにする。"""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_scan_context_rejects_push_rewritten_to_another_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pushInsteadOf で push 先だけが別のリポジトリに書き換わると、PR の base の履歴を検査せずに送る。"""
    isolate_git_config(monkeypatch)
    root, value, pr = scan_checkout(tmp_path)
    git(root, "commit", "--allow-empty", "-m", "fix")
    git(root, "config", "url.https://github.com/someone/.pushInsteadOf", "https://github.com/usaturn/")

    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=gh_for(pr))
    assert error_code(excinfo) == target.EXIT_CLONE


def test_scan_context_destination_only_checks_every_push_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """2 つ目の push 先を見ないと未検査の commit を別のリポジトリへ送り、URL を表示すると認証情報が出る。"""
    isolate_git_config(monkeypatch)
    root = make_parent(tmp_path / "空白 と 日本語")
    userinfo = "agent:" + "-".join(["not", "a", "real", "token"])
    git(root, "config", "--add", "remote.origin.pushurl", PARENT_ORIGIN)
    git(root, "config", "--add", "remote.origin.pushurl", f"https://{userinfo}@github.com/someone/xrift-poc.git")

    assert target.main(["scan-context", "--destination-only", "--checkout", str(root)]) == target.EXIT_CLONE
    captured = capsys.readouterr()
    assert userinfo not in captured.err + captured.out
