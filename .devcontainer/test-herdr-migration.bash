#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZSHRC_TEMPLATE="${REPO_ROOT}/.devcontainer/zshrc.txt"
HERDR_CONFIG="${REPO_ROOT}/.devcontainer/herdr.toml"
INSTALLER="${REPO_ROOT}/.devcontainer/install_beforehand.bash"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
assert_contains() {
    rg -q -- "$2" "$1" || fail "$1 does not contain $2"
}
assert_absent() {
    ! rg -q -i -- "$2" "$1" || fail "$1 must not contain $2"
}
extract_cc_launcher_unsets() {
    sed -n '/^typeset -a cc_launcher_unsets=(/,/^)/p' "$1" \
        | rg -o -- '-u [A-Z0-9_]+' \
        | sort -u \
        | sed 's/^-u //'
}
# zshrc テンプレートを隔離環境で実行する共通部。$@ で追加の VAR=value を渡せる。
run_zshrc_code() {
    local test_home="$1"
    local zsh_code="$2"
    shift 2
    env -u TMUX -u HERDR_ENV -u HERDR_SOCKET_PATH \
        HOME="$test_home" \
        PATH="$test_home/bin:/usr/bin:/bin" \
        ZSHRC_TEMPLATE="$ZSHRC_TEMPLATE" \
        "$@" /usr/bin/zsh -f -c "$zsh_code" </dev/null >/dev/null
}
# zshrc の source に要る starship と、status server --json に答える herdr のスタブを置く。
make_stub_home() {
    local dir="$1"
    mkdir -p "$dir/bin"
    printf '%s\n' '#!/bin/sh' 'exit 0' > "$dir/bin/starship"
    printf '%s\n' '#!/bin/sh' \
        'if [ "${1:-} ${2:-} ${3:-}" = "status server --json" ]; then' \
        '    printf "%s\n" "${HERDR_TEST_STATUS_JSON:-{\"running\":false\}}"' \
        '    exit "${HERDR_TEST_STATUS_RC:-0}"' \
        'fi' \
        > "$dir/bin/herdr"
    chmod +x "$dir/bin/starship" "$dir/bin/herdr"
}

write_session_fixture() {
    local session_file="$1"
    local workspace_root="$2"
    local workspace_dir="$3"
    local worktree_dir="$4"
    mkdir -p "$(dirname "$session_file")" "$workspace_dir" "$worktree_dir"
    python3 - "$session_file" "$workspace_root" "$worktree_dir" <<'PY'
import json
import os
import sys

session_file, workspace_root, worktree_dir = sys.argv[1:]
data = {
    "version": 3,
    "unknown_top_level": {"keep": True},
    "workspaces": [{
        "id": "w1",
        "identity_cwd": "/home/vscode",
        "unknown_workspace_field": "keep",
        "tabs": [{
            "id": "w1:t1",
            "panes": {
                "0": {
                    "cwd": "/deleted/worktree",
                    "agent_session": {"provider": "codex", "id": "resume-me"},
                },
                "1": {"cwd": worktree_dir},
                "2": {"cwd": os.path.join(workspace_root, "deleted-repo")},
            },
        }],
    }],
}
with open(session_file, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
PY
}

write_multi_workspace_fixture() {
    local session_file="$1"
    local workspace_dir="$2"
    mkdir -p "$(dirname "$session_file")" "$workspace_dir"
    python3 - "$session_file" <<'PY'
import json
import sys

session_file = sys.argv[1]
data = {
    "version": 3,
    "workspaces": [
        {
            "id": "w1",
            "identity_cwd": "/home/vscode",
            "tabs": [{"id": "w1:t1", "panes": {"0": {"cwd": "/home/vscode"}}}],
        },
        {
            "id": "w2",
            "identity_cwd": "/home/vscode/other",
            "tabs": [{"id": "w2:t1", "panes": {"0": {"cwd": "/nonexistent/gone"}}}],
        },
    ],
}
with open(session_file, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
PY
}

run_session_repair() {
    local test_home="$1"
    local workspace_root="$2"
    local workspace_dir="$3"
    shift 3
    run_zshrc_code "$test_home" \
        'source "$ZSHRC_TEMPLATE"; herdr_repair_session_cwd "$HERDR_TEST_WORKSPACE"' \
        HERDR_WORKSPACE_ROOT="$workspace_root" \
        HERDR_TEST_WORKSPACE="$workspace_dir" \
        "$@"
}

# このテストが無いと、修復が worktree の pane の cwd・agent_session を書き換える、または herdr が稼働中か分からないときや未知の形式に書き込み、エージェントのセッションが別の場所で再開する・新しい状態を失う。
test_session_cwd_repair() {
    local test_home="${TEST_ROOT}/home-repair"
    local workspace_root="${TEST_ROOT}/workspaces"
    local workspace_dir="${workspace_root}/maatlog"
    local worktree_dir="${workspace_dir}/.worktrees/feature"
    local session_file="${test_home}/.config/herdr/session.json"
    make_stub_home "$test_home"

    write_session_fixture "$session_file" "$workspace_root" "$workspace_dir" "$worktree_dir"
    run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
        HERDR_TEST_STATUS_JSON='{"running":false}'
    python3 - "$session_file" "$workspace_dir" "$worktree_dir" <<'PY'
import json
import sys

session_file, workspace_dir, worktree_dir = sys.argv[1:]
with open(session_file, encoding="utf-8") as handle:
    data = json.load(handle)
workspace = data["workspaces"][0]
panes = workspace["tabs"][0]["panes"]
assert workspace["identity_cwd"] == workspace_dir
assert panes["0"]["cwd"] == workspace_dir
assert panes["1"]["cwd"] == worktree_dir
assert panes["2"]["cwd"] == workspace_dir
assert panes["0"]["agent_session"] == {"provider": "codex", "id": "resume-me"}
assert workspace["unknown_workspace_field"] == "keep"
assert data["unknown_top_level"] == {"keep": True}
PY

    # 稼働中・未知の形式・status の失敗・running キーの欠落では書かない
    local status_case
    for status_case in running_true unknown_version command_failure missing_running; do
        write_session_fixture "$session_file" "$workspace_root" "$workspace_dir" "$worktree_dir"
        if [ "$status_case" = unknown_version ]; then
            python3 - "$session_file" <<'PY'
import json
import sys

session_file = sys.argv[1]
with open(session_file, encoding="utf-8") as handle:
    data = json.load(handle)
data["version"] = 4
with open(session_file, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
PY
        fi
        cp "$session_file" "${session_file}.before"
        case "$status_case" in
            running_true)
                run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
                    HERDR_TEST_STATUS_JSON='{"running":true}'
                ;;
            unknown_version)
                run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
                    HERDR_TEST_STATUS_JSON='{"running":false}'
                ;;
            command_failure)
                run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
                    HERDR_TEST_STATUS_JSON='{"running":false}' HERDR_TEST_STATUS_RC=1
                ;;
            missing_running)
                run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
                    HERDR_TEST_STATUS_JSON='{"status":"unknown"}'
                ;;
        esac
        cmp -s "$session_file" "${session_file}.before" \
            || fail "$status_case must leave session unchanged"
    done
}

# このテストが無いと、修復が複数のワークスペースを同じ identity_cwd に収束させる・健全な兄弟の pane を今のワークスペースへ移し、別のプロジェクトのエージェントのセッションが今のリポジトリで再開する。
test_session_cwd_repair_multi_workspace() {
    local test_home="${TEST_ROOT}/home-repair-multi"
    local workspace_root="${TEST_ROOT}/workspaces-multi"
    local workspace_dir="${workspace_root}/maatlog"
    local session_file="${test_home}/.config/herdr/session.json"
    make_stub_home "$test_home"

    # 修復先をまだ誰も持たない場合: 先頭の壊れたエントリだけを修復し、2 件目は触らない
    write_multi_workspace_fixture "$session_file" "$workspace_dir"
    run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
        HERDR_TEST_STATUS_JSON='{"running":false}'
    python3 - "$session_file" "$workspace_dir" <<'PY'
import json
import sys

session_file, workspace_dir = sys.argv[1:]
with open(session_file, encoding="utf-8") as handle:
    data = json.load(handle)
w1, w2 = data["workspaces"]
assert w1["identity_cwd"] == workspace_dir
assert w1["tabs"][0]["panes"]["0"]["cwd"] == workspace_dir
assert w2["identity_cwd"] == "/home/vscode/other"
assert w2["tabs"][0]["panes"]["0"]["cwd"] == "/nonexistent/gone"
PY

    # 修復先を既に identity_cwd に持つエントリがある場合: 壊れたエントリは無変更
    python3 - "$session_file" "$workspace_dir" <<'PY'
import json
import sys

session_file, workspace_dir = sys.argv[1:]
data = {
    "version": 3,
    "workspaces": [
        {"id": "w1", "identity_cwd": workspace_dir, "tabs": []},
        {
            "id": "w2",
            "identity_cwd": "/home/vscode/other",
            "tabs": [{"id": "w2:t1", "panes": {"0": {"cwd": "/nonexistent/gone"}}}],
        },
    ],
}
with open(session_file, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
PY
    cp "$session_file" "${session_file}.before"
    run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
        HERDR_TEST_STATUS_JSON='{"running":false}'
    cmp -s "$session_file" "${session_file}.before" \
        || fail 'workspace entry must remain unchanged when repair target is taken'

    # 健全な兄弟 identity + 壊れた pane: pane は兄弟 identity へ。起動中 workspace へ流れない
    local other_dir="${workspace_root}/other-project"
    mkdir -p "$other_dir"
    python3 - "$session_file" "$workspace_dir" "$other_dir" <<'PY'
import json
import sys

session_file, workspace_dir, other_dir = sys.argv[1:]
data = {
    "version": 3,
    "workspaces": [
        {
            "id": "w-other",
            "identity_cwd": other_dir,
            "tabs": [{
                "id": "w-other:t1",
                "panes": {
                    "0": {
                        "cwd": "/home/vscode",
                        "agent_session": {"provider": "codex", "id": "keep-me"},
                    },
                },
            }],
        },
        {
            "id": "w-current",
            "identity_cwd": workspace_dir,
            "tabs": [{"id": "w-current:t1", "panes": {"0": {"cwd": workspace_dir}}}],
        },
    ],
}
with open(session_file, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
PY
    run_session_repair "$test_home" "$workspace_root" "$workspace_dir" \
        HERDR_TEST_STATUS_JSON='{"running":false}'
    python3 - "$session_file" "$workspace_dir" "$other_dir" <<'PY'
import json
import sys

session_file, workspace_dir, other_dir = sys.argv[1:]
with open(session_file, encoding="utf-8") as handle:
    data = json.load(handle)
other, current = data["workspaces"]
assert other["identity_cwd"] == other_dir
assert other["tabs"][0]["panes"]["0"]["cwd"] == other_dir
assert other["tabs"][0]["panes"]["0"]["agent_session"] == {
    "provider": "codex",
    "id": "keep-me",
}
assert current["identity_cwd"] == workspace_dir
assert current["tabs"][0]["panes"]["0"]["cwd"] == workspace_dir
assert other["tabs"][0]["panes"]["0"]["cwd"] != workspace_dir
PY
}


# install_beforehand.bash から、指定したラベルの step 1 つ分（次の step の手前まで）を
# 取り出し、一時 HOME で実行する。
run_installer_step() {
    local home="$1" label="$2"
    shift 2
    local fragment="${home}/installer-step.bash"
    mkdir -p "$home"
    {
        printf '%s\n' 'step() { :; }' 'fail() { printf "%s\n" "$1" >&2; exit 1; }'
        awk -v start="step \"${label}\"" \
            '$0 == start { copy = 1; next } copy && /^step "/ { exit } copy' "$INSTALLER"
    } > "$fragment"
    [ "$(wc -l < "$fragment")" -gt 2 ] || fail "installer step not found: ${label}"
    (
        cd "$REPO_ROOT"
        env HOME="$home" XDG_CONFIG_HOME="${home}/.config" WORKSPACE_ROOT="$REPO_ROOT" \
            PATH="${home}/stub-bin:${home}/.local/bin:/usr/bin:/bin" \
            "$@" bash "$fragment"
    )
}

# Herdr の導入（curl | sh）を、~/.local/bin に herdr のスタブを置く処理に差し替える。
make_herdr_installer_stub() {
    local home="$1"
    mkdir -p "${home}/stub-bin"
    printf '%s\n' '#!/bin/sh' 'cat "$FAKE_HERDR_INSTALLER"' > "${home}/stub-bin/curl"
    chmod +x "${home}/stub-bin/curl"
    printf '%s\n' \
        'mkdir -p "$HOME/.local/bin"' \
        "printf '%s\\n' '#!/bin/sh' 'echo herdr-test' > \"\$HOME/.local/bin/herdr\"" \
        'chmod +x "$HOME/.local/bin/herdr"' \
        > "${home}/herdr-install.sh"
}


# このテストが無いと、導入を同じコンテナで再実行したときに、利用者が編集した ~/.config/herdr/config.toml を黙って上書きする。
test_installer_keeps_an_existing_herdr_config() {
    local home config
    home="${TEST_ROOT}/installer-herdr"
    config="${home}/.config/herdr/config.toml"
    make_herdr_installer_stub "$home"
    # 対照: config が無ければ雛形を置く（手順が実際に動いたことを確かめる）
    run_installer_step "$home" 'Installing Herdr' \
        FAKE_HERDR_INSTALLER="${home}/herdr-install.sh" >/dev/null \
        || fail 'Installing Herdr must succeed with a working installer'
    cmp -s "$HERDR_CONFIG" "$config" || fail 'installer must place herdr.toml when no config exists'
    printf '%s\n' '# user config' > "$config"
    run_installer_step "$home" 'Installing Herdr' \
        FAKE_HERDR_INSTALLER="${home}/herdr-install.sh" >/dev/null \
        || fail 'Installing Herdr must succeed with an existing config'
    [ "$(cat "$config")" = '# user config' ] || fail 'installer must keep an existing herdr config'
}

# このテストが無いと、ccdd・ccddg が共有の unset の一覧を使わなくなったときに、親シェルの Anthropic の API key・OAuth token などが DeepInfra 向けの子の claude へ黙って渡る。
test_cc_launcher_env_unsets() {
    local test_home="${TEST_ROOT}/home-cc-launcher"
    make_stub_home "$test_home"
    local dump_dir="${TEST_ROOT}/cc-launcher-env"
    mkdir -p "$dump_dir"

    printf '%s\n' \
        '#!/bin/sh' \
        'env | sort >> "$HERDR_TEST_CLAUDE_DUMP"' \
        > "$test_home/bin/claude"
    chmod +x "$test_home/bin/claude"

    local -a unset_vars
    mapfile -t unset_vars < <(extract_cc_launcher_unsets "$ZSHRC_TEMPLATE")
    # 一覧が空だと、何も汚染しないまま下の確認が通る
    [ "${#unset_vars[@]}" -gt 0 ] || fail 'cc_launcher_unsets must not be empty'

    local -a env_args=(DEEPINFRA_TOKEN=fake-di)
    local v
    for v in "${unset_vars[@]}"; do
        if [ "$v" = DEEPINFRA_TOKEN ]; then
            continue
        fi
        env_args+=("${v}=POLLUTED_${v}")
    done

    local fn log
    for fn in ccdd ccddg; do
        log="${dump_dir}/${fn}.log"
        : > "$log"
        run_zshrc_code "$test_home" \
            "source \"\$ZSHRC_TEMPLATE\" >/dev/null 2>&1; ${fn} probe" \
            HERDR_TEST_CLAUDE_DUMP="$log" \
            "${env_args[@]}"
        assert_absent "$log" 'POLLUTED_'
        # 対照: 子の claude が実際に起動し、自分の割り当て値を受け取った
        assert_contains "$log" '^ANTHROPIC_AUTH_TOKEN=fake-di$'
    done
}

test_installer_keeps_an_existing_herdr_config
test_session_cwd_repair
test_session_cwd_repair_multi_workspace
test_cc_launcher_env_unsets
printf 'PASS: herdr Dev Container migration\n'
