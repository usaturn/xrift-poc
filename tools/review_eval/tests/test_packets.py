"""packet の書き出しと回答の取り込み: 管理用情報・正解の混入、正解担当の evaluator 化、stale な回答を確かめる。"""

import json
from pathlib import Path

import pytest

from tools.review_eval.ledger import Ledger, LedgerError
from tools.review_eval.ledger_ops import Role
from tools.review_eval.packets import export_packet, import_answers
from tools.test_support.review_eval_ledger import CURATOR, HEAD, claim, permit, setup

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")

RUNTIME = "claude-code"
IMPORTER = "importer"
RATIONALE = "diff の該当行を同じ SHA で読んで確かめた"


def _confirmed_claim(ledger: Ledger) -> str:
    claim_id = claim(ledger, "usaturn/alpha", 5)
    permit(ledger, "repo:usaturn/alpha")
    ledger.append({"op": "claim.confirm", "claim_id": claim_id}, by=CURATOR)
    ledger.append({"op": "split.assign", "claim_id": claim_id, "split": "calibration", "reason": "較正"}, by=CURATOR)
    return claim_id


def _export(ledger: Ledger, tmp_path: Path, role: Role, worker: str, claim_id: str, name: str) -> Path:
    return export_packet(
        ledger, role=role, worker=worker, runtime=RUNTIME, claim_ids=[claim_id], out_dir=tmp_path / name, by=CURATOR
    )


def _answer_file(packet: Path, worker: str, label: str, destination: Path) -> Path:
    document = json.loads(packet.read_text(encoding="utf-8"))
    answers: list[dict[str, object]] = [
        {"item_id": item["item_id"], "label": label, "rationale": RATIONALE, "evidence_refs": []}
        for item in document["items"]
    ]
    destination.write_text(
        json.dumps(
            {
                "schema": "review-eval-answers/v1",
                "packet_id": document["packet_id"],
                "worker": worker,
                "answers": answers,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return destination


def test_evaluator_packet_hides_management_data_and_rejects_gold_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テストが無いと、evaluator の packet に正解・管理用情報が混ざり、予測が正解を見て作られる。"""
    ledger = setup(tmp_path, monkeypatch)
    claim_id = _confirmed_claim(ledger)
    (candidate_id,) = ledger.state().claims[claim_id].candidates
    ledger.append({"op": "family.confirm", "claim_id": claim_id}, by=CURATOR)
    gold: tuple[tuple[Role, str], ...] = (("g1", "gold-1"), ("g2", "gold-2"))
    for role, worker in gold:
        packet = _export(ledger, tmp_path, role, worker, claim_id, f"packet-{role}")
        target = json.loads(packet.read_text(encoding="utf-8"))["items"][0]["target"]
        assert (target["repository"], target["after_sha"]) == ("usaturn/alpha", HEAD)
        answers = _answer_file(packet, worker, "supports", tmp_path / f"answers-{role}.json")
        assert import_answers(ledger, answers, by=IMPORTER) == 1

    evaluator = _export(ledger, tmp_path, "evaluator", "eval-1", claim_id, "packet-e")
    text = evaluator.read_text(encoding="utf-8")
    document = json.loads(text)
    assert [set(item) for item in document["items"]] == [{"item_id", "state"}]
    for hidden in (claim_id, candidate_id, "usaturn", "alpha", HEAD, RATIONALE, "gold-1", "gold-2", CURATOR, IMPORTER):
        assert hidden not in text

    for worker in ("gold-1", IMPORTER):
        with pytest.raises(LedgerError):
            _export(ledger, tmp_path, "evaluator", worker, claim_id, f"rejected-{worker}")
        assert not (tmp_path / f"rejected-{worker}").exists()


def test_answer_for_revised_claim_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """テストが無いと、改訂前の主張への回答が取り込まれ、古い版の正解が黙って使われる。"""
    ledger = setup(tmp_path, monkeypatch)
    claim_id = _confirmed_claim(ledger)
    packet = _export(ledger, tmp_path, "g1", "gold-1", claim_id, "packet-g1")
    answers = _answer_file(packet, "gold-1", "supports", tmp_path / "answers.json")
    state = ledger.state().claims[claim_id].state.model_dump(mode="json")
    ledger.append(
        {"op": "claim.revise", "claim_id": claim_id, "state": {**state, "claim": "改訂した主張"}}, by=CURATOR
    )
    before = ledger.state().last_seq
    with pytest.raises(LedgerError):
        import_answers(ledger, answers, by=IMPORTER)
    after = ledger.state()
    assert after.last_seq == before
    assert after.answers == []


def test_evaluator_answer_is_rejected_after_its_worker_imported_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テストが無いと、正解を見た担当の予測が取り込まれ、評価が正解の漏えいで汚れる。"""
    ledger = setup(tmp_path, monkeypatch)
    claim_id = _confirmed_claim(ledger)
    evaluator = _export(ledger, tmp_path, "evaluator", "eval-1", claim_id, "packet-e")
    gold = _export(ledger, tmp_path, "g1", "gold-1", claim_id, "packet-g1")
    import_answers(ledger, _answer_file(gold, "gold-1", "supports", tmp_path / "answers-g1.json"), by="eval-1")
    before = ledger.state().last_seq

    with pytest.raises(LedgerError):
        import_answers(ledger, _answer_file(evaluator, "eval-1", "supports", tmp_path / "answers-e.json"), by=IMPORTER)

    assert ledger.state().last_seq == before
