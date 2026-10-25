#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIT_CORE="${REPO_ROOT}/.devcontainer/herdr-git-status.bash"
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

# このテストが無いと、git status の解釈を誤ったときに、変更のある worktree をサイドバーに clean と黙って表示する。
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
}

# このテストが無いと、未 push の commit がある branch に ↑ が出なくなっても気づけない。
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

test_git_core_states
test_git_core_ahead_indicator
printf 'OK test-herdr-status\n'
