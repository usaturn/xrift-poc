from collections.abc import Mapping
from pathlib import Path

import pytest

from tools.review_triage.inputs import load_reviews
from tools.review_triage.models import DEFAULT_MODEL_ID, PreparedState, Repository, ReviewBatch
from tools.review_triage.questions import (
    NotAllowedStateError,
    build_request,
    question_set_for_finding,
    question_set_for_pair,
)
from tools.review_triage.state import prepare_state

SYNTHETIC = Path(__file__).parent / "fixtures" / "input" / "pr-901-synthetic"
REVIEWED_HEAD = "a" * 40
NOTHING_ALLOWED: frozenset[str] = frozenset()
L1 = "alpha-model/active/L-1"
BLOB_KEY = "blob:src/widget.py#L10-L12"
BLOB = "def ratio(total, count):\n    return total / count\n"
TEMP_REVIEW_HEADER = "- 対象: PR #903 / head `aaaaaaaaaaaa` / 2026-09-22\n\n## Low\n\n"
PAIR_DIR = "pr-905-pair"
PAIR_ALPHA = "alpha/active/L-1"
PAIR_BETA = "beta/active/L-1"


def _batch(directory: Path = SYNTHETIC) -> ReviewBatch:
    return load_reviews(directory, repository=Repository.ENV, reviewed_head=REVIEWED_HEAD)


def _low_finding(code: str, title: str, content: str) -> str:
    """箇条がそろった合成の指摘 1 件（`## Low` 節に置く）。content は `- 内容:` の値。"""
    return (
        f"### {code}: {title}\n\n- 位置: `a.py:1`\n- 検出時 HEAD: `aaaaaaaaaaaa`\n"
        f"- 内容: {content}\n- 根拠: 合成データ。\n- 推奨対応: 伏せる。\n"
    )


def _temp_batch(tmp_path: Path, dir_name: str, reviews: Mapping[str, str]) -> ReviewBatch:
    """tmp_path/<dir_name> に `<model>.md` を書いて読み込む。値は `## Low` 節の本文（_low_finding の連結）。"""
    directory = tmp_path / dir_name
    directory.mkdir(parents=True)
    for model, low_section in reviews.items():
        (directory / f"{model}.md").write_text(TEMP_REVIEW_HEADER + low_section, encoding="utf-8")
    return _batch(directory)


def _pair_batch(tmp_path: Path, *, alpha: str, beta: str) -> ReviewBatch:
    """PAIR_DIR に、alpha と beta の 2 モデルが同じ code の L-1 を書いたレビューを作る。引数は `- 内容:` の値。"""
    title = "一時ファイルが残る"
    return _temp_batch(
        tmp_path, PAIR_DIR, {"alpha": _low_finding("L-1", title, alpha), "beta": _low_finding("L-1", title, beta)}
    )


def _state(
    batch: ReviewBatch,
    finding_ids: list[str],
    context: dict[str, str] | None = None,
    allowed: frozenset[str] = NOTHING_ALLOWED,
) -> PreparedState:
    return prepare_state(
        batch, finding_ids=finding_ids, committed_context=context or {}, allowed_source_hashes=allowed
    )


def _approved(batch: ReviewBatch, finding_ids: list[str], context: dict[str, str] | None = None) -> PreparedState:
    """その state 自身の content_sha256 を許可して作り直した state。"""
    draft = _state(batch, finding_ids, context)
    return _state(batch, finding_ids, context, allowed=frozenset({draft.content_sha256}))


def _fake_secrets() -> dict[str, str]:
    """検出器ごとの偽の値。token 形式のリテラルをリポジトリに置かないため、実行時に連結で作る。"""
    return {
        "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----",
        "aws_access_key_id": "key id " + "AKIA" + "ABCDEFGHIJKLMNOP",
        "github_token": "token " + "ghp" + "_" + "a" * 36,
        "slack_token": "xox" + "b-" + "1234567890-abcdef",
        "sk_api_key": "key " + "sk-" + "ant-" + "b" * 24,
        "google_api_key": "AI" + "za" + "c" * 35,
        "jwt": "eyJ" + "d" * 12 + "." + "eyJ" + "e" * 12 + "." + "f" * 12,
        "bearer_token": "Authorization: " + "Bearer " + "g" * 20,
        "assigned_secret": "api_key = " + '"' + "h" * 16 + '"',
    }


def _assert_secret_blocks(secret: str) -> None:
    """evidence に ``secret`` を足した state は許可しても送れず、理由に秘密の値を書かない。"""
    batch = _batch()
    assert _approved(batch, [L1], {BLOB_KEY: BLOB}).allowed
    approved = _approved(batch, [L1], {BLOB_KEY: BLOB + secret + "\n"})
    assert not approved.allowed
    assert all(secret not in reason for reason in approved.blocked_reasons)


def test_approval_is_bound_to_the_exact_state_hash() -> None:
    """許可を本文全体のハッシュ以外（出典ごとのハッシュ・許可の後に変わった本文）で通し、未許可の本文を送る。"""
    batch = _batch()
    key = batch.finding(L1).finding_key
    context = {BLOB_KEY: BLOB}
    draft = _state(batch, [L1], context)
    assert not draft.allowed
    approved = _state(batch, [L1], context, allowed=frozenset({draft.content_sha256}))
    assert (approved.allowed, approved.allowed_by, approved.blocked_reasons) == (True, (draft.content_sha256,), ())
    assert approved.sections == draft.sections
    # 許可集合に他のハッシュが混ざっていても、allowed_by にはこの state の content_sha256 だけが載る
    among_others = _state(batch, [L1], context, allowed=frozenset({"0" * 64, draft.content_sha256, "f" * 64}))
    assert (among_others.allowed, among_others.allowed_by) == (True, (draft.content_sha256,))
    changed = _state(batch, [L1], {BLOB_KEY: BLOB + "# edited\n"}, allowed=frozenset({draft.content_sha256}))
    assert (changed.allowed, changed.allowed_by) == (False, ())
    per_source = frozenset(source.sha256 for source in draft.sources)
    assert not _state(batch, [L1], context, allowed=per_source).allowed
    request = build_request(approved, questions=question_set_for_finding(key), model=DEFAULT_MODEL_ID)
    assert (request.state, request.state_sha256) == (approved.sections, approved.content_sha256)
    with pytest.raises(NotAllowedStateError):
        build_request(draft, questions=question_set_for_finding(key), model=DEFAULT_MODEL_ID)


def test_secret_in_either_pair_body_blocks_the_pair(tmp_path: Path) -> None:
    """重複候補の組の 2 つ目の本文にある秘密を検出せず、組の state を許可だけで送れる状態にする。"""
    token = "ghp" + "_" + "w" * 36
    clean = _pair_batch(tmp_path / "clean", alpha="alpha の本文。", beta="beta の本文。")
    assert _approved(clean, [PAIR_ALPHA, PAIR_BETA]).allowed
    # このディレクトリ名では finding_key の順が beta → alpha なので、alpha の本文は finding_b になる
    batch = _pair_batch(tmp_path / "secret", alpha=f"ログに `{token}` が出る。", beta="beta の本文。")
    approved = _approved(batch, [PAIR_ALPHA, PAIR_BETA])
    assert token in approved.sections["finding_b"]
    assert not approved.allowed
    questions = question_set_for_pair(batch.finding(PAIR_ALPHA).finding_key, batch.finding(PAIR_BETA).finding_key)
    with pytest.raises(NotAllowedStateError):
        build_request(approved, questions=questions, model=DEFAULT_MODEL_ID)


def test_path_escape_keeps_content_out_of_the_state() -> None:
    """リポジトリの外を指すキーの内容を state の本文・出典に入れ、許可待ちの送信候補に並べる。"""
    batch = _batch()
    key = "blob:../outside/secret.txt"
    state = _state(batch, [L1], {key: "LEAKED", BLOB_KEY: BLOB})
    assert all("LEAKED" not in text for text in state.sections.values())
    assert BLOB in state.sections["evidence"]
    assert all("outside" not in source.path for source in state.sources)
    assert "LEAKED" not in state.model_dump_json()
    assert not _state(batch, [L1], {key: "LEAKED", BLOB_KEY: BLOB}, allowed=frozenset({state.content_sha256})).allowed


@pytest.mark.parametrize("detector", sorted(_fake_secrets()))
def test_each_secret_detector_blocks_the_state(detector: str) -> None:
    """検出器ごとの種類の秘密（鍵・トークン・代入形）を含む state を、許可しただけで送れる状態にする。"""
    _assert_secret_blocks(_fake_secrets()[detector])


def test_token_next_to_japanese_text_is_detected() -> None:
    """日本語の文に続けて書かれたトークン（日本語のレビューで起きる形）を秘密として検出しない。"""
    _assert_secret_blocks("値は" + "ghp" + "_" + "k" * 36 + "です")


def test_secret_in_review_text_blocks_the_finding_section(tmp_path: Path) -> None:
    """レビュー本文（finding 節）に貼られたトークンを検出せず、許可だけで送れる状態にする。"""
    token = "ghp" + "_" + "z" * 36
    finding = _low_finding("L-1", "ログに token が出る", f"ログに `{token}` が出力される。")
    batch = _temp_batch(tmp_path, "pr-903-secret", {"m": finding})
    approved = _approved(batch, ["m/active/L-1"], {"blob:a.py": "print(value)\n"})
    assert token in approved.sections["finding"]
    assert not approved.allowed
