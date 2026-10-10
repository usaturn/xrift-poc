#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIT_CORE="${REPO_ROOT}/.devcontainer/herdr-git-status.bash"
UPDATER="${REPO_ROOT}/.devcontainer/herdr-status-updater.bash"
HERDR_CONFIG="${REPO_ROOT}/.devcontainer/herdr.toml"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    [ "$actual" = "$expected" ] || fail "$msg: expected='$expected' actual='$actual'"
}

make_repo() {
    local dir="$1"
    mkdir -p "$dir"
    git -C "$dir" init -q -b main
    git -C "$dir" config user.email "test@example.com"
    git -C "$dir" config user.name "test"
    printf 'x\n' > "$dir/file.txt"
    git -C "$dir" add file.txt
    git -C "$dir" commit -q -m init
}

test_git_core_states() {
    local repo="${TEST_ROOT}/repo"
    make_repo "$repo"

    # clean
    out="$("$GIT_CORE" "$repo")"
    assert_eq "$out" $'clean\tmain' "clean state"

    # staged
    printf 'y\n' > "$repo/file.txt"
    git -C "$repo" add file.txt
    out="$("$GIT_CORE" "$repo")"
    assert_eq "$out" $'staged\tmain' "staged state"

    # dirty (unstaged after staged content committed? use unstaged only)
    git -C "$repo" commit -q -m staged
    printf 'z\n' > "$repo/file.txt"
    out="$("$GIT_CORE" "$repo")"
    assert_eq "$out" $'dirty\tmain' "dirty state"

    # non-git
    local nongit="${TEST_ROOT}/nongit"
    mkdir -p "$nongit"
    out="$("$GIT_CORE" "$nongit")"
    assert_eq "$out" "" "non-git empty"
    # exit code already enforced by set -e; call again capturing status
    status=0
    "$GIT_CORE" "$nongit" >/dev/null || status=$?
    assert_eq "$status" "0" "non-git exit 0"
}

test_git_core_ahead_indicator() {
    local repo="${TEST_ROOT}/ahead"
    make_repo "$repo"
    git -C "$repo" checkout -q -b feature
    # bare remote
    local remote="${TEST_ROOT}/remote.git"
    git init -q --bare "$remote"
    git -C "$repo" remote add origin "$remote"
    git -C "$repo" push -q -u origin feature
    printf 'ahead\n' >> "$repo/file.txt"
    git -C "$repo" add file.txt
    git -C "$repo" commit -q -m ahead
    out="$("$GIT_CORE" "$repo")"
    assert_eq "$out" $'clean\tfeature ↑' "ahead indicator"
}

# private flock で live daemon と隔離して updater を --once dry-run
run_updater_dry() {
    local snap="$1"
    local lock_dir="${TEST_ROOT}/updater-lock-$$-${RANDOM}"
    mkdir -p "$lock_dir"
    HERDR_STATUS_DRY_RUN=1 \
    HERDR_STATUS_SNAPSHOT_FILE="$snap" \
    HERDR_STATUS_LOCK_DIR="$lock_dir" \
    HERDR_GIT_STATUS="$GIT_CORE" \
    TZ=Asia/Tokyo \
    "$UPDATER" --once
}

write_focused_snap() {
    local snap="$1" ws_id="$2" cwd="$3"
    cat > "$snap" <<EOF
{
  "result": {
    "snapshot": {
      "focused_workspace_id": "$ws_id",
      "focused_pane_id": "${ws_id}:p1",
      "workspaces": [
        {"workspace_id": "$ws_id", "active_tab_id": "${ws_id}:t1", "focused": true, "label": "demo"}
      ],
      "tabs": [
        {"tab_id": "${ws_id}:t1", "workspace_id": "$ws_id", "focused": true}
      ],
      "panes": [
        {
          "pane_id": "${ws_id}:p1",
          "workspace_id": "$ws_id",
          "tab_id": "${ws_id}:t1",
          "cwd": "$cwd",
          "foreground_cwd": "$cwd",
          "focused": true
        }
      ]
    }
  }
}
EOF
}

test_updater_dry_run() {
    [ -x "$UPDATER" ] || fail "updater missing"
    local repo="${TEST_ROOT}/upd-repo"
    make_repo "$repo"
    local snap="${TEST_ROOT}/snapshot.json"
    write_focused_snap "$snap" "w1" "$repo"
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w1\tclock\t' || fail "dry-run missing clock: $out"
    # 状態行は clean|staged|dirty、ブランチ名は git_name に分離
    printf '%s\n' "$out" | rg -q $'w1\tgit_clean\tclean' || fail "dry-run missing git_clean text: $out"
    printf '%s\n' "$out" | rg -q $'w1\tgit_name\tmain' || fail "dry-run missing git_name: $out"
    printf '%s\n' "$out" | rg -q $'w1\tclear_token\tgit_staged' || fail "dry-run missing clear staged"
    printf '%s\n' "$out" | rg -q $'w1\tclear_token\tgit_dirty' || fail "dry-run missing clear dirty"
}

test_updater_dry_run_nongit() {
    [ -x "$UPDATER" ] || fail "updater missing"
    local nongit="${TEST_ROOT}/upd-nongit"
    mkdir -p "$nongit"
    local snap="${TEST_ROOT}/snapshot-nongit.json"
    write_focused_snap "$snap" "w2" "$nongit"
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w2\tclock\t' || fail "nongit dry-run missing clock: $out"
    printf '%s\n' "$out" | rg -q $'w2\tclear_token\tgit_clean' || fail "nongit missing clear git_clean: $out"
    printf '%s\n' "$out" | rg -q $'w2\tclear_token\tgit_staged' || fail "nongit missing clear git_staged: $out"
    printf '%s\n' "$out" | rg -q $'w2\tclear_token\tgit_dirty' || fail "nongit missing clear git_dirty: $out"
    printf '%s\n' "$out" | rg -q $'w2\tclear_token\tgit_name' || fail "nongit missing clear git_name: $out"
    printf '%s\n' "$out" | rg -q $'w2\tgit_' && fail "nongit should not set git tokens: $out" || true
}

test_updater_dry_run_staged() {
    [ -x "$UPDATER" ] || fail "updater missing"
    local repo="${TEST_ROOT}/upd-staged"
    make_repo "$repo"
    printf 'staged\n' > "$repo/file.txt"
    git -C "$repo" add file.txt
    local snap="${TEST_ROOT}/snapshot-staged.json"
    write_focused_snap "$snap" "w3" "$repo"
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w3\tgit_staged\tstaged' || fail "staged missing git_staged text: $out"
    printf '%s\n' "$out" | rg -q $'w3\tgit_name\tmain' || fail "staged missing git_name: $out"
    printf '%s\n' "$out" | rg -q $'w3\tclear_token\tgit_clean' || fail "staged missing clear clean: $out"
    printf '%s\n' "$out" | rg -q $'w3\tclear_token\tgit_dirty' || fail "staged missing clear dirty: $out"
    printf '%s\n' "$out" | rg -q $'w3\tgit_clean\t' && fail "staged should not set git_clean: $out" || true
}

test_updater_dry_run_dirty() {
    [ -x "$UPDATER" ] || fail "updater missing"
    local repo="${TEST_ROOT}/upd-dirty"
    make_repo "$repo"
    printf 'dirty\n' > "$repo/file.txt"
    local snap="${TEST_ROOT}/snapshot-dirty.json"
    write_focused_snap "$snap" "w4" "$repo"
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w4\tgit_dirty\tdirty' || fail "dirty missing git_dirty text: $out"
    printf '%s\n' "$out" | rg -q $'w4\tgit_name\tmain' || fail "dirty missing git_name: $out"
    printf '%s\n' "$out" | rg -q $'w4\tclear_token\tgit_clean' || fail "dirty missing clear clean: $out"
    printf '%s\n' "$out" | rg -q $'w4\tclear_token\tgit_staged' || fail "dirty missing clear staged: $out"
    printf '%s\n' "$out" | rg -q $'w4\tgit_clean\t' && fail "dirty should not set git_clean: $out" || true
}

test_shorten_branch_label() {
    [ -x "$UPDATER" ] || fail "updater missing"
    local repo="${TEST_ROOT}/upd-long-branch"
    make_repo "$repo"
    local snap="${TEST_ROOT}/snapshot-long.json"

    # feat/ は表示から外す
    git -C "$repo" checkout -q -b "feat/herdr-status-datetime-git"
    write_focused_snap "$snap" "w6" "$repo"
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w6\tgit_name\therdr-status-datetime-git' \
        || fail "feat/ prefix should be stripped: $out"
    printf '%s\n' "$out" | rg -q $'w6\tgit_name\tfeat/' \
        && fail "feat/ must not remain in git_name: $out" || true

    # 強制的に短い上限では …suffix
    out="$(
        HERDR_STATUS_BRANCH_MAX_LEN=18 \
        run_updater_dry "$snap"
    )"
    name_line="$(printf '%s\n' "$out" | rg $'w6\tgit_name\t' | head -1)"
    [ -n "$name_line" ] || fail "long branch missing git_name: $out"
    value="${name_line#*$'\tgit_name\t'}"
    [ "${#value}" -le 18 ] || fail "git_name too long under max 18 (${#value}): '$value'"
    printf '%s\n' "$value" | rg -q 'datetime-git|status-datetime' \
        || fail "git_name lost distinctive suffix: '$value'"

    # 短いブランチはそのまま
    git -C "$repo" checkout -q -b "main-short"
    write_focused_snap "$snap" "w6" "$repo"
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w6\tgit_name\tmain-short' \
        || fail "short branch should be untruncated: $out"
}

test_updater_dry_run_cwd_fallback() {
    [ -x "$UPDATER" ] || fail "updater missing"
    local repo="${TEST_ROOT}/upd-fallback"
    make_repo "$repo"
    local snap="${TEST_ROOT}/snapshot-fallback.json"
    # focused pane なし → active_tab の先頭 pane cwd を使う
    cat > "$snap" <<EOF
{
  "result": {
    "snapshot": {
      "workspaces": [
        {"workspace_id": "w5", "active_tab_id": "w5:t1", "focused": true}
      ],
      "panes": [
        {
          "pane_id": "w5:p1",
          "workspace_id": "w5",
          "tab_id": "w5:t1",
          "cwd": "$repo",
          "foreground_cwd": "$repo",
          "focused": false
        }
      ]
    }
  }
}
EOF
    out="$(run_updater_dry "$snap")"
    printf '%s\n' "$out" | rg -q $'w5\tclock\t' || fail "fallback missing clock: $out"
    printf '%s\n' "$out" | rg -q $'w5\tgit_clean\tclean' || fail "fallback missing git_clean text: $out"
    printf '%s\n' "$out" | rg -q $'w5\tgit_name\tmain' || fail "fallback missing git_name: $out"
    printf '%s\n' "$out" | rg -q $'w5\tclear_token\tgit_staged' || fail "fallback missing clear staged"
    printf '%s\n' "$out" | rg -q $'w5\tclear_token\tgit_dirty' || fail "fallback missing clear dirty"
}

# Spaces 行のカスタム token（$ で始まるもの）と、updater が投稿・消去する token が一致する。
# 片方だけ名前を変えると、サイドバーに時刻や git の状態が出なくなる。
test_herdr_toml_spaces_show_the_updater_tokens() {
    local repo="${TEST_ROOT}/upd-tokens"
    make_repo "$repo"
    local snap="${TEST_ROOT}/snapshot-tokens.json"
    write_focused_snap "$snap" "w7" "$repo"
    local managed shown
    managed="$(run_updater_dry "$snap" \
        | awk -F'\t' '$2 == "clear_token" { print $3; next } { print $2 }' | sort -u)"
    [ -n "$managed" ] || fail "herdr-status-updater must post or clear tokens"
    shown="$(spaces_custom_tokens | sort -u)"
    assert_eq "$shown" "$managed" "[ui.sidebar.spaces] custom tokens must match the updater tokens"
}

spaces_custom_tokens() {
    python3 - "$HERDR_CONFIG" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    rows = tomllib.load(handle)["ui"]["sidebar"]["spaces"]["rows"]
for row in rows:
    for cell in row:
        token = cell["token"] if isinstance(cell, dict) else cell
        if token.startswith("$"):
            print(token[1:])
PY
}

test_git_core_states
test_git_core_ahead_indicator
test_updater_dry_run
test_updater_dry_run_nongit
test_updater_dry_run_staged
test_updater_dry_run_dirty
test_shorten_branch_label
test_updater_dry_run_cwd_fallback
test_herdr_toml_spaces_show_the_updater_tokens
printf 'OK test-herdr-status (task1+2+3+4+shorten)\n'
