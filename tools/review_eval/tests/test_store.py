import multiprocessing
from pathlib import Path

import pytest

from tools.review_eval.models import Observation
from tools.review_eval.store import Store, StoreError
from tools.test_support.review_eval import make_observation, put_revisions_worker

REVISIONS = [
    "# レビュー\n\n指摘 1: 日本語と改行を含む本文\n".encode(),
    "# レビュー\n\n指摘 1 を修正した 2 つ目の revision\n\n- 追加\n".encode(),
]


def _put(store: Store, data: bytes, observed_at: str) -> Observation:
    sha = store.put_blob(data)
    observation = make_observation(sha, len(data), observed_at=observed_at)
    assert store.put_observation(observation)
    return observation


def test_restore_returns_every_revision_after_originals_are_gone(tmp_path: Path) -> None:
    """テストが無いと、上書き後の前の版を復元できない、または復元の bytes が変わるのが黙って進む。"""
    store = Store.init(tmp_path / "store")
    review_dir = tmp_path / "reviews" / "o" / "r" / "#1-b"
    review_dir.mkdir(parents=True)
    original = review_dir / "model.md"
    observed: list[Observation] = []
    for index, data in enumerate(REVISIONS):
        original.write_bytes(data)
        observed.append(_put(store, data, f"2026-10-30T00:00:0{index}Z"))
    original.unlink()
    review_dir.rmdir()

    for index, data in enumerate(REVISIONS):
        dest = tmp_path / f"restored-{index}"
        results = store.restore([observed[index]], dest)
        assert results[0]["status"] == "restored"
        written = dest / "#1-b" / "model.md"
        assert written.read_bytes() == data


def test_concurrent_puts_create_one_observation_per_revision(tmp_path: Path) -> None:
    """テストが無いと、並行の取得で同じ (場所, 内容) が二重に登録されたり欠けたりするのが黙って進む。"""
    store = Store.init(tmp_path / "store")
    revisions = [f"revision {index} 日本語\n".encode() for index in range(3)]
    context = multiprocessing.get_context("spawn")
    with context.Pool(8) as pool:
        results = pool.starmap(put_revisions_worker, [(str(store.root), revisions)] * 8)
    for index in range(3):
        assert sum(1 for created in results if created[index]) == 1
    observations = store.observations()
    assert len(observations) == 3
    assert len(list((store.root / "observations").rglob("*.json"))) == 3
    for observation in observations:
        assert store.read_blob(observation.sha256)
    assert store.verify() == []


def test_existing_blob_with_wrong_bytes_is_not_overwritten(tmp_path: Path) -> None:
    """テストが無いと、壊れた blob を黙って上書きして破損の証拠を失い、別の内容を同じ hash で信じ続ける。"""
    store = Store.init(tmp_path / "store")
    data = REVISIONS[0]
    sha = store.put_blob(data)
    blob = next(path for path in (store.root / "blobs").rglob("*") if path.is_file())
    blob.chmod(0o600)
    blob.write_bytes(b"tampered")
    with pytest.raises(StoreError):
        store.put_blob(data)
    assert blob.read_bytes() == b"tampered"
    assert any(sha in problem for problem in store.verify())
