"""Shared output helpers."""

from tools.agent_runner.adapters.output import embedded_object


def test_embedded_object_rejects_whole_duplicate_answer() -> None:
    """JSON だけの回答で重複キーを採ると、前の指摘が後の空の配列で上書きされて黙って消える。"""
    assert embedded_object('{"findings":[{"id":"消える指摘"}],"findings":[]}') is None
