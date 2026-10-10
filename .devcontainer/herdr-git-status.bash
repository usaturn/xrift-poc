#!/bin/bash
# herdr 用の git 状態コア
# 使い方: herdr-git-status.bash <dir>
# 成功時 stdout: <clean|staged|dirty>\t<label>
# 非 git / 失敗: exit 0, stdout 空

set -euo pipefail

readonly DETACHED_PREFIX="detached@"
readonly NAME_REV_UNDEFINED="undefined"
readonly PUSH_INDICATOR_AHEAD=" ↑"
readonly PUSH_INDICATOR_MISSING_UPSTREAM=" !"
readonly GIT_STATE_CLEAN="clean"
readonly GIT_STATE_STAGED="staged"
readonly GIT_STATE_DIRTY="dirty"

dir="${1:-.}"

resolve_named_ref() {
    local refs_glob="$1"
    local prefix="$2"
    local ref_name
    ref_name=$(git name-rev --name-only --exclude='refs/tags/*' --refs="$refs_glob" HEAD 2>/dev/null) || return 1
    if [ -z "$ref_name" ] || [ "$ref_name" = "$NAME_REV_UNDEFINED" ]; then
        return 1
    fi
    printf '%s\n' "${ref_name#"$prefix"}"
}

resolve_branch_name() {
    local branch_name short_hash
    branch_name=$(git symbolic-ref --short HEAD 2>/dev/null) || true
    if [ -n "${branch_name:-}" ]; then
        printf '%s\n' "$branch_name"
        return 0
    fi
    branch_name=$(resolve_named_ref 'refs/remotes/*' 'remotes/') || true
    if [ -n "${branch_name:-}" ]; then
        printf '%s\n' "$branch_name"
        return 0
    fi
    branch_name=$(resolve_named_ref 'refs/heads/*' 'heads/') || true
    if [ -n "${branch_name:-}" ]; then
        printf '%s\n' "$branch_name"
        return 0
    fi
    short_hash=$(git rev-parse --short HEAD 2>/dev/null) || return 1
    printf '%s%s\n' "$DETACHED_PREFIX" "$short_hash"
}

classify_git_state() {
    local porcelain_status line index_status worktree_status
    local has_staged=0 has_dirty=0
    porcelain_status=$(git status --porcelain=v1 --untracked-files=all 2>/dev/null) || return 1
    if [ -z "$porcelain_status" ]; then
        printf '%s\n' "$GIT_STATE_CLEAN"
        return 0
    fi
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        index_status=${line:0:1}
        worktree_status=${line:1:1}
        if [ "$index_status" = "?" ] && [ "$worktree_status" = "?" ]; then
            has_dirty=1
            continue
        fi
        if [ "$index_status" != " " ]; then
            has_staged=1
        fi
        if [ "$worktree_status" != " " ]; then
            has_dirty=1
        fi
    done <<< "$porcelain_status"
    if [ "$has_dirty" -eq 1 ]; then
        printf '%s\n' "$GIT_STATE_DIRTY"
        return 0
    fi
    if [ "$has_staged" -eq 1 ]; then
        printf '%s\n' "$GIT_STATE_STAGED"
        return 0
    fi
    printf '%s\n' "$GIT_STATE_DIRTY"
}

resolve_push_indicator() {
    local head_ref upstream_ref
    head_ref=$(git symbolic-ref -q HEAD 2>/dev/null) || return 0
    upstream_ref=$(git for-each-ref --format='%(upstream:short)' "$head_ref" 2>/dev/null)
    if [ -z "$upstream_ref" ]; then
        return 0
    fi
    if ! git show-ref --verify --quiet "refs/remotes/${upstream_ref}"; then
        printf '%s\n' "$PUSH_INDICATOR_MISSING_UPSTREAM"
        return 0
    fi
    if [ -n "$(git rev-list --max-count=1 "${upstream_ref}..HEAD" 2>/dev/null)" ]; then
        printf '%s\n' "$PUSH_INDICATOR_AHEAD"
    fi
}

cd "$dir" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

branch_name=$(resolve_branch_name) || exit 0
git_state=$(classify_git_state) || exit 0
push_indicator=$(resolve_push_indicator || true)
printf '%s\t%s%s\n' "$git_state" "$branch_name" "${push_indicator:-}"
