from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result
from tools.test_support.dsh import write_session

SCRIPT = Path(__file__).parents[1] / "scripts" / "review_run.py"
SPEC = importlib.util.spec_from_file_location("review_run", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
review_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_run)
REPOSITORY = "example/syncer"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    parent, source, remote, caller = (tmp_path / name for name in ("parent", "source", "remote.git", "caller"))
    for checkout in (parent, source):
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        git(checkout, "config", "user.name", "Test")
        git(checkout, "config", "user.email", "test@example.com")
        (checkout / ".gitignore").write_text("/reviews/\n", encoding="utf-8")
        git(checkout, "add", ".gitignore")
        git(checkout, "commit", "-m", "base")
    git(parent, "remote", "add", "origin", "https://github.com/usaturn/xrift-poc.git")
    git(source, "switch", "-c", "feature")
    (source / "README.md").write_text("a harmless fixture\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "PR change")
    head = git(source, "rev-parse", "HEAD")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(source, "push", str(remote), "main", "feature", "feature:refs/pull/7/head")
    git(source, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"https://github.com/{REPOSITORY}.git")
    target = review_run.workspace.load_review_target()

    def live_pr(repository: str, number: int, *, runner: Any = None) -> Any:
        assert (repository, number) == (REPOSITORY, 7)
        return target.PullRequest(
            repository,
            number,
            f"https://github.com/{repository}/pull/{number}",
            "feature",
            head,
            repository,
            "main",
            "open",
            f"https://github.com/{repository}.git",
        )

    def parent_root() -> Path:
        return parent

    monkeypatch.setattr(target, "fetch_pull_request", live_pr)
    monkeypatch.setattr(target, "default_parent_root", parent_root)
    caller.mkdir()
    monkeypatch.chdir(caller)
    request = Request(
        run_id="run-568",
        request_id="request-7",
        agent_id="fixture-reviewer",
        role="inspect",
        work_dir=caller,
        prompt="review",
        deadline_seconds=600.0,
        result_shape="json",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    comments = tmp_path / "comments.json"
    comments.write_text("[]", encoding="utf-8")
    argv = [
        "prepare",
        "--pr",
        f"https://github.com/{REPOSITORY}/pull/7",
        "--runtime",
        "claude-code",
        "--model-name",
        "observed-model",
        "--request",
        str(request_path),
        "--source-root",
        str(source),
        "--records-comments",
        str(comments),
        "--temp-root",
        str(tmp_path / "sessions"),
    ]
    args = review_run.parser().parse_args(argv)
    return {
        "args": args,
        "argv": argv,
        "parent": parent,
        "source": source,
        "remote": remote,
        "head": head,
        "request_path": request_path,
        "comments": comments,
        "caller": caller,
    }


def parent_input(prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    args = review_run.parser().parse_args(
        [
            "prepare-parent",
            "--pr",
            prepared["args"].pr,
            "--source-root",
            str(prepared["source"]),
            "--records-comments",
            str(prepared["comments"]),
            "--output-dir",
            str(tmp_path / "parent-input"),
        ]
    )
    args.requirements = {"pr": {"body": "日本語のPR本文\n", "closingIssuesReferences": []}, "issues": []}
    descriptor = review_run.prepare_parent(args)
    request = Request.model_validate_json(prepared["request_path"].read_text())
    request.prompt = "日本語の依頼\n```json\n" + json.dumps({"parent_pr_data": descriptor}) + "\n```\n"
    prepared["request_path"].write_text(request.model_dump_json())
    prepared["args"].runtime = "dsh"
    prepared["args"].model_name = None
    prepared["args"].temp_root = str(prepared["request_path"].parent / "dsh" / "tmp")
    home = tmp_path / "identity"
    write_session(home, prepared["caller"], provider="commandcode", model="actual/model")
    for key, value in {
        "DSH_HOME": str(home),
        "DSH_SESSION_ID": "session-test",
        "DSH_SHELL": "1",
        "DSH_PROFILE": "headless",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return descriptor


def staged_batch(prepared: dict[str, Any], staged: dict[str, Any]) -> BatchResult:
    request = Request.model_validate_json(prepared["request_path"].read_text())
    return BatchResult(
        run_id=request.run_id,
        state="success",
        assignments={"inspect": [request.agent_id]},
        results=[
            Result(
                request_id=request.request_id,
                role="inspect",
                requested_agent=request.agent_id,
                actual_agent=request.agent_id,
                state="success",
                attempts=[
                    Attempt(
                        agent_id=request.agent_id,
                        state="success",
                        identity=Identity(
                            provider="commandcode", model="actual/model", effort="high", session_id="session-test"
                        ),
                        exit_code=0,
                        elapsed_seconds=1.0,
                        stop_confirmed=True,
                        work_dir=prepared["caller"],
                        request_path=prepared["request_path"],
                        payload=staged,
                    )
                ],
            )
        ],
        outside_changes=[],
        elapsed_seconds=1.0,
        artifact_dir=prepared["request_path"].parent,
    )


def test_parent_bundle_child_needs_no_github_and_parent_saves(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DSH の子の差分の起点（base）は親の snapshot から来る。誤ると子は誤った範囲を黙ってレビューする。"""
    parent_input(prepared, tmp_path, monkeypatch)
    target = review_run.workspace.load_review_target()
    live = target.fetch_pull_request

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("child must not query GitHub")

    monkeypatch.setattr(target, "fetch_pull_request", unavailable)
    monkeypatch.setattr(review_run, "command", unavailable)
    # Even removing the PR's remote ref must not break the local bundled input.
    git(prepared["remote"], "update-ref", "-d", "refs/pull/7/head")
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared", result.get("reason")
    assert result["base"] == git(prepared["source"], "rev-parse", "main")
    mark_reviewed(result)
    staged = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert staged["status"] == "staged", staged
    assert not Path(result["destination"]).exists()
    assert Path(result["worktree"]).exists()
    monkeypatch.setattr(target, "fetch_pull_request", live)
    batch = staged_batch(prepared, staged)
    review_run.finalize_parent_batch(batch)
    completed = batch.results[0].attempts[0].payload
    assert isinstance(completed, dict) and completed["status"] == "success", completed
    assert completed["cleanup"] == "success"
    assert Path(str(completed["review_file"])).is_file()
    assert Path(str(completed["findings_file"])).is_file()
    assert not Path(result["worktree"]).exists()


def fake_requirements_gh(pr: Any, issues: dict[tuple[str, str], Any]) -> tuple[Any, list[list[str]]]:
    """parent_requirements の gh。値が例外なら送出し、文字列ならそのまま、それ以外は JSON にして返す。"""
    calls: list[list[str]] = []

    def answer(value: Any) -> str:
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)

    def github(argv: list[str]) -> str:
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return answer(pr)
        if argv[:3] == ["gh", "issue", "view"]:
            return answer(issues[(argv[argv.index("--repo") + 1], argv[3])])
        raise AssertionError(argv)

    return github, calls


ACCEPTED_BODY = "日本語の説明\n複数行の本文\nCloses #42\nother/repo#333333 は別リポジトリの裸参照\n"
ISSUE_42 = {"body": "受け入れ条件\n第二行", "comments": [{"body": "承認済み"}]}


@pytest.mark.parametrize(
    ("addition", "failing", "answer"),
    [
        (
            "背景色は `#333333` にする\n",
            (REPOSITORY, "333333"),
            review_run.workspace.WorkspaceError("gh failed: GraphQL: Could not resolve (token ghp_secretvalue)"),
        ),
        (
            "参照: https://github.com/usaturn/private-nonexistent-repo/issues/1\n",
            ("usaturn/private-nonexistent-repo", "1"),
            review_run.workspace.WorkspaceError("gh failed: GraphQL: Could not resolve to a Repository"),
        ),
    ],
    ids=["color", "inaccessible-url"],
)
def test_parent_requirements_unavailable_issue_does_not_abort(
    monkeypatch: pytest.MonkeyPatch, addition: str, failing: tuple[str, str], answer: Any
) -> None:
    """本文の `#N`・Issue の URL を落とすと子の要件が黙って欠け、gh の出力（token を含みうる）が子へ渡る。"""
    github, calls = fake_requirements_gh(
        {"body": ACCEPTED_BODY + addition, "closingIssuesReferences": []},
        {(REPOSITORY, "42"): ISSUE_42, failing: answer},
    )
    monkeypatch.setattr(review_run, "command", github)
    requirements = review_run.parent_requirements(f"https://github.com/{REPOSITORY}/pull/7", REPOSITORY, 7)
    assert requirements["pr"]["body"] == ACCEPTED_BODY + addition
    assert requirements["issues"] == [{"repository": REPOSITORY, "number": 42, **ISSUE_42}]
    [unavailable] = requirements["unavailable"]
    assert (unavailable["repository"], str(unavailable["number"])) == failing
    assert isinstance(unavailable["reason"], str) and unavailable["reason"]
    # gh の出力は理由に写さない（秘密値が混ざりうる）
    assert "ghp_secretvalue" not in json.dumps(requirements)
    # 別リポジトリの裸参照 owner/repo#N は取得しない
    assert sorted(call[3] for call in calls if call[:3] == ["gh", "issue", "view"]) == sorted(["42", failing[1]])


def test_parent_requirements_empty_nullable_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文が null（GitHub の実入力）でも closing の参照を落とすと、閉じる Issue の条件が子から黙って抜ける。"""
    closing = [{"number": 42, "url": f"https://github.com/{REPOSITORY}/issues/42"}]
    github, _calls = fake_requirements_gh(
        {"body": None, "closingIssuesReferences": closing},
        {(REPOSITORY, "42"): {"body": None, "comments": [{"body": "コメント"}]}},
    )
    monkeypatch.setattr(review_run, "command", github)
    requirements = review_run.parent_requirements(f"https://github.com/{REPOSITORY}/pull/7", REPOSITORY, 7)
    assert requirements["pr"] == {"body": "", "closingIssuesReferences": closing}
    assert requirements["issues"] == [
        {"repository": REPOSITORY, "number": 42, "body": "", "comments": [{"body": "コメント"}]}
    ]
    assert requirements["unavailable"] == []


@pytest.mark.parametrize("changed", ["draft", "stop", "identity"])
def test_parent_finalize_refuses_stale_or_untrusted_staging(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """停止を確かめていない子・staged の後の draft・名乗りと違うモデルのレビューを、親が黙って保存する。"""
    parent_input(prepared, tmp_path, monkeypatch)
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared", result.get("reason")
    mark_reviewed(result)
    staged = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert staged["status"] == "staged"
    batch = staged_batch(prepared, staged)
    if changed == "draft":
        Path(result["draft"]).write_text("changed after staging")
    elif changed == "stop":
        batch.results[0].attempts[0].stop_confirmed = False
    else:
        batch.results[0].attempts[0].identity.model = "other-model"
    review_run.finalize_parent_batch(batch)
    payload = batch.results[0].attempts[0].payload
    assert isinstance(payload, dict) and payload["status"] == "failed", payload
    assert not Path(result["destination"]).exists()
    assert Path(result["worktree"]).exists()


def mark_reviewed(result: dict[str, Any]) -> None:
    draft = Path(result["draft"])
    draft.write_text(
        f"- 対象: PR #7 / head {result['reviewed_head'][:12]} / 2026-10-08\n"
        "- レビュアー: observed-model（ランタイム: claude-code）\n\n"
        "## Review history\n\n| Reviewed at (UTC) | HEAD | Mode |\n|---|---|---|\n"
        f"| 2026-10-08T00:00:00Z | `{result['reviewed_head'][:12]}` | Initial |\n\n"
        "## Summary\n\n全体差分を確認。activeな指摘なし。\n",
        encoding="utf-8",
    )
    findings = Path(result["findings_draft"])
    document = json.loads(findings.read_text(encoding="utf-8"))
    document["coverage"]["read"] = ["PR全体差分とREADME.md全文"]
    findings.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_dsh_prepare_complete_records_live_identity(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sandbox の子が共有の checkout の Git を書き換え、親の値で保存先（モデル名）を黙って誤る。"""
    provider = "opencode-go"
    parent_input(prepared, tmp_path, monkeypatch)
    home = tmp_path / "dsh"
    write_session(home, prepared["caller"], provider=provider, model="actual/model")
    for key, value in {
        "DSH_HOME": str(home),
        "DSH_SESSION_ID": "session-test",
        "DSH_PROFILE": "headless",
        "DSH_SHELL": "1",
        "PI_PROVIDER": "parent",
        "PI_MODEL": "wrong-parent",
    }.items():
        monkeypatch.setenv(key, value)
    prepared["args"].runtime = "dsh"
    prepared["args"].model_name = None
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared"
    # A sandboxed reviewer must not mutate the shared source's Git directory.
    state = json.loads(Path(result["state_file"]).read_text())
    assert state["target_kind"] == "clone"
    assert Path(state["target_root"]).is_relative_to(Path(result["state_file"]).parent)
    reviewer = result["reviewer"]
    assert reviewer["model"] == "actual/model"
    assert reviewer["runtime"] == "dsh"
    assert reviewer["provenance"]["provider"] == provider
    assert reviewer["provenance"]["effort"] == "high"
    assert reviewer["provenance"]["session_id"] == "session-test"
    assert reviewer["review_file"] == f"{provider}-actual-model.md"
    mark_reviewed(result)
    staged = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert staged["status"] == "staged", staged
    batch = staged_batch(prepared, staged)
    batch.results[0].attempts[0].identity.provider = provider
    review_run.finalize_parent_batch(batch)
    completed = batch.results[0].attempts[0].payload
    assert isinstance(completed, dict) and completed["status"] == "success", completed
    assert completed["cleanup"] == "success"
    document = json.loads(Path(str(completed["findings_file"])).read_text())
    assert document["reviewer"] == reviewer
    execution_record = json.loads(Path(str(completed["run_record"])).read_text())
    assert execution_record["reviewer"]["model"] == "actual/model"


def test_prepare_binds_request_and_local_records(prepared: dict[str, Any]) -> None:
    """レビュアーがそのまま実行する diff_command が全体差分を指さないと、一部の差分だけを読んで黙って終える。"""
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared"
    assert result["base"] == git(prepared["source"], "rev-parse", "main")
    assert result["diff_command"][-1] == f"{result['base']}..{prepared['head']}"


def configure_base_fetch(
    prepared: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    relation: str,
    route: str,
    local_commit: bool,
) -> tuple[Path, str, str]:
    source, remote = prepared["source"], prepared["remote"]
    base = git(source, "rev-parse", "main")
    if relation != "ahead":
        git(source, "switch", "-c", "remote-base", "main")
        (source / "base.txt").write_text("remote base advanced\n", encoding="utf-8")
        git(source, "add", "base.txt")
        git(source, "commit", "-m", "advance remote base")
        git(source, "push", str(remote), "HEAD:main")
        git(source, "tag", "remote-base-only")
        git(source, "push", str(remote), "refs/tags/remote-base-only")
        git(source, "tag", "-d", "remote-base-only")
        git(source, "switch", "feature")
    remote_base = git(remote, "rev-parse", "refs/heads/main")
    if relation != "missing":
        tracked = {"ahead": prepared["head"], "behind": base, "diverged": prepared["head"], "equal": remote_base}
        git(source, "update-ref", "refs/remotes/origin/main", tracked[relation])
    if local_commit:
        (source / "local.txt").write_text("not pushed\n", encoding="utf-8")
        git(source, "add", "local.txt")
        git(source, "commit", "-m", "local review target")
        prepared["args"].review_commit = git(source, "rev-parse", "HEAD")
        absent = subprocess.run(
            ["git", "-C", str(remote), "cat-file", "-e", prepared["args"].review_commit], capture_output=True
        )
        assert absent.returncode != 0
    if route == "registered":
        target = review_run.workspace.load_review_target()
        original = target.resolve

        def resolve(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {**original(*args, **kwargs), "target_root": str(source)}

        monkeypatch.setattr(target, "resolve", resolve)
        prepared["args"].source_root = None
    sibling = tmp_path / "sibling worktree"
    git(source, "worktree", "add", "--detach", str(sibling), prepared["head"])
    git(source, "update-ref", "refs/worktree/pr-review-base", prepared["head"])
    git(sibling, "update-ref", "refs/worktree/pr-review-base", prepared["head"])
    return sibling, base, remote_base


def shared_refs(root: Path) -> str:
    return git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes", "refs/tags")


@pytest.mark.parametrize(
    ("relation", "route", "local_commit"),
    [("diverged", "registered", True), ("ahead", "explicit", False)],
    ids=["diverged-registered-unpushed-commit", "ahead-explicit-pr-head"],
)
def test_prepare_preserves_shared_refs_and_other_worktree_base_refs(
    prepared: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
    route: str,
    local_commit: bool,
) -> None:
    """利用者の branch・追跡 ref・tag と他の worktree の ref を書き換え、古い追跡 ref で差分の範囲を黙ってずらす。"""
    sibling, base, remote_base = configure_base_fetch(
        prepared, tmp_path, monkeypatch, relation=relation, route=route, local_commit=local_commit
    )
    source = prepared["source"]
    before = shared_refs(source)
    snapshot = review_run.workspace.snapshot(source)
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared"
    assert shared_refs(source) == shared_refs(sibling) == before
    assert review_run.workspace.snapshot(source) == snapshot
    assert result["base"] == base
    assert git(Path(result["worktree"]), "rev-parse", "refs/worktree/pr-review-base") == remote_base
    for checkout in (source, sibling):
        assert git(checkout, "rev-parse", "refs/worktree/pr-review-base") == prepared["head"]
    expected = {"README.md", "local.txt"} if local_commit else {"README.md"}
    assert (
        set(git(Path(result["worktree"]), "diff", "--name-only", f"{base}..{result['reviewed_head']}").splitlines())
        == expected
    )
    mark_reviewed(result)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success" and completed["cleanup"] == "success"
    assert shared_refs(source) == shared_refs(sibling) == before
    assert not Path(result["state_file"]).parent.exists()


def test_prepare_returns_skip_decisions_through_ordinary_comments(prepared: dict[str, Any], tmp_path: Path) -> None:
    """prepare が判断の記録を渡さないと、Skip・Fix の判断が黙って抜け、決定済みの指摘を新しい指摘として出し直す。"""
    builder = review_run.workspace._load_script(
        "fixture_builder",
        SCRIPT.parents[1] / "evals/build_fixture.py",
    )
    built = builder.build("update-decided", tmp_path / "fixture")
    prepared["comments"].write_bytes(Path(built["pr_comments"]).read_bytes())
    result = review_run.prepare(prepared["args"])
    assert {item["state"] for item in result["records"]["decisions"]} == {"fix", "skip"}


@pytest.mark.parametrize("failure", ["reviewer", "coverage"])
def test_complete_preserves_failure_as_failed_result(
    prepared: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """読んでいない雛形を指摘 0 件として保存し、prepare で固定した欄（loop は見ない）の書き換えを黙って保存する。"""
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    findings = Path(result["findings_draft"])
    document = json.loads(findings.read_text(encoding="utf-8"))
    if failure == "reviewer":
        document["reviewer"]["provenance"]["agent_id"] = "another-agent"
    else:
        document["coverage"]["read"] = []
    findings.write_text(json.dumps(document), encoding="utf-8")
    assert review_run.main(["complete", "--state", result["state_file"]]) != 0
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert failed["retained_paths"]["worktree"] == result["worktree"]
    assert failed["retained_paths"]["draft"] == result["draft"]
    assert Path(result["state_file"]).exists()


def test_complete_reports_saved_but_uncleaned_result_as_failure(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存の後の後片付けの失敗を success と返すと、loop が受け付け、残したセッションも報告されない。"""
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)

    def cleanup_failure(_args: argparse.Namespace) -> None:
        raise review_run.workspace.WorkspaceError("cleanup refused")

    monkeypatch.setattr(review_run.workspace, "cleanup", cleanup_failure)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "failed"
    assert completed["cleanup"] == "failed"
    assert completed["reason"] == "cleanup refused"
    assert completed["retained_paths"]["worktree"] == result["worktree"]
    assert Path(completed["review_file"]).is_file()
    assert Path(completed["findings_file"]).is_file()
    assert Path(result["state_file"]).exists()


def run_records(parent: Path) -> list[Path]:
    return sorted((parent / "reviews").glob("*/*/#*/runs/*.json"))


def test_symlinked_review_directory_refuses_to_start(prepared: dict[str, Any], tmp_path: Path) -> None:
    """レビューのディレクトリが symlink なら、実行記録とレビューを reviews/ の外へ黙って書く。"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    reviews = prepared["parent"] / "reviews" / "example" / "syncer"
    reviews.mkdir(parents=True)
    (reviews / "#7-feature").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(review_run.workspace.WorkspaceError):
        review_run.prepare(prepared["args"])
    assert list(elsewhere.iterdir()) == []
    assert not (tmp_path / "sessions").exists() or list((tmp_path / "sessions").iterdir()) == []


# --- ランナーの観測値（attach-runner） ------------------------------------------------------------------------------


def batch_result(path: Path) -> Path:
    agent_id = "fixture-reviewer"
    attempt = Attempt(
        agent_id=agent_id,
        state="success",
        identity=Identity(model="observed-model", effort="high", session_id="session-1", cli_version="9.9.9"),
        exit_code=0,
        elapsed_seconds=12.5,
        stop_confirmed=True,
    )
    batch = BatchResult(
        run_id="run-568",
        state="success",
        assignments={"inspect": [agent_id]},
        results=[
            Result(
                request_id="request-7",
                role="inspect",
                requested_agent=agent_id,
                actual_agent=agent_id,
                state="success",
                attempts=[attempt],
            )
        ],
        outside_changes=[],
        elapsed_seconds=13.0,
        artifact_dir=path.parent,
    )
    path.write_text(batch.model_dump_json(), encoding="utf-8")
    return path


def attach(source: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = review_run.main(["attach-runner", "--result", str(source)])
    return code, json.loads(capsys.readouterr().out)


def completed_review(prepared: dict[str, Any]) -> dict[str, Any]:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    assert review_run.complete(argparse.Namespace(state=result["state_file"]))["status"] == "success"
    return result


def test_attach_runner_skips_files_outside_the_record_layout(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """symlink の先（reviews/ の外）・形式の違う他の文書・他の PR の記録に、runner 欄を黙って書き込む。"""
    completed_review(prepared)
    original = run_records(prepared["parent"])[0]
    content = original.read_bytes()
    original.unlink()
    # symlink の階層の下・形式の違う JSON・ディレクトリと PR 番号の合わない記録は、どれも書き先にしない
    linked = prepared["parent"] / "reviews" / "example" / "linked"
    linked.mkdir(parents=True)
    (linked / original.parent.parent.name).symlink_to(original.parent.parent, target_is_directory=True)
    (original.parent / "notes.json").write_text(json.dumps({"kind": "planning"}), encoding="utf-8")
    other_runs = linked.parent / "syncer" / "#8-other" / "runs"
    other_runs.mkdir(parents=True)
    (other_runs / original.name).write_bytes(content)
    code, payload = attach(batch_result(tmp_path / "result.json"), capsys)
    assert code == 0 and payload["attached"] == [] and payload["skipped"] == 3
    assert json.loads((other_runs / original.name).read_text(encoding="utf-8"))["runner"] is None
