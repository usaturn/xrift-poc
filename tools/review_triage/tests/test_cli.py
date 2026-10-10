"""cli（#389）のオフライン試験。外部ネットワーク・実キー・実データ（reviews/）を使わない。"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from pydantic import TypeAdapter

from tools.review_triage.cli import (
    EXIT_OK,
    EXIT_USAGE,
    EvidenceNote,
    GitError,
    GitSource,
    LocationRef,
    RunRecord,
    StateRecord,
    build_evidence,
    candidate_pairs,
    location_refs,
    main,
    request_sha256,
)
from tools.review_triage.client import API_KEY_ENV, SystemOneClient, connectivity_request
from tools.review_triage.inputs import load_reviews
from tools.review_triage.models import (
    API_ENDPOINT,
    ClientOutcome,
    ErrorKind,
    HttpRequest,
    HttpResponse,
    Proposal,
    ReportStatus,
    Repository,
    ReviewBatch,
    ReviewFinding,
    Transport,
    TriageReport,
)
from tools.review_triage.report import render_report

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
SOURCE = FIXTURES / "source"
REVIEWS = FIXTURES / "reviews" / "pr-9001-synthetic"
# 実データのディレクトリ名（reviews/#NNN-branch）と同じく `#` を含む名前で試す
REVIEW_DIR_NAME = "#9001-feat-synthetic"
FINDING_IDS = (
    "alpha-model/active/M-1",
    "alpha-model/active/L-1",
    "alpha-model/active/I-1",
    "alpha-model/resolved/I-2",
    "beta-model/active/M-1",
    "beta-model/active/L-1",
    "beta-model/active/I-1",
    "gamma-model/active/L-1",
    "gamma-model/active/L-2",
)
ACTIVE_IDS = tuple(finding_id for finding_id in FINDING_IDS if "/active/" in finding_id)
KEY = "ts-cli-test-key-SENTINEL-5c1e"  # gitleaks:allow
SECRET_LINE = "-----BEGIN TEST PRIVATE KEY-----"
FIXED_NOW = datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
AWAITING_FINDINGS = {
    "alpha-model/active/M-1",
    "alpha-model/active/L-1",
    "beta-model/active/M-1",
    "beta-model/active/L-1",
    "gamma-model/active/L-1",
}


def _render_reviews(tmp_path: Path, head: str, *, name: str = REVIEW_DIR_NAME) -> Path:
    """fixture のレビューの ``{{HEAD}}`` を ``head`` の先頭 12 桁に置き換えて tmp へ書く。"""
    target = tmp_path / "reviews" / name
    target.mkdir(parents=True)
    for source in sorted(REVIEWS.iterdir()):
        text = source.read_text(encoding="utf-8").replace("{{HEAD}}", head[:12])
        target.joinpath(source.name).write_text(text, encoding="utf-8")
    return target


_FIXTURE_DATE = "2026-09-01T00:00:00Z"


def _fixture_git(*args: str, cwd: Path) -> str:
    """試験の準備用の git。利用者の設定と GIT_ 変数を読まず、作者と日時を固定する。"""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env |= {
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": _FIXTURE_DATE,
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_DATE": _FIXTURE_DATE,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    completed = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _commit(repo: Path, message: str = "fixture") -> str:
    _fixture_git("add", "-A", cwd=repo)
    _fixture_git("commit", "-q", "--allow-empty", "-m", message, cwd=repo)
    return _fixture_git("rev-parse", "HEAD", cwd=repo)


@dataclass(frozen=True)
class Workspace:
    """合成ソースの git リポジトリと、その HEAD を指すレビューディレクトリ。"""

    tmp: Path
    repo: Path
    head: str
    reviews: Path


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    repo = tmp_path / "repo"
    shutil.copytree(SOURCE, repo)
    _fixture_git("init", "-q", "-b", "main", cwd=repo)
    head = _commit(repo)
    return Workspace(tmp=tmp_path, repo=repo, head=head, reviews=_render_reviews(tmp_path, head))


@pytest.fixture
def odd_ws(ws: Workspace) -> Workspace:
    """symlink・UTF-8 でないファイル・`#` を含むパス・同名ファイル・CRLF・長いファイルを足した commit。"""
    src = ws.repo / "src"
    os.symlink("calc.js", src / "link.js")
    (src / "latin1.txt").write_bytes(b"caf\xe9\n")
    (src / "has#hash.js").write_text("x\n", encoding="utf-8")
    (src / "crlf.js").write_bytes(b"first\r\nsecond\r\nthird\r\n")
    for name in ("a", "b"):
        (ws.repo / name).mkdir()
        (ws.repo / name / "x.js").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (ws.repo / "docs").mkdir()
    long_text = "".join(f"line {number}\n" for number in range(1, 201))
    (ws.repo / "docs" / "long.txt").write_text(long_text, encoding="utf-8")
    head = _commit(ws.repo, "odd files")
    return Workspace(tmp=ws.tmp, repo=ws.repo, head=head, reviews=ws.reviews)


def _batch(ws: Workspace) -> ReviewBatch:
    return load_reviews(ws.reviews, repository=Repository.PARENT, reviewed_head=ws.head)


def _numbered(path: Path, start: int, end: int) -> str:
    """``path`` の ``start``〜``end`` 行を、CLI の抜粋と同じ行番号付きの形にする。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    return "\n".join(f"{number:>6}  {lines[number - 1]}" for number in range(start, end + 1))


def _with_location(ws: Workspace, location: str) -> ReviewFinding:
    """fixture の指摘の ``location`` だけを差し替える。"""
    return _batch(ws).finding("alpha-model/active/L-1").model_copy(update={"location": location})


# --- location_refs -----------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("`src/calc.js:13`", [("src/calc.js", 13, 13)]),
        ("`src/calc.js:10-12`", [("src/calc.js", 10, 12)]),
        ("`src/calc.js:10–12`（関数）", [("src/calc.js", 10, 12)]),
        ("`src/calc.js:L10-L20`", [("src/calc.js", 10, 20)]),
        ("`a.py:5〜7`", [("a.py", 5, 7)]),
        ("`docs/configuration.rst:481`–`483`", [("docs/configuration.rst", 481, 483)]),
        ("`src/a.py:579`・`:586`・`:614`", [("src/a.py", 579, 579), ("", 586, 586), ("", 614, 614)]),
        ("`SKILL.md:25`, 同 `:29`", [("SKILL.md", 25, 25), ("", 29, 29)]),
        ("`src/calc.js:13`、`calc.js:14`（`clamp()` の分岐）", [("src/calc.js", 13, 13), ("calc.js", 14, 14)]),
        ("`src/calc.js:12-3`", []),
        ("`src/calc.js:0`", []),
        ("`src/calc.js:1234567890`", []),
        ("`src/calc.js`（ファイル全体）", []),
        ("`https://example.invalid:8443/x`", []),
        ("`resolve_variant_generator` の分岐", []),
        ("src/calc.js:13（バッククォートなし）", []),
        ("", []),
    ],
)
def test_location_refs(location: str, expected: list[tuple[str, int, int]]) -> None:
    assert location_refs(location) == tuple(LocationRef(*ref) for ref in expected)


def test_fixture_reviews_parse_cleanly(tmp_path: Path) -> None:
    """fixture は input の規則どおりに読め、anomaly と未解釈ブロックを出さない。"""
    head = "0123456789ab"
    batch = load_reviews(_render_reviews(tmp_path, head), repository=Repository.PARENT, reviewed_head=head)
    assert batch.anomalies == ()
    assert batch.unparsed == ()
    assert tuple(finding.finding_id for finding in batch.findings) == FINDING_IDS
    assert tuple(finding.finding_id for finding in batch.active_findings()) == ACTIVE_IDS


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI の試験では、名前解決と接続をすべて試験の失敗にする。"""

    def refuse(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("network access attempted in a CLI test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class RefusingTransport:
    """呼ばれたら試験を失敗させる transport（dry-run と replay が client を使わないことの確認）。"""

    def send(self, request: HttpRequest) -> HttpResponse:
        raise AssertionError("the transport must not be used")


class FakeTransport:
    """質問の型に応じた回答を返す transport。受け取った request 本文を記録する。

    ``failure`` を渡すと、送信のたびにその例外を送出する（ネットワーク障害の再現）。
    """

    def __init__(self, *, failure: BaseException | None = None, model: str = "jev-2026-09-01") -> None:
        self.bodies: list[bytes] = []
        self._failure = failure
        self._model = model

    @property
    def hashes(self) -> list[str]:
        return [hashlib.sha256(body).hexdigest() for body in self.bodies]

    def send(self, request: HttpRequest) -> HttpResponse:
        self.bodies.append(request.body)
        if self._failure is not None:
            raise self._failure
        payload: dict[str, Any] = json.loads(request.body)
        answers: dict[str, object] = {}
        for question_id, question in payload["questions"].items():
            if question["type"] == "choice":
                answers[question_id] = {"type": "choice", "choice": "fix_now", "confidence": 0.97}
            else:
                answers[question_id] = {"type": "noul", "noul": 0.99}
        body = json.dumps(
            {"model": self._model, "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 0}}
        ).encode()
        headers = {
            "content-type": "application/json",
            "x-typesafe-request-id": "req-" + hashlib.sha256(request.body).hexdigest()[:12],
        }
        return HttpResponse(status=200, headers=headers, body=body, final_url=API_ENDPOINT)


def _outcomes(out: Path) -> list[ClientOutcome]:
    return TypeAdapter(list[ClientOutcome]).validate_json((out / "outcomes.json").read_bytes())


def _dry_and_allow(ws: Workspace) -> Path:
    """dry-run を ``dry`` へ出し、許可待ちの state をすべて許可したファイルを返す。"""
    assert _run(_args(ws, "dry")) == EXIT_OK
    return _allow_file(ws, ws.tmp / "dry")


def _args(ws: Workspace, out: str, *extra: str) -> list[str]:
    return [
        "--review-dir",
        os.fspath(ws.reviews),
        "--repository",
        Repository.PARENT.value,
        "--reviewed-head",
        ws.head[:12],
        "--output-dir",
        os.fspath(ws.tmp / out),
        "--source-repo",
        os.fspath(ws.repo),
        *extra,
    ]


def _run(argv: Sequence[str], *, environ: Mapping[str, str] | None = None, transport: Transport | None = None) -> int:
    return main(argv, environ={} if environ is None else environ, transport=transport, now=lambda: FIXED_NOW)


def _replace(argv: list[str], option: str, value: str) -> list[str]:
    result = list(argv)
    result[result.index(option) + 1] = value
    return result


def _records(out: Path) -> list[StateRecord]:
    return TypeAdapter(list[StateRecord]).validate_json((out / "states.json").read_bytes())


def _report(out: Path) -> TriageReport:
    return TriageReport.model_validate_json((out / "report.json").read_bytes())


def _run_record(out: Path) -> RunRecord:
    return RunRecord.model_validate_json((out / "run.json").read_bytes())


def _allow_file(
    ws: Workspace,
    dry_out: Path,
    *,
    name: str = "allow.txt",
    skip: frozenset[str] = frozenset(),
    bom: bool = False,
    crlf: bool = False,
) -> Path:
    """dry-run の states.json から、許可待ちの state のハッシュを許可ファイルに書く。

    ``skip`` の指摘を含む state は書かない。
    """
    lines = ["# 許可した state"]
    for record in _records(dry_out):
        if record.awaiting_approval and not skip & set(record.finding_ids):
            lines.append(f"{record.content_sha256}  # {', '.join(record.finding_ids)}")
    newline = "\r\n" if crlf else "\n"
    text = ("﻿" if bom else "") + newline.join(lines) + newline
    path = ws.tmp / name
    path.write_bytes(text.encode("utf-8"))
    return path


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    """ディレクトリ配下の全エントリの (サイズ, mtime_ns, 内容の SHA-256)。"""
    state: dict[str, tuple[int, int, str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            regular = path.is_file() and not path.is_symlink()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if regular else ""
            state[os.fspath(path)] = (info.st_size, info.st_mtime_ns, digest)
    return state


def _record_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """``subprocess.run`` の呼び出しの argv を記録する（実行はそのまま行う）。"""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording(
        args: list[str], *, stdin: int, capture_output: bool, env: dict[str, str], timeout: float, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(args))
        return real_run(args, stdin=stdin, capture_output=capture_output, env=env, timeout=timeout, check=check)

    monkeypatch.setattr(subprocess, "run", recording)
    return calls


# --- GitSource ---------------------------------------------------------------


def test_git_source_resolves_short_and_full_heads(ws: Workspace) -> None:
    source = GitSource(ws.repo)
    assert source.resolve_commit(ws.head[:7]) == ws.head
    assert source.resolve_commit(ws.head) == ws.head


@pytest.mark.parametrize("sha", ["0123456789abcdef", "XYZ", "--help", "", "abc"])
def test_git_source_rejects_unknown_or_malformed_commits(ws: Workspace, sha: str) -> None:
    with pytest.raises(GitError):
        GitSource(ws.repo).resolve_commit(sha)


def test_git_source_rejects_a_ref_that_shadows_a_short_head(ws: Workspace) -> None:
    """git は短縮 SHA より同名の ref を優先するため、別の commit を指す ref があれば解決しない。"""
    other = _commit(ws.repo, "other")
    _fixture_git("branch", ws.head[:7], other, cwd=ws.repo)
    source = GitSource(ws.repo)
    with pytest.raises(GitError, match="another commit"):
        source.resolve_commit(ws.head[:7])
    assert source.resolve_commit(ws.head) == ws.head


def test_git_source_rejects_a_directory_outside_git(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(GitError):
        GitSource(tmp_path / "plain")


def test_git_source_reads_paths_from_the_repository_root(ws: Workspace) -> None:
    source = GitSource(ws.repo / "src")
    assert source.root.resolve() == ws.repo.resolve()
    expected = (ws.repo / "src" / "calc.js").read_text(encoding="utf-8").split("\n")[:-1]
    assert source.blob_lines(ws.head, "src/calc.js") == tuple(expected)


# --- build_evidence ----------------------------------------------------------


def test_evidence_numbers_lines_and_adds_context(ws: Workspace) -> None:
    finding = _batch(ws).finding("alpha-model/active/L-1")
    evidence = build_evidence(finding, GitSource(ws.repo), head=ws.head)
    assert evidence.context == {"blob:src/calc.js#L1-L10": _numbered(ws.repo / "src" / "calc.js", 1, 10)}
    assert evidence.anchors == (LocationRef("src/calc.js", 5, 5),)
    assert evidence.note == EvidenceNote(
        finding_id="alpha-model/active/L-1", context_keys=("blob:src/calc.js#L1-L10",)
    )


@pytest.mark.parametrize(
    ("finding_id", "keys", "anchors"),
    [
        # 範囲の参照（13-14）を前後 5 行に広げる
        ("alpha-model/active/M-1", ["blob:src/calc.js#L8-L19"], [("src/calc.js", 13, 14)]),
        # 短いパス calc.js は同じ位置欄の src/calc.js に解決し、重なる範囲を 1 つにまとめる
        ("beta-model/active/M-1", ["blob:src/calc.js#L8-L19"], [("src/calc.js", 13, 13), ("src/calc.js", 14, 14)]),
        # `:20` は直前のパスを指す
        ("beta-model/active/L-1", ["blob:src/calc.js#L15-L26"], [("src/calc.js", 21, 21), ("src/calc.js", 20, 20)]),
        # ファイル末尾（33 行）で切り詰める
        ("gamma-model/active/L-1", ["blob:src/calc.js#L25-L33"], [("src/calc.js", 30, 30)]),
        ("beta-model/active/I-1", ["blob:src/util.js#L1-L5"], [("src/util.js", 4, 4)]),
        # 行番号の無い参照は evidence にならない
        ("alpha-model/active/I-1", [], []),
    ],
)
def test_evidence_for_fixture_findings(
    ws: Workspace, finding_id: str, keys: list[str], anchors: list[tuple[str, int, int]]
) -> None:
    evidence = build_evidence(_batch(ws).finding(finding_id), GitSource(ws.repo), head=ws.head)
    assert list(evidence.context) == keys
    assert evidence.anchors == tuple(LocationRef(*anchor) for anchor in anchors)
    assert evidence.note.unresolved == ()


@pytest.mark.parametrize(
    ("location", "unresolved"),
    [
        ("`src/missing.js:3`", "src/missing.js:3-3: not found at reviewed head"),
        ("`src/link.js:1`", "src/link.js:1-1: not a regular file at reviewed head"),
        ("`src:1`", "src:1-1: not a regular file at reviewed head"),
        ("`src/latin1.txt:1`", "src/latin1.txt:1-1: not valid UTF-8"),
        ("`src/has#hash.js:1`", "src/has#hash.js:1-1: unusable path"),
        ("`../outside.js:1`", "../outside.js:1-1: unusable path"),
        ("`src/calc.js:99`", "src/calc.js:99-99: line out of range"),
        ("`:3`", ":3-3: bare line reference without a preceding path"),
        ("`a/x.js:1`、`b/x.js:1`、`x.js:2`", "x.js:2-2: ambiguous short path"),
    ],
)
def test_unusable_references_are_recorded_and_skipped(odd_ws: Workspace, location: str, unresolved: str) -> None:
    evidence = build_evidence(_with_location(odd_ws, location), GitSource(odd_ws.repo), head=odd_ws.head)
    assert unresolved in evidence.note.unresolved
    assert all(not key.startswith(f"blob:{unresolved.split(':')[0]}#") for key in evidence.context)


@pytest.mark.parametrize(
    ("middle", "reason"),
    [
        ("src/link.js:20", "src/link.js:20-20: not a regular file at reviewed head"),
        ("src/removed.js:20", "src/removed.js:20-20: not found at reviewed head"),
    ],
)
def test_bare_line_after_an_unresolved_path_is_not_excerpted(odd_ws: Workspace, middle: str, reason: str) -> None:
    """`:N` は解決できなかった直前のパスを飛ばして、それより前の別ファイルに結び付かない。"""
    location = f"`src/calc.js:3`、`{middle}`、`:25`"
    evidence = build_evidence(_with_location(odd_ws, location), GitSource(odd_ws.repo), head=odd_ws.head)
    assert list(evidence.context) == ["blob:src/calc.js#L1-L8"]
    assert evidence.anchors == (LocationRef("src/calc.js", 3, 3),)
    assert evidence.note.unresolved == (reason, ":25-25: bare line reference after an unresolved path")


def test_bare_line_after_an_out_of_range_line_uses_that_path(odd_ws: Workspace) -> None:
    """行番号だけが範囲外のパスは直前のパスになり、続く `:N` はそのファイルの行として抜粋する。"""
    location = "`src/calc.js:3`、`docs/long.txt:999`、`:25`"
    evidence = build_evidence(_with_location(odd_ws, location), GitSource(odd_ws.repo), head=odd_ws.head)
    assert list(evidence.context) == ["blob:src/calc.js#L1-L8", "blob:docs/long.txt#L20-L30"]
    assert evidence.anchors == (LocationRef("src/calc.js", 3, 3), LocationRef("docs/long.txt", 25, 25))
    assert evidence.note.unresolved == ("docs/long.txt:999-999: line out of range",)


def test_crlf_lines_lose_the_carriage_return(odd_ws: Workspace) -> None:
    evidence = build_evidence(_with_location(odd_ws, "`src/crlf.js:2`"), GitSource(odd_ws.repo), head=odd_ws.head)
    assert evidence.context == {"blob:src/crlf.js#L1-L3": "     1  first\n     2  second\n     3  third"}


def test_excerpts_are_limited_per_finding(odd_ws: Workspace) -> None:
    refs = "、".join(f"`docs/long.txt:{line}`" for line in range(10, 200, 20))
    evidence = build_evidence(_with_location(odd_ws, refs), GitSource(odd_ws.repo), head=odd_ws.head)
    assert list(evidence.context) == [f"blob:docs/long.txt#L{line - 5}-L{line + 5}" for line in range(10, 130, 20)]
    assert "excerpt limit reached: 4 excerpt(s) dropped" in evidence.note.unresolved
    assert len(evidence.anchors) == 10


def test_diff_base_adds_a_diff_section_for_changed_paths(ws: Workspace) -> None:
    calc = ws.repo / "src" / "calc.js"
    lines = calc.read_text(encoding="utf-8").split("\n")
    lines[13] = "    return high;"
    calc.write_text("\n".join(lines), encoding="utf-8")
    new_head = _commit(ws.repo, "fix clamp")
    source = GitSource(ws.repo)
    batch = _batch(ws)
    changed = build_evidence(batch.finding("alpha-model/active/M-1"), source, head=new_head, diff_base=ws.head)
    assert list(changed.context) == ["blob:src/calc.js#L8-L19", "diff:src/calc.js"]
    assert changed.context["diff:src/calc.js"].startswith("diff --git a/src/calc.js b/src/calc.js\n")
    assert "+    return high;" in changed.context["diff:src/calc.js"]
    untouched = build_evidence(batch.finding("beta-model/active/I-1"), source, head=new_head, diff_base=ws.head)
    assert list(untouched.context) == ["blob:src/util.js#L1-L5"]
    plain = build_evidence(batch.finding("alpha-model/active/M-1"), source, head=new_head)
    assert list(plain.context) == ["blob:src/calc.js#L8-L19"]


# --- candidate_pairs ---------------------------------------------------------


def test_candidate_pairs_link_near_references_across_files(ws: Workspace) -> None:
    batch = _batch(ws)
    source = GitSource(ws.repo)
    active = batch.active_findings()
    anchors = {finding.finding_id: build_evidence(finding, source, head=ws.head).anchors for finding in active}
    assert candidate_pairs(active, anchors) == (("alpha-model/active/M-1", "beta-model/active/M-1"),)


@pytest.mark.parametrize(
    ("first", "second", "paired"),
    [
        (("src/a.js", 10, 10), ("src/a.js", 15, 15), True),  # 間が 5 行
        (("src/a.js", 10, 10), ("src/a.js", 16, 16), False),  # 間が 6 行
        (("src/a.js", 10, 20), ("src/a.js", 12, 12), True),  # 内側
        (("src/a.js", 10, 10), ("src/b.js", 10, 10), False),  # 別のパス
    ],
)
def test_candidate_pairs_gap(
    ws: Workspace, first: tuple[str, int, int], second: tuple[str, int, int], paired: bool
) -> None:
    batch = _batch(ws)
    alpha, beta = batch.finding("alpha-model/active/L-1"), batch.finding("beta-model/active/L-1")
    anchors = {alpha.finding_id: [LocationRef(*first)], beta.finding_id: [LocationRef(*second)]}
    expected = ((alpha.finding_id, beta.finding_id),) if paired else ()
    assert candidate_pairs([alpha, beta], anchors) == expected


def test_candidate_pairs_skip_the_same_file_and_missing_anchors(ws: Workspace) -> None:
    batch = _batch(ws)
    m1, l1, gamma = (
        batch.finding(finding_id)
        for finding_id in ("alpha-model/active/M-1", "alpha-model/active/L-1", "gamma-model/active/L-1")
    )
    same = [LocationRef("src/a.js", 10, 10)]
    assert candidate_pairs([m1, l1, gamma], {m1.finding_id: same, l1.finding_id: same}) == ()


# --- main: 引数と入力の検証 --------------------------------------------------------

_BASE = ["--review-dir", "r", "--repository", "usaturn/maatlog-poc", "--reviewed-head", "abcdef1", "--output-dir", "o"]


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == EXIT_OK
    assert "--allow-file" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        _BASE[:-2],
        [*_BASE, "--live", "--response-file", "x.json", "--allow-file", "a.txt"],
        [*_BASE, "--live"],
        [*_BASE, "--response-file", "x.json"],
        _replace(_BASE, "--repository", "other/repo"),
        [*_BASE, "--max-calls", "0"],
        [*_BASE, "--max-calls", "many"],
    ],
)
def test_argument_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def _write_input(ws: Workspace, name: str, data: bytes) -> str:
    path = ws.tmp / name
    path.write_bytes(data)
    return os.fspath(path)


def _case_head_format(ws: Workspace) -> list[str]:
    return _replace(_args(ws, "out"), "--reviewed-head", "XYZ")


def _case_head_unknown(ws: Workspace) -> list[str]:
    return _replace(_args(ws, "out"), "--reviewed-head", "0123456789abcdef")


def _case_diff_base_unknown(ws: Workspace) -> list[str]:
    return _args(ws, "out", "--diff-base", "0123456")


def _case_source_not_git(ws: Workspace) -> list[str]:
    (ws.tmp / "plain").mkdir()
    return _replace(_args(ws, "out"), "--source-repo", os.fspath(ws.tmp / "plain"))


def _case_output_exists(ws: Workspace) -> list[str]:
    (ws.tmp / "out").mkdir()
    return _args(ws, "out")


def _case_output_parent_missing(ws: Workspace) -> list[str]:
    return _args(ws, "missing/out")


def _case_output_inside_review_dir(ws: Workspace) -> list[str]:
    return _replace(_args(ws, "out"), "--output-dir", os.fspath(ws.reviews / "out"))


def _case_review_dir_missing(ws: Workspace) -> list[str]:
    return _replace(_args(ws, "out"), "--review-dir", os.fspath(ws.tmp / "nope"))


def _case_allow_line(ws: Workspace) -> list[str]:
    return _args(ws, "out", "--allow-file", _write_input(ws, "allow.txt", b"not-a-hash\n"))


def _case_allow_missing(ws: Workspace) -> list[str]:
    return _args(ws, "out", "--allow-file", os.fspath(ws.tmp / "absent.txt"))


def _case_allow_not_utf8(ws: Workspace) -> list[str]:
    return _args(ws, "out", "--allow-file", _write_input(ws, "allow.txt", b"\xff\n"))


def _case_response_json(ws: Workspace) -> list[str]:
    allow = _write_input(ws, "allow.txt", b"")
    return _args(ws, "out", "--response-file", _write_input(ws, "r.json", b"{"), "--allow-file", allow)


def _case_response_schema(ws: Workspace) -> list[str]:
    allow = _write_input(ws, "allow.txt", b"")
    return _args(ws, "out", "--response-file", _write_input(ws, "r.json", b'[{"ok": true}]'), "--allow-file", allow)


def _case_thresholds(ws: Workspace) -> list[str]:
    data = json.dumps({"model_id": "jev-latest", "noul_no_max": 0.99}).encode()
    return _args(ws, "out", "--thresholds", _write_input(ws, "t.json", data))


@pytest.mark.parametrize(
    ("case", "message"),
    [
        (_case_head_format, "--reviewed-head must be 7 to 40 lowercase hex characters"),
        (_case_head_unknown, "--reviewed-head 0123456789abcdef is not a commit in "),
        (_case_diff_base_unknown, "--diff-base 0123456 is not a commit in "),
        (_case_source_not_git, "--source-repo is not a readable git work tree: "),
        (_case_output_exists, "--output-dir already exists: "),
        (_case_output_parent_missing, "the parent of --output-dir does not exist: "),
        (_case_output_inside_review_dir, "--output-dir must not be inside --review-dir: "),
        (_case_review_dir_missing, "review_dir must be an existing directory and not a symlink"),
        (_case_allow_line, "--allow-file line 1 is not a sha256 value"),
        (_case_allow_missing, "--allow-file cannot be read: "),
        (_case_allow_not_utf8, "--allow-file is not valid UTF-8"),
        (_case_response_json, "--response-file is not a JSON list of ClientOutcome records"),
        (_case_response_schema, "--response-file is not a JSON list of ClientOutcome records"),
        (_case_thresholds, "--thresholds is not a valid Thresholds JSON object"),
    ],
)
def test_input_errors_exit_2_without_output(
    ws: Workspace, case: Callable[[Workspace], list[str]], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = case(ws)
    out = Path(argv[argv.index("--output-dir") + 1])
    existed = out.exists()
    assert _run(argv) == EXIT_USAGE
    err = capsys.readouterr().err
    assert f"review_triage: error: {message}" in err
    assert (out.exists() and not any(out.iterdir())) if existed else not out.exists()


def test_diff_base_shadowed_by_a_ref_exits_2_without_output(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    """``--diff-base`` は review header との照合が無いため、同名の ref による取り違えを解決の時点で止める。"""
    other = _commit(ws.repo, "other")
    _fixture_git("branch", other[:7], ws.head, cwd=ws.repo)
    assert _run(_args(ws, "out", "--diff-base", other[:7])) == EXIT_USAGE
    assert f"review_triage: error: --diff-base {other[:7]} is not a commit in " in capsys.readouterr().err
    assert not (ws.tmp / "out").exists()


# --- main: dry-run ------------------------------------------------------------


def test_dry_run_writes_outputs_without_network(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    assert _run(_args(ws, "out"), environ={API_KEY_ENV: KEY}, transport=RefusingTransport()) == EXIT_OK
    out = ws.tmp / "out"
    assert sorted(path.name for path in out.iterdir()) == [
        "report.json",
        "report.md",
        "run.json",
        "states.json",
        "states.md",
    ]
    report = _report(out)
    assert report.status is ReportStatus.FALLBACK
    assert report.reason == "fallback: no outcomes for 8 active finding(s)"
    assert report.review_dir == REVIEW_DIR_NAME
    assert report.reviewed_head == ws.head
    assert report.generated_at == "2026-09-29T00:00:00Z"
    assert tuple(decision.finding_id for decision in report.decisions) == FINDING_IDS
    assert all(decision.proposal is Proposal.HUMAN_REVIEW for decision in report.decisions)
    assert (out / "report.md").read_text(encoding="utf-8") == render_report(report)
    run = _run_record(out)
    assert (run.mode, run.reviewed_head, run.generated_at) == ("dry-run", ws.head, "2026-09-29T00:00:00Z")
    assert (run.counts.requests, run.counts.calls_made, run.counts.outcomes) == (0, 0, 0)
    err = capsys.readouterr().err.splitlines()
    assert err[:4] == [
        "review_triage: mode=dry-run findings=9 active=8 unparsed=0 anomalies=0",
        "review_triage: states=9 (finding 8, pair 1) sendable=0 awaiting_approval=6 blocked=3 withheld=1",
        "review_triage: dry-run: no request was sent",
        "review_triage: report status=fallback: fallback: no outcomes for 8 active finding(s)",
    ]
    assert err[4] == (f"review_triage: wrote {out}: report.md report.json states.md states.json run.json")
    assert any(line.startswith(f"review_triage: to send: check {out / 'states.md'}, ") for line in err)
    assert err[-1] == (
        "review_triage: 9 of 9 finding(s) need human review: "
        f"continue with the existing review-fix-planning procedure for {ws.reviews}"
    )


def test_review_records_v1_are_excluded_from_request_states(ws: Workspace) -> None:
    fixture = FIXTURES.parent / "input" / "pr-903-review-records-v1" / "model.md"
    text = fixture.read_text(encoding="utf-8").replace("aaaaaaaaaaaa", ws.head[:12])
    (ws.reviews / "recorded.md").write_text(text, encoding="utf-8")
    assert _run(_args(ws, "out"), transport=RefusingTransport()) == EXIT_OK
    out = ws.tmp / "out"
    run = _run_record(out)
    assert (run.counts.findings, run.counts.active_findings, run.counts.unparsed) == (11, 8, 0)
    records = _records(out)
    assert len(records) == 9
    assert {fid for record in records for fid in record.finding_ids} == set(ACTIVE_IDS)
    report = _report(out)
    assert [d.finding_id for d in report.decisions] == [*FINDING_IDS, "recorded/unverified/U-1"]
    assert report.model_dump(mode="json")["decided_count"] == 1
    assert report.model_dump(mode="json")["unverified_count"] == 1


@pytest.mark.parametrize("section", ["Unverified candidates", "Decided findings"])
@pytest.mark.parametrize(
    "preamble", ["なし。理由を添える。", "- なし", "なし。\n候補は検証済み。", "該当なし（`skip` がない）"]
)
def test_recorded_section_preambles_do_not_add_cli_warnings(
    ws: Workspace, section: str, preamble: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (ws.reviews / "recorded.md").write_text(
        f"- 対象: PR #9001 / head `{ws.head[:12]}` / 2026-10-03\n\n## {section}\n\n{preamble}\n",
        encoding="utf-8",
    )
    assert _run(_args(ws, "out"), transport=RefusingTransport()) == EXIT_OK
    out = ws.tmp / "out"
    assert _run_record(out).counts.unparsed == 0
    report = _report(out)
    assert report.unparsed_count == 0
    assert tuple(d.finding_id for d in report.decisions) == FINDING_IDS
    assert len(_records(out)) == 9
    assert {fid for record in _records(out) for fid in record.finding_ids} == set(ACTIVE_IDS)
    assert "解釈できなかったブロック" not in (out / "report.md").read_text(encoding="utf-8")
    assert "unparsed block(s) were not triaged" not in capsys.readouterr().err


def test_dry_run_states_list_every_candidate(ws: Workspace) -> None:
    assert _run(_args(ws, "out")) == EXIT_OK
    out = ws.tmp / "out"
    records = _records(out)
    assert [record.index for record in records] == list(range(1, 10))
    assert [record.finding_ids for record in records[:8]] == [(finding_id,) for finding_id in ACTIVE_IDS]
    pair = records[8]
    assert pair.purpose == "pair"
    assert set(pair.finding_ids) == {"alpha-model/active/M-1", "beta-model/active/M-1"}
    assert pair.awaiting_approval and pair.sections is not None
    by_id = {record.finding_ids[0]: record for record in records[:8]}
    assert {finding_id for finding_id, record in by_id.items() if record.awaiting_approval} == AWAITING_FINDINGS
    assert by_id["alpha-model/active/I-1"].blocked_reasons == ("missing_evidence", "not_approved")
    assert by_id["gamma-model/active/L-2"].blocked_reasons == ("missing_evidence", "not_approved")
    assert by_id["beta-model/active/I-1"].blocked_reasons == ("secret_suspected:finding:private_key", "not_approved")
    finding = _batch(ws).finding("alpha-model/active/L-1")
    assert by_id["alpha-model/active/L-1"].sections == {
        "finding": finding.body,
        "evidence": "--- blob:src/calc.js#L1-L10\n" + _numbered(ws.repo / "src" / "calc.js", 1, 10),
    }
    notes = {note.finding_id: note for note in _run_record(out).evidence}
    assert notes["gamma-model/active/L-2"] == EvidenceNote(
        finding_id="gamma-model/active/L-2", unresolved=("src/missing.js:3-3: not found at reviewed head",)
    )
    markdown = (out / "states.md").read_text(encoding="utf-8")
    assert "## 1. finding: `alpha-model/active/M-1`" in markdown
    assert f"- content_sha256: `{records[0].content_sha256}`" in markdown
    assert "- 状態: 許可待ち" in markdown


def test_secret_state_body_is_never_written(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(_args(ws, "out")) == EXIT_OK
    out = ws.tmp / "out"
    record = next(record for record in _records(out) if record.finding_ids == ("beta-model/active/I-1",))
    assert record.withheld and record.sections is None
    for path in out.iterdir():
        assert SECRET_LINE not in path.read_text(encoding="utf-8"), path.name
    assert SECRET_LINE not in capsys.readouterr().err
    assert "秘密の疑いがあるため本文を表示しない。" in (out / "states.md").read_text(encoding="utf-8")


def test_dry_run_with_allow_file_marks_states_but_sends_nothing(ws: Workspace) -> None:
    assert _run(_args(ws, "first")) == EXIT_OK
    allow = _allow_file(ws, ws.tmp / "first")
    assert _run(_args(ws, "second", "--allow-file", os.fspath(allow)), transport=RefusingTransport()) == EXIT_OK
    out = ws.tmp / "second"
    assert sum(record.allowed for record in _records(out)) == 6
    run = _run_record(out)
    assert (run.counts.requests, run.counts.calls_made, run.counts.allowed_hashes_unused) == (6, 0, 0)
    assert run.allow_file_sha256 == hashlib.sha256(allow.read_bytes()).hexdigest()
    assert not (out / "outcomes.json").exists()


def test_allow_file_accepts_bom_and_crlf(ws: Workspace) -> None:
    assert _run(_args(ws, "first")) == EXIT_OK
    allow = _allow_file(ws, ws.tmp / "first", bom=True, crlf=True)
    assert _run(_args(ws, "second", "--allow-file", os.fspath(allow))) == EXIT_OK
    assert sum(record.allowed for record in _records(ws.tmp / "second")) == 6


def test_directory_without_reviews_still_writes_a_report(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    empty = ws.tmp / "reviews" / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("メモ\n", encoding="utf-8")
    assert _run(_replace(_args(ws, "out"), "--review-dir", os.fspath(empty))) == EXIT_OK
    out = ws.tmp / "out"
    assert _report(out).decisions == ()
    assert _run_record(out).anomalies == ("notes.txt: ignored non-markdown entry", "no review files")
    assert "送信候補の state はない。" in (out / "states.md").read_text(encoding="utf-8")
    assert "anomalies=2" in capsys.readouterr().err


def test_unparsed_blocks_point_back_to_review_fix_planning(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    odd = ws.tmp / "reviews" / "odd"
    odd.mkdir()
    header = f"- 対象: PR #9001 / head `{ws.head[:12]}` / 2026-09-28\n"
    text = header + "\n## Low\n\n見出しの無い散文が指摘の節にある。\n"
    (odd / "delta-model.md").write_text(text, encoding="utf-8")
    assert _run(_replace(_args(ws, "out"), "--review-dir", os.fspath(odd))) == EXIT_OK
    out = ws.tmp / "out"
    assert _report(out).unparsed_count == 1
    assert "解釈できなかったブロックが 1 件ある" in (out / "report.md").read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert f"review_triage: 1 unparsed block(s) were not triaged: check {odd} with the existing" in err


def test_summary_escapes_control_characters(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    target = _render_reviews(ws.tmp, ws.head, name="pr-\x1b[31m-escape")
    assert _run(_replace(_args(ws, "out"), "--review-dir", os.fspath(target))) == EXIT_OK
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "pr-\\x1b[31m-escape" in err
    assert "\x1b" not in (ws.tmp / "out" / "states.md").read_text(encoding="utf-8")


def test_thresholds_file_is_used_and_recorded(ws: Workspace) -> None:
    path = ws.tmp / "thresholds.json"
    path.write_text(json.dumps({"model_id": "jev-latest", "choice_min_confidence": 0.8}), encoding="utf-8")
    assert _run(_args(ws, "out", "--thresholds", os.fspath(path))) == EXIT_OK
    assert _report(ws.tmp / "out").thresholds.choice_min_confidence == 0.8
    assert _run_record(ws.tmp / "out").thresholds_file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_writes_only_the_output_directory(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot(ws.reviews, ws.repo)
    calls = _record_subprocess(monkeypatch)
    assert _run(_args(ws, "out", "--diff-base", ws.head[:7])) == EXIT_OK
    assert _snapshot(ws.reviews, ws.repo) == before
    assert calls
    for argv in calls:
        assert Path(argv[0]).name == "git" and argv[1] == "-C", argv
        assert argv[3] in {"rev-parse", "ls-tree", "cat-file", "diff"}, argv


def test_module_entry_point_prints_help() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [sys.executable, "-m", "tools.review_triage", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--review-dir" in completed.stdout


# --- main: live と replay ---------------------------------------------------------


def test_request_sha256_matches_the_client_outcome() -> None:
    request = connectivity_request()
    outcome = SystemOneClient(api_key=KEY, model=request.model, transport=FakeTransport()).evaluate(request)
    assert outcome.ok
    assert request_sha256(request) == outcome.request_sha256


def test_live_sends_only_approved_states(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    allow = _dry_and_allow(ws)
    transport = FakeTransport()
    argv = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(argv, environ={API_KEY_ENV: KEY}, transport=transport) == EXIT_OK
    out = ws.tmp / "live"
    outcomes = _outcomes(out)
    assert len(transport.bodies) == 6
    assert [outcome.request_sha256 for outcome in outcomes] == transport.hashes
    assert all(outcome.ok for outcome in outcomes)
    sent = {frozenset(outcome.finding_ids) for outcome in outcomes}
    assert sent == {frozenset(record.finding_ids) for record in _records(out) if record.allowed}
    report = _report(out)
    assert report.status is ReportStatus.ADVISORY
    assert report.model_id_responded == "jev-2026-09-01"
    decisions = {decision.finding_id: decision for decision in report.decisions}
    assert {finding_id: decision.proposal for finding_id, decision in decisions.items()} == {
        "alpha-model/active/M-1": Proposal.FIX_NOW,
        "alpha-model/active/L-1": Proposal.FIX_NOW,
        "alpha-model/active/I-1": Proposal.HUMAN_REVIEW,
        "alpha-model/resolved/I-2": Proposal.HUMAN_REVIEW,
        "beta-model/active/M-1": Proposal.FIX_NOW,
        "beta-model/active/L-1": Proposal.FIX_NOW,
        "beta-model/active/I-1": Proposal.HUMAN_REVIEW,
        "gamma-model/active/L-1": Proposal.FIX_NOW,
        "gamma-model/active/L-2": Proposal.HUMAN_REVIEW,
    }
    assert decisions["alpha-model/active/M-1"].duplicate_candidates == ("beta-model/active/M-1",)
    assert decisions["beta-model/active/I-1"].escalation_reasons == ("missing_outcome",)
    assert decisions["alpha-model/resolved/I-2"].escalation_reasons == ("status_resolved",)
    run = _run_record(out)
    assert (run.mode, run.counts.requests, run.counts.calls_made, run.counts.outcomes) == ("live", 6, 6, 6)
    err = capsys.readouterr().err
    assert "review_triage: requests=6 calls_made=6" in err
    assert f"wrote {out}: report.md report.json states.md states.json outcomes.json run.json" in err
    assert KEY not in err
    for path in out.iterdir():
        assert KEY not in path.read_text(encoding="utf-8"), path.name


def test_live_refuses_more_requests_than_max_calls(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    allow = _dry_and_allow(ws)
    transport = FakeTransport()
    argv = _args(ws, "live", "--live", "--allow-file", os.fspath(allow), "--max-calls", "5")
    assert _run(argv, environ={API_KEY_ENV: KEY}, transport=transport) == EXIT_USAGE
    assert transport.bodies == []
    assert not (ws.tmp / "live").exists()
    assert "review_triage: error: 6 request(s) exceed --max-calls 5; nothing was sent" in capsys.readouterr().err


def test_live_without_key_falls_back_to_human_review(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    allow = _dry_and_allow(ws)
    argv = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(argv, environ={}, transport=RefusingTransport()) == EXIT_OK
    out = ws.tmp / "live"
    outcomes = _outcomes(out)
    assert len(outcomes) == 6
    assert all(
        outcome.error is not None and outcome.error.kind is ErrorKind.NO_KEY and outcome.calls_made == 0
        for outcome in outcomes
    )
    report = _report(out)
    assert report.status is ReportStatus.FALLBACK
    assert all(decision.proposal is Proposal.HUMAN_REVIEW for decision in report.decisions)
    err = capsys.readouterr().err
    assert "review_triage: requests=6 calls_made=0" in err
    assert (
        "review_triage: TYPESAFE_API_KEY is not set or unusable; no request was sent "
        "(see tools/review_triage/access.rst)"
    ) in err
    assert "need human review: continue with the existing review-fix-planning procedure" in err


def test_live_network_failure_falls_back(ws: Workspace) -> None:
    allow = _dry_and_allow(ws)
    transport = FakeTransport(failure=ConnectionRefusedError())
    argv = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(argv, environ={API_KEY_ENV: KEY}, transport=transport) == EXIT_OK
    out = ws.tmp / "live"
    assert all(outcome.error is not None and outcome.error.kind is ErrorKind.CONNECTION for outcome in _outcomes(out))
    report = _report(out)
    assert report.status is ReportStatus.FALLBACK
    assert "client failures: connection x6" in report.reason
    assert _run_record(out).counts.calls_made == 6


def test_live_with_other_thresholds_model_is_uncalibrated(ws: Workspace) -> None:
    allow = _dry_and_allow(ws)
    thresholds = ws.tmp / "thresholds.json"
    thresholds.write_text(json.dumps({"model_id": "jev-2026-01-01"}), encoding="utf-8")
    argv = _args(ws, "live", "--live", "--allow-file", os.fspath(allow), "--thresholds", os.fspath(thresholds))
    assert _run(argv, environ={API_KEY_ENV: KEY}, transport=FakeTransport()) == EXIT_OK
    report = _report(ws.tmp / "live")
    assert report.status is ReportStatus.UNCALIBRATED
    assert "does not match thresholds.model_id jev-2026-01-01" in report.reason


def test_replay_reproduces_the_live_report(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    allow = _dry_and_allow(ws)
    live = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(live, environ={API_KEY_ENV: KEY}, transport=FakeTransport()) == EXIT_OK
    responses = ws.tmp / "live" / "outcomes.json"
    replay = _args(ws, "replay", "--response-file", os.fspath(responses), "--allow-file", os.fspath(allow))
    capsys.readouterr()
    assert _run(replay, transport=RefusingTransport()) == EXIT_OK
    for name in ("report.json", "report.md", "outcomes.json"):
        assert (ws.tmp / "replay" / name).read_bytes() == (ws.tmp / "live" / name).read_bytes(), name
    run = _run_record(ws.tmp / "replay")
    assert (run.mode, run.counts.outcomes, run.counts.outcomes_unmatched, run.counts.calls_made) == (
        "replay",
        6,
        0,
        0,
    )
    assert run.response_file_sha256 == hashlib.sha256(responses.read_bytes()).hexdigest()
    assert "review_triage: outcomes used=6 unmatched=0" in capsys.readouterr().err


def test_replay_ignores_outcomes_for_requests_it_did_not_build(ws: Workspace) -> None:
    allow = _dry_and_allow(ws)
    live = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(live, environ={API_KEY_ENV: KEY}, transport=FakeTransport()) == EXIT_OK
    smaller = _allow_file(ws, ws.tmp / "dry", name="smaller.txt", skip=frozenset({"gamma-model/active/L-1"}))
    responses = os.fspath(ws.tmp / "live" / "outcomes.json")
    replay = _args(ws, "replay", "--response-file", responses, "--allow-file", os.fspath(smaller))
    assert _run(replay, transport=RefusingTransport()) == EXIT_OK
    run = _run_record(ws.tmp / "replay")
    assert (run.counts.outcomes, run.counts.outcomes_unmatched) == (5, 1)
    decisions = {decision.finding_id: decision for decision in _report(ws.tmp / "replay").decisions}
    assert decisions["gamma-model/active/L-1"].escalation_reasons == ("missing_outcome",)
