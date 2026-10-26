from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result

SCRIPT = Path(__file__).parents[1] / "scripts" / "review_run.py"
SPEC = importlib.util.spec_from_file_location("review_run", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
review_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_run)
REPOSITORY = "example/syncer"
LOOP_SCRIPT = Path(__file__).parents[2] / "pr-review-fix-loop" / "scripts" / "loop.py"


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


ACCEPTED_BODY = "日本語の説明\n複数行の本文\nCloses #42\nother/repo#333333 は別リポジトリの裸参照\n"
ISSUE_42 = {"body": "受け入れ条件\n第二行", "comments": [{"body": "承認済み"}]}


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


# --- Review history の HEAD（保存と PR 修正ループの照合） ---------------------------------------------------------


def load_loop() -> Any:
    """PR 修正ループの helper。pydantic のモデルを解決できるよう、一意の名前で sys.modules に登録する。"""
    spec = importlib.util.spec_from_file_location("pr_review_tests_loop", LOOP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_history_head(result: dict[str, Any], value: str) -> None:
    """mark_reviewed が書いた Review history の行の HEAD を value にする。"""
    draft = Path(result["draft"])
    text = draft.read_text(encoding="utf-8")
    row = f"| `{result['reviewed_head'][:12]}` | Initial |"
    assert text.count(row) == 1
    draft.write_text(text.replace(row, f"| `{value}` | Initial |"), encoding="utf-8")


def test_complete_saves_a_longer_prefix_of_the_reviewed_head_that_the_loop_accepts(prepared: dict[str, Any]) -> None:
    """complete と loop で Review history の HEAD の規則が違うと、complete が success を返したレビューを loop が
    却下し、レビュアーは誤りを知らないまま試行を終える（#103。PR #102 では同じ commit の 13 桁で起きた）。"""
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    head = result["reviewed_head"]
    write_history_head(result, head[:13])

    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success", completed["reason"]

    loop = load_loop()
    attempt = Attempt(
        agent_id="fixture-reviewer",
        state="success",
        identity=Identity(model="observed-model"),
        exit_code=0,
        elapsed_seconds=1.0,
        stop_confirmed=True,
        work_dir=prepared["caller"],
        request_path=prepared["request_path"],
        payload=completed,
    )
    runner_result = Result(
        request_id="request-7",
        role="inspect",
        requested_agent="fixture-reviewer",
        actual_agent="fixture-reviewer",
        state="success",
        attempts=[attempt],
    )
    pr = loop.PrRef(repository=REPOSITORY, number=7, url=f"https://github.com/{REPOSITORY}/pull/7")
    entry = loop.verify_reviewer(runner_result, pr=pr, head=head, review_dir=Path(completed["review_file"]).parent)
    assert entry.verdict == "accepted", entry.reason


def test_complete_refuses_a_review_history_head_of_another_commit(prepared: dict[str, Any]) -> None:
    """別の commit の HEAD を書いたレビューを complete が保存して success を返すと、loop が却下するまで
    レビュアーは誤りを知らない（#103）。"""
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    write_history_head(result, git(prepared["source"], "rev-parse", "main")[:12])

    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))

    assert completed["status"] == "failed"
    assert "Review history" in completed["reason"]
    assert completed["review_file"] is None


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
