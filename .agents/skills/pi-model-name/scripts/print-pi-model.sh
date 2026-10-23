#!/usr/bin/env bash
# Print the model identity the running agent runtime presents to this shell.
# pi は PI_PROVIDER / PI_MODEL を提示する。
# Strict by default: never falls back to session history for MODEL_NAME.
set -euo pipefail

usage() {
  cat <<'USAGE'
usage: print-pi-model.sh [--model|--provider|--json|--session] [--session-file PATH] [--allow-session-history]

  No flags:            print "provider/model" (e.g. "opencode-go/muse-spark-1.3-contributor")
  --model:             print the model id only (PI_MODEL)
  --provider:          print the provider only (PI_PROVIDER)
  --json:              print {"provider":..., "model":..., "provenance":"runtime"}
  --session:           print the session id, session file, and reasoning level
  --session-file PATH: cross-check this explicitly identified session JSONL
  --allow-session-history:
                       informational: when runtime vars are missing but
                       a session file exists, print the last model_change
                       modelId as a reference value with a stderr warning.
                       File priority: --session-file, PI_SESSION_FILE,
                       PI_SUBAGENT_SESSION.
                       Never use this output as formal MODEL_NAME.
USAGE
}

# provider / model の値を JSON 文字列として出力する。
json_string() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

mode="full"
allow_history=0
explicit_session_file=""

while (($# > 0)); do
  case "$1" in
    --model) mode="model" ;;
    --provider) mode="provider" ;;
    --json) mode="json" ;;
    --session) mode="session" ;;
    --session-file)
      if (($# < 2)) || [[ -z "$2" ]]; then
        printf '%s\n' '--session-file requires a path' >&2
        exit 64
      fi
      explicit_session_file="$2"
      shift ;;
    --allow-session-history) allow_history=1 ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'unknown flag: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

provider="${PI_PROVIDER:-}"
model="${PI_MODEL:-}"

if [[ -n "$provider" && -n "$model" ]]; then
  check_file=""
  check_source=""
  if [[ -n "$explicit_session_file" ]]; then
    check_file="$explicit_session_file"
    check_source="explicit"
  elif [[ -n "${PI_SESSION_FILE:-}" ]]; then
    check_file="$PI_SESSION_FILE"
    check_source="runtime"
  elif [[ -n "${PI_SUBAGENT_SESSION:-}" ]]; then
    check_file="$PI_SUBAGENT_SESSION"
    check_source="subagent"
  fi
  if [[ -n "$check_file" ]]; then
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    exec python3 "$script_dir/check_session.py" --mode "$mode" --session-file "$check_file" --source "$check_source"
  fi
  case "$mode" in
    full) printf '%s/%s\n' "$provider" "$model" ;;
    model) printf '%s\n' "$model" ;;
    provider) printf '%s\n' "$provider" ;;
    json) printf '{"provider":%s,"model":%s,"provenance":"runtime","ok":true,"session_file":null,"selected":null,"observed":null,"conflicts":[],"warnings":[]}\n' \
      "$(json_string "$provider")" "$(json_string "$model")" ;;
    session)
      printf 'provider=%s\nmodel=%s\nsession_id=%s\nsession_file=%s\nreasoning=%s\n' \
        "$provider" "$model" "${PI_SESSION_ID:-}" "${PI_SESSION_FILE:-}" "${PI_REASONING_LEVEL:-}" ;;
  esac
  exit 0
fi

session_file="${explicit_session_file:-${PI_SESSION_FILE:-${PI_SUBAGENT_SESSION:-}}}"
if [[ "$allow_history" == "1" && -n "$session_file" && -r "$session_file" ]]; then
  hist_model="$(python3 - "$session_file" <<'PY'
import json, sys
last = None
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "model_change" and o.get("modelId"):
                last = o
except Exception:
    last = None
if last:
    print(last.get("modelId", ""))
PY
)"
  hist_provider="$(python3 - "$session_file" <<'PY'
import json, sys
last = None
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "model_change" and o.get("modelId"):
                last = o
except Exception:
    last = None
print(last.get("provider", "") if last else "")
PY
)"
  if [[ -n "$hist_model" ]]; then
    printf 'provenance=session-history (informational only, NOT formal MODEL_NAME)\n' >&2
    case "$mode" in
      full) printf '%s/%s\n' "${hist_provider:-unknown}" "$hist_model" ;;
      model) printf '%s\n' "$hist_model" ;;
      provider) printf '%s\n' "${hist_provider:-unknown}" ;;
      json) printf '{"provider":%s,"model":%s,"provenance":"session-history"}\n' \
        "$(json_string "${hist_provider:-unknown}")" "$(json_string "$hist_model")" ;;
      session)
        printf 'provider=%s\nmodel=%s\nsession_id=%s\nsession_file=%s\nreasoning=%s\nprovenance=session-history\n' \
          "${hist_provider:-unknown}" "$hist_model" "${PI_SESSION_ID:-}" "$session_file" "${PI_REASONING_LEVEL:-}" ;;
    esac
    exit 0
  fi
fi

cat >&2 <<'MSG'
PI_PROVIDER/PI_MODEL is not presented by the pi runtime in this shell.
Stop and ask the user for the exact model identifier.
Do not fall back to ~/.pi/agent/sessions history for formal MODEL_NAME.
MSG
exit 2
