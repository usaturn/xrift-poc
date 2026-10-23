#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${REPO_ROOT}/.devcontainer/pi-superagents"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT
unset PI_CODING_AGENT_DIR
cd "$REPO_ROOT"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
assert_eq() {
    [ "$1" = "$2" ] || fail "$3: expected='$2' actual='$1'"
}
setup_for_home() {
    local output
    output="$(env HOME="$1" zsh "${REPO_ROOT}/scripts/setup_pi_superagents.zsh" 2>&1)" \
        || fail "pi-superagents setup failed: ${output}"
}
write_oauth_auth() {
    printf '%s\n' '{"openai-codex":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999},"xai":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "$1"
}
# dry-run 出力から pi へ渡る引数だけを取り出す（"pi" より後ろ）。
pi_args_of() {
    printf '%s\n' "$1" | awk 'seen { print } $0 == "pi" { seen = 1 }'
}
# <option> <args...>: args の中で option の直後にある値を出す。
option_value() {
    local option="$1"
    shift
    while [ "$#" -gt 1 ]; do
        if [ "$1" = "$option" ]; then
            printf '%s\n' "$2"
            return 0
        fi
        shift
    done
    return 1
}

# ランチャーは .devcontainer/pi-superagents/<launcher>.json と 1 対 1 に対応する。
# 一覧と、そのうち親 Pi が openai-codex を使うランチャーを、テストへ書き写さずに導く。
# OAuth の確認が本文にあるかではなく、OAuth のある HOME で実際に渡す --provider で選ぶ。
# 確認を外したランチャーが検査対象から黙って抜けないようにするためである。
mapfile -t LAUNCHERS < <(find "$CONFIG_DIR" -maxdepth 1 -name '*.json' -printf '%f\n' \
    | sed 's/\.json$//' | LC_ALL=C sort)
[ "${#LAUNCHERS[@]}" -gt 0 ] || fail "no launcher configs in ${CONFIG_DIR}"
CODEX_LAUNCHERS=()
collect_codex_launchers() {
    local home="${TEST_ROOT}/discover" launcher output provider
    local -a own
    mkdir -p "${home}/.pi/agent"
    write_oauth_auth "${home}/.pi/agent/auth.json"
    setup_for_home "$home"
    for launcher in "${LAUNCHERS[@]}"; do
        output="$(env HOME="$home" PI_CODING_AGENT_DIR="${home}/.pi/agent" \
            COMMANDCODE_API_KEY=dummy-for-dry-run OPENCODE_API_KEY=dummy-for-dry-run \
            PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
            zsh "${REPO_ROOT}/.devcontainer/${launcher}" 2>&1)" \
            || fail "${launcher} dry-run failed: ${output}"
        mapfile -t own < <(pi_args_of "$output")
        provider="$(option_value --provider "${own[@]}")" || provider=""
        if [ "$provider" = openai-codex ]; then
            CODEX_LAUNCHERS+=("$launcher")
        fi
    done
}
collect_codex_launchers
[ "${#CODEX_LAUNCHERS[@]}" -gt 0 ] || fail "no launcher uses openai-codex"

# PI_CODING_AGENT_DIR の ~ 形式は、絶対パスで指定したときと同じ引数で起動する。
test_launchers_accept_tilde_agent_dir() {
    local home="${TEST_ROOT}/tilde-auth" launcher agent_dir baseline output
    mkdir -p "${home}/.pi/agent"
    write_oauth_auth "${home}/.pi/agent/auth.json"
    write_oauth_auth "${home}/auth.json"
    setup_for_home "$home"

    for launcher in "${CODEX_LAUNCHERS[@]}"; do
        baseline="$(env HOME="$home" PI_CODING_AGENT_DIR="${home}/.pi/agent" \
            PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
            zsh "${REPO_ROOT}/.devcontainer/${launcher}" --resume 2>&1)" \
            || fail "${launcher} rejected the absolute agent dir: ${baseline}"
        assert_eq "$(printf '%s\n' "$baseline" | sed -n 1p)" bwrap "${launcher} must reach argv"
        for agent_dir in '~/.pi/agent' '~'; do
            output="$(env HOME="$home" PI_CODING_AGENT_DIR="$agent_dir" \
                PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
                zsh "${REPO_ROOT}/.devcontainer/${launcher}" --resume 2>&1)" \
                || fail "${launcher} rejected ${agent_dir}: ${output}"
            assert_eq "$(pi_args_of "$output")" "$(pi_args_of "$baseline")" \
                "${launcher} Pi arguments with ${agent_dir}"
        done
    done
}

test_missing_oauth_guides_login_without_requesting_api_key() {
    local home="${TEST_ROOT}/no-oauth" launcher output status
    mkdir -p "$home"
    setup_for_home "$home"

    for launcher in "${CODEX_LAUNCHERS[@]}"; do
        set +e
        output="$(env HOME="$home" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
            zsh "${REPO_ROOT}/.devcontainer/${launcher}" 2>&1)"
        status=$?
        set -e
        assert_eq "$status" "1" "${launcher} must stop without OAuth"
        printf '%s\n' "$output" | grep -Fq '/login openai-codex' \
            || fail "${launcher} must explain direct Codex OAuth login"
        case "$output" in
            *'ChatGPT Plus/Pro (Codex)'*) fail "${launcher} must not name a nonexistent login choice" ;;
            *OPENAI_API_KEY*) fail "${launcher} must not request OPENAI_API_KEY" ;;
        esac
    done
}

test_install_places_all_launchers_with_regular_mode() {
    local home="${TEST_ROOT}/install" launcher output
    local -a args
    mkdir -p "${home}/.pi/agent"
    write_oauth_auth "${home}/.pi/agent/auth.json"
    cp "${REPO_ROOT}/.devcontainer/pi-models.json" "${home}/.pi/agent/models.json"
    {
        printf '%s\n' 'step() { :; }' 'fail() { printf "%s\n" "$1" >&2; exit 1; }'
        awk '/^step "Placing the launchers / { copy = 1 } /^step "Installing the shared skills / { copy = 0 } copy' \
            "${REPO_ROOT}/.devcontainer/install_beforehand.bash"
    } | env HOME="$home" bash -e
    cmp -s "${REPO_ROOT}/.devcontainer/pi-superagents-exec.zsh" "${home}/.local/bin/pi-superagents-exec.zsh" \
        || fail "install must place the current helper"

    for launcher in "${LAUNCHERS[@]}"; do
        [ -x "${home}/.local/bin/${launcher}" ] || fail "install did not place executable ${launcher}"
        cmp -s "${REPO_ROOT}/.devcontainer/${launcher}" "${home}/.local/bin/${launcher}" \
            || fail "installed ${launcher} differs from the source"
        output="$(env HOME="$home" PI_CODING_AGENT_DIR="${home}/.pi/agent" \
            COMMANDCODE_API_KEY=dummy-for-dry-run OPENCODE_API_KEY=dummy-for-dry-run \
            PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
            zsh "${home}/.local/bin/${launcher}" --resume)"
        mapfile -t args < <(pi_args_of "$output")
        assert_eq "${args[0]}" --tui-mode "installed ${launcher} tui option"
        assert_eq "${args[1]}" regular "installed ${launcher} tui mode"
        assert_eq "${args[${#args[@]}-1]}" --resume "installed ${launcher} user argument"
    done
}

# 実環境の Pi が、ランチャー（親 Pi）と tier が使う openai-codex のモデルを提供しているかを確かめる。
# モデル名はテストへ書き写さず、ランチャーの起動引数と tier 設定から導く。
test_live_openai_codex_models() {
    [ "${PI_CODEX_LIVE_CHECK:-0}" = 1 ] || return 0

    local home="${TEST_ROOT}/live" launcher provider model output
    local -a own models=()
    mkdir -p "${home}/.pi/agent"
    write_oauth_auth "${home}/.pi/agent/auth.json"
    setup_for_home "$home"
    for launcher in "${LAUNCHERS[@]}"; do
        output="$(env HOME="$home" PI_CODING_AGENT_DIR="${home}/.pi/agent" \
            COMMANDCODE_API_KEY=dummy-for-dry-run OPENCODE_API_KEY=dummy-for-dry-run \
            PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
            zsh "${REPO_ROOT}/.devcontainer/${launcher}")" || fail "${launcher} dry-run failed"
        mapfile -t own < <(pi_args_of "$output")
        provider="$(option_value --provider "${own[@]}")" || provider=""
        if [ "$provider" = openai-codex ]; then
            models+=("$(option_value --model "${own[@]}")")
        fi
    done
    mapfile -t -O "${#models[@]}" models < <(python3 - "$CONFIG_DIR" <<'PY'
import json
import pathlib
import sys

for config in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    for tier in json.loads(config.read_text())["superagents"]["modelTiers"].values():
        provider, _, model = tier["model"].partition("/")
        if provider == "openai-codex":
            print(model)
PY
)
    [ "${#models[@]}" -gt 0 ] || fail "no launcher or tier uses openai-codex"

    for model in $(printf '%s\n' "${models[@]}" | sort -u); do
        output="$(pi --list-models "$model")" || fail "pi --list-models ${model} failed"
        printf '%s\n' "$output" | awk -v model="$model" \
            '$1 == "openai-codex" && $2 == model { found = 1 } END { exit !found }' \
            || fail "openai-codex/${model} is unavailable; log in with /login or update Pi"
    done
}

test_launchers_accept_tilde_agent_dir
test_missing_oauth_guides_login_without_requesting_api_key
test_install_places_all_launchers_with_regular_mode
test_live_openai_codex_models
printf 'OK test-pi-openai-codex\n'
