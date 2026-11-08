from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import hooks
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
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> dict[str, Any]:
    pr_number = getattr(request, "param", 7)
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
    git(source, "push", str(remote), "main", "feature", f"feature:refs/pull/{pr_number}/head")
    git(source, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"https://github.com/{REPOSITORY}.git")
    target = review_run.workspace.load_review_target()

    def live_pr(repository: str, number: int, *, runner: Any = None) -> Any:
        assert (repository, number) == (REPOSITORY, pr_number)
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
    run_request = Request(
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
    request_path.write_text(run_request.model_dump_json(), encoding="utf-8")
    comments = tmp_path / "comments.json"
    comments.write_text("[]", encoding="utf-8")
    argv = [
        "prepare",
        "--pr",
        f"https://github.com/{REPOSITORY}/pull/{pr_number}",
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


def batch_result(
    path: Path,
    *,
    state: Any = "success",
    effort: str | None = "high",
    request_path: Path | None = None,
) -> Path:
    agent_id = "fixture-reviewer"
    attempt = Attempt(
        agent_id=agent_id,
        state=state,
        identity=Identity(model="observed-model", effort=effort, session_id="session-1", cli_version="9.9.9"),
        exit_code=0,
        elapsed_seconds=12.5,
        stop_confirmed=True,
        request_path=request_path,
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
                state=state,
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
    # 有効な外部記録を同じ repository/PR の runs symlink から参照させる。
    # 正常な original も残し、常に拒否する実装では通らない対照にする。
    elsewhere = tmp_path / "external-records"
    elsewhere.mkdir()
    external_record = elsewhere / original.name
    external_record.write_bytes(content)
    linked = original.parent.parent.with_name("#7-linked")
    linked.mkdir()
    (linked / "runs").symlink_to(elsewhere, target_is_directory=True)
    (original.parent / "notes.json").write_text(json.dumps({"kind": "planning"}), encoding="utf-8")
    other_runs = linked.parent / "#8-other" / "runs"
    other_runs.mkdir(parents=True)
    (other_runs / original.name).write_bytes(content)
    code, payload = attach(batch_result(tmp_path / "result.json"), capsys)
    assert external_record.read_bytes() == content
    assert code == 0 and payload["attached"] == [str(original)] and payload["skipped"] == 3
    assert json.loads(original.read_text(encoding="utf-8"))["runner"] is not None
    assert (other_runs / original.name).read_bytes() == content


# --- 停止確認済みで未着手の失敗セッションの退役 -------------------------------------------------------------------


def stopped_review(prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    result = review_run.prepare(prepared["args"])
    code, _payload = attach(batch_result(tmp_path / "stopped.json", state="awaiting_authentication"), capsys)
    assert code == 0
    return result


def failed_cleanup_args(result: dict[str, Any], **kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(state=result["state_file"], strict_reap=True, allow_failed=True, **kwargs)


def registrations(prepared: dict[str, Any]) -> str:
    return git(prepared["source"], "worktree", "list", "--porcelain")


def test_failed_retirement_removes_only_the_stopped_pristine_session(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """退役の分岐が無いと、停止した未着手のセッションの worktree の登録と pinned ref が黙って残り続ける
    （拒否のテストの対照。実行記録を書き換えず、保存先も作らないことも確かめる）。"""
    result = stopped_review(prepared, tmp_path, capsys)
    findings = Path(result["findings_draft"])
    findings.write_text(json.dumps(json.loads(findings.read_text()), indent=4), encoding="utf-8")
    record = Path(result["run_record"])
    before = record.read_bytes()
    cleaned = review_run.workspace.cleanup(failed_cleanup_args(result))
    assert cleaned["retirement"] == "failed"
    assert cleaned["stop_evidence"]["source"] == "runner"
    assert not Path(result["state_file"]).parent.exists()
    assert result["worktree"] not in registrations(prepared)
    assert not git(prepared["source"], "for-each-ref", "refs/pr-review/7")
    assert record.read_bytes() == before
    assert not (prepared["parent"] / result["destination"]).exists()


def test_failed_retirement_keeps_edited_candidates(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """scaffold との完全一致を見ないと、timing など編集した候補を持つセッションを理由付き再判定で黙って消す。"""
    result = stopped_review(prepared, tmp_path, capsys)
    findings = Path(result["findings_draft"])
    document = json.loads(findings.read_text())
    document["timing"]["waiting_seconds"] = 3
    findings.write_text(json.dumps(document), encoding="utf-8")
    before = findings.read_bytes()
    with pytest.raises(review_run.workspace.WorkspaceError):
        review_run.workspace.cleanup(failed_cleanup_args(result, operator_confirmed_stopped="停止を確認した"))
    assert findings.read_bytes() == before
    assert result["worktree"] in registrations(prepared)


@pytest.mark.parametrize("prepared", [1], indirect=True)
def test_failed_retirement_keeps_candidates_with_boolean_pr_number(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Python では True == 1 なので、pr.number を True に書き換えた候補を scaffold と一致と見なして黙って消す。"""
    result = stopped_review(prepared, tmp_path, capsys)
    findings = Path(result["findings_draft"])
    document = json.loads(findings.read_text(encoding="utf-8"))
    document["pr"]["number"] = True
    findings.write_text(json.dumps(document), encoding="utf-8")
    before = findings.read_bytes()
    before_registrations = registrations(prepared)
    refs = git(prepared["source"], "for-each-ref", "refs/pr-review/")
    with pytest.raises(review_run.workspace.WorkspaceError):
        review_run.workspace.cleanup(failed_cleanup_args(result))
    assert findings.read_bytes() == before
    assert Path(result["state_file"]).is_file()
    assert registrations(prepared) == before_registrations
    assert git(prepared["source"], "for-each-ref", "refs/pr-review/") == refs


@pytest.mark.parametrize("artifact", ["draft", "previous", "unknown", "dirty", "symlink"])
def test_failed_retirement_keeps_unsaved_session_data(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], artifact: str
) -> None:
    """draft・前回のレビュー・未知のファイル・未 commit の変更・symlink の候補を失敗の退役が見逃すと、
    成果のあるセッションを理由付き再判定でも黙って消す。"""
    earlier_review: Path | None = None
    if artifact == "previous":
        # state["previous_review"] が入るセッションにするため、保存先にレビューを置いてから prepare し直す。
        first = review_run.prepare(prepared["args"])
        saved_earlier = prepared["parent"] / first["destination"]
        saved_earlier.parent.mkdir(parents=True, exist_ok=True)
        saved_earlier.write_text("earlier review", encoding="utf-8")
        earlier_review = saved_earlier
        review_run.workspace.cleanup(failed_cleanup_args(first, operator_confirmed_stopped="停止を確認した"))
        assert not Path(first["state_file"]).parent.exists()
    result = stopped_review(prepared, tmp_path, capsys)
    session = Path(result["state_file"]).parent
    if artifact == "draft":
        retained = Path(result["draft"])
        retained.write_text("finished review", encoding="utf-8")
        # 保存先は作らない（未保存の draft）。保存先と一致する draft は保存済みとして消えるのが正しい。
    elif artifact == "previous":
        assert json.loads(Path(result["state_file"]).read_text())["previous_review"] is not None
        retained = session / "review-backup" / "previous-review.md"
        assert retained.read_text(encoding="utf-8") == "earlier review"
    elif artifact == "dirty":
        retained = Path(result["worktree"]) / "README.md"
        retained.write_text("unsaved edit", encoding="utf-8")
    elif artifact == "symlink":
        retained = Path(result["findings_draft"])
        elsewhere = tmp_path / "candidates.json"
        retained.rename(elsewhere)
        retained.symlink_to(elsewhere)
    else:
        retained = session / "notes.txt"
        retained.write_text("unfinished investigation", encoding="utf-8")
    before = retained.read_bytes()
    with pytest.raises(review_run.workspace.WorkspaceError):
        review_run.workspace.cleanup(failed_cleanup_args(result, operator_confirmed_stopped="子プロセスの終了を確認"))
    assert retained.read_bytes() == before
    if earlier_review is not None:
        assert earlier_review.read_bytes() == b"earlier review"
    assert Path(result["state_file"]).exists()
    assert result["worktree"] in registrations(prepared)


@pytest.mark.parametrize(
    "evidence", ["none", "not-stopped", "success", "other-binding", "other-session", "other-head", "symlink"]
)
def test_failed_retirement_requires_its_own_stopped_failed_runner(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], evidence: str
) -> None:
    """runner の停止確認・失敗・自分の記録を確かめずにモデルの fail や別の記録を信じて生きたセッションを消す。"""
    result = stopped_review(prepared, tmp_path, capsys)
    path = Path(result["run_record"])
    document = json.loads(path.read_text())
    if evidence == "none":
        review_run.fail_review(prepared["parent"], review_run.workspace.load_state(result["state_file"])[1], "stopped")
        document = json.loads(path.read_text())
        document["runner"] = None
    elif evidence == "not-stopped":
        document["runner"]["attempt"]["stop_confirmed"] = False
    elif evidence == "success":
        document["runner"]["attempt"]["state"] = "success"
    elif evidence == "other-binding":
        document["reviewer"]["provenance"]["request_id"] = "another-request"
    elif evidence == "other-head":
        document["pr_head"] = "f" * 40
    elif evidence == "other-session":
        state_file = Path(result["state_file"])
        state = json.loads(state_file.read_text())
        other = path.with_name(path.stem[:-8] + "deadbeef.json")
        other.write_bytes(path.read_bytes())
        state["run_context"]["run_record"] = str(other.relative_to(prepared["parent"]))
        state_file.write_text(json.dumps(state), encoding="utf-8")
    else:
        elsewhere = tmp_path / "run.json"
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
    if evidence != "symlink":
        path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(review_run.workspace.WorkspaceError):
        review_run.workspace.cleanup(failed_cleanup_args(result))
    assert Path(result["state_file"]).exists()
    assert Path(result["findings_draft"]).exists()
    assert result["worktree"] in registrations(prepared)


def test_failed_retirement_operator_reason_only_replaces_stop_evidence(prepared: dict[str, Any]) -> None:
    """利用者の理由付き確認が runner の無いセッションを退役できないと放棄したセッションが残り続け、
    逆に理由が他の検査まで免除すると成果のあるセッションを消す（上の拒否のテストの対照）。"""
    result = review_run.prepare(prepared["args"])
    reason = "子と孫の終了を確認した。\n担当者: テスト操作員"
    cleaned = review_run.workspace.cleanup(failed_cleanup_args(result, operator_confirmed_stopped=reason))
    assert cleaned["stop_evidence"] == {"source": "operator", "reason": reason}
    assert cleaned["session_dir"] == str(Path(result["state_file"]).parent)
    assert not Path(result["worktree"]).exists()


@pytest.mark.parametrize("registration", ["removed", "prunable", "locked", "wrong-path"])
def test_failed_retirement_only_accepts_absent_worktree_without_registration(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], registration: str
) -> None:
    """worktree のディレクトリが無いことだけで退役すると、locked・prunable・別のパスの Git の登録を黙って落とす。"""
    result = stopped_review(prepared, tmp_path, capsys)
    worktree = Path(result["worktree"])
    if registration in {"removed", "wrong-path"}:
        git(prepared["source"], "worktree", "remove", str(worktree))
    else:
        if registration == "locked":
            git(prepared["source"], "worktree", "lock", str(worktree))
        shutil.rmtree(worktree)
    if registration == "wrong-path":
        state_file = Path(result["state_file"])
        state = json.loads(state_file.read_text())
        state["worktree"] = str(worktree.parent / "other-worktree")
        state_file.write_text(json.dumps(state), encoding="utf-8")
    if registration == "removed":
        review_run.workspace.cleanup(failed_cleanup_args(result))
        assert not Path(result["state_file"]).parent.exists()
    else:
        before = registrations(prepared)
        with pytest.raises(review_run.workspace.WorkspaceError):
            review_run.workspace.cleanup(failed_cleanup_args(result))
        assert Path(result["findings_draft"]).is_file()
        assert registrations(prepared) == before


@pytest.mark.parametrize("cleanup_mode", ["failed", "saved", "saved-strict"])
def test_cleanup_keeps_session_clone_used_by_another_worktree(
    prepared: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    cleanup_mode: str,
) -> None:
    """一時 clone を別の worktree が使っているのに消すと、その worktree だけにある commit と index を黙って失う。"""
    prepared["args"].source_root = None
    cleanup = review_run.workspace.cleanup
    if cleanup_mode == "failed":
        result = stopped_review(prepared, tmp_path, capsys)
        args = failed_cleanup_args(result)
    else:
        result = review_run.prepare(prepared["args"])
        mark_reviewed(result)

        def cleanup_failure(_args: argparse.Namespace) -> None:
            raise review_run.workspace.WorkspaceError("deferred")

        with monkeypatch.context() as patch:
            patch.setattr(review_run.workspace, "cleanup", cleanup_failure)
            assert review_run.complete(argparse.Namespace(state=result["state_file"]))["status"] == "failed"
        args = argparse.Namespace(state=result["state_file"], strict_reap=cleanup_mode == "saved-strict")
    session = Path(result["state_file"]).parent
    clone = session / "repo"
    elsewhere = tmp_path / "moved worktree\n調査中"
    if cleanup_mode == "failed":
        git(clone, "worktree", "move", result["worktree"], str(elsewhere))
    else:
        git(clone, "worktree", "add", "--detach", str(elsewhere), prepared["head"])
    readme = elsewhere / "README.md"
    readme.write_text("unpublished investigation\n", encoding="utf-8")
    git(elsewhere, "add", "README.md")
    git(elsewhere, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "local investigation")
    local_commit = git(elsewhere, "rev-parse", "HEAD")
    readme.write_text("staged follow-up\n", encoding="utf-8")
    git(elsewhere, "add", "README.md")
    staged = git(elsewhere, "diff", "--cached")
    before_registrations = git(clone, "worktree", "list", "--porcelain")
    refs = git(clone, "for-each-ref")
    retained = {Path(result[key]): Path(result[key]).read_bytes() for key in ("state_file", "findings_draft")}

    with pytest.raises(review_run.workspace.WorkspaceError):
        cleanup(args)
    assert {path: path.read_bytes() for path in retained} == retained
    assert git(clone, "worktree", "list", "--porcelain") == before_registrations
    assert git(clone, "for-each-ref") == refs
    assert git(elsewhere, "rev-parse", "HEAD") == local_commit
    assert git(elsewhere, "diff", "--cached") == staged
    assert readme.read_text(encoding="utf-8") == "staged follow-up\n"
    with pytest.raises(review_run.workspace.WorkspaceError):
        review_run.workspace.check_reap_candidate(args)


# --- after_run による退役（試行の tmp の下だけを探す） -------------------------------------------------------------


def after_run_attach(source: Path, parent: Path, tmp_path: Path) -> dict[str, Any]:
    """実際の hook と CLI を、試行の tmp とも prepare の既定とも別の TMPDIR で実行する。"""
    launcher = (
        "import importlib.util, os, sys; from pathlib import Path; "
        f"os.environ['TMPDIR'] = {str(tmp_path / 'hook-tmp')!r}; "
        f"spec = importlib.util.spec_from_file_location('hook_review_run', {str(SCRIPT)!r}); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        f"module.workspace.load_review_target().default_parent_root = lambda: Path({str(parent)!r}); "
        "raise SystemExit(module.main(sys.argv[1:]))"
    )
    command = [sys.executable, "-c", launcher, "attach-runner", "--result", "{result}"]
    reports = hooks.run_after_run([command], result_path=source, cwd=parent, cancel=threading.Event())
    assert reports[0]["state"] == "success", Path(str(reports[0]["stderr_path"])).read_text()
    return json.loads(Path(str(reports[0]["stdout_path"])).read_text())


def test_after_run_retires_only_its_stopped_pristine_session_in_the_attempt_tmp(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    """探す場所を試行の tmp にしないと担当のセッションが残り続け、payload を信じると別のセッションを消す。"""
    attempt_dir = tmp_path / "artifacts" / "request-7" / "attempt-1"
    attempt_tmp = attempt_dir / "tmp"
    attempt_tmp.mkdir(parents=True)
    prepared["args"].temp_root = str(attempt_tmp)
    result = review_run.prepare(prepared["args"])
    request = json.loads(prepared["request_path"].read_text())
    request["request_id"] = "not-this-attempt"
    prepared["request_path"].write_text(json.dumps(request), encoding="utf-8")
    sibling = review_run.prepare(prepared["args"])
    source = batch_result(
        tmp_path / "result.json", state="awaiting_authentication", request_path=attempt_dir / "request.json"
    )
    payload = json.loads(source.read_text())
    payload["results"][0]["attempts"][0]["payload"] = {
        "state_file": sibling["state_file"],
        "session_dir": str(Path(sibling["state_file"]).parent),
    }
    source.write_text(json.dumps(payload), encoding="utf-8")
    attached = after_run_attach(source, prepared["parent"], tmp_path)
    assert not Path(result["state_file"]).parent.exists()
    assert result["worktree"] not in registrations(prepared)
    assert Path(sibling["state_file"]).exists()
    assert sibling["worktree"] in registrations(prepared)
    record = json.loads(Path(result["run_record"]).read_text())
    assert record["runner"]["attempt"]["stop_confirmed"] is True
    assert record["status"] == "started" and record["outputs"] is None
    assert any(item["session_dir"] == str(Path(result["state_file"]).parent) for item in attached["retired"])
    assert not (prepared["parent"] / result["destination"]).exists()
    # 2 回目の hook は記録を変えず、別のセッションも消さない。
    before = Path(result["run_record"]).read_bytes()
    after_run_attach(source, prepared["parent"], tmp_path)
    assert Path(result["run_record"]).read_bytes() == before
    assert Path(sibling["state_file"]).exists()


def test_attach_runner_conflict_inhibits_all_retirement(prepared: dict[str, Any], tmp_path: Path) -> None:
    """衝突した attach の後でも、保存済みの runner を根拠に退役すると照合していない値で消す。"""
    first = review_run.prepare(prepared["args"])
    second = review_run.prepare(prepared["args"])
    original = batch_result(tmp_path / "first.json", state="timed_out", effort=None)
    # セッションの無い場所を渡して、どちらも退役させずに診断だけを書く。
    review_run.record.attach_runner(original, prepared["parent"], temp_root=tmp_path / "no-sessions-here")
    second_record = Path(second["run_record"])
    document = json.loads(second_record.read_text())
    document["runner"] = None
    second_record.write_text(json.dumps(document), encoding="utf-8")
    before = {path: path.read_bytes() for path in run_records(prepared["parent"])}
    attached = review_run.record.attach_runner(
        batch_result(tmp_path / "conflicting.json", state="timed_out", effort="high"),
        prepared["parent"],
        temp_root=Path(prepared["args"].temp_root),
    )
    assert attached["status"] == "failed" and attached["conflicts"]
    assert attached["retired"] == [] and attached["retained"]
    for result in (first, second):
        assert Path(result["state_file"]).exists()
        assert result["worktree"] in registrations(prepared)
    assert {path: path.read_bytes() for path in before} == before


def test_attach_runner_requires_session_to_point_back_to_attached_record(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    """同じ末尾の別の記録の停止を根拠にすると、今回照合していない記録の停止でセッションを消す。"""
    result = review_run.prepare(prepared["args"])
    state_path = Path(result["state_file"])
    state = json.loads(state_path.read_text())
    original = Path(result["run_record"])
    document = json.loads(original.read_text())
    stamp = "2026-10-01T00:00:00Z"
    renamed = original.with_name(stamp.replace("-", "").replace(":", "") + "-" + original.name.partition("-")[2])
    document["timing"]["started_at"] = stamp
    state["run_context"]["started_at"] = stamp
    state["run_context"]["run_record"] = renamed.relative_to(prepared["parent"]).as_posix()
    candidates = Path(result["findings_draft"])
    scaffold = json.loads(candidates.read_text())
    scaffold["timing"].update(started_at=stamp, finished_at=stamp)
    document["reviewer"]["provenance"]["request_id"] = "not-this-attempt"
    state["run_context"]["binding"]["request_id"] = "not-this-attempt"
    state["run_context"]["reviewer"]["provenance"]["request_id"] = "not-this-attempt"
    state["run_context"]["immutable"]["reviewer"]["provenance"]["request_id"] = "not-this-attempt"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    scaffold["reviewer"]["provenance"]["request_id"] = "not-this-attempt"
    candidates.write_text(json.dumps(scaffold), encoding="utf-8")
    source = batch_result(tmp_path / "result.json", state="timed_out")
    attempt = BatchResult.model_validate_json(source.read_text()).results[0].attempts[0]
    document["runner"] = review_run.record.runner_block(attempt)
    renamed.write_text(json.dumps(document), encoding="utf-8")
    # 逆照合が無ければ、helper は別の記録の証拠で退役を認める。
    assert review_run.workspace.check_reap_candidate(failed_cleanup_args(result))["retirement"] == "failed"
    attached = review_run.record.attach_runner(source, prepared["parent"], temp_root=Path(prepared["args"].temp_root))
    assert attached["status"] == "success" and attached["attached"] == [str(original)]
    assert attached["retired"] == [] and attached["retained"][0]["reason"]
    assert state_path.exists() and candidates.exists()
    assert result["worktree"] in registrations(prepared)
