""".claude/ project settings のプラグイン／マーケットプレイス整合ガード.

追跡対象の ``.claude/settings.json`` と ``.claude/settings.local.json`` の両方を読む。
``enabledPlugins`` は ``<plugin>@<marketplace>`` 形式でプラグインを参照する。
参照するマーケットプレイスが両ファイルの ``extraKnownMarketplaces`` 合算集合に登録されていない
プラグインは、新規環境では黙ってロードされない（Claude Code v2.1.195 以降の仕様）。
自動登録は対話的な初回起動時にしか行われず、非対話環境やポリシーで
ブロックされたことのある環境では登録が欠けたままになるため、
公式マーケットプレイスを含め全ての登録を要求する。
"""

from __future__ import annotations

import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SETTINGS_PATHS = (
    REPOSITORY_ROOT / ".claude" / "settings.json",
    REPOSITORY_ROOT / ".claude" / "settings.local.json",
)


def missing_marketplaces(paths: tuple[Path, Path]) -> list[str]:
    referenced: set[str] = set()
    registered: set[str] = set()

    for path in paths:
        settings = json.loads(path.read_text(encoding="utf-8"))
        referenced.update(key.rsplit("@", 1)[1] for key in settings.get("enabledPlugins", {}) if "@" in key)
        registered.update(settings.get("extraKnownMarketplaces", {}))

    return sorted(referenced - registered)


def test_every_enabled_plugin_marketplace_is_registered() -> None:
    """登録の無いマーケットプレイスのプラグインを有効にすると、新しい環境の Claude Code がそれを黙って読み込まない。"""
    missing = missing_marketplaces(SETTINGS_PATHS)

    assert not missing, (
        f"enabledPlugins references unregistered marketplaces: {missing}. "
        "Register them in extraKnownMarketplaces so fresh environments can resolve the plugins."
    )
