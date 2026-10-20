#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${REPO_ROOT}/.devcontainer/herdr-agent-task.bash"
HERDR_CONFIG="${REPO_ROOT}/.devcontainer/herdr.toml"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    [ "$actual" = "$expected" ] || fail "$msg: expected='$expected' actual='$actual'"
}

run_dry() {
    HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="w1:pA" "$CLI" "$@"
}

test_set_emits_token_and_ttl() {
    out="$(run_dry set "Implement page transition")"
    rg -q $'w1:pA\ttoken\ttask\tImplement page transition' <<<"$out" \
        || fail "set missing task token: $out"
    rg -q $'w1:pA\tsource\therdr-agent-task' <<<"$out" \
        || fail "set missing source: $out"
    rg -q $'w1:pA\tttl_ms\t86400000' <<<"$out" \
        || fail "set missing default ttl: $out"
}

# Herdr の state_labels は agent_status をキーとするマップで、state_text は
# 現在の agent_status に対応する label しか引かない。working キーだけに載せると
# CI 待ちで idle に落ちた pane から状態語が消えるため、全キーへ載せる。
HERDR_AGENT_STATUSES=(idle working blocked done unknown)

assert_label_on_every_status() {
    local out="$1" label="$2" status
    for status in "${HERDR_AGENT_STATUSES[@]}"; do
        rg -q $'w1:pA\tstate_label\t'"${status}=${label}"'$' <<<"$out" \
            || fail "${label}: missing ${status} key: $out"
    done
}

test_state_mapping() {
    assert_label_on_every_status "$(run_dry set "t" --state working)" "Working"
    assert_label_on_every_status "$(run_dry set "t" --state waiting)" "Waiting"
    assert_label_on_every_status "$(run_dry set "t" --state reviewing)" "Reviewing"
    assert_label_on_every_status "$(run_dry set "t" --state completed)" "Completed"

    out="$(run_dry set "t" --state idle)"
    rg -q $'w1:pA\tclear_state_labels' <<<"$out" || fail "idle: $out"
    if rg -q $'\tstate_label\t' <<<"$out"; then
        fail "idle must clear labels instead of setting them: $out"
    fi
}

test_state_default_is_working() {
    out="$(run_dry set "t")"
    rg -q $'w1:pA\tstate_label\tworking=Working' <<<"$out" \
        || fail "default state must be working: $out"
}

test_attention_is_rejected() {
    status=0
    run_dry set "t" --state attention >/dev/null 2>&1 || status=$?
    assert_eq "$status" "64" "attention must be rejected with exit 64"
}

test_unknown_state_is_rejected() {
    status=0
    run_dry set "t" --state bogus >/dev/null 2>&1 || status=$?
    assert_eq "$status" "64" "unknown state must be rejected with exit 64"
}

test_title_truncation_ascii() {
    local long="0123456789012345678901234567890123456789"   # 40 文字
    out="$(run_dry set "$long")"
    local line value
    line="$(rg -m1 $'\ttoken\ttask\t' <<<"$out")"
    value="${line##*$'\t'}"
    assert_eq "${#value}" "36" "ascii title must be 36 chars"
    case "$value" in
        *…) : ;;
        *) fail "truncated ascii title must end with …: $value" ;;
    esac
}

test_title_truncation_japanese() {
    # 40 文字（120 バイト）。バイト単位で切ると 36 にならない
    local long="あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもや"
    long="${long}らりるれ"
    out="$(run_dry set "$long")"
    local line value
    line="$(rg -m1 $'\ttoken\ttask\t' <<<"$out")"
    value="${line##*$'\t'}"
    LC_ALL=C.UTF-8 bash -c '[ "${#1}" = "36" ]' _ "$value" \
        || fail "japanese title must be truncated to 36 chars: $value"
}

test_title_truncation_under_c_locale() {
    # 素の C ロケールで呼ばれてもバイト数で切らない（ensure_utf8_locale の回帰）
    local long="あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやらりるれ"
    local out line value
    out="$(LC_ALL=C HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="w1:pA" \
        "$CLI" set "$long")"
    line="$(rg -m1 $'\ttoken\ttask\t' <<<"$out")"
    value="${line##*$'\t'}"
    LC_ALL=C.UTF-8 bash -c '[ "${#1}" = "36" ]' _ "$value" \
        || fail "C locale でも 36 文字で truncate する必要がある: $value"
}

test_short_title_is_not_truncated() {
    out="$(run_dry set "short")"
    rg -q $'\ttoken\ttask\tshort$' <<<"$out" || fail "short title altered: $out"
}

test_newline_title_is_flattened() {
    out="$(run_dry set "$(printf 'line1\nline2')")"
    assert_eq "$(printf '%s\n' "$out" | wc -l)" "8" \
        "newline title must not split the TSV record: $out"
    rg -q $'w1:pA\ttoken\ttask\tline1 line2$' <<<"$out" \
        || fail "newline must be flattened to a space: $out"
}

test_tab_title_is_flattened() {
    out="$(run_dry set "$(printf 'colA\tcolB')")"
    rg -q $'w1:pA\ttoken\ttask\tcolA colB$' <<<"$out" \
        || fail "tab must be flattened to a space: $out"
}

test_title_whitespace_is_squeezed_and_trimmed() {
    out="$(run_dry set "  spaced   out  ")"
    rg -q $'w1:pA\ttoken\ttask\tspaced out$' <<<"$out" \
        || fail "surrounding and repeated whitespace must be squeezed: $out"
}

test_empty_title_is_error() {
    status=0
    run_dry set "" >/dev/null 2>&1 || status=$?
    assert_eq "$status" "64" "empty title must exit 64"
}

test_whitespace_only_title_is_error() {
    local candidate
    for candidate in "   " "$(printf ' \t ')" "$(printf ' \n ')"; do
        status=0
        run_dry set "$candidate" >/dev/null 2>&1 || status=$?
        assert_eq "$status" "64" "whitespace-only title must exit 64"
    done
}

test_invalid_title_max_len_is_error() {
    local candidate
    for candidate in abc 1 0 -1 3.5; do
        status=0
        HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="w1:pA" \
            HERDR_AGENT_TASK_TITLE_MAX_LEN="$candidate" "$CLI" set "t" >/dev/null 2>&1 || status=$?
        assert_eq "$status" "64" "TITLE_MAX_LEN='$candidate' must exit 64"
    done
}

test_invalid_ttl_ms_is_error() {
    local candidate
    for candidate in abc 0 -1 1.5; do
        status=0
        HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="w1:pA" \
            HERDR_AGENT_TASK_TTL_MS="$candidate" "$CLI" set "t" >/dev/null 2>&1 || status=$?
        assert_eq "$status" "64" "TTL_MS='$candidate' must exit 64 on set"

        status=0
        HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="w1:pA" \
            HERDR_AGENT_TASK_TTL_MS="$candidate" "$CLI" clear >/dev/null 2>&1 || status=$?
        assert_eq "$status" "64" "TTL_MS='$candidate' must exit 64 on clear"
    done
}

test_clear_emits_clears() {
    out="$(run_dry clear)"
    rg -q $'w1:pA\tclear_token\ttask' <<<"$out" || fail "clear missing token clear: $out"
    rg -q $'w1:pA\tclear_state_labels' <<<"$out" || fail "clear missing state clear: $out"
}

test_explicit_pane_wins() {
    out="$(run_dry set "t" --pane "w9:pZ")"
    rg -q $'w9:pZ\ttoken\ttask\tt' <<<"$out" || fail "--pane ignored: $out"
}

test_missing_pane_id_is_error() {
    status=0
    HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="" "$CLI" set "t" >/dev/null 2>&1 || status=$?
    assert_eq "$status" "64" "missing pane id must exit 64"
}

test_missing_title_is_error() {
    status=0
    run_dry set >/dev/null 2>&1 || status=$?
    assert_eq "$status" "64" "missing title must exit 64"
}

test_unknown_subcommand_is_error() {
    status=0
    run_dry bogus >/dev/null 2>&1 || status=$?
    assert_eq "$status" "64" "unknown subcommand must exit 64"
}

test_missing_herdr_binary_is_noop() {
    status=0
    HERDR_PANE_ID="w1:pA" HERDR_BIN="herdr-does-not-exist-$$" \
        "$CLI" set "t" >/dev/null 2>&1 || status=$?
    assert_eq "$status" "0" "missing herdr must exit 0"
}

test_ttl_override() {
    out="$(HERDR_AGENT_TASK_DRY_RUN=1 HERDR_PANE_ID="w1:pA" \
        HERDR_AGENT_TASK_TTL_MS=600000 "$CLI" set "t")"
    rg -q $'w1:pA\tttl_ms\t600000' <<<"$out" || fail "ttl override ignored: $out"
}

# herdr.toml の指定した table の rows が参照するカスタム token（$ で始まるもの）を 1 行ずつ出す。
sidebar_custom_tokens() {
    python3 - "$HERDR_CONFIG" "$1" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    table = tomllib.load(handle)
for key in sys.argv[2].split("."):
    table = table[key]
for row in table["rows"]:
    for cell in row:
        token = cell["token"] if isinstance(cell, dict) else cell
        if token.startswith("$"):
            print(token[1:])
PY
}

# Agents 行のカスタム token と、この CLI が投稿する token が一致する。
# 片方だけ名前を変えると、サイドバーにタスク名が出なくなる。
test_herdr_toml_agents_show_the_posted_token() {
    local posted shown
    posted="$(run_dry set "t" | awk -F'\t' '$2 == "token" { print $3 }' | sort -u)"
    [ -n "$posted" ] || fail "herdr-agent-task must post a token"
    shown="$(sidebar_custom_tokens ui.sidebar.agents | sort -u)"
    assert_eq "$shown" "$posted" "[ui.sidebar.agents] custom tokens must match the posted tokens"
}

test_herdr_toml_passes_config_check() {
    command -v herdr >/dev/null 2>&1 || return 0
    local probe="${TEST_ROOT}/xdg"
    mkdir -p "${probe}/herdr"
    cp "$HERDR_CONFIG" "${probe}/herdr/config.toml"
    out="$(XDG_CONFIG_HOME="$probe" herdr config check 2>&1)"
    assert_eq "$out" "config: ok" "herdr.toml must pass config check"
}

test_set_emits_token_and_ttl
test_state_mapping
test_state_default_is_working
test_attention_is_rejected
test_unknown_state_is_rejected
test_title_truncation_ascii
test_title_truncation_japanese
test_title_truncation_under_c_locale
test_short_title_is_not_truncated
test_newline_title_is_flattened
test_tab_title_is_flattened
test_title_whitespace_is_squeezed_and_trimmed
test_empty_title_is_error
test_whitespace_only_title_is_error
test_invalid_title_max_len_is_error
test_invalid_ttl_ms_is_error
test_clear_emits_clears
test_explicit_pane_wins
test_missing_pane_id_is_error
test_missing_title_is_error
test_unknown_subcommand_is_error
test_missing_herdr_binary_is_noop
test_ttl_override
test_herdr_toml_agents_show_the_posted_token
test_herdr_toml_passes_config_check
printf 'OK test-herdr-agent-task (task2)\n'
