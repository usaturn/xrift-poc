"""Issue #483: review_target.py が PR の解決・保存先・レビューディレクトリ・Git 操作の対象を決める仕様。"""

from __future__ import annotations

import importlib.util
import json
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
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    git(
        work,
        "push",
        "-q",
        str(remote),
        "main",
        branch,
        f"{branch}:refs/pull/{number}/head",
    )
    return remote.resolve(), head


def redirect(monkeypatch: pytest.MonkeyPatch, remote: Path, url: str) -> None:
    """GitHub の URL の fetch・clone をローカルの bare remote へ向ける（URL の文字列は変えない）。"""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)


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
            "repo": {
                "full_name": repository,
                "clone_url": f"https://github.com/{repository}.git",
            },
        },
    }


class FakeGh:
    """gh api -i の応答を API のパスごとに返す。呼ばれた argv を calls に残す。"""

    def __init__(self, responses: dict[str, tuple[str, str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
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


# 指定の解釈と名前


def test_parse_number_url_and_reject_other() -> None:
    assert target.parse_pull_request_argument("3") == target.PullRequestArgument(None, 3)
    assert target.parse_pull_request_argument(" #3 ") == target.PullRequestArgument(None, 3)
    assert target.parse_pull_request_argument(
        "https://github.com/usaturn/wabun-rst-ulint/pull/3/files"
    ) == target.PullRequestArgument("usaturn/wabun-rst-ulint", 3)
    for value in ("owner/repo#3", "https://gitlab.com/a/b/pull/3", "#0", "pull/3", ""):
        with pytest.raises(target.TargetError) as excinfo:
            target.parse_pull_request_argument(value)
        assert error_code(excinfo) == target.EXIT_USAGE


def test_github_repository_reads_https_and_ssh() -> None:
    assert target.github_repository("https://github.com/usaturn/xrift-poc.git") == "usaturn/xrift-poc"
    assert target.github_repository("https://github.com/usaturn/xrift-sample-world") == "usaturn/xrift-sample-world"
    assert target.github_repository("git@github.com:usaturn/xrift-sample-world.git") == "usaturn/xrift-sample-world"
    assert target.github_repository("ssh://git@github.com/usaturn/xrift-sample-world") == "usaturn/xrift-sample-world"
    assert target.github_repository("file:///tmp/remote.git") is None
    assert target.github_repository("https://github.com/usaturn/..") is None


# 制作物・親の origin の設定値として使われる、GitHub を指すことが明らかな書き方（usaturn/xrift-poc#7）
LENIENT_GITHUB_URLS = [
    "https://user@github.com/someone/my-world.git",
    "https://user:token@github.com/someone/my-world.git",
    "https://GitHub.com/someone/my-world",
    "http://github.com/someone/my-world/",
    "git@GitHub.com:someone/my-world.git",
]


@pytest.mark.parametrize("url", LENIENT_GITHUB_URLS)
def test_github_repository_reads_unambiguous_github_spellings(url: str) -> None:
    assert target.github_repository(url) == "someone/my-world"


@pytest.mark.parametrize(
    "url",
    [
        "github-personal:someone/my-world.git",  # SSH のホスト別名は行き先を決められない
        "https://github.com@evil.test/someone/my-world",
        "https://github.com.evil.test/someone/my-world",
        "https://a@b@github.com/someone/my-world",
        "https://github.com:443/someone/my-world",
        "https://gitlab.com/someone/my-world.git",
    ],
)
def test_github_repository_ignores_lookalike_hosts(url: str) -> None:
    assert target.github_repository(url) is None


def test_review_directory_uses_owner_and_repo_levels(tmp_path: Path) -> None:
    assert target.review_directory(tmp_path, "usaturn/wabun-rst-ulint", 3, "hogehoge") == (
        tmp_path / "reviews" / "usaturn" / "wabun-rst-ulint" / "#3-hogehoge"
    )
    assert target.review_directory(tmp_path, "usaturn/xrift-sample-world", 12, "feature/x").name == "#12-feature-x"


@pytest.mark.parametrize("value", ["usaturn/..", "usaturn/.", "../x", "a/b/c", "a", "-a/b", "a/b c"])
def test_validate_repository_rejects_unsafe_names(value: str) -> None:
    with pytest.raises(target.TargetError) as excinfo:
        target.validate_repository(value)
    assert error_code(excinfo) == target.EXIT_USAGE


def test_naming_rules_keep_the_helper_messages() -> None:
    with pytest.raises(target.TargetError, match="head branch cannot produce a safe review directory"):
        target.normalize_head_branch("feature/")
    with pytest.raises(target.TargetError, match="PR number cannot produce a safe review directory"):
        target.review_directory_name(0, "x")
    with pytest.raises(target.TargetError, match="model identifier is not a safe filename"):
        target.validate_model_name(" gpt")
    assert target.validate_model_name("opencode/deepseek v4") == "opencode-deepseek-v4"


# GitHub への照会


def test_fetch_pull_request_success() -> None:
    gh = gh_for(
        pull(
            "usaturn/xrift-sample-world",
            12,
            "feat/x",
            head_repository="someone/xrift-sample-world",
        )
    )

    pr = target.fetch_pull_request("usaturn/xrift-sample-world", 12, runner=gh)

    assert pr.repository == "usaturn/xrift-sample-world"
    assert pr.head_ref == "feat/x"
    assert pr.head_repository == "someone/xrift-sample-world"
    assert pr.clone_url == "https://github.com/usaturn/xrift-sample-world.git"
    assert gh.calls == [["gh", "api", "-i", "repos/usaturn/xrift-sample-world/pulls/12"]]


def test_fetch_pull_request_not_found_when_repository_exists() -> None:
    gh = FakeGh({"repos/usaturn/xrift-poc": (http(200, {}), "")})

    with pytest.raises(target.TargetError) as excinfo:
        target.fetch_pull_request("usaturn/xrift-poc", 999, runner=gh)

    assert error_code(excinfo) == target.EXIT_PR_NOT_FOUND
    assert "usaturn/xrift-poc に PR #999 は存在しません" in str(excinfo.value)
    assert "PR URL" in str(excinfo.value)


def test_fetch_pull_request_repository_missing() -> None:
    with pytest.raises(target.TargetError) as excinfo:
        target.fetch_pull_request("usaturn/nothing", 1, runner=FakeGh({}))

    assert error_code(excinfo) == target.EXIT_REPOSITORY_NOT_FOUND


@pytest.mark.parametrize("status", [401, 403])
def test_fetch_pull_request_auth(status: int) -> None:
    gh = FakeGh(
        {
            "repos/usaturn/xrift-sample-world/pulls/1": (
                http(status, {"message": "Bad credentials"}),
                "",
            )
        }
    )

    with pytest.raises(target.TargetError) as excinfo:
        target.fetch_pull_request("usaturn/xrift-sample-world", 1, runner=gh)

    assert error_code(excinfo) == target.EXIT_AUTH


def test_fetch_pull_request_without_status_is_api_failure() -> None:
    gh = FakeGh(
        {
            "repos/usaturn/xrift-sample-world/pulls/1": (
                "",
                "dial tcp: connection refused",
            )
        }
    )

    with pytest.raises(target.TargetError) as excinfo:
        target.fetch_pull_request("usaturn/xrift-sample-world", 1, runner=gh)

    assert error_code(excinfo) == target.EXIT_API
    assert "存在しません" not in str(excinfo.value)
    assert "connection refused" in str(excinfo.value)


def test_fetch_pull_request_broken_body() -> None:
    broken = pull("usaturn/xrift-sample-world", 1, "x")
    del broken["head"]["sha"]
    gh = FakeGh({"repos/usaturn/xrift-sample-world/pulls/1": (http(200, broken), "")})

    with pytest.raises(target.TargetError) as excinfo:
        target.fetch_pull_request("usaturn/xrift-sample-world", 1, runner=gh)

    assert error_code(excinfo) == target.EXIT_API


def test_fetch_pull_request_uses_the_canonical_name_after_a_rename() -> None:
    # gh は転送を内部で追い、改名後のリポジトリの応答だけを出す
    gh = FakeGh({"repos/usaturn/old/pulls/1": (http(200, pull("usaturn/renamed", 1, "x")), "")})

    assert target.fetch_pull_request("usaturn/old", 1, runner=gh).repository == "usaturn/renamed"


# resolve（pr-review）


def test_resolve_number_uses_parent_origin(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    gh = gh_for(pull("usaturn/xrift-poc", 4, "fix/x"))

    result = target.resolve("#4", parent_root=parent, runner=gh)

    assert gh.calls[0][3] == "repos/usaturn/xrift-poc/pulls/4"
    assert result["repository"] == "usaturn/xrift-poc"
    assert result["review_dir"] == str(parent / "reviews" / "usaturn" / "xrift-poc" / "#4-fix-x")
    assert result["target_root"] == str(parent)
    assert result["parent_root"] == str(parent)


def test_resolve_url_does_not_require_parent_relation(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    gh = gh_for(pull("usaturn/wabun-rst-ulint", 3, "hogehoge"))

    result = target.resolve(
        "https://github.com/usaturn/wabun-rst-ulint/pull/3",
        parent_root=parent,
        runner=gh,
    )

    assert result["review_dir"] == str(parent / "reviews" / "usaturn" / "wabun-rst-ulint" / "#3-hogehoge")
    assert result["target_root"] is None
    assert result["clone_url"] == "https://github.com/usaturn/wabun-rst-ulint.git"


def test_resolve_uses_canonical_repository_name(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    gh = FakeGh(
        {
            "repos/Usaturn/Xrift-Sample-World/pulls/3": (
                http(200, pull("usaturn/xrift-sample-world", 3, "feat/x")),
                "",
            )
        }
    )

    result = target.resolve(
        "https://github.com/Usaturn/Xrift-Sample-World/pull/3",
        parent_root=parent,
        runner=gh,
    )

    assert result["repository"] == "usaturn/xrift-sample-world"
    assert result["review_dir"] == str(parent / "reviews" / "usaturn" / "xrift-sample-world" / "#3-feat-x")


def test_resolve_fork_uses_base_repository(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    gh = gh_for(
        pull(
            "usaturn/xrift-sample-world",
            5,
            "patch",
            head_repository="someone/xrift-sample-world",
        )
    )

    result = target.resolve(
        "https://github.com/usaturn/xrift-sample-world/pull/5",
        parent_root=parent,
        runner=gh,
    )

    assert result["repository"] == "usaturn/xrift-sample-world"
    assert result["head_repository"] == "someone/xrift-sample-world"
    assert "usaturn" in str(result["review_dir"]).split("/reviews/")[1]


def test_resolve_stops_without_parent_origin(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent", origin=None)
    gh = gh_for(pull("usaturn/xrift-poc", 4, "x"))

    with pytest.raises(target.TargetError) as excinfo:
        target.resolve("4", parent_root=parent, runner=gh)

    assert error_code(excinfo) == target.EXIT_PARENT
    assert "PR URL" in str(excinfo.value)
    assert gh.calls == []


def test_require_parent_root_needs_ignored_reviews(tmp_path: Path) -> None:
    parent = make_repo(tmp_path / "parent")
    (parent / "README.md").write_text("x\n", encoding="utf-8")
    git(parent, "add", "README.md")
    git(parent, "commit", "-m", "x")

    with pytest.raises(target.TargetError) as excinfo:
        target.require_parent_root(parent)

    assert error_code(excinfo) == target.EXIT_PARENT


def test_require_parent_root_rejects_linked_worktree(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    linked = tmp_path / "linked"
    git(parent, "worktree", "add", "-q", "--detach", str(linked))

    with pytest.raises(target.TargetError) as excinfo:
        target.require_parent_root(linked)

    assert error_code(excinfo) == target.EXIT_PARENT


# Git 操作の対象


def add_submodule(parent: Path, tmp_path: Path, url: str, *, origin: str | None = None) -> Path:
    source = make_repo(tmp_path / "public-source")
    (source / "README.md").write_text("public\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "public")
    subprocess.run(
        [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "public",
        ],
        check=True,
        capture_output=True,
    )
    git(parent, "config", "-f", ".gitmodules", "submodule.public.url", url)
    git(parent / "public", "config", "remote.origin.url", origin or url)
    git(parent, "add", ".gitmodules", "public")
    git(parent, "commit", "-m", "public")
    return parent / "public"


def test_target_checkout_uses_parent_and_registered_submodule(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    public = add_submodule(parent, tmp_path, "https://github.com/usaturn/xrift-sample-world.git")

    assert target.target_checkout(parent, "usaturn/xrift-poc") == parent
    assert target.target_checkout(parent, "Usaturn/Xrift-Sample-World") == public.resolve()
    assert target.target_checkout(parent, "usaturn/wabun-rst-ulint") is None


def test_target_checkout_stops_for_broken_submodule(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    add_submodule(
        parent,
        tmp_path,
        "https://github.com/usaturn/xrift-sample-world.git",
        origin="https://github.com/x/y.git",
    )

    with pytest.raises(target.TargetError) as excinfo:
        target.target_checkout(parent, "usaturn/xrift-sample-world")

    assert error_code(excinfo) == target.EXIT_PARENT


# レビューディレクトリの探索（review-fix-planning・review-verify）


def review_dir(parent: Path, *parts: str) -> Path:
    directory = parent.joinpath("reviews", *parts)
    directory.mkdir(parents=True)
    (directory / "model.md").write_text("review\n", encoding="utf-8")
    return directory


def test_find_review_directory_by_number(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    directory = review_dir(parent, "usaturn", "xrift-poc", "#4-feat-x")

    result = target.find_review_directory(
        "#4",
        parent_root=parent,
        for_planning=True,
        runner=gh_for(pull("usaturn/xrift-poc", 4, "feat/x")),
    )

    assert result["review_dir"] == str(directory)
    assert result["target_root"] == str(parent)


def test_find_review_directory_reports_missing_review_and_legacy_dir(
    tmp_path: Path,
) -> None:
    parent = make_parent(tmp_path / "parent")
    legacy = review_dir(parent, "#4-feat-x")

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            "4",
            parent_root=parent,
            for_planning=True,
            runner=gh_for(pull("usaturn/xrift-poc", 4, "feat/x")),
        )

    assert error_code(excinfo) == target.EXIT_NO_REVIEW
    assert "存在しますが" in str(excinfo.value)
    assert str(legacy) in str(excinfo.value)


def test_find_review_directory_does_not_cross_repositories(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    review_dir(parent, "usaturn", "xrift-sample-world", "#4-feat-x")

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            "#4",
            parent_root=parent,
            for_planning=True,
            runner=gh_for(pull("usaturn/xrift-poc", 4, "feat/x")),
        )

    assert error_code(excinfo) == target.EXIT_NO_REVIEW


def test_find_review_directory_lists_multiple_candidates(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    first = review_dir(parent, "usaturn", "xrift-poc", "#4-a")
    second = review_dir(parent, "usaturn", "xrift-poc", "#4-b")

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            "#4",
            parent_root=parent,
            for_planning=False,
            runner=gh_for(pull("usaturn/xrift-poc", 4, "b")),
        )

    assert error_code(excinfo) == target.EXIT_AMBIGUOUS
    assert str(first) in str(excinfo.value) and str(second) in str(excinfo.value)


def test_find_review_directory_rejects_other_owner_before_api(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    gh = gh_for(pull("someone/x", 1, "y"))

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            "https://github.com/someone/x/pull/1",
            parent_root=parent,
            for_planning=True,
            runner=gh,
        )

    assert error_code(excinfo) == target.EXIT_OWNER
    assert "対象外" in str(excinfo.value)
    assert gh.calls == []


def test_find_review_directory_without_planning_accepts_other_owner(
    tmp_path: Path,
) -> None:
    parent = make_parent(tmp_path / "parent")
    directory = review_dir(parent, "someone", "x", "#1-y")

    result = target.find_review_directory(
        "https://github.com/someone/x/pull/1",
        parent_root=parent,
        for_planning=False,
        runner=gh_for(pull("someone/x", 1, "y")),
    )

    assert result["review_dir"] == str(directory)


def test_find_review_directory_accepts_legacy_path(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    legacy = review_dir(parent, "#4-feat-x")
    gh = gh_for(pull("usaturn/xrift-poc", 4, "feat/x"))

    result = target.find_review_directory(str(legacy), parent_root=parent, for_planning=True, runner=gh)

    assert result["review_dir"] == str(legacy)
    assert gh.calls[0][3] == "repos/usaturn/xrift-poc/pulls/4"


def test_find_review_directory_accepts_legacy_submodule_path(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    public = add_submodule(parent, tmp_path, "https://github.com/usaturn/xrift-sample-world.git")
    legacy = public / "reviews" / "#12-feat-x"
    legacy.mkdir(parents=True)
    gh = gh_for(pull("usaturn/xrift-sample-world", 12, "feat/x"))

    result = target.find_review_directory(str(legacy), parent_root=parent, for_planning=True, runner=gh)

    assert result["review_dir"] == str(legacy.resolve())
    assert result["target_root"] == str(public.resolve())


def test_find_review_directory_rejects_name_mismatch(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    directory = review_dir(parent, "usaturn", "xrift-poc", "#4-old-name")

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            str(directory),
            parent_root=parent,
            for_planning=True,
            runner=gh_for(pull("usaturn/xrift-poc", 4, "feat/x")),
        )

    assert error_code(excinfo) == target.EXIT_PATH


def write_findings(directory: Path, repository: str, number: int, name: str = "m-0123456789ab.json") -> None:
    findings = directory / "findings"
    findings.mkdir(parents=True, exist_ok=True)
    document = {"pr": {"repository": repository, "number": number}}
    (findings / name).write_text(json.dumps(document), encoding="utf-8")


def test_find_review_directory_from_findings_json(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    elsewhere = tmp_path / "elsewhere"
    write_findings(elsewhere, "usaturn/xrift-sample-world", 7)
    gh = gh_for(pull("usaturn/xrift-sample-world", 7, "z"))

    result = target.find_review_directory(str(elsewhere), parent_root=parent, for_planning=True, runner=gh)

    assert result["review_dir"] == str(elsewhere.resolve())
    assert gh.calls[0][3] == "repos/usaturn/xrift-sample-world/pulls/7"


def test_find_review_directory_rejects_findings_for_another_pr(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    directory = review_dir(parent, "usaturn", "xrift-poc", "#4-feat-x")
    write_findings(directory, "usaturn/xrift-sample-world", 4)

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(
            str(directory),
            parent_root=parent,
            for_planning=True,
            runner=gh_for(pull("usaturn/xrift-poc", 4, "feat/x")),
        )

    assert error_code(excinfo) == target.EXIT_PATH


def test_find_review_directory_rejects_unidentifiable_path(tmp_path: Path) -> None:
    parent = make_parent(tmp_path / "parent")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(str(elsewhere), parent_root=parent, for_planning=True, runner=FakeGh({}))

    assert error_code(excinfo) == target.EXIT_PATH


def test_find_review_directory_rejects_other_owner_path_before_api(
    tmp_path: Path,
) -> None:
    parent = make_parent(tmp_path / "parent")
    directory = review_dir(parent, "someone", "x", "#1-y")
    gh = FakeGh({})

    with pytest.raises(target.TargetError) as excinfo:
        target.find_review_directory(str(directory), parent_root=parent, for_planning=True, runner=gh)

    assert error_code(excinfo) == target.EXIT_OWNER
    assert gh.calls == []


# 一時 clone


def clone_target(head_sha: str, *, head_repository: str | None = None) -> Any:
    return target.CloneTarget.from_json(
        {
            "repository": "usaturn/wabun-rst-ulint",
            "number": 3,
            "head_ref": "hogehoge",
            "head_sha": head_sha,
            "head_repository": head_repository or "usaturn/wabun-rst-ulint",
            "clone_url": "https://github.com/usaturn/wabun-rst-ulint.git",
        }
    )


def test_clone_for_review_fetches_pull_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, head = make_remote(tmp_path)
    redirect(monkeypatch, remote, "https://github.com/usaturn/wabun-rst-ulint.git")

    root = target.clone_repository(clone_target(head), tmp_path / "clone", checkout_branch=False)

    assert git(root, "rev-parse", "refs/pr-review/3") == head
    assert git(root, "config", "--get", "remote.origin.url") == "https://github.com/usaturn/wabun-rst-ulint.git"
    assert sorted(entry.name for entry in root.iterdir()) == [".git"]
    assert git(root, "rev-parse", "origin/main")


@pytest.mark.parametrize("head_moved", [False, True])
def test_fetch_pull_head_preserves_tracking_refs_tags_and_fetch_head(tmp_path: Path, head_moved: bool) -> None:
    remote, head = make_remote(tmp_path)
    checkout = tmp_path / "source"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            "--no-tags",
            str(remote),
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )
    git(
        checkout,
        "config",
        "--add",
        "remote.origin.fetch",
        "+refs/pull/*/head:refs/remotes/origin/pr-*",
    )
    git(checkout, "config", "remote.origin.tagOpt", "--tags")
    git(remote, "update-ref", "refs/tags/published-after-clone", head)
    fetch_head = checkout / ".git" / "FETCH_HEAD"
    fetch_head.write_text("preserve caller fetch result\n", encoding="utf-8")
    before = git(
        checkout,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
    )
    if head_moved:
        with pytest.raises(target.TargetError):
            target.fetch_pull_head(checkout, 3, "b" * 40)
    else:
        ref = target.fetch_pull_head(checkout, 3, head)
        assert git(checkout, "rev-parse", ref) == head
    assert (
        git(
            checkout,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/remotes",
            "refs/tags",
        )
        == before
    )
    assert fetch_head.read_text(encoding="utf-8") == "preserve caller fetch result\n"


def test_fetch_pull_head_stops_when_head_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, _head = make_remote(tmp_path)
    redirect(monkeypatch, remote, "https://github.com/usaturn/wabun-rst-ulint.git")

    with pytest.raises(target.TargetError) as excinfo:
        target.clone_repository(clone_target("b" * 40), tmp_path / "clone", checkout_branch=False)

    assert error_code(excinfo) == target.EXIT_CLONE
    assert "変わりました" in str(excinfo.value)


def test_clone_for_fix_checks_out_head_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, head = make_remote(tmp_path)
    redirect(monkeypatch, remote, "https://github.com/usaturn/wabun-rst-ulint.git")

    root = target.clone_repository(clone_target(head), tmp_path / "clone", checkout_branch=True)

    assert git(root, "branch", "--show-current") == "hogehoge"
    assert git(root, "rev-parse", "HEAD") == head


def test_clone_for_fix_rejects_fork(tmp_path: Path) -> None:
    with pytest.raises(target.TargetError) as excinfo:
        target.clone_repository(
            clone_target("a" * 40, head_repository="someone/wabun-rst-ulint"),
            tmp_path / "clone",
            checkout_branch=True,
        )

    assert error_code(excinfo) == target.EXIT_CLONE
    assert not (tmp_path / "clone").exists()


def test_clone_rejects_existing_destination_and_foreign_url(tmp_path: Path) -> None:
    existing = tmp_path / "clone"
    existing.mkdir()
    with pytest.raises(target.TargetError) as excinfo:
        target.clone_repository(clone_target("a" * 40), existing, checkout_branch=False)
    assert error_code(excinfo) == target.EXIT_USAGE

    with pytest.raises(target.TargetError) as excinfo:
        target.CloneTarget.from_json(
            {
                "repository": "usaturn/wabun-rst-ulint",
                "number": 3,
                "head_ref": "hogehoge",
                "head_sha": "a" * 40,
                "head_repository": "usaturn/wabun-rst-ulint",
                "clone_url": "https://github.com/attacker/other.git",
            }
        )
    assert error_code(excinfo) == target.EXIT_USAGE


def clone_target_with_url(clone_url: str) -> Any:
    return target.CloneTarget.from_json(
        {
            "repository": "someone/my-world",
            "number": 3,
            "head_ref": "hogehoge",
            "head_sha": "a" * 40,
            "head_repository": "someone/my-world",
            "clone_url": clone_url,
        }
    )


def test_clone_target_accepts_only_canonical_urls() -> None:
    # clone_url はそのまま git clone に渡すので、平文の http や認証情報付きの URL は受け付けない
    for url in (
        "https://github.com/someone/my-world.git",
        "git@github.com:someone/my-world.git",
    ):
        assert clone_target_with_url(url).clone_url == url
    for url in LENIENT_GITHUB_URLS:
        with pytest.raises(target.TargetError) as excinfo:
            clone_target_with_url(url)
        assert error_code(excinfo) == target.EXIT_USAGE


# CLI


def test_cli_reports_error_code_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = make_parent(tmp_path / "parent")
    monkeypatch.setattr(target, "default_parent_root", lambda: parent)

    code = target.main(
        ["review-dir", "https://github.com/someone/x/pull/1", "--for-planning"],
        runner=FakeGh({}),
    )

    assert code == target.EXIT_OWNER
    assert "対象外" in capsys.readouterr().err


def test_cli_resolve_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = make_parent(tmp_path / "parent")
    monkeypatch.setattr(target, "default_parent_root", lambda: parent)

    code = target.main(["resolve", "#4"], runner=gh_for(pull("usaturn/xrift-poc", 4, "fix/x")))

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["review_dir"].endswith("/reviews/usaturn/xrift-poc/#4-fix-x")


# push 前の PR・checkout の照合。照合の削除や base/head の取り違えを検出する。


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


@pytest.mark.parametrize("detached", [False, True])
def test_scan_context_accepts_descendant_and_uses_pr_base(tmp_path: Path, detached: bool) -> None:
    root, value, pr = scan_checkout(tmp_path)
    if detached:
        git(root, "checkout", "--detach")
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


@pytest.mark.parametrize(
    "case",
    ["closed", "fork", "deleted-head", "head-sha", "head-ref", "base-ref", "renamed"],
)
def test_scan_context_rejects_changed_or_unpushable_pr(tmp_path: Path, case: str) -> None:
    root, value, pr = scan_checkout(tmp_path)
    if case == "closed":
        pr["state"] = "closed"
    elif case == "fork":
        pr["head"]["repo"]["full_name"] = "someone/fork"
    elif case == "deleted-head":
        pr["head"]["repo"] = None
    elif case == "head-sha":
        pr["head"]["sha"] = "b" * 40
    elif case == "head-ref":
        pr["head"]["ref"] = "other"
    elif case == "base-ref":
        pr["base"]["ref"] = "other"
    else:
        pr["base"]["repo"]["full_name"] = "usaturn/renamed"
    gh = FakeGh({"repos/usaturn/xrift-sample-world/pulls/7": (http(200, pr), "")})

    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=gh)
    assert error_code(excinfo) == target.EXIT_CLONE


@pytest.mark.parametrize(
    "case",
    [
        "origin",
        "no-origin",
        "many-origins",
        "pushurl",
        "many-pushurls",
        "subdir",
        "history",
    ],
)
def test_scan_context_rejects_wrong_checkout_or_destination(tmp_path: Path, case: str) -> None:
    root, value, pr = scan_checkout(tmp_path)
    checkout = root
    if case == "origin":
        git(root, "remote", "set-url", "origin", "https://github.com/usaturn/other.git")
    elif case == "no-origin":
        git(root, "remote", "remove", "origin")
    elif case == "many-origins":
        git(
            root,
            "config",
            "--add",
            "remote.origin.url",
            "https://github.com/usaturn/other.git",
        )
    elif case == "pushurl":
        git(
            root,
            "config",
            "remote.origin.pushurl",
            "https://github.com/usaturn/other.git",
        )
    elif case == "many-pushurls":
        for _ in range(2):
            git(root, "config", "--add", "remote.origin.pushurl", value["clone_url"])
    elif case == "subdir":
        checkout = root / "nested"
        checkout.mkdir()
    else:
        git(root, "checkout", "--orphan", "other")
        git(root, "commit", "-m", "unrelated")

    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, checkout, runner=gh_for(pr))
    assert error_code(excinfo) == target.EXIT_CLONE


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("base_ref", None),
        ("base_ref", "bad..ref"),
        ("head_ref", "bad ref"),
        ("number", 0),
        ("repository", "../x"),
        ("clone_url", "https://github.com/usaturn/other.git"),
    ],
)
def test_scan_context_rejects_malformed_target(tmp_path: Path, key: str, replacement: Any) -> None:
    root, value, pr = scan_checkout(tmp_path)
    value[key] = replacement
    with pytest.raises(target.TargetError) as excinfo:
        target.scan_context(value, root, runner=gh_for(pr))
    assert error_code(excinfo) == target.EXIT_USAGE


def test_scan_context_owner_and_api_failures_are_not_approval(tmp_path: Path) -> None:
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


def test_cli_scan_context_prints_checked_refs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, value, pr = scan_checkout(tmp_path)
    assert (
        target.main(
            ["scan-context", "--target", json.dumps(value), "--checkout", str(root)],
            runner=gh_for(pr),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["base_ref"] == "integration/world"
