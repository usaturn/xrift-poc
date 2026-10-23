"""cli（#389）のオフライン試験。外部ネットワーク・実キー・実データ（reviews/）を使わない。"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from pydantic import TypeAdapter

from tools.review_triage.cli import (
    EXIT_OK,
    FindingEvidence,
    GitError,
    GitSource,
    LocationRef,
    RunRecord,
    StateRecord,
    build_evidence,
    location_refs,
    main,
)
from tools.review_triage.client import API_KEY_ENV
from tools.review_triage.inputs import load_reviews
from tools.review_triage.models import (
    API_ENDPOINT,
    ClientOutcome,
    HttpRequest,
    HttpResponse,
    Proposal,
    Repository,
    ReviewBatch,
    ReviewFinding,
    Thresholds,
    Transport,
    TriageReport,
)
from tools.review_triage.policy import EscalationCode

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
SOURCE = FIXTURES / "source"
REVIEWS = FIXTURES / "reviews" / "pr-9001-synthetic"
# 実データのディレクトリ名（reviews/#NNN-branch）と同じく `#` を含む名前で試す
REVIEW_DIR_NAME = "#9001-feat-synthetic"
KEY = "ts-cli-test-key-SENTINEL-5c1e"  # gitleaks:allow
SECRET_LINE = "-----BEGIN TEST PRIVATE KEY-----"
FIXED_NOW = datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
_EXCERPT_KEY = re.compile(r"blob:(?P<path>[^#]+)#L(?P<start>[0-9]+)-L(?P<end>[0-9]+)")


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
    """同じ名前のファイル（`a/x.js`・`b/x.js`）を足した commit。"""
    for name in ("a", "b"):
        (ws.repo / name).mkdir()
        (ws.repo / name / "x.js").write_text("one\ntwo\nthree\n", encoding="utf-8")
    head = _commit(ws.repo, "odd files")
    return Workspace(tmp=ws.tmp, repo=ws.repo, head=head, reviews=ws.reviews)


def _batch(ws: Workspace) -> ReviewBatch:
    return load_reviews(ws.reviews, repository=Repository.ENV, reviewed_head=ws.head)


def _with_location(ws: Workspace, location: str) -> ReviewFinding:
    """fixture の指摘の ``location`` だけを差し替える。"""
    return _batch(ws).finding("alpha-model/active/L-1").model_copy(update={"location": location})


def _excerpt_ranges(evidence: FindingEvidence) -> list[tuple[str, int, int]]:
    """抜粋のキー ``blob:<path>#L<a>-L<b>`` の (パス, 開始行, 終了行)。"""
    ranges: list[tuple[str, int, int]] = []
    for key in evidence.context:
        match = _EXCERPT_KEY.fullmatch(key)
        assert match is not None, key
        ranges.append((match["path"], int(match["start"]), int(match["end"])))
    return ranges


# --- location_refs -----------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        pytest.param("`src/calc.js:10-12`", [("src/calc.js", 10, 12)], id="range"),
        pytest.param(
            "`src/a.py:579`・`:586`・`:614`",
            [("src/a.py", 579, 579), ("", 586, 586), ("", 614, 614)],
            id="continued-path",
        ),
        pytest.param("`docs/configuration.rst:481`–`483`", [("docs/configuration.rst", 481, 483)], id="split-range"),
    ],
)
def test_location_refs(location: str, expected: list[tuple[str, int, int]]) -> None:
    """位置欄の範囲・直前のパスの続き・分かれた範囲を誤読し、参照と違う行を根拠として抜粋する。"""
    assert location_refs(location) == tuple(LocationRef(*ref) for ref in expected)


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
    """質問の型に応じた確信ありの回答を返す transport。受け取った request 本文を記録する。"""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    @property
    def hashes(self) -> list[str]:
        return [hashlib.sha256(body).hexdigest() for body in self.bodies]

    def send(self, request: HttpRequest) -> HttpResponse:
        self.bodies.append(request.body)
        payload: dict[str, Any] = json.loads(request.body)
        answers: dict[str, object] = {}
        for question_id, question in payload["questions"].items():
            if question["type"] == "choice":
                answers[question_id] = {"type": "choice", "choice": "fix_now", "confidence": 0.97}
            else:
                answers[question_id] = {"type": "noul", "noul": 0.99}
        body = json.dumps(
            {"model": "jev-2026-09-01", "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 0}}
        ).encode()
        return HttpResponse(status=200, headers={}, body=body, final_url=API_ENDPOINT)


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
        Repository.ENV.value,
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


def _records(out: Path) -> list[StateRecord]:
    return TypeAdapter(list[StateRecord]).validate_json((out / "states.json").read_bytes())


def _report(out: Path) -> TriageReport:
    return TriageReport.model_validate_json((out / "report.json").read_bytes())


def _run_record(out: Path) -> RunRecord:
    return RunRecord.model_validate_json((out / "run.json").read_bytes())


def _allow_file(ws: Workspace, dry_out: Path, *, name: str = "allow.txt", skip: frozenset[str] = frozenset()) -> Path:
    """dry-run の states.json から、許可待ちの state のハッシュを許可ファイルに書く。

    ``skip`` の指摘を含む state は書かない。
    """
    lines = ["# 許可した state"]
    for record in _records(dry_out):
        if record.awaiting_approval and not skip & set(record.finding_ids):
            lines.append(f"{record.content_sha256}  # {', '.join(record.finding_ids)}")
    path = ws.tmp / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _allowed_hashes(allow: Path) -> set[str]:
    return {line.split()[0] for line in allow.read_text(encoding="utf-8").splitlines() if not line.startswith("#")}


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


def test_git_source_rejects_a_ref_that_shadows_a_short_head(ws: Workspace) -> None:
    """短縮 SHA と同名の ref が別の commit を指すとき、git が優先する ref の commit を対象（差分の基準）として読む。"""
    other = _commit(ws.repo, "other")
    _fixture_git("branch", ws.head[:7], other, cwd=ws.repo)
    source = GitSource(ws.repo)
    with pytest.raises(GitError):
        source.resolve_commit(ws.head[:7])
    assert source.resolve_commit(ws.head) == ws.head


# --- build_evidence ----------------------------------------------------------


def test_evidence_numbers_lines_and_adds_context(ws: Workspace) -> None:
    """抜粋の行番号と行の内容がずれ、承認者と Jev に、参照した行と違う行を根拠として見せる。"""
    finding = _batch(ws).finding("alpha-model/active/L-1")
    evidence = build_evidence(finding, GitSource(ws.repo), head=ws.head)
    assert evidence.anchors == (LocationRef("src/calc.js", 5, 5),)
    [(path, start, end)] = _excerpt_ranges(evidence)
    assert path == "src/calc.js" and start <= 5 <= end
    source_lines = (ws.repo / path).read_text(encoding="utf-8").split("\n")
    shown = next(iter(evidence.context.values())).split("\n")
    assert len(shown) == end - start + 1
    for number, line in zip(range(start, end + 1), shown, strict=True):
        assert line.lstrip().startswith(f"{number} "), line
        assert line.endswith(source_lines[number - 1]), line


def test_unusable_references_are_recorded_and_skipped(odd_ws: Workspace) -> None:
    """同じ位置欄の `a/x.js`・`b/x.js` のどちらかを短いパス `x.js` として推測で選び、別ファイルの行を根拠にする。"""
    finding = _with_location(odd_ws, "`a/x.js:1`、`b/x.js:1`、`x.js:2`")
    evidence = build_evidence(finding, GitSource(odd_ws.repo), head=odd_ws.head)
    assert evidence.anchors == (LocationRef("a/x.js", 1, 1), LocationRef("b/x.js", 1, 1))
    assert any("x.js:2" in note for note in evidence.note.unresolved)


def test_bare_line_after_an_unresolved_path_is_not_excerpted(ws: Workspace) -> None:
    """解決できなかったパスに続く `:N` を、それより前の別ファイルの行として抜粋する。"""
    finding = _with_location(ws, "`src/calc.js:3`、`src/removed.js:20`、`:25`")
    evidence = build_evidence(finding, GitSource(ws.repo), head=ws.head)
    assert evidence.anchors == (LocationRef("src/calc.js", 3, 3),)
    assert all(not start <= 25 <= end for _, start, end in _excerpt_ranges(evidence))


# --- main: dry-run ------------------------------------------------------------


def test_secret_state_body_is_never_written(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    """秘密の疑いがある state の本文を、states.json・states.md・標準エラーなどの出力に書く。"""
    assert _run(_args(ws, "out")) == EXIT_OK
    out = ws.tmp / "out"
    record = next(record for record in _records(out) if record.finding_ids == ("beta-model/active/I-1",))
    assert record.withheld and record.sections is None
    for path in out.iterdir():
        assert SECRET_LINE not in path.read_text(encoding="utf-8"), path.name
    assert SECRET_LINE not in capsys.readouterr().err


def test_dry_run_with_allow_file_marks_states_but_sends_nothing(ws: Workspace) -> None:
    """--live を付けない実行でも、許可済みの state を Jev へ送る。"""
    assert _run(_args(ws, "first")) == EXIT_OK
    allow = _allow_file(ws, ws.tmp / "first")
    # キーがあれば送れる状態にする。キーが無いと、送ろうとしても client が no_key で止まり、送信の退行が見えない
    argv = _args(ws, "second", "--allow-file", os.fspath(allow))
    assert _run(argv, environ={API_KEY_ENV: KEY}, transport=RefusingTransport()) == EXIT_OK
    out = ws.tmp / "second"
    assert any(record.allowed for record in _records(out))
    assert _run_record(out).counts.calls_made == 0
    assert not (out / "outcomes.json").exists()


def test_thresholds_file_is_used_and_recorded(ws: Workspace) -> None:
    """--thresholds で渡した閾値ファイルを無視し、既定の閾値で助言する。"""
    path = ws.tmp / "thresholds.json"
    path.write_text(json.dumps({"model_id": "jev-latest", "choice_min_confidence": 0.8}), encoding="utf-8")
    given = Thresholds.model_validate_json(path.read_bytes())
    assert given != Thresholds(model_id=given.model_id)
    assert _run(_args(ws, "out", "--thresholds", os.fspath(path))) == EXIT_OK
    assert _report(ws.tmp / "out").thresholds == given


def test_run_writes_only_the_output_directory(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """レビューディレクトリ・作業ツリーを書き換える、または読み取り以外の git サブコマンド（fetch など）を使う。"""
    before = _snapshot(ws.reviews, ws.repo)
    calls = _record_subprocess(monkeypatch)
    assert _run(_args(ws, "out", "--diff-base", ws.head[:7])) == EXIT_OK
    assert _snapshot(ws.reviews, ws.repo) == before
    assert calls
    for argv in calls:
        assert Path(argv[0]).name == "git" and argv[1] == "-C", argv
        assert argv[3] in {"rev-parse", "ls-tree", "cat-file", "diff"}, argv


# --- main: live と replay ---------------------------------------------------------


def test_live_sends_only_approved_states(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    """許可していない state を Jev へ送る、または API キーを出力ファイル・標準エラーに書く。"""
    assert _run(_args(ws, "dry")) == EXIT_OK
    # 許可待ちの state を 1 件だけ許可ファイルから外し、その state を送らないことを確かめる
    withheld = "gamma-model/active/L-1"
    allow = _allow_file(ws, ws.tmp / "dry", skip=frozenset({withheld}))
    transport = FakeTransport()
    argv = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(argv, environ={API_KEY_ENV: KEY}, transport=transport) == EXIT_OK
    out = ws.tmp / "live"
    outcomes = _outcomes(out)
    assert transport.bodies
    assert [outcome.request_sha256 for outcome in outcomes] == transport.hashes
    sent = {frozenset(outcome.finding_ids) for outcome in outcomes}
    approved = _allowed_hashes(allow)
    assert sent == {frozenset(record.finding_ids) for record in _records(out) if record.content_sha256 in approved}
    assert all(withheld not in finding_ids for finding_ids in sent)
    assert KEY not in capsys.readouterr().err
    for path in out.iterdir():
        assert KEY not in path.read_text(encoding="utf-8"), path.name


def test_replay_ignores_outcomes_for_requests_it_did_not_build(ws: Workspace) -> None:
    """今回作っていない request（許可を外した state）への保存済みの回答を、今回の指摘の助言に使う。"""
    allow = _dry_and_allow(ws)
    live = _args(ws, "live", "--live", "--allow-file", os.fspath(allow))
    assert _run(live, environ={API_KEY_ENV: KEY}, transport=FakeTransport()) == EXIT_OK
    smaller = _allow_file(ws, ws.tmp / "dry", name="smaller.txt", skip=frozenset({"gamma-model/active/L-1"}))
    responses = os.fspath(ws.tmp / "live" / "outcomes.json")
    replay = _args(ws, "replay", "--response-file", responses, "--allow-file", os.fspath(smaller))
    assert _run(replay, transport=RefusingTransport()) == EXIT_OK
    decisions = {decision.finding_id: decision for decision in _report(ws.tmp / "replay").decisions}
    assert decisions["alpha-model/active/L-1"].proposal is Proposal.FIX_NOW
    skipped = decisions["gamma-model/active/L-1"]
    assert skipped.proposal is Proposal.HUMAN_REVIEW
    assert [reason.split(":", 1)[0] for reason in skipped.escalation_reasons] == [EscalationCode.MISSING_OUTCOME.value]
