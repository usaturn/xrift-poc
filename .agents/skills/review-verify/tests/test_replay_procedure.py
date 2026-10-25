"""replay.md（過去の PR の再生評価の手順）の bash ブロックを、実際の親と同じ shallow な clone で実行する。

評価の結果が汚れるのを防ぐ境界（答えの隔離、答えを知る環境の検出、検索前の停止）だけを確かめる。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
PROCEDURE = SKILL_DIR / "evals" / "replay.md"


def _block(marker: str) -> str:
    text = PROCEDURE.read_text(encoding="utf-8")
    match = re.search(r"```bash\n(" + re.escape(marker) + r"\n.*?)\n```", text, re.DOTALL)
    assert match is not None, f"{marker!r} block not found in {PROCEDURE}"
    return match.group(1) + "\n"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _run(block: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], **env},
        text=True,
        capture_output=True,
        check=False,
    )


@dataclass(frozen=True)
class History:
    parent: Path
    to: str
    later: str
    evaluation: Path
    home: Path


@pytest.fixture
def history(tmp_path: Path) -> History:
    """答えを含む evals と、再生する `to` より後の commit を持つ履歴を、shallow な親として用意する。"""
    source = tmp_path / "source"
    _git(tmp_path, "init", "-q", "-b", "main", str(source))
    _write(
        source,
        {
            "README.md": "base\n",
            "tools/tool.py": "VALUE = 1\n",
            ".agents/skills/review-verify/SKILL.md": "# review-verify\n",
            ".agents/skills/review-verify/evals/validation.md": "#9001 の期待する検出は docs/x.rst:7\n",
            ".agents/skills/other/evals/fixtures/case.md": "#9001 の答え\n",
        },
    )
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "base")
    _write(source, {"tools/tool.py": "VALUE = 2\n"})
    _git(source, "commit", "-qam", "to")
    to = _git(source, "rev-parse", "HEAD")
    _write(source, {"tools/tool.py": "VALUE = 3  # LATER\n"})
    _git(source, "commit", "-qam", "later")
    later = _git(source, "rev-parse", "HEAD")
    parent = tmp_path / "parent"
    _git(tmp_path, "clone", "-q", "--depth", "2", f"file://{source}", str(parent))
    assert _git(parent, "rev-parse", "--is-shallow-repository") == "true"
    evaluation = tmp_path / "eval"
    evaluation.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return History(parent=parent, to=to, later=later, evaluation=evaluation, home=home)


def _fails(*args: str) -> bool:
    return subprocess.run(["git", *args], capture_output=True, check=False).returncode != 0


def test_isolate_builds_a_tool_root_without_evals_and_a_repository_ending_at_to(history: History) -> None:
    """
    隔離が evals/ を残すか to より後の commit を持ち込むと、
    評価担当が答えを読めるまま再生し、評価の結果が黙って汚れる。
    """
    completed = _run(
        _block("# review-verify replay: isolate"),
        {
            "HOME": str(history.home),
            "PARENT": str(history.parent),
            "TOOL_HEAD": history.later,
            "TO": history.to[:7],
            "EVAL": str(history.evaluation),
        },
    )

    assert completed.returncode == 0, completed.stderr
    toolroot = history.evaluation / "toolroot"
    assert (toolroot / ".agents/skills/review-verify/SKILL.md").is_file()
    assert "LATER" in (toolroot / "tools/tool.py").read_text(encoding="utf-8")
    assert not [path for path in toolroot.rglob("evals") if path.is_dir()]
    assert not (history.evaluation / "toolroot.tar").exists()
    repo = history.evaluation / "repo"
    assert _git(repo, "rev-parse", "HEAD") == history.to
    assert _fails("-C", str(repo), "cat-file", "-e", f"{history.later}^{{commit}}")


def _context(tmp_path: Path, patterns: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "answer-patterns.txt").write_text(patterns, encoding="utf-8")
    evaluation = tmp_path / "eval"
    evaluation.mkdir()
    _write(evaluation / "toolroot", {".agents/review-records.md": "記入例: 範囲外の話\n"})
    return {"HOME": str(home), "ANSWERS": str(answers), "EVAL": str(evaluation)}


def test_context_check_reports_a_conclusion_in_the_memory_index(tmp_path: Path) -> None:
    """検索の対象から自動メモリが外れると、記憶にある結論を見逃して当たりなしを返し、答えを知る環境で再生する。"""
    env = _context(tmp_path, "#9001\r\n\r\n  \ndocs/x.rst:7\n")
    memory = Path(env["HOME"]) / ".claude/projects/-work-repo/memory/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- [pr-9001-status](pr-9001-status.md) — PR #9001 の指摘は docs/x.rst:7\n", encoding="utf-8")

    completed = _run(_block("# review-verify replay: context check"), env)

    assert completed.returncode == 0, completed.stderr
    assert str(memory) in completed.stdout
    assert "記入例" not in completed.stdout


def test_context_check_reports_a_conclusion_in_the_tool_root(tmp_path: Path) -> None:
    """検索の対象から tool root が外れると、tool root の文書にある結論を見逃して当たりなしを返す。"""
    env = _context(tmp_path, "再生する PR の結論\n")
    _write(Path(env["EVAL"]) / "toolroot", {".agents/review-records.md": "記入例: 再生する PR の結論\n"})

    completed = _run(_block("# review-verify replay: context check"), env)

    assert completed.returncode == 0, completed.stderr
    assert str(Path(env["EVAL"]) / "toolroot/.agents/review-records.md") in completed.stdout


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # 一覧から外れると、set -u の終了コード 1 が当たりなしと区別できない。
        pytest.param("ANSWERS", None, id="ANSWERS-unset"),
        pytest.param("EVAL", None, id="EVAL-unset"),
        # 空のままだと、記憶を検索しないまま、エラーを出さずに当たりなしを返す。
        pytest.param("HOME", "", id="HOME-empty"),
    ],
)
def test_context_check_without_a_required_variable_stops_before_searching(
    tmp_path: Path, name: str, value: str | None
) -> None:
    """必要な変数が無いまま検索に進むと、当たりなしと区別できない結果を返し、答えを知る環境で再生する（ケースごとの損害はコメント）。"""
    env = _context(tmp_path, "記入例\n")
    if value is None:
        del env[name]
    else:
        env[name] = value

    completed = _run(_block("# review-verify replay: context check"), env, cwd=tmp_path)

    assert completed.returncode not in (0, 1), completed.stderr  # 当たりあり（0）・当たりなし（1）として返さない
    assert completed.stdout == ""


def test_leak_scan_reports_traces_of_answer_material(tmp_path: Path) -> None:
    """実行後の検索が答えの資料を読んだ跡を見逃すと、汚れた実行を採点に使う。"""
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "leak-patterns.txt").write_text("ground-truth\nevals/\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _write(run_dir, {"attempt-1/stdout.log": "読んだ: /work/answers/ground-truth/9001.md\n"})
    env = {"ANSWERS": str(answers), "RUN_DIR": str(run_dir)}

    found = _run(_block("# review-verify replay: leak scan"), env)

    assert found.returncode == 0, found.stderr
    assert "ground-truth/9001.md" in found.stdout


@pytest.mark.parametrize(
    "name",
    [
        # 一覧から外れると、set -u の終了コード 1 が当たりなしと区別できない。
        pytest.param("ANSWERS", id="ANSWERS-unset"),
        pytest.param("RUN_DIR", id="RUN_DIR-unset"),
    ],
)
def test_leak_scan_without_a_required_variable_stops_before_searching(tmp_path: Path, name: str) -> None:
    """必要な変数が無いまま検索に進むと、当たりなしと区別できない結果を返し、汚れた実行を採点に使う（ケースごとの損害はコメント）。"""
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "leak-patterns.txt").write_text("ground-truth\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _write(run_dir, {"attempt-1/stdout.log": "読んだ: /work/answers/ground-truth/9001.md\n"})
    env = {"ANSWERS": str(answers), "RUN_DIR": str(run_dir)}
    del env[name]

    completed = _run(_block("# review-verify replay: leak scan"), env, cwd=tmp_path)

    assert completed.returncode not in (0, 1), completed.stderr  # 当たりあり（0）・当たりなし（1）として返さない
    assert completed.stdout == ""
