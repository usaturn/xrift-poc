from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner.config import load_config
from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result

SCRIPT = Path(__file__).parents[1] / "scripts" / "review_run.py"
ROOT = Path(__file__).resolve().parents[4]
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
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared"
    assert (result["run_id"], result["request_id"], result["agent_id"], result["role"]) == (
        "run-568",
        "request-7",
        "fixture-reviewer",
        "inspect",
    )
    assert result["pr_head"] == result["reviewed_head"] == prepared["head"]
    assert result["records"]["decisions"] == []
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


@pytest.mark.parametrize("relation", ["ahead", "behind", "diverged", "equal", "missing"])
@pytest.mark.parametrize("route", ["explicit", "registered"])
@pytest.mark.parametrize("local_commit", [False, True], ids=["pr-head", "unpushed-commit"])
def test_prepare_preserves_shared_refs_and_other_worktree_base_refs(
    prepared: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
    route: str,
    local_commit: bool,
) -> None:
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


@pytest.mark.parametrize("route", ["explicit", "registered"])
def test_prepare_failure_after_base_fetch_keeps_shared_refs_unchanged(
    prepared: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    sibling, _base, _remote_base = configure_base_fetch(
        prepared, tmp_path, monkeypatch, relation="diverged", route=route, local_commit=True
    )
    before = shared_refs(prepared["source"])
    original = review_run.workspace.git

    def fail_merge_base(root: Path, *args: str, **kwargs: Any) -> Any:
        if args[0] == "merge-base":
            raise review_run.workspace.WorkspaceError("merge-base refused")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(review_run.workspace, "git", fail_merge_base)
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "failed" and result["reason"] == "merge-base refused"
    assert shared_refs(prepared["source"]) == shared_refs(sibling) == before
    assert Path(result["state_file"]).is_file()


def test_prepare_clone_keeps_base_ref_private_and_completes(prepared: dict[str, Any]) -> None:
    prepared["args"].source_root = None
    result = review_run.prepare(prepared["args"])
    assert result["status"] == "prepared"
    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert state["target_kind"] == "clone"
    worktree, clone = Path(result["worktree"]), Path(state["target_root"])
    assert git(worktree, "rev-parse", "refs/worktree/pr-review-base") == result["base"]
    assert git(clone, "for-each-ref", "refs/worktree/pr-review-base") == ""
    mark_reviewed(result)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success" and completed["cleanup"] == "success"
    assert not clone.exists()


def test_prepare_parallel_sessions_pin_their_own_base(prepared: dict[str, Any]) -> None:
    first = review_run.prepare(prepared["args"])
    source, remote = prepared["source"], prepared["remote"]
    git(source, "push", str(remote), "feature:main")
    second = review_run.prepare(prepared["args"])
    assert first["status"] == second["status"] == "prepared"
    assert first["base"] != second["base"]
    assert git(Path(first["worktree"]), "rev-parse", "refs/worktree/pr-review-base") == first["base"]
    assert git(Path(second["worktree"]), "rev-parse", "refs/worktree/pr-review-base") == second["base"]
    mark_reviewed(first)
    assert review_run.complete(argparse.Namespace(state=first["state_file"]))["cleanup"] == "success"
    assert Path(second["state_file"]).exists()
    assert git(Path(second["worktree"]), "rev-parse", "refs/worktree/pr-review-base") == second["base"]


def test_prepare_returns_skip_decisions_through_ordinary_comments(prepared: dict[str, Any], tmp_path: Path) -> None:
    builder = review_run.workspace._load_script(
        "fixture_builder",
        SCRIPT.parents[1] / "evals/build_fixture.py",
    )
    built = builder.build("update-decided", tmp_path / "fixture")
    prepared["comments"].write_bytes(Path(built["pr_comments"]).read_bytes())
    result = review_run.prepare(prepared["args"])
    assert {item["state"] for item in result["records"]["decisions"]} == {"fix", "skip"}


def test_complete_saves_zero_findings_with_provenance(prepared: dict[str, Any]) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success"
    assert completed["active_findings"] == 0
    assert completed["request_id"] == "request-7"
    assert completed["cleanup"] == "success"
    assert completed["cleanup_details"]["pr_ref_status"] == "deleted"
    document = json.loads(Path(completed["findings_file"]).read_text(encoding="utf-8"))
    assert document["schema"] == "review-findings/v2"
    assert document["reviewer"]["provenance"]["agent_id"] == "fixture-reviewer"
    assert document["reviewer"]["provenance"]["provider"] == "unknown"
    assert document["reviewer"]["provenance"]["run_id"] == "run-568"
    assert document["timing"]["waiting_seconds"] is None
    assert document["timing"]["seconds"] > 0
    markdown = Path(completed["review_file"]).read_text(encoding="utf-8")
    execution = json.loads(markdown.split("<!-- pr-review:execution -->\n```json\n", 1)[1].split("\n```", 1)[0])
    assert execution["timing"] == document["timing"]
    assert execution["reviewer"] == document["reviewer"]
    assert not Path(result["state_file"]).parent.exists()


@pytest.mark.parametrize(
    "failure", ["missing", "invalid", "binding", "reviewer", "coverage", "coverage_kind", "destination", "head", "api"]
)
def test_complete_preserves_failure_as_failed_result(
    prepared: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    if failure == "missing":
        Path(result["draft"]).unlink()
    elif failure == "invalid":
        Path(result["findings_draft"]).write_text("invalid JSON", encoding="utf-8")
    elif failure == "binding":
        request = json.loads(prepared["request_path"].read_text(encoding="utf-8"))
        request["request_id"] = "another-request"
        prepared["request_path"].write_text(json.dumps(request), encoding="utf-8")
    elif failure in {"reviewer", "coverage", "coverage_kind"}:
        findings = Path(result["findings_draft"])
        document = json.loads(findings.read_text(encoding="utf-8"))
        if failure == "reviewer":
            document["reviewer"]["provenance"]["agent_id"] = "another-agent"
        elif failure == "coverage_kind":
            document["coverage"] = ["README.md"]
        else:
            document["coverage"]["read"] = []
        findings.write_text(json.dumps(document), encoding="utf-8")
    elif failure == "destination":
        destination = prepared["parent"] / result["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("another review", encoding="utf-8")
    else:

        def moved(_repository: str, _number: int) -> str:
            if failure == "api":
                raise review_run.workspace.WorkspaceError("API unavailable")
            return "f" * 40

        monkeypatch.setattr(review_run.workspace, "current_pr_head", moved)
    assert review_run.main(["complete", "--state", result["state_file"]]) != 0
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert failed["request_id"] == "request-7"
    assert failed["active_findings"] is None
    assert failed["reason"]
    assert failed["retained_paths"]["worktree"] == result["worktree"]
    assert failed["retained_paths"]["draft"] == result["draft"]
    assert Path(result["state_file"]).exists()


@pytest.mark.parametrize("field", ["work_dir", "result_shape"])
def test_prepare_rejects_wrong_request_workdir_and_shape(prepared: dict[str, Any], field: str) -> None:
    value = json.loads(prepared["request_path"].read_text(encoding="utf-8"))
    value[field] = str(prepared["source"]) if field == "work_dir" else "text"
    prepared["request_path"].write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(review_run.workspace.WorkspaceError, match="work_dir|result_shape"):
        review_run.prepare(prepared["args"])


def test_prepare_nonrunner_marks_unobserved_identity_unknown(prepared: dict[str, Any]) -> None:
    prepared["args"].request = None
    result = review_run.prepare(prepared["args"])
    assert result["run_id"] == result["request_id"] == result["agent_id"] == result["role"] == "unknown"
    document = json.loads(Path(result["findings_draft"]).read_text(encoding="utf-8"))
    provenance = document["reviewer"]["provenance"]
    assert provenance["provider"] == provenance["effort"] == provenance["cli_version"] == "unknown"


def test_complete_reports_saved_but_uncleaned_result_as_failure(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_prepare_base_fetch_failure_returns_correlated_retained_paths(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = review_run.workspace.git

    def fail_base_fetch(root: Path, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("fetch", "--quiet") and "--no-write-fetch-head" in args:
            raise review_run.workspace.WorkspaceError("base fetch failed")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(review_run.workspace, "git", fail_base_fetch)
    assert review_run.main(prepared["argv"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed" and failed["request_id"] == "request-7"
    assert failed["repository"] == REPOSITORY and failed["pr_head"] == prepared["head"]
    assert failed["active_findings"] is None and failed["reason"] == "base fetch failed"
    assert Path(failed["state_file"]).is_file()
    assert Path(failed["retained_paths"]["worktree"]).is_dir()


def test_prepare_fails_without_observed_model(prepared: dict[str, Any]) -> None:
    prepared["args"].model_name = None
    with pytest.raises(review_run.workspace.WorkspaceError, match="model"):
        review_run.prepare(prepared["args"])


def test_prepare_pi_requires_both_current_environment_identifiers(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared["args"].runtime = "pi"
    monkeypatch.setenv("PI_MODEL", "observed-model")
    monkeypatch.delenv("PI_PROVIDER", raising=False)
    with pytest.raises(review_run.workspace.WorkspaceError, match="PI_PROVIDER"):
        review_run.prepare(prepared["args"])


# --- 実行記録（Issue #601） ---------------------------------------------------------------------------------------


def run_records(parent: Path) -> list[Path]:
    return sorted((parent / "reviews").glob("*/*/#*/runs/*.json"))


def only_record(parent: Path) -> dict[str, Any]:
    paths = run_records(parent)
    assert len(paths) == 1, paths
    return review_run.record.findings.validate_document(json.loads(paths[0].read_text(encoding="utf-8")))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_success_record_matches_saved_outputs_and_candidates(prepared: dict[str, Any]) -> None:
    result = review_run.prepare(prepared["args"])
    assert Path(result["run_record"]) == run_records(prepared["parent"])[0]
    assert only_record(prepared["parent"])["status"] == "started"
    mark_reviewed(result)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success" and completed["run_record"] == result["run_record"]
    record = only_record(prepared["parent"])
    candidates = json.loads(Path(completed["findings_file"]).read_text(encoding="utf-8"))
    assert record["status"] == "success" and record["mode"] == "Initial" and record["review_kind"] == "pr_head"
    assert record["reviewer"] == {key: candidates["reviewer"][key] for key in ("model", "runtime", "provenance")}
    assert record["timing"]["started_at"] == candidates["timing"]["started_at"]
    assert record["base"] == candidates["base"] and record["reviewed_head"] == candidates["reviewed_head"]
    assert record["outputs"] == {
        "review_file": Path(completed["review_file"]).name,
        "review_sha256": sha256(Path(completed["review_file"])),
        "findings_file": "findings/" + Path(completed["findings_file"]).name,
        "findings_sha256": sha256(Path(completed["findings_file"])),
        "active_findings": 0,
        "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        "unverified": 0,
    }
    assert record["inputs"]["request"] == {"sha256": sha256(prepared["request_path"]), "deadline_seconds": 600.0}
    assert record["inputs"]["previous_review"] is None
    assert record["inputs"]["records"]["decisions"] == {}
    assert record["inputs"]["diff"] == {"files": 1, "added": 1, "deleted": 0, "binary": 0}
    assert all(record["timing"][key] is not None for key in review_run.record.findings.RUN_STAGE_SECONDS)
    suffix = Path(result["state_file"]).parent.name.rsplit("-", 1)[1]
    assert Path(result["run_record"]).name.endswith(f"-observed-model-{suffix}.json")


def test_second_review_records_update_mode_and_previous_digest(prepared: dict[str, Any]) -> None:
    first = review_run.prepare(prepared["args"])
    mark_reviewed(first)
    saved = review_run.complete(argparse.Namespace(state=first["state_file"]))
    second = review_run.prepare(prepared["args"])
    document = json.loads(Path(second["run_record"]).read_text(encoding="utf-8"))
    assert document["mode"] == "Update"
    assert document["inputs"]["previous_review"] == {"sha256": sha256(Path(saved["review_file"]))}
    assert len(run_records(prepared["parent"])) == 2


def test_prepare_without_request_records_null_request(prepared: dict[str, Any]) -> None:
    prepared["args"].request = None
    result = review_run.prepare(prepared["args"])
    document = json.loads(Path(result["run_record"]).read_text(encoding="utf-8"))
    assert document["inputs"]["request"] is None
    assert document["reviewer"]["provenance"]["run_id"] == "unknown"


def test_prepare_alone_leaves_started_record_like_a_killed_run(prepared: dict[str, Any]) -> None:
    review_run.prepare(prepared["args"])
    record = only_record(prepared["parent"])
    assert record["status"] == "started" and record["inputs"] is not None
    assert record["timing"]["prepare_seconds"] is not None
    assert record["timing"]["finished_at"] is None and record["timing"]["total_seconds"] is None


def test_prepare_failure_before_resolve_writes_no_record(prepared: dict[str, Any]) -> None:
    prepared["args"].model_name = None
    with pytest.raises(review_run.workspace.WorkspaceError, match="model"):
        review_run.prepare(prepared["args"])
    assert run_records(prepared["parent"]) == []


def test_prepare_failure_after_resolve_records_prepare(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(_target: dict[str, Any], _comments: str | None) -> dict[str, Any]:
        raise review_run.workspace.WorkspaceError("comments unavailable")

    monkeypatch.setattr(review_run, "read_records", unavailable)
    assert review_run.main(prepared["argv"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed" and failed["reason"] == "comments unavailable"
    assert failed["repository"] == REPOSITORY and failed["request_id"] == "request-7"
    record = only_record(prepared["parent"])
    assert failed["run_record"] == str(run_records(prepared["parent"])[0])
    assert (record["status"], record["failed_at"], record["reason"]) == ("failed", "prepare", "comments unavailable")
    assert record["inputs"] is None and record["timing"]["prepare_seconds"] is not None


def test_prepare_failure_is_kept_when_the_model_later_calls_fail(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = review_run.workspace.git

    def fail_base_fetch(root: Path, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("fetch", "--quiet") and "--no-write-fetch-head" in args:
            raise review_run.workspace.WorkspaceError("base fetch failed")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(review_run.workspace, "git", fail_base_fetch)
    assert review_run.main(prepared["argv"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["run_record"] == str(run_records(prepared["parent"])[0])
    assert review_run.main(["fail", "--state", failed["state_file"], "--reason", "止めた"]) == 1
    capsys.readouterr()
    record = only_record(prepared["parent"])
    assert (record["status"], record["failed_at"], record["reason"]) == ("failed", "prepare", "base fetch failed")


def test_model_fail_records_review(prepared: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    result = review_run.prepare(prepared["args"])
    assert review_run.main(["fail", "--state", result["state_file"], "--reason", "レビューを続けられない"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["run_record"] == result["run_record"]
    record = only_record(prepared["parent"])
    assert (record["status"], record["failed_at"], record["reason"]) == ("failed", "review", "レビューを続けられない")
    assert record["outputs"] is None
    assert record["timing"]["review_seconds"] is not None and record["timing"]["total_seconds"] is not None


def test_refused_complete_records_complete_and_a_retry_succeeds(prepared: dict[str, Any]) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    findings = Path(result["findings_draft"])
    document = json.loads(findings.read_text(encoding="utf-8"))
    document["coverage"]["read"] = []
    findings.write_text(json.dumps(document), encoding="utf-8")
    refused = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert refused["status"] == "failed" and refused["run_record"] == result["run_record"]
    record = only_record(prepared["parent"])
    assert (record["status"], record["failed_at"]) == ("failed", "complete")
    mark_reviewed(result)
    assert review_run.complete(argparse.Namespace(state=result["state_file"]))["status"] == "success"
    record = only_record(prepared["parent"])
    assert (record["status"], record["failed_at"], record["reason"]) == ("success", None, None)


def test_save_failure_records_save(prepared: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)
    destination = prepared["parent"] / result["destination"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("another review", encoding="utf-8")
    assert review_run.main(["complete", "--state", result["state_file"]]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["run_record"] == result["run_record"]
    record = only_record(prepared["parent"])
    assert (record["status"], record["failed_at"]) == ("failed", "save")
    assert record["outputs"] is None and record["timing"]["save_seconds"] is not None


def test_cleanup_failure_records_cleanup_with_saved_outputs(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)

    def cleanup_failure(_args: argparse.Namespace) -> None:
        raise review_run.workspace.WorkspaceError("cleanup refused")

    monkeypatch.setattr(review_run.workspace, "cleanup", cleanup_failure)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    record = only_record(prepared["parent"])
    assert (record["status"], record["failed_at"], record["reason"]) == ("failed", "cleanup", "cleanup refused")
    assert record["outputs"]["review_sha256"] == sha256(Path(completed["review_file"]))


def test_record_update_failure_does_not_change_the_review_result(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)

    def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(review_run.record, "update", broken)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success" and "disk full" in completed["run_record_error"]
    assert only_record(prepared["parent"])["status"] == "started"


def test_symlinked_review_directory_refuses_to_start(prepared: dict[str, Any], tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    reviews = prepared["parent"] / "reviews" / "example" / "syncer"
    reviews.mkdir(parents=True)
    (reviews / "#7-feature").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(review_run.workspace.WorkspaceError, match="run record"):
        review_run.prepare(prepared["args"])
    assert list(elsewhere.iterdir()) == []
    assert not (tmp_path / "sessions").exists() or list((tmp_path / "sessions").iterdir()) == []


# --- ランナーの観測値（attach-runner） ------------------------------------------------------------------------------


def batch_result(
    path: Path,
    *,
    agent_id: str = "fixture-reviewer",
    request_id: str = "request-7",
    state: Any = "success",
    effort: str | None = "high",
) -> Path:
    attempt = Attempt(
        agent_id=agent_id,
        state=state,
        identity=Identity(model="observed-model", effort=effort, session_id="session-1", cli_version="9.9.9"),
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
                request_id=request_id,
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


def test_attach_runner_writes_the_runner_block_once_and_keeps_provenance(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = completed_review(prepared)
    before = only_record(prepared["parent"])
    source = batch_result(tmp_path / "result.json", effort=None)
    code, first = attach(source, capsys)
    assert code == 0 and first["status"] == "success"
    assert first["attached"] == [result["run_record"]] and first["unmatched_count"] == 0
    after = only_record(prepared["parent"])
    assert after["runner"] == {
        "identity": {
            "model": "observed-model",
            "provider": "unknown",
            "effort": "unknown",
            "session_id": "session-1",
            "cli_version": "9.9.9",
        },
        "attempt": {"state": "success", "exit_code": 0, "elapsed_seconds": 12.5, "stop_confirmed": True},
    }
    assert after["reviewer"] == before["reviewer"]
    assert {key: value for key, value in after.items() if key != "runner"} == {
        key: value for key, value in before.items() if key != "runner"
    }
    code, second = attach(source, capsys)
    assert code == 0 and second["attached"] == [] and second["unchanged"] == [result["run_record"]]


def test_attach_runner_rejects_conflicting_values_without_writing(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    completed_review(prepared)
    assert attach(batch_result(tmp_path / "first.json", effort=None), capsys)[0] == 0
    written = only_record(prepared["parent"])
    code, payload = attach(batch_result(tmp_path / "second.json", effort="high"), capsys)
    assert code == 1 and payload["status"] == "failed" and payload["conflicts"] and payload["reason"]
    assert payload["attached"] == []
    assert only_record(prepared["parent"]) == written


def test_attach_runner_reports_attempts_without_a_record(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    completed_review(prepared)
    before = only_record(prepared["parent"])
    code, payload = attach(batch_result(tmp_path / "result.json", agent_id="other-reviewer"), capsys)
    assert code == 0 and payload["attached"] == [] and payload["unmatched_count"] == 1
    assert payload["unmatched"] == [
        {
            "run_id": "run-568",
            "request_id": "request-7",
            "agent_id": "other-reviewer",
            "role": "inspect",
            "state": "success",
        }
    ]
    assert only_record(prepared["parent"]) == before


def test_attach_runner_marks_a_timed_out_attempt_on_the_started_record(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review_run.prepare(prepared["args"])
    code, payload = attach(batch_result(tmp_path / "result.json", state="timed_out"), capsys)
    assert code == 0 and len(payload["attached"]) == 1
    record = only_record(prepared["parent"])
    assert record["status"] == "started" and record["runner"]["attempt"]["state"] == "timed_out"


def test_attach_runner_skips_files_outside_the_record_layout(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_attach_runner_rejects_an_invalid_result_file(
    prepared: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "result.json"
    source.write_text(json.dumps({"unknown": 1}), encoding="utf-8")
    code, payload = attach(source, capsys)
    assert code == 1 and payload["status"] == "failed" and payload["reason"]
    assert payload["result_file"] == str(source)


def test_shipped_runner_template_calls_attach_runner() -> None:
    config = load_config(ROOT / ".devcontainer/agent-runner.toml")
    commands = config.workflows["review"].after_run
    assert commands
    for argv in commands:
        script = next(index for index, argument in enumerate(argv) if argument.endswith("review_run.py"))
        assert (ROOT / argv[script]).resolve() == SCRIPT.resolve()
        assert (ROOT / argv[argv.index("--project") + 1] / "pyproject.toml").is_file()
        rest = [argument.replace("{result}", "/tmp/result.json") for argument in argv[script + 1 :]]
        assert review_run.parser().parse_args(rest).command == "attach-runner"


def test_record_outputs_failure_after_save_keeps_the_review_result(
    prepared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = review_run.prepare(prepared["args"])
    mark_reviewed(result)

    def broken(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("saved file vanished")

    monkeypatch.setattr(review_run.record, "outputs_document", broken)
    completed = review_run.complete(argparse.Namespace(state=result["state_file"]))
    assert completed["status"] == "success" and completed["cleanup"] == "success"
    assert "saved file vanished" in completed["run_record_error"]
    assert only_record(prepared["parent"])["status"] == "started"


def test_payloads_without_a_record_still_name_the_run_record_key(
    prepared: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert review_run.main(["fail", "--reason", "PR が分からない"]) == 1
    assert json.loads(capsys.readouterr().out)["run_record"] is None
    argv = [argument for argument in prepared["argv"] if argument not in {"--model-name", "observed-model"}]
    assert review_run.main(argv) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed" and failed["run_record"] is None
