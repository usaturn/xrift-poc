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
    missing = missing_marketplaces(SETTINGS_PATHS)

    assert not missing, (
        f"enabledPlugins references unregistered marketplaces: {missing}. "
        "Register them in extraKnownMarketplaces so fresh environments can resolve the plugins."
    )


def write_settings_pair(
    tmp_path: Path,
    main_enabled: dict[str, bool],
    main_known: dict[str, dict[str, str]],
    local_enabled: dict[str, bool],
    local_known: dict[str, dict[str, str]],
) -> tuple[Path, Path]:
    main = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    main.write_text(json.dumps({"enabledPlugins": main_enabled, "extraKnownMarketplaces": main_known}))
    local.write_text(json.dumps({"enabledPlugins": local_enabled, "extraKnownMarketplaces": local_known}))
    return main, local


def test_marketplace_union_and_unknown_are_detected(tmp_path: Path) -> None:
    main, local = write_settings_pair(
        tmp_path,
        main_enabled={"one@first": True},
        main_known={"second": {}},
        local_enabled={"two@second": True, "three@unknown": True},
        local_known={"first": {}},
    )
    assert missing_marketplaces((main, local)) == ["unknown"]
