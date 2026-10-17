"""レビュアーの起動・結果の検査・再利用・資源の回収と、2 サイクル以上の流れを確かめる。

レビュアーは runner の adapter を差し替えた偽の CLI（conftest の REVIEWER_PROGRAM）で、PR は bare remote と偽の gh。
計画担当（review-fix-planning）の代わりに、テストが記録の投稿・push・指定 commit のレビューの保存・マージを行う。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from tools.agent_runner.config import load_config
from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")


def test_dsh_review_grants_only_artifacts_and_regular_save_lock(loop: Any, env: Any) -> None:
    started = env.start()
    run = loop.load_run(Path(started["run_dir"]))
    assert loop.review_write_paths(run, {"alpha": "pi"}) == ()
    paths = loop.review_write_paths(run, {"alpha": "dsh"})
    assert paths == (env.review_dir, env.parent / ".git" / "pr-review-save.lock")
    assert paths[0].is_dir() and paths[1].is_file()
    assert env.parent not in paths and env.parent / ".git" not in paths
    protected = env.parent / ".git" / "protected"
    protected.write_text("original")
    paths[1].unlink()
    paths[1].symlink_to(protected)
    with pytest.raises(loop.LoopError):
        loop.review_write_paths(run, {"alpha": "dsh"})
    assert protected.read_text() == "original"


def registered_worktrees(root: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    return [line.removeprefix("worktree ") for line in listing.splitlines() if line.startswith("worktree ")]


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- 結果の検査 ---------------------------------------------------------------------------------------------------


def accepted_result(env: Any, tmp_path: Path) -> tuple[Result, str]:
    """検査を通る 1 人分の runner の結果（成果 2 件をレビューディレクトリに置く）。"""
    head = env.gh.head()
    review, candidates = env.place_review(head, "alpha-model", findings=1)
    request = Request(
        run_id="run-1",
        request_id="request-1",
        agent_id="alpha",
        role="inspect",
        work_dir=tmp_path / "work",
        prompt="review",
        deadline_seconds=60.0,
        result_shape="json",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    document = read(candidates)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "success",
        "run_id": "run-1",
        "request_id": "request-1",
        "agent_id": "alpha",
        "role": "inspect",
        "repository": "usaturn/xrift-poc",
        "pr_number": 900,
        "pr_head": head,
        "reviewed_head": head,
        "review_file": str(review),
        "findings_file": str(candidates),
        "active_findings": 1,
        "cleanup": "success",
        "cleanup_details": {},
        "retained_paths": None,
        "reason": None,
        "reviewer": document["reviewer"],
        "timing": document["timing"],
    }
    attempt = Attempt(
        agent_id="alpha",
        state="success",
        identity=Identity(model="alpha-model"),
        exit_code=0,
        elapsed_seconds=1.0,
        stop_confirmed=True,
        work_dir=tmp_path / "work",
        request_path=request_path,
        payload=payload,
    )
    result = Result(
        request_id="request-1",
        role="inspect",
        requested_agent="alpha",
        actual_agent="alpha",
        state="success",
        attempts=[attempt],
    )
    return result, head


def broken(result: Result, case: str, env: Any, tmp_path: Path) -> Result:
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    attempt_update: dict[str, Any] = {}
    result_update: dict[str, Any] = {}
    if case == "outer_failed":
        result_update["state"] = "timed_out"
    elif case == "stop_unconfirmed":
        attempt_update["stop_confirmed"] = False
    elif case == "payload_failed":
        payload["status"] = "failed"
    elif case == "request_mismatch":
        payload["request_id"] = "request-2"
    elif case == "actual_agent_mismatch":
        result_update["actual_agent"] = "beta"
    elif case == "other_pr":
        payload["pr_number"] = 901
    elif case == "reviewed_head_mismatch":
        payload["reviewed_head"] = "0" * 40
    elif case == "review_outside_dir":
        outside = tmp_path / "outside.md"
        outside.write_text(Path(payload["review_file"]).read_text(encoding="utf-8"), encoding="utf-8")
        payload["review_file"] = str(outside)
    elif case == "review_symlink":
        link = env.review_dir / "link.md"
        link.symlink_to(payload["review_file"])
        payload["review_file"] = str(link)
    elif case == "findings_invalid":
        Path(payload["findings_file"]).write_text("{}", encoding="utf-8")
    elif case == "pair_mismatch":
        review = Path(payload["review_file"])
        review.write_text(review.read_text(encoding="utf-8").split("## High")[0], encoding="utf-8")
    elif case == "active_count_mismatch":
        payload["active_findings"] = 0
    attempt_update["payload"] = payload
    attempts = [attempt.model_copy(update=attempt_update)]
    return result.model_copy(update={**result_update, "attempts": attempts})


@pytest.mark.parametrize(
    "case",
    [
        "outer_failed",
        "stop_unconfirmed",
        "payload_failed",
        "request_mismatch",
        "actual_agent_mismatch",
        "other_pr",
        "reviewed_head_mismatch",
        "review_outside_dir",
        "review_symlink",
        "findings_invalid",
        "pair_mismatch",
        "active_count_mismatch",
    ],
)
def test_verify_reviewer_rejects_each_broken_condition(loop: Any, env: Any, tmp_path: Path, case: str) -> None:
    result, head = accepted_result(env, tmp_path)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(broken(result, case, env, tmp_path), pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected"
    assert entry.reason


def test_verify_reviewer_accepts_a_complete_payload_and_records_hashes(loop: Any, env: Any, tmp_path: Path) -> None:
    result, head = accepted_result(env, tmp_path)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(result, pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "accepted", entry.reason
    assert entry.active_findings == 1
    assert entry.review_sha256 == loop.sha256_file(Path(entry.review_file))
    assert entry.findings_sha256 == loop.sha256_file(Path(entry.findings_file))
    assert entry.identity["model"] == "alpha-model"


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "quoted_in_body",
        "model",
        "provider",
        "session_id",
        "cleanup",
        "payload",
        "body_header",
        "duplicate_header",
        "missing_header",
    ],
)
def test_dsh_pair_requires_observed_runner_identity(loop: Any, env: Any, tmp_path: Path, mismatch: str | None) -> None:
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[0]
    payload = cast(dict[str, Any], attempt.payload)
    candidates = Path(payload["findings_file"])
    document = read(candidates)
    reviewer = document["reviewer"]
    reviewer["runtime"] = "dsh"
    reviewer["provenance"].update(
        provider="actual-provider",
        effort="high",
        session_id="session-test",
        run_id="run-1",
        request_id="request-1",
        agent_id="alpha",
        role="inspect",
    )
    payload["reviewer"] = reviewer.copy()
    candidates.write_text(json.dumps(document))
    review = Path(payload["review_file"])
    review.write_text(
        review.read_text().replace("（ランタイム: pi）", "（ランタイム: dsh）")
        + "\n## Review timing\n\n<!-- pr-review:execution -->\n```json\n"
        + json.dumps({"reviewer": reviewer, "timing": document["timing"]})
        + "\n```\n"
    )
    attempt.identity = Identity(
        model="alpha-model", provider="actual-provider", effort="high", session_id="session-test"
    )
    if mismatch in {"model", "provider", "session_id"}:
        setattr(attempt.identity, mismatch, "wrong-observation")
    elif mismatch == "cleanup":
        payload["cleanup"] = "failed"
    elif mismatch == "payload":
        payload["reviewer"] = {**reviewer, "model": "different-model"}
    elif mismatch == "body_header":
        review.write_text(review.read_text().replace("レビュアー: alpha-model", "レビュアー: parent-model"))
    header = "- レビュアー: alpha-model（ランタイム: dsh）\n"
    if mismatch == "quoted_in_body":
        # 本文で担当行を引用しても、冒頭の識別行には数えない
        quote = "\n- レビュアー: 引用した古い担当（ランタイム: pi）\n"
        review.write_text(review.read_text().replace("## Summary\n\nテスト\n", "## Summary\n\nテスト\n" + quote))
    elif mismatch == "duplicate_header":
        review.write_text(review.read_text().replace(header, header * 2, 1))
    elif mismatch == "missing_header":
        review.write_text(review.read_text().replace(header, "", 1))
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")
    entry = loop.verify_reviewer(result, pr=pr, head=head, review_dir=env.review_dir)
    assert entry.verdict == ("accepted" if mismatch in {None, "quoted_in_body"} else "rejected"), entry.reason


def test_review_config_removes_only_dsh_github_tokens(loop: Any, env: Any) -> None:
    config = load_config(env.template)
    config.agents["alpha"].runtime = "dsh"
    config.agents["alpha"].env = ["GH_TOKEN", "GITHUB_TOKEN", "COMMANDCODE_API_KEY", "PATH"]
    config.agents["beta"].env = ["GH_TOKEN", "PATH"]
    safe = loop.review_config_without_github_tokens(config)
    assert safe.agents["alpha"].env == ["COMMANDCODE_API_KEY", "PATH"]
    assert safe.agents["beta"].env == ["GH_TOKEN", "PATH"]
    assert config.agents["alpha"].env == ["GH_TOKEN", "GITHUB_TOKEN", "COMMANDCODE_API_KEY", "PATH"]


PR_URL = "https://github.com/usaturn/xrift-poc/pull/900"


def dsh_parent_env(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """alpha を DSH にし、実際の親準備（prepare_parent）を bare remote と偽の gh で動かす。"""
    template = env.template.read_text().replace('runtime = "pi"', 'runtime = "dsh"')
    env.template.write_text(template)
    subprocess.run(["git", "-C", str(env.remote), "update-ref", "refs/pull/900/head", env.gh.head()], check=True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{env.remote.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/usaturn/xrift-poc.git")

    def api(argv: list[str]) -> subprocess.CompletedProcess[str]:
        response = env.gh.runner([argument for argument in argv if argument != "-i"])
        if "-i" in argv:
            document = json.loads(response.stdout)
            document["base"]["repo"]["clone_url"] = "https://github.com/usaturn/xrift-poc.git"
            return subprocess.CompletedProcess(
                argv, response.returncode, "HTTP/2 200 OK\n\n" + json.dumps(document), response.stderr
            )
        return response

    env.github = loop.GitHub(api)
    # 親の保存（save）も同じ偽の gh で現在の PR の HEAD を確かめる
    target = loop.review_entry.workspace.load_review_target()
    original = target.fetch_pull_request

    def fetch_pull_request(repository: str, number: int, *, runner: Any = api) -> Any:
        return original(repository, number, runner=runner)

    monkeypatch.setattr(target, "fetch_pull_request", fetch_pull_request)

    def metadata(argv: list[str]) -> str:
        assert argv[:3] == ["gh", "pr", "view"]
        return json.dumps({"body": "テストのPR", "closingIssuesReferences": []})

    monkeypatch.setattr(loop.review_entry, "command", metadata)


def no_reviewers(output: Path) -> BatchResult:
    output.mkdir()
    return BatchResult(
        run_id="fixture-run",
        state="failed",
        assignments={"inspect": ["alpha", "beta"]},
        results=[],
        outside_changes=[],
        elapsed_seconds=0.1,
        artifact_dir=output,
    )


def dispatch_no_reviewers(*args: Any, **kwargs: Any) -> BatchResult:
    return no_reviewers(kwargs["output_dir"])


def test_loop_prepares_bound_bundle_before_dsh_dispatch(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    dsh_parent_env(loop, env, monkeypatch)
    seen: list[dict[str, Any]] = []

    def dispatch(config: Any, workflow: str, prompt: str, root: Path, **kwargs: Any) -> BatchResult:
        descriptor = loop.review_entry.parent_data.descriptor_from_prompt(prompt)
        snapshot = loop.review_entry.parent_data.load(
            loop.review_entry.workspace, descriptor, "https://github.com/usaturn/xrift-poc/pull/900"
        )
        seen.append(snapshot)
        return no_reviewers(kwargs["output_dir"])

    monkeypatch.setattr(loop, "run_workflow", dispatch)
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    assert error.value.code == 6
    assert seen[0]["target"]["head_sha"] == env.gh.head()
    assert seen[0]["target"]["number"] == 900
    assert Path(seen[0]["bundle"]).is_file()


def test_dsh_runner_refusal_before_launch_can_be_retried(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    dsh_parent_env(loop, env, monkeypatch)
    dispatched: list[str] = []

    def refused(*args: Any, **kwargs: Any) -> BatchResult:
        raise loop.RunnerError()

    def dispatch(config: Any, workflow: str, prompt: str, root: Path, **kwargs: Any) -> BatchResult:
        descriptor = loop.review_entry.parent_data.descriptor_from_prompt(prompt)
        loop.review_entry.parent_data.load(loop.review_entry.workspace, descriptor, PR_URL)
        dispatched.append(descriptor["path"])
        return no_reviewers(kwargs["output_dir"])

    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    monkeypatch.setattr(loop, "run_workflow", refused)
    with pytest.raises(loop.LoopError) as first:
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    assert (first.value.code, first.value.stage) == (5, "runner")
    assert first.value.next_command[-3:] == ["review", "--run", str(run_dir)]

    monkeypatch.setattr(loop, "run_workflow", dispatch)
    with pytest.raises(loop.LoopError) as second:
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    # 案内どおりのやり直しは親準備で止まらず、レビュアーの起動まで進む
    assert (second.value.code, second.value.stage) == (6, "review")
    assert len(dispatched) == 1 and Path(dispatched[0]).is_file()


def test_parent_preparation_failure_after_clone_can_be_retried(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsh_parent_env(loop, env, monkeypatch)
    original = loop.review_entry.workspace.git
    failures: list[str] = []

    def transient(root: Path, *args: str, **kwargs: Any) -> Any:
        if not failures and args[0] == "fetch" and any(item.endswith(":refs/pr-review-parent/base") for item in args):
            failures.append(str(root))
            raise loop.review_entry.workspace.WorkspaceError("simulated transient fetch failure")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(loop.review_entry.workspace, "git", transient)
    monkeypatch.setattr(loop, "run_workflow", dispatch_no_reviewers)
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    with pytest.raises(loop.LoopError) as first:
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    assert (first.value.code, first.value.stage) == (5, "parent_preparation")
    assert failures and not (run_dir / "cycle-1" / "parent-pr-data").exists()

    with pytest.raises(loop.LoopError) as second:
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    assert (second.value.code, second.value.stage) == (6, "review")
    assert (run_dir / "cycle-1" / "parent-pr-data" / "snapshot.json").is_file()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_parent_preparation_keeps_an_existing_output(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    dsh_parent_env(loop, env, monkeypatch)
    monkeypatch.setattr(loop, "run_workflow", dispatch_no_reviewers)
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    output = run_dir / "cycle-1" / "parent-pr-data"
    other = tmp_path / "third-party"
    other.mkdir()
    (other / "keep.txt").write_text("third party", encoding="utf-8")
    if kind == "directory":
        output.mkdir()
        (output / "keep.txt").write_text("third party", encoding="utf-8")
    else:
        output.symlink_to(other, target_is_directory=True)
    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    assert (error.value.code, error.value.stage) == (5, "parent_preparation")
    assert (output / "keep.txt").read_text(encoding="utf-8") == "third party"
    assert sorted(path.name for path in other.iterdir()) == ["keep.txt"]
    assert output.is_symlink() is (kind == "symlink")


DSH_MODEL = "actual/model"


def staged_reviewer(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch, *, case: str | None) -> Any:
    """実際の pr-review prepare/complete で DSH の staged を作る偽の runner（子の起動と停止の観測だけを模す）。"""
    from tools.test_support.dsh import write_session

    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    launched: list[str] = []

    def dispatch(config: Any, workflow: str, prompt: str, root: Path, **kwargs: Any) -> BatchResult:
        output: Path = kwargs["output_dir"]
        work = output / "request-1" / "attempt-1" / "worktree"
        work.mkdir(parents=True)
        launched.append("alpha")
        request = Request(
            run_id="fixture-run",
            request_id="request-1",
            agent_id="alpha",
            role="inspect",
            work_dir=work,
            prompt=prompt,
            deadline_seconds=60.0,
            result_shape="json",
        )
        request_path = work.parent / "request.json"
        request_path.write_text(request.model_dump_json(), encoding="utf-8")
        home = work.parent / "dsh" / "state"
        write_session(home, work, provider="commandcode", model=DSH_MODEL)
        environment = {"DSH_HOME": str(home), "DSH_SESSION_ID": "session-test", "DSH_SHELL": "1"}
        for key, value in {**environment, "DSH_PROFILE": "headless"}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.chdir(work)
        parameters = loop.review_entry.parent_data.parameters_from_prompt(prompt)
        prepared = loop.review_entry.prepare(
            argparse.Namespace(
                pr=parameters["pr_url"],
                runtime="dsh",
                model_name=None,
                request=str(request_path),
                review_commit=None,
                source_root=None,
                records_comments=parameters["records_comments"],
                temp_root=None,
            )
        )
        assert prepared["status"] == "prepared", prepared
        head = prepared["reviewed_head"][:12]
        Path(prepared["draft"]).write_text(
            f"- 対象: PR #900 / head {head} / 2026-10-14\n- レビュアー: {DSH_MODEL}（ランタイム: dsh）\n\n"
            "## Review history\n\n| Reviewed at (UTC) | HEAD | Mode |\n|---|---|---|\n"
            f"| 2026-10-14T00:00:00Z | `{head}` | Initial |\n\n## Summary\n\n全体差分を確認した。\n",
            encoding="utf-8",
        )
        findings = Path(prepared["findings_draft"])
        document = json.loads(findings.read_text(encoding="utf-8"))
        document["coverage"]["read"] = ["PR 全体の差分と change.txt の全文"]
        findings.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        staged = loop.review_entry.complete(argparse.Namespace(state=prepared["state_file"]))
        assert staged["status"] == "staged", staged
        identity = Identity(provider="commandcode", model=DSH_MODEL, effort="high", session_id="session-test")
        if case == "identity":
            identity.model = "other-model"
        attempt = Attempt(
            agent_id="alpha",
            state="success",
            identity=identity,
            exit_code=0,
            elapsed_seconds=1.0,
            stop_confirmed=case != "unstopped",
            work_dir=work,
            request_path=request_path,
            payload=staged,
        )
        result = Result(
            request_id="request-1",
            role="inspect",
            requested_agent="alpha",
            actual_agent="alpha",
            state="success",
            attempts=[attempt],
        )
        return BatchResult(
            run_id="fixture-run",
            state="success",
            assignments={"inspect": ["alpha"]},
            results=[result],
            outside_changes=[],
            elapsed_seconds=1.0,
            artifact_dir=output,
        )

    monkeypatch.setattr(loop, "run_workflow", dispatch)
    return launched


@pytest.mark.parametrize("case", [None, "unstopped", "identity"])
def test_resumed_staged_review_is_finalized_without_hooks(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch, case: str | None
) -> None:
    dsh_parent_env(loop, env, monkeypatch)
    launched = staged_reviewer(loop, env, monkeypatch, case=case)
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    real = loop.review_entry.finalize_parent_batch

    def interrupted(batch: BatchResult) -> None:
        raise KeyboardInterrupt

    # 子が staged で停止した後、親の確定の前に helper が止まった
    monkeypatch.setattr(loop.review_entry, "finalize_parent_batch", interrupted)
    with pytest.raises(KeyboardInterrupt):
        loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
    monkeypatch.setattr(loop.review_entry, "finalize_parent_batch", real)
    saved = env.review_dir / "commandcode-actual-model.md"
    assert not saved.exists()

    if case is None:
        reviewed = loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
        assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]
        assert Path(reviewed["accepted"][0]["review_file"]) == saved and saved.is_file()
    else:
        with pytest.raises(loop.LoopError) as error:
            loop.review(run_dir, github=env.github, tmp_root=env.tmp_root)
        assert (error.value.code, error.value.stage) == (6, "review")
        assert not saved.exists()
    assert launched == ["alpha"]
    assert read(run_dir / "cycle-1.json")["review"]["after_run"] == []


@pytest.mark.parametrize("change", ["payload_failed", "payload_unchanged"])
def test_after_run_result_is_reloaded_before_reviewer_validation(
    loop: Any, env: Any, tmp_path: Path, change: str
) -> None:
    # 後処理（after_run）が result.json を更新したら、レビュアーの検査はその保存された結果を使う
    program = (
        "import json,pathlib,sys; path=pathlib.Path(sys.argv[1]); batch=json.loads(path.read_text())\n"
        "for item in batch['results']:\n"
        "    if item['actual_agent'] == 'alpha' and " + repr(change) + " == 'payload_failed':\n"
        "        item['attempts'][-1]['payload'].update(status='failed', reason='後処理が保存を拒否した')\n"
        "path.write_text(json.dumps(batch))\n"
    )
    with env.template.open("a") as stream:
        stream.write(
            "\n[workflows.review]\nafter_run = " + json.dumps([[sys.executable, "-c", program, "{result}"]]) + "\n"
        )
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "failed"})
    if change == "payload_unchanged":
        reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
        assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]
        return
    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (error.value.code, error.value.stage) == (6, "review")
    reviewers = read(run_dir / "cycle-1.json")["review"]["reviewers"]
    alpha = next(item for item in reviewers if item["actual_agent"] == "alpha")
    assert alpha["verdict"] == "rejected" and "後処理が保存を拒否した" in alpha["reason"]
    saved = read(run_dir / "cycle-1" / "review-result.json")
    saved_alpha = next(item for item in saved["results"] if item["actual_agent"] == "alpha")
    assert saved_alpha["attempts"][-1]["payload"]["status"] == "failed"


def test_review_executes_after_run_against_saved_result(loop: Any, env: Any, tmp_path: Path) -> None:
    marker = tmp_path / "hook-result.json"
    program = f"import pathlib,sys; pathlib.Path({str(marker)!r}).write_bytes(pathlib.Path(sys.argv[1]).read_bytes())"
    with env.template.open("a") as stream:
        stream.write(
            "\n[workflows.review]\nafter_run = " + json.dumps([[sys.executable, "-c", program, "{result}"]]) + "\n"
        )
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    reviewed = loop.review(
        run_dir,
        github=env.github,
        adapter_loader=env.reviewers({"alpha": "success", "beta": "success"}),
        tmp_root=env.tmp_root,
    )
    assert len(reviewed["accepted"]) == 2
    assert read(marker)["state"] == "success"
    reports = read(run_dir / "cycle-1.json")["review"]["after_run"]
    assert reports[0]["state"] == "success" and reports[0]["stop_confirmed"]


def test_reuse_counts_only_pairs_for_the_current_head(loop: Any, env: Any, kit: Any) -> None:
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")
    old = env.gh.head()
    env.place_review(old, "gamma")
    new = env.push("新しい commit")
    env.place_review(new, "alpha", commit=True)
    # gamma の本文を新しい HEAD のものに上書きした（候補一覧は古い HEAD のまま）
    env.place_review(new, "gamma")
    stale = env.review_dir / "findings" / f"gamma-{old[:12]}.json"
    assert stale.exists()

    reused_new = loop.find_reusable(env.review_dir, pr, new)
    reused_old = loop.find_reusable(env.review_dir, pr, old)

    assert sorted(Path(item.review_file).name for item in reused_new) == ["alpha-commit-" + new + ".md", "gamma.md"]
    assert reused_old == []


# --- レビューの起動 -----------------------------------------------------------------------------------------------


def test_review_refuses_when_the_head_moved_after_cycle_begin(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "success"})
    env.push("cycle-begin の後の push")

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (4, "head_moved")
    assert adapter.launched == []


def test_review_records_partial_failure_and_cleans_owned_worktrees(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "failed"}, findings=1)

    reviewed = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert sorted(adapter.launched) == ["alpha", "beta"]
    assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]
    assert [item["agent"] for item in reviewed["rejected"]] == ["beta"]
    assert reviewed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    cycle = read(run_dir / "cycle-1.json")
    assert cycle["state"] == "planning"
    beta = next(item for item in cycle["review"]["reviewers"] if item["actual_agent"] == "beta")
    assert beta["verdict"] == "rejected" and beta["active_findings"] is None
    assert (run_dir / "cycle-1/review-result.json").is_file()
    run = read(run_dir / "run.json")
    assert [resource["state"] for resource in run["resources"]] == ["removed"]
    assert not Path(run["resources"][0]["path"]).exists()
    assert registered_worktrees(env.parent) == [str(env.parent)]


def test_review_with_no_accepted_reviewer_fails_with_code_6(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "failed", "beta": "failed"})

    with pytest.raises(loop.LoopError) as error:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert (error.value.code, error.value.stage) == (6, "review")
    assert error.value.next_command[-4:-1] == ["finish", "--run", str(run_dir)]
    cycle = read(run_dir / "cycle-1.json")
    assert [item["verdict"] for item in cycle["review"]["reviewers"]] == ["rejected", "rejected"]
    finished = loop.finish(run_dir, abort="external: レビュアーが全員失敗した")
    assert finished["lock_released"] is True


# --- 2 サイクル以上 -----------------------------------------------------------------------------------------------


def test_two_cycles_reuse_the_pre_push_review_and_end_merged(loop: Any, env: Any, kit: Any, tmp_path: Path) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])

    # サイクル 1: レビュアー 2 人（1 人は失敗）→ 計画が skip 1 件・fix 1 件を決め、修正を push する
    first = loop.cycle_begin(run_dir, github=env.github)
    assert first["review"]["mode"] == "run"
    head1 = first["head"]
    adapter1 = env.reviewers({"alpha": "success", "beta": "failed"}, findings=1)
    loop.review(run_dir, github=env.github, adapter_loader=adapter1, tmp_root=env.tmp_root)
    skip_ref = f"alpha-model#M-1@{head1[:12]}"
    fix_ref = f"alpha-model#H-1@{head1[:12]}"
    head2 = env.push("H-1 の修正")
    # 計画の push 前の PR 全体レビュー（指定 commit のレビュー）の成果が、レビューディレクトリに残る
    env.place_review(head2, "planner-model", commit=True)
    env.gh.post_record(
        kit.round_record(
            1,
            head1,
            "fixes_pushed",
            [kit.decision(skip_ref, "skip", "new"), kit.decision(fix_ref, "fix", "new")],
            commit=head2,
            seats=[kit.seat(head1, "alpha-model")],
        )
    )
    ended1 = loop.cycle_end(run_dir, github=env.github)
    assert ended1["next"][-3:] == ["cycle-begin", "--run", str(run_dir)]

    # サイクル 2: HEAD は push した commit。レビュアーを起動せずに push 前のレビューを使う
    adapter2 = env.reviewers({"alpha": "success", "beta": "success"})
    second = loop.cycle_begin(run_dir, github=env.github)
    assert second["review"]["mode"] == "reuse"
    assert [Path(item["review_file"]).name for item in second["review"]["reused"]] == [
        f"planner-model-commit-{head2}.md"
    ]
    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter2, tmp_root=env.tmp_root)
    assert adapter2.launched == []
    env.gh.post_record(
        kit.round_record(
            2,
            head2,
            "no_fix_needed",
            [
                kit.decision(skip_ref, "skip", "inherited"),
                kit.decision(fix_ref, "resolved", "changed", previous_state="fix"),
            ],
            seats=[kit.seat(head1, "alpha-model", rerun=False), kit.seat(head2, f"planner-model-commit-{head2}")],
        )
    )
    final = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)
    env.gh.merge()
    ended2 = loop.cycle_end(run_dir, github=env.github, finalize_result=final, round_dir=tmp_path)
    finished = loop.finish(run_dir)

    assert ended2["outcome"] == "no_fix_needed"
    assert finished["result"] == {
        "outcome": "no_fix_needed",
        "ended_by": "outcome",
        "stop_reason": None,
        "merged": True,
        "cycles": 2,
    }
    assert not (env.review_dir / "loop/lock.json").exists()
    # サイクルごとに参考データの索引が残る
    cycle1, cycle2 = read(run_dir / "cycle-1.json"), read(run_dir / "cycle-2.json")
    alpha = next(item for item in cycle1["review"]["reviewers"] if item["actual_agent"] == "alpha")
    assert alpha["findings_sha256"] == loop.sha256_file(Path(alpha["findings_file"]))
    assert alpha["identity"]["model"] == "alpha-model"
    assert cycle1["planning"]["decisions"]["state"] == {"skip": 1, "fix": 1}
    assert cycle2["review"]["reused"][0]["reviewed_head"] == head2
    assert cycle2["planning"]["decisions"]["change"] == {"inherited": 1, "changed": 1}
    assert cycle2["planning"]["finalize"]["round_dir"] == str(tmp_path.resolve())


def test_reviewers_receive_the_previous_round_when_the_head_moved(loop: Any, env: Any, kit: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    head1 = loop.cycle_begin(run_dir, github=env.github)["head"]
    loop.review(run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root)
    skip_ref = f"alpha-model#M-1@{head1[:12]}"
    pushed = env.push("修正")
    env.gh.post_record(
        kit.round_record(
            1,
            head1,
            "fixes_pushed",
            [kit.decision(skip_ref, "skip", "new"), kit.decision(f"alpha-model#H-1@{head1[:12]}", "fix", "new")],
            commit=pushed,
            seats=[kit.seat(head1, "alpha-model")],
        )
    )
    loop.cycle_end(run_dir, github=env.github)
    head3 = env.push("別の人の push")

    second = loop.cycle_begin(run_dir, github=env.github)
    assert second["review"]["mode"] == "run"
    assert second["notes"]
    loop.review(run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root)

    received = kit.records.read_state(kit.records.comments_from_api(read(env.root / "copies/alpha.json")))
    assert [entry["round"] for entry in received["rounds"]] == [1]
    assert received["decisions"][0]["state"] == "skip"
    env.gh.post_record(
        kit.round_record(
            2,
            head3,
            "stopped",
            [kit.decision(skip_ref, "skip", "inherited")],
            stop_reason="permission: 追加レビューの認可が無い",
            seats=[kit.seat(head3, "alpha-model")],
        )
    )
    ended = loop.cycle_end(run_dir, github=env.github)
    assert ended["outcome"] == "stopped"
    assert loop.finish(run_dir)["result"]["outcome"] == "stopped"


# --- 排他の保持・中断と再開 ---------------------------------------------------------------------------------------


def test_unconfirmed_stop_holds_the_lock_until_operator_recovery(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    real = loop.run_workflow

    def unconfirmed(*args: Any, **kwargs: Any) -> BatchResult:
        batch: BatchResult = real(*args, **kwargs)
        results = [
            item.model_copy(
                update={
                    "attempts": [attempt.model_copy(update={"stop_confirmed": False}) for attempt in item.attempts]
                }
            )
            for item in batch.results
        ]
        return batch.model_copy(update={"results": results})

    monkeypatch.setattr(loop, "run_workflow", unconfirmed)
    with pytest.raises(loop.LoopError) as review_error:
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    assert review_error.value.code == 6
    resource = Path(read(run_dir / "run.json")["resources"][0]["path"])
    assert resource.exists()

    with pytest.raises(loop.LoopError) as held:
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")
    assert (held.value.code, held.value.stage) == (7, "resources")
    assert read(run_dir / "run.json")["state"] == "held"
    with pytest.raises(loop.LoopError) as refused:
        loop.recover(run_dir)
    assert refused.value.code == 7
    assert (env.review_dir / "loop/lock.json").exists()

    recovered = loop.recover(run_dir, operator_confirmed_stopped="ps で runner の子プロセスが無いことを確かめた")

    assert recovered["lock_released"] is True
    assert not resource.exists()
    assert registered_worktrees(env.parent) == [str(env.parent)]
    run = read(run_dir / "run.json")
    assert run["state"] == "ended"
    assert run["resources"][0]["state"] == "removed"
    assert "ps で" in run["resources"][0]["reason"]


def test_interrupted_review_without_result_is_not_rerun(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success"})

    def interrupted(*args: Any, **kwargs: Any) -> BatchResult:
        Path(kwargs["output_dir"]).mkdir()
        raise KeyboardInterrupt

    real = loop.run_workflow
    monkeypatch.setattr(loop, "run_workflow", interrupted)
    with pytest.raises(KeyboardInterrupt):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    monkeypatch.setattr(loop, "run_workflow", real)

    with pytest.raises(loop.LoopError) as again:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (again.value.code, again.value.stage) == (3, "interrupted_review")
    assert again.value.next_command[-3:] == ["recover", "--run", str(run_dir)]
    assert adapter.launched == []
    assert loop.status(run_dir=run_dir)["next"][-3:] == ["review", "--run", str(run_dir)]

    recovered = loop.recover(run_dir, operator_confirmed_stopped="中断した helper のプロセスが無いことを確かめた")
    assert recovered["lock_released"] is True
    run = read(run_dir / "run.json")
    assert run["state"] == "abandoned"
    # runner の結果が無いと秘密が残りうるファイルを判別できないので、ディレクトリは残して報告する
    resource = run["resources"][0]
    assert resource["state"] == "retained" and Path(resource["path"]).exists()
    assert resource["path"] in recovered["retained"]
    assert registered_worktrees(env.parent) == [str(env.parent)]


def test_resume_after_review_continues_from_status(loop: Any, env: Any, kit: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    head = loop.cycle_begin(run_dir, github=env.github)["head"]
    loop.review(run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root)

    # helper のプロセスが変わった後も、保存した状態だけから次の操作が分かる
    resumed = loop.status(run_dir=run_dir)
    assert resumed["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    env.gh.post_record(
        kit.round_record(1, head, "stopped", [], stop_reason="external: テスト", seats=[kit.seat(head, "alpha-model")])
    )
    assert loop.cycle_end(run_dir, github=env.github)["outcome"] == "stopped"
    assert loop.status(run_dir=run_dir)["next"][-3:] == ["finish", "--run", str(run_dir)]


def test_cycle_end_reads_the_roles_json_that_run_roles_writes(loop: Any, env: Any, kit: Any, tmp_path: Path) -> None:
    # run_roles.py は roles.json の batch を BatchResult.model_dump(mode="json") で書く（パスは文字列になる）。
    # 実機の確認で、strict な runner のモデルを Python モードで検証して読めなかった不具合の再現。
    started = env.start()
    run_dir = Path(started["run_dir"])
    head = env.gh.head()
    env.place_review(head)
    loop.cycle_begin(run_dir, github=env.github)
    attempt = Attempt(
        agent_id="alpha",
        state="success",
        identity=Identity(model="alpha-model", session_id="s-1"),
        exit_code=0,
        elapsed_seconds=3.0,
        stop_confirmed=True,
        work_dir=tmp_path / "implement/worktree",
        stdout_path=tmp_path / "implement/stdout.log",
        stderr_path=tmp_path / "implement/stderr.log",
        request_path=tmp_path / "implement/request.json",
        payload={"status": "completed"},
    )
    batch = BatchResult(
        run_id="run-implement",
        state="success",
        assignments={"implement": ["alpha"]},
        results=[
            Result(
                request_id="r-1",
                role="implement",
                requested_agent="alpha",
                actual_agent="alpha",
                state="success",
                attempts=[attempt],
            )
        ],
        outside_changes=[],
        elapsed_seconds=3.0,
        artifact_dir=tmp_path / "implement",
    )
    role_run = tmp_path / "implement"
    role_run.mkdir()
    (role_run / "roles.json").write_text(
        json.dumps(
            {"request": {}, "batch": batch.model_dump(mode="json"), "self_prompts": {}, "review_changes": None}
        ),
        encoding="utf-8",
    )
    pushed = env.push("修正")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))

    loop.cycle_end(run_dir, github=env.github, role_runs=[role_run])

    recorded = read(run_dir / "cycle-1.json")["planning"]["role_runs"]
    assert recorded[0]["assignments"] == {"implement": ["alpha"]}
    assert recorded[0]["results"][0]["actual_agent"] == "alpha"
    assert recorded[0]["results"][0]["identity"]["session_id"] == "s-1"


# --- 最終レビューの指摘の再現 ---------------------------------------------------------------------------------------


def unconfirmed_batch(loop: Any, monkeypatch: pytest.MonkeyPatch, sensitive: Path | None = None) -> None:
    """runner が停止を確認できなかった結果の形（stop_confirmed=false と、秘密が残りうる receipt）を返させる。"""
    real = loop.run_workflow

    def unconfirmed(*args: Any, **kwargs: Any) -> BatchResult:
        batch: BatchResult = real(*args, **kwargs)
        results: list[Result] = []
        for item in batch.results:
            attempts: list[Attempt] = []
            for attempt in item.attempts:
                paths: list[Path] = (
                    [attempt.request_path.parent / "receipt.json"] if attempt.request_path is not None else []
                )
                if sensitive is not None:
                    paths.append(sensitive)
                for path in paths:
                    path.write_text("{}", encoding="utf-8")
                attempts.append(attempt.model_copy(update={"stop_confirmed": False, "sensitive_paths": paths}))
            results.append(item.model_copy(update={"attempts": attempts}))
        return batch.model_copy(update={"results": results})

    monkeypatch.setattr(loop, "run_workflow", unconfirmed)


def has_placeholder(argv: list[str] | None) -> bool:
    return argv is not None and any("<" in item and ">" in item for item in argv)


def test_next_commands_never_carry_placeholders_and_recover_rejects_them(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    unconfirmed_batch(loop, monkeypatch)
    with pytest.raises(loop.LoopError) as failed:
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    assert failed.value.code == 6
    assert not has_placeholder(failed.value.next_command)
    with pytest.raises(loop.LoopError) as held:
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")
    with pytest.raises(loop.LoopError) as refused:
        loop.recover(run_dir)
    assert not has_placeholder(held.value.next_command)
    assert not has_placeholder(refused.value.next_command)

    with pytest.raises(loop.LoopError) as placeholder:
        loop.recover(run_dir, operator_confirmed_stopped="<確かめた事実>")
    assert placeholder.value.code == 2
    with pytest.raises(loop.LoopError):
        loop.finish(run_dir, abort="external: <理由>")
    assert (env.review_dir / "loop/lock.json").exists()


def test_recover_keeps_files_that_may_hold_secrets(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    unconfirmed_batch(loop, monkeypatch)
    with pytest.raises(loop.LoopError):
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    receipts = list(Path(read(run_dir / "run.json")["resources"][0]["path"]).rglob("receipt.json"))
    assert receipts
    with pytest.raises(loop.LoopError):
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")

    recovered = loop.recover(run_dir, operator_confirmed_stopped="ps で runner の子プロセスが無いことを確かめた")

    assert all(path.exists() for path in receipts)
    assert set(map(str, receipts)) <= set(recovered["retained"])
    assert registered_worktrees(env.parent) == [str(env.parent)]
    assert recovered["lock_released"] is True


def test_failed_cleanup_paths_are_reported(loop: Any, env: Any, tmp_path: Path) -> None:
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    payload.update(
        status="failed",
        cleanup="failed",
        reason="cleanup refused",
        retained_paths={"worktree": "/tmp/pr-review-900-x/worktree", "session_dir": "/tmp/pr-review-900-x"},
    )
    failed = result.model_copy(update={"attempts": [attempt.model_copy(update={"payload": payload})]})
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(failed, pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected"
    assert sorted(entry.retained_paths) == ["/tmp/pr-review-900-x", "/tmp/pr-review-900-x/worktree"]


def test_status_after_every_reviewer_failed_points_to_abort(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "failed", "beta": "failed"})
    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    next_command = loop.status(run_dir=run_dir)["next"]
    assert "finish" in next_command and any(item.startswith("--abort=external: ") for item in next_command)
    assert not has_placeholder(next_command)
    with pytest.raises(loop.LoopError) as again:
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    assert (again.value.code, again.value.stage) == (6, "review")
    assert sorted(adapter.launched) == ["alpha", "beta"]


def test_runner_refusing_before_launch_can_be_retried(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)

    def refused(*args: Any, **kwargs: Any) -> BatchResult:
        raise loop.RunnerError()

    real = loop.run_workflow
    monkeypatch.setattr(loop, "run_workflow", refused)
    with pytest.raises(loop.LoopError) as error:
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    monkeypatch.setattr(loop, "run_workflow", real)
    assert (error.value.code, error.value.stage) == (5, "runner")
    assert error.value.next_command[-3:] == ["review", "--run", str(run_dir)]
    assert [resource["state"] for resource in read(run_dir / "run.json")["resources"]] == ["removed"]

    reviewed = loop.review(
        run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
    )
    assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"]


@pytest.mark.parametrize("case", ["other_pr_document", "symlink_inside_dir", "stale_body"])
def test_verify_reviewer_rejects_artifacts_that_do_not_belong_to_this_head(
    loop: Any, env: Any, kit: Any, tmp_path: Path, case: str
) -> None:
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    review, candidates = Path(payload["review_file"]), Path(payload["findings_file"])
    if case == "other_pr_document":
        document = read(candidates)
        document["pr"] = {"repository": "usaturn/xrift-poc", "number": 901}
        candidates.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    elif case == "symlink_inside_dir":
        target = env.review_dir / "real.md"
        review.rename(target)
        review.symlink_to(target)
    else:
        review.write_text(review.read_text(encoding="utf-8").replace(head[:12], "1" * 12), encoding="utf-8")
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    entry = loop.verify_reviewer(result, pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected", case


@pytest.mark.parametrize("entrypoint", ["cleanup", "audit"])
@pytest.mark.parametrize("locked", [False, True])
def test_missing_output_parent_does_not_hide_registered_worktrees(
    loop: Any, env: Any, tmp_path: Path, entrypoint: str, locked: bool
) -> None:
    from tools.agent_runner.workspace import create_worktree

    run_dir = Path(env.start()["run_dir"])
    run = loop.load_run(run_dir)
    parent = tmp_path / "xrift-poc-pr-loop-owned"
    worktree = create_worktree(env.parent, "HEAD", parent / "child\n日本語" / "worktree")
    if locked:
        subprocess.run(["git", "-C", str(env.parent), "worktree", "lock", str(worktree)], check=True)
    other = create_worktree(env.parent, "HEAD", tmp_path / "other-run" / "worktree")
    (other / "untracked").write_text("do not remove")
    resource = loop.Resource(path=str(parent), cycle=1, state="created")
    run.resources.append(resource)
    shutil.rmtree(parent)
    if entrypoint == "cleanup":
        assert loop.cleanup_output(env.parent, resource, None, force=True) == [str(worktree)]
    else:
        assert loop.audit_resources(run) == [str(parent)]
        # A retained resource still requires stop evidence on later calls.
        assert loop.audit_resources(run) == [str(parent)]
    assert resource.state == "retained"
    assert str(worktree) in [str(path) for path in loop._registered_worktrees(env.parent)]
    assert (other / "untracked").read_text() == "do not remove"
    assert not parent.exists()


@pytest.mark.parametrize("entrypoint", ["cleanup", "audit"])
@pytest.mark.parametrize("failure", ["inaccessible", "dangling", "git_unavailable"])
def test_missing_output_checks_fail_closed(loop: Any, env: Any, tmp_path: Path, entrypoint: str, failure: str) -> None:
    run = loop.load_run(Path(env.start()["run_dir"]))
    ancestor = tmp_path / "protected"
    ancestor.mkdir()
    parent = ancestor / "xrift-poc-pr-loop-owned"
    gitdir = env.parent / ".git"
    moved_gitdir = env.parent / "hidden-git"
    if failure == "inaccessible":
        parent.mkdir()
        ancestor.chmod(0)
    elif failure == "dangling":
        parent.symlink_to(tmp_path / "absent-target", target_is_directory=True)
    else:
        gitdir.rename(moved_gitdir)
    resource = loop.Resource(path=str(parent), cycle=1, state="created")
    run.resources.append(resource)
    try:
        result = (
            loop.cleanup_output(env.parent, resource, None, force=True)
            if entrypoint == "cleanup"
            else loop.audit_resources(run)
        )
        assert result == [str(parent)] and resource.state != "removed"
    finally:
        if failure == "inaccessible":
            ancestor.chmod(0o755)
        elif failure == "git_unavailable":
            moved_gitdir.rename(gitdir)


@pytest.mark.parametrize("entrypoint", ["cleanup", "audit"])
def test_missing_parent_with_no_registration_is_removed(loop: Any, env: Any, tmp_path: Path, entrypoint: str) -> None:
    run = loop.load_run(Path(env.start()["run_dir"]))
    resource = loop.Resource(path=str(tmp_path / "xrift-poc-pr-loop-gone"), cycle=1, state="created")
    run.resources.append(resource)
    result = loop.cleanup_output(env.parent, resource, None) if entrypoint == "cleanup" else loop.audit_resources(run)
    assert result == [] and resource.state == "removed"
