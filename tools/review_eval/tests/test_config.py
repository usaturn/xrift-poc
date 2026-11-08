import os
from pathlib import Path

import pytest

from tools.review_eval import shim
from tools.review_eval.config import ConfigError, load_config
from tools.test_support.review_eval import make_collection_workspace, write_collection_config


def test_store_inside_a_product_directory_is_rejected(tmp_path: Path) -> None:
    """拒否が外れると、私有の保存先が制作物のリポジトリの中にでき、制作物へ commit・公開されうる。"""
    workspace_root, repos = make_collection_workspace(tmp_path / "ws", ["octo/world"])
    store = workspace_root / "worlds" / "w" / "private"
    config = write_collection_config(tmp_path / "c.toml", store=store, workspace_root=workspace_root, repos=repos)
    with pytest.raises(ConfigError):
        load_config(config)


def test_test_session_starts_with_collection_disabled() -> None:
    """isolation plugin が外れると、テストが利用者の実設定を読み、実の保存先へ fixture の記録を書く。"""
    assert os.environ["REVIEW_EVAL_CONFIG"] == ""
    assert shim.config_path() is None
