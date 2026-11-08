"""review-eval の CLI。終了コード: 0 成功、1 処理としての失敗、2 入力・設定の誤り。"""

import argparse
import dataclasses
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from tools.review_eval import shim
from tools.review_eval.capture import (
    QUIET_STATUSES,
    CaptureError,
    VirtualRecord,
    collect,
    identify_review_dir,
    valid_virtual_path,
)
from tools.review_eval.config import ROUTES, Config, ConfigError, Route, check_registered, load_config
from tools.review_eval.inventory import inventory, reconcile, review_dirs
from tools.review_eval.ledger import Ledger, LedgerError, new_id
from tools.review_eval.ledger_ops import (
    CANDIDATE_STATUSES,
    EXPOSURE_KINDS,
    RELATIONS,
    ROLES,
    SCOPES,
    SPLITS,
    STAGES,
)
from tools.review_eval.models import Observation
from tools.review_eval.packets import export_packet, import_answers
from tools.review_eval.store import Store, StoreError


def _config_check(args: argparse.Namespace) -> int:
    path: Path | None = Path(args.config).expanduser() if args.config else shim.config_path()
    report: dict[str, object] = {"config_path": None if path is None else str(path), "enabled": False, "sources": []}
    if path is not None and path.is_file():
        try:
            config = load_config(path)
            if config.enabled:
                check_registered(config)
        except ConfigError as exc:
            print(f"review-eval: config: {exc}", file=sys.stderr)
            return 2
        report["enabled"] = config.enabled
        report["sources"] = [source.model_dump(mode="json") for source in config.sources]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _load_store(args: argparse.Namespace) -> tuple[Config, Store] | int:
    path = Path(args.config).expanduser() if args.config else shim.config_path()
    if path is None or not path.is_file():
        print("review-eval: config: 設定ファイルが無い", file=sys.stderr)
        return 2
    try:
        config = load_config(path)
        return config, Store.open(config.store)
    except (ConfigError, StoreError) as exc:
        print(f"review-eval: {exc}", file=sys.stderr)
        return 2


def _store_init(args: argparse.Namespace) -> int:
    try:
        store = Store.init(Path(args.store).expanduser())
    except StoreError as exc:
        print(f"review-eval: store: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"store": str(store.root)}, ensure_ascii=False))
    return 0


def _status(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else shim.config_path()
    if path is None or not path.is_file():
        print(json.dumps({"enabled": False}))
        return 0
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    config, store = loaded
    pending = [
        {"id": failure_id, "kind": failure.kind, "at": failure.at, "reason": failure.reason}
        for failure_id, failure, is_pending in store.failures()
        if is_pending
    ]
    report = {
        "enabled": config.enabled,
        "store": str(store.root),
        "observations": len(store.observations()),
        "blob_bytes": store.blob_bytes_total(),
        "pending_failures": pending,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if pending else 0


def _verify(args: argparse.Namespace) -> int:
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    problems = loaded[1].verify()
    print(json.dumps({"problems": problems}, ensure_ascii=False, indent=2))
    return 1 if problems else 0


def _restore(args: argparse.Namespace) -> int:
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    store = loaded[1]
    selected: list[Observation]
    if args.observation:
        selected = [o for o in store.observations() if o.observation_id == args.observation]
    else:
        selected = store.observations(args.repo, args.pr)
    if not selected:
        print("review-eval: restore: 対象の observation が無い", file=sys.stderr)
        return 2
    try:
        results = store.restore(selected, Path(args.dest).expanduser())
    except StoreError as exc:
        print(f"review-eval: restore: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(r["status"] != "restored" for r in results) else 0


def _failures_ack(args: argparse.Namespace) -> int:
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    try:
        loaded[1].ack_failure(args.id, by=args.by, reason=args.reason)
    except StoreError as exc:
        print(f"review-eval: failures: {exc}", file=sys.stderr)
        return 2
    return 0


def _hook(args: argparse.Namespace) -> int:
    """保存点からの収集。`disabled`・`out_of_scope`・`captured` は 0、それ以外は 1。"""
    if (args.repo is None) != (args.pr is None):
        print("review-eval: hook: --repo と --pr は一緒に指定する", file=sys.stderr)
        return 2
    context: dict[str, object] | None = None
    if args.context_json is not None:
        try:
            value: object = json.loads(args.context_json)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            print("review-eval: hook: --context-json は JSON のオブジェクトにする", file=sys.stderr)
            return 2
        context = cast(dict[str, object], value)
    records: list[VirtualRecord] = []
    for argument in args.record:
        vpath, separator, file = str(argument).partition("=")
        if not separator or not valid_virtual_path(vpath) or not file:
            print(
                "review-eval: hook: --record は @VPATH=FILE の形にする（VPATH は空・. 始まりの要素を含まない）",
                file=sys.stderr,
            )
            return 2
        try:
            records.append(VirtualRecord(vpath, Path(file).expanduser().read_bytes()))
        except OSError as exc:
            print(f"review-eval: failed: --record のファイルを読めない（{type(exc).__name__}）", file=sys.stderr)
            return 1
    report = collect(
        route=args.route,
        event=args.event,
        review_dir=None if args.review_dir is None else Path(args.review_dir).expanduser(),
        files=[Path(file).expanduser() for file in args.file],
        records=records,
        repository=args.repo,
        pr=args.pr,
        context=context,
        trigger="save",
    )
    return 0 if report.status in QUIET_STATUSES else 1


def _ingest_route(config: Config, directory: Path) -> Route:
    """経路の範囲の確認に使う経路（取ったファイルの経路はパスの規則で決まる）。"""
    try:
        repository, _, _ = identify_review_dir(config.workspace_root, directory)
    except CaptureError:
        return ROUTES[0]
    source = config.source_for(repository)
    return ROUTES[0] if source is None else source.routes[0]


def _ingest(args: argparse.Namespace) -> int:
    """手動の取り込み（trigger: manual）。結果を JSON で出し、`captured` 以外を含めば 1。"""
    path = Path(args.config).expanduser() if args.config else shim.config_path()
    if path is None or not path.is_file():
        print("review-eval: config: 設定ファイルが無い", file=sys.stderr)
        return 2
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"review-eval: config: {exc}", file=sys.stderr)
        return 2
    if args.review_dir is not None:
        if args.pr is not None:
            print("review-eval: ingest: --pr は --repo と一緒に指定する", file=sys.stderr)
            return 2
        directories: list[Path] | None = [Path(args.review_dir).expanduser()]
    else:
        directories = review_dirs(config, args.repo, args.pr)
    if directories is None:
        print("review-eval: ingest: --repo は <owner>/<repo> の形にする", file=sys.stderr)
        return 2
    results: list[dict[str, object]] = []
    for directory in directories:
        report = collect(
            route=_ingest_route(config, directory),
            event="ingest",
            review_dir=directory,
            trigger="manual",
            config_path=path,
        )
        results.append({"review_dir": str(directory), **dataclasses.asdict(report)})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result["status"] in QUIET_STATUSES for result in results) else 1


def _inventory(args: argparse.Namespace) -> int:
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    print(json.dumps(inventory(loaded[1], args.repo, args.pr), ensure_ascii=False, indent=2))
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    """未観測・未処理・失われた版のどれかが（`--apply` の後も）残れば 1。"""
    path = Path(args.config).expanduser() if args.config else shim.config_path()
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    config, store = loaded
    report = reconcile(store, config, repository=args.repo, apply=args.apply, config_path=path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["unobserved"] or report["pending_failures"] or report["lost_revisions"] else 0


def _ledger(args: argparse.Namespace) -> int:
    """台帳の操作。拒否（LedgerError）は終了コード 2 で、理由を標準エラーに出す。"""
    loaded = _load_store(args)
    if isinstance(loaded, int):
        return loaded
    config, store = loaded
    try:
        result = args.action(Ledger(store, lock_timeout=config.lock_timeout_seconds), args)
    except LedgerError as exc:
        print(f"review-eval: ledger: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _read_json(path: str) -> object:
    try:
        return json.loads(Path(path).expanduser().read_bytes())
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{path} を JSON として読めない（{type(exc).__name__}）") from None


def _append(ledger: Ledger, args: argparse.Namespace, op: dict[str, object]) -> dict[str, object]:
    return {"seq": ledger.append(op, by=args.by)}


def _show(ledger: Ledger, args: argparse.Namespace) -> dict[str, object]:
    """候補なら項目の内容、主張なら revision・系列・適格性（正解のラベルは出さない）。"""
    state = ledger.state()
    if (candidate := state.candidates.get(args.id)) is not None:
        return {"candidate": dataclasses.asdict(candidate), "item": ledger.candidate_item(candidate)}
    claim = state.claim(args.id)
    fields = {field.name: getattr(claim, field.name) for field in dataclasses.fields(claim) if field.name != "state"}
    eligible, reasons = state.eligibility(claim.claim_id)
    return {
        "claim": {**fields, "state": claim.state.model_dump(mode="json"), "confirmed": claim.confirmed},
        "family": dataclasses.asdict(state.family_of(claim.claim_id)),
        "gold_complete": state.gold_complete(claim.claim_id) is not None,
        "eligible": eligible,
        "reasons": reasons,
    }


def _candidates(ledger: Ledger, args: argparse.Namespace) -> list[dict[str, object]]:
    return [
        dataclasses.asdict(candidate)
        for candidate in ledger.state().candidates.values()
        if (args.repo is None or candidate.repository.casefold() == args.repo.casefold())
        and (args.status is None or candidate.status == args.status)
    ]


def _claim_create(ledger: Ledger, args: argparse.Namespace) -> dict[str, object]:
    claim_id = new_id()
    op: dict[str, object] = {
        "op": "claim.create",
        "claim_id": claim_id,
        "candidates": args.candidates,
        "target": {"before_sha": args.before_sha, "after_sha": args.after_sha},
        "issue": args.issue,
        "language": args.language,
        "state": _read_json(args.state_file),
    }
    return {"claim_id": claim_id, **_append(ledger, args, op)}


_LEDGER_OPS: dict[str, Callable[[argparse.Namespace], dict[str, object]]] = {
    "candidate-status": lambda a: {
        "op": "candidate.status",
        "candidate_id": a.id,
        "status": a.status,
        "reason": a.reason,
    },
    "claim-revise": lambda a: {"op": "claim.revise", "claim_id": a.id, "state": _read_json(a.state_file)},
    "claim-confirm": lambda a: {"op": "claim.confirm", "claim_id": a.id},
    "relate": lambda a: {
        "op": "relation.add",
        "left": a.left,
        "right": a.right,
        "relation": a.relation,
        "note": a.note,
    },
    "family-confirm": lambda a: {"op": "family.confirm", "claim_id": a.claim_id},
    "permit": lambda a: {
        "op": "permission.set",
        "subject": a.subject,
        "scope": a.scope,
        "runtime": a.runtime,
        "grant": a.grant,
        "note": a.note,
    },
    "expose": lambda a: {"op": "exposure.add", "claim_id": a.claim_id, "who": a.who, "kind": a.kind},
    "split": lambda a: {"op": "split.assign", "claim_id": a.claim_id, "split": a.split, "reason": a.reason},
    "time": lambda a: {
        "op": "time.add",
        "stage": a.stage,
        "seconds": a.seconds,
        "claims": a.claims,
        "note": a.note,
    },
}


def _add_ledger_parsers(root: argparse.ArgumentParser) -> None:
    ledger = root.add_subparsers(dest="ledger_command", required=True).add_parser
    claim = ledger("claim").add_subparsers(dest="claim_command", required=True).add_parser
    family = ledger("family").add_subparsers(dest="family_command", required=True).add_parser

    def leaf(
        add_parser: Callable[[str], argparse.ArgumentParser],
        name: str,
        action: Callable[[Ledger, argparse.Namespace], object] | None = None,
        op_name: str | None = None,
    ) -> argparse.ArgumentParser:
        parser = add_parser(name)
        parser.add_argument("--by", required=True)
        parser.add_argument("--config", default=None)
        if action is None:
            build = _LEDGER_OPS[op_name or name]
            action = lambda ledger, args: _append(ledger, args, build(args))  # noqa: E731
        parser.set_defaults(handler=_ledger, action=action)
        return parser

    leaf(ledger, "sync", lambda ledger, args: {"added": ledger.sync(by=args.by)})
    listing = leaf(ledger, "candidates", _candidates)
    listing.add_argument("--repo", default=None)
    listing.add_argument("--status", choices=CANDIDATE_STATUSES, default=None)
    status = leaf(ledger, "candidate-status")
    status.add_argument("id")
    status.add_argument("--status", required=True, choices=CANDIDATE_STATUSES)
    status.add_argument("--reason", default=None)
    create = leaf(claim, "create", _claim_create)
    create.add_argument("--candidates", nargs="+", required=True)
    create.add_argument("--after-sha", required=True)
    create.add_argument("--before-sha", default=None)
    create.add_argument("--issue", type=int, default=None)
    create.add_argument("--language", required=True)
    create.add_argument("--state-file", required=True)
    revise = leaf(claim, "revise", op_name="claim-revise")
    revise.add_argument("id")
    revise.add_argument("--state-file", required=True)
    leaf(claim, "confirm", op_name="claim-confirm").add_argument("id")
    relate = leaf(ledger, "relate")
    relate.add_argument("left")
    relate.add_argument("right")
    relate.add_argument("--relation", required=True, choices=RELATIONS)
    relate.add_argument("--note", default=None)
    leaf(family, "confirm", op_name="family-confirm").add_argument("claim_id")
    permit = leaf(ledger, "permit")
    permit.add_argument("--subject", required=True)
    permit.add_argument("--scope", required=True, choices=SCOPES)
    permit.add_argument("--runtime", default=None)
    decision = permit.add_mutually_exclusive_group(required=True)
    decision.add_argument("--grant", dest="grant", action="store_true")
    decision.add_argument("--deny", dest="grant", action="store_false")
    permit.add_argument("--note", default=None)
    expose = leaf(ledger, "expose")
    expose.add_argument("claim_id")
    expose.add_argument("--who", required=True)
    expose.add_argument("--kind", required=True, choices=EXPOSURE_KINDS)
    split = leaf(ledger, "split")
    split.add_argument("claim_id")
    split.add_argument("--split", required=True, choices=SPLITS)
    split.add_argument("--reason", required=True)
    spent = leaf(ledger, "time")
    spent.add_argument("--stage", required=True, choices=STAGES)
    spent.add_argument("--seconds", required=True, type=int)
    spent.add_argument("--claims", nargs="+", default=[])
    spent.add_argument("--note", default=None)
    leaf(ledger, "show", _show).add_argument("id")
    leaf(ledger, "stats", lambda ledger, args: ledger.state().stats(args.repo)).add_argument("--repo", default=None)


def _packet_export(ledger: Ledger, args: argparse.Namespace) -> dict[str, object]:
    path = export_packet(
        ledger,
        role=args.role,
        worker=args.worker,
        runtime=args.runtime,
        claim_ids=args.claims,
        out_dir=Path(args.out).expanduser(),
        by=args.by,
    )
    return {"packet": str(path)}


def _packet_import(ledger: Ledger, args: argparse.Namespace) -> dict[str, object]:
    return {"imported": import_answers(ledger, Path(args.answers).expanduser(), by=args.by)}


def _add_packet_parsers(root: argparse.ArgumentParser) -> None:
    packet = root.add_subparsers(dest="packet_command", required=True).add_parser
    export = packet("export")
    export.add_argument("--role", required=True, choices=ROLES)
    export.add_argument("--worker", required=True)
    export.add_argument("--runtime", required=True)
    export.add_argument("--claims", nargs="+", required=True)
    export.add_argument("--out", required=True)
    imported = packet("import")
    imported.add_argument("--answers", required=True)
    for parser, action in ((export, _packet_export), (imported, _packet_import)):
        parser.add_argument("--by", required=True)
        parser.add_argument("--config", default=None)
        parser.set_defaults(handler=_ledger, action=action)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review-eval")
    sub = parser.add_subparsers(dest="command", required=True)
    config_parser = sub.add_parser("config")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check")
    check.add_argument("--config", default=None)
    check.set_defaults(handler=_config_check)
    init_parser = sub.add_parser("store").add_subparsers(dest="store_command", required=True).add_parser("init")
    init_parser.add_argument("--store", required=True)
    init_parser.set_defaults(handler=_store_init)
    for name, handler in (("status", _status), ("verify", _verify)):
        simple = sub.add_parser(name)
        simple.add_argument("--config", default=None)
        simple.set_defaults(handler=handler)
    restore = sub.add_parser("restore")
    restore.add_argument("--dest", required=True)
    target = restore.add_mutually_exclusive_group(required=True)
    target.add_argument("--observation")
    target.add_argument("--repo")
    restore.add_argument("--pr", type=int, default=None)
    restore.add_argument("--config", default=None)
    restore.set_defaults(handler=_restore)
    ack = sub.add_parser("failures").add_subparsers(dest="failures_command", required=True).add_parser("ack")
    ack.add_argument("id")
    ack.add_argument("--by", required=True)
    ack.add_argument("--reason", required=True)
    ack.add_argument("--config", default=None)
    ack.set_defaults(handler=_failures_ack)
    hook = sub.add_parser("hook")
    hook.add_argument("--route", required=True, choices=ROUTES)
    hook.add_argument("--event", required=True)
    hook.add_argument("--review-dir", default=None)
    hook.add_argument("--file", action="append", default=[])
    hook.add_argument("--record", action="append", default=[])
    hook.add_argument("--repo", default=None)
    hook.add_argument("--pr", type=int, default=None)
    hook.add_argument("--context-json", default=None)
    hook.set_defaults(handler=_hook)
    ingest = sub.add_parser("ingest")
    ingest_target = ingest.add_mutually_exclusive_group(required=True)
    ingest_target.add_argument("--review-dir")
    ingest_target.add_argument("--repo")
    ingest.add_argument("--pr", type=int, default=None)
    ingest.add_argument("--config", default=None)
    ingest.set_defaults(handler=_ingest)
    listing = sub.add_parser("inventory")
    listing.add_argument("--repo", required=True)
    listing.add_argument("--pr", type=int, required=True)
    listing.add_argument("--config", default=None)
    listing.set_defaults(handler=_inventory)
    check_missing = sub.add_parser("reconcile")
    check_missing.add_argument("--repo", default=None)
    check_missing.add_argument("--apply", action="store_true")
    check_missing.add_argument("--config", default=None)
    check_missing.set_defaults(handler=_reconcile)
    _add_ledger_parsers(sub.add_parser("ledger"))
    _add_packet_parsers(sub.add_parser("packet"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    return args.handler(args)
