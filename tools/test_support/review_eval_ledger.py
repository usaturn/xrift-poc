"""確認待ち台帳の fixture: 収集した v2 の候補一覧から候補・主張・許可・正解を作る（台帳と packet のテスト共用）。"""

import json
from pathlib import Path

import pytest

from tools.review_eval.capture import collect
from tools.review_eval.ledger import Label, Ledger, new_id
from tools.review_eval.store import Store
from tools.test_support.review_eval import make_collection_workspace, write_collection_config

PRODUCTS = ("usaturn/alpha", "usaturn/beta")
HEAD = "9" * 40
BASE = "8" * 40
CURATOR = "curator"
RUNTIME = "claude-code"


def candidates_document(repository: str, number: int) -> bytes:
    document = {
        "schema": "review-findings/v2",
        "kind": "candidates",
        "source": "reviewer",
        "pr": {"repository": repository, "number": number},
        "reviewed_head": HEAD,
        "base": BASE,
        "reviewer": {"model": "claude-opus-5-5", "runtime": RUNTIME, "review_file": "claude-opus-5-5.md"},
        "findings": [
            {
                "id": "M-1",
                "severity": "Medium",
                "title": "オフセット付きの日時を UTC として読む\n2 つ目の主張: 書き出しも同じ",
                "locations": ["src/schedule/importer.py:41"],
                "detected_head": HEAD[:12],
            }
        ],
        "unverified_candidates": [],
    }
    return json.dumps(document, ensure_ascii=False, indent=2).encode()


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """alpha の #5・#6 と beta の #5（同じ番号の別 repo）の候補一覧を収集し、台帳に候補として登録する。"""
    root, repos = make_collection_workspace(tmp_path / "base", PRODUCTS)
    store = Store.init(tmp_path / "store")
    config = write_collection_config(tmp_path / "review-eval.toml", store=store.root, workspace_root=root, repos=repos)
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(config))
    for repository, number in (("usaturn/alpha", 5), ("usaturn/alpha", 6), ("usaturn/beta", 5)):
        review_dir = root / "reviews" / repository / f"#{number}-feat"
        findings = review_dir / "findings" / f"claude-opus-5-5-{HEAD[:12]}.json"
        findings.parent.mkdir(parents=True)
        findings.write_bytes(candidates_document(repository, number))
        collect(route="pr-review", event="complete", review_dir=review_dir, files=[findings])
    ledger = Ledger(store, lock_timeout=10)
    assert ledger.sync(by=CURATOR) == 3
    assert ledger.sync(by=CURATOR) == 0
    return ledger


def claim(ledger: Ledger, repository: str, pr: int) -> str:
    (candidate,) = [c for c in ledger.state().candidates.values() if c.repository == repository and c.pr == pr]
    ledger.append(
        {"op": "candidate.status", "candidate_id": candidate.candidate_id, "status": "selected", "reason": "日時"},
        by=CURATOR,
    )
    claim_id = new_id()
    state = {
        "claim": "オフセット付きの日時を UTC として読む",
        "diff": "-    return parse(value)\n+    return parse(value).astimezone(UTC)\n",
        "evidence": [
            {"at": "before", "path": "src/a.py", "start_line": 40, "end_line": 41, "text": "return parse(value)"},
            {"at": "after", "path": "src/a.py", "start_line": 40, "end_line": 41, "text": "astimezone(UTC)"},
        ],
    }
    ledger.append(
        {
            "op": "claim.create",
            "claim_id": claim_id,
            "candidates": [candidate.candidate_id],
            "target": {"before_sha": BASE, "after_sha": HEAD},
            "language": "ja",
            "state": state,
        },
        by=CURATOR,
    )
    return claim_id


def permit(ledger: Ledger, subject: str) -> None:
    for scope, runtime in (("local_use", None), ("runtime_presentation", RUNTIME)):
        ledger.append(
            {"op": "permission.set", "subject": subject, "scope": scope, "runtime": runtime, "grant": True},
            by=CURATOR,
        )


def export(ledger: Ledger, claim_id: str, role: str, worker: str) -> tuple[str, str]:
    """1 件の主張の packet を書き出したことにする（実際の書き出しは test_packets.py）。(packet_id, item_id) を返す。"""
    claim = ledger.state().claims[claim_id]
    packet_id, item_id = new_id(), new_id()
    item = {"item_id": item_id, "claim_id": claim_id, "revision": claim.revision, "state_sha256": claim.state_sha256}
    packet = {"op": "packet.export", "packet_id": packet_id, "role": role, "worker": worker, "runtime": RUNTIME}
    ledger.append({**packet, "items": [item]}, by=CURATOR)
    return packet_id, item_id


def answer(ledger: Ledger, exported: tuple[str, str], worker: str, label: Label) -> None:
    packet_id, item_id = exported
    answer: dict[str, object] = {
        "item_id": item_id,
        "label": label,
        "rationale": "同じ SHA で確かめた",
        "evidence_refs": [],
    }
    ledger.append({"op": "answer.import", "packet_id": packet_id, "worker": worker, "answers": [answer]}, by=CURATOR)


def agreeing_gold(ledger: Ledger, claim_id: str, label: Label) -> None:
    """G1・G2 の packet を書き出したことにし、同じラベルの回答を取り込む。"""
    for role, worker in (("g1", "gold-1"), ("g2", "gold-2")):
        answer(ledger, export(ledger, claim_id, role, worker), worker, label)
