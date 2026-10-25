"""手順 5 の投稿前の検査（``check_body``）を、SKILL.md のブロックのまま bash と zsh で実行する。

投稿は検査の成否で分岐するので、条件の中から呼ばれても、検査のどれかが失敗した本文を通してはならない
（関数の中の ``set -e`` は条件の中では効かない）。条件の中で ``set -e`` が効かないのは ``if``・``&&``・``||``・
``!``・入れ子の関数で同じなので、``if`` から呼ぶ形で確かめる。
scanner の不在と rg の不在は、``PATH`` を一時ディレクトリだけにして作る。
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

CLEAN = (
    "<!-- review-carry-over-followup:v1 -->\n"
    "## 持ち越し候補の再調査（claude-opus-5-5）\n\n"
    "- 確かめた事実: `.agents/skills/review-carry-over-followup/SKILL.md:130` を `$TMP` の一時 clone で実行した\n\n"
    "```bash\n"
    'check_body "$SESSION/followup.md" || echo 投稿しない\n'
    "```\n\n" + FINDING_CONTENT
)
# 本文に足す内容、fake の Gitleaks の終了コード（None は不在）、rg を置くか、投稿に進んでよいか
CASES: dict[str, tuple[str, int | None, bool, bool]] = {
    "clean": ("", 0, True, True),
    "scanner-missing": ("", None, True, False),
    "planning-marker": ("\n<!-- review-fix-planning:state -->\n", 0, True, False),
    "records-marker": ("\n<!-- review-records:v1 -->\n", 0, True, False),
    "absolute-path": ("\nログ: /home/someone/project/run.log\n", 0, True, False),
    "rg-missing": ("", 0, False, False),
    "too-long": ("a" * 65537, 0, True, False),
}
# zsh は rg の段と対照だけを確かめる（Gitleaks の段は、実物の Gitleaks のテストが zsh でも確かめる）
COMBINATIONS = [("bash", case) for case in CASES] + [("zsh", "clean"), ("zsh", "absolute-path")]


def fake_bin(tmp_path: Path, scanner_exit: int | None, rg: bool) -> Path:
    """``check_body`` が使うコマンドだけを置いたディレクトリ。Gitleaks は fake か不在、rg は実物か不在にする。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("env", "mktemp", "rm", "wc", *(("rg",) if rg else ())):
        found = shutil.which(name)
        assert found is not None, f"{name} が PATH に無い"
        (bin_dir / name).symlink_to(found)
    if scanner_exit is not None:
        (bin_dir / "gitleaks").write_text(f"#!/bin/sh\nexit {scanner_exit}\n", encoding="utf-8")
        (bin_dir / "gitleaks").chmod(0o755)
    return bin_dir


def run_check(skill_shell: Any, tmp_path: Path, shell: str, body: str, env: dict[str, str]) -> tuple[bool, Any]:
    body_file = tmp_path / "followup.md"
    body_file.write_text(body, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = f'{skill_shell.block("check body")}\nif check_body "$BODY"; then echo ACCEPTED; fi\necho END\n'
    result = skill_shell.run(
        shell, script, cwd=tmp_path, env={**env, "BODY": str(body_file), "TMPDIR": str(scratch)}, replace_env=True
    )
    # 外側のスクリプトが途中で止まっても ACCEPTED は出ないので、最後まで実行されたことを確かめる
    assert result.stdout.splitlines()[-1:] == ["END"], result.stderr
    return "ACCEPTED" in result.stdout.splitlines(), result


@pytest.mark.parametrize(("shell", "case"), [pytest.param(*pair, id="-".join(pair)) for pair in COMBINATIONS])
def test_check_body_rejects_failures(skill_shell: Any, tmp_path: Path, shell: str, case: str) -> None:
    """検査の段が失敗を通すと、記録のマーカー・絶対パスを含む本文や未検査の本文が公開の PR・Issue に黙って載る。

    長すぎる本文は GitHub が拒否するが、手順 6 の先の書き込み（新しい Issue など）が済んだ後になる。
    ``clean`` は、常に拒否する実装（fake の ``PATH`` の不足を含む）でも拒否のケースが通るのを防ぐ対照である。
    """
    extra, scanner_exit, rg, expected = CASES[case]
    bin_dir = fake_bin(tmp_path, scanner_exit, rg)
    accepted, result = run_check(
        skill_shell, tmp_path, shell, CLEAN + extra, {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    )
    assert accepted is expected, result.stderr


def secret_like_token() -> str:
    # Gitleaks の既定の github-pat 規則に当たる形。リポジトリにリテラルを置かないよう、実行時に組み立てる。
    rng = random.Random(62)
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_check_body_with_the_real_gitleaks(skill_shell: Any, tmp_path: Path, shell: str) -> None:
    """実物の Gitleaks の呼び方（既定の規則・allow の無視・伏せ字など）が壊れると、秘密が黙って公開される。"""
    if shutil.which("gitleaks") is None:
        pytest.fail("the Dev Container and CI install the approved Gitleaks pin; this test needs it on PATH")
    token = secret_like_token()
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path))}
    accepted, result = run_check(skill_shell, tmp_path, shell, CLEAN + f"\nGH_TOKEN={token}\n", env)
    assert accepted is False, result.stderr
    # 検出による拒否であること（検査を実行できずに拒否した空振りでないこと）
    found = re.search(r"leaks found: (\d+)", result.stderr)
    assert found is not None and int(found.group(1)) >= 1, result.stderr
    assert token not in result.stdout + result.stderr


def test_rendered_skill_checks_the_supplied_body_even_if_second_skill_argument_is_a_file(
    skill_shell: Any, tmp_path: Path
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
        "bash",
        f'{rendered}\nif check_body "$BODY"; then echo ACCEPTED; else echo REJECTED; fi\n',
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "BODY": str(body)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["REJECTED"]
    assert token not in result.stdout + result.stderr
