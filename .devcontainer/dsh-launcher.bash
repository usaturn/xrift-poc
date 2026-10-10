#!/usr/bin/env bash
# dshccd / dshgo 共通の起動処理。キーの値や引数は表示・保存しない。
set -euo pipefail
launcher="${1:-}"
case "$launcher" in
    dshccd) key_name=COMMANDCODE_API_KEY ;;
    dshgo) key_name=OPENCODE_API_KEY ;;
    *) printf 'Use dshccd or dshgo to launch the DSH terminal UI.\n' >&2; exit 64 ;;
esac
shift

# DSH と pi-tui が持つ値付き option の値を、offline option と誤認しない。
# 元の "$@" は保持して転送する。-- 以降は app 引数として扱う。
args=("$@")
offline=false
bundle_defaults=false
for ((i=0; i<${#args[@]}; i++)); do
    case "${args[i]}" in
        --) break ;;
        --patch|--profile|--from-default-profile|--session|--preset) i=$((i + 1)) ;;
        --help|-h|--version|-V|--dump-config|--dump-config-schema) offline=true ;;
        --dump-default-config) offline=true; bundle_defaults=true ;;
    esac
done
if [[ "$offline" == false && -z "${!key_name:-}" ]]; then
    printf '%s: %s is required; export it in this shell before starting the TUI.\n' "$launcher" "$key_name" >&2
    exit 1
fi
if ! command -v dsh >/dev/null 2>&1; then
    printf '%s: dsh is not on PATH; see .devcontainer/README.md.\n' "$launcher" >&2
    exit 127
fi

# 外側の pi のモデル情報を DSH の現在値として扱わない。両方の起動出口で
# 共通の前処理にし、DSH の事実が無いときも親の pi の値へ落とさない。
unset PI_PROVIDER PI_MODEL PI_SESSION_ID PI_SESSION_FILE PI_SUBAGENT_SESSION PI_REASONING_LEVEL

# --dump-default-config は upstream が --patch との併用を拒否する。
# bundle 雛形だけの確認なのでランチャーの patch は付けない。
if [[ "$bundle_defaults" == true ]]; then
    exec dsh --profile pi-tui "$@"
fi
script="$(readlink -f -- "${BASH_SOURCE[0]}")"
bin_dir="$(dirname -- "$script")"
patch="${bin_dir}/${launcher}.patch.yml"
# dsh-model-env.patch.yml は実行中の provider / model を DSH_* としてシェルへ渡す
# patch（#577）。先に重ね、接続先を選ぶ launcher patch を最後に置く。
model_env_patch="${bin_dir}/dsh-model-env.patch.yml"
for required in "$model_env_patch" "$patch"; do
    if [[ ! -r "$required" ]]; then
        printf '%s: launcher patch is missing or unreadable (%s); reinstall the DSH launcher assets.\n' \
            "$launcher" "$required" >&2
        exit 1
    fi
done
exec dsh --profile pi-tui --patch "$model_env_patch" --patch "$patch" "$@"
