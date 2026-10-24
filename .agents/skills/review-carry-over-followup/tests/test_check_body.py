"""手順 5 の投稿前の検査（``check_body``）を、SKILL.md のブロックのまま bash と zsh で実行する。

投稿は検査の成否で分岐するので、``if``・``&&``・``||``・``!``・条件から呼ばれた関数のどの呼び方でも、
検査のどれかが失敗した本文を通してはならない（関数の中の ``set -e`` は条件の中では効かない）。
scanner の検出・失敗・不在と rg の失敗は、``PATH`` を一時ディレクトリだけにして fake のコマンドで作る。
"""

from __future__ import annotations

import os
import random
import re
import shutil
import string
from pathlib import Path
from typing import Any

import pytest

from tools.test_support.texts import FINDING_CONTENT

SHELLS = ("bash", "zsh")
# 投稿に進むときだけ ACCEPTED を出す。END はスクリプトが最後まで実行されたことの印
INVOCATIONS = {
    "direct": 'check_body "$BODY"\nrc=$?\nif [ "$rc" -eq 0 ]; then echo ACCEPTED; fi',
    "if": 'if check_body "$BODY"; then echo ACCEPTED; fi',
    "and": 'check_body "$BODY" && echo ACCEPTED',
    "or-exit": '( check_body "$BODY" || exit 1; echo ACCEPTED )',
    "not": 'if ! check_body "$BODY"; then echo REJECTED; else echo ACCEPTED; fi',
    "nested": 'gate() { check_body "$1"; }\nif gate "$BODY"; then echo ACCEPTED; fi',
}
CLEAN = (
    "<!-- review-carry-over-followup:v1 -->\n"
    "## 持ち越し候補の再調査（claude-opus-5-5）\n\n"
    "- 確かめた事実: `.agents/skills/review-carry-over-followup/SKILL.md:130` を `$TMP` の一時 clone で実行した\n\n"
    "```bash\n"
    'check_body "$SESSION/followup.md" || echo 投稿しない\n'
    "```\n\n" + FINDING_CONTENT
)
# 本文に足す内容、fake の Gitleaks の終了コード（None は不在）、rg の置き方、投稿に進んでよいか
CASES: dict[str, tuple[str | None, int | None, str, bool]] = {
    "clean": ("", 0, "real", True),
    "secret-detected": ("", 1, "real", False),
    "scanner-error": ("", 2, "real", False),
    "scanner-missing": ("", None, "real", False),
    "body-missing": (None, 0, "real", False),
    "planning-marker": ("\n<!-- review-fix-planning:state -->\n", 0, "real", False),
    "records-marker": ("\n<!-- review-records:v1 -->\n", 0, "real", False),
    "absolute-path": ("\nログ: /home/someone/project/run.log\n", 0, "real", False),
    "too-long": ("a" * 65537, 0, "real", False),
    "rg-error": ("", 0, "error", False),
    "rg-missing": ("", 0, "missing", False),
}


def fake_bin(tmp_path: Path, scanner_exit: int | None, rg: str) -> Path:
    """``check_body`` が使うコマンドだけを置いたディレクトリ。Gitleaks と rg は指定に応じて fake にするか置かない。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("env", "mktemp", "rm", "wc", *(("rg",) if rg == "real" else ())):
        found = shutil.which(name)
        assert found is not None, f"{name} が PATH に無い"
        (bin_dir / name).symlink_to(found)
    fakes = {"gitleaks": scanner_exit, "rg": 2 if rg == "error" else None}
    for name, code in fakes.items():
        if code is not None:
            (bin_dir / name).write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
            (bin_dir / name).chmod(0o755)
    return bin_dir


def run_check(
    skill_shell: Any, tmp_path: Path, shell: str, invocation: str, body: str | None, env: dict[str, str]
) -> tuple[bool, Any]:
    body_file = tmp_path / "followup.md"
    if body is not None:
        body_file.write_text(body, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = f"{skill_shell.block('check body')}\n{INVOCATIONS[invocation]}\necho END\n"
    result = skill_shell.run(
        shell, script, cwd=tmp_path, env={**env, "BODY": str(body_file), "TMPDIR": str(scratch)}, replace_env=True
    )
    assert result.stdout.splitlines()[-1:] == ["END"], result.stderr
    # 検査の一時ディレクトリは、どの結果でも消える
    assert list(scratch.iterdir()) == []
    return "ACCEPTED" in result.stdout.splitlines(), result


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("invocation", INVOCATIONS)
@pytest.mark.parametrize("shell", SHELLS)
def test_check_body_rejects_failures_in_every_invocation(
    skill_shell: Any, tmp_path: Path, shell: str, invocation: str, case: str
) -> None:
    extra, scanner_exit, rg, expected = CASES[case]
    bin_dir = fake_bin(tmp_path, scanner_exit, rg)
    body = None if extra is None else CLEAN + extra
    accepted, result = run_check(
        skill_shell, tmp_path, shell, invocation, body, {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    )
    assert accepted is expected, result.stderr


def secret_like_token() -> str:
    # Gitleaks の既定の github-pat 規則に当たる形。リポジトリにリテラルを置かないよう、実行時に組み立てる。
    rng = random.Random(62)
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


@pytest.mark.parametrize("form", ["assignment", "fenced", "none"])
@pytest.mark.parametrize("shell", SHELLS)
def test_check_body_with_the_real_gitleaks(skill_shell: Any, tmp_path: Path, shell: str, form: str) -> None:
    if shutil.which("gitleaks") is None:
        pytest.fail("the Dev Container and CI install the approved Gitleaks pin; this test needs it on PATH")
    token = secret_like_token()
    leak = {
        "assignment": f"\nGH_TOKEN={token}\n",
        "fenced": f'\n再現に使った設定:\n\n```bash\nexport GH_TOKEN="{token}"\n```\n',
        "none": "",
    }[form]
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path))}
    accepted, result = run_check(skill_shell, tmp_path, shell, "if", CLEAN + leak, env)
    assert accepted is (form == "none"), result.stderr
    if form != "none":
        found = re.search(r"leaks found: (\d+)", result.stderr)
        assert found is not None and int(found.group(1)) >= 1, result.stderr
    assert token not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_rendered_skill_checks_the_supplied_body_even_if_second_skill_argument_is_a_file(
    skill_shell: Any, tmp_path: Path, shell: str
) -> None:
    """Skill 引数の置換で別ファイルを検査すると、秘密を含む本文が黙って投稿される。"""
    harmless = tmp_path / "existing candidates.md"
    harmless.write_text("公開できる候補\n", encoding="utf-8")
    body = tmp_path / "投稿 本文.md"
    token = secret_like_token()
    body.write_text(f"GH_TOKEN={token}\n", encoding="utf-8")
    rendered = re.sub(
        r"\$[1-9]",
        lambda match: str(harmless) if match.group() == "$1" else "unused",
        skill_shell.block("check body"),
    )
    result = skill_shell.run(
        shell,
        f'{rendered}\nif check_body "$BODY"; then echo ACCEPTED; else echo REJECTED; fi\n',
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "BODY": str(body)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["REJECTED"]
    assert token not in result.stdout + result.stderr
