#!/usr/bin/env zsh
PROVIDERS=("deepinfra")

# PI_CODING_AGENT_DIR が設定されている場合も考慮
PI_DIR="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
MODELS_JSON="${PI_DIR}/models.json"

# モデル定義の正は .devcontainer/pi-models.json とする。
# ここに定義を埋め込むと install_beforehand.bash が配置する内容とずれる。
MODELS_SOURCE="${0:A:h}/../.devcontainer/pi-models.json"

echo "=== Pi + DeepInfra setup ==="

#
# 1. API token check
#
if [[ -z "${DEEPINFRA_TOKEN:-}" ]]; then
  echo "ERROR: DEEPINFRA_TOKEN is not set."
  echo
  echo 'Run:'
  echo '  export DEEPINFRA_TOKEN="your-token"'
  exit 1
fi

echo "DEEPINFRA_TOKEN: SET"

if [[ ! -f "${MODELS_SOURCE}" ]]; then
  echo "ERROR: Model definition source not found: ${MODELS_SOURCE}"
  exit 1
fi

echo "Model source: ${MODELS_SOURCE}"

#
# 2. Pi config directory
#
mkdir -p "${PI_DIR}"

echo "Pi config dir: ${PI_DIR}"

#
# 3. Backup
#
if [[ -f "${MODELS_JSON}" ]]; then
  BACKUP="${MODELS_JSON}.bak.$(date +%Y%m%d-%H%M%S)"
  cp "${MODELS_JSON}" "${BACKUP}"
  echo "Backup: ${BACKUP}"
fi

#
# 4. Add/update the DeepInfra provider
#
# 定義は .devcontainer/pi-models.json から読み取る。
# deepinfra 以外のプロバイダはそのまま保持する。
#
python3 - "${MODELS_JSON}" "${MODELS_SOURCE}" "${PROVIDERS[@]}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = Path(sys.argv[2])
requested = sys.argv[3:]

try:
    source_providers = json.loads(source.read_text()).get("providers", {})
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON in {source}: {e}", file=sys.stderr)
    sys.exit(1)

definitions = {}
for provider in requested:
    definition = source_providers.get(provider)
    if definition is None:
        print(
            f"ERROR: Provider {provider!r} is not defined in {source}",
            file=sys.stderr,
        )
        sys.exit(1)
    definitions[provider] = definition

if path.exists():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)
else:
    data = {}

providers = data.setdefault("providers", {})

for provider, definition in definitions.items():
    providers[provider] = definition

path.write_text(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n"
)

print(f"Updated: {path}")
PY

#
# 5. Validate JSON
#
python3 -m json.tool "${MODELS_JSON}" >/dev/null

echo "models.json JSON validation: OK"

#
# 6. Result
#
echo
echo "=== Setup complete ==="
echo "Providers: ${PROVIDERS[*]}"
echo "Config   : ${MODELS_JSON}"
echo
echo "Verify with:"
echo "  pi --list-models deepinfra"
