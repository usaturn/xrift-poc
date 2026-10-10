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
    "text",
    [
        'mktemp -d "${TMPDIR:-/tmp}/review-fix-planning.XXXXXX"',
        "`/tmp/pr-review-<PR_NUMBER>-<ランダム値>/worktree`",
        "Edit(//tmp/pr-review-*/review-backup/**)",
        "`TMPDIR` が `/tmp` 以外",
        "`${TMPDIR:-/tmp}` 直下へ",
        'ls -t "$(uv run --no-project python .agents/scripts/agent_tmp.py root)/tmp/docs_draft/claude_handoffs/"',
        '"$ROOT/tmp/agents/x"',
        "Edit(tmp/agents/pr-review-scratch-*/**)",
        "Edit(//tmp/xrift-poc-pr-loop-*/worktree/**)",
    ],
)
def test_scan_accepts_approved_tmp_usage(text: str) -> None:
    assert disallowed_tmp_paths(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("> /tmp/gate-a.out 2> /tmp/gate-a.err", ["/tmp/gate-a.out", "/tmp/gate-a.err"]),
        (
            'mktemp -d "${TMPDIR:-/tmp}/xrift-poc-pr-review-scratch-1-XXXXXXXX"',
            ["${TMPDIR:-/tmp}/xrift-poc-pr-review-scratch-1-XXXXXXXX"],
        ),
        ("Edit(//tmp/notes/**)", ["//tmp/notes/**"]),
        ("ログは/tmp/foo.log に置く", ["/tmp/foo.log"]),
        ('"/tmp/release-notes.md"', ["/tmp/release-notes.md"]),
    ],
)
def test_scan_rejects_unapproved_tmp_paths(text: str, expected: list[str]) -> None:
    assert disallowed_tmp_paths(text) == expected


def test_scan_covers_every_skill_file() -> None:
    # symlink のスキル（`.claude/skills/<name>` → `.agents/skills/<name>`）も実体で数える。
    skills = {path.resolve() for skill_root in SKILL_ROOTS for path in skill_root.glob("*/SKILL.md")}

    assert skills
    assert skills <= set(instruction_files())


def test_skill_instructions_use_only_approved_tmp_locations() -> None:
    violations = {
        path.relative_to(ROOT).as_posix(): found
        for path in instruction_files()
        if (found := disallowed_tmp_paths(path.read_text(encoding="utf-8")))
    }

    assert violations == {}
