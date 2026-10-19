"""check-options が、start と同じ検査を PR・ロック・run・GitHub・herdr に触れずに行うことを確かめる（#678）。

issue-to-pr は worktree を作る前に check-options を呼び、何時間も実装した後で start が拒否する事態を避ける。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# テスト用の雛形（conftest の TEST_TEMPLATE）に、runtime が dsh の担当 delta を足す個人設定
DSH_AGENT = """
[agents.delta]
runtime = "dsh"
command = ["fake-delta"]
model = "delta-model"
"""
# runtime が codex の担当。名前ではなく runtime（adapter の宣言）で止まることを確かめるため、別名にする（#52）
CODEX_AGENT = """
[agents.gpt-review]
runtime = "codex"
command = ["fake-codex"]
model = "gpt-review-model"
"""
CODEX_INSPECT_FALLBACK = """
[workflows.review.assignments.inspect]
agents = ["alpha"]
fallback = ["gpt-review"]
"""


def check_options(loop: Any, capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, Any]]:
    capsys.readouterr()
    code = loop.main(["check-options", *arguments])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def isolated(loop: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """check-options が GitHub や親 checkout を使えば、偽の gh の呼び出しとして見えるようにする。"""
    monkeypatch.setattr(loop, "GitHub", lambda: env.github)
    monkeypatch.setattr(loop.target, "default_parent_root", lambda: env.parent)
    return env


def assert_untouched(env: Any) -> None:
    assert env.gh.calls == []
    assert not (env.parent / "reviews").exists()


def test_check_options_accepts_the_template_and_reports_effective_assignments(
    loop: Any, isolated: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = check_options(loop, capsys, "--template", str(isolated.template))

    assert code == 0
    assert output["status"] == "ok"
    assert output["next"] is None
    assert output["options"] == {
        "max_cycles": 3,
        "hold_action": "skip",
        "assign": {},
        "template": str(isolated.template),
        "config": None,
    }
    assert output["assignments"]["review"]["inspect"]["agents"] == ["alpha", "beta"]
    assert output["assignments"]["fix"]["implement"]["agents"] == ["alpha"]
    assert_untouched(isolated)


def test_check_options_reports_the_options_start_would_record(
    loop: Any, isolated: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = check_options(
        loop,
        capsys,
        "--max-cycles",
        "2",
        "--hold-action",
        "fix",
        "--assign",
        "inspect=beta",
        "--assign",
        "implement=beta",
        "--template",
        str(isolated.template),
    )

    assert code == 0
    started = isolated.start(max_cycles=2, hold_action="fix", assign=["inspect=beta", "implement=beta"])
    run = json.loads((Path(started["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert output["options"] == {key: value for key, value in run["options"].items() if key != "fresh_review"}
    assert output["assignments"] == run["assignments"]


def without_fix_check(template: Path, root: Path) -> Path:
    path = root / "no-fix-check.toml"
    text = template.read_text(encoding="utf-8")
    section = '[workflows.fix.assignments.fix-check]\nagents = ["self"]\n'
    assert section in text
    path.write_text(text.replace(section, ""), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("start_options", "cli"),
    [
        pytest.param({"max_cycles": 0}, ["--max-cycles", "0"], id="max-cycles-0"),
        pytest.param({"max_cycles": 11}, ["--max-cycles", "11"], id="max-cycles-11"),
        pytest.param({"assign": ["inspect=self"]}, ["--assign", "inspect=self"], id="inspect-self"),
        pytest.param({"assign": ["plan=alpha"]}, ["--assign", "plan=alpha"], id="unknown-role"),
        pytest.param({"assign": ["inspect=nobody"]}, ["--assign", "inspect=nobody"], id="unknown-agent"),
        pytest.param({"assign": ["inspect="]}, ["--assign", "inspect="], id="empty-agent"),
        pytest.param(
            {"assign": ["implement=alpha,beta"]}, ["--assign", "implement=alpha,beta"], id="two-implementers"
        ),
        pytest.param({"assign": ["self-review=delta"]}, ["--assign", "self-review=delta"], id="dsh-self-review"),
        pytest.param({"template": "no-fix-check"}, [], id="missing-fix-check"),
        pytest.param(
            {"assign": ["inspect=alpha,gpt-review"]}, ["--assign", "inspect=alpha,gpt-review"], id="codex-inspect"
        ),
        pytest.param({"personal": CODEX_INSPECT_FALLBACK}, [], id="codex-inspect-fallback"),
        pytest.param(
            {"assign": ["self-review=gpt-review"]}, ["--assign", "self-review=gpt-review"], id="codex-self-review"
        ),
        pytest.param({"assign": ["fix-check=gpt-review"]}, ["--assign", "fix-check=gpt-review"], id="codex-fix-check"),
    ],
)
def test_check_options_rejects_what_start_rejects_with_the_same_reason(
    loop: Any,
    isolated: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    start_options: dict[str, Any],
    cli: list[str],
) -> None:
    personal = tmp_path / "personal.toml"
    personal.write_text(DSH_AGENT + CODEX_AGENT + start_options.pop("personal", ""), encoding="utf-8")
    template = isolated.template
    if start_options.get("template") == "no-fix-check":
        template = without_fix_check(template, tmp_path)
        start_options = {key: value for key, value in start_options.items() if key != "template"}

    code, output = check_options(loop, capsys, *cli, "--template", str(template), "--config", str(personal))
    with pytest.raises(loop.LoopError) as error:
        isolated.start(template=template, config=personal, **start_options)

    assert code == 2
    assert output["status"] == "error"
    assert (output["stage"], output["reason"]) == (error.value.stage, error.value.reason)
    assert output["next"] is None
    assert_untouched(isolated)


def test_check_options_rejects_codex_reviewers_in_the_real_template(
    loop: Any, isolated: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """README の例（--assign inspect=claude-opus,codex）を、実際の雛形で run の前に止める（#52）。"""
    personal = tmp_path / "personal.toml"
    personal.write_text("", encoding="utf-8")
    code, output = check_options(
        loop,
        capsys,
        "--assign",
        "inspect=claude-opus,codex",
        "--template",
        str(loop.DEFAULT_TEMPLATE),
        "--config",
        str(personal),
    )

    assert code == 2
    assert (output["status"], output["stage"]) == ("error", "config")
    assert "inspect=codex" in output["reason"]
    assert_untouched(isolated)


def test_check_options_keeps_codex_as_the_implementer(
    loop: Any, isolated: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """implement は writes=true の役割で、codex は workspace-write で起動する。止めない（#52）。"""
    personal = tmp_path / "personal.toml"
    personal.write_text("", encoding="utf-8")
    code, output = check_options(
        loop,
        capsys,
        "--assign",
        "implement=codex",
        "--template",
        str(loop.DEFAULT_TEMPLATE),
        "--config",
        str(personal),
    )

    assert code == 0
    assert output["assignments"]["fix"]["implement"]["agents"] == ["codex"]
    assert_untouched(isolated)


def test_check_options_reads_the_personal_config_when_not_given(
    loop: Any, isolated: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--config が無いときは、start と同じく既存の個人設定を重ねる（個人設定だけにある担当を使える）。"""
    personal = tmp_path / "personal.toml"
    personal.write_text(DSH_AGENT, encoding="utf-8")
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(personal))

    code, output = check_options(loop, capsys, "--assign", "implement=delta", "--template", str(isolated.template))

    assert code == 0, output
    assert output["options"]["config"] is None
    assert output["assignments"]["fix"]["implement"]["agents"] == ["delta"]


def test_check_options_does_not_touch_herdr(
    loop: Any,
    isolated: Any,
    fake_herdr: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w9:p9")

    code, _ = check_options(loop, capsys, "--template", str(isolated.template))

    assert code == 0
    assert fake_herdr.calls() == []


def test_check_options_does_not_accept_fresh_review(loop: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """--fresh-review は既存のレビュー結果を使うかどうかの指定で、PR の前の検査には意味が無い。"""
    with pytest.raises(SystemExit) as error:
        loop.main(["check-options", "--fresh-review"])

    assert error.value.code == 2
    assert capsys.readouterr().out == ""
