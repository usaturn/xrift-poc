"""レビュー文書を ReviewBatch に分解する（#383 input）。

``review_dir`` 直下の通常ファイル ``*.md`` だけを読む。
``review_dir`` を一度だけ開いてその記述子からの相対名で読むので、途中で差し替えられても外を読まない。
symlink・サブディレクトリ・本文に書かれたパスはたどらない。
ネットワーク・サブプロセス・環境変数・Git を使わず、書き込みもしない。
レビュー本文はデータとしてだけ扱い、本文中の命令・パス・URL を実行・参照しない。
読まなかった項目と解釈できなかったブロックは捨てず、``anomalies`` と ``unparsed`` に残す。
"""

import hashlib
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from tools.review_triage.models import (
    FindingStatus,
    Marker,
    Repository,
    ReviewBatch,
    ReviewFile,
    ReviewFinding,
    ReviewHistoryRow,
    Severity,
    UnparsedBlock,
    finding_key,
)

# 1 ファイルの上限（バイト）。実データの最大 45,024 バイトに十分な余裕を持たせつつ、
# 異常に大きなファイルは読まない（spec D-13）
REVIEW_FILE_BYTE_LIMIT = 1_000_000

_HEAD = re.compile(r"[0-9a-f]{7,40}")
_HEADER = re.compile(
    r"- 対象: (?:PR #(?P<pr>[1-9][0-9]*)|\[PR #(?P<pr_link>[1-9][0-9]*)\]\([^)]*\))"
    r" / head `?(?P<head>[0-9a-f]{7,40})`? / (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})(?: .*)?"
)
_FENCE = re.compile(r"`{3,}|~{3,}")
_FINDING_HEADING = re.compile(r"### (?P<code>[A-Z]{1,2}-[0-9]+)[:：][ \t]*(?P<rest>.*)")
_FIELD = re.compile(r"- (?P<name>[^:：\s][^:：]{0,19}?)[:：][ \t]?(?P<value>.*)")
_DETECTED_HEAD = re.compile(r"`?(?P<head>[0-9a-f]{7,40})(?![0-9A-Za-z])")
_TABLE_SEPARATOR = re.compile(r":?-+:?")
_FINDING_CODE = re.compile(r"[A-Z]{1,2}-[0-9]+")

_ACTIVE_SECTIONS: dict[str, Severity] = {
    "Critical": Severity.CRITICAL,
    "High": Severity.HIGH,
    "Medium": Severity.MEDIUM,
    "Low": Severity.LOW,
    "Info": Severity.INFO,
}
_CARRIED_SECTIONS: dict[str, FindingStatus] = {
    "Resolved findings": FindingStatus.RESOLVED,
    "Pending previous findings": FindingStatus.PENDING,
    "Unverified candidates": FindingStatus.UNVERIFIED,
    "Decided findings": FindingStatus.DECIDED,
}
_CODE_SEVERITY: dict[str, Severity] = {
    "C": Severity.CRITICAL,
    "H": Severity.HIGH,
    "M": Severity.MEDIUM,
    "L": Severity.LOW,
    "I": Severity.INFO,
}
_MARKERS: dict[str, Marker] = {
    "検証済み": Marker.VERIFIED,
    "要設計判断": Marker.DESIGN_JUDGMENT,
    "確認推奨": Marker.CONFIRM_RECOMMENDED,
}
# missing の判定に使う期待箇条（期待順）。_EXPECTED_ACTIVE は active と pending の両方に使う
_EXPECTED_ACTIVE = ("位置", "検出時 HEAD", "内容", "根拠", "推奨対応")
_EXPECTED_RESOLVED = ("検出時 HEAD", "解消確認時 HEAD", "確認根拠")
# 候補・判断済み記録は active の根拠欄を要求せず、固有の欄を body に保つ
_EXPECTED_RECORDED = ("位置", "検出時 HEAD")
# 値を使う箇条名（フィールドと missing の判定）。これ以外の名前は body に残るだけで、重複しても記録しない
_KNOWN_FIELDS = frozenset(_EXPECTED_ACTIVE + _EXPECTED_RESOLVED)
# 「指摘なし」とみなす 1 行の記述の定型。行全体で照合するので、既定語を含むだけの散文や、箇条・見出し・引用・表の行は
# 一致しない。括弧の中にはバッククォートと入れ子の括弧を許さない（コード片を含む括弧書きは指摘の可能性がある）
_NO_FINDINGS = re.compile(
    r"(?:[（(](?:該当|指摘|特に)?(?:なし|ありません|省略|N/A|None)[）)]"  # （該当なし）
    r"|(?:該当|指摘|特に)?(?:なし|ありません|省略|N/A|None)(?:[（(][^（()）`]*[）)])?"  # 該当なし（初回のため）
    r"|[（(][^（()）`]*(?:なし|ありません|省略)[）)]"  # （初回レビューのため省略）
    r")[。.]?"
)
# 「指摘なし」とみなす 1 行の記述の最大文字数。実データの記述 30 種を集計して決めた（spec D-14）
_NO_FINDINGS_MAX_CHARS = 40
_HISTORY_SECTION = "Review history"

# 読み込み判定表（spec「読み込み対象と安全性」）の anomaly の文言。表で固定された文言なので、ここだけで定義する
_SKIP_HIDDEN = "ignored hidden entry"
_SKIP_SYMLINK = "skipped symlink"
_SKIP_DIRECTORY = "ignored directory"
_SKIP_NON_MARKDOWN = "ignored non-markdown entry"
_SKIP_NON_REGULAR = "skipped non-regular file"
_SKIP_TOO_LARGE = "skipped, larger than {limit} bytes"
_SKIP_UNREADABLE = "skipped, unreadable ({error})"
_SKIP_NOT_UTF8 = "skipped, not valid UTF-8"


class ReviewInputError(ValueError):
    """review_dir・repository・reviewed_head の指定が不正。"""


class _Skip(Exception):
    """読まなかった項目。引数は anomaly に書く理由（読み込み判定表の文言）。"""


def _too_large() -> _Skip:
    """呼び出した時点の ``REVIEW_FILE_BYTE_LIMIT`` で上限超過の ``_Skip`` を作る。"""
    return _Skip(_SKIP_TOO_LARGE.format(limit=REVIEW_FILE_BYTE_LIMIT))


def _unreadable(error: OSError) -> _Skip:
    """例外メッセージ（パスを含み得る）は使わず、例外クラス名だけを記録する。"""
    return _Skip(_SKIP_UNREADABLE.format(error=type(error).__name__))


class _Section(NamedTuple):
    """節の種類。``severity`` を使うのは active の節だけで、_classify_section が必ず設定する。"""

    name: str
    status: FindingStatus | None = None
    severity: Severity = Severity.UNKNOWN
    history: bool = False


class _Context:
    """1 ファイルの解析状態。"""

    def __init__(self, *, source_file: str, repository: Repository, review_dir: str, reviewed_head: str) -> None:
        self.source_file = source_file
        self.model_name = source_file.removesuffix(".md")
        self.repository = repository
        self.review_dir = review_dir
        self.reviewed_head = reviewed_head
        self.lines: list[str] = []
        self.fenced: list[bool] = []
        self.header_head: str | None = None
        self.findings: list[ReviewFinding] = []
        self.unparsed: list[UnparsedBlock] = []
        self.anomalies: list[str] = []
        self.used_ids: set[str] = set()
        # finding_id の基底ごとに、次に試す接尾辞の番号。同じ code が k 回あっても ~2 から探し直さない
        self.next_suffix: dict[str, int] = {}

    def note(self, line: int | None, message: str) -> None:
        """anomaly を記録する。

        ``line`` は 1 始まりのファイルの行番号（内部の 0 始まりの添字 + 1）。``None`` はファイル単位の anomaly。
        """
        where = self.source_file if line is None else f"{self.source_file}:{line}"
        self.anomalies.append(f"{where}: {message}")


def load_reviews(review_dir: Path, *, repository: Repository, reviewed_head: str) -> ReviewBatch:
    """``review_dir`` 直下のレビューを ``ReviewBatch`` に分解する。

    ``repository`` は呼び出し側が明示する（``str`` も受け付けて正規化する）。
    指定が不正なら ``ReviewInputError``。読まなかった項目は例外にせず ``anomalies`` に記録する。
    """
    try:
        repo = Repository(repository)
    except ValueError as exc:
        raise ReviewInputError(f"unknown repository: {repository!r}") from exc
    if not _HEAD.fullmatch(reviewed_head):
        raise ReviewInputError(f"reviewed_head must be 7 to 40 lowercase hex characters: {reviewed_head!r}")
    shown_dir = _display_name(os.fspath(review_dir))
    if review_dir.is_symlink() or not review_dir.is_dir():
        raise ReviewInputError(f"review_dir must be an existing directory and not a symlink: {shown_dir}")
    dir_name = review_dir.resolve().name
    if not dir_name:
        raise ReviewInputError(f"review_dir has no directory name: {shown_dir}")
    if _display_name(dir_name) != dir_name:
        raise ReviewInputError(f"review_dir name is not valid UTF-8: {_display_name(dir_name)}")
    # 以後は開いた記述子からの相対名で読む。記述子がディレクトリを固定するので、差し替えられても外を読まない
    try:
        dir_fd = os.open(review_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ReviewInputError(f"review_dir cannot be opened: {shown_dir}") from exc
    try:
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            raise ReviewInputError(f"review_dir cannot be listed: {shown_dir}") from exc
        return _load_entries(dir_fd, names, repository=repo, review_dir=dir_name, reviewed_head=reviewed_head)
    finally:
        os.close(dir_fd)


def _load_entries(
    dir_fd: int, names: Sequence[str], *, repository: Repository, review_dir: str, reviewed_head: str
) -> ReviewBatch:
    files: list[ReviewFile] = []
    findings: list[ReviewFinding] = []
    unparsed: list[UnparsedBlock] = []
    anomalies: list[str] = []
    for name in names:
        try:
            text = _read_review_text(dir_fd, name)
        except _Skip as skip:
            anomalies.append(f"{_display_name(name)}: {skip}")
            continue
        context = _Context(source_file=name, repository=repository, review_dir=review_dir, reviewed_head=reviewed_head)
        files.append(_parse_review(text, context))
        findings.extend(context.findings)
        unparsed.extend(context.unparsed)
        anomalies.extend(context.anomalies)
    if not files:
        anomalies.append("no review files")
    return ReviewBatch(
        repository=repository,
        review_dir=review_dir,
        reviewed_head=reviewed_head,
        files=tuple(files),
        findings=tuple(findings),
        unparsed=tuple(unparsed),
        anomalies=tuple(anomalies),
    )


def _display_name(name: str) -> str:
    """UTF-8 として不正なバイト（surrogateescape で ``\\udcXX`` になった文字）を ``\\xff`` の形にした名前。

    anomaly と例外の文言に使う。名前が正しい UTF-8 なら ``name`` そのものを返す。
    """
    return os.fsencode(name).decode("utf-8", "backslashreplace")


def _read_review_text(dir_fd: int, name: str) -> str:
    """読む対象なら本文を返し、読まない項目は理由付きの ``_Skip`` を送出する。

    判定は読み込み判定表の順（hidden → symlink → ディレクトリ → 非 md → 非通常ファイル → サイズ → 読み込み → UTF-8）。
    名前が UTF-8 として不正な ``.md`` は、開く前に ``not valid UTF-8`` として読み飛ばす。
    """
    if name.startswith("."):
        raise _Skip(_SKIP_HIDDEN)
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise _unreadable(exc) from exc
    if stat.S_ISLNK(info.st_mode):
        raise _Skip(_SKIP_SYMLINK)
    if stat.S_ISDIR(info.st_mode):
        raise _Skip(_SKIP_DIRECTORY)
    if not name.endswith(".md"):
        raise _Skip(_SKIP_NON_MARKDOWN)
    if not stat.S_ISREG(info.st_mode):
        raise _Skip(_SKIP_NON_REGULAR)
    if info.st_size > REVIEW_FILE_BYTE_LIMIT:
        raise _too_large()
    if _display_name(name) != name:
        raise _Skip(_SKIP_NOT_UTF8)
    try:
        data = _read_regular_file(dir_fd, name)
    except OSError as exc:
        raise _unreadable(exc) from exc
    if len(data) > REVIEW_FILE_BYTE_LIMIT:
        raise _too_large()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Skip(_SKIP_NOT_UTF8) from exc


def _read_regular_file(dir_fd: int, name: str) -> bytes:
    """``dir_fd`` からの相対名 ``name`` を ``O_NOFOLLOW | O_NONBLOCK`` で開き、上限 + 1 バイトまで読む。

    開いた後も ``os.fstat`` で通常ファイルかと上限以下かを確かめ、そうでなければ ``_Skip`` を送出する。
    ``O_NONBLOCK`` は lstat の後に FIFO へ差し替えられた項目で ``open`` が止まらないようにするため。
    上限 + 1 バイトまで読むのは、fstat の後に伸びたファイルを切り詰めずに上限超過と判定するため。
    """
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _Skip(_SKIP_NON_REGULAR)
        if info.st_size > REVIEW_FILE_BYTE_LIMIT:
            raise _too_large()
        chunks: list[bytes] = []
        remaining = REVIEW_FILE_BYTE_LIMIT + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_review(text: str, context: _Context) -> ReviewFile:
    context.lines = _split_lines(text)
    target_pr, header_date = _parse_header(context)
    context.fenced = _fence_mask(context)
    history = _walk(context)
    return ReviewFile(
        source_file=context.source_file,
        model_name=context.model_name,
        target_pr=target_pr,
        header_head=context.header_head,
        header_date=header_date,
        history=tuple(history),
    )


def _split_lines(text: str) -> list[str]:
    """``\\n`` だけで行に分ける。``str.splitlines()`` は ``\\x0b`` などでも分割し、行番号がずれる。"""
    lines = [line.removesuffix("\r") for line in text.removeprefix("\ufeff").split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _parse_header(context: _Context) -> tuple[int | None, str | None]:
    """1 行目から PR 番号と日付を読み、``header_head`` を設定する。HEAD の混在を記録する。"""
    match = _HEADER.fullmatch(context.lines[0]) if context.lines else None
    target_pr = None
    if match is not None:
        try:
            target_pr = int(match.group("pr") or match.group("pr_link"))
        except ValueError:  # int の桁数上限（既定 4300 桁）を超える PR 番号。1 ファイルの問題で全体を止めない
            target_pr = None
    if match is None or target_pr is None:
        context.note(1, "unrecognized header line")
        return None, None
    head = match.group("head")
    context.header_head = head
    if not (head.startswith(context.reviewed_head) or context.reviewed_head.startswith(head)):
        context.note(None, f"header head {head} differs from reviewed_head {context.reviewed_head}")
    return target_pr, match.group("date")


def _fence_mask(context: _Context) -> list[bool]:
    """コードフェンスの内側（開始行と終了行を含む）を真にする。閉じないフェンスは無視して記録する。

    CommonMark と同じく、バッククォートの開始は後ろ（info 文字列）にバッククォートを含む行では開かない。
    行頭のインラインコード（```` ```x``` ````）を開始とみなすと、別のフェンスの終了行までの見出しが隠れるため。
    後ろに終了行の候補が無い開始は走査せずに閉じないと判定し、閉じる場合だけ最初の終了行まで走査する。
    閉じた範囲は読み飛ばすので、全体で行数に比例する時間で済む。
    """
    lines = context.lines
    fenced = [False] * len(lines)
    longest_closer = _longest_closers(lines)
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        opener = _FENCE.match(stripped)
        if opener is None or (stripped[0] == "`" and "`" in stripped[opener.end() :]):
            index += 1
            continue
        fence = opener.group()
        closable = longest_closer[fence[0]][index + 1] >= len(fence)
        close = _fence_close(lines, index + 1, fence) if closable else None
        if close is None:
            context.note(index + 1, "unclosed code fence ignored")
            index += 1
            continue
        for covered in range(index, close + 1):
            fenced[covered] = True
        index = close + 1
    return fenced


def _longest_closers(lines: Sequence[str]) -> dict[str, list[int]]:
    """フェンス文字ごとに、``i`` 行目以降にある終了行の候補の最大の長さ（無ければ 0）を ``[i]`` に持つ表を返す。

    終了行の候補は、前後の空白を除くとその文字だけが 3 個以上続く行。
    長さ ``n`` の開始の後ろに終了行があるのは、次の行からの最大が ``n`` 以上のときに限る。
    """
    longest = {char: [0] * (len(lines) + 1) for char in ("`", "~")}
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        closer = stripped[0] if len(stripped) >= 3 and stripped == stripped[0] * len(stripped) else ""
        for char, maxima in longest.items():
            maxima[index] = max(maxima[index + 1], len(stripped) if char == closer else 0)
    return longest


def _fence_close(lines: Sequence[str], start: int, opener: str) -> int | None:
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if len(stripped) >= len(opener) and stripped == opener[0] * len(stripped):
            return index
    return None


def _walk(context: _Context) -> list[ReviewHistoryRow]:
    """節・見出し・本文を順に読み、指摘と unparsed を ``context`` に積む。履歴の行を返す。"""
    lines, fenced = context.lines, context.fenced
    history: list[ReviewHistoryRow] = []
    section = _Section("")
    paragraph: list[int] = []
    table_row = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if not fenced[index] and line.startswith(("# ", "## ")):
            _flush_preamble(context, paragraph)
            section = _classify_section(line)
            table_row = 0
        elif not fenced[index] and line.startswith("### "):
            _flush_preamble(context, paragraph)
            end = _block_end(context, index + 1)
            _heading_block(context, section, index, end)
            if not section.history:
                index = end
                continue
            # Review history 節の境界は # と ## だけなので、### の小見出しの後も表の行を読み続ける
            table_row = 0
        elif section.status in (FindingStatus.ACTIVE, FindingStatus.RESOLVED, FindingStatus.PENDING):
            if line.strip():
                paragraph.append(index)
            else:
                _flush_preamble(context, paragraph)
        elif section.history:
            # フェンス内の行は表の行にしない。フェンスで表は途切れたものとして扱う
            table_row = 0 if fenced[index] else _history_line(context, index, table_row, history)
        index += 1
    _flush_preamble(context, paragraph)
    return history


def _classify_section(line: str) -> _Section:
    """``## `` の後ろ（末尾の空白だけ除く）で節を判定する。``# `` の見出しは指摘の節にも履歴の節にもならない。"""
    if not line.startswith("## "):
        return _Section(line.removeprefix("# ").rstrip())
    name = line.removeprefix("## ").rstrip()
    if name in _ACTIVE_SECTIONS:
        return _Section(name, FindingStatus.ACTIVE, _ACTIVE_SECTIONS[name])
    if name in _CARRIED_SECTIONS:
        return _Section(name, _CARRIED_SECTIONS[name])
    return _Section(name, history=name == _HISTORY_SECTION)


def _history_line(context: _Context, index: int, table_row: int, history: list[ReviewHistoryRow]) -> int:
    """Review history 節の 1 行を読み、表の中での次の行番号（表の外なら 0）を返す。"""
    line = context.lines[index]
    if not line.startswith("|"):
        return 0
    row = _history_row(line)
    if row is not None:
        history.append(row)
    elif table_row > 0 and not _is_separator(line):
        context.note(index + 1, "unrecognized review history row")
    return table_row + 1


def _table_cells(line: str) -> list[str]:
    """両端の ``|`` を 1 つずつ除いて ``|`` で分け、各セルの前後の空白を除く。"""
    return [cell.strip() for cell in line.strip().removeprefix("|").removesuffix("|").split("|")]


def _history_row(line: str) -> ReviewHistoryRow | None:
    cells = _table_cells(line)
    if len(cells) != 3:
        return None
    reviewed_at, head, mode = cells[0], cells[1].strip("`"), cells[2]
    if not reviewed_at or not mode or not _HEAD.fullmatch(head):
        return None
    return ReviewHistoryRow(reviewed_at=reviewed_at, head=head, mode=mode)


def _is_separator(line: str) -> bool:
    return all(_TABLE_SEPARATOR.fullmatch(cell) for cell in _table_cells(line))


def _block_end(context: _Context, start: int) -> int:
    """``start`` 以降で最初のフェンス外の ``#`` / ``##`` / ``###`` 見出しの位置（無ければ行数）。"""
    for index in range(start, len(context.lines)):
        if not context.fenced[index] and context.lines[index].startswith(("# ", "## ", "### ")):
            return index
    return len(context.lines)


def _last_content(lines: Sequence[str], start: int, end: int) -> int:
    last = start
    for index in range(start, end):
        if lines[index].strip():
            last = index
    return last


def _add_unparsed(context: _Context, start: int, end: int, reason: str) -> None:
    """0 始まりの ``start``〜``end - 1`` から末尾の空行を除いた範囲を、原文なしで記録する。"""
    last = _last_content(context.lines, start, end)
    text = "\n".join(context.lines[start : last + 1])
    context.unparsed.append(
        UnparsedBlock(
            source_file=context.source_file,
            line_start=start + 1,
            line_end=last + 1,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            reason=reason,
        )
    )


def _flush_preamble(context: _Context, paragraph: list[int]) -> None:
    """指摘の節で最初の見出しより前にある段落を、「指摘なし」の記述でなければ unparsed にする。

    ``paragraph`` に行を積むのは指摘の節の中だけで、節が変わる前に必ずこの関数で空にするので、節は確かめない。
    """
    if not paragraph:
        return
    if not _is_no_findings_statement([context.lines[index] for index in paragraph]):
        _add_unparsed(context, paragraph[0], paragraph[-1] + 1, "text outside a finding heading")
    paragraph.clear()


def _is_no_findings_statement(lines: Sequence[str]) -> bool:
    """行全体が ``（該当なし）`` のような定型に一致する 1 行の記述か。長文と指摘コードを含む文は該当しない。"""
    if len(lines) != 1:
        return False
    text = lines[0].strip()
    return (
        len(text) <= _NO_FINDINGS_MAX_CHARS
        and _FINDING_CODE.search(text) is None
        and _NO_FINDINGS.fullmatch(text) is not None
    )


def _heading_block(context: _Context, section: _Section, start: int, end: int) -> None:
    match = _FINDING_HEADING.fullmatch(context.lines[start])
    status = section.status
    if status is None:
        if match is not None:
            _add_unparsed(context, start, end, "finding heading outside a finding section")
        return
    if match is None:
        _add_unparsed(context, start, end, "unrecognized finding heading")
        return
    finding = _make_finding(context, section, status, start, end, match)
    if finding is None:
        _add_unparsed(context, start, end, "finding heading without title")
        return
    context.findings.append(finding)


def _make_finding(
    context: _Context, section: _Section, status: FindingStatus, start: int, end: int, match: re.Match[str]
) -> ReviewFinding | None:
    code = match.group("code")
    title, marker, unknown_marker = _split_marker(match.group("rest").strip())
    if not title:
        return None
    heading_line = start + 1
    if unknown_marker:
        # anomaly にはレビューの原文（マーカの中身）を入れない。未知のマーカは title に残る
        context.note(heading_line, "unknown marker 【...】")
    letter_severity = _CODE_SEVERITY.get(code.split("-")[0])
    if status is FindingStatus.ACTIVE:
        severity = section.severity
        if letter_severity is not severity:
            context.note(heading_line, f"code {code} does not match section {section.name}")
    elif status is FindingStatus.UNVERIFIED:
        severity = Severity.UNKNOWN
    else:
        severity = letter_severity or Severity.UNKNOWN
    last = _last_content(context.lines, start, end)
    fields = _parse_fields(context, start + 1, last + 1)
    detected_head = _detected_head(context, heading_line, fields.get("検出時 HEAD"))
    finding_id = _unique_finding_id(context, heading_line, f"{context.model_name}/{status.value}/{code}")
    if status is FindingStatus.RESOLVED:
        expected = _EXPECTED_RESOLVED
    elif status in (FindingStatus.UNVERIFIED, FindingStatus.DECIDED):
        expected = _EXPECTED_RECORDED
    else:
        expected = _EXPECTED_ACTIVE
    return ReviewFinding(
        finding_id=finding_id,
        finding_key=finding_key(context.repository, context.review_dir, finding_id),
        repository=context.repository,
        review_dir=context.review_dir,
        source_file=context.source_file,
        model_name=context.model_name,
        line_start=heading_line,
        line_end=last + 1,
        reviewed_head=context.header_head,
        detected_head=detected_head,
        severity=severity,
        status=status,
        code=code,
        title=title,
        marker=marker,
        body="\n".join(context.lines[start : last + 1]),
        location=fields.get("位置") or None,
        content=fields.get("内容") or None,
        evidence=fields.get("根拠") or None,
        recommendation=fields.get("推奨対応") or None,
        missing=tuple(name for name in expected if not fields.get(name)),
    )


def _split_marker(rest: str) -> tuple[str, Marker, bool]:
    """見出し末尾のマーカを分け、(title, marker, 未知のマーカがあるか) を返す。未知のマーカは title に残す。

    末尾の ``】`` と、その手前にある最後の ``】`` より後で最初の ``【`` との間をマーカとする。
    ``(?P<title>.*?)【(?P<marker>[^】]*)】`` の fullmatch と同じ結果になる（``rest`` は改行を含まない 1 行）。
    この正規表現は閉じない ``【`` が並ぶ行でバックトラックが二乗時間になるため、文字列の検索で線形時間に分ける。
    """
    if not rest.endswith("】"):
        return rest, Marker.NONE, False
    last = len(rest) - 1
    opening = rest.find("【", rest.rfind("】", 0, last) + 1, last)
    if opening < 0:
        return rest, Marker.NONE, False
    marker = _MARKERS.get(rest[opening + 1 : last])
    if marker is None:
        return rest, Marker.NONE, True
    return rest[:opening].strip(), marker, False


def _parse_fields(context: _Context, start: int, end: int) -> dict[str, str]:
    """フェンス外で字下げのない ``- 名前: 値`` を箇条として読み、値を使う名前だけを返す。

    空行・空白で始まる行・フェンス内の行は継続行。名前は空白を除かずに照合し、同じ名前は最初の値を採る。
    """
    lines, fenced = context.lines, context.fenced
    values: dict[str, str] = {}
    index = start
    while index < end:
        match = None if fenced[index] else _FIELD.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        bullet_line = index + 1
        name = match.group("name")
        parts = [match.group("value")]
        index += 1
        while index < end and (fenced[index] or not lines[index].strip() or lines[index].startswith((" ", "\t"))):
            parts.append(lines[index].removeprefix("  "))
            index += 1
        if name not in _KNOWN_FIELDS:
            continue
        if name in values:
            context.note(bullet_line, f"duplicate field {name}; first value kept")
            continue
        values[name] = "\n".join(parts).strip()
    return values


def _detected_head(context: _Context, heading_line: int, value: str | None) -> str | None:
    if not value:
        return None
    match = _DETECTED_HEAD.match(value)
    if match is None:
        context.note(heading_line, "unrecognized 検出時 HEAD value")
        return None
    return match.group("head")


def _unique_finding_id(context: _Context, heading_line: int, base: str) -> str:
    """同じファイルで使われた ID なら ``~2``・``~3``… を付ける。

    基底ごとに次の接尾辞を覚えるので、同じ code の重複が k 件でも全体で O(k) で済む。
    ``used_ids`` の確認は、別の基底から同じ ID ができた場合の安全策として残す。
    """
    finding_id, suffix = base, context.next_suffix.get(base, 2)
    while finding_id in context.used_ids:
        finding_id, suffix = f"{base}~{suffix}", suffix + 1
    context.next_suffix[base] = suffix
    if finding_id != base:
        context.note(heading_line, f"duplicate finding id {base}; renamed to {finding_id}")
    context.used_ids.add(finding_id)
    return finding_id
