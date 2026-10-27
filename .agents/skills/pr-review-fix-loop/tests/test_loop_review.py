"""レビュアーの起動・結果の検査・再利用・資源の回収を確かめる。

レビュアーは runner の adapter を差し替えた偽の CLI（conftest の REVIEWER_PROGRAM）で、PR は bare remote と偽の gh。
計画担当（review-fix-planning）の代わりに、テストが記録の投稿・push・指定 commit のレビューの保存・マージを行う。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

from tools.agent_runner.models import Attempt, BatchResult, Identity, Request, Result

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_broken_personal_config_never_falls_back_to_template(tmp_path: Path) -> None:
    """壊れた個人設定リンクを無視すると、利用者の割り当てやafter_runを失ったままレビューが進む。"""
    import os

    personal = tmp_path / "個人 設定.toml"
    personal.symlink_to(tmp_path / "missing.toml")
    script = Path(__file__).resolve().parents[1] / "scripts/loop.py"
    result = subprocess.run(
        [sys.executable, str(script), "check-options"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "AGENT_RUNNER_CONFIG": str(personal),
            "XDG_CONFIG_HOME": str(tmp_path),
        },
    )
    assert result.returncode != 0, result.stdout


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


def broken(result: Result, case: str, tmp_path: Path) -> Result:
    attempt = result.attempts[-1]
    payload: dict[str, Any] = dict(cast(dict[str, Any], attempt.payload))
    if case == "payload_failed":
        payload["status"] = "failed"
    elif case == "payload_repository":
        payload["repository"] = "another/repository"
    elif case == "payload_pr_number":
        payload["pr_number"] += 1
    elif case == "payload_pr_head":
        payload["pr_head"] = "0" * 40
    elif case == "payload_reviewed_head":
        payload["reviewed_head"] = "0" * 40
    elif case in ("candidates_pr", "candidates_head"):
        path = Path(payload["findings_file"])
        document = read(path)
        if case == "candidates_pr":
            document["pr"]["number"] += 1
        else:
            document["reviewed_head"] = "0" * 40
        path.write_text(json.dumps(document), encoding="utf-8")
    elif case == "markdown_head":
        path = Path(payload["review_file"])
        markdown = path.read_text(encoding="utf-8")
        path.write_text(
            markdown.replace(f"| `{payload['reviewed_head'][:12]}` |", f"| `{'0' * 12}` |"), encoding="utf-8"
        )
    elif case == "review_outside_dir":
        # 別のレビューディレクトリへ保存された成果。名前は元のままにして、場所だけが違う形にする
        outside = tmp_path / "elsewhere" / Path(payload["review_file"]).name
        outside.parent.mkdir()
        outside.write_text(Path(payload["review_file"]).read_text(encoding="utf-8"), encoding="utf-8")
        payload["review_file"] = str(outside)
    attempts = [attempt.model_copy(update={"payload": payload})]
    return result.model_copy(update={"attempts": attempts})


@pytest.mark.parametrize(
    "case",
    [
        # pr-review が失敗と返した結果を受け付けると、途中で止まったレビューを完了したレビューとして計画に渡し、
        # 今の HEAD のレビュアーとして no_fix_needed を通す
        "payload_failed",
        # レビューディレクトリの外に保存した成果を受け付けると、計画はレビューディレクトリだけを読むので、
        # そのレビュアーの指摘が黙って抜ける
        "review_outside_dir",
        # 下流の判断記録は元payload/候補一覧/本文を再照合しない。別PR/HEADの受け付けはここで防ぐ。
        "payload_repository",
        "payload_pr_number",
        "payload_pr_head",
        "payload_reviewed_head",
        "candidates_pr",
        "candidates_head",
        "markdown_head",
    ],
)
def test_verify_reviewer_rejects_each_broken_condition(loop: Any, env: Any, tmp_path: Path, case: str) -> None:
    """レビュアーの結果を受け付ける条件が外れると、
    計画に入らないレビューを受け付けたことにして進む（ケースごとの損害はコメント）。"""
    result, head = accepted_result(env, tmp_path)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")

    # 壊す前の結果は受け付ける（拒否のテストが入力の古さで空回りしないための対照）
    assert loop.verify_reviewer(result, pr=pr, head=head, review_dir=env.review_dir).verdict == "accepted"

    entry = loop.verify_reviewer(broken(result, case, tmp_path), pr=pr, head=head, review_dir=env.review_dir)

    assert entry.verdict == "rejected"


def test_after_run_result_is_reloaded_before_reviewer_validation(loop: Any, env: Any, tmp_path: Path) -> None:
    """後処理（after_run）が保存を拒否した結果を読み直さないと、
    拒否されたレビュアーを黙って受け付ける（利用者の個人設定に after_run がある）。"""
    program = (
        "import json,pathlib,sys; path=pathlib.Path(sys.argv[1]); batch=json.loads(path.read_text())\n"
        "for item in batch['results']:\n"
        "    if item['actual_agent'] == 'alpha':\n"
        "        item['attempts'][-1]['payload'].update(status='failed', reason='後処理が保存を拒否した')\n"
        "path.write_text(json.dumps(batch))\n"
    )
    with env.template.open("a") as stream:
        stream.write(
            "\n[workflows.review]\nafter_run = " + json.dumps([[sys.executable, "-c", program, "{result}"]]) + "\n"
        )
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "success", "beta": "success"})

    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    reviewers = read(run_dir / "cycle-1.json")["review"]["reviewers"]
    alpha = next(item for item in reviewers if item["actual_agent"] == "alpha")
    assert alpha["verdict"] == "rejected"


def test_reuse_counts_only_pairs_for_the_current_head(loop: Any, env: Any) -> None:
    """古い HEAD の候補一覧・本文の組を再利用すると、新しい commit をレビューせずに計画とマージへ進む。"""
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


def test_review_with_no_accepted_reviewer_stops_before_planning(loop: Any, env: Any) -> None:
    """全員の失敗で止まらないと、レビューの無いまま計画・no_fix_needed・マージへ進む。"""
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": "failed", "beta": "failed"})
    # 全員が runner の成功の後に却下されたので、まず全員を 1 回だけ再実行する
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert read(run_dir / "cycle-1.json")["state"] == "reviewing"


@pytest.mark.parametrize("phase", ["initial", "rerun"])
def test_head_movement_before_review_does_not_reach_planning(loop: Any, env: Any, phase: str) -> None:
    """開始後や再実行前のHEAD移動を見逃すと、古いHEADのレビューを現在の計画へ渡す。"""
    run_dir = Path(env.start()["run_dir"])
    old_head = env.gh.head()
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": ["failed", "success"], "beta": "success"})
    if phase == "rerun":
        first = loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
        assert "planning" not in first
    launched = list(adapter.launched)
    assert env.push("別のHEAD 日本語\n  空白を含む変更") != old_head

    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    cycle = read(run_dir / "cycle-1.json")
    assert cycle["head"] == old_head and cycle["state"] == "reviewing"
    assert read(run_dir / "run.json")["result"] is None
    assert adapter.launched == launched


def test_unconfirmed_stop_holds_the_lock_until_operator_recovery(
    loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止を確認できない試行を停止済みとして扱うと、生きているかもしれないレビュアーの worktree を消し、
    ロックを解放して別の run を並行して入れる。"""
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
    with pytest.raises(loop.LoopError):
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    resource = Path(read(run_dir / "run.json")["resources"][0]["path"])
    assert resource.exists()

    with pytest.raises(loop.LoopError):
        loop.finish(run_dir, abort="environment: レビュアーの停止を確認できない")
    assert (env.review_dir / "loop/lock.json").exists() and resource.exists()
    with pytest.raises(loop.LoopError):
        loop.recover(run_dir)
    assert (env.review_dir / "loop/lock.json").exists() and resource.exists()


def test_interrupted_review_without_result_is_not_rerun(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """中断の跡のある review でレビュアーを起動し直すと、
    止まっていないかもしれない前のレビュアーと同じレビューファイルに並行して書く。"""
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

    with pytest.raises(loop.LoopError):
        loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    assert adapter.launched == []


def unconfirmed_batch(loop: Any, monkeypatch: pytest.MonkeyPatch) -> None:
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
                for path in paths:
                    path.write_text("{}", encoding="utf-8")
                attempts.append(attempt.model_copy(update={"stop_confirmed": False, "sensitive_paths": paths}))
            results.append(item.model_copy(update={"attempts": attempts}))
        return batch.model_copy(update={"results": results})

    monkeypatch.setattr(loop, "run_workflow", unconfirmed)


def test_recover_rejects_a_placeholder_confirmation(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """recover が書きかけの確認（<確かめた事実>）を受け付けると、
    停止を確かめないまま worktree の強制削除と資源の削除が進む。"""
    started = env.start()
    run_dir = Path(started["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    unconfirmed_batch(loop, monkeypatch)
    with pytest.raises(loop.LoopError):
        loop.review(
            run_dir, github=env.github, adapter_loader=env.reviewers({"alpha": "success"}), tmp_root=env.tmp_root
        )
    resource = Path(read(run_dir / "run.json")["resources"][0]["path"])

    with pytest.raises(loop.LoopError):
        loop.recover(run_dir, operator_confirmed_stopped="<確かめた事実>")

    assert (env.review_dir / "loop/lock.json").exists() and resource.exists()


def test_inaccessible_output_directory_holds_the_resource(loop: Any, env: Any, tmp_path: Path) -> None:
    """調べられない出力ディレクトリを回収済みとすると、
    中の生きているかもしれない worktree を報告せずにロックを解放する。"""
    run = loop.load_run(Path(env.start()["run_dir"]))
    ancestor = tmp_path / "protected"
    ancestor.mkdir()
    parent = ancestor / "xrift-poc-pr-loop-owned"
    parent.mkdir()
    ancestor.chmod(0)
    resource = loop.Resource(path=str(parent), cycle=1, state="created")
    run.resources.append(resource)
    try:
        assert loop.audit_resources(run) == [str(parent)]
        assert resource.state != "removed"
    finally:
        ancestor.chmod(0o755)


def test_request_is_kept_only_from_a_regular_file(loop: Any, env: Any, tmp_path: Path) -> None:
    """却下した試行の request.json がレビュアーの置いた symlink や停止を確かめられない試行のファイルのとき写すと、
    loop（sandbox の外）がリンク先のファイルをレビューディレクトリへ運び、
    sandbox の中のレビュアーが読めないはずのファイルを次のレビュアーが読める場所に置く。"""
    result, head = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    payload = {**cast(dict[str, Any], attempt.payload), "status": "failed"}
    target = tmp_path / "elsewhere.json"
    target.write_text(cast(Path, attempt.request_path).read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "linked-request.json"
    link.symlink_to(target)
    pr = loop.PrRef(repository="usaturn/xrift-poc", number=900, url="https://github.com/usaturn/xrift-poc/pull/900")
    kept = tmp_path / "kept"
    # 停止を確かめられない試行のファイルは、生きた書き手が差し替えうるので読まない
    for request_path, stopped, expected in (
        (link, True, False),
        (tmp_path / "missing.json", True, False),
        (attempt.request_path, False, False),
        (attempt.request_path, True, True),
    ):
        update = {"payload": payload, "request_path": request_path, "stop_confirmed": stopped}
        failed = result.model_copy(update={"attempts": [attempt.model_copy(update=update)]})
        entry = loop.verify_reviewer(failed, pr=pr, head=head, review_dir=env.review_dir)
        loop.keep_rejected_request(failed, entry, kept)
        assert (entry.request_file is not None) is expected
    assert sorted(path.name for path in kept.iterdir()) == ["request-1.json"]


def session_worktrees(env: Any) -> list[str]:
    """偽のレビュアーが pr-review の helper のセッションを模して残した worktree（作った順）。"""
    worktrees = [path / "worktree" for path in env.sessions.glob("pr-review-*")]
    return [str(path) for path in sorted(worktrees, key=lambda path: path.stat().st_mtime_ns)]


def test_finish_reports_the_first_attempt_and_keeps_its_worktree(loop: Any, env: Any) -> None:
    """finish の要約から 1 回目の試行の outside_changes が落ちると、
    tool_root の追跡ファイルの書き換えが利用者に知らされないまま残り（#41）、
    回収がレビュアーの worktree を消すと、他者の作業場所を消す。"""
    run_dir = Path(env.start()["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    adapter = env.reviewers({"alpha": ["failed+retained+outside", "success"], "beta": "success"})
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)
    loop.review(run_dir, github=env.github, adapter_loader=adapter, tmp_root=env.tmp_root)

    finished = loop.finish(run_dir, abort="external: テストの終了")

    [worktree] = session_worktrees(env)
    assert str(env.parent) in finished["cycles"][0]["outside_changes"]
    # 消さない。パスに改行を含むので、-z で読む loop の一覧で照合する
    assert Path(worktree).is_dir() and Path(worktree) in loop._registered_worktrees(env.parent)


def test_cleanup_keeps_sessions_that_pr_review_retained_inside_the_output(loop: Any, env: Any, tmp_path: Path) -> None:
    """runner の出力領域（試行の TMPDIR）の中に pr-review が残すと報告した session を回収で消すと、
    制作物の PR の一時 clone と下書きが、保持の報告の前に消える（一時 clone は tool_root に登録されない）。
    報告したパスを含む登録済みの worktree を外しても、報告したパスが一緒に消える。"""
    result, _ = accepted_result(env, tmp_path)
    attempt = result.attempts[-1]
    parent = Path(tempfile.mkdtemp(prefix=str(loop.TMP_PREFIX), dir=env.tmp_root))
    resource = loop.Resource(path=str(parent), cycle=1, state="created")
    output = loop._output_dir(resource)
    reported: dict[str, object] = {}
    declared: list[Path] = []
    # helper の dict 形式と、loop が受け付ける list 形式
    for agent, as_list in (("alpha", False), ("beta", True)):
        session = output / f"{agent}-run/attempt-1/tmp/pr-review-900-{agent}"
        clone = session / "worktree"
        subprocess.run(["git", "clone", "-q", str(env.remote), str(clone)], check=True, capture_output=True)
        draft = session / "review-backup" / "draft.md"
        draft.parent.mkdir()
        draft.write_text("下書き\n", encoding="utf-8")
        paths = {"worktree": str(clone), "draft": str(draft), "session_dir": str(session), "pr_ref": "refs/x/900"}
        reported[agent] = list(paths.values()) if as_list else paths
        declared += [clone, draft, session]
    assert not any(path.is_relative_to(parent) for path in loop._registered_worktrees(env.parent))
    # tool_root に登録した clean な worktree の追跡ファイルだけを報告する
    worktree = output / "gamma-run/attempt-1/tmp/pr-review-900-gamma/worktree"
    subprocess.run(
        ["git", "-C", str(env.parent), "worktree", "add", "-q", "--detach", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
    )
    reported["gamma"] = [str(worktree / "README.md")]
    declared.append(worktree / "README.md")
    results: list[Result] = []
    for agent, retained_paths in reported.items():
        payload = {
            **cast(dict[str, Any], attempt.payload),
            "agent_id": agent,
            "status": "failed",
            "cleanup": "failed",
            "retained_paths": retained_paths,
        }
        failed = attempt.model_copy(update={"agent_id": agent, "payload": payload})
        results.append(result.model_copy(update={"actual_agent": agent, "attempts": [failed]}))
    batch = BatchResult(
        run_id="run-1",
        state="success",
        assignments={"inspect": list(reported)},
        results=results,
        outside_changes=[],
        elapsed_seconds=1.0,
        artifact_dir=output,
    )

    retained = loop.cleanup_output(env.parent, resource, batch)

    assert [path for path in declared if not path.exists()] == []
    assert {str(path) for path in declared} <= set(retained)
    assert resource.state == "retained"
