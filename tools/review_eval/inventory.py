"""収集した記録の対応付け（inventory）と、取りこぼしの確認（reconcile）。

対応は、取った記録の中身（loop のサイクルの記録・pr-review の実行記録）と observation の context からだけ導き、
推測で埋めない。本文を取っていない記録・参照先を取っていない版は、欠損としてそのまま出す。
"""

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from tools.review_eval.capture import (
    REVIEW_DIR_NAME,
    CaptureError,
    classify,
    collect,
    failure_key,
    identify_review_dir,
    in_period,
    read_once,
    sweep_paths,
)
from tools.review_eval.config import Config, Source
from tools.review_eval.models import Failure, Observation, observation_identity
from tools.review_eval.store import Store, StoreError

_LOOP_FILE = re.compile(r"loop/(?P<run_id>[0-9a-f]{32})/(?:run|cycle-(?P<cycle>[1-9][0-9]*))\.json")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# サイクルの記録が成果を参照する (パス, SHA-256) の組（loop.py の ReviewerEntry・ReusedReview・PlanningPart）
_REFERENCE_KEYS = (
    ("review_file", "review_sha256"),
    ("findings_file", "findings_sha256"),
    ("request_file", "request_sha256"),
    ("diagnostic_file", "diagnostic_sha256"),
    ("verified_file", "verified_sha256"),
    ("planning_file", "planning_sha256"),
)
# 同じ秒に取った revision の前後を決める、記録の進み具合（大きいほど後）
_RUN_PROGRESS = {"running": 0, "held": 1, "ended": 2, "abandoned": 2}
_CYCLE_PROGRESS = {"reviewing": 0, "planning": 1, "ended": 2}
_PR_REVIEW_PROGRESS = {"started": 0, "failed": 1, "success": 1}

type Reference = tuple[str, str, str]  # (review_dir からの相対 path, sha256, 出典のキー)


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_mapping(item) for item in cast(list[object], value) if _mapping(item)]


def _document(store: Store, observation: Observation) -> dict[str, Any]:
    """本文を保存した JSON の記録。本文が無い・読めない・JSON のオブジェクトでなければ空。"""
    if observation.body != "stored":
        return {}
    try:
        value: object = json.loads(store.read_blob(observation.sha256))
    except StoreError, ValueError, RecursionError:
        return {}
    return _mapping(value)


def _latest(
    store: Store, observations: Sequence[Observation], progress: Mapping[str, int], key: str
) -> dict[str, Any]:
    """最も新しい revision の記録。observed_at は秒の単位なので、同じ秒の revision は記録の進み具合で決める。"""
    documents = [(o, document) for o in observations if (document := _document(store, o))]
    if not documents:
        return {}
    return max(documents, key=lambda pair: (pair[0].observed_at, progress.get(str(pair[1].get(key)), 0)))[1]


def _relative(value: object, rel_dir: str | None) -> str | None:
    """loop の記録の絶対パスを review_dir からの相対パスにする。review_dir の中を指さなければ None。"""
    if not isinstance(value, str) or rel_dir is None or not PurePosixPath(value).is_absolute():
        return None
    _, separator, rest = value.partition(f"/{rel_dir}/")
    parts = PurePosixPath(rest).parts
    if not separator or not parts or any(part == ".." or part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def _cycle_references(document: Mapping[str, Any], rel_dir: str | None) -> Iterator[Reference]:
    review = _mapping(document.get("review"))
    replaced = _items(_mapping(review.get("rerun")).get("replaced"))
    groups: list[tuple[str, dict[str, Any]]] = [
        *((f"review.reviewers[{i}]", entry) for i, entry in enumerate(_items(review.get("reviewers")))),
        *((f"review.rerun.replaced[{i}]", entry) for i, entry in enumerate(replaced)),
        *((f"review.reused[{i}]", entry) for i, entry in enumerate(_items(review.get("reused")))),
        ("planning", _mapping(document.get("planning"))),
    ]
    for label, entry in groups:
        for file_key, sha_key in _REFERENCE_KEYS:
            path = _relative(entry.get(file_key), rel_dir)
            sha256 = entry.get(sha_key)
            if path is not None and isinstance(sha256, str) and _SHA256.fullmatch(sha256):
                yield path, sha256, f"cycle-{document.get('cycle')}:{label}.{sha_key}"


def _by_cycle(
    observations: Sequence[Observation], run_id: str, review_dir: str | None = None
) -> dict[int | None, list[Observation]]:
    """run の記録の observation を、サイクルの番号ごと（run.json は None）に分ける。"""
    found: dict[int | None, list[Observation]] = {}
    for o in observations:
        match = _LOOP_FILE.fullmatch(o.path)
        if match is None or match["run_id"] != run_id or (review_dir is not None and o.review_dir != review_dir):
            continue
        found.setdefault(None if match["cycle"] is None else int(match["cycle"]), []).append(o)
    return found


def _references(
    store: Store, observations: Sequence[Observation], run_id: str, review_dir: str | None
) -> list[Reference]:
    found: dict[tuple[str, str], str] = {}
    for number, records in _by_cycle(observations, run_id, review_dir).items():
        if number is None:
            continue
        for o in records:
            for path, sha256, key in _cycle_references(_document(store, o), o.review_dir):
                found.setdefault((path, sha256), key)
    return [(path, sha256, key) for (path, sha256), key in found.items()]


def loop_references(
    store: Store, repository: str, pr: int, run_id: str, *, review_dir: str | None = None
) -> list[Reference]:
    """観測済みのサイクルの記録（`loop/<run_id>/cycle-<n>.json`）が参照する (相対 path, sha256, 出典のキー)。

    同じ秒に取った revision の前後は observed_at では決まらないので、取ったすべての revision の参照を含める
    （最新の revision の参照はその一部）。同じ (path, sha256) は最初に見つけた出典だけを残す。
    """
    return _references(store, store.observations(repository, pr), run_id, review_dir)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lost(
    store: Store,
    config: Config,
    directory: Path,
    rel_dir: str,
    repository: str,
    pr: int,
    run_id: str,
    observations: Sequence[Observation],
) -> list[Failure]:
    source = config.source_for(repository)
    routes: set[str] = set(source.routes) if source is not None else set()
    lost: list[Failure] = []
    for path, sha256, key in _references(store, observations, run_id, rel_dir):
        route = classify(path)
        if route is None or route not in routes:
            continue  # 収集の経路に入っていない成果は数えない
        if store.has_observation(observation_identity(repository, pr, rel_dir, path, sha256)):
            continue
        try:
            current = read_once(directory, path, 0)
        except OSError:
            current = None
        if current is not None and current.sha256 == sha256:
            continue  # 失われていない（呼び出し元の取得・sweep が取る）
        lost.append(
            Failure(
                schema="review-eval-failure/v1",
                kind="lost_revision",
                at=_now(),
                repository=source.repo if source is not None else repository,
                pr=pr,
                review_dir=rel_dir,
                path=path,
                sha256=sha256,
                route=route,
                event=None,
                reason=f"loop の記録（{key}）が参照する版を取っておらず、今のファイルの内容とも違う。復元できない",
            )
        )
    return lost


def check_loop_run(
    store: Store, config: Config, review_dir: Path, repository: str, pr: int, run_id: str
) -> list[Failure]:
    """サイクルの記録が参照する版のうち、観測が無く、今のファイルの内容とも違うもの（復元できない）を返す。"""
    _, _, rel_dir = identify_review_dir(config.workspace_root, review_dir)
    directory = Path(os.path.normpath(review_dir.absolute()))
    return _lost(store, config, directory, rel_dir, repository, pr, run_id, store.observations(repository, pr))


def record_failures(store: Store, failures: Sequence[Failure]) -> None:
    """同じ kind・場所・内容の failure（ack 済み・解消済みを含む）が無いものだけを書く。"""
    if not failures:
        return
    recorded = {failure_key(existing) for _, existing, _pending in store.failures()}
    for failure in failures:
        key = failure_key(failure)
        if key not in recorded:
            store.add_failure(failure)
            recorded.add(key)


# inventory


def _revision(o: Observation) -> dict[str, Any]:
    return {
        "observation_id": o.observation_id,
        "sha256": o.sha256,
        "bytes": o.bytes,
        "body": o.body,
        "observed_at": o.observed_at,
        "file_mtime": o.file_mtime,
        "trigger": o.trigger,
        "event": o.event,
        "format": o.format.model_dump(by_alias=True),
        "reviewed_head": o.target.reviewed_head,
    }


def _cycle(store: Store, number: int, records: Sequence[Observation], observed: Mapping[Any, str]) -> dict[str, Any]:
    latest = _latest(store, records, _CYCLE_PROGRESS, "state")
    review = _mapping(latest.get("review"))
    planning = _mapping(latest.get("planning"))
    references: dict[tuple[str, str], dict[str, Any]] = {}
    for o in records:
        for path, sha256, key in _cycle_references(_document(store, o), o.review_dir):
            body = observed.get((o.review_dir, path, sha256))
            references.setdefault((path, sha256), {"path": path, "sha256": sha256, "key": key, "body": body})
    head = latest.get("head")
    round_ = planning.get("round")
    finalize = None
    if planning.get("finalize") and isinstance(head, str):
        finalize = observed.get((None, f"@finalize/{head[:12]}/result.json"))
    return {
        "cycle": number,
        "record_missing": not latest,  # 本文を取った revision が無い
        "head": head,
        "mode": review.get("mode"),
        "state": latest.get("state"),
        "batch_run_ids": [
            _mapping(part.get("batch")).get("run_id")
            for part in (review, _mapping(review.get("rerun")))
            if part.get("batch")
        ],
        "planning": None
        if not planning
        else {
            "round": round_,
            "outcome": planning.get("outcome"),
            "comment_id": planning.get("comment_id"),
            "record": observed.get((None, f"@pr-comment/round-{round_}/record.json")),
            "finalize": finalize,
        },
        "references": list(references.values()),
    }


def _loop_runs(store: Store, observations: Sequence[Observation]) -> list[dict[str, Any]]:
    """loop の run とサイクル。参照ごとの `body` は、その版の observation の body（取っていなければ null）。"""
    run_ids = sorted({match["run_id"] for o in observations if (match := _LOOP_FILE.fullmatch(o.path))})
    observed: dict[Any, str] = {(o.review_dir, o.path, o.sha256): o.body for o in observations}
    observed.update({(None, o.path): o.body for o in observations if o.review_dir is None})
    runs: list[dict[str, Any]] = []
    for run_id in run_ids:
        by_cycle = _by_cycle(observations, run_id)
        run = _latest(store, by_cycle.get(None, []), _RUN_PROGRESS, "state")
        runs.append(
            {
                "run_id": run_id,
                "review_dir": next(o.review_dir for records in by_cycle.values() for o in records),
                "state": run.get("state"),
                "result": run.get("result"),
                "cycles": [
                    _cycle(store, number, by_cycle[number], observed)
                    for number in sorted(n for n in by_cycle if n is not None)
                ],
            }
        )
    return runs


def _pr_review_runs(
    store: Store, observations: Sequence[Observation], loop_runs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """`runs/*.json`（pr-review の実行記録）の状態と起動の種類。loop 経由かは runner の run_id の一致で決める。"""
    batches = {
        run_id: {"run_id": run["run_id"], "cycle": cycle["cycle"]}
        for run in loop_runs
        for cycle in cast(list[dict[str, Any]], run["cycles"])
        for run_id in cast(list[object], cycle["batch_run_ids"])
        if isinstance(run_id, str)
    }
    groups: dict[tuple[str | None, str], list[Observation]] = {}
    for o in observations:
        if o.path.startswith("runs/") and classify(o.path) == "pr-review":
            groups.setdefault((o.review_dir, o.path), []).append(o)
    found: list[dict[str, Any]] = []
    for (review_dir, path), records in sorted(groups.items(), key=lambda item: (item[0][0] or "", item[0][1])):
        latest = _latest(store, records, _PR_REVIEW_PROGRESS, "status")
        provenance = _mapping(_mapping(latest.get("reviewer")).get("provenance"))
        runner_run_id = provenance.get("run_id")
        found.append(
            {
                "review_dir": review_dir,
                "path": path,
                "status": latest.get("status"),
                "review_kind": latest.get("review_kind"),
                "reviewed_head": latest.get("reviewed_head"),
                "runner_run_id": runner_run_id,
                "request_id": provenance.get("request_id"),
                # pr-review の保存点が渡した起動の種類（runner の子は収集しないので、loop の sweep で取った版には無い）
                "invocation": sorted({kind for o in records if isinstance(kind := o.context.get("invocation"), str)}),
                "loop": batches.get(runner_run_id) if isinstance(runner_run_id, str) else None,
                "outputs": latest.get("outputs"),
            }
        )
    return found


def _pending(store: Store, repository: str | None, pr: int | None = None) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for failure_id, failure, pending in store.failures():
        if not pending or (pr is not None and failure.pr != pr):
            continue
        if repository is not None and (failure.repository or "").casefold() != repository.casefold():
            continue
        selected.append({"id": failure_id, **failure.model_dump(exclude={"schema_", "at", "route", "event"})})
    return selected


def inventory(store: Store, repository: str, pr: int) -> dict[str, Any]:
    """repo・PR の収集の一覧。

    path ごとの revision 列、loop の run とサイクル、pr-review の実行、仮想記録、未処理の failure、
    本文を取っていない件数。
    """
    observations = store.observations(repository, pr)
    paths: dict[tuple[str | None, str], list[Observation]] = {}
    for o in observations:
        paths.setdefault((o.review_dir, o.path), []).append(o)
    loop_runs = _loop_runs(store, observations)
    return {
        "repository": repository,
        "pr": pr,
        "paths": [
            {
                "review_dir": directory,
                "path": path,
                "route": records[0].route,
                "revisions": list(map(_revision, records)),
            }
            for (directory, path), records in sorted(paths.items(), key=lambda item: (item[0][0] or "", item[0][1]))
        ],
        "loop_runs": loop_runs,
        "pr_review_runs": _pr_review_runs(store, observations, loop_runs),
        "virtual_records": [
            {"path": o.path, "event": o.event, "context": o.context, **_revision(o)}
            for o in observations
            if o.review_dir is None
        ],
        "pending_failures": _pending(store, repository, pr),
        "withheld": sum(1 for o in observations if o.body != "stored"),
    }


# reconcile


def review_dirs(config: Config, repository: str, pr: int | None = None) -> list[Path] | None:
    """`reviews/<owner>/<repo>/` の下の `#<N>-*`（symlink を除く）。repo の形が誤りなら None。"""
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name or owner.startswith(".") or name.startswith("."):
        return None
    base = config.workspace_root / "reviews" / owner / name
    try:
        entries = sorted(base.iterdir()) if base.is_dir() and not base.is_symlink() else []
    except OSError:
        entries = []
    selected: list[Path] = []
    for entry in entries:
        match = REVIEW_DIR_NAME.fullmatch(entry.name)
        if match and (pr is None or int(match["number"]) == pr) and entry.is_dir() and not entry.is_symlink():
            selected.append(entry)
    return selected


@dataclass
class _Scan:
    unobserved: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    unobserved_dirs: list[tuple[Path, Source]] = field(default_factory=list[tuple[Path, Source]])
    lost: list[Failure] = field(default_factory=list[Failure])
    out_of_period: int = 0
    unclassified: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    # ack 済み（または解消済み）の failure の (repository, pr, review_dir, path, sha256)。
    # 人が確認した保留は (a) に数えない
    settled: set[tuple[object, ...]] = field(default_factory=set[tuple[object, ...]])


def _unclassified(directory: Path) -> int:
    """許可パターンにも除外（`.` 始まり・`*.log`・`loop/lock.json`）にも当たらないファイルの件数（symlink を含む）。"""
    count = 0
    for current, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        prefix = Path(current).relative_to(directory)
        for name in files:
            relpath = (prefix / name).as_posix()
            if name.startswith(".") or name.endswith(".log") or relpath == "loop/lock.json":
                continue
            if classify(relpath) is None or os.path.islink(os.path.join(current, name)):
                count += 1
    return count


def _scan_dir(
    store: Store, config: Config, source: Source, directory: Path, observations: Sequence[Observation], scan: _Scan
) -> None:
    try:
        _, pr, rel_dir = identify_review_dir(config.workspace_root, directory)
    except CaptureError as exc:
        scan.skipped.append({"review_dir": str(directory), "reason": exc.kind})
        return
    now = datetime.now(UTC)
    found = len(scan.unobserved)
    for relpath in sweep_paths(directory):
        if classify(relpath) not in source.routes:
            continue
        try:
            content = read_once(directory, relpath, 0)
        except OSError:
            content = None
        if content is None:
            continue
        if not in_period(source, content.mtime or now):
            scan.out_of_period += 1
            continue
        settled = (source.repo.casefold(), pr, rel_dir, relpath, content.sha256) in scan.settled
        if not settled and not store.has_observation(
            observation_identity(source.repo, pr, rel_dir, relpath, content.sha256)
        ):
            scan.unobserved.append({"review_dir": rel_dir, "path": relpath, "sha256": content.sha256})
    if len(scan.unobserved) > found:
        scan.unobserved_dirs.append((directory, source))
    scan.unclassified += _unclassified(directory)
    mine = [o for o in observations if o.pr == pr and o.repository.casefold() == source.repo.casefold()]
    run_ids = {match["run_id"] for o in mine if o.review_dir == rel_dir and (match := _LOOP_FILE.fullmatch(o.path))}
    for run_id in sorted(run_ids):
        scan.lost += _lost(store, config, directory, rel_dir, source.repo, pr, run_id, mine)


def _scan(store: Store, config: Config, sources: Sequence[Source]) -> _Scan:
    scan = _Scan()
    scan.settled = {
        ((f.repository or "").casefold(), f.pr, f.review_dir, f.path, f.sha256)
        for _, f, pending in store.failures()
        if not pending
    }
    observations = store.observations()
    for source in sources:
        for directory in review_dirs(config, source.repo) or []:
            _scan_dir(store, config, source, directory, observations, scan)
    return scan


def _legacy_dirs(config: Config) -> int:
    reviews = config.workspace_root / "reviews"
    try:
        entries = list(reviews.iterdir()) if reviews.is_dir() and not reviews.is_symlink() else []
    except OSError:
        return 0
    return sum(1 for entry in entries if REVIEW_DIR_NAME.fullmatch(entry.name) and not entry.is_symlink())


def reconcile(
    store: Store, config: Config, *, repository: str | None, apply: bool, config_path: Path | None = None
) -> dict[str, Any]:
    """Spec 4.5 の (a) 未観測のファイル、(b) 未処理の failure、(c) 失われた loop の版、(d) 期間外・未分類・旧配置。

    `apply` は (a) を自動取得と同じ `collect`（`trigger: manual`、設定は `config_path`。apply では必須）で取り込み、
    (c) を failure として記録する（人が確認して ack できるようにする。復元はできない）。同じ版の failure が ack 済みの
    ファイルは (a) に、ack 済みの (c) は (c) に出さない（人が確認した保留を未処理として数え続けない）。
    """
    if apply and config_path is None:
        # collect は設定をパスから読み直す。無いと環境変数の設定（渡した store・config と別の保存先）を読みうる
        raise ValueError("reconcile の apply には、渡した config の設定ファイルのパス（config_path）が要る")
    sources = [s for s in config.sources if repository is None or s.repo.casefold() == repository.casefold()]
    scan = _scan(store, config, sources)
    applied: list[dict[str, Any]] = []
    if apply:
        for directory, source in scan.unobserved_dirs:
            report = collect(
                route=source.routes[0],
                event="reconcile",
                review_dir=directory,
                trigger="manual",
                config_path=config_path,
            )
            applied.append({"review_dir": str(directory), "status": report.status, "failures": report.failures})
        record_failures(store, scan.lost)
        scan = _scan(store, config, sources)
    acked = {failure_key(failure) for _, failure, pending in store.failures() if not pending}
    return {
        "unobserved": scan.unobserved,
        "pending_failures": _pending(store, repository),
        "lost_revisions": [
            failure.model_dump(include={"repository", "pr", "review_dir", "path", "sha256", "reason"})
            for failure in scan.lost
            if failure_key(failure) not in acked
        ],
        "counts": {
            "out_of_period": scan.out_of_period,
            "unclassified": scan.unclassified,
            "legacy_layout": _legacy_dirs(config),
        },
        "applied": applied,
        "skipped": scan.skipped,
    }
