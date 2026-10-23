"""ロック・サイクルの状態・判断の記録の照合・結果ごとの終わり方を、偽の gh と bare remote で確かめる。

計画担当（review-fix-planning）の代わりに、テストが記録の投稿・push・マージを行う。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest


def reuse_or_run(loop: Any, env: Any) -> dict[str, Any]:
    """現在の HEAD の成果 2 件を置いてから cycle-begin を呼び、レビュアーを起動せずに計画の段階へ進める。

    レビュアーの起動は test_loop_review で確かめる。
    """
    env.place_review(env.gh.head())
    begun: dict[str, Any] = loop.cycle_begin(Path(env.run_dirs[-1]), github=env.github)
    assert begun["review"]["mode"] == "reuse"
    return begun


def test_second_start_is_refused_while_the_first_run_holds_the_lock(loop: Any, env: Any) -> None:
    first = env.start()

    with pytest.raises(loop.LoopError) as error:
        env.start()

    assert error.value.code == 3
    assert error.value.stage == "lock_held"
    assert first["run_id"] in error.value.reason
    lock = json.loads((env.review_dir / "loop/lock.json").read_text(encoding="utf-8"))
    assert lock["run_id"] == first["run_id"]


def test_start_refuses_a_closed_pr(loop: Any, env: Any) -> None:
    env.gh.state = "closed"

    with pytest.raises(loop.LoopError) as error:
        env.start()

    assert (error.value.code, error.value.stage) == (4, "pr_not_open")
    assert not (env.review_dir / "loop/lock.json").exists()


def test_start_rejects_an_unknown_role_and_inspect_self(loop: Any, env: Any) -> None:
    for assign in (["plan=claude-opus"], ["inspect=self"], ["implement=claude-opus,codex"]):
        with pytest.raises(loop.LoopError) as error:
            env.start(assign=assign)
        assert error.value.code == 2, assign
    assert not (env.review_dir / "loop/lock.json").exists()


CONFIG_REJECTED = "ConfigError: invalid_config"


def write_personal(root: Path, extra: str = "") -> Path:
    path = root / "personal.toml"
    path.write_text(extra, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("assign", "extra", "expected"),
    [
        pytest.param(
            [],
            '[workflows.fix.assignments.self-review]\nagents = ["alpha", "beta"]\n',
            CONFIG_REJECTED,
            id="self-review-two-primaries",
        ),
        pytest.param([], "[roles.self-review]\nwrites = true\n", CONFIG_REJECTED, id="self-review-writes"),
    ],
)
def test_start_rejects_fix_assignments_that_run_roles_refuses(
    loop: Any,
    env: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    assign: list[str],
    extra: str,
    expected: str,
) -> None:
    """run_roles が設定だけで拒否する割り当ては、レビューや implement の後ではなく start で止める（#675）。"""
    personal = write_personal(tmp_path, extra)

    with pytest.raises(loop.LoopError) as error:
        env.start(assign=assign, config=personal)

    assert (error.value.code, error.value.stage) == (2, "config")
    assert expected in error.value.reason
    assert env.gh.calls == []
    assert not (env.review_dir / "loop").exists()
    # 同じ設定を run_roles に渡したときの理由と同じ文・分類で止まる
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    arguments = ["--phase", "self-review", "--request", str(request), "--output-dir", str(tmp_path / "roles")]
    arguments += ["--template", str(env.template), "--config", str(personal)]
    arguments += [option for entry in assign for option in ("--assign", entry)]
    capsys.readouterr()
    assert loop.run_roles.main(arguments) == 2
    reported = re.search(r"failed \((.*)\); no verification success implied", capsys.readouterr().err)
    assert reported is not None
    assert reported.group(1) in error.value.reason


@pytest.mark.parametrize(
    ("assign", "expected"),
    [
        pytest.param([], {"implement": ["alpha"], "self-review": ["self"], "fix-check": ["self"]}, id="default"),
        pytest.param(
            ["self-review=alpha", "fix-check=beta"],
            {"implement": ["alpha"], "self-review": ["alpha"], "fix-check": ["beta"]},
            id="external-review-roles",
        ),
    ],
)
def test_start_accepts_fix_assignments_that_run_roles_accepts(
    env: Any, tmp_path: Path, assign: list[str], expected: dict[str, list[str]]
) -> None:
    """既定と外部レビューの割り当てを受け付ける。"""
    started = env.start(assign=assign, config=write_personal(tmp_path))

    assert started["status"] == "ok"
    assert {role: value["agents"] for role, value in started["assignments"]["fix"].items()} == expected


@pytest.mark.parametrize(
    ("entrypoint", "hold_options", "expected"),
    [
        ("api", {}, "skip"),
        ("api", {"hold_action": None}, "skip"),
        ("api", {"hold_action": "skip"}, "skip"),
        ("api", {"hold_action": "fix"}, "fix"),
        ("cli", {}, "skip"),
        ("cli", {"hold_action": "skip"}, "skip"),
        ("cli", {"hold_action": "fix"}, "fix"),
    ],
)
def test_start_records_options_and_effective_assignments(
    loop: Any,
    env: Any,
    kit: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    hold_options: dict[str, Any],
    expected: str,
) -> None:
    # CLI でも保存と計画への引き渡しは実 helper を使い、GitHub と親 checkout の探索だけ隔離する。
    if entrypoint == "api":
        started = env.start(assign=["inspect=beta,alpha", "implement=beta"], max_cycles=2, **hold_options)
    else:
        monkeypatch.setattr(loop, "GitHub", lambda: env.github)
        monkeypatch.setattr(loop.target, "default_parent_root", lambda: env.parent)
        args = [
            "start",
            f"#{kit.number}",
            "--controller-model",
            "test-planner",
            "--controller-runtime",
            "pi",
            "--max-cycles",
            "2",
            "--assign",
            "inspect=beta,alpha",
            "--assign",
            "implement=beta",
            "--template",
            str(env.template),
        ]
        if hold_options:
            args += ["--hold-action", hold_options["hold_action"]]
        assert loop.main(args) == 0
        started = json.loads(capsys.readouterr().out)
        env.run_dirs.append(Path(started["run_dir"]))

    run = json.loads((Path(started["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert run["schema"] == "pr-review-fix-loop/v1"
    assert run["options"]["max_cycles"] == 2
    assert run["options"]["hold_action"] == expected
    assert reuse_or_run(loop, env)["planning"]["hold_action"] == expected
    assert run["assignments"]["review"]["inspect"]["agents"] == ["beta", "alpha"]
    assert run["assignments"]["fix"]["implement"]["agents"] == ["beta"]
    if entrypoint == "api":
        assert run["tool_commit"] == kit.git(env.parent, "rev-parse", "HEAD")
    assert started["next"][-3:] == ["cycle-begin", "--run", started["run_dir"]]


def test_out_of_order_calls_are_refused(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])

    with pytest.raises(loop.LoopError) as error:
        loop.cycle_end(run_dir, github=env.github)

    assert (error.value.code, error.value.stage) == (3, "state")


def test_cycle_end_before_the_record_is_posted_keeps_the_cycle_open(loop: Any, env: Any, kit: Any) -> None:
    # 関連検証が落ちて計画が修正を続けている間は、記録が無い。loop はサイクルを終えずに待つ。
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    run_dir = env.run_dirs[-1]

    with pytest.raises(loop.LoopError) as error:
        loop.cycle_end(run_dir, github=env.github)
    assert (error.value.code, error.value.stage) == (4, "record_missing")
    cycle = json.loads((run_dir / "cycle-1.json").read_text(encoding="utf-8"))
    assert cycle["state"] == "planning"

    pushed = env.push("修正")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))
    ended = loop.cycle_end(run_dir, github=env.github)

    assert ended["outcome"] == "fixes_pushed"
    assert ended["next"][-3:] == ["cycle-begin", "--run", str(run_dir)]


def test_record_for_another_head_is_not_taken_as_this_cycle(loop: Any, env: Any, kit: Any) -> None:
    env.start()
    old_head = env.gh.head()
    env.gh.post_record(kit.round_record(1, old_head, "stopped", [], stop_reason="external: 古い記録", seats=[]))
    env.push("別の commit")
    reuse_or_run(loop, env)

    with pytest.raises(loop.LoopError) as error:
        loop.cycle_end(env.run_dirs[-1], github=env.github)

    assert error.value.stage == "record_missing"


def test_fixes_pushed_requires_the_pr_head_to_be_the_pushed_commit(loop: Any, env: Any, kit: Any) -> None:
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    pushed = env.push("修正")
    env.push("さらに別の push")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))

    with pytest.raises(loop.LoopError) as error:
        loop.cycle_end(env.run_dirs[-1], github=env.github)

    assert (error.value.code, error.value.stage) == (4, "pushed_head_mismatch")


def test_inherited_skip_changed_without_evidence_is_rejected(loop: Any, env: Any, kit: Any) -> None:
    env.start()
    first = env.gh.head()
    reuse_or_run(loop, env)
    skip_ref = f"alpha#M-1@{first[:12]}"
    fix_ref = f"alpha#H-1@{first[:12]}"
    pushed = env.push("修正")
    env.gh.post_record(
        kit.round_record(
            1,
            first,
            "fixes_pushed",
            [kit.decision(skip_ref, "skip", "new"), kit.decision(fix_ref, "fix", "new")],
            commit=pushed,
        )
    )
    loop.cycle_end(env.run_dirs[-1], github=env.github)
    reuse_or_run(loop, env)
    # 引き継いだ skip の状態を変えた記録（inherited のまま fix）は、前の状態との照合で拒否される
    env.gh.post_record(
        kit.round_record(
            2,
            pushed,
            "stopped",
            [
                kit.decision(skip_ref, "fix", "inherited"),
                kit.decision(fix_ref, "resolved", "changed", previous_state="fix"),
            ],
            stop_reason="permission: テスト",
            seats=[kit.seat(pushed)],
        )
    )

    with pytest.raises(loop.LoopError) as error:
        loop.cycle_end(env.run_dirs[-1], github=env.github)

    assert (error.value.code, error.value.stage) == (4, "record_inconsistent")


def test_no_fix_needed_needs_a_merged_finalize_result_at_the_same_head(
    loop: Any, env: Any, kit: Any, tmp_path: Path
) -> None:
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    env.gh.post_record(kit.round_record(1, head, "no_fix_needed", []))

    with pytest.raises(loop.LoopError) as missing:
        loop.cycle_end(env.run_dirs[-1], github=env.github)
    assert (missing.value.code, missing.value.stage) == (2, "arguments")

    result = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)
    with pytest.raises(loop.LoopError) as unmerged:
        loop.cycle_end(env.run_dirs[-1], github=env.github, finalize_result=result)
    assert (unmerged.value.code, unmerged.value.stage) == (4, "merge_mismatch")

    env.gh.merge()
    ended = loop.cycle_end(env.run_dirs[-1], github=env.github, finalize_result=result)
    assert ended["outcome"] == "no_fix_needed"
    run = json.loads((env.run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert run["result"]["merged"] is True
    assert run["result"]["ended_by"] == "outcome"


def test_finalize_stopped_ends_the_run_as_stopped_and_not_merged(
    loop: Any, env: Any, kit: Any, tmp_path: Path
) -> None:
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    env.gh.post_record(kit.round_record(1, head, "no_fix_needed", []))
    result = kit.finalize_result(
        tmp_path / "final.json", "stopped", merged=False, stop_reason="external: mergeable が UNKNOWN"
    )

    ended = loop.cycle_end(env.run_dirs[-1], github=env.github, finalize_result=result)

    assert ended["outcome"] == "stopped"
    run = json.loads((env.run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert run["result"] == {
        "outcome": "stopped",
        "ended_by": "outcome",
        "stop_reason": "external: mergeable が UNKNOWN",
        "merged": False,
        "cycles": 1,
    }
    cycle = json.loads((env.run_dirs[-1] / "cycle-1.json").read_text(encoding="utf-8"))
    assert cycle["planning"]["finalize"]["result"]["outcome"] == "stopped"


def test_cycle_limit_ends_with_fixes_pushed(loop: Any, env: Any, kit: Any) -> None:
    env.start(max_cycles=1)
    head = env.gh.head()
    reuse_or_run(loop, env)
    pushed = env.push("修正")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))

    ended = loop.cycle_end(env.run_dirs[-1], github=env.github)

    assert ended["outcome"] == "fixes_pushed"
    assert ended["next"][-3:] == ["finish", "--run", str(env.run_dirs[-1])]
    run = json.loads((env.run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert run["result"]["ended_by"] == "cycle_limit"
    with pytest.raises(loop.LoopError) as error:
        loop.cycle_begin(env.run_dirs[-1], github=env.github)
    assert error.value.stage == "state"


def test_stopped_record_ends_the_run_and_finish_releases_the_lock(loop: Any, env: Any, kit: Any) -> None:
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    env.gh.post_record(
        kit.round_record(1, head, "stopped", [], stop_reason="permission: 追加レビューの認可が無い", seats=[])
    )

    ended = loop.cycle_end(env.run_dirs[-1], github=env.github)
    finished = loop.finish(env.run_dirs[-1])

    assert ended["outcome"] == "stopped"
    assert finished["result"]["stop_reason"] == "permission: 追加レビューの認可が無い"
    assert not (env.review_dir / "loop/lock.json").exists()
    assert env.start()["status"] == "ok"


def test_finish_releases_only_its_own_lock(loop: Any, env: Any) -> None:
    started = env.start()
    run_dir = Path(started["run_dir"])
    lock = env.review_dir / "loop/lock.json"
    other = json.loads(lock.read_text(encoding="utf-8"))
    other["run_id"] = "f" * 32
    lock.write_text(json.dumps(other), encoding="utf-8")

    with pytest.raises(loop.LoopError) as error:
        loop.finish(run_dir, abort="external: テスト")

    assert (error.value.code, error.value.stage) == (3, "not_owner")
    assert json.loads(lock.read_text(encoding="utf-8"))["run_id"] == "f" * 32


def test_abort_needs_a_classified_reason(loop: Any, env: Any) -> None:
    started = env.start()

    with pytest.raises(loop.LoopError) as error:
        loop.finish(Path(started["run_dir"]), abort="レビュアーが全員失敗した")
    assert error.value.code == 2

    finished = loop.finish(Path(started["run_dir"]), abort="external: レビュアーが全員失敗した")
    assert finished["result"]["ended_by"] == "abort"
    assert not (env.review_dir / "loop/lock.json").exists()


@pytest.mark.parametrize("saved_hold_action", [None, "skip", "fix"])
def test_status_returns_the_next_command_after_a_restart(
    loop: Any, env: Any, kit: Any, saved_hold_action: str | None
) -> None:
    started = env.start(hold_action="fix")
    run_dir = Path(started["run_dir"])
    # 変更前の run.json（null を含む）を保存済み記録として用意する。新規 start の既定を使わない。
    run_file = run_dir / "run.json"
    saved = json.loads(run_file.read_text(encoding="utf-8"))
    saved["options"]["hold_action"] = saved_hold_action
    run_file.write_text(json.dumps(saved), encoding="utf-8")
    loop.status(run_dir=run_dir)
    assert reuse_or_run(loop, env)["planning"]["hold_action"] == saved_hold_action
    assert json.loads(run_file.read_text(encoding="utf-8"))["options"]["hold_action"] == saved_hold_action

    by_run = loop.status(run_dir=run_dir)
    by_pr = loop.status(argument=f"#{kit.number}", github=env.github, parent_root=env.parent)

    assert by_run["next"][-3:] == ["cycle-end", "--run", str(run_dir)]
    assert by_pr["run_id"] == started["run_id"]
    assert by_pr["next"] == by_run["next"]


def test_record_from_an_earlier_run_for_the_same_head_is_not_taken(loop: Any, env: Any, kit: Any) -> None:
    # 前の run が同じ HEAD で stopped を投稿した後、同じ HEAD で新しい run を始めた場合
    # （例: 権限不足で止まった run を同じ HEAD で再実行）
    first = env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    env.gh.post_record(
        kit.round_record(1, head, "stopped", [], stop_reason="permission: 追加レビューの認可が無い", seats=[])
    )
    loop.cycle_end(Path(first["run_dir"]), github=env.github)
    loop.finish(Path(first["run_dir"]))

    second = env.start(hold_action="fix")
    run_dir = Path(second["run_dir"])
    assert loop.cycle_begin(run_dir, github=env.github)["review"]["mode"] == "reuse"

    with pytest.raises(loop.LoopError) as error:
        loop.cycle_end(run_dir, github=env.github)
    assert (error.value.code, error.value.stage) == (4, "record_missing")

    env.gh.post_record(kit.round_record(2, head, "stopped", [], stop_reason="external: 二回目の計画の停止", seats=[]))
    ended = loop.cycle_end(run_dir, github=env.github)
    assert (ended["round"], ended["stop_reason"]) == (2, "external: 二回目の計画の停止")


def test_a_differently_cased_pr_url_shares_the_lock(loop: Any, env: Any, kit: Any) -> None:
    first = env.start()

    with pytest.raises(loop.LoopError) as error:
        loop.start(
            "https://github.com/Usaturn/Xrift-POC/pull/900",
            controller_model="m",
            controller_runtime="claude-code",
            github=env.github,
            parent_root=env.parent,
            tool_root=env.parent,
            template=env.template,
        )

    assert (error.value.code, error.value.stage) == (3, "lock_held")
    assert first["run_id"] in error.value.reason


def test_a_failed_lock_write_leaves_no_lock(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk full")

    real = loop.LockRecord.model_dump_json
    monkeypatch.setattr(loop.LockRecord, "model_dump_json", broken)
    with pytest.raises(OSError):
        env.start()
    monkeypatch.setattr(loop.LockRecord, "model_dump_json", real)

    assert not (env.review_dir / "loop/lock.json").exists()
    assert env.start()["status"] == "ok"
