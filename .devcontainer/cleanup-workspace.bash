#!/bin/bash
#
# .devcontainer/cleanup-workspace.bash
#
# ディスクを圧迫する一時ファイルを安全に掃除する。
#   - /tmp/pr-review-* は helper が作ったセッションだけを対象とし、保存済みレビューと
#     pinned worktree を helper が厳密に検証できる場合に限り reaper CLI 経由で削除する。
#     名前や経過日数だけでは削除しない。未知・不完全なセッションは理由とパスを報告して残す。
#   - loose な pr-review-* ファイルは出所を検証できないため、名前に一致しても削除せず保護を報告する。
#   - /tmp/review-fix-planning.XXXXXX の残骸は汎用 cleanup で安全条件を確認して掃除する。
#     公式 mktemp のドット区切り（review-fix-planning.*）のみ対象。ハイフン区切りの歴史的残骸
#     （-wt / -head.txt 等）は対象外で手動対応。
#   - yarn キャッシュ（#147 で npm に一本化したため不要）
#
# 汎用 cleanup の安全条件（Issue #152 共有仕様 §4。すべて AND で判断する）:
#   - 使用中（プロセスの cwd）と判定できるものは削除しない
#   - 中のどれか（最上位のディレクトリ自身を含む）が MIN_AGE_DAYS 日以内に更新されたものは削除しない
#     （最上位の mtime は、サブディレクトリの中への書き込みや既存のファイルの書き直しでは変わらない）
#   - 経過日数のみを削除条件にしない
#   - 中の git 作業ツリー（.git）は深さに関係なく探し、すべて検査してから撤去する。
#     1 つでも次に当たれば、何も撤去せずに残す
#     - 未コミット変更がある、または git 状態（.git の一覧を含む）を判定できない
#     - items/・worlds/ にデータ（親で無視したファイルを含む）がある
#       （AGENTS.md「items/・worlds/ は片付けの対象にしない」。空のディレクトリだけなら対象にする）
#   - 冪等に何度でも実行できる。削除した対象と件数を必ず出力する
#   - git worktree として登録済みのものは、安全条件を満たす場合に限り
#     `git worktree remove --force`（1 回。locked worktree は外さない）で撤去し、管理状態と整合させる。
#     --force は dirty も消すため、status --porcelain --untracked-files=normal（利用者の
#     status.showUntrackedFiles に左右されない）が成功し空のときだけ呼ぶ。
#     status 失敗は判定不能として残す。
#   - pr-review helper セッションは独自の rm -rf/worktree 操作をせず、helper の strict cleanup に任せる。
#
# verify.sh のログ rotation はこのスクリプトの対象外（#150 の責務）。
#
# 環境変数（既定値はコンテナの実運用、テストでは差し替える）:
#   XRIFT_POC_CLEANUP_TMP_ROOT       pr-review-* / review-fix-planning.* を探すルート（既定 /tmp）
#   XRIFT_POC_CLEANUP_MIN_AGE_DAYS   最終更新からの最小経過日数（review-fix-planning.* は中のどれかの
#                                  最終更新から）。0 以下で無効化（既定 1）
#   XRIFT_POC_CLEANUP_YARN_CACHE_DIR yarn キャッシュのパス（既定 $HOME/.cache/yarn）
#   XRIFT_POC_CLEANUP_DRY_RUN        1 で実際には削除・git worktree remove をせず
#                                  対象だけ表示する（既定 0）

set -u

readonly TMP_ROOT="${XRIFT_POC_CLEANUP_TMP_ROOT:-/tmp}"
readonly MIN_AGE_DAYS="${XRIFT_POC_CLEANUP_MIN_AGE_DAYS:-1}"
readonly YARN_CACHE_DIR="${XRIFT_POC_CLEANUP_YARN_CACHE_DIR:-${HOME:-/root}/.cache/yarn}"
readonly DRY_RUN="${XRIFT_POC_CLEANUP_DRY_RUN:-0}"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")" || {
    echo "cannot resolve cleanup script path; refusing workspace cleanup" >&2
    exit 1
}
if [ -z "$SCRIPT_PATH" ] || [ ! -f "$SCRIPT_PATH" ]; then
    echo "cleanup script target is unavailable; refusing workspace cleanup" >&2
    exit 1
fi
SCRIPT_ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd -P)" || {
    echo "cannot resolve cleanup workspace root; refusing workspace cleanup" >&2
    exit 1
}
readonly SCRIPT_PATH SCRIPT_ROOT
readonly PR_REVIEW_HELPER="${SCRIPT_ROOT}/.agents/skills/pr-review/scripts/pr_review_workspace.py"

# $1 配下に実行中プロセスの cwd があれば真を返す（現在使用中の判定）。
path_is_in_use() {
    local dir="$1" target proc_cwd link
    target="$(cd "$dir" 2>/dev/null && pwd -P)" || return 1
    [ -n "$target" ] || return 1
    for proc_cwd in /proc/[0-9]*/cwd; do
        [ -e "$proc_cwd" ] || continue
        link="$(readlink -f "$proc_cwd" 2>/dev/null)" || continue
        case "$link" in
            "$target" | "$target"/*) return 0 ;;
        esac
    done
    return 1
}

# $1 が MIN_AGE_DAYS 以上前に更新されていれば真を返す。MIN_AGE_DAYS <= 0 なら常に真。
dir_is_old_enough() {
    local dir="$1"
    if [ "$MIN_AGE_DAYS" -le 0 ] 2>/dev/null; then
        return 0
    fi
    local threshold=$((MIN_AGE_DAYS - 1))
    [ -n "$(find "$dir" -maxdepth 0 -mtime "+${threshold}" 2>/dev/null)" ]
}

# $1 の配下（$1 自身を含む）のどれも MIN_AGE_DAYS 日以内に更新されていなければ真を返す。
# MIN_AGE_DAYS <= 0 なら常に真。最上位の mtime は、サブディレクトリの中への書き込みや既存の
# ファイルの書き直しでは変わらないので、使っている最中の作業領域を古いと見誤らないよう配下も見る。
# find が配下を辿りきれないとき（権限など）は判定できないので、偽を返して残す。
tree_is_old_enough() {
    local dir="$1" recent
    if [ "$MIN_AGE_DAYS" -le 0 ] 2>/dev/null; then
        return 0
    fi
    recent="$(find "$dir" -mtime "-${MIN_AGE_DAYS}" -print -quit 2>/dev/null)" || return 1
    [ -z "$recent" ]
}

# $1（basename）から PR 番号を取り出して stdout に出す。
# `pr-review-<数字>` の直後が 行末・`-`・`.` のいずれかであるものだけを受け付ける。
# 取り出せなければ 1 を返す（安全側: 対象外にする）。
pr_number_of() {
    local base="$1"
    if [[ "$base" =~ ^pr-review-([0-9]+)([-.].*)?$ ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

# $1（絶対パス、実パス化済み）が自分自身のリポジトリの
# `git worktree list --porcelain` に登録されていれば真を返す。
# git worktree list は常に自分自身を含むため、単純な grep ではなく
# 一覧を実パスに正規化して自分と突き合わせる。
worktree_is_registered() {
    local real_gitdir="$1" registered_paths wp real_wp
    registered_paths="$(git -C "$real_gitdir" worktree list --porcelain 2>/dev/null \
        | awk '/^worktree /{ $1=""; sub(/^ /, ""); print }')"
    while IFS= read -r wp; do
        [ -n "$wp" ] || continue
        real_wp="$(cd "$wp" 2>/dev/null && pwd -P)" || continue
        if [ "$real_wp" = "$real_gitdir" ]; then
            return 0
        fi
    done <<<"$registered_paths"
    return 1
}

# 登録済み worktree $1（絶対パス）を `git worktree remove --force` で撤去する。
# --force は 1 回だけ（locked は残す）。呼び出し元が porcelain 成功かつ空を保証する。
release_registered_worktree() {
    local wt="$1" err
    if [ "$DRY_RUN" = "1" ]; then
        echo "dry-run: would run: git worktree remove --force ${wt}"
        return 0
    fi
    if err="$(git -C "$wt" worktree remove --force "$wt" 2>&1)"; then
        echo "released registered worktree via git worktree remove: ${wt}"
        return 0
    fi
    echo "failed to release worktree (kept as-is): ${wt}: ${err}" >&2
    return 1
}

# $1 配下に見つかった git 作業ツリーを、深さに関係なくすべて検査してから撤去する。
# 次のどれかが 1 つでもあれば、何も撤去せずに 1 を返す（呼び出し元はディレクトリ全体を削除しない）。
#   - .git の一覧を確かめられない、git 状態が判定不能、または未コミット変更がある
#   - items/・worlds/ にデータ（無視されたファイルを含む）がある
# すべての検査を通ったら、登録済みでクリーンなものを内側から git worktree remove で撤去する
# （失敗したら 1 を返す）。未登録でクリーン（既に手動 remove 済みの抜け殻）なものは何もしない。
# すべて安全に処理できたら 0 を返す（この後ディレクトリ全体を rm -rf してよい）。
resolve_git_worktrees_inside() {
    local dir="$1" git_entry gitdir real_gitdir status_out product_out i
    local -a git_entries=() releasable=()

    # 削除・remove で配下を変更しながら同時に find で辿ると事故りやすいため、
    # 対象一覧を先に確定させてから処理する。深さを限ると、深い submodule や入れ子のリポジトリを
    # 検査せずに消す。名前の順に並べ、readdir の順序（ファイルシステムで変わる）に左右されないようにする。
    mapfile -d '' -t git_entries < <(set -o pipefail; find "$dir" -mindepth 1 -name .git -prune -print0 2>/dev/null | LC_ALL=C sort -z)
    if ! wait "$!"; then
        echo "block (cannot list git work trees): ${dir}" >&2
        return 1
    fi

    # 撤去しながら検査すると、先に撤去した親の worktree と一緒に、まだ検査していない
    # submodule の変更を --force で消す。すべて検査し終えてから撤去する。
    for git_entry in "${git_entries[@]}"; do
        gitdir="$(dirname "$git_entry")"

        if ! git -C "$gitdir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            echo "block (unverifiable git state): ${gitdir}" >&2
            return 1
        fi

        real_gitdir="$(cd "$gitdir" && pwd -P)"

        status_out="$(git -C "$real_gitdir" status --porcelain --untracked-files=normal 2>/dev/null)" || {
            echo "block (unverifiable git state): ${real_gitdir}" >&2
            return 1
        }
        if [ -n "$status_out" ]; then
            echo "block (uncommitted changes): ${real_gitdir}" >&2
            return 1
        fi

        # items/・worlds/ は親で無視しているため status --porcelain に出ないが、
        # git worktree remove（--force の有無に関係なく）と rm -rf は中身ごと消す。
        # 空のディレクトリは出ないので、空なら従来どおり撤去する。
        # --untracked-files=no の設定では !! も出なくなるため、normal を明示する。
        product_out="$(git -C "$real_gitdir" status --porcelain --ignored --untracked-files=normal -- items worlds 2>/dev/null)" || {
            echo "block (unverifiable git state): ${real_gitdir}" >&2
            return 1
        }
        if [ -n "$product_out" ]; then
            echo "block (product data in items/ or worlds/): ${real_gitdir}" >&2
            return 1
        fi

        if worktree_is_registered "$real_gitdir"; then
            releasable+=("$real_gitdir")
        fi
    done

    # 外側を先に撤去すると内側の作業ツリーも消え、内側の登録だけが残る。名前の逆順（内側が先）に撤去する。
    for ((i = ${#releasable[@]} - 1; i >= 0; i--)); do
        [ -e "${releasable[i]}" ] || continue
        if ! release_registered_worktree "${releasable[i]}"; then
            return 1
        fi
    done
    return 0
}

# TMP_ROOT 直下にある、既存の PR ファイル名パターンに一致する loose ファイルを報告する。
# 出所と内容を検証できないため、年齢や同じ PR のディレクトリ有無に関係なく削除しない。
cleanup_orphaned_pr_session_files() {
    local entry base pr protected=0

    for entry in "${TMP_ROOT}"/pr-review-*; do
        [ -f "$entry" ] || continue
        base="$(basename "$entry")"
        pr="$(pr_number_of "$base")" || continue
        echo "protected loose pr-review file (unverifiable provenance): ${entry} (PR ${pr})"
        protected=$((protected + 1))
    done

    echo "orphaned session file cleanup: protected=${protected}"
}

# $1: ログ用 kind（review-fix-planning）
# $2...: 呼び出し側が未クォート glob を展開して渡したパス
cleanup_tmp_dirs() {
    local kind="$1"
    shift
    local removed=0 skipped=0 dir

    for dir in "$@"; do
        [ -d "$dir" ] || continue

        if path_is_in_use "$dir"; then
            echo "skip (in use by a running process): ${dir}"
            skipped=$((skipped + 1))
            continue
        fi

        if ! tree_is_old_enough "$dir"; then
            echo "skip (modified within ${MIN_AGE_DAYS}d somewhere inside): ${dir}"
            skipped=$((skipped + 1))
            continue
        fi

        if ! resolve_git_worktrees_inside "$dir"; then
            echo "skip (dirty, unverifiable, or product-data git worktree inside): ${dir}"
            skipped=$((skipped + 1))
            continue
        fi

        if [ "$DRY_RUN" = "1" ]; then
            echo "dry-run: would remove ${kind} debris: ${dir}"
            removed=$((removed + 1))
            continue
        fi

        if rm -rf -- "$dir"; then
            echo "removed ${kind} debris: ${dir}"
            removed=$((removed + 1))
        else
            echo "failed to remove: ${dir}" >&2
            skipped=$((skipped + 1))
        fi
    done

    echo "${kind} cleanup: removed=${removed} skipped=${skipped}"
}

cleanup_pr_review_dirs() {
    local dir check_error cleanup_output
    local removed=0 skipped=0

    for dir in "${TMP_ROOT}"/pr-review-*; do
        [ -d "$dir" ] || continue

        if [ -L "$dir" ]; then
            echo "skip (symlink pr-review directory): ${dir}"
            skipped=$((skipped + 1))
            continue
        fi

        if path_is_in_use "$dir"; then
            echo "skip (in use by a running process): ${dir}"
            skipped=$((skipped + 1))
            continue
        fi

        if ! dir_is_old_enough "$dir"; then
            echo "skip (younger than ${MIN_AGE_DAYS}d): ${dir}"
            skipped=$((skipped + 1))
            continue
        fi

        if ! check_error="$(python3 "$PR_REVIEW_HELPER" reap-check --state "$dir/session.json" 2>&1)"; then
            echo "skip (unverifiable pr-review session): ${dir}: ${check_error}"
            skipped=$((skipped + 1))
            continue
        fi

        if [ "$DRY_RUN" = "1" ]; then
            echo "dry-run: would remove pr-review debris: ${dir}"
            removed=$((removed + 1))
            continue
        fi

        if cleanup_output="$(python3 "$PR_REVIEW_HELPER" cleanup --strict-reap --state "$dir/session.json" 2>&1)"; then
            echo "removed pr-review debris: ${dir}"
            removed=$((removed + 1))
        else
            echo "skip (helper refused cleanup): ${dir}: ${cleanup_output}" >&2
            skipped=$((skipped + 1))
        fi
    done

    echo "pr-review cleanup: removed=${removed} skipped=${skipped}"
}

cleanup_review_fix_planning_dirs() {
    cleanup_tmp_dirs "review-fix-planning" "${TMP_ROOT}"/review-fix-planning.*
}

cleanup_yarn_cache() {
    if [ -d "$YARN_CACHE_DIR" ]; then
        local size
        size="$(du -sh "$YARN_CACHE_DIR" 2>/dev/null | cut -f1)"
        if [ "$DRY_RUN" = "1" ]; then
            echo "dry-run: would remove yarn cache: ${YARN_CACHE_DIR} (${size:-unknown size})"
            return 0
        fi
        if rm -rf -- "$YARN_CACHE_DIR"; then
            echo "removed yarn cache: ${YARN_CACHE_DIR} (${size:-unknown size})"
        else
            echo "failed to remove yarn cache: ${YARN_CACHE_DIR}" >&2
        fi
    else
        echo "yarn cache cleanup: nothing to remove at ${YARN_CACHE_DIR}"
    fi
}

main() {
    echo "=== workspace cleanup: $(date -u +%Y-%m-%dT%H:%M:%SZ) (root=${TMP_ROOT}) ==="
    cleanup_pr_review_dirs
    cleanup_orphaned_pr_session_files
    cleanup_review_fix_planning_dirs
    cleanup_yarn_cache
}

main "$@"
