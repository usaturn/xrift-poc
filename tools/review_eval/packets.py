"""packet の書き出しと回答の取り込み（Spec 5.1）。

検査（確認済み・許可・担当の除外・1 役割 1 回答・stale）は `Ledger.append` が lock の中で行う。ここでは
packet.json の中身の組み立て、入出力ファイルの形の検査、ファイルと journal の書き込み順だけを担当する。
"""

import json
import os
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.review_eval.canonical import hfile
from tools.review_eval.ledger import LABELS, Ledger, LedgerError, new_id
from tools.review_eval.ledger_ops import GOLD_ROLES, Answer, Role
from tools.review_eval.ledger_state import Claim, LedgerState

PACKET_SCHEMA = "review-eval-packet/v1"
ANSWERS_SCHEMA = "review-eval-answers/v1"
PACKET_FILE = "packet.json"
_TEMP_FILE = ".packet.json.tmp"


class AnswersFile(BaseModel):
    """`review-eval-answers/v1`。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_: Literal["review-eval-answers/v1"] = Field(alias="schema")
    packet_id: str
    worker: str
    answers: list[Answer] = Field(min_length=1)


def _item(state: LedgerState, claim: Claim, role: Role, item_id: str) -> dict[str, object]:
    """packet の item。主張 ID・候補・担当名は入れない。target は正解担当だけ、G1/G2 の回答は adjudicator だけ。"""
    item: dict[str, object] = {"item_id": item_id, "state": claim.state.model_dump(mode="json")}
    if role in GOLD_ROLES:
        item["target"] = {"repository": claim.repository, "before_sha": claim.before_sha, "after_sha": claim.after_sha}
    if role == "adjudicator":
        item["gold"] = {
            answer.role: {"label": answer.label, "rationale": answer.rationale}
            for answer in state.answers
            if answer.claim_id == claim.claim_id and answer.revision == claim.revision and answer.role in ("g1", "g2")
        }
    return item


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def export_packet(
    ledger: Ledger,
    *,
    role: Role,
    worker: str,
    runtime: str,
    claim_ids: Sequence[str],
    out_dir: Path,
    by: str,
) -> Path:
    """`out_dir`（存在しないパス）に packet.json を書き、journal に packet.export を追記して、そのパスを返す。

    書き込み順: ① out_dir（0700）と一時名の packet（0600）を作る → ② journal へ追記（検査はここで行う。拒否されたら
    一時名の packet と out_dir を消して LedgerError）→ ③ 一時名を packet.json へ rename。
    journal より先に packet.json が現れることは無い。③ が失敗した場合だけ、journal に記録があり packet.json が無い
    状態になるので、その旨と一時名のパスを LedgerError に書く（packet.json は一時名から rename すれば得られる）。
    """
    state = ledger.state()
    claims = [state.claim(claim_id) for claim_id in claim_ids]
    if not claims:
        raise LedgerError("主張を 1 件以上指定する")
    random.SystemRandom().shuffle(claims)
    packet_id = new_id()
    items = [(new_id(), claim) for claim in claims]
    document = {
        "schema": PACKET_SCHEMA,
        "packet_id": packet_id,
        "role": role,
        "labels": list(LABELS),
        "items": [_item(state, claim, role, item_id) for item_id, claim in items],
    }
    try:
        out_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise LedgerError(f"出力先を新しく作れない: {out_dir}（{type(exc).__name__}）") from None
    temp = out_dir / _TEMP_FILE
    try:
        out_dir.chmod(0o700)
        _write_private(temp, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        ledger.append(
            {
                "op": "packet.export",
                "packet_id": packet_id,
                "role": role,
                "worker": worker,
                "runtime": runtime,
                "items": [
                    {
                        "item_id": item_id,
                        "claim_id": claim.claim_id,
                        "revision": claim.revision,
                        "state_sha256": claim.state_sha256,
                    }
                    for item_id, claim in items
                ],
            },
            by=by,
        )
    except BaseException as exc:
        temp.unlink(missing_ok=True)
        try:
            out_dir.rmdir()
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise LedgerError(f"packet を書き出せない: {type(exc).__name__}") from None
        raise
    path = out_dir / PACKET_FILE
    try:
        temp.rename(path)
    except OSError as exc:
        raise LedgerError(
            f"journal に packet.export（{packet_id}）を記録したが packet.json に rename できなかった"
            f"（{type(exc).__name__}）。{temp} を {path} へ移す"
        ) from None
    return path


def import_answers(ledger: Ledger, answers_file: Path, *, by: str) -> int:
    """回答ファイルの全件を `answer.import` として追記する（全件が通るか、1 件も書かないか）。取り込んだ件数を返す。

    item_id から主張への対応は journal の packet.export にだけある。stale・担当・重複の検査は `append` が行う。
    """
    try:
        data = answers_file.read_bytes()
    except OSError as exc:
        raise LedgerError(f"回答ファイルを読めない（{type(exc).__name__}）") from None
    try:
        parsed = AnswersFile.model_validate_json(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise LedgerError(f"回答ファイルの形が不正: {details}") from None
    ledger.append(
        {
            "op": "answer.import",
            "packet_id": parsed.packet_id,
            "worker": parsed.worker,
            "answers": [answer.model_dump(mode="json") for answer in parsed.answers],
            "answers_sha256": hfile(data),
        },
        by=by,
    )
    return len(parsed.answers)
