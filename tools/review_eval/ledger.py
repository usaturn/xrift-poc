"""確認待ち台帳: journal（追記のみ）、op の検査、候補の導出（Spec 5 章）。

`<store>/ledger/journal.jsonl` に op を 1 行ずつ追記する。追記は保存先の lock の中で journal を fold し、今の状態に
対して検査してから行い、fsync する。過去の op は書き換えない。適格は fold した状態（`ledger_state.py`）から計算で
決め、自動で採用しない。op のモデルは `ledger_ops.py`。
"""

import json
import os
import re
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from tools.review_eval.canonical import canonical_bytes, hjson
from tools.review_eval.formats import FULL_SHA
from tools.review_eval.ledger_ops import (
    GOLD_ROLES,
    LABELS,
    LEDGER_SCHEMA,
    MAX_STATE_BYTES,
    OP_ADAPTER,
    AnswerImport,
    CandidateKind,
    CandidateStatusSet,
    CandidateSync,
    ClaimConfirm,
    ClaimCreate,
    ClaimRevise,
    ClaimState,
    ExposureAdd,
    FamilyConfirm,
    Label,
    LedgerError,
    Op,
    PacketExport,
    PermissionSet,
    RelationAdd,
    Role,
    SplitAssign,
    TimeAdd,
    candidate_identity,
    identity_key,
    new_id,
)
from tools.review_eval.ledger_state import CONTAMINATING, Candidate, Claim, Family, LedgerState, repo_key
from tools.review_eval.models import Observation
from tools.review_eval.store import Store, StoreError

__all__ = [
    "LABELS",
    "Candidate",
    "Claim",
    "Family",
    "Label",
    "Ledger",
    "LedgerError",
    "LedgerState",
    "derive_candidates",
    "new_id",
]

_FINDINGS_SCHEMAS = frozenset({"review-findings/v1", "review-findings/v2"})
_HEAD12 = re.compile(r"[0-9a-f]{12}")
_FINDINGS_FILE = re.compile(r"findings/(?P<stem>.+)-[0-9a-f]{12}\.json")
_REPO_SUBJECT = re.compile(r"repo:[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
_DIR_MODE = 0o700
_FILE_MODE = 0o600


# 検査（今の状態に対して。拒否は LedgerError）
def _check_state(state: ClaimState, state_sha256: str, before_sha: str | None) -> None:
    data = state.model_dump(mode="json")
    if hjson(data) != state_sha256:
        raise LedgerError("state_sha256 が state と一致しない")
    if len(canonical_bytes(data)) > MAX_STATE_BYTES:
        raise LedgerError(f"state が {MAX_STATE_BYTES} bytes を超える（切り詰めない。主張を分けるか根拠を減らす）")
    if before_sha is None and any(evidence.at == "before" for evidence in state.evidence):
        raise LedgerError("before_sha の無い主張の根拠に at: before は使えない")


def _check_members(state: LedgerState, claim_id: str, members: Sequence[str], members_sha256: str) -> Family:
    family = state.family_of(claim_id)
    if tuple(members) != family.members or members_sha256 != family.members_sha256:
        raise LedgerError("系列の構成が今の状態と一致しない")
    return family


def _check_packet(state: LedgerState, op: PacketExport) -> None:
    if op.packet_id in state.packets:
        raise LedgerError("その packet_id はすでにある")
    item_ids = [item.item_id for item in op.items]
    claim_ids = [item.claim_id for item in op.items]
    if len(set(item_ids)) != len(item_ids) or any(state.has_item(item_id) for item_id in item_ids):
        raise LedgerError("item_id が重複している")
    if len(set(claim_ids)) != len(claim_ids):
        raise LedgerError("同じ主張を 1 つの packet に 2 回入れられない")
    worker = identity_key(op.worker)
    for item in op.items:
        claim = state.claim(item.claim_id)
        if (item.revision, item.state_sha256) != (claim.revision, claim.state_sha256):
            raise LedgerError(f"主張の今の revision ではない: {claim.claim_id}")
        if not claim.confirmed:
            raise LedgerError(f"確認していない revision は書き出せない: {claim.claim_id}")
        if not state.permitted(claim.claim_id, "local_use"):
            raise LedgerError(f"local_use の許可が無い: {claim.claim_id}")
        if not state.permitted(claim.claim_id, "runtime_presentation", op.runtime):
            raise LedgerError(f"runtime_presentation（{op.runtime}）の許可が無い: {claim.claim_id}")
        workers = state.role_workers(claim)
        others: dict[Role, set[str]] = {
            "g1": workers["g2"] | workers["adjudicator"],
            "g2": workers["g1"] | workers["adjudicator"],
            "adjudicator": workers["g1"] | workers["g2"],
            "evaluator": set(),
        }
        if worker in others[op.role]:
            raise LedgerError(f"同じ主張の別の正解担当と同じ識別子は {op.role} にできない: {claim.claim_id}")
        if op.role in GOLD_ROLES:
            _check_gold_worker(state, claim.claim_id, op.role, worker)
        if op.role == "adjudicator" and not state.disagreement(claim.claim_id):
            raise LedgerError(f"G1・G2 の回答が一致しない主張だけを adjudicator に渡せる: {claim.claim_id}")
        if op.role == "evaluator":
            if state.family_of(claim.claim_id).split is None:
                raise LedgerError(f"split を割り当てていない系列の主張は evaluator に渡せない: {claim.claim_id}")
            _check_evaluator(state, claim.claim_id, worker)


def _check_evaluator(state: LedgerState, claim_id: str, worker: str, *, answering: PacketExport | None = None) -> None:
    """evaluator（盲検予測）の担当が正解・対象の系列を知らないこと。書き出しと取り込みの両方で検査する。

    取り込み（`answering` は回答する packet）では、その packet の書き出しが記録した `seen` の露出だけを理由にしない。
    書き出しの後に正解を知った・露出を記録された担当の回答は、盲検予測として取り込まない。
    """
    if worker in state.gold_identities():
        raise LedgerError("正解担当・正解を取り込んだ識別子は evaluator になれない")
    ignored = () if answering is None else (answering.seq,)
    if worker in state.exposed_identities(state.family_of(claim_id), ignore_seqs=ignored):
        raise LedgerError(f"系列に露出がある識別子は evaluator になれない: {claim_id}")


def _check_gold_worker(state: LedgerState, claim_id: str, role: Role, worker: str) -> None:
    """正解担当の書き出し・取り込みの共通の検査（今の revision でその役割は 1 回だけ答える。予測を見た担当は不可）。"""
    if state.role_labels(claim_id, role):
        raise LedgerError(f"今の revision の {role} の回答はすでにある: {claim_id}")
    if worker in state.prediction_identities():
        raise LedgerError("予測を見た識別子（evaluator の担当・その回答を取り込んだ識別子）は正解担当になれない")


def _check_answers(state: LedgerState, op: AnswerImport) -> None:
    packet = state.packets.get(op.packet_id)
    if packet is None:
        raise LedgerError("その packet は書き出していない")
    worker = identity_key(op.worker)
    if worker != identity_key(packet.worker):
        raise LedgerError("回答の担当が packet の担当と違う")
    item_ids = [answer.item_id for answer in op.answers]
    if len(set(item_ids)) != len(item_ids):
        raise LedgerError("同じ item の回答が 2 つある")
    items = {item.item_id: item for item in packet.items}
    for answer in op.answers:
        item = items.get(answer.item_id)
        if item is None:
            raise LedgerError("packet に無い item の回答がある")
        if state.answered(op.packet_id, answer.item_id):
            raise LedgerError("その item の回答はすでに取り込んだ")
        claim = state.claim(item.claim_id)
        if (item.revision, item.state_sha256) != (claim.revision, claim.state_sha256):
            raise LedgerError("stale: packet の書き出しの後に主張が改訂された（古い入力の回答は取り込まない）")
        if packet.role in GOLD_ROLES:
            _check_gold_worker(state, claim.claim_id, packet.role, worker)
        if packet.role == "adjudicator" and not state.disagreement(claim.claim_id):
            raise LedgerError("G1・G2 の回答が一致しない主張だけを裁定できる")
        if packet.role == "evaluator":
            _check_evaluator(state, claim.claim_id, worker, answering=packet)


def check(state: LedgerState, op: Op, observation: Callable[[str], Observation | None]) -> None:
    """op を今の状態に対して検査する。`observation` は保存先の observation を id で読む関数。"""
    match op:
        case CandidateSync():
            if op.candidate_id in state.candidates:
                raise LedgerError("その候補はすでに登録されている")
            if candidate_identity(op.repository, op.pr, op.review_dir, op.ref, op.item_sha256) != op.candidate_id:
                raise LedgerError("candidate_id が内容と一致しない")
            source = observation(op.observation_id)
            if source is None or source.body != "stored":
                raise LedgerError("候補の出典は本文を保存した observation にする")
            recorded = (source.repository, source.pr, source.review_dir, source.path)
            if recorded != (op.repository, op.pr, op.review_dir, op.path):
                raise LedgerError("候補の repo・PR・review_dir・path が出典の observation と一致しない")
        case CandidateStatusSet():
            state.candidate(op.candidate_id)
            if op.status != "pending" and not (op.reason and op.reason.strip()):
                raise LedgerError("pending 以外の状態には理由が必要")
        case ClaimCreate():
            if op.claim_id in state.claims or op.claim_id in state.candidates:
                raise LedgerError("その claim_id はすでにある")
            if len(set(op.candidates)) != len(op.candidates):
                raise LedgerError("親の候補が重複している")
            parents = [state.candidate(candidate_id) for candidate_id in op.candidates]
            if any(parent.status != "selected" for parent in parents):
                raise LedgerError("親の候補は selected のものだけにする")
            if {(repo_key(p.repository), p.pr) for p in parents} != {(repo_key(op.target.repository), op.pr)}:
                raise LedgerError("親の候補は同じ repo・PR のものだけにする")
            for parent in parents:
                found = observation(parent.observation_id)
                if found is None or found.body != "stored":
                    raise LedgerError(f"本文を保存していない observation の候補は親にできない: {parent.candidate_id}")
            _check_state(op.state, op.state_sha256, op.target.before_sha)
        case ClaimRevise():
            claim = state.claim(op.claim_id)
            if op.revision != claim.revision + 1:
                raise LedgerError("revision が今の revision の次ではない")
            _check_state(op.state, op.state_sha256, claim.before_sha)
            if op.state_sha256 == claim.state_sha256:
                raise LedgerError("内容が今の revision と同じ（改訂しない）")
        case ClaimConfirm():
            claim = state.claim(op.claim_id)
            if (op.revision, op.state_sha256) != (claim.revision, claim.state_sha256):
                raise LedgerError("確認する revision が今の revision ではない")
        case RelationAdd():
            state.claim(op.left)
            state.claim(op.right)
            if op.left == op.right:
                raise LedgerError("同じ主張どうしに関係は張れない")
        case FamilyConfirm():
            _check_members(state, op.claim_id, op.members, op.members_sha256)
        case PermissionSet():
            if op.subject.startswith("repo:"):
                if not _REPO_SUBJECT.fullmatch(op.subject):
                    raise LedgerError("subject は主張 id か repo:<owner>/<repo> にする")
            else:
                state.claim(op.subject)
            if (op.scope == "runtime_presentation") != (op.runtime is not None):
                raise LedgerError("runtime は runtime_presentation のときだけ指定する（そのときは必須）")
        case ExposureAdd():
            state.claim(op.claim_id)
        case SplitAssign():
            family = _check_members(state, op.claim_id, op.members, op.members_sha256)
            if op.split == "held_out":
                if not family.confirmed:
                    raise LedgerError("held_out には系列の確認が必要")
                if any(e.kind in CONTAMINATING and e.claim_id in family.members for e in state.exposures):
                    raise LedgerError("pilot・tuning の露出がある系列は held_out にできない")
        case PacketExport():
            _check_packet(state, op)
        case AnswerImport():
            _check_answers(state, op)
        case TimeAdd():
            for claim_id in op.claims:
                state.claim(claim_id)


# 派生項目（台帳が今の状態から埋める。呼び出し元が書いたなら一致を確かめる）
def _fill(raw: dict[str, Any], key: str, value: object) -> None:
    if key in raw and raw[key] != value:
        raise LedgerError(f"{key} が今の状態と一致しない")
    raw[key] = value


def _complete(state: LedgerState, raw: dict[str, Any]) -> None:
    name = raw.get("op")
    if name in ("claim.create", "claim.revise") and isinstance(raw.get("state"), dict):
        _fill(raw, "state_sha256", hjson(raw["state"]))
    if name == "claim.create":
        raw.setdefault("issue", None)
        parents = raw.get("candidates")
        target = raw.get("target")
        if isinstance(parents, list) and parents and isinstance(target, dict):
            first = state.candidate(str(cast(list[object], parents)[0]))
            fields = cast(dict[str, Any], target)
            claimed = fields.get("repository")
            if isinstance(claimed, str) and repo_key(claimed) != repo_key(first.repository):
                raise LedgerError("target.repository が親の候補の repo と違う")
            fields["repository"] = first.repository
            _fill(raw, "pr", first.pr)
    elif name in ("claim.revise", "claim.confirm") and isinstance(raw.get("claim_id"), str):
        claim = state.claim(raw["claim_id"])
        if name == "claim.revise":
            _fill(raw, "revision", claim.revision + 1)
        else:
            _fill(raw, "revision", claim.revision)
            _fill(raw, "state_sha256", claim.state_sha256)
    elif name in ("family.confirm", "split.assign") and isinstance(raw.get("claim_id"), str):
        family = state.family_of(raw["claim_id"])
        _fill(raw, "members", list(family.members))
        _fill(raw, "members_sha256", family.members_sha256)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(op: Op) -> bytes:
    data = op.model_dump(mode="json", by_alias=True)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


# 候補の導出（candidate.sync）
def _json_document(store: Store, observation: Observation) -> dict[str, Any] | None:
    def reject(constant: str) -> object:
        raise ValueError(constant)

    try:
        value: object = json.loads(store.read_blob(observation.sha256), parse_constant=reject)
    except StoreError, ValueError, RecursionError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _head12(value: object) -> str | None:
    if isinstance(value, str) and FULL_SHA.fullmatch(value):
        return value[:12]
    return value if isinstance(value, str) and _HEAD12.fullmatch(value) else None


def _fields(item: object) -> dict[str, object]:
    return cast(dict[str, object], item) if isinstance(item, dict) else {}


def _item_id(item: object) -> str:
    value = _fields(item).get("id")
    return value if isinstance(value, str) and value and "#" not in value and "@" not in value else "unknown"


def _review_stem(document: dict[str, Any], path: str) -> str:
    review_file = _fields(document.get("reviewer")).get("review_file")
    if isinstance(review_file, str) and review_file.endswith(".md") and len(review_file) > 3:
        return PurePosixPath(review_file).name.removesuffix(".md")
    match = _FINDINGS_FILE.fullmatch(path)
    return match["stem"] if match else PurePosixPath(path).stem


def derive_candidates(store: Store) -> list[dict[str, Any]]:
    """本文を保存した observation から候補（`candidate.sync` の op）を導く。同じ id は 1 つにする。

    - 候補一覧（`kind: candidates`）の `findings[]`・`unverified_candidates[]`。
      ref は `<review_file の stem>#<ID>@<HEAD12>`（指摘は検出 HEAD、無ければ reviewed_head）
    - 確認済みの一覧（`kind: verified`）の `sources` が空の指摘（fix_check の新規の問題）。
      ref は `fix-check#<ID>@<to の 12 文字>`
    - レビューファイル（`<model>.md`）で、同じ review_dir・stem・reviewed_head の候補一覧を本文ごと取っていないもの
      → markdown_only の unresolved 1 枠（reviewed_head が不明なら、照合できないので枠を作る）
    """
    observations = [o for o in store.observations() if o.body == "stored" and o.review_dir is not None]
    found: dict[str, dict[str, Any]] = {}
    covered: set[tuple[str, int, str, str, str]] = set()

    def add(o: Observation, kind: CandidateKind, ref: str, item: object) -> None:
        review_dir = cast(str, o.review_dir)
        item_sha256 = hjson(item)
        candidate_id = candidate_identity(o.repository, o.pr, review_dir, ref, item_sha256)
        op = {"op": "candidate.sync", "candidate_id": candidate_id, "repository": o.repository, "pr": o.pr}
        op.update(review_dir=review_dir, ref=ref, item_sha256=item_sha256, kind=kind)
        found.setdefault(candidate_id, {**op, "observation_id": o.observation_id, "path": o.path})

    for o in observations:
        if o.format.schema_ not in _FINDINGS_SCHEMAS:
            continue
        document = _json_document(store, o)
        if document is None:
            continue
        head = document.get("reviewed_head")
        findings, unverified = document.get("findings"), document.get("unverified_candidates")
        if o.format.kind == "candidates" and isinstance(findings, list) and isinstance(unverified, list):
            stem = _review_stem(document, o.path)
            head12 = _head12(head) or "unknown"
            if isinstance(head, str) and FULL_SHA.fullmatch(head):
                covered.add((repo_key(o.repository), o.pr, cast(str, o.review_dir), stem, head))
            for item in cast(list[object], findings):
                detected = _head12(_fields(item).get("detected_head"))
                add(o, "finding", f"{stem}#{_item_id(item)}@{detected or head12}", item)
            for item in cast(list[object], unverified):
                add(o, "unverified_candidate", f"{stem}#{_item_id(item)}@{head12}", item)
        elif o.format.kind == "verified" and isinstance(findings, list):
            fix_check = _fields(document.get("fix_check"))
            to = fix_check.get("to", head)
            for item in cast(list[object], findings):
                if _fields(item).get("sources") == []:
                    add(o, "new_problem", f"fix-check#{_item_id(item)}@{_head12(to) or 'unknown'}", item)
    for o in observations:
        if o.format.kind != "review_markdown":
            continue
        stem, head = o.path.removesuffix(".md"), o.target.reviewed_head
        if head is not None and (repo_key(o.repository), o.pr, o.review_dir, stem, head) in covered:
            continue
        item = {"review_file": o.path, "sha256": o.sha256}
        add(o, "markdown_only", f"{stem}#markdown_only@{_head12(head) or 'unknown'}", item)
    return list(found.values())


class Ledger:
    def __init__(self, store: Store, *, lock_timeout: float) -> None:
        self.store = store
        self.lock_timeout = lock_timeout
        self.path = store.root / "ledger" / "journal.jsonl"

    @contextmanager
    def _locked(self) -> Generator[None]:
        try:
            lock = self.store.locked(self.lock_timeout)
            lock.__enter__()
        except StoreError as exc:
            raise LedgerError(str(exc)) from None
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def _fold(self) -> LedgerState:
        state = LedgerState()
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return state
        except OSError as exc:
            raise LedgerError(f"journal を読めない: {type(exc).__name__}") from None
        lines = data.split(b"\n")
        if lines[-1]:
            raise LedgerError(f"journal の {len(lines)} 行目が改行で終わっていない（追記の途中で止まった可能性）")
        for number, line in enumerate(lines[:-1], start=1):
            try:
                op = OP_ADAPTER.validate_json(line)
            except ValidationError:
                raise LedgerError(f"journal の {number} 行目を読めない") from None
            if op.seq != state.last_seq + 1:
                raise LedgerError(f"journal の {number} 行目の seq が連続しない")
            try:
                state.apply(op)
            except (KeyError, LedgerError) as exc:
                raise LedgerError(f"journal の {number} 行目を適用できない: {exc}") from None
        return state

    def state(self) -> LedgerState:
        with self._locked():
            return self._fold()

    def _prepare(self, state: LedgerState, op: Mapping[str, object], by: str) -> Op:
        reserved = {"schema", "seq", "at", "by"}.intersection(op)
        if reserved:
            raise LedgerError(f"台帳が付ける項目は op に書かない: {', '.join(sorted(reserved))}")
        try:
            raw = cast(dict[str, Any], json.loads(json.dumps(op, ensure_ascii=False, allow_nan=False)))
        except (TypeError, ValueError) as exc:
            raise LedgerError(f"op を JSON にできない: {type(exc).__name__}") from None
        _complete(state, raw)
        raw.update({"schema": LEDGER_SCHEMA, "seq": state.last_seq + 1, "at": _now(), "by": by})
        try:
            validated = OP_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            raise LedgerError(f"op の形が不正: {details}") from None
        check(state, validated, self.store.observation)
        return validated

    def _transaction(self, build: Callable[[LedgerState], Sequence[Mapping[str, object]]], *, by: str) -> list[int]:
        """lock の中で fold → 検査 → 追記 → fsync。どれかの op が拒否されたら 1 件も書かない。"""
        with self._locked():
            state = self._fold()
            lines: list[bytes] = []
            seqs: list[int] = []
            for op in build(state):
                validated = self._prepare(state, op, by)
                state.apply(validated)
                lines.append(_line(validated))
                seqs.append(validated.seq)
            if lines:
                self._write(b"".join(lines))
            return seqs

    def _write(self, data: bytes) -> None:
        directory = self.path.parent
        new_directory, new_file = not directory.exists(), not self.path.exists()
        directory.mkdir(mode=_DIR_MODE, exist_ok=True)
        directory.chmod(_DIR_MODE)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, _FILE_MODE)
        try:
            os.fchmod(fd, _FILE_MODE)
            size = os.fstat(fd).st_size
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            except OSError:
                os.ftruncate(fd, size)  # 書きかけの行を残さない
                raise
        finally:
            os.close(fd)
        if new_file:
            _fsync_directory(directory)
        if new_directory:
            _fsync_directory(directory.parent)

    def append(self, op: Mapping[str, object], *, by: str) -> int:
        """op を 1 件検査して追記し、seq を返す。`schema`・`seq`・`at`・`by` は台帳が付ける。"""
        return self._transaction(lambda _state: [op], by=by)[0]

    def sync(self, *, by: str) -> int:
        """観測済みの候補を登録する（冪等）。追加した件数を返す。"""
        derived = derive_candidates(self.store)
        return len(
            self._transaction(lambda state: [c for c in derived if c["candidate_id"] not in state.candidates], by=by)
        )

    def candidate_item(self, candidate: Candidate) -> object | None:
        """候補の項目の内容（観測した本文から取り出す）。本文が無ければ None。"""
        found = self.store.observation(candidate.observation_id)
        if found is None or found.body != "stored":
            return None
        if candidate.kind == "markdown_only":
            return {"review_file": found.path, "sha256": found.sha256}
        document = _json_document(self.store, found) or {}
        for key in ("findings", "unverified_candidates"):
            items = document.get(key)
            for item in cast(list[object], items) if isinstance(items, list) else []:
                if hjson(item) == candidate.item_sha256:
                    return item
        return None


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
