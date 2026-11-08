"""確認待ち台帳: 確認なしの適格化、露出した系列の held-out、並行の追記の取りこぼしを確かめる。

候補は、収集で観測させた v2 の候補一覧から `sync` で作る（実際の経路）。正解は packet.export と answer.import の
op を台帳へ直接追記して与える（packet の書き出しの経路は test_packets.py）。
"""

import json
import multiprocessing
from pathlib import Path

import pytest

from tools.review_eval.ledger import Ledger, LedgerError
from tools.review_eval.store import Store
from tools.test_support.review_eval import append_times_worker
from tools.test_support.review_eval_ledger import (
    CURATOR,
    agreeing_gold,
    answer,
    claim,
    export,
    permit,
    setup,
)

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")


def test_claim_is_not_eligible_until_claim_and_family_are_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テストが無いと、主張と系列の確認前に適格になり、確認していない正解が評価に混ざる。"""
    ledger = setup(tmp_path, monkeypatch)
    claim_id = claim(ledger, "usaturn/alpha", 5)
    permit(ledger, "repo:usaturn/alpha")
    ledger.append({"op": "split.assign", "claim_id": claim_id, "split": "calibration", "reason": "pilot"}, by=CURATOR)
    with pytest.raises(LedgerError):
        agreeing_gold(ledger, claim_id, "supports")
    assert ledger.state().eligibility(claim_id)[0] is False

    ledger.append({"op": "claim.confirm", "claim_id": claim_id}, by=CURATOR)
    export(ledger, claim_id, "evaluator", "zed")
    with pytest.raises(LedgerError):
        export(ledger, claim_id, "g1", "zed")
    spare = export(ledger, claim_id, "g1", "gold-3")
    agreeing_gold(ledger, claim_id, "supports")
    with pytest.raises(LedgerError):
        answer(ledger, spare, "gold-3", "contradicts")
    state = ledger.state()
    assert state.gold_complete(claim_id) == "supports"
    assert state.eligibility(claim_id)[0] is False

    ledger.append({"op": "family.confirm", "claim_id": claim_id}, by=CURATOR)
    assert ledger.state().eligibility(claim_id) == (True, [])

    other = claim(ledger, "usaturn/alpha", 6)
    ledger.append({"op": "relation.add", "left": claim_id, "right": other, "relation": "near_duplicate"}, by=CURATOR)
    state = ledger.state()
    assert state.family_of(claim_id).members == tuple(sorted((claim_id, other)))
    assert state.family_of(claim_id).confirmed is False
    assert state.eligibility(claim_id)[0] is False


def test_exposed_family_cannot_be_held_out_and_cross_repo_relation_joins_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テストが無いと、露出した系列が held-out のまま残り、調整に使った事例で評価が甘くなる。"""
    ledger = setup(tmp_path, monkeypatch)
    alpha = claim(ledger, "usaturn/alpha", 5)
    beta = claim(ledger, "usaturn/beta", 5)
    assert ledger.state().family_of(alpha).members == (alpha,)
    ledger.append({"op": "relation.add", "left": alpha, "right": beta, "relation": "same_defect"}, by=CURATOR)
    assert ledger.state().family_of(beta).members == tuple(sorted((alpha, beta)))
    ledger.append({"op": "family.confirm", "claim_id": alpha}, by=CURATOR)
    ledger.append({"op": "exposure.add", "claim_id": beta, "who": "tuner", "kind": "pilot"}, by=CURATOR)
    with pytest.raises(LedgerError):
        ledger.append({"op": "split.assign", "claim_id": alpha, "split": "held_out", "reason": "未知"}, by=CURATOR)
    assert ledger.state().family_of(alpha).split is None

    held = claim(ledger, "usaturn/alpha", 6)
    permit(ledger, "repo:usaturn/alpha")
    ledger.append({"op": "claim.confirm", "claim_id": held}, by=CURATOR)
    agreeing_gold(ledger, held, "contradicts")
    ledger.append({"op": "family.confirm", "claim_id": held}, by=CURATOR)
    ledger.append({"op": "split.assign", "claim_id": held, "split": "held_out", "reason": "未知"}, by=CURATOR)
    assert ledger.state().eligibility(held) == (True, [])
    ledger.append({"op": "exposure.add", "claim_id": held, "who": "tuner", "kind": "tuning"}, by=CURATOR)
    state = ledger.state()
    assert state.family_of(held).held_out_invalidated is True
    assert state.eligibility(held)[0] is False


def test_concurrent_appends_keep_every_operation(tmp_path: Path) -> None:
    """テストが無いと、並行の追記で seq が重なり、台帳の操作が黙って失われる。"""
    store = Store.init(tmp_path / "store")
    context = multiprocessing.get_context("spawn")
    with context.Pool(4) as pool:
        results = pool.starmap(append_times_worker, [(str(store.root), f"worker-{n}", 20) for n in range(4)])
    lines = (store.root / "ledger" / "journal.jsonl").read_bytes().splitlines()
    seqs = sorted(json.loads(line)["seq"] for line in lines)
    assert seqs == list(range(1, 81))
    assert sorted(seq for returned in results for seq in returned) == seqs
    assert Ledger(store, lock_timeout=10).state().last_seq == 80
