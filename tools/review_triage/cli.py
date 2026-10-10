"""レビュートリアージ PoC の CLI（#389 cli）。

input（#383）・client（#385）・policy（#387）を 1 つのコマンドで結線する。
既定は通信しない dry-run で、送信候補の state と内容ハッシュを書き出す。
``--live`` は許可ファイルに書いたハッシュの state だけを Jev へ送る。
``--response-file`` は保存した outcome を再生する。
書き込むのは新規作成した出力ディレクトリの中だけで、git は読み取り専用のサブコマンドだけを使う。
使い方の定義元は ``README.rst``、型と署名の定義元は ``interfaces.rst``。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from tools.review_triage.client import SystemOneClient, api_key_from_env
from tools.review_triage.inputs import ReviewInputError, load_reviews
from tools.review_triage.models import (
    DEFAULT_MODEL_ID,
    ClientOutcome,
    ErrorKind,
    PreparedState,
    Proposal,
    ReportStatus,
    Repository,
    ReviewBatch,
    ReviewFinding,
    StateSource,
    SystemOneRequest,
    Thresholds,
    Transport,
    TriageReport,
    UnparsedBlock,
)
from tools.review_triage.policy import triage
from tools.review_triage.questions import (
    QUESTION_SET_VERSION,
    build_request,
    question_set_for_finding,
    question_set_for_pair,
)
from tools.review_triage.report import render_report
from tools.review_triage.state import prepare_state

__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EvidenceNote",
    "FindingEvidence",
    "GitError",
    "GitSource",
    "LocationRef",
    "RunCounts",
    "RunRecord",
    "StateRecord",
    "build_evidence",
    "candidate_pairs",
    "location_refs",
    "main",
    "request_sha256",
]

EXIT_OK = 0
EXIT_USAGE = 2
DEFAULT_MAX_CALLS = 50
CONTEXT_LINES = 5  # 抜粋で参照の前後に足す行数
PAIR_LINE_GAP = 5  # 重複候補とみなす、同じパスの参照の間の最大行数
MAX_EXCERPTS_PER_FINDING = 6
BLOB_BYTE_LIMIT = 1_000_000  # 読む blob と差分の上限（バイト）
GIT_TIMEOUT_S = 30.0
ALLOW_FILE_BYTE_LIMIT = 1_000_000
RESPONSE_FILE_BYTE_LIMIT = 20_000_000
THRESHOLDS_FILE_BYTE_LIMIT = 65_536
_REASON_LIMIT = 300  # 標準エラーに出す report.reason の最大文字数
_PROGRAM = "review_triage"

_SPAN = re.compile(r"`([^`\n]+)`")
# `path:481`–`483` のように、終わりの行が別のバッククォートにある形
_SPLIT_RANGE = re.compile(r"`([^`\n]*:L?[1-9][0-9]{0,8})`\s*[-–~〜]\s*`L?([1-9][0-9]{0,8})`")
# 行番号は 9 桁まで。巨大な値で int() の桁数上限に当たらないようにする
_REF = re.compile(r"(?P<path>[^\s:`]*):L?(?P<start>[1-9][0-9]{0,8})(?:\s*[-–~〜]\s*L?(?P<end>[1-9][0-9]{0,8}))?")
_HEAD = re.compile(r"[0-9a-f]{7,40}")
_FULL_HEAD = re.compile(r"[0-9a-f]{40}")
# パスに含めない文字の一般カテゴリ（input のパス逸脱と同じ）。端末と Markdown の表示でもエスケープする
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_REGULAR_MODES = frozenset({"100644", "100755"})
_ALLOW_LINE = re.compile(r"(?P<sha>[0-9a-f]{64})(?:\s+#.*)?")
_BACKTICK_RUN = re.compile(r"`+")


class LocationRef(NamedTuple):
    """``location`` の 1 つの参照。``path`` が空文字なら、直前の参照のパスを指す。"""

    path: str
    start: int
    end: int


def location_refs(location: str) -> tuple[LocationRef, ...]:
    """``location`` のバッククォートで囲んだ ``path:行`` の参照を出現順に返す純粋関数。

    ``path:12``・``path:10-12``・``path:L10-L20``・``:586``（直前のパス）と、
    終わりの行が別のバッククォートにある ``path:481``–``483`` を読む。``end < start`` の参照は捨てる。
    """
    text = _SPLIT_RANGE.sub(lambda match: f"`{match.group(1)}-{match.group(2)}`", location)
    refs: list[LocationRef] = []
    for span in _SPAN.findall(text):
        match = _REF.fullmatch(span.strip())
        if match is None:
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end >= start:
            refs.append(LocationRef(match.group("path"), start, end))
    return tuple(refs)


# --- git の読み取り ---------------------------------------------------------------


class GitError(Exception):
    """git の読み取りに失敗した。メッセージは短い英語で、ファイルの本文を含めない。"""


def _usable_path(path: str) -> bool:
    """リポジトリ root からの正規化された POSIX パスで、``committed_context`` のキーに使えるか。

    input のパス逸脱の規則に、キー文法の区切りと衝突する ``#`` を加えたもの。
    """
    if not path or path.startswith(("/", "~")) or "\\" in path or "#" in path:
        return False
    if any(unicodedata.category(char) in _HIDDEN_CATEGORIES for char in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``GIT_`` で始まる変数を除き、読み取り専用で通信しない設定を足す。"""
    env = {name: value for name, value in environ.items() if not name.startswith("GIT_")}
    env.update(
        GIT_LITERAL_PATHSPECS="1",  # パスをパターンとして解釈しない
        GIT_NO_LAZY_FETCH="1",  # partial clone で不足した object を取りに行かない（通信しない）
        GIT_OPTIONAL_LOCKS="0",  # index の更新などの任意の書き込みをしない
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _ls_tree_entry(listing: bytes, path: str) -> tuple[str, str, str, int] | None:
    """``git ls-tree -z -l`` の出力から ``path`` の (mode, type, object, size) を取り出す。無ければ ``None``。"""
    wanted = path.encode("utf-8")
    for record in listing.split(b"\0"):
        meta, tab, name = record.partition(b"\t")
        if not tab or name != wanted:
            continue
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 4:
            return None
        mode, kind, oid, size = fields
        return mode, kind, oid, int(size) if size.isdigit() else -1
    return None


class GitSource:
    """git の作業ツリーから、対象 commit の committed 内容を読み取り専用のサブコマンドで読む。

    使うサブコマンドは ``rev-parse``・``ls-tree``・``cat-file``・``diff``（commit 同士）だけで、
    作業ツリー・index・refs へ書き込まない。パスはリポジトリ root からの相対として扱う。
    """

    def __init__(self, repo: Path) -> None:
        git = shutil.which("git")
        if git is None:
            raise GitError("git is not available")
        self._git = git
        self._env = _git_env(os.environ)
        self._root = repo
        top = self._run("rev-parse", "--show-toplevel").strip()
        if not top:
            raise GitError("git rev-parse did not report a work tree")
        self._root = Path(os.fsdecode(top))
        self._blobs: dict[tuple[str, str], tuple[str, ...] | str] = {}

    @property
    def root(self) -> Path:
        """作業ツリーの root。"""
        return self._root

    def _run(self, *args: str) -> bytes:
        try:
            completed = subprocess.run(
                [self._git, "-C", os.fspath(self._root), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=self._env,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {args[0]} failed ({type(exc).__name__})") from exc
        if completed.returncode != 0:
            raise GitError(f"git {args[0]} exited with status {completed.returncode}")
        return completed.stdout

    def resolve_commit(self, sha: str) -> str:
        """7〜40 桁の小文字 hex を 40 桁の commit に解決する。

        解決できないとき、または同名の ref が別の commit を指すときは ``GitError``。
        """
        if not _HEAD.fullmatch(sha):
            raise GitError("a commit must be 7 to 40 lowercase hex characters")
        full = self._run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
        text = full.decode("ascii", "replace").strip()
        if not _FULL_HEAD.fullmatch(text):
            raise GitError("only SHA-1 object names are supported")
        # git は短縮 SHA より同名の ref を優先し、--quiet で曖昧さの警告も出さない
        if not text.startswith(sha):
            raise GitError("a ref with the same name points to another commit")
        return text

    def blob_lines(self, commit: str, path: str) -> tuple[str, ...] | str:
        """``commit`` の通常ファイル ``path`` の行（行末の ``\\r`` を除く）。使えなければ理由の短文を返す。"""
        key = (commit, path)
        if key not in self._blobs:
            self._blobs[key] = self._load_blob(commit, path)
        return self._blobs[key]

    def _load_blob(self, commit: str, path: str) -> tuple[str, ...] | str:
        if not _usable_path(path):
            return "unusable path"
        try:
            entry = _ls_tree_entry(self._run("ls-tree", "-z", "-l", "--full-tree", commit, "--", path), path)
            if entry is None:
                return "not found at reviewed head"
            mode, kind, oid, size = entry
            if kind != "blob" or mode not in _REGULAR_MODES:
                return "not a regular file at reviewed head"
            if size > BLOB_BYTE_LIMIT:
                return f"larger than {BLOB_BYTE_LIMIT} bytes"
            data = self._run("cat-file", "blob", oid)
        except GitError as exc:
            return str(exc)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "not valid UTF-8"
        if not text:
            return ()
        # splitlines() は \x0c や U+2028 でも分けるため、エディタや git と行番号がずれる
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        return tuple(line.removesuffix("\r") for line in lines)

    def diff(self, base: str, head: str, path: str) -> str | None:
        """``base`` から ``head`` への ``path`` の差分。空・読めない・UTF-8 でない・上限超過なら ``None``。

        利用者の git 設定で見出しや形式が変わらないよう、prefix・色・外部 diff・textconv・rename を固定する。
        """
        if not _usable_path(path):
            return None
        try:
            data = self._run(
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--no-relative",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--unified=3",
                base,
                head,
                "--",
                path,
            )
        except GitError:
            return None
        if not data or len(data) > BLOB_BYTE_LIMIT:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None


# --- evidence と重複候補 ------------------------------------------------------------


class EvidenceNote(BaseModel):
    """1 指摘の evidence の記録（``run.json`` に書く）。参照のキーと理由だけで、本文を持たない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str
    context_keys: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingEvidence:
    """``prepare_state`` に渡す ``committed_context`` と、組の選択に使う参照（前後に広げる前の範囲）。"""

    context: dict[str, str]
    anchors: tuple[LocationRef, ...]
    note: EvidenceNote


def build_evidence(
    finding: ReviewFinding, source: GitSource, *, head: str, diff_base: str | None = None
) -> FindingEvidence:
    """``finding.location`` の行番号付き参照から、対象 HEAD の抜粋を ``committed_context`` にする。

    抜粋は参照の前後 ``CONTEXT_LINES`` 行を含み、各行に行番号を付ける。
    同じパスで重なるか隣接する範囲は 1 つにし、1 指摘あたり ``MAX_EXCERPTS_PER_FINDING`` 件までにする。
    使えない参照は飛ばし、理由を ``note.unresolved`` に残す。
    直前の参照のパスを解決できなかったときは、続く ``:N`` も抜粋しない。
    ``diff_base`` を渡すと、抜粋したパスごとに ``diff:<path>`` を足す。
    """
    files: dict[str, tuple[str, ...]] = {}  # 解決したパスの行（最初の出現順）
    anchors: list[LocationRef] = []
    unresolved: list[str] = []
    last: str | None = None  # 直前の参照のパス。行番号だけが範囲外でも、パスを解決できていれば入れる
    # 直前の参照のパスを解決できなかった。続く `:N` をそれより前の別ファイルに結び付けない
    after_unresolved = False
    for ref in location_refs(finding.location or ""):
        shown = f"{ref.path}:{ref.start}-{ref.end}"
        if not ref.path and after_unresolved:
            unresolved.append(f"{shown}: bare line reference after an unresolved path")
            continue
        resolved = _resolve(ref.path, last, list(files), source, head)
        if isinstance(resolved, str):
            unresolved.append(f"{shown}: {resolved}")
            last, after_unresolved = None, bool(ref.path)
            continue
        path, lines = resolved
        last, after_unresolved = path, False
        if ref.start > len(lines):
            unresolved.append(f"{shown}: line out of range")
            continue
        files[path] = lines
        anchors.append(LocationRef(path, ref.start, min(ref.end, len(lines))))
    excerpts = _excerpts(anchors, files)
    if len(excerpts) > MAX_EXCERPTS_PER_FINDING:
        unresolved.append(f"excerpt limit reached: {len(excerpts) - MAX_EXCERPTS_PER_FINDING} excerpt(s) dropped")
        excerpts = excerpts[:MAX_EXCERPTS_PER_FINDING]
    context: dict[str, str] = {}
    for path, start, end in excerpts:
        numbered = (f"{number:>6}  {files[path][number - 1]}" for number in range(start, end + 1))
        context[f"blob:{path}#L{start}-L{end}"] = "\n".join(numbered)
    if diff_base is not None:
        for path in dict.fromkeys(path for path, _, _ in excerpts):
            text = source.diff(diff_base, head, path)
            if text:
                context[f"diff:{path}"] = text
    note = EvidenceNote(finding_id=finding.finding_id, context_keys=tuple(context), unresolved=tuple(unresolved))
    return FindingEvidence(context=context, anchors=tuple(anchors), note=note)


def _resolve(
    path: str, last: str | None, seen: Sequence[str], source: GitSource, head: str
) -> tuple[str, tuple[str, ...]] | str:
    """参照のパスを解決して (パス, 行) を返す。解決できなければ理由の短文を返す。

    空のパスは ``last`` （直前の参照のパス）。対象 HEAD に無い短いパスは、同じ位置欄で解決済みのパスとの後方一致が
    1 件だけのときに、そのパスとみなす。
    """
    if not path:
        if last is None:
            return "bare line reference without a preceding path"
        path = last
    lines = source.blob_lines(head, path)
    if not isinstance(lines, str):
        return path, lines
    matches = [candidate for candidate in seen if candidate.endswith("/" + path)]
    if len(matches) > 1:
        return "ambiguous short path"
    if matches:
        found = source.blob_lines(head, matches[0])
        if not isinstance(found, str):
            return matches[0], found
    return lines


def _excerpts(anchors: Sequence[LocationRef], files: Mapping[str, tuple[str, ...]]) -> list[tuple[str, int, int]]:
    """参照を前後に広げ、同じパスで重なるか隣接する範囲を 1 つにする（パスの出現順・開始行の順）。"""
    spans: dict[str, list[tuple[int, int]]] = {}
    for anchor in anchors:
        count = len(files[anchor.path])
        spans.setdefault(anchor.path, []).append(
            (max(1, anchor.start - CONTEXT_LINES), min(count, anchor.end + CONTEXT_LINES))
        )
    merged: list[tuple[str, int, int]] = []
    for path, ranges in spans.items():
        for start, end in sorted(ranges):
            if merged and merged[-1][0] == path and start <= merged[-1][2] + 1:
                merged[-1] = (path, merged[-1][1], max(merged[-1][2], end))
            else:
                merged.append((path, start, end))
    return merged


def candidate_pairs(
    findings: Sequence[ReviewFinding], anchors: Mapping[str, Sequence[LocationRef]]
) -> tuple[tuple[str, str], ...]:
    """別のレビューファイルの指摘で、同じパスの参照の間が ``PAIR_LINE_GAP`` 行以内の組を ``findings`` の順に返す。"""
    pairs: list[tuple[str, str]] = []
    for index, first in enumerate(findings):
        for second in findings[index + 1 :]:
            if first.source_file == second.source_file:
                continue
            if _near(anchors.get(first.finding_id, ()), anchors.get(second.finding_id, ())):
                pairs.append((first.finding_id, second.finding_id))
    return tuple(pairs)


def _near(first: Sequence[LocationRef], second: Sequence[LocationRef]) -> bool:
    return any(
        a.path == b.path and max(a.start, b.start) - min(a.end, b.end) <= PAIR_LINE_GAP for a in first for b in second
    )


# --- 出力の型 ---------------------------------------------------------------------

Mode = Literal["dry-run", "live", "replay"]


class StateRecord(BaseModel):
    """``states.json`` の 1 件。秘密の疑いがある state は ``sections`` を持たない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    purpose: Literal["finding", "pair"]
    finding_ids: tuple[str, ...]
    content_sha256: str
    char_count: int
    byte_count: int
    allowed: bool
    awaiting_approval: bool  # 送れない理由が not_approved だけ
    blocked_reasons: tuple[str, ...]
    sources: tuple[StateSource, ...]
    withheld: bool
    sections: dict[str, str] | None


class RunCounts(BaseModel):
    """``run.json`` の件数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    files: int
    findings: int
    active_findings: int
    unparsed: int
    anomalies: int
    states: int
    finding_states: int
    pair_states: int
    allowed_states: int
    awaiting_approval: int
    withheld_states: int
    requests: int
    calls_made: int
    outcomes: int
    outcomes_unmatched: int
    allowed_hashes_unused: int


class RunRecord(BaseModel):
    """``run.json``。実行の条件・入力ファイルの SHA-256・件数・anomaly・evidence の解決結果を残す。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    mode: Mode
    repository: Repository
    review_dir: str
    reviewed_head: str
    diff_base: str | None
    model_requested: str
    question_set_version: str
    thresholds: Thresholds
    generated_at: str
    allow_file_sha256: str | None
    response_file_sha256: str | None
    thresholds_file_sha256: str | None
    counts: RunCounts
    anomalies: tuple[str, ...]
    unparsed: tuple[UnparsedBlock, ...]
    evidence: tuple[EvidenceNote, ...]
    report_status: ReportStatus
    report_reason: str


_STATE_RECORDS = TypeAdapter(list[StateRecord])
_OUTCOME_LIST = TypeAdapter(list[ClientOutcome])


# --- 引数 ----------------------------------------------------------------------


class _UsageError(Exception):
    """終了コード 2 にする入力・利用方法のエラー。メッセージに本文やキーを含めない。"""


@dataclass(frozen=True)
class _Options:
    review_dir: Path
    repository: Repository
    reviewed_head: str
    output_dir: Path
    source_repo: Path
    diff_base: str | None
    allow_file: Path | None
    live: bool
    response_file: Path | None
    thresholds_file: Path | None
    max_calls: int

    @property
    def mode(self) -> Mode:
        if self.live:
            return "live"
        return "replay" if self.response_file is not None else "dry-run"


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text[:20]!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.review_triage",
        description=(
            "Triage multi-model review findings with Jev and write an advisory draft. "
            "Without --live or --response-file it is a dry run that sends nothing."
        ),
        color=False,
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        required=True,
        help="review directory such as reviews/usaturn/xrift-poc/#123-branch",
    )
    parser.add_argument(
        "--repository",
        required=True,
        choices=[repository.value for repository in Repository],
        help="repository the reviews belong to",
    )
    parser.add_argument("--reviewed-head", required=True, help="target HEAD (7 to 40 lowercase hex characters)")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new directory for the outputs (must not exist)"
    )
    parser.add_argument(
        "--source-repo", type=Path, default=Path("."), help="git work tree to read committed content from (default: .)"
    )
    parser.add_argument("--diff-base", help="base commit; adds a diff section to each finding state")
    parser.add_argument(
        "--allow-file",
        type=Path,
        help="content_sha256 values approved for sending (required with --live and --response-file)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="send the approved states to Jev")
    mode.add_argument("--response-file", type=Path, help="replay outcomes.json from an earlier live run")
    parser.add_argument(
        "--thresholds",
        type=Path,
        dest="thresholds_file",
        help="Thresholds JSON (default: provisional values for jev-latest)",
    )
    parser.add_argument(
        "--max-calls",
        type=_positive_int,
        default=DEFAULT_MAX_CALLS,
        help=f"maximum requests in a live run (default: {DEFAULT_MAX_CALLS})",
    )
    return parser


def _options(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> _Options:
    live: bool = namespace.live
    response_file: Path | None = namespace.response_file
    allow_file: Path | None = namespace.allow_file
    if (live or response_file is not None) and allow_file is None:
        parser.error("--live and --response-file need --allow-file")
    return _Options(
        review_dir=namespace.review_dir,
        repository=Repository(namespace.repository),
        reviewed_head=namespace.reviewed_head,
        output_dir=namespace.output_dir,
        source_repo=namespace.source_repo,
        diff_base=namespace.diff_base,
        allow_file=allow_file,
        live=live,
        response_file=response_file,
        thresholds_file=namespace.thresholds_file,
        max_calls=namespace.max_calls,
    )


# --- 入力ファイル -------------------------------------------------------------------


@dataclass(frozen=True)
class _Inputs:
    allowed: frozenset[str]
    allow_file_sha256: str | None
    recorded: tuple[ClientOutcome, ...]
    response_file_sha256: str | None
    thresholds: Thresholds
    thresholds_file_sha256: str | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_file(path: Path, *, option: str, limit: int) -> bytes:
    """通常ファイルを ``limit`` バイトまで読む。読めなければ ``_UsageError``。"""
    shown = os.fspath(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise _UsageError(f"{option} is not a regular file: {shown}")
        if info.st_size > limit:
            raise _UsageError(f"{option} is larger than {limit} bytes: {shown}")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise _UsageError(f"{option} cannot be read: {shown} ({type(exc).__name__})") from None
    if len(data) > limit:
        raise _UsageError(f"{option} is larger than {limit} bytes: {shown}")
    return data


def _parse_allow_file(data: bytes) -> frozenset[str]:
    """許可ファイルのハッシュ。BOM と CRLF を受け付け、``#`` の行と値の後ろのコメントを飛ばす。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _UsageError("--allow-file is not valid UTF-8") from None
    hashes: set[str] = set()
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ALLOW_LINE.fullmatch(line)
        if match is None:
            raise _UsageError(f"--allow-file line {number} is not a sha256 value")
        hashes.add(match.group("sha"))
    return frozenset(hashes)


def _read_inputs(options: _Options) -> _Inputs:
    allowed: frozenset[str] = frozenset()
    allow_sha: str | None = None
    if options.allow_file is not None:
        data = _read_file(options.allow_file, option="--allow-file", limit=ALLOW_FILE_BYTE_LIMIT)
        allowed, allow_sha = _parse_allow_file(data), _sha256(data)
    recorded: tuple[ClientOutcome, ...] = ()
    response_sha: str | None = None
    if options.response_file is not None:
        data = _read_file(options.response_file, option="--response-file", limit=RESPONSE_FILE_BYTE_LIMIT)
        try:
            recorded = tuple(_OUTCOME_LIST.validate_json(data))
        except ValidationError:
            raise _UsageError("--response-file is not a JSON list of ClientOutcome records") from None
        response_sha = _sha256(data)
    thresholds = Thresholds(model_id=DEFAULT_MODEL_ID)
    thresholds_sha: str | None = None
    if options.thresholds_file is not None:
        data = _read_file(options.thresholds_file, option="--thresholds", limit=THRESHOLDS_FILE_BYTE_LIMIT)
        try:
            thresholds = Thresholds.model_validate_json(data)
        except ValidationError:
            raise _UsageError("--thresholds is not a valid Thresholds JSON object") from None
        thresholds_sha = _sha256(data)
    return _Inputs(allowed, allow_sha, recorded, response_sha, thresholds, thresholds_sha)


# --- 計画 ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    batch: ReviewBatch
    reviewed_head: str
    diff_base: str | None
    states: tuple[PreparedState, ...]
    requests: tuple[SystemOneRequest, ...]
    evidence: tuple[EvidenceNote, ...]
    inputs: _Inputs


def _check_output_dir(output_dir: Path, review_dir: Path) -> None:
    """出力先が存在せず、親が存在し、レビューディレクトリの内側でないことを確かめる。"""
    shown = os.fspath(output_dir)
    try:
        if output_dir.exists() or output_dir.is_symlink():
            raise _UsageError(f"--output-dir already exists: {shown}")
        if not output_dir.parent.is_dir():
            raise _UsageError(f"the parent of --output-dir does not exist: {shown}")
        inside = output_dir.resolve().is_relative_to(review_dir.resolve())
    except OSError as exc:
        raise _UsageError(f"--output-dir cannot be checked: {shown} ({type(exc).__name__})") from None
    if inside:
        raise _UsageError(f"--output-dir must not be inside --review-dir: {shown}")


def _resolve_option(source: GitSource, option: str, sha: str) -> str:
    try:
        return source.resolve_commit(sha)
    except GitError as exc:
        raise _UsageError(f"{option} {sha} is not a commit in {os.fspath(source.root)} ({exc})") from None


def _request(batch: ReviewBatch, state: PreparedState) -> SystemOneRequest:
    keys = [batch.finding(finding_id).finding_key for finding_id in state.finding_ids]
    if state.purpose == "finding":
        questions = question_set_for_finding(keys[0])
    else:
        questions = question_set_for_pair(keys[0], keys[1])
    return build_request(state, questions=questions, model=DEFAULT_MODEL_ID)


def _prepare(options: _Options) -> _Plan:
    """入力をすべて検証し、state と request を組み立てる。失敗は ``_UsageError``。出力はまだ作らない。"""
    for option, value in (("--reviewed-head", options.reviewed_head), ("--diff-base", options.diff_base)):
        if value is not None and not _HEAD.fullmatch(value):
            raise _UsageError(f"{option} must be 7 to 40 lowercase hex characters")
    _check_output_dir(options.output_dir, options.review_dir)
    try:
        source = GitSource(options.source_repo)
    except GitError as exc:
        shown = os.fspath(options.source_repo)
        raise _UsageError(f"--source-repo is not a readable git work tree: {shown} ({exc})") from None
    head = _resolve_option(source, "--reviewed-head", options.reviewed_head)
    diff_base = None if options.diff_base is None else _resolve_option(source, "--diff-base", options.diff_base)
    inputs = _read_inputs(options)
    try:
        batch = load_reviews(options.review_dir, repository=options.repository, reviewed_head=head)
    except ReviewInputError as exc:
        raise _UsageError(str(exc)) from None
    active = batch.active_findings()
    evidence = {
        finding.finding_id: build_evidence(finding, source, head=head, diff_base=diff_base) for finding in active
    }
    pairs = candidate_pairs(active, {finding_id: item.anchors for finding_id, item in evidence.items()})
    states = [
        prepare_state(
            batch,
            finding_ids=[finding.finding_id],
            committed_context=evidence[finding.finding_id].context,
            allowed_source_hashes=inputs.allowed,
        )
        for finding in active
    ]
    states += [
        prepare_state(batch, finding_ids=list(pair), committed_context={}, allowed_source_hashes=inputs.allowed)
        for pair in pairs
    ]
    requests = tuple(_request(batch, state) for state in states if state.allowed)
    if options.live and len(requests) > options.max_calls:
        raise _UsageError(f"{len(requests)} request(s) exceed --max-calls {options.max_calls}; nothing was sent")
    notes = tuple(item.note for item in evidence.values())
    return _Plan(batch, head, diff_base, tuple(states), requests, notes, inputs)


# --- outcome -------------------------------------------------------------------


def _collect_outcomes(
    options: _Options, plan: _Plan, *, environ: Mapping[str, str] | None, transport: Transport | None
) -> tuple[tuple[ClientOutcome, ...], int]:
    """mode ごとに outcome を集める。戻り値は (outcome, 照合できなかった保存 outcome の件数)。"""
    if options.mode == "live":
        return _live_outcomes(plan.requests, environ=environ, transport=transport, max_calls=options.max_calls), 0
    if options.mode == "replay":
        return _replay_outcomes(plan.requests, plan.inputs.recorded)
    return (), 0


def request_sha256(request: SystemOneRequest) -> str:
    """client の ``ClientOutcome.request_sha256`` と同じ直列化（送信本文）の SHA-256。"""
    body = json.dumps(request.to_api_payload(), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8", errors="surrogatepass")).hexdigest()


def _live_outcomes(
    requests: Sequence[SystemOneRequest],
    *,
    environ: Mapping[str, str] | None,
    transport: Transport | None,
    max_calls: int,
) -> tuple[ClientOutcome, ...]:
    """許可済みの request を client で 1 回ずつ送る。キーが無ければ client が通信せずに ``no_key`` を返す。"""
    if not requests:
        return ()
    client = SystemOneClient(
        api_key=api_key_from_env(environ), model=DEFAULT_MODEL_ID, transport=transport, max_calls=max_calls
    )
    return tuple(client.evaluate(request) for request in requests)


def _replay_outcomes(
    requests: Sequence[SystemOneRequest], recorded: Sequence[ClientOutcome]
) -> tuple[tuple[ClientOutcome, ...], int]:
    """保存した outcome を ``request_sha256`` で今回の request に対応づける。

    並びは request の順で、同じ値の outcome はファイルの順に並べる。
    どの request とも一致しない outcome は使わない。
    別の HEAD や許可ファイルで作った答えを、別の本文への答えとして使わないため。
    """
    by_hash: dict[str, list[ClientOutcome]] = {}
    for outcome in recorded:
        by_hash.setdefault(outcome.request_sha256, []).append(outcome)
    used: list[ClientOutcome] = []
    for request in requests:
        used.extend(by_hash.pop(request_sha256(request), []))
    return tuple(used), len(recorded) - len(used)


# --- 出力 ----------------------------------------------------------------------


def _create_output_dir(output_dir: Path) -> None:
    try:
        output_dir.mkdir()
    except FileExistsError:
        raise _UsageError(f"--output-dir already exists: {os.fspath(output_dir)}") from None
    except OSError as exc:
        raise _UsageError(f"--output-dir cannot be created: {os.fspath(output_dir)} ({type(exc).__name__})") from None


def _write(path: Path, text: str) -> None:
    """新規ファイルとして書く。新規作成したディレクトリの中なので既存のファイルは無い。"""
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _state_record(index: int, state: PreparedState) -> StateRecord:
    withheld = any(reason.startswith("secret_suspected:") for reason in state.blocked_reasons)
    return StateRecord(
        index=index,
        purpose=state.purpose,
        finding_ids=state.finding_ids,
        content_sha256=state.content_sha256,
        char_count=state.char_count,
        byte_count=state.byte_count,
        allowed=state.allowed,
        awaiting_approval=state.blocked_reasons == ("not_approved",),
        blocked_reasons=state.blocked_reasons,
        sources=state.sources,
        withheld=withheld,
        sections=None if withheld else dict(state.sections),
    )


def _escape_char(char: str) -> str:
    code = ord(char)
    if code < 0x80:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _display(text: str, *, keep: str = "") -> str:
    """制御文字・書式文字・行区切りなど（``keep`` を除く）を ``\\xNN`` などの表記にする。"""
    return "".join(
        char if char in keep or unicodedata.category(char) not in _HIDDEN_CATEGORIES else _escape_char(char)
        for char in text
    )


def _inline(value: object) -> str:
    """値を 1 行の inline code にする。中のバッククォートより長いフェンスで囲む。"""
    text = _display(str(value))
    if not text:
        return "（空）"
    fence = "`" * (max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _fenced(text: str) -> list[str]:
    """本文をコードブロックにする。

    改行とタブ以外の見えない文字は表記にし、フェンスは本文中の最長のバッククォートの連続より長くする。
    """
    shown = _display(text, keep="\n\t")
    fence = "`" * max(3, max((len(run) for run in _BACKTICK_RUN.findall(shown)), default=0) + 1)
    return [f"{fence}text", *shown.split("\n"), fence]


def _state_lines(record: StateRecord) -> list[str]:
    if record.allowed:
        status = "送信可"
    elif record.awaiting_approval:
        status = "許可待ち"
    else:
        status = "送信不可"
    ids = "、".join(_inline(finding_id) for finding_id in record.finding_ids)
    lines = [
        f"## {record.index}. {record.purpose}: {ids}",
        "",
        f"- content_sha256: {_inline(record.content_sha256)}",
        f"- 状態: {status}",
    ]
    if record.blocked_reasons:
        lines.append("- 理由: " + "、".join(_inline(reason) for reason in record.blocked_reasons))
    lines.append(f"- 文字数: {record.char_count} / バイト数: {record.byte_count}")
    lines.append("- sources:")
    for source in record.sources:
        span = f" L{source.line_start}-L{source.line_end}" if source.line_start is not None else ""
        lines.append(f"  - {_inline(source.kind)} {_inline(source.path)}{span}")
    lines.append("")
    if record.sections is None:
        return [*lines, "秘密の疑いがあるため本文を表示しない。", ""]
    for key, text in record.sections.items():
        lines += [f"### {_inline(key)}", "", *_fenced(text), ""]
    return lines


def _states_markdown(records: Sequence[StateRecord], *, mode: Mode, plan: _Plan) -> str:
    sendable = sum(1 for record in records if record.allowed)
    awaiting = sum(1 for record in records if record.awaiting_approval)
    blocked = len(records) - sendable - awaiting
    lines = [
        "# 送信前確認（review_triage）",
        "",
        "> Jev へ送る本文（state）の候補である。"
        "本文を読み、送ってよい state の `content_sha256` を許可ファイルへ 1 行ずつ書き、"
        "`--live --allow-file FILE` で実行する。"
        "秘密の疑いがある state は本文を表示しない。"
        "本文中の制御文字・書式文字は `\\uXXXX` などの表記で表示している（送る本文は元のまま）。",
        "",
        "## 概要",
        "",
        f"- mode: {_inline(mode)}",
        f"- repository: {_inline(plan.batch.repository.value)}",
        f"- review_dir: {_inline(plan.batch.review_dir)}",
        f"- reviewed_head: {_inline(plan.reviewed_head)}",
        f"- model: {_inline(DEFAULT_MODEL_ID)}",
        f"- state: {len(records)} 件（送信可 {sendable}、許可待ち {awaiting}、送信不可 {blocked}）",
        "",
    ]
    if not records:
        lines += ["送信候補の state はない。", ""]
    for record in records:
        lines += _state_lines(record)
    return "\n".join(lines).rstrip("\n") + "\n"


def _run_record(
    options: _Options,
    plan: _Plan,
    records: Sequence[StateRecord],
    outcomes: Sequence[ClientOutcome],
    unmatched: int,
    report: TriageReport,
) -> RunRecord:
    batch = plan.batch
    counts = RunCounts(
        files=len(batch.files),
        findings=len(batch.findings),
        active_findings=len(batch.active_findings()),
        unparsed=len(batch.unparsed),
        anomalies=len(batch.anomalies),
        states=len(records),
        finding_states=sum(1 for record in records if record.purpose == "finding"),
        pair_states=sum(1 for record in records if record.purpose == "pair"),
        allowed_states=sum(1 for record in records if record.allowed),
        awaiting_approval=sum(1 for record in records if record.awaiting_approval),
        withheld_states=sum(1 for record in records if record.withheld),
        requests=len(plan.requests),
        calls_made=sum(outcome.calls_made for outcome in outcomes) if options.mode == "live" else 0,
        outcomes=len(outcomes),
        outcomes_unmatched=unmatched,
        allowed_hashes_unused=len(plan.inputs.allowed - {record.content_sha256 for record in records}),
    )
    return RunRecord(
        mode=options.mode,
        repository=batch.repository,
        review_dir=batch.review_dir,
        reviewed_head=plan.reviewed_head,
        diff_base=plan.diff_base,
        model_requested=DEFAULT_MODEL_ID,
        question_set_version=QUESTION_SET_VERSION,
        thresholds=plan.inputs.thresholds,
        generated_at=report.generated_at,
        allow_file_sha256=plan.inputs.allow_file_sha256,
        response_file_sha256=plan.inputs.response_file_sha256,
        thresholds_file_sha256=plan.inputs.thresholds_file_sha256,
        counts=counts,
        anomalies=batch.anomalies,
        unparsed=batch.unparsed,
        evidence=plan.evidence,
        report_status=report.status,
        report_reason=report.reason,
    )


# --- 標準エラーの要約 -----------------------------------------------------------------


def _say(message: str) -> None:
    print(f"{_PROGRAM}: {_display(message)}", file=sys.stderr)


def _summary(options: _Options, run: RunRecord, report: TriageReport, outcomes: Sequence[ClientOutcome]) -> list[str]:
    """件数・status・reason・パスだけの要約。本文・キー・応答を含めない。"""
    counts = run.counts
    blocked = counts.states - counts.allowed_states - counts.awaiting_approval
    lines = [
        f"mode={run.mode} findings={counts.findings} active={counts.active_findings} "
        f"unparsed={counts.unparsed} anomalies={counts.anomalies}",
        f"states={counts.states} (finding {counts.finding_states}, pair {counts.pair_states}) "
        f"sendable={counts.allowed_states} awaiting_approval={counts.awaiting_approval} blocked={blocked} "
        f"withheld={counts.withheld_states}",
    ]
    if run.mode == "live":
        lines.append(f"requests={counts.requests} calls_made={counts.calls_made}")
    elif run.mode == "replay":
        lines.append(f"outcomes used={counts.outcomes} unmatched={counts.outcomes_unmatched}")
    else:
        lines.append("dry-run: no request was sent")
    reason = report.reason if len(report.reason) <= _REASON_LIMIT else report.reason[: _REASON_LIMIT - 3] + "..."
    lines.append(f"report status={report.status.value}: {reason}")
    names = ["report.md", "report.json", "states.md", "states.json"]
    names += ["outcomes.json"] if run.mode != "dry-run" else []
    lines.append(f"wrote {os.fspath(options.output_dir)}: {' '.join([*names, 'run.json'])}")
    if any(outcome.error is not None and outcome.error.kind is ErrorKind.NO_KEY for outcome in outcomes):
        lines.append(
            "TYPESAFE_API_KEY is not set or unusable; no request was sent (see tools/review_triage/access.rst)"
        )
    if run.mode == "dry-run" and counts.awaiting_approval:
        lines.append(
            f"to send: check {os.fspath(options.output_dir / 'states.md')}, list the approved content_sha256 values "
            "in an allow file, and rerun with --live --allow-file FILE"
        )
    if report.unparsed_count:
        lines.append(
            f"{report.unparsed_count} unparsed block(s) were not triaged: check {os.fspath(options.review_dir)} "
            "with the existing review-fix-planning procedure"
        )
    human = sum(1 for decision in report.decisions if decision.proposal is Proposal.HUMAN_REVIEW)
    if human:
        lines.append(
            f"{human} of {len(report.decisions)} finding(s) need human review: continue with the existing "
            f"review-fix-planning procedure for {os.fspath(options.review_dir)}"
        )
    return lines


# --- 入口 ----------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """CLI の入口。終了コード 0 = レポートを生成、2 = 入力・利用方法のエラー。

    ``environ`` は API キーの読み出し元（``None`` なら ``os.environ``）、``transport`` は live の差し替え口、
    ``now`` は ``generated_at`` の時計（いずれも試験用）。
    """
    parser = _parser()
    try:
        options = _options(parser, parser.parse_args(argv))
    except SystemExit as exc:  # argparse の --help（0）と解析エラー（2）
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        plan = _prepare(options)
        _create_output_dir(options.output_dir)
    except _UsageError as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    out = options.output_dir
    records = tuple(_state_record(index, state) for index, state in enumerate(plan.states, start=1))
    _write(out / "states.json", _STATE_RECORDS.dump_json(list(records), indent=2).decode() + "\n")
    _write(out / "states.md", _states_markdown(records, mode=options.mode, plan=plan))
    outcomes, unmatched = _collect_outcomes(options, plan, environ=environ, transport=transport)
    if options.mode != "dry-run":
        _write(out / "outcomes.json", _OUTCOME_LIST.dump_json(list(outcomes), indent=2).decode() + "\n")
    generated_at = (now or _utc_now)().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = triage(plan.batch, outcomes, thresholds=plan.inputs.thresholds, generated_at=generated_at)
    _write(out / "report.json", report.model_dump_json(indent=2) + "\n")
    _write(out / "report.md", render_report(report))
    run = _run_record(options, plan, records, outcomes, unmatched, report)
    _write(out / "run.json", run.model_dump_json(indent=2) + "\n")
    for line in _summary(options, run, report, outcomes):
        _say(line)
    return EXIT_OK
