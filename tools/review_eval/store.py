"""内容アドレスの私有保存先（blob・observation・failure）。"""

import fcntl
import json
import os
import re
import time
from collections.abc import Generator, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import ValidationError

from tools.review_eval.canonical import hfile
from tools.review_eval.models import Failure, Observation, observation_identity

STORE_SCHEMA = "review-eval-store/v1"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_FAILURE_ID = re.compile(r"[0-9a-f]{32}")
_OBSERVATION_ID = re.compile(r"[0-9a-f]{64}")


class StoreError(Exception):
    def __init__(self, message: str, kind: str = "store_error") -> None:
        super().__init__(message)
        self.kind = kind


def _mkdir(path: Path) -> None:
    path.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    path.chmod(_DIR_MODE)


def _mkdir_one(path: Path) -> None:
    path.mkdir(mode=_DIR_MODE, exist_ok=True)
    path.chmod(_DIR_MODE)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def init(cls, root: Path) -> Self:
        if root.exists():
            if not root.is_dir():
                raise StoreError("保存先がディレクトリではない")
            if (root / "STORE.json").is_file():
                return cls.open(root)
            if any(root.iterdir()):
                raise StoreError("空でなく STORE.json も無いディレクトリは保存先にできない")
        _mkdir(root)
        store = cls(root)
        store._create(root / "STORE.json", json.dumps({"schema": STORE_SCHEMA, "created_at": _now()}).encode("utf-8"))
        for name in ("blobs", "observations", "failures", "acks", "tmp"):
            _mkdir(root / name)
        return store

    @classmethod
    def open(cls, root: Path) -> Self:
        try:
            data = json.loads((root / "STORE.json").read_bytes())
        except OSError, ValueError:
            raise StoreError("保存先が初期化されていない", kind="store_not_initialized") from None
        if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:  # pyright: ignore[reportUnknownMemberType]
            raise StoreError("保存先の版が違う", kind="store_not_initialized")
        return cls(root)

    def _create(self, final: Path, data: bytes) -> bool:
        """一時ファイル + os.link で作る。既にあれば上書きせず False。"""
        tmp_dir = self.root / "tmp"
        _mkdir(tmp_dir)
        tmp = tmp_dir / f"{os.getpid()}-{time.monotonic_ns()}-{os.urandom(4).hex()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _mkdir(final.parent)
            try:
                os.link(tmp, final)
            except FileExistsError:
                return False
            return True
        finally:
            tmp.unlink(missing_ok=True)

    # blob
    def _blob_path(self, sha256: str) -> Path:
        return self.root / "blobs" / sha256[:2] / sha256

    def has_blob(self, sha256: str) -> bool:
        return self._blob_path(sha256).is_file()

    def put_blob(self, data: bytes) -> str:
        sha = hfile(data)
        if not self._create(self._blob_path(sha), data):
            self.read_blob(sha)
        return sha

    def read_blob(self, sha256: str) -> bytes:
        try:
            data = self._blob_path(sha256).read_bytes()
        except OSError:
            raise StoreError(f"blob を読めない: {sha256}") from None
        if hfile(data) != sha256:
            raise StoreError(f"blob の内容が hash と一致しない: {sha256}")
        return data

    def _blob_files(self) -> Iterator[Path]:
        yield from (path for path in sorted((self.root / "blobs").rglob("*")) if path.is_file())

    def blob_bytes_total(self) -> int:
        return sum(path.stat().st_size for path in self._blob_files())

    # observation
    def _observation_path(self, observation_id: str) -> Path:
        return self.root / "observations" / observation_id[:2] / f"{observation_id}.json"

    def has_observation(self, observation_id: str) -> bool:
        return self._observation_path(observation_id).is_file()

    def observation(self, observation_id: str) -> Observation | None:
        """id の observation。無い・読めない・id の形でなければ None。"""
        if not _OBSERVATION_ID.fullmatch(observation_id):
            return None
        try:
            return Observation.model_validate_json(self._observation_path(observation_id).read_bytes())
        except OSError, ValidationError:
            return None

    def put_observation(self, observation: Observation) -> bool:
        data = observation.model_dump_json(by_alias=True).encode("utf-8")
        return self._create(self._observation_path(observation.observation_id), data)

    def _load_observations(self) -> tuple[list[Observation], list[str]]:
        found: list[Observation] = []
        problems: list[str] = []
        for path in sorted((self.root / "observations").rglob("*.json")):
            try:
                found.append(Observation.model_validate_json(path.read_bytes()))
            except OSError, ValidationError:
                problems.append(f"observation を読めない: {path.name}")
        return found, problems

    def observations(self, repository: str | None = None, pr: int | None = None) -> list[Observation]:
        found, _ = self._load_observations()
        selected = [
            o
            for o in found
            if (repository is None or o.repository.casefold() == repository.casefold()) and (pr is None or o.pr == pr)
        ]
        return sorted(selected, key=lambda o: (o.observed_at, o.observation_id))

    # failure
    def add_failure(self, failure: Failure) -> str:
        data = failure.model_dump_json(by_alias=True).encode("utf-8")
        failure_id = hfile(data)[:32]
        self._create(self.root / "failures" / f"{failure_id}.json", data)
        return failure_id

    def failures(self) -> list[tuple[str, Failure, bool]]:
        found, _ = self._load_observations()
        stored = {(o.repository.casefold(), o.pr, o.review_dir, o.path, o.sha256) for o in found if o.body == "stored"}
        acked = {path.stem for path in (self.root / "acks").glob("*.json")}
        result: list[tuple[str, Failure, bool]] = []
        for path in sorted((self.root / "failures").glob("*.json")):
            try:
                failure = Failure.model_validate_json(path.read_bytes())
            except OSError, ValidationError:
                continue
            pending = path.stem not in acked
            if pending and failure.repository and failure.pr is not None and failure.path and failure.sha256:
                key = (failure.repository.casefold(), failure.pr, failure.review_dir, failure.path, failure.sha256)
                if key in stored:
                    pending = False
            result.append((path.stem, failure, pending))
        return sorted(result, key=lambda item: (item[1].at, item[0]))

    def ack_failure(self, failure_id: str, *, by: str, reason: str) -> None:
        if not _FAILURE_ID.fullmatch(failure_id) or not (self.root / "failures" / f"{failure_id}.json").is_file():
            raise StoreError("その failure は無い")
        data = json.dumps({"by": by, "reason": reason, "at": _now()}, ensure_ascii=False).encode("utf-8")
        self._create(self.root / "acks" / f"{failure_id}.json", data)

    # lock
    def locked(self, timeout: float) -> AbstractContextManager[None]:
        return self._locked(timeout)

    @contextmanager
    def _locked(self, timeout: float) -> Generator[None]:
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, _FILE_MODE)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StoreError("保存先の lock を取れない", kind="lock_timeout") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)

    # verify / restore
    def verify(self) -> list[str]:
        problems: list[str] = []
        try:
            Store.open(self.root)
        except StoreError as exc:
            return [str(exc)]
        for path in self._blob_files():
            if hfile(path.read_bytes()) != path.name:
                problems.append(f"blob の内容が hash と一致しない: {path.name}")
        found, unreadable = self._load_observations()
        problems.extend(unreadable)
        for o in found:
            expected = observation_identity(o.repository, o.pr, o.review_dir, o.path, o.sha256)
            if expected != o.observation_id:
                problems.append(f"observation の id が内容と一致しない: {o.observation_id}")
            if o.body == "stored":
                if not self.has_blob(o.sha256):
                    problems.append(f"blob が無い: {o.sha256} (observation {o.observation_id})")
                elif self._blob_path(o.sha256).stat().st_size != o.bytes:
                    problems.append(f"blob の大きさが記録と違う: {o.sha256}")
        for path in sorted((self.root / "failures").glob("*.json")):
            try:
                Failure.model_validate_json(path.read_bytes())
            except OSError, ValidationError:
                problems.append(f"failure を読めない: {path.name}")
        for path in sorted((self.root / "acks").glob("*.json")):
            try:
                ack = json.loads(path.read_bytes())
            except OSError, ValueError:
                ack = None
            if not isinstance(ack, dict):
                problems.append(f"ack を読めない: {path.name}")
        return problems

    def restore(self, observations: Sequence[Observation], dest: Path) -> list[dict[str, str]]:
        if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
            raise StoreError("復元先は存在しないか空のディレクトリにする")
        dest_root = Path(os.path.normpath(dest.absolute()))
        results: list[dict[str, str]] = []
        for o in observations:
            if o.body != "stored":
                results.append({"path": o.path, "status": "withheld"})
                continue
            target = self._restore_target(dest_root, o)
            data = self.read_blob(o.sha256)
            if target.exists() and hfile(target.read_bytes()) != o.sha256:
                target = target.with_name(f"{target.name}.{o.sha256[:12]}")
            self._private_dirs(dest_root, target.parent)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            target.chmod(_FILE_MODE)
            status = "restored" if hfile(target.read_bytes()) == o.sha256 else "hash_mismatch"
            results.append({"path": str(target.relative_to(dest_root)), "status": status})
        return results

    @staticmethod
    def _private_dirs(dest_root: Path, directory: Path) -> None:
        current = dest_root
        _mkdir(current)
        for part in directory.relative_to(dest_root).parts:
            current = current / part
            _mkdir_one(current)

    @staticmethod
    def _restore_target(dest_root: Path, o: Observation) -> Path:
        if o.path.startswith("@"):
            relative = PurePosixPath("@virtual", o.repository, str(o.pr), o.path.replace("@", ""))
        else:
            relative = PurePosixPath(o.review_dir or "", o.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise StoreError("復元先の外へ出るパス")
        target = Path(os.path.normpath(dest_root / relative.as_posix()))
        if dest_root not in target.parents:
            raise StoreError("復元先の外へ出るパス")
        return target
