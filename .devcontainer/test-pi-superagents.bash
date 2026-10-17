#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="${REPO_ROOT}/.devcontainer/pi-superagents-exec.zsh"
TEST_ROOT="$(mktemp -d)"
ZSH_BIN="$(command -v zsh)"
trap 'rm -rf "${TEST_ROOT}"' EXIT

# 呼び出し側の PI_CODING_AGENT_DIR で結果が変わらないよう、スイート全体で
# 無効化する。必要なテストは呼び出し単位で明示的に設定する。
unset PI_CODING_AGENT_DIR

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    [ "$actual" = "$expected" ] || fail "$msg: expected='$expected' actual='$actual'"
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    case "$haystack" in
        *"$needle"*) ;;
        *) fail "$msg: '$needle' not found in '$haystack'" ;;
    esac
}

# ランチャー用の一時 HOME を作る。config を置くかどうかを引数で選ぶ。
make_home() {
    local name="$1" with_config="$2"
    local home="${TEST_ROOT}/${name}"
    mkdir -p "${home}/.pi/agent/extensions/subagent"
    if [ "$with_config" = "with-config" ]; then
        mkdir -p "${home}/.pi/agent/extensions/subagent.d/${name}"
        printf '{"superagents":{"modelTiers":{}}}\n' \
            > "${home}/.pi/agent/extensions/subagent.d/${name}/config.json"
    fi
    printf '%s' "$home"
}

# pixg の起動前認証を、実ランチャーに渡す一時 HOME の auth.json で試す。
write_xai_auth() {
    local home="$1"
    mkdir -p "${home}/.pi/agent"
    printf '%s\n' \
        '{"xai":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "${home}/.pi/agent/auth.json"
}

# OAuth で起動前に確認するランチャー（openai-codex・xai）をまとめて起動できる auth.json を置く。
write_oauth_auth() {
    local home="$1"
    mkdir -p "${home}/.pi/agent"
    printf '%s\n' \
        '{"openai-codex":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999},"xai":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "${home}/.pi/agent/auth.json"
}

# ヘルパを source して pi_superagents_exec を dry-run で呼ぶ。
run_helper() {
    local home="$1" launcher="$2"; shift 2
    env HOME="$home" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec '${launcher}' \"\$@\"" _ "$@"
}

test_dry_run_emits_bwrap_command() {
    local home out
    home="$(make_home pigo with-config)"
    out="$(run_helper "$home" pigo --provider opencode-go --model muse-spark-1.3-contributor)"
    assert_eq "$(printf '%s\n' "$out" | sed -n 1p)" "bwrap" "argv[0]"
    assert_contains "$out" "--dev-bind" "dev-bind option"
    assert_contains "$out" "--die-with-parent" "die-with-parent option"
    assert_contains "$out" "${home}/.pi/agent/extensions/subagent.d/pigo" "bind source"
    assert_contains "$out" "${home}/.pi/agent/extensions/subagent" "bind target"
    # bind 元・重ね先を位置で固定する。これらは argv 配列の 7 番目 (src) と
    # 8 番目 (dst) に並ぶ。部分一致だけでは重ね先が bind 元の部分文字列に
    # なるため、SRC/DEST が逆でも通ってしまう。
    assert_eq "$(printf '%s\n' "$out" | sed -n 7p)" \
        "${home}/.pi/agent/extensions/subagent.d/pigo" "bind src must be argv[7]"
    assert_eq "$(printf '%s\n' "$out" | sed -n 8p)" \
        "${home}/.pi/agent/extensions/subagent" "bind dst must be argv[8]"
    assert_contains "$out" "pi" "pi command"
    assert_contains "$out" "--provider" "passthrough of pi args"
    assert_contains "$out" "muse-spark-1.3-contributor" "passthrough of pi args"
    assert_eq "$(pi_args_of "$out")" \
        "$(printf '%s\n' --tui-mode regular --provider opencode-go --model muse-spark-1.3-contributor)" \
        "helper must select regular before forwarding pi args"
}

test_bind_target_ignores_pi_coding_agent_dir() {
    # 拡張は os.homedir() を直接見るので、PI_CODING_AGENT_DIR を設定しても
    # bind 先は HOME 基準でなければならない。
    local home out
    home="$(make_home pigo with-config)"
    out="$(env PI_CODING_AGENT_DIR="${TEST_ROOT}/elsewhere" \
        HOME="$home" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec pigo")"
    assert_contains "$out" "${home}/.pi/agent/extensions/subagent" "bind target must follow HOME"
    # 重ね先 (argv[8]) を HOME 基準の共有ディレクトリに位置で固定する。
    assert_eq "$(printf '%s\n' "$out" | sed -n 8p)" \
        "${home}/.pi/agent/extensions/subagent" "bind dst must equal HOME-based shared dir"
    case "$out" in
        *"${TEST_ROOT}/elsewhere"*) fail "bind target must not follow PI_CODING_AGENT_DIR" ;;
    esac
}

test_missing_config_fails_closed() {
    local home status out
    home="$(make_home pigod without-config)"
    set +e
    out="$(run_helper "$home" pigod 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "missing config must exit 1"
    assert_contains "$out" "setup_pi_superagents.zsh" "must point at the setup script"
    assert_contains "$out" "pigod" "must name the launcher"
}

test_missing_bwrap_fails_closed() {
    # command -v bwrap を失敗させるため PATH を空にする。zsh 自体は絶対パスで
    # 起動するので、PATH 制限は zsh プロセス側だけに効く。
    local home status out
    home="$(make_home piccg with-config)"
    mkdir -p "${TEST_ROOT}/emptybin"
    set +e
    out="$(env HOME="$home" PATH="${TEST_ROOT}/emptybin" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec piccg" 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "missing bwrap must exit 1"
    assert_contains "$out" "bwrap" "must name bwrap"
}

# ------------------------------------------------------------------ dry-run 判定
# PI_SUPERAGENTS_LAUNCHER_DRY_RUN が文字列 "1" のときだけ argv 表示になり、
# それ以外（未設定 / "" / "0" / "false"）は本当に bwrap を起動する。本当の
# bwrap を起動しないよう、PATH 先頭に呼び出しを記録する fake bwrap を置く。
BWRAP_SHIM_DIR="${TEST_ROOT}/bwrapbin"
FAKE_BWRAP_LOG="${TEST_ROOT}/bwrap-state/log"

make_bwrap_shim() {
    mkdir -p "$BWRAP_SHIM_DIR" "${TEST_ROOT}/bwrap-state"
    cat > "${BWRAP_SHIM_DIR}/bwrap" <<'BWRAP_SHIM_EOF'
#!/bin/bash
printf 'bwrap invoked with %s args\n' "$#" >> "${FAKE_BWRAP_LOG:?}"
printf '%s\n' "$@" >> "${FAKE_BWRAP_LOG:?}"
BWRAP_SHIM_EOF
    chmod +x "${BWRAP_SHIM_DIR}/bwrap"
}

# ヘルパを指定の dry-run 値で呼ぶ。PATH 先頭に fake bwrap を載せ、実環境の
# bwrap に影響を与えない。
run_helper_dry_run() {
    local home="$1" launcher="$2" drv="$3"; shift 3
    env HOME="$home" PATH="${BWRAP_SHIM_DIR}:${PATH}" FAKE_BWRAP_LOG="$FAKE_BWRAP_LOG" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN="$drv" \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec '${launcher}' \"\$@\"" _ "$@"
}

test_dry_run_one_prints_argv_without_exec() {
    # =1 は argv を表示するだけで、fake bwrap を呼ばない。
    local home out
    home="$(make_home dry1 with-config)"
    make_bwrap_shim
    rm -f "$FAKE_BWRAP_LOG"
    out="$(run_helper_dry_run "$home" dry1 1 --provider opencode-go)"
    assert_eq "$(printf '%s\n' "$out" | sed -n 1p)" "bwrap" "argv[0]"
    assert_contains "$out" "--dev-bind" "dev-bind option"
    [ ! -e "$FAKE_BWRAP_LOG" ] || fail "dry-run (=1) must not exec bwrap"
}

test_dry_run_zero_execs_bwrap() {
    # =0 は通常起動と同じく fake bwrap を exec する。
    local home log
    home="$(make_home dry0 with-config)"
    make_bwrap_shim
    rm -f "$FAKE_BWRAP_LOG"
    run_helper_dry_run "$home" dry0 0 --provider opencode-go
    log="$(cat "$FAKE_BWRAP_LOG")"
    assert_contains "$log" "bwrap invoked with" "=0 must exec bwrap"
    assert_contains "$log" "--dev-bind" "=0 must pass the bind args to bwrap"
    assert_contains "$log" "--provider" "=0 must pass user args to bwrap"
}

test_actual_and_dry_run_argv_match() {
    local home out expected
    local -a command_args
    home="$(make_home argv-match with-config)"
    make_bwrap_shim
    rm -f "$FAKE_BWRAP_LOG"
    out="$(run_helper "$home" argv-match --session 'session with spaces.jsonl' -- 'message with spaces')"
    mapfile -t command_args <<< "$out"
    expected="$(printf 'bwrap invoked with %s args\n' "$((${#command_args[@]} - 1))"; \
        printf '%s\n' "${command_args[@]:1}")"
    run_helper_dry_run "$home" argv-match 0 --session 'session with spaces.jsonl' -- 'message with spaces'
    assert_eq "$(<"$FAKE_BWRAP_LOG")" "$expected" "actual and dry-run must use identical argv"
}

CONFIG_DIR="${REPO_ROOT}/.devcontainer/pi-superagents"
PI_MODELS="${REPO_ROOT}/.devcontainer/pi-models.json"

# ランチャーは .devcontainer/pi-superagents/<launcher>.json の tier 設定と 1 対 1 に対応する。
# 一覧をテストへ書き写さず、設定ファイルから導く。
mapfile -t LAUNCHERS < <(find "$CONFIG_DIR" -maxdepth 1 -name '*.json' -printf '%f\n' \
    | sed 's/\.json$//' | LC_ALL=C sort)
[ "${#LAUNCHERS[@]}" -gt 0 ] || fail "no launcher configs in ${CONFIG_DIR}"

# <launcher> <tier> の model 文字列を取り出す。
tier_model() {
    python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
print(data["superagents"]["modelTiers"][sys.argv[2]]["model"])
' "${CONFIG_DIR}/$1.json" "$2"
}

test_commandcode_models_are_defined_in_pi_models() {
    # commandcode は Pi の built-in provider ではないので、models.json に
    # 定義が無いモデルを tier に置くと子 Pi が起動時に解決できない。
    python3 -c '
import json, sys

models = json.load(open(sys.argv[1]))
defined = {
    entry["id"]
    for entry in models["providers"]["commandcode"]["models"]
}
missing = []
for launcher in ("piccg", "piccd"):
    cfg = json.load(open(f"{sys.argv[2]}/{launcher}.json"))
    for tier, spec in cfg["superagents"]["modelTiers"].items():
        model = spec["model"]
        provider, _, model_id = model.partition("/")
        if provider != "commandcode":
            missing.append(f"{launcher}.{tier}: {model} is not a commandcode model")
        elif model_id not in defined:
            missing.append(f"{launcher}.{tier}: {model_id} not in pi-models.json")
if missing:
    print("\n".join(missing))
    sys.exit(1)
' "$PI_MODELS" "$CONFIG_DIR" || fail "commandcode tier models are not defined in pi-models.json"
}

test_opencode_go_tiers_use_opencode_go() {
    local launcher tier model
    for launcher in pigo pigod; do
        for tier in cheap balanced max; do
            model="$(tier_model "$launcher" "$tier")"
            case "$model" in
                opencode-go/*) ;;
                *) fail "${launcher}.${tier} must use opencode-go: ${model}" ;;
            esac
        done
    done
}

test_configs_contain_no_credentials() {
    local f
    for f in "${CONFIG_DIR}"/*.json; do
        if grep -qiE "api[_-]?key|token|\\\$[A-Z_]*KEY" "$f"; then
            fail "$f must not contain credentials"
        fi
    done
}

test_configs_are_minimal_partial_configs() {
    # modelTiers だけを持つ部分 config にする。他のキーは bundled defaults から
    # deep-merge されるため、書くと更新に追従できなくなる。
    local f
    for f in "${CONFIG_DIR}"/*.json; do
        python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
assert list(data) == ["superagents"], list(data)
assert list(data["superagents"]) == ["modelTiers"], list(data["superagents"])
tiers = data["superagents"]["modelTiers"]
assert sorted(tiers) == ["balanced", "cheap", "max"], sorted(tiers)
for name, tier in tiers.items():
    assert sorted(tier) == ["model", "thinking"], (name, tier)
    provider, _, model = tier["model"].partition("/")
    assert provider and model, (name, tier)
    assert isinstance(tier["thinking"], str) and tier["thinking"], (name, tier)
' "$f" || fail "$f must contain only superagents.modelTiers with cheap/balanced/max of {model: provider/id, thinking}"
    done
}

# ランチャーは install 後 ~/.local/bin に置かれるが、テストはリポジトリ内の
# 元ファイルを直接叩く。ヘルパの探索がリポジトリ相対で解決できることも同時に見る。
run_launcher() {
    local home="$1" launcher="$2"; shift 2
    env HOME="$home" \
        PI_CODING_AGENT_DIR="${home}/.pi/agent" \
        COMMANDCODE_API_KEY="dummy-for-dry-run" \
        OPENCODE_API_KEY="dummy-for-dry-run" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        zsh "${REPO_ROOT}/.devcontainer/${launcher}" "$@"
}

test_pigs_opencode_key_reaches_child_process() {
    local home="${TEST_ROOT}/pigs-opencode-key" shim_dir="${TEST_ROOT}/pigs-opencode-bin"
    local row primary legacy expected output
    local -a key_env cases=(
        '<unset>:dummy-legacy:dummy-legacy'
        ':dummy-legacy:dummy-legacy'
        'dummy-primary:dummy-legacy:dummy-primary'
        '<unset>:<unset>:<unset>'
    )
    mkdir -p "${home}/.pi/agent/extensions/subagent.d/pigs" "$shim_dir"
    cp "${CONFIG_DIR}/pigs.json" "${home}/.pi/agent/extensions/subagent.d/pigs/config.json"
    printf '%s\n' \
        '{"openai-codex":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "${home}/.pi/agent/auth.json"

    # 実ランチャーから exec された bwrap の子で、export された値を観測する。
    # 実際の mount namespace や Pi の API 呼び出しはこのテストには不要。
    cat > "${shim_dir}/bwrap" <<'BWRAP_ENV_EOF'
#!/bin/bash
bash -c 'printf "%s\n" "${OPENCODE_API_KEY-<unset>}"'
BWRAP_ENV_EOF
    chmod +x "${shim_dir}/bwrap"

    for row in "${cases[@]}"; do
        IFS=: read -r primary legacy expected <<< "$row"
        key_env=(env -u OPENCODE_API_KEY -u OPENCODE_GO_API_KEY)
        if [ "$primary" != '<unset>' ]; then
            key_env+=("OPENCODE_API_KEY=$primary")
        fi
        if [ "$legacy" != '<unset>' ]; then
            key_env+=("OPENCODE_GO_API_KEY=$legacy")
        fi
        output="$("${key_env[@]}" HOME="$home" \
            PI_CODING_AGENT_DIR="${home}/.pi/agent" PATH="${shim_dir}:${PATH}" \
            PI_SUPERAGENTS_LAUNCHER_DRY_RUN=0 \
            "${ZSH_BIN}" "${REPO_ROOT}/.devcontainer/pigs")"
        assert_eq "$output" "$expected" "pigs child key with primary='${primary}' legacy='${legacy}'"
    done
}

# dry-run 出力から pi へ渡る引数だけを取り出す（"--" の次の "pi" より後ろ）。
pi_args_of() {
    printf '%s\n' "$1" | awk 'seen { print } $0 == "pi" { seen = 1 }'
}

# run_launcher は zsh 経由で起動するため実行ビットを必要としない。
# リポジトリからの直接実行を案内するランチャーの git モード落ちをここで検出する。
test_launcher_files_are_executable() {
    local launcher
    for launcher in "${LAUNCHERS[@]}"; do
        [ -x "${REPO_ROOT}/.devcontainer/${launcher}" ] \
            || fail ".devcontainer/${launcher} must be executable (git mode 100755)"
    done
}

# 各ランチャーは自分の tier 設定を共有ディレクトリへ重ねて Pi を起動する。
# bind 元・重ね先は --bind の直後の 2 要素で見る（部分一致だと重ね先が bind 元の
# 部分文字列に化けて通るため）。
test_launchers_wrap_pi_with_their_own_config() {
    local launcher home out
    local -a argv
    local i bind_at
    for launcher in "${LAUNCHERS[@]}"; do
        home="$(make_home "$launcher" with-config)"
        write_oauth_auth "$home"
        out="$(run_launcher "$home" "$launcher")"
        mapfile -t argv <<< "$out"
        assert_eq "${argv[0]}" "bwrap" "${launcher} argv[0]"
        bind_at=""
        for i in "${!argv[@]}"; do
            if [ "${argv[$i]}" = "--bind" ]; then
                bind_at="$i"
                break
            fi
        done
        [ -n "$bind_at" ] || fail "${launcher} must pass --bind to bwrap"
        assert_eq "${argv[$((bind_at + 1))]}" \
            "${home}/.pi/agent/extensions/subagent.d/${launcher}" "${launcher} bind src"
        assert_eq "${argv[$((bind_at + 2))]}" \
            "${home}/.pi/agent/extensions/subagent" "${launcher} bind dst"
    done
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

# 各ランチャーは Pi を regular TUI で起動し、provider・model・thinking を指定したうえで、
# 利用者の引数を空白を含めてそのまま末尾へ渡す。モデル名はランチャーの設定なので写さず、
# commandcode（Pi の built-in provider ではない）のモデルは pi-models.json に定義があることを見る。
test_launchers_select_regular_and_forward_user_arguments() {
    local launcher home option value provider models
    local -a args own user_args=(--resume --session 'session with spaces.jsonl' -- 'message with spaces')
    for launcher in "${LAUNCHERS[@]}"; do
        home="$(make_home "$launcher" with-config)"
        write_oauth_auth "$home"
        mapfile -t args < <(pi_args_of "$(run_launcher "$home" "$launcher" "${user_args[@]}")")
        [ "${#args[@]}" -gt "$((2 + ${#user_args[@]}))" ] || fail "${launcher} passed too few Pi arguments"
        assert_eq "${args[0]} ${args[1]}" "--tui-mode regular" "${launcher} must select the regular TUI first"
        assert_eq "$(printf '%s\n' "${args[@]:${#args[@]}-${#user_args[@]}}")" \
            "$(printf '%s\n' "${user_args[@]}")" "${launcher} must forward user arguments last"
        own=("${args[@]:2:${#args[@]}-2-${#user_args[@]}}")
        for option in --provider --model --thinking; do
            value="$(option_value "$option" "${own[@]}")" || fail "${launcher} must pass ${option}"
            [ -n "$value" ] || fail "${launcher} must pass a value for ${option}"
        done
        provider="$(option_value --provider "${own[@]}")"
        if [ "$provider" = commandcode ]; then
            models="$(option_value --model "${own[@]}")"
            if value="$(option_value --models "${own[@]}")"; then
                models="${models},${value}"
            fi
            python3 - "$PI_MODELS" "$models" <<'PY' \
                || fail "${launcher} must use commandcode models defined in pi-models.json"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    defined = {entry["id"] for entry in json.load(source)["providers"]["commandcode"]["models"]}
missing = sorted(set(sys.argv[2].split(",")) - defined)
assert not missing, missing
PY
        fi
    done
}

# Pi に google-vertex を渡すランチャーだけが auth.json の読み取り専用 overlay を重ねる。
# helper の Vertex ランチャーの一覧へ登録し忘れると、保存済みの Vertex key や OAuth が
# overlay なしで Pi に渡る。bwrap 側の引数（"pi" の行より前）だけを見る。
test_only_vertex_launchers_overlay_auth() {
    local launcher home out provider overlay
    local -a own
    for launcher in "${LAUNCHERS[@]}"; do
        home="$(make_home "$launcher" with-config)"
        write_oauth_auth "$home"
        out="$(run_launcher "$home" "$launcher")"
        mapfile -t own < <(pi_args_of "$out")
        provider="$(option_value --provider "${own[@]}")" || provider=""
        overlay=0
        if printf '%s\n' "$out" | awk '$0 == "pi" { exit } $0 == "--ro-bind-data" { found = 1 } END { exit !found }'; then
            overlay=1
        fi
        if [ "$provider" = google-vertex ]; then
            assert_eq "$overlay" 1 "${launcher} passes google-vertex to Pi and must overlay auth.json"
        else
            assert_eq "$overlay" 0 "${launcher} does not use google-vertex and must not overlay auth.json"
        fi
    done
}

test_pixg_oauth_guidance() {
    local home status out
    # 専用の home を使う。先行テストが ${TEST_ROOT}/pixg に置いた auth.json が
    # 残っていると未認証ケースを再現できない。
    home="${TEST_ROOT}/pixg-oauth-guidance"
    mkdir -p "${home}/.pi/agent/extensions/subagent" \
        "${home}/.pi/agent/extensions/subagent.d/pixg"
    printf '{"superagents":{"modelTiers":{}}}\n' \
        > "${home}/.pi/agent/extensions/subagent.d/pixg/config.json"

    # auth.json が無ければ OAuth 未設定として /login の案内で止まる。
    set +e
    out="$(run_launcher "$home" pixg 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "pixg must exit 1 without xai OAuth"
    assert_contains "$out" "/login xai" "must explain xai OAuth login"
    assert_contains "$out" "SuperGrok" "must name the subscription sign-in"
    case "$out" in
        *XAI_API_KEY*) fail "pixg must not request XAI_API_KEY" ;;
    esac

    # oauth 以外の xai credential（API key 等）も未設定として案内する。
    printf '%s\n' '{"xai":{"type":"api_key","key":"dummy"}}' \
        > "${home}/.pi/agent/auth.json"
    set +e
    out="$(run_launcher "$home" pixg 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "pixg must exit 1 for non-oauth xai credentials"
    assert_contains "$out" "/login xai" "non-oauth credential must still guide to /login"

    # xAI OAuth があれば起動する。cheap / balanced tier の子 Pi が使う
    # opencode-go の資格情報は auth.json の api_key または OPENCODE_API_KEY で
    # 解決し、ランチャーは起動前に確認しない（pigo / pigod と同じ）。
    write_xai_auth "$home"
    out="$(run_launcher "$home" pixg)"
    assert_eq "$(printf '%s\n' "$out" | sed -n 1p)" "bwrap" "valid xAI OAuth must reach argv"

    # PI_CODING_AGENT_DIR の ~ 展開も共有ヘルパと同じ受理形にする。
    out="$(env HOME="$home" PI_CODING_AGENT_DIR='~/.pi/agent' \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        zsh "${REPO_ROOT}/.devcontainer/pixg" 2>&1)"
    assert_eq "$(printf '%s\n' "$out" | sed -n 1p)" "bwrap" "tilde agent dir must resolve the OAuth credential"
}

test_commandcode_key_check_runs_before_launch() {
    local home status out
    home="$(make_home piccd with-config)"
    set +e
    out="$(env HOME="$home" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        COMMANDCODE_API_KEY= \
        zsh "${REPO_ROOT}/.devcontainer/piccd" 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "missing COMMANDCODE_API_KEY must exit 1"
    assert_contains "$out" "COMMANDCODE_API_KEY" "existing key check must still fire"
}

test_launcher_missing_config_fails_closed() {
    local home status out
    home="$(make_home pigo without-config)"
    # make_home は TEST_ROOT 配下を名前で再利用するため、先行する
    # with-config テストが置いた config.json が残りうる。確実に「config 無し」を
    # 作るため、このテストが使う launcher の config 一式を先に取り除く。
    rm -rf "${home}/.pi/agent/extensions/subagent.d/pigo"
    set +e
    out="$(run_launcher "$home" pigo 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "launcher must exit 1 without its config"
    assert_contains "$out" "setup_pi_superagents.zsh" "must point at the setup script"
}

test_run_launcher_ignores_external_pi_coding_agent_dir() {
    local home foreign status out
    # 呼び出し側で PI_CODING_AGENT_DIR を export していても、run_launcher は
    # テスト用 home 配下へ固定する。実環境側の auth.json を読んで誤って
    # 成功扱いになることを防ぐ（test_bind_target_ignores_pi_coding_agent_dir
    # と対になるランチャー側の観点）。
    home="${TEST_ROOT}/pixg-env-isolation"
    foreign="${TEST_ROOT}/pixg-env-isolation-foreign"
    mkdir -p "${home}/.pi/agent/extensions/subagent" \
        "${home}/.pi/agent/extensions/subagent.d/pixg" "$foreign"
    printf '{"superagents":{"modelTiers":{}}}\n' \
        > "${home}/.pi/agent/extensions/subagent.d/pixg/config.json"
    printf '%s\n' '{}' > "${home}/.pi/agent/auth.json"
    # 外部環境側は xai OAuth 設定済み。こちらを読まれると exit 0 になる。
    printf '%s\n' \
        '{"xai":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "${foreign}/auth.json"
    set +e
    out="$(PI_CODING_AGENT_DIR="$foreign" run_launcher "$home" pixg 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "run_launcher must pin PI_CODING_AGENT_DIR to the test home"
    assert_contains "$out" "/login xai" "must read the test home's auth.json"
}

SETUP="${REPO_ROOT}/scripts/setup_pi_superagents.zsh"

run_setup() {
    local home="$1"; shift
    env HOME="$home" zsh "$SETUP" "$@"
}

test_setup_places_all_launcher_configs() {
    local home launcher
    home="${TEST_ROOT}/setup-fresh"
    mkdir -p "$home"
    run_setup "$home" >/dev/null
    for launcher in "${LAUNCHERS[@]}"; do
        [ -f "${home}/.pi/agent/extensions/subagent.d/${launcher}/config.json" ] \
            || fail "setup did not place ${launcher} config"
        diff -q "${CONFIG_DIR}/${launcher}.json" \
            "${home}/.pi/agent/extensions/subagent.d/${launcher}/config.json" >/dev/null \
            || fail "${launcher} config differs from the repository source"
    done
    [ -d "${home}/.pi/agent/extensions/subagent" ] || fail "setup did not create the shared dir"
    cmp -s "$HELPER" "${home}/.local/bin/pi-superagents-exec.zsh" \
        || fail "setup must place the current helper"
}

test_setup_backs_up_modified_config() {
    local home config backups
    home="${TEST_ROOT}/setup-backup"
    mkdir -p "$home"
    run_setup "$home" >/dev/null
    config="${home}/.pi/agent/extensions/subagent.d/pigo/config.json"
    printf '{"superagents":{"modelTiers":{}}}\n' > "$config"
    run_setup "$home" >/dev/null
    backups="$(find "${home}/.pi/agent/extensions/subagent.d/pigo" -name 'config.json.bak.*' | wc -l)"
    [ "$backups" -ge 1 ] || fail "setup must back up a modified config"
    diff -q "${CONFIG_DIR}/pigo.json" "$config" >/dev/null \
        || fail "setup must restore the repository content"
}

# 固定時刻の date shim。PATH の先頭に載せて setup_pi_superagents.zsh から見える
# ようにする。実時刻に依存せず、同一 "秒" での退避衝突を再現できるようにする。
DATE_SHIM_DIR="${TEST_ROOT}/dateshim"

make_date_shim() {
    local ts="$1"
    mkdir -p "$DATE_SHIM_DIR"
    cat > "${DATE_SHIM_DIR}/date" <<DATE_SHIM_EOF
#!/bin/bash
if [ "\$1" = "+%Y%m%d-%H%M%S" ]; then
    printf '%s\n' '$ts'
    exit 0
fi
exec /usr/bin/date "\$@"
DATE_SHIM_EOF
    chmod +x "${DATE_SHIM_DIR}/date"
}

# date shim を PATH に載せて setup を実行する。
run_setup_shim() {
    local home="$1"; shift
    env HOME="$home" PATH="${DATE_SHIM_DIR}:${PATH}" zsh "$SETUP" "$@"
}

test_setup_preserves_prior_generation_on_name_collision() {
    # 固定時刻の date shim で「同一秒」の退避名衝突を作る。内容の異なる config を
    # 2 回退避させ、2 世代が別ファイル (.bak.<ts> と .bak.<ts>.1) に残り、
    # それぞれの世代の内容が上書きされず保存されることを内容まで検証する。
    local ts home config dir gen1 gen2 backups base
    ts='20260912-094438'
    make_date_shim "$ts"
    home="${TEST_ROOT}/setup-collision"
    mkdir -p "$home"
    run_setup_shim "$home" >/dev/null
    dir="${home}/.pi/agent/extensions/subagent.d/pigo"
    config="${dir}/config.json"
    gen1='{"superagents":{"modelTiers":{"gen":"one"}}}'
    gen2='{"superagents":{"modelTiers":{"gen":"two"}}}'
    printf '%s\n' "$gen1" > "$config"
    run_setup_shim "$home" >/dev/null
    printf '%s\n' "$gen2" > "$config"
    run_setup_shim "$home" >/dev/null

    backups="$(find "$dir" -name 'config.json.bak.*' | sort)"
    assert_eq "$(printf '%s\n' "$backups" | wc -l)" "2" \
        "two generations must be preserved, not overwritten"
    base="config.json.bak.${ts}"
    assert_contains "$backups" "${base}" "generation 1 must keep the base name"
    assert_contains "$backups" "${base}.1" "generation 2 must be saved as .1 on collision"
    grep -q '"gen":"one"' "${dir}/${base}" \
        || fail "generation 1 content must be preserved in ${base}"
    grep -q '"gen":"two"' "${dir}/${base}.1" \
        || fail "generation 2 content must be preserved in ${base}.1"
}

test_setup_is_idempotent_without_changes() {
    local home backups
    home="${TEST_ROOT}/setup-idempotent"
    mkdir -p "$home"
    run_setup "$home" >/dev/null
    run_setup "$home" >/dev/null
    backups="$(find "${home}/.pi/agent/extensions/subagent.d" -name 'config.json.bak.*' | wc -l)"
    assert_eq "$backups" "0" "unchanged configs must not accumulate backups"
}

test_setup_if_absent_preserves_user_edits() {
    local home config
    home="${TEST_ROOT}/setup-if-absent"
    mkdir -p "$home"
    run_setup "$home" >/dev/null
    config="${home}/.pi/agent/extensions/subagent.d/piccd/config.json"
    printf '{"superagents":{"modelTiers":{"cheap":{"model":"x/y","thinking":"max"}}}}\n' > "$config"
    printf '# stale helper\n' > "${home}/.local/bin/pi-superagents-exec.zsh"
    run_setup "$home" --if-absent >/dev/null
    grep -q '"x/y"' "$config" || fail "--if-absent must not overwrite an existing config"
    cmp -s "$HELPER" "${home}/.local/bin/pi-superagents-exec.zsh" \
        || fail "--if-absent must refresh the helper while preserving configs"
}

test_setup_output_mentions_each_launcher() {
    local home out launcher
    home="${TEST_ROOT}/setup-report"
    mkdir -p "$home"
    out="$(run_setup "$home")"
    for launcher in "${LAUNCHERS[@]}"; do
        assert_contains "$out" "$launcher" "setup report must mention ${launcher}"
    done
}

# setup は settings を所有しない。書式を含めて既存のバイト列を比較する。
test_setup_leaves_user_settings_untouched() {
    local mode fixture case_home dir settings snapshot shim out
    local pass launcher original config
    local -a setup_args
    shim="${TEST_ROOT}/settings-pi-shim"
    mkdir -p "$shim"
    cat > "${shim}/pi" <<'PI_EOF'
#!/bin/bash
if [ "${1:-}" = list ]; then
    printf '%s\n' 'npm:@teelicht/pi-superagents'
    exit 0
fi
exit 64
PI_EOF
    chmod +x "${shim}/pi"

    for mode in normal if-absent; do
        setup_args=()
        if [ "$mode" = if-absent ]; then
            setup_args=(--if-absent)
        fi
        for fixture in missing valid legacy broken; do
            case_home="${TEST_ROOT}/settings-${mode}-${fixture}"
            dir="${case_home}/.pi/agent"
            settings="${dir}/settings.json"
            snapshot="${TEST_ROOT}/snapshot-${mode}-${fixture}"
            mkdir -p "$dir" "$snapshot"
            case "$fixture" in
                missing) ;;
                valid)
                    printf '{\r\n\t"skills": ["./other skill"], "packages": ["npm:x"],\r\n\t"theme": "dark"\r\n}\r\n\r\n' > "$settings"
                    ;;
                legacy)
                    printf '%s' '{ "skills" : ["./other skill", "git/github.com/obra/superpowers/skills"], "packages": ["npm:x"], "theme": "dark" }' > "$settings"
                    ;;
                broken)
                    printf '{ broken\r\n\n' > "$settings"
                    ;;
            esac
            printf 'generation one\r\n\n' > "${dir}/settings.json.bak.20260922-000000"
            printf '%s' 'generation two without newline' > "${dir}/settings.json.bak.20260922-000000.1"
            cp "$dir"/settings.json* "$snapshot/"
            find "$dir" -maxdepth 1 -name 'settings.json*' -printf '%f\n' \
                | LC_ALL=C sort > "${snapshot}/names.before"

            if [ "$mode" = if-absent ]; then
                for launcher in "${LAUNCHERS[@]}"; do
                    config="${dir}/extensions/subagent.d/${launcher}/config.json"
                    mkdir -p "${config%/*}"
                    printf '%s\n' '{"user":"keep"}' > "$config"
                done
            fi

            for pass in 1 2; do
                out="${TEST_ROOT}/output-${mode}-${fixture}-${pass}"
                if ! PATH="${shim}:${PATH}" run_setup "$case_home" "${setup_args[@]}" >"$out" 2>&1; then
                    fail "setup failed: ${mode}/${fixture}/${pass}"
                fi
                if [ "$fixture" = missing ]; then
                    [ ! -e "$settings" ] && [ ! -L "$settings" ] \
                        || fail "setup created settings: ${mode}/${fixture}/${pass}"
                fi
                find "$dir" -maxdepth 1 -name 'settings.json*' -printf '%f\n' \
                    | LC_ALL=C sort > "${snapshot}/names.after"
                cmp -s "${snapshot}/names.before" "${snapshot}/names.after" \
                    || fail "setup changed settings file names: ${mode}/${fixture}/${pass}"
                for original in "$snapshot"/settings.json*; do
                    cmp -s "$original" "${dir}/${original##*/}" \
                        || fail "setup changed settings bytes: ${mode}/${fixture}/${pass}/${original##*/}"
                done
                if grep -Eq 'skills:|settings\.json|Superpowers entry skill|Superpowers スキルの探索パス' "$out"; then
                    fail "setup emitted settings diagnostics: ${mode}/${fixture}/${pass}"
                fi
                for launcher in "${LAUNCHERS[@]}"; do
                    config="${dir}/extensions/subagent.d/${launcher}/config.json"
                    if [ "$mode" = if-absent ]; then
                        printf '%s\n' '{"user":"keep"}' | cmp -s - "$config" \
                            || fail "setup replaced edited config: ${launcher}"
                    else
                        cmp -s "${CONFIG_DIR}/${launcher}.json" "$config" \
                            || fail "setup did not place config: ${launcher}"
                    fi
                done
                [ -f "${case_home}/.local/bin/pi-superagents-exec.zsh" ] \
                    || fail "setup did not install helper"
                [ -d "${dir}/extensions/subagent" ] \
                    || fail "setup did not create bind target"
            done
        done
    done
}

# ------------------------------------------------------------------ 完了表示の起動経路
# 完了表示は、ランチャーが実際に Pi を起動する経路（bwrap wrapper 経由）を正しく
# 説明しなければならない。ランチャー自身の summary は provider/model/models/thinking
# をランチャーから Pi へ渡す引数として示し、その経路が pi-superagents の bwrap
# wrapper を通ることと、tier 設定が ~/.pi/agent/extensions/subagent.d/<launcher>/
# config.json にあることを含める。
SETUP_OPENCODE="${REPO_ROOT}/scripts/setup_pi_opencode_go.zsh"
SETUP_COMMANDCODE="${REPO_ROOT}/scripts/setup_pi_commandcode_goat.zsh"

# 両 setup スクリプトが command -v pi / pi --list-models / pi update --models を
# 使うため、PATH 先頭に置く fake pi。実環境の pi には影響を与えない。
make_setup_pi_fake() {
    mkdir -p "${TEST_ROOT}/setuppi"
    cat > "${TEST_ROOT}/setuppi/pi" <<'SETUP_PI_EOF'
#!/bin/bash
case "$1" in
    update)
        exit 0
        ;;
    --list-models)
        case "$2" in
            muse-spark-1.3-contributor)
                printf 'opencode-go muse-spark-1.3-contributor 128k\n'
                ;;
            deepseek-v4.1-flash)
                printf 'opencode-go deepseek-v4.1-flash 128k\n'
                ;;
            *)
                printf 'No models matching "%s"\n' "$2"
                exit 0
                ;;
        esac
        exit 0
        ;;
esac
exit 0
SETUP_PI_EOF
    chmod +x "${TEST_ROOT}/setuppi/pi"
}

run_opencode_setup() {
    local home="$1"; shift
    env HOME="$home" PATH="${TEST_ROOT}/setuppi:${PATH}" \
        OPENCODE_API_KEY="dummy-opencode-key" zsh "$SETUP_OPENCODE" "$@"
}

run_commandcode_setup() {
    local home="$1"; shift
    # このテストはキー無し経路（models.json 直接検証）を意図する。外側の環境に
    # COMMANDCODE_API_KEY が残っているとキーあり経路に入り、fake pi が
    # --list-models commandcode に応答できず失敗するため、確実に除去する。
    env -u COMMANDCODE_API_KEY HOME="$home" PATH="${TEST_ROOT}/setuppi:${PATH}" \
        zsh "$SETUP_COMMANDCODE" "$@"
}

test_setup_opencode_go_completion_mentions_bwrap_and_subagent_d() {
    local home out
    make_setup_pi_fake
    home="${TEST_ROOT}/setup-ocgo-report"
    mkdir -p "$home"
    out="$(run_opencode_setup "$home")"
    assert_contains "$out" "bwrap" "opencode-go completion must explain the bwrap wrapper"
    # 部分一致の "subagent.d" ではなく、tier config の完全パスを要求する。
    for l in pigo pigod; do
        assert_contains "$out" "~/.pi/agent/extensions/subagent.d/${l}/config.json" \
            "opencode-go completion must name the exact ${l} config path"
    done
}

test_setup_commandcode_goat_completion_mentions_bwrap_and_subagent_d() {
    local home out
    make_setup_pi_fake
    home="${TEST_ROOT}/setup-ccgoat-report"
    mkdir -p "$home"
    out="$(run_commandcode_setup "$home")"
    assert_contains "$out" "bwrap" "commandcode-goat completion must explain the bwrap wrapper"
    # 部分一致の "subagent.d" ではなく、tier config の完全パスを要求する。
    for l in piccg piccd; do
        assert_contains "$out" "~/.pi/agent/extensions/subagent.d/${l}/config.json" \
            "commandcode-goat completion must name the exact ${l} config path"
    done
}

INSTALL_SCRIPT="${REPO_ROOT}/.devcontainer/install_beforehand.bash"

# ---------------------------------------------------------------- pi 拡張導入の fail-loud 化
# install_beforehand.bash 全体は sudo apt 等を含みテストで実行できないため、
# pi-superagents の導入だけを関数 install_pi_extensions() として
# 取り出し、fake pi を PATH に載せた独立スクリプトで実行して検証する。
# fake pi は TEST_ROOT 配下の registry を一覧として返し、環境変数で各失敗を演出する。
FAKE_PI="${TEST_ROOT}/bin/pi"
FAKE_STATE="${TEST_ROOT}/pi-state/registry"
FAKE_LOG="${TEST_ROOT}/pi-state/log"

make_fake_pi() {
    mkdir -p "${TEST_ROOT}/bin" "${TEST_ROOT}/pi-state" "${TEST_ROOT}/fake-home"
    cat > "$FAKE_PI" <<'FAKE_PI_EOF'
#!/bin/bash
set -u
state="${PI_FAKE_STATE:?}"
log="${PI_FAKE_LOG:?}"
cmd="${1:-}"
shift || true
case "$cmd" in
    list)
        if [ "${FAKE_PI_FAIL_LIST:-0}" = 1 ]; then
            echo "fake pi: list failed" >&2
            exit 1
        fi
        if [ "${FAKE_PI_MISSING_NEW:-0}" = 1 ]; then
            [ -f "$state" ] && grep -v '^npm:@teelicht/pi-superagents$' "$state" || true
            exit 0
        fi
        [ -f "$state" ] && cat "$state" || true
        ;;
    remove)
        printf 'remove: %s\n' "$*" >> "$log"
        registered=0
        if [ -f "$state" ] && [ -n "${1:-}" ]; then
            if grep -qx "${1}" "$state"; then
                registered=1
            fi
            # 全行を除去して空になるケースでも grep -v は 1 を返すため、成功として扱う。
            grep -v "^${1}$" "$state" > "${state}.tmp" || true
            mv "${state}.tmp" "$state"
        fi
        # 実物の pi remove は、settings に未登録でも管理マニフェストの記載は掃除し、
        # 「No matching package found」で exit 1 を返す。この差が #289 の論点なので、
        # fake でも同じ振る舞いを再現する。
        if [ -n "${PI_FAKE_NPM_DIR:-}" ]; then
            for manifest in "${PI_FAKE_NPM_DIR}/package.json" "${PI_FAKE_NPM_DIR}/package-lock.json"; do
                [ -f "$manifest" ] || continue
                grep -v -- "${1#npm:}" "$manifest" > "${manifest}.tmp" || true
                mv "${manifest}.tmp" "$manifest"
            done
        fi
        [ "$registered" = 1 ] || exit 1
        ;;
    install)
        printf 'install: %s\n' "$*" >> "$log"
        if [ "${FAKE_PI_FAIL_INSTALL:-0}" = 1 ]; then
            echo "fake pi: install failed" >&2
            exit 1
        fi
        touch "$state"
        if ! grep -qx "$2" "$state"; then
            printf '%s\n' "$2" >> "$state"
        fi
        ;;
    *)
        echo "fake pi: unexpected subcommand: ${cmd}" >&2
        exit 1
        ;;
esac
FAKE_PI_EOF
    chmod +x "$FAKE_PI"
}

# install_beforehand.bash の install_pi_extensions() 関数だけを取り出して実行する。
run_install_fragment() {
    local src="${TEST_ROOT}/fragment.bash"
    awk '/^install_pi_extensions\(\) \{/{p=1} p{print} p && /^\}/{exit}' "$INSTALL_SCRIPT" > "$src"
    printf 'install_pi_extensions\n' >> "$src"
    env HOME="${TEST_ROOT}/fake-home" PATH="${TEST_ROOT}/bin:${PATH}" \
        PI_FAKE_STATE="$FAKE_STATE" PI_FAKE_LOG="$FAKE_LOG" \
        PI_FAKE_NPM_DIR="${TEST_ROOT}/fake-home/.pi/agent/npm" \
        bash "$src"
}

test_install_installs_superagents_without_removing_anything() {
    # 旧拡張が登録済みでも撤去はせず（警告は別の手順）、新拡張の導入と事後確認だけを行う。
    rm -f "$FAKE_STATE" "$FAKE_LOG"
    printf 'npm:pi-subagents\n' >> "$FAKE_STATE"
    local status out
    set +e
    out="$(run_install_fragment 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "0" "installing pi-superagents must exit 0"
    if grep -q '^remove: ' "$FAKE_LOG"; then
        fail "install_pi_extensions must not remove extensions"
    fi
    grep -q '^install: --approve npm:@teelicht/pi-superagents$' "$FAKE_LOG" \
        || fail "install of pi-superagents must be called"
}

test_install_new_failure_is_fail_loud() {
    # 新拡張の導入に失敗したら非 0 で、対象を stderr に示す。
    rm -f "$FAKE_STATE" "$FAKE_LOG"
    local status out
    set +e
    out="$(FAKE_PI_FAIL_INSTALL=1 run_install_fragment 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "failed install must exit 1"
    assert_contains "$out" "npm:@teelicht/pi-superagents" "must name the failing extension"
}

test_install_fails_when_new_missing_after() {
    # 導入後の一覧に新拡張が存在しなかったら非 0 で、不足している拡張を stderr に示す。
    rm -f "$FAKE_STATE" "$FAKE_LOG"
    printf 'npm:pi-subagents\n' >> "$FAKE_STATE"
    local status out
    set +e
    out="$(FAKE_PI_MISSING_NEW=1 run_install_fragment 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "new extension missing after install must exit 1"
    assert_contains "$out" "npm:@teelicht/pi-superagents" "must name the missing extension"
}

test_install_fails_when_list_fails() {
    # 導入後の一覧の取得に失敗したら非 0 で、理由を stderr に示す。
    rm -f "$FAKE_STATE" "$FAKE_LOG"
    local status out
    set +e
    out="$(FAKE_PI_FAIL_LIST=1 run_install_fragment 2>&1)"
    status=$?
    set -e
    assert_eq "$status" "1" "failed pi list must exit 1"
    assert_contains "$out" "Failed to list pi extensions" "must mention the failing list command"
}

# 実環境の Pi が、ランチャー（親 Pi）と tier が使う xai のモデルを提供しているかを確かめる。
# 未認証では xai 行が出ないため、先に素の pi で /login xai から SuperGrok / X Premium の
# OAuth を済ませておくこと。
test_pixg_live_model() {
    [ "${PI_XAI_LIVE_CHECK:-0}" = 1 ] || return 0

    local launcher home provider model output
    local -a own models=()
    for launcher in "${LAUNCHERS[@]}"; do
        home="$(make_home "$launcher" with-config)"
        write_oauth_auth "$home"
        mapfile -t own < <(pi_args_of "$(run_launcher "$home" "$launcher")")
        provider="$(option_value --provider "${own[@]}")" || provider=""
        if [ "$provider" = xai ]; then
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
        if provider == "xai":
            print(model)
PY
)
    [ "${#models[@]}" -gt 0 ] || fail "no launcher or tier uses xai"

    output="$(pi --list-models xai)" || fail "pi --list-models xai failed"
    for model in $(printf '%s\n' "${models[@]}" | sort -u); do
        printf '%s\n' "$output" | awk -v model="$model" '$1 == "xai" && $2 == model { found = 1 } END { exit !found }' \
            || fail "xai/${model} is unavailable; run /login xai or update Pi"
    done
}

test_dry_run_emits_bwrap_command
test_bind_target_ignores_pi_coding_agent_dir
test_missing_config_fails_closed
test_missing_bwrap_fails_closed
test_dry_run_one_prints_argv_without_exec
test_dry_run_zero_execs_bwrap
test_actual_and_dry_run_argv_match
test_commandcode_models_are_defined_in_pi_models
test_opencode_go_tiers_use_opencode_go
test_configs_contain_no_credentials
test_configs_are_minimal_partial_configs
test_launcher_files_are_executable
test_launchers_wrap_pi_with_their_own_config
test_launchers_select_regular_and_forward_user_arguments
test_only_vertex_launchers_overlay_auth
test_pigs_opencode_key_reaches_child_process
test_pixg_oauth_guidance
test_commandcode_key_check_runs_before_launch
test_launcher_missing_config_fails_closed
test_run_launcher_ignores_external_pi_coding_agent_dir
test_setup_places_all_launcher_configs
test_setup_backs_up_modified_config
test_setup_preserves_prior_generation_on_name_collision
test_setup_is_idempotent_without_changes
test_setup_if_absent_preserves_user_edits
test_setup_output_mentions_each_launcher
test_setup_leaves_user_settings_untouched
test_setup_opencode_go_completion_mentions_bwrap_and_subagent_d
test_setup_commandcode_goat_completion_mentions_bwrap_and_subagent_d
make_fake_pi
test_install_installs_superagents_without_removing_anything
test_install_new_failure_is_fail_loud
test_install_fails_when_new_missing_after
test_install_fails_when_list_fails
test_pixg_live_model
printf 'OK test-pi-superagents (task5)\n'
