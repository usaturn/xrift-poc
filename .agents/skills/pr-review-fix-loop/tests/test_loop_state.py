"""ロック・サイクルの状態・判断の記録の照合・結果ごとの終わり方を、偽の gh と bare remote で確かめる。

計画担当（review-fix-planning）の代わりに、テストが記録の投稿・push・マージを行う。
"""

from __future__ import annotations

import json
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
    """ロックを上書きできると、同じ PR で 2 つの run が並行して修正を push し、判断の記録を書き合う。"""
    first = env.start()

    with pytest.raises(loop.LoopError):
        env.start()

    lock = json.loads((env.review_dir / "loop/lock.json").read_text(encoding="utf-8"))
    assert lock["run_id"] == first["run_id"]


def test_start_rejects_an_unknown_role(loop: Any, env: Any) -> None:
    """`--assign` の役割名の誤記を拒まないと、その指定を黙って捨て、
    利用者が指定したのと違う担当でレビュー・修正を進める。
    """
    with pytest.raises(loop.LoopError):
        env.start(assign=["plan=alpha"])


@pytest.mark.parametrize(
    ("hold_options", "expected"),
    [
        # `--hold-action` を書かないとき argparse は skip を渡す。既定が外れると Hold の修正を黙って push する
        pytest.param({}, "skip", id="default"),
        # 指定した fix が届かないと Hold の修正を黙って Skip する
        pytest.param({"hold_action": "fix"}, "fix", id="fix"),
    ],
)
def test_start_records_options_and_effective_assignments(
    loop: Any,
    env: Any,
    kit: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hold_options: dict[str, Any],
    expected: str,
) -> None:
    """Hold の方針・サイクルの上限・`--assign` が run と計画への引き渡しで入れ替わると、
    利用者が許していない Hold の修正の push、上限を超えた修正の push、指定と違う担当での修正が黙って進む。
    """
    # 保存と計画への引き渡しは実 helper を使い、GitHub と親 checkout の探索だけ隔離する。
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
    assert run["options"]["max_cycles"] == 2
    assert run["options"]["hold_action"] == expected
    planning = reuse_or_run(loop, env)["planning"]
    assert planning["hold_action"] == expected
    options = planning["run_roles_options"]
    assert options[options.index("--assign") + 1] == "implement=beta"
    review_config = loop.load_review_config(loop.load_run(Path(started["run_dir"])))
    assert review_config.workflows["review"].assignments["inspect"].agents == ["beta", "alpha"]
    assert run["assignments"]["review"]["inspect"]["agents"] == ["beta", "alpha"]
    assert run["assignments"]["fix"]["implement"]["agents"] == ["beta"]


def test_record_for_another_head_is_not_taken_as_this_cycle(loop: Any, env: Any, kit: Any) -> None:
    """cycle-begin の後に投稿された別の HEAD の記録をこのサイクルの記録として受け取ると、
    計画していないサイクルが別の HEAD の結果で黙って終わり、このサイクルのレビューの指摘が計画されない。
    """
    env.start()
    old_head = env.gh.head()
    env.push("別の commit")
    reuse_or_run(loop, env)
    env.gh.post_record(
        kit.round_record(1, old_head, "stopped", [], stop_reason="external: 古い HEAD の記録", seats=[])
    )

    with pytest.raises(loop.LoopError):
        loop.cycle_end(env.run_dirs[-1], github=env.github)

    run = json.loads((env.run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert run["result"] is None


def test_fixes_pushed_requires_the_pr_head_to_be_the_pushed_commit(loop: Any, env: Any, kit: Any) -> None:
    """記録の commit と PR の HEAD を照合しないと、
    push の失敗・別の push による上書きを「修正を push した」として次のサイクル・上限まで進め、そう報告する。
    """
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    pushed = env.push("修正")
    env.push("さらに別の push")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))

    with pytest.raises(loop.LoopError):
        loop.cycle_end(env.run_dirs[-1], github=env.github)


def test_inherited_skip_changed_without_evidence_is_rejected(loop: Any, env: Any, kit: Any) -> None:
    """cycle-end が記録を前の状態と照合しないと、手で投稿した記録（引き継いだ Skip を根拠なく変えたもの）を
    黙って受け取り、後のサイクルの照合はそれを前の状態として使う。
    """
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

    with pytest.raises(loop.LoopError):
        loop.cycle_end(env.run_dirs[-1], github=env.github)


def test_no_fix_needed_needs_a_merged_finalize_result_at_the_same_head(
    loop: Any, env: Any, kit: Any, tmp_path: Path
) -> None:
    """finalize の「マージ済み」を PR の実際の状態と照合しないと、
    マージしていない PR を merged・no_fix_needed として終え、完了と報告する。
    """
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    env.gh.post_record(kit.round_record(1, head, "no_fix_needed", []))
    result = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)

    with pytest.raises(loop.LoopError):
        loop.cycle_end(env.run_dirs[-1], github=env.github, finalize_result=result)

    env.gh.merge()
    loop.cycle_end(env.run_dirs[-1], github=env.github, finalize_result=result)
    run = json.loads((env.run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert run["result"]["merged"] is True


def test_merged_pr_at_another_head_does_not_complete_the_cycle(loop: Any, env: Any, kit: Any, tmp_path: Path) -> None:
    """mergedだけで完了すると、別HEADのマージをこのサイクルの検証済み完了として記録する。"""
    env.start()
    head = env.gh.head()
    reuse_or_run(loop, env)
    env.gh.post_record(kit.round_record(1, head, "no_fix_needed", []))
    result = kit.finalize_result(tmp_path / "final.json", "no_fix_needed", merged=True)
    moved = env.push("別のHEADのマージ")
    env.gh.merge()
    assert env.gh.head() == moved != head

    with pytest.raises(loop.LoopError):
        loop.cycle_end(env.run_dirs[-1], github=env.github, finalize_result=result)

    run_dir = env.run_dirs[-1]
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["result"] is None
    assert json.loads((run_dir / "cycle-1.json").read_text(encoding="utf-8"))["state"] == "planning"


def test_finalize_stopped_ends_the_run_as_stopped_and_not_merged(
    loop: Any, env: Any, kit: Any, tmp_path: Path
) -> None:
    """finalize の stopped を no_fix_needed のまま記録すると、
    マージも Issue の更新もしていない run を完了として報告する。
    """
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
    assert run["result"]["outcome"] == "stopped"
    assert run["result"]["merged"] is False


def test_cycle_limit_ends_with_fixes_pushed(loop: Any, env: Any, kit: Any) -> None:
    """サイクルの上限が効かないと、利用者が許した回数を超えて修正の push とマージが自動で続く。"""
    env.start(max_cycles=1)
    head = env.gh.head()
    reuse_or_run(loop, env)
    pushed = env.push("修正")
    ref = f"alpha#H-1@{head[:12]}"
    env.gh.post_record(kit.round_record(1, head, "fixes_pushed", [kit.decision(ref, "fix", "new")], commit=pushed))

    loop.cycle_end(env.run_dirs[-1], github=env.github)

    run = json.loads((env.run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert run["result"]["ended_by"] == "cycle_limit"
    with pytest.raises(loop.LoopError):
        loop.cycle_begin(env.run_dirs[-1], github=env.github)


def test_finish_releases_only_its_own_lock(loop: Any, env: Any) -> None:
    """他の run のロックを消すと、その run が動いている PR に別の run が並行して入れる。"""
    started = env.start()
    run_dir = Path(started["run_dir"])
    lock = env.review_dir / "loop/lock.json"
    other = json.loads(lock.read_text(encoding="utf-8"))
    other["run_id"] = "f" * 32
    lock.write_text(json.dumps(other), encoding="utf-8")

    with pytest.raises(loop.LoopError):
        loop.finish(run_dir, abort="external: テスト")

    assert json.loads(lock.read_text(encoding="utf-8"))["run_id"] == "f" * 32


def test_record_from_an_earlier_run_for_the_same_head_is_not_taken(loop: Any, env: Any, kit: Any) -> None:
    """前の run が同じ HEAD で投稿した記録を拾うと、
    新しい run の計画が終わる前に cycle-end が古い結果で黙って run を終える。
    """
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

    with pytest.raises(loop.LoopError):
        loop.cycle_end(run_dir, github=env.github)

    env.gh.post_record(kit.round_record(2, head, "stopped", [], stop_reason="external: 二回目の計画の停止", seats=[]))
    ended = loop.cycle_end(run_dir, github=env.github)
    assert (ended["round"], ended["stop_reason"]) == (2, "external: 二回目の計画の停止")


def test_a_differently_cased_pr_url_shares_the_lock(loop: Any, env: Any) -> None:
    """大文字小文字の違う PR の URL でロックが分かれると、同じ PR に 2 つの run が並行して入る。"""
    first = env.start()

    with pytest.raises(loop.LoopError):
        loop.start(
            "https://github.com/Usaturn/Xrift-POC/pull/900",
            controller_model="m",
            controller_runtime="claude-code",
            github=env.github,
            parent_root=env.parent,
            tool_root=env.parent,
            template=env.template,
        )

    lock = json.loads((env.review_dir / "loop/lock.json").read_text(encoding="utf-8"))
    assert lock["run_id"] == first["run_id"]
