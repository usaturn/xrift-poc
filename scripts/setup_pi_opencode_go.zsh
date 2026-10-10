#!/usr/bin/env zsh

set -euo pipefail

#
# Pi + OpenCode Go + Muse Spark 1.3 Contributor
#
# Idempotent setup script
#
# Creates:
#   ~/.pi/agent/auth.json     OpenCode Go credential
#   ~/.pi/agent/models.json   OpenCode Go model definition
#   ~/.local/bin/pigo         Pi launcher
#   ~/.local/bin/pigod        Pi launcher
#
# Uses Pi's BUILT-IN opencode-go provider for authentication and
# endpoint resolution. The model definition itself comes from
# .devcontainer/pi-models.json, which is the single source of truth.
#
# Running this script is NOT required after a container rebuild:
# ~/.pi is bind mounted and .devcontainer/install_beforehand.bash
# places pigo. Use it when rotating the API key, or when the host side
# ~/.pi has to be rebuilt from scratch.
#

readonly PROVIDER="opencode-go"
readonly MODEL="muse-spark-1.3-contributor"
readonly THINKING="max"

readonly PI_DIR="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
readonly AUTH_JSON="${PI_DIR}/auth.json"
readonly MODELS_JSON="${PI_DIR}/models.json"

#
# The model definition is kept in the repository so that
# install_beforehand.bash and this script cannot drift apart.
#
readonly MODELS_SOURCE="${0:A:h}/../.devcontainer/pi-models.json"

#
# Launchers placed by this script, as parallel arrays: each launcher is
# copied verbatim from .devcontainer/<name> and uses the model listed at
# the same index as its default.
#
# pigod shares the opencode-go provider and auth.json with pigo and
# differs only in its default model, so it is installed here as well.
#
readonly LAUNCHER_NAMES=(pigo pigod)
readonly LAUNCHER_MODELS=("${MODEL}" "deepseek-v4.1-flash")
readonly LAUNCHER_DIR="${0:A:h}/../.devcontainer"

readonly BIN_DIR="${HOME}/.local/bin"


log() {
    print -r -- "[setup_pi_opencode_go] $*"
}

warn() {
    print -ru2 -- "[setup_pi_opencode_go] WARNING: $*"
}

die() {
    print -ru2 -- "[setup_pi_opencode_go] ERROR: $*"
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
# Launchers are installed verbatim from the repository, so the model this
# script validates must be the one each launcher actually runs. The
# default model differs per launcher, so compare them pairwise.
#
for _i in {1..${#LAUNCHER_NAMES}}; do
    _source="${LAUNCHER_DIR}/${LAUNCHER_NAMES[_i]}"

    [[ -f "${_source}" ]] ||
        die "Launcher source not found: ${_source}"

    grep -Fq -- "--model ${LAUNCHER_MODELS[_i]}" "${_source}" ||
        die "${_source} does not use --model ${LAUNCHER_MODELS[_i]}."
done

mkdir -p "${PI_DIR}"
mkdir -p "${BIN_DIR}"

log "Pi executable : $(command -v pi)"
log "Pi config dir : ${PI_DIR}"
log "Model source  : ${MODELS_SOURCE}"
log "Launchers     : ${LAUNCHER_NAMES[*]}"


#
# ----------------------------------------------------------------------
# 1. Resolve OpenCode API key
#
# Pi's official environment variable is OPENCODE_API_KEY.
#
# For compatibility with the previous script, OPENCODE_GO_API_KEY is
# accepted as a fallback.
#
# If neither exists, an existing auth.json credential is kept. Because
# ~/.pi is bind mounted, the credential normally survives a container
# rebuild and no environment variable is needed.
# ----------------------------------------------------------------------
#

API_KEY="${OPENCODE_API_KEY:-${OPENCODE_GO_API_KEY:-}}"

if [[ -n "${API_KEY}" ]]; then
    log "OpenCode API key found in environment."

    #
    # Make the official Pi variable available to commands executed by
    # this script as well.
    #
    export OPENCODE_API_KEY="${API_KEY}"
else
    log "No OpenCode API key found in environment."
    log "Checking existing ${AUTH_JSON} ..."
fi


#
# ----------------------------------------------------------------------
# 2. Upsert auth.json
#
# auth.json schema:
#
# {
#   "opencode-go": {
#     "type": "api_key",
#     "key": "..."
#   }
# }
#
# Existing credentials for other providers are preserved.
#
# We intentionally do NOT make backup copies of auth.json because doing
# so would unnecessarily duplicate plaintext credentials.
# ----------------------------------------------------------------------
#

python3 - "${AUTH_JSON}" "${API_KEY}" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
new_key = sys.argv[2]

if path.exists():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: Invalid JSON in {path}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(data, dict):
        print(
            f"ERROR: {path} must contain a JSON object.",
            file=sys.stderr,
        )
        sys.exit(1)
else:
    data = {}

existing = data.get("opencode-go")

if new_key:
    desired = {
        "type": "api_key",
        "key": new_key,
    }

    if existing == desired:
        print("OpenCode Go credential: already configured")
    else:
        data["opencode-go"] = desired

        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            prefix=".auth.json.",
            dir=path.parent,
            text=True,
        )

        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())

            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
            os.chmod(path, 0o600)

        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

        print("OpenCode Go credential: configured")

else:
    if (
        isinstance(existing, dict)
        and existing.get("type") == "api_key"
        and existing.get("key")
    ):
        print("OpenCode Go credential: using existing auth.json entry")
    else:
        print(
            "ERROR: No OpenCode Go API key is configured.\n"
            "\n"
            "Set one of:\n"
            '  export OPENCODE_API_KEY="..."\n'
            "\n"
            "or, for compatibility with the previous setup:\n"
            '  export OPENCODE_GO_API_KEY="..."\n',
            file=sys.stderr,
        )
        sys.exit(1)
PY

chmod 600 "${AUTH_JSON}"


#
# ----------------------------------------------------------------------
# 3. Apply the OpenCode Go model definition
#
# muse-spark-1.3-contributor is not in Pi's built-in catalog
# (~/.pi/agent/models-store.json), so models.json has to define it.
#
# The definition is read from .devcontainer/pi-models.json and the
# whole providers.opencode-go entry is replaced, so that re-running
# this script always converges on the repository definition. This also
# clears the provider generated by the previous version of this script,
# whose provider level apiKey/baseUrl/api overrode Pi's built-in
# metadata.
#
# Providers other than opencode-go are preserved untouched.
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

providers = data.setdefault("providers", {})

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
# 4. Install the launchers into ~/.local/bin
#
# Launchers are copied verbatim from .devcontainer/<name>, which are the
# same files .devcontainer/install_beforehand.bash places on rebuild.
# Generating them here instead would let the two drift apart.
#
# ~/.local/bin is already added to PATH by .devcontainer/zshrc.txt, so
# this script does not touch ~/.zshrc.
# ----------------------------------------------------------------------
#

for _i in {1..${#LAUNCHER_NAMES}}; do
    _name="${LAUNCHER_NAMES[_i]}"
    _source="${LAUNCHER_DIR}/${_name}"
    _launcher="${BIN_DIR}/${_name}"

    if [[ -f "${_launcher}" ]] && cmp -s "${_source}" "${_launcher}"; then
        log "${_name} launcher: already up to date."
    else
        install -m 0755 "${_source}" "${_launcher}"
        log "${_name} launcher: installed."
    fi
done


#
# ----------------------------------------------------------------------
# 5. Refresh Pi model catalog
#
# Pi has a built-in OpenCode Go provider and its catalog can be refreshed
# independently of models.json.
#
# Failure to refresh is not fatal: an existing catalog may still work.
# ----------------------------------------------------------------------
#

log "Refreshing Pi model catalog..."

if pi update --models >/dev/null 2>&1; then
    log "Pi model catalog: refreshed."
else
    warn "'pi update --models' failed."
    warn "Continuing with the currently installed model catalog."
fi


#
# ----------------------------------------------------------------------
# 6. Validate provider/model availability
#
# Both launchers' models are checked here. pigo's comes from models.json,
# so this is the step that catches a stale definition. pigod's
# comes from the model catalog, which pi.dev can rename: a launcher left on a
# retired ID still starts, but pi then substitutes another model's definition
# and only prints a warning, so the stale ID has to fail loudly here.
#
# The listing is matched by column rather than by substring. `pi` prints
# `No models matching "<id>"` on a miss, and that message contains the ID
# itself, so a plain `grep -F` over the output succeeds even when nothing
# matched. It also succeeds on another provider's row: commandcode carries
# `deepseek/deepseek-v4.1-flash`, which contains the opencode-go ID as a
# substring. Comparing the provider and model columns avoids both.
# ----------------------------------------------------------------------
#

log "Checking OpenCode Go model availability..."

for _i in {1..${#LAUNCHER_NAMES}}; do
    _model="${LAUNCHER_MODELS[_i]}"

    MODEL_LIST="$(
        pi --list-models "${_model}" 2>&1 || true
    )"

    if ! print -r -- "${MODEL_LIST}" |
        awk -v p="${PROVIDER}" -v m="${_model}" \
            '$1 == p && $2 == m { found = 1 } END { exit !found }'; then
        print -ru2 -- ""
        print -ru2 -- "OpenCode Go model was not found."
        print -ru2 -- ""
        print -ru2 -- "Expected:"
        print -ru2 -- "  ${PROVIDER}/${_model}"
        print -ru2 -- ""
        print -ru2 -- "Pi output:"
        print -ru2 -- "${MODEL_LIST}"
        print -ru2 -- ""
        print -ru2 -- "Try updating Pi:"
        print -ru2 -- "  pi update"
        print -ru2 -- ""
        exit 1
    fi

    log "Model found: ${PROVIDER}/${_model}"
done


#
# ----------------------------------------------------------------------
# 7. Verify launchers
# ----------------------------------------------------------------------
#

for _i in {1..${#LAUNCHER_NAMES}}; do
    _launcher="${BIN_DIR}/${LAUNCHER_NAMES[_i]}"

    [[ -x "${_launcher}" ]] ||
        die "${_launcher} was not created correctly."
done

log "Launchers executable: OK"


#
# ----------------------------------------------------------------------
# 8. Summary
# ----------------------------------------------------------------------
#

print
print "========================================"
print " Pi + OpenCode Go setup complete"
print "========================================"
print
print "Provider : ${PROVIDER}"
print "Thinking : ${THINKING}"
print "Auth     : ${AUTH_JSON}"
print "Models   : ${MODELS_JSON}"
print
print "Launchers:"
for _i in {1..${#LAUNCHER_NAMES}}; do
    print "  ${BIN_DIR}/${LAUNCHER_NAMES[_i]}  (--model ${LAUNCHER_MODELS[_i]})"
done
print
print "Each launcher starts Pi via the pi-superagents 'bwrap' wrapper,"
print "which binds the launcher's own model tier config into Pi."
print "The tier config for each launcher lives at:"
print
for _i in {1..${#LAUNCHER_NAMES}}; do
    print "  ~/.pi/agent/extensions/subagent.d/${LAUNCHER_NAMES[_i]}/config.json"
done
print
print "Each launcher passes the following arguments to Pi:"
print
print "  pi \\"
print "    --provider ${PROVIDER} \\"
print "    --model <the model shown above> \\"
print "    --thinking ${THINKING}"
print
print "Run:"
print
for _i in {1..${#LAUNCHER_NAMES}}; do
    print "  ${LAUNCHER_NAMES[_i]}"
done
print
print "If the current shell cannot find them yet:"
print
print "  source ~/.zshrc"
print "  rehash"
print "  ${LAUNCHER_NAMES[1]}"
print
