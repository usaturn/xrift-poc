#!/usr/bin/env zsh

set -euo pipefail

#
# Pi + Command Code GOAT (Muse Spark 1.3 / DeepSeek V4.1 Flash)
#
# Idempotent setup script
#
# Creates:
#   ~/.pi/agent/models.json   Command Code provider/model definition
#   ~/.local/bin/piccg        Pi launcher (Muse Spark 1.3)
#   ~/.local/bin/piccd        Pi launcher (DeepSeek V4.1 Flash)
#
# commandcode は Pi の built-in provider ではない（opencode-go と違う）
# ため、auth.json は使わない。認証は models.json の apiKey フィールド
# （"$COMMANDCODE_API_KEY"）が実行時に環境変数を参照して解決する。
# API キーのローテーションでこのスクリプトを実行し直す必要はない。
#
# モデル定義の元データは .devcontainer/pi-models.json である。
#
# piccg と piccd は同じ commandcode プロバイダを共有する。プロバイダ定義の
# 書き手を 1 本に保つため、両ランチャーをこのスクリプトが受け持つ。分けると
# 2 本が providers.commandcode を全置換し合い、片方だけ実行した時点で
# 他方のモデルが models.json から消える。
#
# Running this script is NOT required after a container rebuild:
# ~/.pi is bind mounted and .devcontainer/install_beforehand.bash
# places piccg and piccd. Use it when the host side ~/.pi has to be
# rebuilt from scratch, or when the model definition in the repository
# changed and the existing models.json needs to converge on it.
#

readonly PROVIDER="commandcode"
readonly THINKING="max"

#
# models.json の providers.commandcode に載るべきモデル全部。ランチャごとの
# 既定モデルと --models の CSV は下の *_DEFAULT_MODEL / *_MODELS_CSV が持つ。
#
readonly MODELS=(
    "meta/muse-spark-1.3"
    "meta/muse-spark-1.3-contributor"
    "deepseek/deepseek-v4.1-flash"
)

readonly PICCG_DEFAULT_MODEL="meta/muse-spark-1.3"
readonly PICCG_MODELS_CSV="meta/muse-spark-1.3,meta/muse-spark-1.3-contributor"

readonly PICCD_DEFAULT_MODEL="deepseek/deepseek-v4.1-flash"
readonly PICCD_MODELS_CSV="deepseek/deepseek-v4.1-flash"

readonly PI_DIR="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
readonly MODELS_JSON="${PI_DIR}/models.json"

#
# The model definition and the launchers are kept in the repository so
# that install_beforehand.bash and this script cannot drift apart.
#
readonly DEVCONTAINER_DIR="${0:A:h}/../.devcontainer"
readonly MODELS_SOURCE="${DEVCONTAINER_DIR}/pi-models.json"

readonly BIN_DIR="${HOME}/.local/bin"


log() {
    print -r -- "[setup_pi_commandcode_goat] $*"
}

warn() {
    print -ru2 -- "[setup_pi_commandcode_goat] WARNING: $*"
}

die() {
    print -ru2 -- "[setup_pi_commandcode_goat] ERROR: $*"
    exit 1
}


#
# ----------------------------------------------------------------------
# 0. Preconditions
# ----------------------------------------------------------------------
#

command -v python3 >/dev/null 2>&1 ||
    die "python3 is required."

command -v pi >/dev/null 2>&1 ||
    die "pi is not installed or is not in PATH."

[[ -f "${MODELS_SOURCE}" ]] ||
    die "Model definition source not found: ${MODELS_SOURCE}"

#
# The launchers are installed verbatim from the repository, so the models
# this script validates must be the ones the launchers actually run.
#
check_launcher_source() {
    local name="$1"
    local default_model="$2"
    local models_csv="$3"
    local source="${DEVCONTAINER_DIR}/${name}"
    local model

    [[ -f "${source}" ]] ||
        die "Launcher source not found: ${source}"

    grep -Fq -- "--provider ${PROVIDER}" "${source}" ||
        die "${source} does not use --provider ${PROVIDER}."

    grep -Fq -- "--model ${default_model}" "${source}" ||
        die "${source} does not use --model ${default_model}."

    for model in "${(@s:,:)models_csv}"; do
        grep -Fq -- "${model}" "${source}" ||
            die "${source} does not mention ${model}."
    done
}

check_launcher_source piccg "${PICCG_DEFAULT_MODEL}" "${PICCG_MODELS_CSV}"
check_launcher_source piccd "${PICCD_DEFAULT_MODEL}" "${PICCD_MODELS_CSV}"

mkdir -p "${PI_DIR}"
mkdir -p "${BIN_DIR}"

log "Pi executable : $(command -v pi)"
log "Pi config dir : ${PI_DIR}"
log "Model source  : ${MODELS_SOURCE}"
log "Launchers     : ${BIN_DIR}/{piccg,piccd}"


#
# ----------------------------------------------------------------------
# 1. Check COMMANDCODE_API_KEY (informational)
#
# piccg / piccd の実行時には COMMANDCODE_API_KEY が必須だが、このスクリプト
# 自体（models.json upsert と pi --list-models 検証）はキーを必要と
# しない。未設定でもセットアップは完了できるので、警告にとどめる。
# ----------------------------------------------------------------------
#

if [[ -n "${COMMANDCODE_API_KEY:-}" ]]; then
    log "Command Code API key found in environment."
else
    warn "COMMANDCODE_API_KEY is not set in the environment."
    warn "piccg and piccd require it at runtime:"
    warn '  export COMMANDCODE_API_KEY="..."'
fi


#
# ----------------------------------------------------------------------
# 2. Apply the Command Code model definition
#
# commandcode は Pi の built-in catalog に存在しないため、
# models.json で provider を完全定義する（deepinfra と同じ方式）。
#
# The definition is read from .devcontainer/pi-models.json and the
# whole providers.commandcode entry is replaced, so that re-running
# this script always converges on the repository definition.
#
# Providers other than commandcode are preserved untouched.
# ----------------------------------------------------------------------
#

python3 - "${MODELS_JSON}" "${MODELS_SOURCE}" "${PROVIDER}" <<'PY'
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

path = Path(sys.argv[1])
source = Path(sys.argv[2])
provider = sys.argv[3]

try:
    definition = json.loads(source.read_text()).get("providers", {}).get(provider)
except json.JSONDecodeError as exc:
    print(f"ERROR: Invalid JSON in {source}: {exc}", file=sys.stderr)
    sys.exit(1)

if definition is None:
    print(
        f"ERROR: Provider {provider!r} is not defined in {source}",
        file=sys.stderr,
    )
    sys.exit(1)

if path.exists():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"ERROR: {path} must contain a JSON object.", file=sys.stderr)
        sys.exit(1)
else:
    data = {}

if "providers" not in data:
    providers = data["providers"] = {}
else:
    providers = data["providers"]
    if not isinstance(providers, dict):
        print(f"ERROR: {path} 'providers' must be an object.", file=sys.stderr)
        sys.exit(1)

if providers.get(provider) == definition:
    print("already up to date")
    sys.exit(0)

if path.exists():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + f".bak.{timestamp}")
    shutil.copy2(path, backup)
    print(f"backup:{backup}")

providers[provider] = definition

tmp = path.with_name(path.name + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, path)

print("updated")
PY

log "models.json: ${PROVIDER} definition applied."


#
# ----------------------------------------------------------------------
# 3. Install ~/.local/bin/piccg and ~/.local/bin/piccd
#
# The launchers are copied verbatim from .devcontainer/, which are the
# same files .devcontainer/install_beforehand.bash places on rebuild.
# Generating them here instead would let the two drift apart.
#
# ~/.local/bin is already added to PATH by .devcontainer/zshrc.txt, so
# this script does not touch ~/.zshrc.
# ----------------------------------------------------------------------
#

install_launcher() {
    local name="$1"
    local source="${DEVCONTAINER_DIR}/${name}"
    local launcher="${BIN_DIR}/${name}"

    if [[ -f "${launcher}" ]] && cmp -s "${source}" "${launcher}"; then
        log "${name} launcher: already up to date."
    else
        install -m 0755 "${source}" "${launcher}"
        log "${name} launcher: installed."
    fi
}

install_launcher piccg
install_launcher piccd


#
# ----------------------------------------------------------------------
# 4. Validate provider/model availability
#
# Pi の built-in catalog 更新（pi update --models）は commandcode に
# は無関係なので実行しない。models.json の定義だけで解決できるはず。
#
# 検証はキーの有無で二分する。理由は次の 2 点である。
#
#   1. authHeader: true の provider は apiKey が解決できないと
#      `pi --list-models` の一覧に一切現れない。キー未設定を許容する
#      このスクリプトでは、pi による解決検証はそのままでは成立しない
#   2. pi はマッチが無いとき `No models matching "<id>"` と出力する。
#      この失敗メッセージ自身がモデル ID を含むため、出力に対する素の
#      `grep -F "${MODEL}"` は provider が未定義でも必ず成立してしまう
#      （= 検証が常に成功する偽陽性になる）
#
# よってキーがあるときだけ pi に解決させ、その場合も provider 名込みの
# 行一致で判定する。キーが無いときは models.json の内容を直接検証する。
# ----------------------------------------------------------------------
#

if [[ -n "${COMMANDCODE_API_KEY:-}" ]]; then
    log "Checking Command Code model availability..."

    MODEL_LIST="$(
        pi --list-models "${PROVIDER}" 2>&1 || true
    )"

    #
    # 一覧は `<provider> <model> <context> ...` の行で出る。provider 名から
    # 始まる行に限定することで、上記 2. の失敗メッセージにはマッチしない。
    # MODELS の全件が解決できることを確認する。
    #
    for _model in "${MODELS[@]}"; do
        if ! print -r -- "${MODEL_LIST}" |
            grep -Eq "^[[:space:]]*${PROVIDER}[[:space:]]+${_model}([[:space:]]|$)"; then
            print -ru2 -- ""
            print -ru2 -- "Command Code model was not found."
            print -ru2 -- ""
            print -ru2 -- "Expected:"
            print -ru2 -- "  ${PROVIDER}/${_model}"
            print -ru2 -- ""
            print -ru2 -- "Pi output:"
            print -ru2 -- "${MODEL_LIST}"
            print -ru2 -- ""
            print -ru2 -- "provider 定義はこのスクリプトが適用済みです:"
            print -ru2 -- "  ${MODELS_JSON}"
            print -ru2 -- ""
            print -ru2 -- "COMMANDCODE_API_KEY の値が有効か確認してください。"
            print -ru2 -- "authHeader: true の provider は、キーが解決できないと"
            print -ru2 -- "pi のモデル一覧に現れません。"
            print -ru2 -- ""
            exit 1
        fi

        log "Model found: ${PROVIDER}/${_model}"
    done
else
    log "No API key: skipping 'pi --list-models' validation."
    log "Verifying ${MODELS_JSON} instead..."

    python3 - "${MODELS_JSON}" "${PROVIDER}" "${MODELS[@]}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
provider = sys.argv[2]
models = sys.argv[3:]

try:
    data = json.loads(path.read_text())
except FileNotFoundError:
    print(f"ERROR: {path} was not created.", file=sys.stderr)
    sys.exit(1)
except json.JSONDecodeError as exc:
    print(f"ERROR: Invalid JSON in {path}: {exc}", file=sys.stderr)
    sys.exit(1)

definition = data.get("providers", {}).get(provider)

if not isinstance(definition, dict):
    print(
        f"ERROR: Provider {provider!r} is missing from {path}.",
        file=sys.stderr,
    )
    sys.exit(1)

ids = [
    entry.get("id")
    for entry in definition.get("models", [])
    if isinstance(entry, dict)
]

for model in models:
    if model not in ids:
        print(
            f"ERROR: Model {model!r} is missing from providers.{provider} "
            f"in {path}.",
            file=sys.stderr,
        )
        print(f"Found: {ids}", file=sys.stderr)
        sys.exit(1)
PY

    log "models.json entries verified: ${PROVIDER}/${MODELS[*]}"
    log "Export COMMANDCODE_API_KEY and rerun to validate with 'pi --list-models'."
fi


#
# ----------------------------------------------------------------------
# 5. Verify launchers
# ----------------------------------------------------------------------
#

for _name in piccg piccd; do
    [[ -x "${BIN_DIR}/${_name}" ]] ||
        die "${BIN_DIR}/${_name} was not created correctly."

    log "Launcher executable: ${_name}: OK"
done


#
# ----------------------------------------------------------------------
# 6. Summary
# ----------------------------------------------------------------------
#

print
print "========================================"
print " Pi + Command Code GOAT setup complete"
print "========================================"
print
print "Provider : ${PROVIDER}"
print "Thinking : ${THINKING}"
print "Models   : ${MODELS[*]}"
print "Models   : ${MODELS_JSON}"
print "Auth     : COMMANDCODE_API_KEY environment variable"
print
print "Each launcher starts Pi via the pi-superagents 'bwrap' wrapper,"
print "which binds the launcher's own model tier config into Pi."
print "The tier config for each launcher lives at:"
print
print "  ~/.pi/agent/extensions/subagent.d/piccg/config.json"
print "  ~/.pi/agent/extensions/subagent.d/piccd/config.json"
print
print "piccg passes the following arguments to Pi:"
print
print "  pi \\"
print "    --provider ${PROVIDER} \\"
print "    --model ${PICCG_DEFAULT_MODEL} \\"
print "    --models \"${PICCG_MODELS_CSV}\" \\"
print "    --thinking ${THINKING}"
print
print "piccd passes the following arguments to Pi:"
print
print "  pi \\"
print "    --provider ${PROVIDER} \\"
print "    --model ${PICCD_DEFAULT_MODEL} \\"
print "    --models \"${PICCD_MODELS_CSV}\" \\"
print "    --thinking ${THINKING}"
print
print "/model や Ctrl+P で切り替えできます。"
print
print "Run:"
print
print '  export COMMANDCODE_API_KEY="..."'
print "  piccg"
print "  piccd"
print
print "If the current shell cannot find them yet:"
print
print "  source ~/.zshrc"
print "  rehash"
print "  piccg"
print "  piccd"
print
