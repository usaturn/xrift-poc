"""確認待ち台帳を fold した状態: 候補・主張・系列・許可・露出・正解・適格性・集計（Spec 5 章・5.2）。

状態は journal の op を順に `apply` して作る。系列（連結成分）・正解・適格性は、その状態から毎回計算する。
"""

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Any

from tools.review_eval.ledger_ops import (
    CANDIDATE_KINDS,
    CANDIDATE_STATUSES,
    GOLD_ROLES,
    LABELS,
    SPLITS,
    STAGES,
    AnswerImport,
    CandidateKind,
    CandidateStatus,
    CandidateStatusSet,
    CandidateSync,
    ClaimConfirm,
    ClaimCreate,
    ClaimRevise,
    ClaimState,
    ExposureAdd,
    ExposureKind,
    FamilyConfirm,
    Label,
    LedgerError,
    Op,
    PacketExport,
    PermissionSet,
    RelationAdd,
    Role,
    Scope,
    Split,
    SplitAssign,
    TimeAdd,
    identity_key,
    members_identity,
)

CONTAMINATING: frozenset[ExposureKind] = frozenset({"pilot", "tuning"})


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    repository: str
    pr: int
    review_dir: str
    ref: str
    item_sha256: str
    kind: CandidateKind
    observation_id: str
    path: str
    status: CandidateStatus = "pending"
    reason: str | None = None


@dataclass(frozen=True)
class Claim:
    claim_id: str
    candidates: tuple[str, ...]
    repository: str
    pr: int
    issue: int | None
    before_sha: str | None
    after_sha: str
    language: str
    state: ClaimState
    state_sha256: str
    revision: int
    confirmed_revision: int | None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_revision == self.revision


@dataclass(frozen=True)
class Family:
    """系列（関係の辺と、同じ repo・PR の自動の辺で作る連結成分）。確認・split は members_sha256 ごとに効く。"""

    members: tuple[str, ...]
    members_sha256: str
    confirmed: bool
    split: Split | None
    held_out_invalidated: bool


@dataclass(frozen=True)
class Exposure:
    claim_id: str
    who: str
    kind: ExposureKind
    seq: int


@dataclass(frozen=True)
class AnswerRecord:
    packet_id: str
    item_id: str
    claim_id: str
    revision: int
    role: Role
    worker: str
    label: Label
    rationale: str
    seq: int


def repo_key(repository: str) -> str:
    return repository.casefold()


def _subject_key(subject: str) -> str:
    return subject.casefold() if subject.startswith("repo:") else subject


class LedgerState:
    """journal を fold した状態。台帳の外から変更しない。"""

    def __init__(self) -> None:
        self.last_seq = 0
        self.candidates: dict[str, Candidate] = {}
        self.claims: dict[str, Claim] = {}
        self.relations: list[RelationAdd] = []
        self.exposures: list[Exposure] = []
        self.packets: dict[str, PacketExport] = {}
        self.answer_imports: list[AnswerImport] = []
        self.answers: list[AnswerRecord] = []
        self.times: list[TimeAdd] = []
        self._family_confirmations: set[str] = set()
        self._splits: dict[str, tuple[Split, int]] = {}
        self._permissions: dict[tuple[str, Scope, str | None], bool] = {}
        self._item_ids: set[str] = set()
        self._answered: set[tuple[str, str]] = set()
        self._families: dict[str, Family] | None = None

    # fold
    def apply(self, op: Op) -> None:
        self.last_seq = op.seq
        self._families = None
        match op:
            case CandidateSync():
                self.candidates[op.candidate_id] = Candidate(
                    candidate_id=op.candidate_id,
                    repository=op.repository,
                    pr=op.pr,
                    review_dir=op.review_dir,
                    ref=op.ref,
                    item_sha256=op.item_sha256,
                    kind=op.kind,
                    observation_id=op.observation_id,
                    path=op.path,
                )
            case CandidateStatusSet():
                candidate = self.candidates[op.candidate_id]
                self.candidates[op.candidate_id] = replace(candidate, status=op.status, reason=op.reason)
            case ClaimCreate():
                target = op.target
                self.claims[op.claim_id] = Claim(
                    claim_id=op.claim_id,
                    candidates=tuple(op.candidates),
                    repository=target.repository,
                    pr=op.pr,
                    issue=op.issue,
                    before_sha=target.before_sha,
                    after_sha=target.after_sha,
                    language=op.language,
                    state=op.state,
                    state_sha256=op.state_sha256,
                    revision=1,
                    confirmed_revision=None,
                )
            case ClaimRevise():
                claim = self.claims[op.claim_id]
                self.claims[op.claim_id] = replace(
                    claim, state=op.state, state_sha256=op.state_sha256, revision=op.revision
                )
            case ClaimConfirm():
                self.claims[op.claim_id] = replace(self.claims[op.claim_id], confirmed_revision=op.revision)
            case RelationAdd():
                self.relations.append(op)
            case FamilyConfirm():
                self._family_confirmations.add(op.members_sha256)
            case PermissionSet():
                runtime = None if op.runtime is None else identity_key(op.runtime)
                self._permissions[(_subject_key(op.subject), op.scope, runtime)] = op.grant
            case ExposureAdd():
                self.exposures.append(Exposure(op.claim_id, op.who, op.kind, op.seq))
            case SplitAssign():
                self._splits[op.members_sha256] = (op.split, op.seq)
            case PacketExport():
                self.packets[op.packet_id] = op
                for item in op.items:
                    self._item_ids.add(item.item_id)
                    self.exposures.append(Exposure(item.claim_id, op.worker, "seen", op.seq))
            case AnswerImport():
                packet = self.packets[op.packet_id]
                items = {item.item_id: item for item in packet.items}
                self.answer_imports.append(op)
                for answer in op.answers:
                    item = items[answer.item_id]
                    self._answered.add((op.packet_id, answer.item_id))
                    self.answers.append(
                        AnswerRecord(
                            packet_id=op.packet_id,
                            item_id=answer.item_id,
                            claim_id=item.claim_id,
                            revision=item.revision,
                            role=packet.role,
                            worker=op.worker,
                            label=answer.label,
                            rationale=answer.rationale,
                            seq=op.seq,
                        )
                    )
            case TimeAdd():
                self.times.append(op)

    def has_item(self, item_id: str) -> bool:
        return item_id in self._item_ids

    def answered(self, packet_id: str, item_id: str) -> bool:
        return (packet_id, item_id) in self._answered

    # 主張・系列
    def claim(self, claim_id: str) -> Claim:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise LedgerError(f"主張が無い: {claim_id}")
        return claim

    def candidate(self, candidate_id: str) -> Candidate:
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise LedgerError(f"候補が無い: {candidate_id}")
        return candidate

    def _family_map(self) -> dict[str, Family]:
        if self._families is not None:
            return self._families
        parent = {claim_id: claim_id for claim_id in self.claims}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            parent[find(left)] = find(right)

        for relation in self.relations:
            union(relation.left, relation.right)
        same_pr: dict[tuple[str, int], str] = {}
        for claim in self.claims.values():  # 同じ repo・PR の主張には same_pr_issue の辺を自動で張る
            first = same_pr.setdefault((repo_key(claim.repository), claim.pr), claim.claim_id)
            union(claim.claim_id, first)
        groups: dict[str, list[str]] = {}
        for claim_id in self.claims:
            groups.setdefault(find(claim_id), []).append(claim_id)
        families: dict[str, Family] = {}
        for group in groups.values():
            members = tuple(sorted(group))
            members_sha256 = members_identity(list(members))
            assigned = self._splits.get(members_sha256)
            split = None if assigned is None else assigned[0]
            invalidated = (
                assigned is not None
                and assigned[0] == "held_out"
                and any(
                    e.kind in CONTAMINATING and e.claim_id in members and e.seq > assigned[1] for e in self.exposures
                )
            )
            family = Family(members, members_sha256, members_sha256 in self._family_confirmations, split, invalidated)
            for member in members:
                families[member] = family
        self._families = families
        return families

    def families(self) -> list[Family]:
        unique = {family.members_sha256: family for family in self._family_map().values()}
        return sorted(unique.values(), key=lambda family: family.members)

    def family_of(self, claim_id: str) -> Family:
        self.claim(claim_id)
        return self._family_map()[claim_id]

    # 許可・露出・正解
    def permitted(self, claim_id: str, scope: Scope, runtime: str | None = None) -> bool:
        """主張か repo のどちらかで許可され、どちらでも拒否されていないこと（拒否が優先）。"""
        claim = self.claim(claim_id)
        runtime_key = None if runtime is None else identity_key(runtime)
        values = [
            grant
            for subject in (claim_id, _subject_key(f"repo:{claim.repository}"))
            if (grant := self._permissions.get((subject, scope, runtime_key))) is not None
        ]
        return bool(values) and all(values)

    def exposed_identities(self, family: Family, *, ignore_seqs: Collection[int] = ()) -> set[str]:
        """系列の主張に露出がある識別子。`ignore_seqs` の op が記録した露出は数えない。"""
        return {
            identity_key(e.who) for e in self.exposures if e.claim_id in family.members and e.seq not in ignore_seqs
        }

    def gold_identities(self) -> set[str]:
        """台帳のどこかで正解を知った識別子: 正解担当の packet・回答の worker、正解の回答を取り込んだ by、
        G1/G2 のラベルを含む adjudicator の packet を書き出した by。"""
        known: set[str] = set()
        for packet in self.packets.values():
            if packet.role in GOLD_ROLES:
                known.add(identity_key(packet.worker))
            if packet.role == "adjudicator":
                known.add(identity_key(packet.by))
        for imported in self.answer_imports:
            if self.packets[imported.packet_id].role in GOLD_ROLES:
                known.update({identity_key(imported.worker), identity_key(imported.by)})
        return known

    def prediction_identities(self) -> set[str]:
        """台帳のどこかで予測を見た識別子: evaluator の packet・回答の worker、evaluator の回答を取り込んだ by。

        正解担当は、互いの回答・予測なしに独立に照合する（`gold_identities` と対称の規則）。"""
        seen: set[str] = set()
        for packet in self.packets.values():
            if packet.role == "evaluator":
                seen.add(identity_key(packet.worker))
        for imported in self.answer_imports:
            if self.packets[imported.packet_id].role == "evaluator":
                seen.update({identity_key(imported.worker), identity_key(imported.by)})
        return seen

    def role_workers(self, claim: Claim) -> dict[Role, set[str]]:
        """今の revision の item を受け取った担当（役割別）。"""
        workers: dict[Role, set[str]] = {"g1": set(), "g2": set(), "adjudicator": set(), "evaluator": set()}
        for packet in self.packets.values():
            if any(i.claim_id == claim.claim_id and i.revision == claim.revision for i in packet.items):
                workers[packet.role].add(identity_key(packet.worker))
        return workers

    def role_labels(self, claim_id: str, role: Role) -> list[Label]:
        """今の revision のその役割の回答のラベル（取り込んだ順）。正解の役割は 1 つまでしか取り込まない。"""
        claim = self.claim(claim_id)
        return [
            a.label for a in self.answers if a.claim_id == claim_id and a.revision == claim.revision and a.role == role
        ]

    def gold_complete(self, claim_id: str) -> Label | None:
        """今の revision の G1・G2 が一致すればそのラベル、不一致で A の裁定があればそのラベル。

        同じ役割に回答が 2 つ以上ある（検査を経ずに journal に入った）ときは、どれも採らず None。"""
        first, second, third = (self.role_labels(claim_id, role) for role in ("g1", "g2", "adjudicator"))
        if len(first) != 1 or len(second) != 1 or len(third) > 1:
            return None
        if first == second:
            return first[0]
        return third[0] if third else None

    def disagreement(self, claim_id: str) -> bool:
        """G1・G2 の回答がそれぞれ 1 つずつあり、ラベルが違う。"""
        first, second = self.role_labels(claim_id, "g1"), self.role_labels(claim_id, "g2")
        return len(first) == len(second) == 1 and first != second

    def eligibility(self, claim_id: str) -> tuple[bool, list[str]]:
        claim = self.claim(claim_id)
        family = self.family_of(claim_id)
        reasons: list[str] = []
        if not claim.confirmed:
            reasons.append("claim_not_confirmed")
        if not family.confirmed:
            reasons.append("family_not_confirmed")
        if not self.permitted(claim_id, "local_use"):
            reasons.append("local_use_not_permitted")
        if self.gold_complete(claim_id) is None:
            reasons.append("gold_incomplete")
        if family.split is None:
            reasons.append("split_unassigned")
        elif family.split == "excluded":
            reasons.append("split_excluded")
        elif family.held_out_invalidated:
            reasons.append("held_out_invalidated")
        return (not reasons, reasons)

    # 集計
    def stats(self, repository: str | None = None) -> dict[str, Any]:
        """Spec 5.2。予測と正解を突き合わせない。held-out は件数だけを出し、ラベルの内訳・ケースを出さない。"""
        if repository is not None:
            return {"repository": repository, **self._section(repo_key(repository))}
        keys = sorted(
            {repo_key(c.repository) for c in self.candidates.values()}
            | {repo_key(c.repository) for c in self.claims.values()}
        )
        return {"total": self._section(None), "by_repository": {key: self._section(key) for key in keys}}

    def _section(self, key: str | None) -> dict[str, Any]:
        candidates = [c for c in self.candidates.values() if key is None or repo_key(c.repository) == key]
        claims = [c for c in self.claims.values() if key is None or repo_key(c.repository) == key]
        claim_ids = {claim.claim_id for claim in claims}
        families = [f for f in self.families() if claim_ids.intersection(f.members)]
        statuses = Counter(c.status for c in candidates)
        split_names = (*SPLITS, "unassigned")
        splits: dict[str, dict[str, int]] = {name: {"claims": 0, "families": 0} for name in split_names}
        invalidated = {"claims": 0, "families": 0}
        for family in families:
            splits[family.split or "unassigned"]["families"] += 1
            invalidated["families"] += family.held_out_invalidated
        gold_labels = dict.fromkeys(LABELS, 0)
        eligible = {"calibration": 0, "held_out": 0}
        counts = Counter[str]()
        for claim in claims:
            family = self.family_of(claim.claim_id)
            splits[family.split or "unassigned"]["claims"] += 1
            invalidated["claims"] += family.held_out_invalidated
            label = self.gold_complete(claim.claim_id)
            if label is None:
                counts["gold_incomplete"] += 1
                counts["gold_disagreement"] += self.disagreement(claim.claim_id)
            elif family.split == "held_out":
                counts["held_out_gold_complete"] += 1
            else:
                gold_labels[label] += 1
            if self.eligibility(claim.claim_id)[0] and family.split in eligible:
                eligible[family.split] += 1
        times = [t for t in self.times if key is None or claim_ids.intersection(t.claims)]
        return {
            "candidates": {
                "by_status": {status: statuses[status] for status in CANDIDATE_STATUSES},
                "by_kind": {kind: sum(1 for c in candidates if c.kind == kind) for kind in CANDIDATE_KINDS},
            },
            "claims": {
                "draft": sum(1 for c in claims if not c.confirmed),
                "confirmed": sum(1 for c in claims if c.confirmed),
                "by_language": dict(sorted(Counter(c.language for c in claims).items())),
            },
            "families": {
                "confirmed": sum(1 for f in families if f.confirmed),
                "unconfirmed": sum(1 for f in families if not f.confirmed),
            },
            "splits": {**splits, "held_out_invalidated": invalidated},
            "gold_labels_excluding_held_out": gold_labels,
            "held_out_gold_complete": counts["held_out_gold_complete"],
            "gaps": {status: statuses[status] for status in ("missing", "failed", "no_permission")},
            "time_seconds": {stage: sum(t.seconds for t in times if t.stage == stage) for stage in STAGES},
            "eligible": eligible,
            "undetermined": {
                "candidates_pending": statuses["pending"],
                "gold_incomplete": counts["gold_incomplete"],
                "gold_disagreement": counts["gold_disagreement"],
            },
        }
