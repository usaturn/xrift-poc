import json
import secrets
import string
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.review_eval.capture import VirtualRecord, collect
from tools.review_eval.store import Store
from tools.test_support.review_eval import FAKE_SCANNER, make_collection_workspace, write_collection_config

pytest_plugins = ("tools.test_support.plugin",)
pytestmark = pytest.mark.usefixtures("outside_runner")

PARENT = "usaturn/xrift-poc"
HEAD = "9" * 40
BASE = "8" * 40


@dataclass(frozen=True)
class Workspace:
    root: Path
    store: Store
    review_dir: Path


def _workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    products: tuple[str, ...] = (),
    init_store: bool = True,
    scanner: Sequence[str] | None = FAKE_SCANNER,
    max_store_bytes: int = 64 * 1024 * 1024,
) -> Workspace:
    root, repos = make_collection_workspace(tmp_path / "base", products)
    review_dir = root / "reviews" / "usaturn" / "xrift-poc" / "#5-feat"
    review_dir.mkdir(parents=True)
    store_root = tmp_path / "store"
    store = Store.init(store_root) if init_store else Store(store_root)
    path = write_collection_config(
        tmp_path / "review-eval.toml",
        store=store_root,
        workspace_root=root,
        repos=repos,
        scanner=scanner,
        max_store_bytes=max_store_bytes,
    )
    monkeypatch.setenv("REVIEW_EVAL_CONFIG", str(path))
    return Workspace(root=root, store=store, review_dir=review_dir)


def _candidates(repository: str, number: int) -> bytes:
    document = {
        "schema": "review-findings/v2",
        "kind": "candidates",
        "source": "reviewer",
        "pr": {"repository": repository, "number": number},
        "reviewed_head": HEAD,
        "base": BASE,
        "findings": [{"id": "M-1", "title": "日時を UTC として読む\n2 つ目の主張: 書き出しも同じ"}],
        "unverified_candidates": [],
    }
    return json.dumps(document, ensure_ascii=False, indent=2).encode()


def _review_markdown(body: str) -> bytes:
    return f"# レビュー\n\n## Medium\n\n### M-1 指摘\n\n{body}\n".encode()


def test_document_for_same_number_in_another_repo_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別 repo の同じ番号の文書が混ざっても、検出されないまま私有の保存先に入る。"""
    ws = _workspace(tmp_path, monkeypatch, products=("usaturn/beta",))
    findings = ws.review_dir / "findings" / "m-900000000000.json"
    findings.parent.mkdir()
    findings.write_bytes(_candidates("usaturn/beta", 5))

    report = collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[findings])

    assert ws.store.observations() == []
    assert "identity_mismatch" in report.failures
    (failure_id,) = [
        i for i, failure, pending in ws.store.failures() if failure.kind == "identity_mismatch" and pending
    ]
    ws.store.ack_failure(failure_id, by="test", reason="確認済み")
    time.sleep(1.1)  # failure の id は記録の時刻（秒）を含むので、秒が変わってから取り直す
    collect(route="pr-review", event="prepare", review_dir=ws.review_dir)
    assert ws.store.observations() == []
    assert [pending for _, failure, pending in ws.store.failures() if failure.kind == "identity_mismatch"] == [False]


def test_symlink_leading_outside_review_dir_is_not_captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """symlink をたどると、review_dir の外のファイルが黙って私有の保存先に入る。"""
    ws = _workspace(tmp_path, monkeypatch)
    outside = tmp_path / "outside.json"
    outside.write_bytes(_candidates(PARENT, 5))
    link = ws.review_dir / "findings" / "m-900000000000.json"
    link.parent.mkdir()
    link.symlink_to(outside)
    review = ws.review_dir / "m.md"
    review.write_bytes(_review_markdown("同じディレクトリの通常のファイル"))

    collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[link])
    collect(route="pr-review", event="prepare", review_dir=ws.review_dir)

    assert [o.path for o in ws.store.observations()] == [review.name]


def test_secret_body_is_withheld_and_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """秘密を含む本文が、黙って保存先・標準出力・標準エラーに出る。"""
    ws = _workspace(tmp_path, monkeypatch, scanner=None)
    alphabet = string.ascii_letters + string.digits
    token = "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))
    review = ws.review_dir / "claude-opus-5-5.md"
    review.write_bytes(_review_markdown(f"設定ファイルにトークンが残っている:\n\n    token = {token}\n"))
    unknown = ws.review_dir / "findings" / "m-900000000000.json"
    unknown.parent.mkdir()
    unknown.write_text(json.dumps({"schema": "x", "kind": f"token = {token}"}), encoding="utf-8")

    collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[review, unknown])

    observations = [o for o in ws.store.observations() if o.path == review.name]
    assert [o.body for o in observations] == ["withheld_secret"]
    assert [o.body for o in ws.store.observations() if o.path == "findings/m-900000000000.json"] == ["withheld_secret"]
    assert not ws.store.has_blob(observations[0].sha256)
    assert any(failure.kind == "secret_withheld" and pending for _, failure, pending in ws.store.failures())
    captured = capsys.readouterr()
    assert token not in captured.err and token not in captured.out
    for path in ws.store.root.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path


def test_collect_failure_is_reported_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """収集の失敗が例外になり、保存点の helper の結果を変える。"""
    ws = _workspace(tmp_path, monkeypatch, init_store=False)
    review = ws.review_dir / "claude-opus-5-5.md"
    review.write_bytes(_review_markdown("本文"))

    report = collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[review])

    assert report.status == "store_not_initialized"
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("review-eval: ")


def test_runner_child_never_touches_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runner の子が保存先に書くと、並行する司令塔の収集と競合し、記録が黙って欠ける。"""
    ws = _workspace(tmp_path, monkeypatch)
    review = ws.review_dir / "claude-opus-5-5.md"
    review.write_bytes(_review_markdown("本文"))
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", "1")

    report = collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[review])

    assert report.status == "disabled"
    assert ws.store.observations() == []
    assert ws.store.failures() == []
    monkeypatch.delenv("AGENT_RUNNER_DEPTH")
    collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[review])
    assert len(ws.store.observations()) == 1


def test_capacity_limit_holds_without_deleting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """容量超過で既存の版が黙って消える、または超過分が黙って落ちる。"""
    first_data = _review_markdown("1 つ目のレビュー。" + "あ" * 40)
    second_data = _review_markdown("2 つ目のレビュー。" + "い" * 40)
    ws = _workspace(tmp_path, monkeypatch, max_store_bytes=len(first_data) + len(second_data) // 2)
    first = ws.review_dir / "model-a.md"
    first.write_bytes(first_data)
    collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[first])
    (first_observation,) = ws.store.observations()
    assert first_observation.body == "stored"

    second = ws.review_dir / "model-b.md"
    second.write_bytes(second_data)
    report = collect(route="pr-review", event="complete", review_dir=ws.review_dir, files=[second])

    assert "capacity_held" in report.failures
    assert ws.store.read_blob(first_observation.sha256) == first_data
    second_observations = [o for o in ws.store.observations() if o.path == second.name]
    assert all(o.body != "stored" for o in second_observations)
    assert not any(ws.store.has_blob(o.sha256) for o in second_observations)
    assert ws.store.blob_bytes_total() == len(first_data)


def test_sweep_keeps_previous_revision_before_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """上書きの前の版が黙って失われ、後から評価に使えない。"""
    ws = _workspace(tmp_path, monkeypatch)
    review = ws.review_dir / "claude-opus-5-5.md"
    revisions = [_review_markdown("1 回目の本文\n複数行"), _review_markdown("2 回目の本文（同じ HEAD の再レビュー）")]

    for data in revisions:
        review.write_bytes(data)
        collect(route="pr-review", event="prepare", review_dir=ws.review_dir, files=[])

    observations = [o for o in ws.store.observations() if o.path == review.name]
    assert len({o.sha256 for o in observations}) == len(observations) == 2
    restored: list[bytes] = []
    for index, observation in enumerate(observations):
        dest = tmp_path / f"restored-{index}"
        (result,) = ws.store.restore([observation], dest)
        assert result["status"] == "restored"
        restored.append((dest / result["path"]).read_bytes())
    assert sorted(restored) == sorted(revisions)


def test_record_held_by_failed_scan_is_stored_once_the_scanner_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scanner の失敗で保留した記録が、回復後も黙って保存されないままになる。"""
    broken = [sys.executable, "-c", "raise SystemExit(3)", "{source}", "{report}"]
    ws = _workspace(tmp_path, monkeypatch, scanner=broken)
    record = VirtualRecord("@pr-comment/round-1/record.json", b'{"round": 1}\n')

    def upsert() -> None:
        collect(
            route="review-fix-planning",
            event="upsert",
            review_dir=None,
            records=[record],
            repository=PARENT,
            pr=5,
        )

    upsert()
    write_collection_config(tmp_path / "review-eval.toml", store=ws.store.root, workspace_root=ws.root, repos=[PARENT])
    upsert()

    (observation,) = [o for o in ws.store.observations() if o.path == record.path]
    assert observation.body == "stored"
    assert ws.store.read_blob(observation.sha256) == record.data
    assert [pending for _, failure, pending in ws.store.failures() if failure.kind == "scan_failed"] == [False]


def test_repeated_sweeps_reach_every_file_beyond_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_sweep_files を超えるファイルが、繰り返しの sweep でも黙って永久に取られない。"""
    ws = _workspace(tmp_path, monkeypatch)
    write_collection_config(
        tmp_path / "review-eval.toml",
        store=ws.store.root,
        workspace_root=ws.root,
        repos=[PARENT],
        max_sweep_files=1,
    )
    names = [f"model-{index}.md" for index in range(3)]
    for name in names:
        (ws.review_dir / name).write_bytes(_review_markdown(name))

    for _ in names:
        collect(route="pr-review", event="prepare", review_dir=ws.review_dir)

    assert sorted(o.path for o in ws.store.observations()) == names


def test_logs_and_unlisted_files_in_review_dir_are_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """許可パターンが広がると、review_dir の生ログ・runner の応答が私有の保存先へ入る。"""
    ws = _workspace(tmp_path, monkeypatch)
    (ws.review_dir / "runs").mkdir()
    (ws.review_dir / "runs" / "x.log").write_bytes(b"conversation log\n")
    (ws.review_dir / "response.txt").write_bytes(b"runner response\n")
    (ws.review_dir / "findings").mkdir()
    findings = ws.review_dir / "findings" / "m-900000000000.json"
    findings.write_bytes(_candidates(PARENT, 5))

    collect(route="pr-review", event="prepare", review_dir=ws.review_dir)

    assert [o.path for o in ws.store.observations()] == ["findings/m-900000000000.json"]
