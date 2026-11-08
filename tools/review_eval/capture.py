"""収集: 範囲・同一性・パターン・秘密検査・容量・sweep・形式の判別。

`collect()` は `Exception` を呼び出し元へ出さない。標準エラーには失敗・保留のときだけ 1 行
（`review-eval: <status>: <reason>`）を出し、本文・秘密の値は出さない。
"""

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import JsonValue

from tools.review_eval import shim
from tools.review_eval.canonical import hfile
from tools.review_eval.config import Config, ConfigError, Route, Source, check_registered, load_config
from tools.review_eval.formats import FULL_SHA, Described, describe, pr_mismatch, withheld
from tools.review_eval.models import Body, Failure, FailureKind, InputRef, Observation, Trigger, observation_identity
from tools.review_eval.scanner import ScanResult, scan
from tools.review_eval.store import Store, StoreError

ReportStatus = Literal["disabled", "out_of_scope", "store_not_initialized", "captured", "partial", "held", "failed"]

COLLECTOR_VERSION = "1"
QUIET_STATUSES: frozenset[str] = frozenset({"disabled", "out_of_scope", "captured"})
_HOLD_KINDS: frozenset[str] = frozenset({"capacity_held", "lock_timeout", "legacy_layout", "sweep_limit"})

REVIEW_DIR_NAME = re.compile(r"#(?P<number>[1-9][0-9]*)-.+")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_REPO = re.compile(r"[A-Za-z0-9._-]+")
_RUN_ID = re.compile(r"[0-9a-f]{32}")
_CYCLE_DIR = re.compile(r"cycle-[1-9][0-9]*")
_CYCLE_FILE = re.compile(r"cycle-[1-9][0-9]*\.json")
_CYCLE_ATTACHMENTS = frozenset({"comments.json", "review-result.json", "rerun-result.json"})
# sweep が下りるのは、許可パターンを含むディレクトリだけ（最も深いのは loop/<run>/cycle-<n>/diagnostics/）
_SWEEP_ROOTS = frozenset({"findings", "runs", "verified", "loop"})
_SWEEP_DIR_DEPTH = 4
# 無い・symlink（O_NOFOLLOW）・途中がディレクトリでない・特殊ファイルは、取らないだけで失敗にしない
_SKIP_ERRNOS = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.ENXIO})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CHUNK = 1 << 20


class CaptureError(Exception):
    def __init__(self, kind: str, message: str | None = None) -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True)
class VirtualRecord:
    """ファイルとして残らない記録（判断の記録・finalize の結果）。`path` は `@` 始まり。"""

    path: str
    data: bytes


@dataclass
class CollectionReport:
    status: ReportStatus
    created: list[str]
    duplicates: int
    failures: list[str]
    message: str | None


def classify(relpath: str) -> Route | None:
    """review_dir からの相対パスを、許可パターン（Spec 4.3 の 4）の経路に分ける。除外・パターン外は None。"""
    path = PurePosixPath(relpath)
    parts = path.parts
    if not parts or path.is_absolute() or any(part.startswith(".") for part in parts) or parts[-1].endswith(".log"):
        return None
    name = parts[-1]
    match parts:
        case (str(),) if name.endswith(".md"):
            return "pr-review"
        case ("findings" | "runs", str()) if name.endswith(".json"):
            return "pr-review"
        case ("verified", str()) if name.endswith(".planning.json"):
            return "review-fix-planning"
        case ("verified", str()) if name.endswith((".json", ".md")):
            return "review-verify"
        case ("loop", run_id, record) if _RUN_ID.fullmatch(run_id) and (
            record == "run.json" or _CYCLE_FILE.fullmatch(record)
        ):
            return "pr-review-fix-loop"
        case ("loop", run_id, cycle, record) if (
            _RUN_ID.fullmatch(run_id) and _CYCLE_DIR.fullmatch(cycle) and record in _CYCLE_ATTACHMENTS
        ):
            return "pr-review-fix-loop"
        case ("loop", run_id, cycle, "diagnostics" | "requests", str()) if (
            _RUN_ID.fullmatch(run_id) and _CYCLE_DIR.fullmatch(cycle) and name.endswith(".json")
        ):
            return "pr-review-fix-loop"
        case _:
            return None


def _normalized(path: Path) -> Path:
    return Path(os.path.normpath(path.absolute()))


def identify_review_dir(workspace_root: Path, review_dir: Path) -> tuple[str, int, str]:
    """`reviews/<owner>/<repo>/#<N>-<branch>` の (repository, pr, workspace_root からの相対パス)。"""
    root = _normalized(workspace_root)
    reviews = root / "reviews"
    directory = _normalized(review_dir)
    if reviews not in directory.parents:
        raise CaptureError("not_review_dir", "reviews の下のディレクトリではない")
    parts = directory.relative_to(reviews).parts
    if len(parts) == 1 and REVIEW_DIR_NAME.fullmatch(parts[0]):
        raise CaptureError("legacy_layout", "旧配置（reviews/#<N>-*）")
    owner, repo, name = parts if len(parts) == 3 else ("", "", "")
    match = REVIEW_DIR_NAME.fullmatch(name)
    if match is None or not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo):
        raise CaptureError("not_review_dir", "reviews/<owner>/<repo>/#<N>-<branch> の形ではない")
    for current in (reviews, *(reviews.joinpath(*parts[: index + 1]) for index in range(len(parts)))):
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise CaptureError("not_review_dir", "review_dir が無い") from None
        if not stat.S_ISDIR(mode):
            raise CaptureError("not_review_dir", "symlink を含むか、ディレクトリではない")
    return f"{owner}/{repo}", int(match["number"]), directory.relative_to(root).as_posix()


@dataclass(frozen=True)
class _Content:
    data: bytes | None  # max_artifact_bytes を超えたら None（hash と大きさだけを持つ）
    sha256: str
    size: int
    mtime: datetime | None


def read_once(directory: Path, relpath: str, limit: int) -> _Content | None:
    """review_dir の中の通常ファイルを、途中も含めて symlink をたどらずに 1 回だけ読む。取らないものは None。"""
    parts = PurePosixPath(relpath).parts
    opened: list[int] = []
    try:
        try:
            current = os.open(directory, _DIR_FLAGS)
            opened.append(current)
            for part in parts[:-1]:
                current = os.open(part, _DIR_FLAGS, dir_fd=current)
                opened.append(current)
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=current)
        except OSError as exc:
            if exc.errno in _SKIP_ERRNOS:
                return None
            raise
    finally:
        for item in opened:
            os.close(item)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            if size <= limit:
                chunks.append(chunk)
            else:
                chunks.clear()
    data = b"".join(chunks) if size <= limit else None
    return _Content(data, digest.hexdigest(), size, datetime.fromtimestamp(info.st_mtime, UTC))


def sweep_paths(directory: Path) -> list[str]:
    """許可パターンに当たるファイルの相対パス（symlink・`.` 始まりの名前はたどらない）。

    更新の新しい順に並べる。max_sweep_files で打ち切るときは、書き換わったばかりのファイルを先に取る。
    """
    found: list[tuple[int, str]] = []

    def walk(current: Path, prefix: tuple[str, ...]) -> None:
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            relative = (*prefix, entry.name)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if len(relative) <= _SWEEP_DIR_DEPTH and (prefix or entry.name in _SWEEP_ROOTS):
                        walk(Path(entry.path), relative)
                elif entry.is_file(follow_symlinks=False) and classify("/".join(relative)) is not None:
                    found.append((entry.stat(follow_symlinks=False).st_mtime_ns, "/".join(relative)))
            except OSError:
                continue

    walk(directory, ())
    return [path for _, path in sorted(found, key=lambda item: (-item[0], item[1]))]


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and not any(part.startswith(".") for part in path.parts)


def valid_virtual_path(path: str) -> bool:
    """仮想記録のパス: `@` 始まりで、空・`.` 始まりの要素を含まない（例 `@pr-comment/round-3/record.json`）。"""
    parts = path[1:].split("/")
    return path.startswith("@") and all(part and not part.startswith(".") for part in parts)


def in_period(source: Source, moment: datetime) -> bool:
    day = moment.astimezone(UTC).date()
    return (source.since is None or day >= source.since) and (source.until is None or day <= source.until)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _context(context: Mapping[str, object] | None) -> dict[str, JsonValue]:
    if context is None:
        return {}
    try:
        value = json.loads(json.dumps(dict(context), ensure_ascii=False, allow_nan=False))
    except TypeError, ValueError, RecursionError:
        return {"context_error": "not_json"}
    return cast(dict[str, JsonValue], value)


@cache
def _tool_commit() -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and FULL_SHA.fullmatch(commit) else "unknown"


# 1 回の収集


@dataclass(frozen=True)
class _Item:
    path: str
    route: Route
    trigger: Trigger
    content: _Content
    virtual: bool
    swept: bool = False  # sweep で見つけた（呼び出し元が渡したものではない）


@dataclass(frozen=True)
class _Prepared:
    item: _Item
    observation_id: str
    described: Described
    inputs: list[InputRef]


@dataclass
class _Collection:
    config: Config
    store: Store
    source: Source
    repository: str
    pr: int
    directory: Path | None
    rel_dir: str | None
    route: Route
    event: str
    context: dict[str, JsonValue]
    trigger: Trigger
    deadline: float | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    created: list[str] = field(default_factory=list[str])
    duplicates: int = 0
    failures: list[str] = field(default_factory=list[str])
    recorded: set[tuple[object, ...]] | None = None  # 記録済みの failure の key（初めて failure を書くときに読む）
    known: dict[str, str] = field(default_factory=dict[str, str])  # この呼び出しで読んだファイルの sha256

    # failure

    def _recorded_keys(self) -> set[tuple[object, ...]]:
        # ack 済み・解消済みも含める。続いている状態を人が ack した後に、同じ failure を作り直さない
        if self.recorded is None:
            self.recorded = {failure_key(failure) for _, failure, _pending in self.store.failures()}
        return self.recorded

    def fail(
        self,
        kind: FailureKind,
        reason: str,
        *,
        path: str | None = None,
        sha256: str | None = None,
        route: Route | None = None,
        virtual: bool = False,
    ) -> None:
        """failure を記録する。同じ kind・場所・内容の failure（ack 済みを含む）があれば重ねて書かない。"""
        self.failures.append(kind)
        failure = Failure(
            schema="review-eval-failure/v1",
            kind=kind,
            at=_timestamp(datetime.now(UTC)),
            repository=self.repository,
            pr=self.pr,
            review_dir=None if virtual else self.rel_dir,
            path=path,
            sha256=sha256,
            route=route or self.route,
            event=self.event,
            reason=reason,
        )
        key = failure_key(failure)
        recorded = self._recorded_keys()
        if key not in recorded:
            self.store.add_failure(failure)
            recorded.add(key)

    def fail_item(self, kind: FailureKind, reason: str, item: _Item) -> None:
        self.fail(kind, reason, path=item.path, sha256=item.content.sha256, route=item.route, virtual=item.virtual)

    # 対象の列挙

    def _file_item(self, relpath: str, trigger: Trigger, *, swept: bool = False) -> _Item | None:
        route = classify(relpath)
        if route is None or route not in self.source.routes or self.directory is None:
            return None
        try:
            content = read_once(self.directory, relpath, self.config.max_artifact_bytes)
        except OSError as exc:
            name = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
            self.fail("unexpected_error", f"読めない: {name}", path=relpath, route=route)
            return None
        return None if content is None else _Item(relpath, route, trigger, content, virtual=False, swept=swept)

    def _explicit(self, files: Sequence[Path]) -> list[_Item]:
        if self.directory is None:
            return []
        items: list[_Item] = []
        seen: set[str] = set()
        for file in files:
            target = _normalized(file)
            if self.directory not in target.parents:
                continue
            relpath = target.relative_to(self.directory).as_posix()
            if relpath not in seen and (item := self._file_item(relpath, self.trigger)) is not None:
                seen.add(relpath)
                items.append(item)
        return items

    def _virtual(self, records: Sequence[VirtualRecord]) -> list[_Item]:
        items: list[_Item] = []
        limit = self.config.max_artifact_bytes
        for record in records:
            data = record.data
            if not valid_virtual_path(record.path):
                self.fail(
                    "unexpected_error", "invalid virtual path", path=record.path, sha256=hfile(data), virtual=True
                )
                continue
            content = _Content(data if len(data) <= limit else None, hfile(data), len(data), None)
            items.append(_Item(record.path, self.route, self.trigger, content, virtual=True))
        return items

    def _swept(self, exclude: set[str]) -> list[_Item]:
        """sweep で取るファイル。観測済み（今の内容の observation がある）・期間外のものは上限に数えない。

        上限で打ち切っても、取った分は次の実行で観測済みになるので、繰り返せば残りへ進む。
        """
        if self.directory is None:
            return []
        limit = self.config.max_sweep_files
        trigger: Trigger = "sweep" if self.trigger == "save" else "manual"
        items: list[_Item] = []
        for path in sweep_paths(self.directory):
            if path in exclude or classify(path) not in self.source.routes:
                continue
            item = self._file_item(path, trigger, swept=True)
            if item is None or not in_period(self.source, item.content.mtime or self.now):
                continue
            observation_id = observation_identity(self.repository, self.pr, self.rel_dir, path, item.content.sha256)
            if self.store.has_observation(observation_id):
                self.duplicates += 1
                continue
            if len(items) == limit:
                reason = f"未観測の sweep の対象が max_sweep_files（{limit}）を超えたので先頭だけを取る"
                self.fail("sweep_limit", reason)
                break
            items.append(item)
        return items

    # 検査と保存

    def run(self, files: Sequence[Path], records: Sequence[VirtualRecord], sweep: bool) -> None:
        items = self._explicit(files) + self._virtual(records)
        if sweep:
            items += self._swept({item.path for item in items if not item.virtual})
        self.known = {item.path: item.content.sha256 for item in items if not item.virtual}
        to_scan: list[_Prepared] = []
        ready: list[_Prepared] = []
        for item in items:
            prepared = self._prepare(item)
            if prepared is None:
                continue
            if item.content.data is None:
                # observation を作らない。上限を上げた後の収集で、同じ版を保存できる（failure はそのとき解消する）
                self.fail_item("too_large", "max_artifact_bytes を超えるので本文を保存しない", item)
            elif self.store.has_blob(item.content.sha256):
                ready.append(prepared)  # 同じ内容は検査を通って保存済み
            else:
                to_scan.append(prepared)
        ready += self._scan(to_scan)
        self._store(ready)

    def _prepare(self, item: _Item) -> _Prepared | None:
        if not in_period(self.source, item.content.mtime or self.now):
            return None  # 期間外は書かない（reconcile が件数を出す）
        rel_dir = None if item.virtual else self.rel_dir
        observation_id = observation_identity(self.repository, self.pr, rel_dir, item.path, item.content.sha256)
        if self.store.has_observation(observation_id):
            self.duplicates += 1
            return None
        described = describe(item.path, item.content.data)
        if pr_mismatch(described.document, self.repository, self.pr):
            self.fail_item("identity_mismatch", "文書の pr が場所（または呼び出し元の repo・PR）と食い違う", item)
            return None
        return _Prepared(item, observation_id, described, self._inputs(item, described.document))

    def _inputs(self, item: _Item, document: dict[str, object] | None) -> list[InputRef]:
        """`verified/*.json` の入力ファイルの、取得時点の sha256（取れないものは入れない）。"""
        if item.virtual or document is None or not item.path.startswith("verified/") or self.directory is None:
            return []
        entries = document.get("inputs")
        if not isinstance(entries, list):
            return []
        refs: list[InputRef] = []
        seen: set[str] = set()
        for entry in cast(list[object], entries):
            if not isinstance(entry, dict):
                continue
            for key in ("review_file", "findings_file"):
                value = cast(dict[str, object], entry).get(key)
                if not isinstance(value, str) or value in seen or not _safe_relative(value):
                    continue
                seen.add(value)
                sha256 = self.known.get(value)
                if sha256 is None:
                    try:
                        content = read_once(self.directory, value, 0)
                    except OSError:
                        content = None
                    sha256 = None if content is None else content.sha256
                if sha256 is not None:
                    refs.append(InputRef(path=value, sha256=sha256))
        return refs

    def _wait_timeout(self, configured: float) -> float:
        if self.deadline is None:
            return configured
        return min(configured, max(0.0, self.deadline - time.monotonic()))

    def _scan(self, prepared: list[_Prepared]) -> list[_Prepared]:
        if not prepared:
            return []
        workdir = self.store.root / "tmp"
        workdir.mkdir(mode=0o700, exist_ok=True)
        contents = [item.item.content.data or b"" for item in prepared]
        timeout = self._wait_timeout(self.config.scan_timeout_seconds)
        result = (
            scan(contents, workdir=workdir, argv=self.config.secret_scanner, timeout=timeout)
            if timeout > 0
            else ScanResult(frozenset(), "timeout")
        )
        if result.failed is not None:
            # observation を作らない。ファイルは次の sweep・reconcile、仮想記録は同じ bytes を渡す次の収集で取り直す
            for item in prepared:
                self.fail_item("scan_failed", f"秘密検査を完了できない: {result.failed}", item.item)
            return []
        passed: list[_Prepared] = []
        for index, item in enumerate(prepared):
            if index in result.flagged:
                self.fail_item("secret_withheld", "秘密検査が検出したので本文を保存しない", item.item)
                self._put_observation(item, "withheld_secret")
            else:
                passed.append(item)
        return passed

    def _store(self, ready: list[_Prepared]) -> None:
        if not ready:
            return
        stored: list[_Prepared] = []
        try:
            with self.store.locked(self._wait_timeout(self.config.lock_timeout_seconds)):
                total = self.store.blob_bytes_total()
                for item in ready:
                    content = item.item.content
                    if content.data is None:
                        continue
                    if not self.store.has_blob(content.sha256):
                        if total + content.size > self.config.max_store_bytes:
                            self.fail_item("capacity_held", "max_store_bytes を超えるので保存を保留する", item.item)
                            continue
                        try:
                            self.store.put_blob(content.data)
                        except StoreError as exc:
                            self.fail_item("unexpected_error", f"blob を保存できない: {exc}", item.item)
                            continue
                        total += content.size
                    stored.append(item)
        except StoreError as exc:
            if exc.kind != "lock_timeout":
                raise
            for item in ready:
                self.fail_item("lock_timeout", "保存先の lock を取れないので保存を保留する", item.item)
            return
        for item in stored:
            self._put_observation(item, "stored")

    def _put_observation(self, prepared: _Prepared, body: Body) -> None:
        item = prepared.item
        # 本文を保存しないときは、検査に通らなかった本文から取った値（未知の形式名・SHA・入力のパス）を残さない
        described = prepared.described if body == "stored" else withheld(prepared.described)
        # sweep で見つけたファイルは呼び出し元の保存点の成果ではないので、呼び出し元の文脈（run_id など）を写さない
        context: dict[str, JsonValue] = (
            {"via_route": self.route, "via_event": self.event} if item.swept else self.context
        )
        observation = Observation(
            schema="review-eval-observation/v1",
            observation_id=prepared.observation_id,
            repository=self.repository,
            pr=self.pr,
            review_dir=None if item.virtual else self.rel_dir,
            path=item.path,
            sha256=item.content.sha256,
            bytes=item.content.size,
            body=body,
            format=described.format,
            target=described.target,
            observed_at=_timestamp(self.now),
            file_mtime=None if item.content.mtime is None else _timestamp(item.content.mtime),
            trigger=item.trigger,
            route=item.route,
            event=self.event,
            context=context,
            inputs=prepared.inputs if body == "stored" else [],
            collector={"version": COLLECTOR_VERSION, "tool_commit": _tool_commit()},
        )
        if self.store.put_observation(observation):
            self.created.append(prepared.observation_id)
        else:
            self.duplicates += 1

    def report(self) -> CollectionReport:
        status: ReportStatus
        if not self.failures:
            status = "captured"
        elif self.created or self.duplicates:
            status = "partial"
        elif all(kind in _HOLD_KINDS for kind in self.failures):
            status = "held"
        else:
            status = "failed"
        return CollectionReport(status, self.created, self.duplicates, self.failures, None)


def failure_key(failure: Failure) -> tuple[object, ...]:
    repository = None if failure.repository is None else failure.repository.casefold()
    return (failure.kind, repository, failure.pr, failure.review_dir, failure.path, failure.sha256)


# 入口


def _plain(status: ReportStatus, message: str) -> CollectionReport:
    return CollectionReport(status, [], 0, [], message)


def _hold_legacy(config: Config, store: Store, route: Route, event: str, review_dir: Path) -> CollectionReport:
    directory = _normalized(review_dir)
    match = REVIEW_DIR_NAME.fullmatch(directory.name)
    failure = Failure(
        schema="review-eval-failure/v1",
        kind="legacy_layout",
        at=_timestamp(datetime.now(UTC)),
        repository=None,
        pr=None if match is None else int(match["number"]),
        review_dir=directory.relative_to(_normalized(config.workspace_root)).as_posix(),
        path=None,
        sha256=None,
        route=route,
        event=event,
        reason="旧配置（reviews/#<N>-*）は自動で取らない。README の手順で扱う",
    )
    key = failure_key(failure)
    if all(failure_key(existing) != key for _, existing, _pending in store.failures()):
        store.add_failure(failure)
    return CollectionReport("held", [], 0, ["legacy_layout"], "legacy_layout")


def _collect(
    *,
    route: Route,
    event: str,
    review_dir: Path | None,
    files: Sequence[Path],
    records: Sequence[VirtualRecord],
    repository: str | None,
    pr: int | None,
    context: Mapping[str, object] | None,
    trigger: Literal["save", "manual"],
    sweep: bool,
    verify_loop_run: str | None,
    config_path: Path | None,
    environ: Mapping[str, str] | None,
    allow_runner_child: bool,
    deadline: float | None,
) -> CollectionReport:
    env = os.environ if environ is None else environ
    if shim.in_runner_child(env) and not allow_runner_child:
        return _plain("disabled", "runner_child")
    path = shim.config_path(env) if config_path is None else config_path
    if path is None or not path.is_file():
        return _plain("disabled", "not_configured")
    flag = shim.enabled_flag(path)
    if flag is None:
        return _plain("failed", "config: 設定ファイルを TOML として読めない")
    if not flag:
        return _plain("disabled", "not_enabled")
    try:
        config = load_config(path)
        check_registered(config)
    except ConfigError as exc:
        return _plain("failed", f"config: {exc}")
    try:
        store = Store.open(config.store)
    except StoreError:
        return _plain("store_not_initialized", "保存先が初期化されていない（store init が必要）")

    directory: Path | None = None
    rel_dir: str | None = None
    if review_dir is None:
        if repository is None or pr is None:
            return _plain("failed", "review_dir の無い呼び出しには repository と pr が必要")
        location: tuple[str, int] = (repository, pr)
    else:
        directory = _normalized(review_dir)
        if _normalized(config.workspace_root / "reviews") not in directory.parents:
            return _plain("out_of_scope", "outside_reviews")
        if not records and not os.path.lexists(directory):
            return _plain("captured", "review_dir_missing")  # 取るものが無い（失われたものも無い）
        try:
            location_repository, location_pr, rel_dir = identify_review_dir(config.workspace_root, directory)
        except CaptureError as exc:
            if exc.kind == "legacy_layout":
                return _hold_legacy(config, store, route, event, directory)
            return _plain("failed", exc.kind)
        location = (location_repository, location_pr)

    source = config.source_for(location[0])
    if source is None or route not in source.routes:
        return _plain("out_of_scope", "not_in_sources")
    collection = _Collection(
        config=config,
        store=store,
        source=source,
        repository=source.repo,
        pr=location[1],
        directory=directory,
        rel_dir=rel_dir,
        route=route,
        event=event,
        context=_context(context),
        trigger=trigger,
        deadline=deadline,
    )
    try:
        if (repository is not None and repository.casefold() != location[0].casefold()) or (
            pr is not None and pr != location[1]
        ):
            collection.fail("identity_mismatch", "呼び出し元の repo・PR が review_dir の場所と食い違う")
        else:
            collection.run(files, records, sweep)
            if verify_loop_run is not None and directory is not None:
                # inventory は capture を使うので、ここで読む（取得の後に、取れた版と今の内容で確かめる）
                from tools.review_eval import inventory

                lost = inventory.check_loop_run(
                    store, config, directory, collection.repository, location[1], verify_loop_run
                )
                inventory.record_failures(store, lost)
                collection.failures += [failure.kind for failure in lost]
    except Exception as exc:
        try:
            collection.fail("unexpected_error", type(exc).__name__)
        except Exception:
            pass
    return collection.report()


def _emit(report: CollectionReport) -> None:
    if report.status in QUIET_STATUSES:
        return
    reason = ",".join(sorted(set(report.failures))) or report.message or report.status
    try:
        print(f"review-eval: {report.status}: {' '.join(reason.split())}", file=sys.stderr)
    except Exception:
        pass


def collect(
    *,
    route: Route,
    event: str,
    review_dir: Path | None,
    files: Sequence[Path] = (),
    records: Sequence[VirtualRecord] = (),
    repository: str | None = None,
    pr: int | None = None,
    context: Mapping[str, object] | None = None,
    trigger: Literal["save", "manual"] = "save",
    sweep: bool = True,
    verify_loop_run: str | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    allow_runner_child: bool = False,
    deadline: float | None = None,
) -> CollectionReport:
    """review 記録を私有の保存先へ写す。例外を出さず、呼び出し元の終了コード・標準出力を変えない。

    `verify_loop_run`（loop の finish・recover）を渡すと、取得の後に、その run のサイクルの記録が参照する版のうち
    取っておらず今の内容とも違うものを `lost_revision` として記録する（復元できない）。

    agent-runner の子（`AGENT_RUNNER_DEPTH` が空でない）では収集しない。`allow_runner_child` は、runner が
    設定の信頼された argv で起動する after_run（pr-review の attach-runner）だけが渡す。after_run の環境にも
    depth が付くが、モデルが動くプロセスではない。

    `deadline` は任意の絶対 monotonic 時刻。秘密検査と lock の待ちを共通の残時間に抑える。
    通常のローカル I/O は別途かかるため、呼び出し元は終了処理の余白を残して指定する。
    """
    try:
        report = _collect(
            route=route,
            event=event,
            review_dir=review_dir,
            files=files,
            records=records,
            repository=repository,
            pr=pr,
            context=context,
            trigger=trigger,
            sweep=sweep,
            verify_loop_run=verify_loop_run,
            config_path=config_path,
            environ=environ,
            allow_runner_child=allow_runner_child,
            deadline=deadline,
        )
    except Exception as exc:
        report = CollectionReport("failed", [], 0, ["unexpected_error"], type(exc).__name__)
    _emit(report)
    return report
