#!/bin/bash
# herdr Agents 向け task title / state reporter
# pane メタデータに $task トークンと state label を投稿する。
# HERDR_AGENT_TASK_DRY_RUN=1 のときは投稿せず TSV を stdout に出す。
#
# Herdr の agent 種別と agent_status は Herdr 自身の自動判定に任せる。
# report-agent（lifecycle authority の奪取）は使わない。

set -euo pipefail

readonly SOURCE_ID="herdr-agent-task"
readonly TOKEN_NAME="task"
readonly EXIT_USAGE=64

TTL_MS="${HERDR_AGENT_TASK_TTL_MS:-86400000}"
TITLE_MAX_LEN="${HERDR_AGENT_TASK_TITLE_MAX_LEN:-36}"
HERDR_BIN="${HERDR_BIN:-herdr}"

# truncate は文字数で行う。素の C ロケールでは ${#var} がバイト数になり、
# 日本語のタスク名が途中で壊れる（実測: "ページ遷移を実装する" が C で 30、C.UTF-8 で 10）。
# ロケール名を決め打ちすると不在の環境で無言のバイト数カウントに戻るため、
# 実際に 1 文字を数えて確認しながら候補を試す。
ensure_utf8_locale() {
    local probe="あ"
    if [ "${#probe}" -eq 1 ]; then
        return 0
    fi
    local candidate
    for candidate in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8; do
        export LC_ALL="$candidate"
        probe="あ"
        if [ "${#probe}" -eq 1 ]; then
            return 0
        fi
    done
    unset LC_ALL
    return 0
}

usage() {
    cat >&2 <<'USAGE'
Usage:
  herdr-agent-task set <title> [--state <state>] [--pane <pane-id>]
  herdr-agent-task clear [--pane <pane-id>]
  herdr-agent-task show [--pane <pane-id>]

States: working (default) | waiting | reviewing | completed | idle
  attention は指定できない。ユーザ入力待ちは Herdr が blocked として自動検出する。

Pane id defaults to $HERDR_PANE_ID.
USAGE
}

die_usage() {
    printf 'herdr-agent-task: %s\n' "$1" >&2
    usage
    exit "$EXIT_USAGE"
}

# TTL と truncate 上限は環境変数で上書きできる。不正値を素通しすると
# TITLE_MAX_LEN は truncate を無言で無効化し、TTL_MS は herdr 側で拒否されて
# report() の握り潰しにより無投稿のまま exit 0 になる。どちらも前段で弾く。
validate_ttl_ms() {
    if ! [[ "$TTL_MS" =~ ^[0-9]+$ ]] || [ "$TTL_MS" -le 0 ]; then
        die_usage "HERDR_AGENT_TASK_TTL_MS は正の整数である: $TTL_MS"
    fi
}

validate_title_max_len() {
    if ! [[ "$TITLE_MAX_LEN" =~ ^[0-9]+$ ]] || [ "$TITLE_MAX_LEN" -lt 2 ]; then
        die_usage "HERDR_AGENT_TASK_TITLE_MAX_LEN は 2 以上の整数である: $TITLE_MAX_LEN"
    fi
}

# タスク名はサイドバーの 1 行に載る。改行・タブは DRY_RUN が出す TSV の
# 「1 レコード 1 行」形式とサイドバー行の両方を割るため、空白へ潰す。
# 連続した空白を 1 つに詰め、前後の空白を落とす。
# サブシェルで実行されるため、この関数の中では die_usage を呼ばない。
sanitize_title() {
    local title="$1"
    title="${title//[$'\n\r\t\v\f']/ }"
    while [ "$title" != "${title//  / }" ]; do
        title="${title//  / }"
    done
    title="${title# }"
    printf '%s' "${title% }"
}

# 文字数単位の truncate。上限超過時は末尾を … に置き換える。
# サブシェルで実行されるため、この関数の中では die_usage を呼ばない。
# max の妥当性は validate_title_max_len が前段で弾く。ここの分岐は、
# die_usage を呼べないこの文脈で不正値を受けても壊れないための安全網である。
truncate_title() {
    local title="$1" max="$2"
    if ! [[ "$max" =~ ^[0-9]+$ ]] || [ "$max" -lt 2 ]; then
        printf '%s' "$title"
        return 0
    fi
    if [ "${#title}" -le "$max" ]; then
        printf '%s' "$title"
        return 0
    fi
    printf '%s…' "${title:0:$((max - 1))}"
}

# 状態名を検証する。不正なら usage を出して終了する。
# die_usage の exit を親へ伝えるため、この関数はサブシェルで呼ばない。
validate_state() {
    case "$1" in
        working | waiting | reviewing | completed | idle)
            return 0
            ;;
        attention)
            die_usage "attention は指定できない。ユーザ入力待ちは Herdr が blocked として自動検出する"
            ;;
        *)
            die_usage "unknown state: $1"
            ;;
    esac
}

# Herdr の state_labels は agent_status をキーとするマップで、state_text は
# 現在の agent_status に対応する label だけを引く（herdr 0.8.2 の API schema:
# AgentStatus = idle | working | blocked | done | unknown）。
# エージェントが申告する状態語は Herdr の自動判定と独立に出したいので、
# 全キーへ同じ語を載せる。記号（state_icon）は Herdr の自動判定のまま残るため、
# 「記号は Herdr の観測、語はエージェントの申告」と役割が分かれる。
HERDR_AGENT_STATUSES=(idle working blocked done unknown)

# 状態名から report-metadata の引数配列を組む。結果は STATE_LABEL_ARGS に入る。
# 戻り値を配列で返す手段が bash に無いため、グローバルを使う。
# コマンド置換やプロセス置換で受けるとサブシェルになり、
# validate_state の exit が親へ伝わらなくなる。
STATE_LABEL_ARGS=()
build_state_label_args() {
    local label=""
    case "$1" in
        working)   label="Working" ;;
        waiting)   label="Waiting" ;;
        reviewing) label="Reviewing" ;;
        completed) label="Completed" ;;
        idle)      STATE_LABEL_ARGS=(--clear-state-labels); return 0 ;;
    esac
    STATE_LABEL_ARGS=()
    local status
    for status in "${HERDR_AGENT_STATUSES[@]}"; do
        STATE_LABEL_ARGS+=(--state-label "${status}=${label}")
    done
}

# pane id を解決する。結果は PANE_ID に入る。理由は build_state_label_args と同じ。
PANE_ID=""
resolve_pane_id() {
    local explicit="$1"
    if [ -n "$explicit" ]; then
        PANE_ID="$explicit"
        return 0
    fi
    if [ -n "${HERDR_PANE_ID:-}" ]; then
        PANE_ID="$HERDR_PANE_ID"
        return 0
    fi
    die_usage "pane id が解決できない。--pane を指定するか HERDR_PANE_ID を設定する"
}

# 実投稿。herdr が無ければ何もせず 0。
report() {
    local pane_id="$1"
    shift
    if ! command -v "$HERDR_BIN" >/dev/null 2>&1; then
        return 0
    fi
    "$HERDR_BIN" pane report-metadata "$pane_id" \
        --source "$SOURCE_ID" \
        --ttl-ms "$TTL_MS" \
        "$@" >/dev/null 2>&1 || true
}

cmd_set() {
    local title="" state="working" pane_explicit=""
    if [ "$#" -eq 0 ] || [ "${1:0:2}" = "--" ]; then
        die_usage "set にはタスク名が必要である"
    fi
    title="$1"
    shift
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --state)
                [ "$#" -ge 2 ] || die_usage "--state に値が無い"
                state="$2"
                shift 2
                ;;
            --pane)
                [ "$#" -ge 2 ] || die_usage "--pane に値が無い"
                pane_explicit="$2"
                shift 2
                ;;
            *)
                die_usage "unknown option: $1"
                ;;
        esac
    done

    validate_ttl_ms
    validate_title_max_len
    validate_state "$state"
    resolve_pane_id "$pane_explicit"
    build_state_label_args "$state"

    # 正規化はサブシェルで行い、空判定は親で行う。die_usage の exit を親へ伝える。
    title="$(sanitize_title "$title")"
    [ -n "$title" ] || die_usage "タスク名が空である"

    local short_title
    short_title="$(truncate_title "$title" "$TITLE_MAX_LEN")"

    if [ "${HERDR_AGENT_TASK_DRY_RUN:-}" = "1" ]; then
        printf '%s\tsource\t%s\n' "$PANE_ID" "$SOURCE_ID"
        printf '%s\tttl_ms\t%s\n' "$PANE_ID" "$TTL_MS"
        printf '%s\ttoken\t%s\t%s\n' "$PANE_ID" "$TOKEN_NAME" "$short_title"
        if [ "${STATE_LABEL_ARGS[0]}" = "--clear-state-labels" ]; then
            printf '%s\tclear_state_labels\n' "$PANE_ID"
        else
            local i value
            for ((i = 1; i < ${#STATE_LABEL_ARGS[@]}; i += 2)); do
                value="${STATE_LABEL_ARGS[i]}"
                printf '%s\tstate_label\t%s\n' "$PANE_ID" "$value"
            done
        fi
        return 0
    fi

    report "$PANE_ID" --token "${TOKEN_NAME}=${short_title}" "${STATE_LABEL_ARGS[@]}"
}

cmd_clear() {
    local pane_explicit=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --pane)
                [ "$#" -ge 2 ] || die_usage "--pane に値が無い"
                pane_explicit="$2"
                shift 2
                ;;
            *)
                die_usage "unknown option: $1"
                ;;
        esac
    done

    validate_ttl_ms
    resolve_pane_id "$pane_explicit"

    if [ "${HERDR_AGENT_TASK_DRY_RUN:-}" = "1" ]; then
        printf '%s\tsource\t%s\n' "$PANE_ID" "$SOURCE_ID"
        printf '%s\tclear_token\t%s\n' "$PANE_ID" "$TOKEN_NAME"
        printf '%s\tclear_state_labels\n' "$PANE_ID"
        return 0
    fi

    report "$PANE_ID" --clear-token "$TOKEN_NAME" --clear-state-labels
}

cmd_show() {
    local pane_explicit=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --pane)
                [ "$#" -ge 2 ] || die_usage "--pane に値が無い"
                pane_explicit="$2"
                shift 2
                ;;
            *)
                die_usage "unknown option: $1"
                ;;
        esac
    done

    resolve_pane_id "$pane_explicit"

    if [ "${HERDR_AGENT_TASK_DRY_RUN:-}" = "1" ]; then
        printf '%s\tshow\n' "$PANE_ID"
        return 0
    fi
    command -v "$HERDR_BIN" >/dev/null 2>&1 || return 0
    "$HERDR_BIN" pane get "$PANE_ID"
}

main() {
    [ "$#" -ge 1 ] || die_usage "サブコマンドが必要である"
    local sub="$1"
    shift
    case "$sub" in
        set)   cmd_set "$@" ;;
        clear) cmd_clear "$@" ;;
        show)  cmd_show "$@" ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown subcommand: $sub" ;;
    esac
}

ensure_utf8_locale
main "$@"
