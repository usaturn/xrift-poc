#!/bin/bash
# herdr Spaces 向け status updater
# snapshot から workspace cwd を解決し、git / clock を report-metadata で投稿する。
# HERDR_STATUS_DRY_RUN=1 のときは投稿せず TSV を stdout に出す。

set -euo pipefail

SOURCE_ID="herdr-status"
INTERVAL_SEC="${HERDR_STATUS_INTERVAL_SEC:-15}"
# TTL は長めに（daemon が一時停止しても Spaces がすぐ空にならない）
# 投稿自体は INTERVAL ごとに更新される
GIT_TTL_MS="${HERDR_STATUS_GIT_TTL_MS:-1800000}"
CLOCK_TTL_MS="${HERDR_STATUS_CLOCK_TTL_MS:-1800000}"
# HERDR_STATUS_LOCK_DIR で上書き可（テスト隔離用）。未設定時は従来どおり runtime 既定。
LOCK_DIR="${HERDR_STATUS_LOCK_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/herdr-status-updater}"
LOCK_FILE="${LOCK_DIR}/lock"
LOG_FILE="${HERDR_STATUS_LOG:-${HOME}/.config/herdr/herdr-status-updater.log}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_CORE="${HERDR_GIT_STATUS:-$script_dir/herdr-git-status.bash}"
HERDR_BIN="${HERDR_BIN:-herdr}"

ONCE=0
DAEMON=0
for arg in "$@"; do
    case "$arg" in
        --once) ONCE=1 ;;
        --daemon) DAEMON=1 ;;
    esac
done

log_debug() {
    if [ "${HERDR_STATUS_DEBUG:-}" = "1" ]; then
        printf '%s\n' "$*" >&2
    fi
}

log_info() {
    local msg="$*"
    # daemon 時はファイルへ（親 nohup で stdout を捨てるため）
    if [ -n "${LOG_FILE:-}" ]; then
        mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
        printf '%s %s\n' "$(date -Iseconds 2>/dev/null || date)" "$msg" >>"$LOG_FILE" 2>/dev/null || true
    fi
    log_debug "$msg"
}

export TZ="${TZ:-Asia/Tokyo}"

mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    # 既に updater が動いている
    exit 0
fi

if [ "$DAEMON" -eq 1 ] && [ "$ONCE" -eq 0 ] && [ "${HERDR_STATUS_DRY_RUN:-}" != "1" ]; then
    # バックグラウンド化（ロックは親が持ったままにしない）
    if [ -z "${HERDR_STATUS_DAEMONIZED:-}" ]; then
        flock -u 9 || true
        mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
        # setsid で端末セッションから切り離し、SIGHUP で死ににくくする
        if command -v setsid >/dev/null 2>&1; then
            HERDR_STATUS_DAEMONIZED=1 setsid -f "$0" "$@" >>"$LOG_FILE" 2>&1
        else
            HERDR_STATUS_DAEMONIZED=1 nohup "$0" "$@" >>"$LOG_FILE" 2>&1 &
            disown 2>/dev/null || true
        fi
        exit 0
    fi
fi

# daemon 子は再度 flock
if [ "${HERDR_STATUS_DAEMONIZED:-}" = "1" ]; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log_info "daemon child could not acquire lock; exiting"
        exit 0
    fi
    log_info "daemon started pid=$$ interval=${INTERVAL_SEC}s git_ttl_ms=${GIT_TTL_MS}"
    # ループ中はエラーでプロセスを落とさない
    set +e
    trap 'log_info "daemon exiting pid=$$ status=$?"' EXIT
fi

get_snapshot_json() {
    if [ -n "${HERDR_STATUS_SNAPSHOT_FILE:-}" ]; then
        cat "$HERDR_STATUS_SNAPSHOT_FILE"
        return 0
    fi
    command -v "$HERDR_BIN" >/dev/null 2>&1 || return 1
    "$HERDR_BIN" api snapshot 2>/dev/null
}

# stdout: workspace_id \t cwd（cwd は空可）
list_workspace_cwds() {
    python3 -c '
import json, sys

raw = json.load(sys.stdin)
snap = raw.get("result", raw).get("snapshot", raw.get("snapshot", raw))
if not isinstance(snap, dict):
    sys.exit(0)

workspaces = snap.get("workspaces") or []
panes = snap.get("panes") or []

def pick_cwd(pane):
    if not pane:
        return ""
    fc = pane.get("foreground_cwd")
    if fc:
        return str(fc)
    c = pane.get("cwd")
    return str(c) if c else ""

for w in workspaces:
    if not isinstance(w, dict):
        continue
    wid = w.get("workspace_id") or ""
    if not wid:
        continue
    w_panes = [
        p for p in panes
        if isinstance(p, dict) and (p.get("workspace_id") or "") == wid
    ]
    cwd = ""
    focused = [p for p in w_panes if p.get("focused")]
    if focused:
        cwd = pick_cwd(focused[0])
    else:
        active_tab = w.get("active_tab_id") or ""
        tab_panes = [
            p for p in w_panes
            if (p.get("tab_id") or "") == active_tab
        ]
        if tab_panes:
            tab_focused = [p for p in tab_panes if p.get("focused")]
            cwd = pick_cwd(tab_focused[0] if tab_focused else tab_panes[0])
    print(f"{wid}\t{cwd}")
'
}

format_clock() {
    date '+%m/%d(%a) %H:%M'
}

# dry-run / live 共通の 1 操作出力
# kind: token | clear_token
# name: clock | git_clean | git_staged | git_dirty
# value: token 値（clear 時は無視）
emit_or_report() {
    local ws_id="$1" kind="$2" name="$3" value="${4:-}" ttl_ms="${5:-$GIT_TTL_MS}"
    if [ "${HERDR_STATUS_DRY_RUN:-}" = "1" ]; then
        if [ "$kind" = "clear_token" ]; then
            printf '%s\tclear_token\t%s\n' "$ws_id" "$name"
        else
            printf '%s\t%s\t%s\n' "$ws_id" "$name" "$value"
        fi
        return 0
    fi
    if ! command -v "$HERDR_BIN" >/dev/null 2>&1; then
        return 0
    fi
    if [ "$kind" = "clear_token" ]; then
        "$HERDR_BIN" workspace report-metadata "$ws_id" \
            --source "$SOURCE_ID" \
            --ttl-ms "$ttl_ms" \
            --clear-token "$name" \
            >/dev/null 2>&1 || true
    else
        "$HERDR_BIN" workspace report-metadata "$ws_id" \
            --source "$SOURCE_ID" \
            --ttl-ms "$ttl_ms" \
            --token "${name}=${value}" \
            >/dev/null 2>&1 || true
    fi
}

# 状態行は短い ASCII 語（色は herdr.toml）。Unicode 記号は端末/UI で消えて見えることがある。
# ブランチ名は別トークン git_name（別行）。

# git 関連トークンを batch 投稿
# tokens は "name=value" の配列、clears は clear:name
report_git_tokens() {
    local ws_id="$1"
    shift
    local -a tokens=()
    local -a clears=()
    local arg
    for arg in "$@"; do
        case "$arg" in
            clear:*)
                clears+=("${arg#clear:}")
                ;;
            *=*)
                tokens+=("$arg")
                ;;
            *)
                log_debug "report_git_tokens: skip arg $arg"
                ;;
        esac
    done

    if [ "${HERDR_STATUS_DRY_RUN:-}" = "1" ]; then
        local t c name value
        for t in "${tokens[@]}"; do
            name="${t%%=*}"
            value="${t#*=}"
            printf '%s\t%s\t%s\n' "$ws_id" "$name" "$value"
        done
        for c in "${clears[@]}"; do
            printf '%s\tclear_token\t%s\n' "$ws_id" "$c"
        done
        return 0
    fi

    if ! command -v "$HERDR_BIN" >/dev/null 2>&1; then
        return 0
    fi

    local args=(workspace report-metadata "$ws_id" --source "$SOURCE_ID" --ttl-ms "$GIT_TTL_MS")
    local t c name value
    for t in "${tokens[@]}"; do
        args+=(--token "$t")
    done
    for c in "${clears[@]}"; do
        args+=(--clear-token "$c")
    done
    if "$HERDR_BIN" "${args[@]}" >/dev/null 2>&1; then
        return 0
    fi
    # 複数フラグ拒否時: 1 操作ずつ
    for t in "${tokens[@]}"; do
        name="${t%%=*}"
        value="${t#*=}"
        emit_or_report "$ws_id" token "$name" "$value" "$GIT_TTL_MS"
    done
    for c in "${clears[@]}"; do
        emit_or_report "$ws_id" clear_token "$c" "" "$GIT_TTL_MS"
    done
}

# サイドバーに収まるようブランチ表示を短縮する。
# 1) Conventional な type/ 接頭辞を落とす（feat/ fix/ ...）
# 2) なお長い場合のみ max 長で …suffix
# HERDR_STATUS_BRANCH_MAX_LEN で上書き可（既定 40）。
BRANCH_MAX_LEN="${HERDR_STATUS_BRANCH_MAX_LEN:-40}"

# label は「ブランチ名 + 任意の push 指標（ ↑ /  !）」
# 戻り値: 表示用文字列（指標は末尾に保持）
shorten_branch_label() {
    local label="$1"
    local max_len="${2:-$BRANCH_MAX_LEN}"
    local name indicator="" short leaf head rest

    if [ -z "$label" ]; then
        printf '\n'
        return 0
    fi
    # max_len が異常ならそのまま
    if ! [[ "$max_len" =~ ^[0-9]+$ ]] || [ "$max_len" -lt 4 ]; then
        printf '%s\n' "$label"
        return 0
    fi

    case "$label" in
        *" ↑")
            name="${label% ↑}"
            indicator=" ↑"
            ;;
        *" !")
            name="${label% !}"
            indicator=" !"
            ;;
        *)
            name="$label"
            ;;
    esac

    # feat/fix/chore など conventional 接頭辞を表示から外す
    # 例: feat/herdr-status-datetime-git → herdr-status-datetime-git
    case "$name" in
        feat/* | feature/* | fix/* | bugfix/* | chore/* | docs/* | doc/* | \
        refactor/* | test/* | tests/* | ci/* | build/* | perf/* | style/* | \
        revert/* | hotfix/* | release/*)
            rest="${name#*/}"
            if [ -n "$rest" ]; then
                name="$rest"
            fi
            ;;
    esac

    # 指標分を除いた上限
    local budget=$max_len
    if [ -n "$indicator" ]; then
        budget=$((max_len - ${#indicator}))
        if [ "$budget" -lt 3 ]; then
            budget=3
        fi
    fi

    if [ "${#name}" -le "$budget" ]; then
        printf '%s%s\n' "$name" "$indicator"
        return 0
    fi

    # path が残っていれば末尾セグメントを優先
    leaf="${name##*/}"
    if [ "$leaf" != "$name" ] && [ "${#leaf}" -le "$budget" ] && [ -n "$leaf" ]; then
        printf '%s%s\n' "$leaf" "$indicator"
        return 0
    fi

    # 末尾優先の省略
    short="…${name: -$((budget - 1))}"
    printf '%s%s\n' "$short" "$indicator"
}

# 非 git: 状態3種 + ブランチ名をすべて clear
report_git_absent() {
    local ws_id="$1"
    report_git_tokens "$ws_id" \
        clear:git_clean clear:git_staged clear:git_dirty clear:git_name
}

# git あり: 状態行は clean|staged|dirty、ブランチは git_name（短縮済み）
report_git_present() {
    local ws_id="$1" state_token="$2" branch_label="$3" state_text="$4"
    local display_name
    display_name="$(shorten_branch_label "$branch_label")"

    case "$state_token" in
        git_clean)
            report_git_tokens "$ws_id" \
                "git_clean=${state_text}" \
                "git_name=${display_name}" \
                clear:git_staged clear:git_dirty
            ;;
        git_staged)
            report_git_tokens "$ws_id" \
                "git_staged=${state_text}" \
                "git_name=${display_name}" \
                clear:git_clean clear:git_dirty
            ;;
        git_dirty)
            report_git_tokens "$ws_id" \
                "git_dirty=${state_text}" \
                "git_name=${display_name}" \
                clear:git_clean clear:git_staged
            ;;
        *)
            report_git_absent "$ws_id"
            ;;
    esac
}

report_clock() {
    local ws_id="$1" clock="$2"
    if [ -n "$clock" ]; then
        if [ "${HERDR_STATUS_DRY_RUN:-}" = "1" ]; then
            printf '%s\tclock\t%s\n' "$ws_id" "$clock"
            return 0
        fi
        if ! command -v "$HERDR_BIN" >/dev/null 2>&1; then
            return 0
        fi
        "$HERDR_BIN" workspace report-metadata "$ws_id" \
            --source "$SOURCE_ID" \
            --ttl-ms "$CLOCK_TTL_MS" \
            --token "clock=${clock}" \
            >/dev/null 2>&1 || true
    else
        emit_or_report "$ws_id" clear_token clock "" "$CLOCK_TTL_MS"
    fi
}

emit_for_workspace() {
    local ws_id="$1" cwd="$2" clock="$3"
    local git_line state label

    report_clock "$ws_id" "$clock"

    git_line=""
    if [ -n "$cwd" ] && [ -x "$GIT_CORE" ]; then
        git_line="$("$GIT_CORE" "$cwd" 2>/dev/null || true)"
    fi

    if [ -z "$git_line" ]; then
        report_git_absent "$ws_id"
        return 0
    fi

    state="${git_line%%$'\t'*}"
    label="${git_line#*$'\t'}"

    case "$state" in
        clean)
            report_git_present "$ws_id" git_clean "$label" "clean"
            ;;
        staged)
            report_git_present "$ws_id" git_staged "$label" "staged"
            ;;
        dirty)
            report_git_present "$ws_id" git_dirty "$label" "dirty"
            ;;
        *)
            log_debug "unknown git state: $state"
            report_git_absent "$ws_id"
            ;;
    esac
}

run_cycle() {
    local snap clock line ws_id cwd
    snap=$(get_snapshot_json) || {
        log_debug "snapshot unavailable"
        return 0
    }
    [ -n "$snap" ] || {
        log_debug "snapshot empty"
        return 0
    }

    clock=$(format_clock 2>/dev/null || true)
    clock="${clock:-}"

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        ws_id="${line%%$'\t'*}"
        cwd="${line#*$'\t'}"
        # タブ無し行は cwd 空
        if [ "$ws_id" = "$line" ]; then
            cwd=""
        fi
        [ -n "$ws_id" ] || continue
        emit_for_workspace "$ws_id" "$cwd" "$clock"
    done < <(printf '%s' "$snap" | list_workspace_cwds)
}

cycle_n=0
while true; do
    cycle_n=$((cycle_n + 1))
    if ! run_cycle; then
        log_info "run_cycle failed cycle=${cycle_n}"
    elif [ "$((cycle_n % 20))" -eq 0 ]; then
        # 約 5 分ごと（15s * 20）に heartbeat
        log_info "heartbeat cycle=${cycle_n} pid=$$"
    fi
    if [ "$ONCE" -eq 1 ]; then
        exit 0
    fi
    sleep "$INTERVAL_SEC" || sleep 15
done
