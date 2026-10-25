"""Issue #333: スキルの指示文書が `/tmp` 直下に固定名のファイルを作らせないこと。

AGENTS.md「コミットしないファイルの置き場所」の安全規則（`/tmp` を使う場合は一意の
ディレクトリを作る）を、すべてのスキルの `SKILL.md` と `references/*.md` に課す。
特定の文を固定するのではなく、指示文書に現れる `/tmp/...` のパスを許可した使い捨ての
場所だけに限る。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOTS = (ROOT / ".agents/skills", ROOT / ".claude/skills", ROOT / ".devin/skills")
# `/tmp` の直前が ASCII の識別子・`.`・`)`・`}`・`]`・`-` なら別のパスの一部とみなす
# （例: `"$(... root)/tmp/agents/"`）。
# `\w` は日本語にも一致するので使わない。日本語の直後の `/tmp/...` は検査対象にする。
TMP_PATH = re.compile(r"(?<![A-Za-z0-9_.)}\]-])(?:\$\{TMPDIR:-/tmp\}|/?/tmp)(?P<rest>/[^\s`\"')]*)?")
# AGENTS.md「コミットしないファイルの置き場所」が `/tmp` に置くことを認める使い捨ての場所。
# 表の `/tmp` の行を変えるときは、この一覧も合わせて更新する。
ALLOWED_TMP_PREFIXES = ("/pr-review-", "/review-fix-planning.", "/xrift-poc-pr-loop-")


def instruction_files() -> list[Path]:
    """エージェントが従う指示文書（各スキルの SKILL.md と references/*.md）。"""
    files: set[Path] = set()
    for skill_root in SKILL_ROOTS:
        for path in skill_root.rglob("*.md"):
            if path.name == "SKILL.md" or path.parent.name == "references":
                files.add(path.resolve())
    return sorted(files)


def disallowed_tmp_paths(text: str) -> list[str]:
    """許可リストに無い `/tmp/...` のパスを、出現順に返す。"""
    found: list[str] = []
    for match in TMP_PATH.finditer(text):
        rest = match.group("rest")
        if rest is None or rest.startswith(ALLOWED_TMP_PREFIXES):
            continue
        found.append(match.group(0))
    return found


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # リダイレクトの基本の形。直前の文字の検査に空白を足すと見落とす
        pytest.param("> /tmp/gate-a.out 2> /tmp/gate-a.err", ["/tmp/gate-a.out", "/tmp/gate-a.err"], id="redirect"),
        # ${TMPDIR:-/tmp} の分岐。外すと ${TMPDIR:-/tmp}/固定名 を見落とす
        pytest.param(
            'mktemp -d "${TMPDIR:-/tmp}/xrift-poc-pr-review-scratch-1-XXXXXXXX"',
            ["${TMPDIR:-/tmp}/xrift-poc-pr-review-scratch-1-XXXXXXXX"],
            id="tmpdir",
        ),
        # 日本語の直後。直前の文字の検査に \w を使うと見落とす
        pytest.param("ログは/tmp/foo.log に置く", ["/tmp/foo.log"], id="after-japanese"),
        # 引用符で囲んだ固定名。直前の文字の検査に " を足すと見落とす
        pytest.param('"/tmp/release-notes.md"', ["/tmp/release-notes.md"], id="quoted"),
    ],
)
def test_scan_rejects_unapproved_tmp_paths(text: str, expected: list[str]) -> None:
    """正規表現が見落とす向きに壊れると、固定名の /tmp のパスを見つけられないまま検査が通る。"""
    assert disallowed_tmp_paths(text) == expected


def test_scan_covers_every_skill_file() -> None:
    """検査の対象の集め方が壊れて文書を読まなくなると、下の検査が黙って空のまま通る。"""
    # symlink のスキル（`.claude/skills/<name>` → `.agents/skills/<name>`）も実体で数える。
    skills = {path.resolve() for skill_root in SKILL_ROOTS for path in skill_root.glob("*/SKILL.md")}

    assert skills
    assert skills <= set(instruction_files())


def test_skill_instructions_use_only_approved_tmp_locations() -> None:
    """指示文書が /tmp の固定名を使わせると、並行するセッションが互いの出力を黙って上書きし、他人の結果を読む。"""
    violations = {
        path.relative_to(ROOT).as_posix(): found
        for path in instruction_files()
        if (found := disallowed_tmp_paths(path.read_text(encoding="utf-8")))
    }

    assert violations == {}
