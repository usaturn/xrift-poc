#!/usr/bin/env zsh

set -euo pipefail

# DeepInfra の定義を明示更新する。利用者追加モデル・独自フィールドは保持し、
# リポジトリで定義している同一 ID のモデルだけを置き換える。
# 認証は保存せず、ランチャー・共通ヘルパ・未配置の tier 設定も配置する。
readonly DEVCONTAINER_DIR="${0:A:h}/../.devcontainer"
readonly MODELS_SOURCE="${DEVCONTAINER_DIR}/pi-models.json"
readonly BIN_DIR="${HOME}/.local/bin"

typeset PI_DIR="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
if [[ "${PI_DIR}" == "~" ]]; then
    PI_DIR="${HOME}"
elif [[ "${PI_DIR}" == "~/"* ]]; then
    PI_DIR="${HOME}/${PI_DIR#'~/'}"
fi
readonly MODELS_JSON="${PI_DIR}/models.json"

print -r -- "=== Pi + DeepInfra setup ==="
if [[ -z "${DEEPINFRA_TOKEN:-}" ]]; then
    print -ru2 -- "ERROR: DEEPINFRA_TOKEN is not set."
    print -ru2 -- '利用者自身でトークンを用意し、export DEEPINFRA_TOKEN="..." してから再実行してください。'
    exit 1
fi

# パーサーやファイル操作の例外には入力値・秘密が含まれ得る。内容は表示せず、
# 失敗した場合は配置へ進まない。書き出し前に検証とバックアップを完了する。
typeset backup_timestamp
backup_timestamp="$(date +%Y%m%d-%H%M%S)"
python3 - "${MODELS_JSON}" "${MODELS_SOURCE}" "${backup_timestamp}" <<'PY' || exit 1
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, cast


def object_fields(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError()
    return cast(dict[str, Any], value)


staging = None
backup = None
try:
    path = Path(sys.argv[1]).resolve()
    source = json.loads(Path(sys.argv[2]).read_text())
    definition = source["providers"]["deepinfra"]
    original = path.read_bytes() if path.exists() else None
    data = object_fields(json.loads(original) if original is not None else {})
    providers = object_fields(data.setdefault("providers", {}))
    existing = object_fields(providers.get("deepinfra", {}))
    raw_models = existing.get("models", [])
    if not isinstance(raw_models, list):
        raise ValueError()
    models = [object_fields(model) for model in cast(list[object], raw_models)]
    if any(not isinstance(model.get("id"), str) for model in models):
        raise ValueError()

    replacements = {model["id"]: model for model in definition["models"]}
    merged = [replacements.get(model["id"], model) for model in models]
    present = {model["id"] for model in models}
    merged.extend(model for id_, model in replacements.items() if id_ not in present)
    providers["deepinfra"] = {**existing, **definition, "models": merged}
    updated = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    if original is not None:
        # mkstemp は同じ秒の再実行でも既存世代を上書きしない。秘密を含み得るので
        # バックアップも新規ファイルも、作成時は本人だけが読める権限にする。
        prefix = path.name + ".bak." + sys.argv[3] + "."
        fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
        backup = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
    fd, name = tempfile.mkstemp(prefix=".models-", dir=path.parent)
    staging = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(updated)
    if original is not None:
        staging.chmod(stat.S_IMODE(path.stat().st_mode))
    staging.replace(path)
except Exception:
    # JSON の内容・例外の入力値・トークンは出さない。
    print("ERROR: models.json の読み取り・更新に失敗しました。入力と書き込み権限を確認してください。", file=sys.stderr)
    sys.exit(1)
finally:
    if staging is not None:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            print("ERROR: 更新用の一時ファイルを片付けられません。書き込み権限を確認してください。", file=sys.stderr)
            sys.exit(1)

if backup is not None:
    print(f"Backup: {backup}")
print(f"Updated: {path}")
PY

mkdir -p "${BIN_DIR}"
install -m 0755 "${DEVCONTAINER_DIR}/pidid" "${BIN_DIR}/pidid"
# 拡張の読み先は HOME 固定。PI_CODING_AGENT_DIR があっても tier は移さない。
zsh "${0:A:h}/setup_pi_superagents.zsh" --if-absent

print -r -- ""
print -r -- "=== Setup complete ==="
print -r -- "Config: ${MODELS_JSON}"
print -r -- "Launcher: ${BIN_DIR}/pidid"
print -r -- "確認: pi --list-models deepinfra"
print -r -- "起動: pidid（DEEPINFRA_TOKEN を export したシェルで実行）"
