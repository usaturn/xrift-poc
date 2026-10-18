"""start が現在の Herdr ペインの名前を PR#<番号> に変えることを、PATH に置いた偽の herdr で確かめる。

実際の herdr は呼ばない。conftest の no_herdr が HERDR_ENV・HERDR_PANE_ID を外し、このファイルのテストだけが
偽の herdr（fake_herdr）を置いてから設定し直す。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

PANE = "w1:p9"


def enter_pane(monkeypatch: pytest.MonkeyPatch, herdr_env: str | None = "1", pane_id: str | None = PANE) -> None:
    """Herdr のペインの中の環境変数にする（None は設定しない）。"""
    for name, value in (("HERDR_ENV", herdr_env), ("HERDR_PANE_ID", pane_id)):
        if value is not None:
            monkeypatch.setenv(name, value)


def start_cli(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    """CLI の start を実行して、終了コードと出力の JSON を返す（GitHub と親 checkout の探索だけ隔離する）。"""
    monkeypatch.setattr(loop, "GitHub", lambda: env.github)
    monkeypatch.setattr(loop.target, "default_parent_root", lambda: env.parent)
    code = loop.main(
        [
            "start",
            "#900",
            "--controller-model",
            "test-planner",
            "--controller-runtime",
            "pi",
            "--template",
            str(env.template),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    if output.get("run_dir"):
        env.run_dirs.append(Path(output["run_dir"]))
    return code, output


def read_run(started: dict[str, Any]) -> dict[str, Any]:
    run: dict[str, Any] = json.loads((Path(started["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    return run


@pytest.mark.parametrize("argument", ["#900", "900", "https://github.com/usaturn/xrift-poc/pull/900"])
def test_start_renames_the_current_pane_once(
    env: Any, fake_herdr: Any, monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    enter_pane(monkeypatch)

    started = env.start(argument)

    assert fake_herdr.calls() == [["pane", "rename", PANE, "PR#900"]]
    assert started["herdr_pane"] == {"state": "renamed", "pane_id": PANE, "label": "PR#900", "reason": None}
    assert read_run(started)["herdr_pane"] == started["herdr_pane"]
    assert started["warnings"] == []


@pytest.mark.parametrize(
    ("herdr_env", "pane_id"),
    [(None, PANE), ("0", PANE), ("true", PANE), ("1", None), ("1", ""), ("1", "  ")],
)
def test_start_skips_herdr_outside_a_pane(
    env: Any, fake_herdr: Any, monkeypatch: pytest.MonkeyPatch, herdr_env: str | None, pane_id: str | None
) -> None:
    enter_pane(monkeypatch, herdr_env, pane_id)

    started = env.start()

    assert fake_herdr.calls() == []
    pane = started["herdr_pane"]
    assert (pane["state"], pane["pane_id"], pane["label"]) == ("skipped", None, "PR#900")
    assert pane["reason"]
    assert read_run(started)["herdr_pane"] == pane
    assert started["warnings"] == []


def test_start_failure_does_not_call_herdr(
    loop: Any, env: Any, fake_herdr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    enter_pane(monkeypatch)
    env.gh.state = "closed"
    with pytest.raises(loop.LoopError) as closed:
        env.start()
    assert (closed.value.code, closed.value.stage) == (4, "pr_not_open")
    assert fake_herdr.calls() == []

    env.gh.state = "open"
    for options in ({"assign": ["plan=alpha"]}, {"max_cycles": 0}):
        with pytest.raises(loop.LoopError) as invalid:
            env.start(**options)
        assert (invalid.value.code, invalid.value.stage) == (2, "arguments"), options
    assert fake_herdr.calls() == []

    env.start()
    assert fake_herdr.calls() == [["pane", "rename", PANE, "PR#900"]]
    with pytest.raises(loop.LoopError) as held:
        env.start()
    assert (held.value.code, held.value.stage) == (3, "lock_held")
    assert fake_herdr.calls() == [["pane", "rename", PANE, "PR#900"]]


@pytest.mark.parametrize("failure", ["missing", "fail", "hang"])
def test_start_succeeds_when_herdr_fails(
    loop: Any,
    env: Any,
    fake_herdr: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    failure: str,
) -> None:
    enter_pane(monkeypatch)
    if failure == "missing":
        # start は git を使うので、git だけを置いた PATH にする（実際の herdr も偽の herdr も見えない）
        git = shutil.which("git")
        assert git is not None
        only_git = tmp_path / "git-only"
        only_git.mkdir()
        (only_git / "git").symlink_to(git)
        monkeypatch.setenv("PATH", str(only_git))
    elif failure == "fail":
        fake_herdr.set_mode("fail")
    else:
        fake_herdr.set_mode("hang")
        monkeypatch.setattr(loop, "HERDR_TIMEOUT_SECONDS", 0.5)

    began = time.monotonic()
    code, started = start_cli(loop, env, monkeypatch, capsys)
    elapsed = time.monotonic() - began

    assert code == 0
    pane = started["herdr_pane"]
    assert (pane["state"], pane["pane_id"], pane["label"]) == ("failed", PANE, "PR#900")
    assert pane["reason"]
    assert any(pane["reason"] in warning for warning in started["warnings"])
    lock = json.loads((env.review_dir / "loop/lock.json").read_text(encoding="utf-8"))
    assert lock["run_id"] == started["run_id"]
    run = read_run(started)
    assert run["state"] == "running"
    assert run["herdr_pane"] == pane
    assert elapsed < 5
    expected_calls = [] if failure == "missing" else [["pane", "rename", PANE, "PR#900"]]
    assert fake_herdr.calls() == expected_calls


def test_run_record_without_herdr_pane_still_loads(loop: Any, env: Any) -> None:
    # この変更より前の run.json には herdr_pane が無い。status・finish がそのまま読めること
    started = env.start()
    run_dir = Path(started["run_dir"])
    record = read_run(started)
    del record["herdr_pane"]
    (run_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    assert loop.status(run_dir)["state"] == "running"
    assert loop.load_run(run_dir).herdr_pane is None
