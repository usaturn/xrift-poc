#!/bin/bash

set -u

readonly GITLEAKS_VERSION="8.30.1"
readonly GITLEAKS_ARCHIVE_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
# DeepSeek Harness は Developer Preview のため、Issue #282 指定の版に固定する。
readonly DSH_VERSION="0.2.0-rc.2"
readonly DSH_PI_TUI_VERSION="0.5.0"
readonly DSH_COMMANDCODE_PROVIDER_VERSION="0.12.2"
readonly DSH_NATIVE_SCRIPT_ALLOWLIST="@deepseek-ai/dsh-subprocess-local,koffi,node-pty"
readonly DSH_TERMINAL_PROFILE="pi-tui"
readonly DSH_MANAGED_BLOCK_BEGIN="# BEGIN maatlog managed provider config"
readonly DSH_MANAGED_BLOCK_END="# END maatlog managed provider config"

# postCreate の出力は、途中でコンテナが止まると残らない。どのステップまで進んだかを
# 後から追えるよう、各ステップの開始を必ず1行で出し、失敗は ERROR 行を残して
# 打ち切る（2026-09-13 のリビルド中断インシデント）。継続してよい処理
# （workspace cleanup）だけを明示的に続行させるため、スクリプト全体には set -e を
# 付けない。
readonly STEP_TOTAL=30
STEP_NUMBER=0

step() {
    STEP_NUMBER=$((STEP_NUMBER + 1))
    printf '[install_beforehand] [%2d/%2d] %s\n' "${STEP_NUMBER}" "${STEP_TOTAL}" "$@"
}

fail() {
    printf '[install_beforehand] ERROR: %s\n' "$@" >&2
    exit 1
}

warn() {
    printf '[install_beforehand] WARN: %s\n' "$@" >&2
}

# アプリの依存は親ではなく public/（公開リポジトリ）の checkout と lock から解決する。
# ここでは既存の checkout を上書きしないことだけを保証し、各失敗は ERROR 行で打ち切る。
prepare_public_checkout() {
    local app_dir="${WORKSPACE_ROOT}/public"
    local top parent status expected actual
    if [ ! -e "${app_dir}/.git" ]; then
        if [ -d "$app_dir" ] && [ -n "$(find "$app_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
            fail "public is not initialized and contains files; leaving it unchanged."
        fi
        if ! git -C "$WORKSPACE_ROOT" submodule update --init public; then
            fail "Failed to initialize public submodule."
        fi
    fi
    if [ -L "$app_dir" ] || [ ! -e "${app_dir}/.git" ]; then
        fail "public is not an initialized submodule."
    fi
    if ! top="$(git -C "$app_dir" rev-parse --show-toplevel)" ||
        ! parent="$(git -C "$app_dir" rev-parse --show-superproject-working-tree)"; then
        fail "public Git checkout is invalid."
    fi
    if [ "$top" != "$app_dir" ] || [ "$parent" != "$WORKSPACE_ROOT" ]; then
        fail "public is not this workspace's submodule."
    fi
    if ! status="$(git -C "$app_dir" status --porcelain --untracked-files=all)"; then
        fail "Cannot inspect public checkout status."
    fi
    if [ -n "$status" ]; then
        fail "public checkout has uncommitted changes; leaving it unchanged."
    fi
    if [ ! -f "$app_dir/pyproject.toml" ] || [ ! -f "$app_dir/uv.lock" ]; then
        fail "public pyproject.toml or uv.lock is missing."
    fi
    expected="$(git -C "$WORKSPACE_ROOT" rev-parse HEAD:public)" || fail "Cannot read public gitlink."
    actual="$(git -C "$app_dir" rev-parse HEAD)" || fail "Cannot read public HEAD."
    if [ "$expected" != "$actual" ]; then
        warn "public HEAD differs from the pinned gitlink; using the existing clean checkout."
    fi
}

# bind mount の target はホスト側に無いと Docker が root 所有で中間ディレクトリごと
# 作成する。書き込めない対象は、そのディレクトリだけを現在ユーザーへ chown して
# 復旧する。再帰しない（配下に bind mount があると、ホスト側データの所有権まで
# 変わるため）。
ensure_user_writable_dir() {
    local dir="$1"
    if ! mkdir -p "$dir" 2>/dev/null; then
        sudo mkdir -p "$dir" || return 1
    fi
    if [ ! -w "$dir" ]; then
        sudo chown "$(id -u):$(id -g)" "$dir" || return 1
    fi
    [ -w "$dir" ]
}

# pi-tui profile の dump-config に TUI bundle と Command Code provider が
# 解決されていることを確認する。$1: profile 名, $2: dump-config 出力。
assert_dsh_terminal_profile_entries() {
    local profile="$1"
    local config="$2"
    local needle
    for needle in "@xmoon76/dsh-pi-tui" "@mars-sea/dsh-commandcode-provider"; do
        if ! printf '%s\n' "${config}" | grep -qF "${needle}"; then
            echo "dsh --profile ${profile} is missing ${needle}; check the profile plugin list." >&2
            return 1
        fi
    done
}

# ~/.dsh/cordis.patch.yml を用意する。無ければ雛形をコピーし、既存ファイルには
# managed block が無いときだけ追記する。既存の利用者編集は書き換えない。
ensure_dsh_cordis_patch() {
    local target="${HOME}/.dsh/cordis.patch.yml"
    local template=".devcontainer/dsh-cordis.patch.yml"
    local remaining block initialized
    if [ ! -f "${target}" ]; then
        if ! cp "${template}" "${target}"; then
            echo "Failed to place dsh cordis.patch.yml." >&2
            return 1
        fi
        return 0
    fi
    if grep -qF "${DSH_MANAGED_BLOCK_BEGIN}" "${target}"; then
        return 0
    fi
    remaining="$(grep -vE '^[[:space:]]*(#|$)' "${target}" || true)"
    if [ -z "${remaining}" ] && grep -qE '^[[:space:]]*#' "${target}"; then
        if ! { printf '\n' && cat "${template}"; } >> "${target}"; then
            echo "Failed to initialize the commented dsh cordis.patch.yml." >&2
            return 1
        fi
        return 0
    fi
    # 空の表記揺れ（`[ ]`・`[] # comment`・`null`・`~`）も雛形で置換する。
    # 完全一致の `[]` だけを見ると、残りは追記経路へ落ちて root node の後に
    # block sequence が続く壊れた YAML になる。marker が入ると次回以降
    # short-circuit して修復不能になるため、ここで正規化して判定する。
    if [ -z "${remaining}" ] || ! printf '%s\n' "${remaining}" | grep -qvE '^[[:space:]]*(\[[[:space:]]*\]|\{[[:space:]]*\}|[Nn][Uu][Ll][Ll]|~)[[:space:]]*(#.*)?$'; then
        if grep -qE '^[[:space:]]*#' "${target}"; then
            # 空 root は除去し、行全体のコメントは雛形の前に残す。
            # 組み立てに失敗しても元ファイルを失わないよう、同じディレクトリで準備する。
            if ! initialized="$(mktemp "${target}.XXXXXX")"; then
                echo "Failed to prepare the commented dsh cordis.patch.yml." >&2
                return 1
            fi
            if ! { grep -E '^[[:space:]]*#' "${target}" && cat "${template}"; } > "${initialized}" ||
                ! mv "${initialized}" "${target}"; then
                rm -f "${initialized}"
                echo "Failed to initialize the commented empty dsh cordis.patch.yml." >&2
                return 1
            fi
        elif ! cp "${template}" "${target}"; then
            echo "Failed to replace the empty dsh cordis.patch.yml." >&2
            return 1
        fi
        return 0
    fi
    if ! block="$(awk -v begin="${DSH_MANAGED_BLOCK_BEGIN}" -v end="${DSH_MANAGED_BLOCK_END}" '
        index($0, begin) == 1 { inside = 1 }
        inside { print }
        $0 == end { inside = 0 }
    ' "${template}")" || [ -z "${block}" ]; then
        echo "Failed to extract the managed provider block from ${template}." >&2
        return 1
    fi
    if ! printf '\n%s\n' "${block}" >> "${target}"; then
        echo "Failed to append the managed provider block to ${target}." >&2
        return 1
    fi
}

# home patch の provider 設定が解決後の構成に現れていることを確認する。
# $1: profile 名, $2: dump-config 出力。
assert_dsh_provider_entries() {
    local profile="$1"
    local config="$2"
    local needle
    # 既定モデルと API root は利用者が変更できるため、雛形の値ではなく entry の存在を見る。
    for needle in "- id: llm-pi-ai" "opencode-go" "- id: llm-commandcode" "- id: agent-default-model"; do
        if ! printf '%s\n' "${config}" | grep -qF -- "${needle}"; then
            echo "dsh --profile ${profile} is missing provider config entry: ${needle}." >&2
            return 1
        fi
    done
}

install_pinned_gitleaks() {
    echo "Installing Gitleaks ${GITLEAKS_VERSION}..."
    gitleaks_tmp="$(mktemp -d)" || return 1
    trap 'rm -rf "$gitleaks_tmp"' EXIT
    archive="${gitleaks_tmp}/gitleaks.tar.gz"
    curl -fL -o "$archive" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" ||
        return 1
    printf '%s  %s\n' "$GITLEAKS_ARCHIVE_SHA256" "$archive" | sha256sum -c - || return 1
    tar -xzf "$archive" -C "$gitleaks_tmp" gitleaks || return 1
    mkdir -p "${HOME}/.local/bin" || return 1
    install -m 0755 "${gitleaks_tmp}/gitleaks" "${HOME}/.local/bin/gitleaks" || return 1
    export PATH="${HOME}/.local/bin:${PATH}"
    gitleaks version || return 1
}

# スクリプトはどの cwd から呼ばれても親 workspace root を正とする。
# `.devcontainer/...` への相対参照と public/ submodule の検査を同一 root で行う。
WORKSPACE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)" || fail "Cannot resolve workspace root."
readonly WORKSPACE_ROOT
cd -- "$WORKSPACE_ROOT" || fail "Cannot enter workspace root."

# 汎用の非対話実行器。既存設定（壊れた symlink も含む）は上書きしない。
# AGENT_RUNNER_CONFIG は個別実行の指定なので、導入時の保存先には使わない。
place_agent_runner_config() {
    local config_home="${XDG_CONFIG_HOME:-}"
    case "$config_home" in
    /*) ;;
    *) config_home="${HOME}/.config" ;;
    esac
    local config="${config_home}/agent-runner/config.toml"
    if [ -e "$config" ] || [ -L "$config" ]; then
        return 0
    fi
    mkdir -p "${config_home}/agent-runner" || return 1
    cp .devcontainer/agent-runner.toml "$config" || return 1
}
step "Ensuring user-writable directories under ~"
# bind mount target の中間ディレクトリ（~/.local/share 等）と target 自体は、
# Docker が root 所有で作る・ホスト側に root 所有のまま残ることがある。
# 以降の全ステップが $HOME 配下へ書き込むため、最初に親から順に書き込み可能を
# 保証する（#296）。~/.claude.json はファイル mount のため対象外。
for dir in \
    "${HOME}/.local" \
    "${HOME}/.local/share" \
    "${HOME}/.local/share/devin" \
    "${HOME}/.local/share/cursor-agent" \
    "${HOME}/.local/bin" \
    "${HOME}/.config" \
    "${HOME}/.config/devin" \
    "${HOME}/.config/gh" \
    "${HOME}/.config/gcloud" \
    "${HOME}/.claude" \
    "${HOME}/.codex" \
    "${HOME}/.grok" \
    "${HOME}/.pi" \
    "${HOME}/.dsh" \
    "${HOME}/.cursor"; do
    if ! ensure_user_writable_dir "$dir"; then
        fail "Failed to prepare writable directory: ${dir}"
    fi
done

if ! place_agent_runner_config; then
    fail "Failed to place the agent-runner config."
fi

step "Preparing the public application checkout"
# 親にはアプリ用 project を置かないため、Playwright の導入に使う public/ の
# checkout をここで検査・初期化する。dirty な checkout は上書きせず失敗にする。
prepare_public_checkout

step "Rewriting the apt sources to HTTPS and updating the package index"
if ! sudo perl -pi -e 's@http://archive\.ubuntu\.com@https://archive.ubuntu.com@g; s@http://security\.ubuntu\.com@https://security.ubuntu.com@g' /etc/apt/sources.list.d/ubuntu.sources; then
    fail "Failed to rewrite /etc/apt/sources.list.d/ubuntu.sources."
fi
if ! sudo apt update; then
    fail "apt update failed."
fi

step "Installing the base packages"
if ! sudo apt update; then
    fail "apt update failed before installing the base packages."
fi
if ! sudo apt install -y vim tig ripgrep fzf bubblewrap libglib2.0-0t64; then
    fail "apt install of the base packages failed."
fi

step "Generating the Japanese locale"
if ! sudo perl -pi -e 's/# ja_JP\.UTF-8/ja_JP.UTF-8/' /etc/locale.gen; then
    fail "Failed to enable ja_JP.UTF-8 in /etc/locale.gen."
fi
if ! sudo locale-gen; then
    fail "locale-gen failed."
fi

step "Installing Claude Code"
if ! (
    set -o pipefail
    curl -fsSL https://claude.ai/install.sh | bash
); then
    fail "Claude Code installation failed."
fi
# 各 CLI インストーラは rc ファイルへ PATH を追記するだけで、実行中のシェルへは export しない。
# postCreateCommand の /bin/bash は非対話・非ログインで rc を読まないため、
# 以降の claude / uv 呼び出しのためにここで一度だけ PATH を通す。
export PATH="${HOME}/.local/bin:${PATH}"
if ! command -v claude >/dev/null 2>&1; then
    fail "Claude Code installation completed without placing claude on PATH."
fi

step "Installing the Claude Code plugins"
if ! claude plugin marketplace add dashed/claude-marketplace; then
    fail "Failed to add the dashed/claude-marketplace marketplace."
fi
for plugin in \
    example-skills@anthropic-agent-skills \
    web-quality-skills@addy-web-quality-skills \
    ripgrep@alberto-marketplace; do
    if ! claude plugin install "$plugin"; then
        fail "Installing plugin ${plugin} failed."
    fi
done

step "Installing the Codex CLI"
if ! npm install -g @openai/codex@latest; then
    fail "Codex CLI installation failed."
fi

step "Installing Grok Build"
if ! (
    set -o pipefail
    curl -fsSL https://x.ai/cli/install.sh | bash
); then
    fail "Grok Build installation failed."
fi

step "Installing the Cursor CLI"
# ~/.local/share/cursor-agent は devcontainer.json で bind mount している。ホスト側に
# 存在しない場合は Docker が root 所有で作成するため、所有権を直す（~/.pi と同じ対応）。
# 認証情報と設定が入る ~/.cursor は既に bind mount 済み。
if ! ensure_user_writable_dir "${HOME}/.local/share/cursor-agent"; then
    fail "Failed to prepare writable directory: ${HOME}/.local/share/cursor-agent."
fi
if ! (
    set -o pipefail
    curl -fsSL https://cursor.com/install | bash
); then
    fail "Cursor CLI installation failed."
fi

step "Installing the Devin CLI"
# ~/.local/share/devin（認証情報と CLI 本体）と ~/.config/devin（設定）は
# devcontainer.json で bind mount し、リビルドでログインを失わないようにする。
# ホスト側に存在しない場合は Docker が root 所有で作成するため、install.sh と
# 以降の devin 呼び出しが書き込めるよう所有権を直す（~/.pi と同じ対応）。
install_devin_cli() {
    local devin_dir
    for devin_dir in "${HOME}/.local/share/devin" "${HOME}/.config/devin"; do
        ensure_user_writable_dir "${devin_dir}" || return 1
    done
    local installer
    installer="$(mktemp)" || return 1
    if ! curl -fsSL https://cli.devin.ai/install.sh -o "${installer}"; then
        rm -f "${installer}"
        return 1
    fi
    # install.sh の末尾は devin setup（対話ウィザード）を起動する。postCreate の実行には
    # TTY がなく、未ログインだとログイン選択プロンプトで停止するため stdin を閉じる。
    # 未ログイン時の setup は「Login canceled」で即失敗するが、バイナリの配置は setup
    # より前に完了しているため、終了コードではなく devin コマンドの存在で成否を判定する。
    bash "${installer}" </dev/null || true
    rm -f "${installer}"
    command -v devin >/dev/null 2>&1 || return 1
    # plugins install はログイン必須。bind mount でログインを永続化しても初回作成時は
    # 未ログインのことがあり、postCreate 中にログインする手段はないため、その場合は
    # 警告してスキップする。-y は install 前の確認プロンプトを自動承認する。
    # auth status は未ログインでも exit 0 で "Not logged in." を返すため終了コードでは
    # 判定できず、先頭が "Logged in" かで分岐する。実行失敗や想定外の出力は未ログインと
    # 区別して警告する。将来 CLI 側にプロンプトが追加されても postCreate が停止しない
    # よう、devin の各呼び出しは stdin を閉じる。
    local auth_status
    auth_status="$(devin auth status </dev/null 2>/dev/null || true)"
    case "${auth_status}" in
    [Ll]ogged\ in*)
        devin plugins install -y obra/superpowers </dev/null || return 1
        ;;
    Not\ logged\ in*)
        warn "devin is not logged in; skipping 'devin plugins install obra/superpowers'."
        warn "Run 'devin auth login' once, then re-run the plugin install."
        ;;
    *)
        warn "devin auth status returned an unexpected state; skipping 'devin plugins install obra/superpowers' (output: ${auth_status:-<empty>})."
        ;;
    esac
}
if ! install_devin_cli; then
    fail "Devin CLI installation failed."
fi

step "Installing pi"
# ~/.pi は devcontainer.json で bind mount している。ホスト側に存在しない場合は
# Docker が root 所有で作成するため、以降の pi install が書き込めるよう所有権を直す。
if ! ensure_user_writable_dir "${HOME}/.pi"; then
    fail "Failed to prepare writable directory: ${HOME}/.pi."
fi
if ! npm install -g --ignore-scripts @earendil-works/pi-coding-agent; then
    fail "pi installation failed."
fi

step "Replacing npm:pi-subagents with @teelicht/pi-superagents"
# --approve は project trust の確認を自動承認する（.agents/skills があると対話プロンプトで止まる）
# npm:pi-subagents は @teelicht/pi-superagents の subagent tool と競合する。
# ~/.pi は bind mount なので、既存環境の settings.json に残った登録も消す。
# この置換は必須なので、失敗を成功扱いしない（fail-loud）。関数化は隔離テスト
# （test-pi-superagents.bash）が install_pi_extensions だけを取り出して fake pi で
# 実行できるようにするため。
install_pi_extensions() {
    local legacy_manifests
    local legacy_listed

    # 1) 導入前の一覧を取得する。取得できないと置換の前提が崩れるので打ち切る。
    if ! before="$(pi list 2>&1)"; then
        echo "Failed to list pi extensions (pi list); aborting pi-superagents replacement." >&2
        exit 1
    fi
    # 2) 旧 npm:pi-subagents の残骸を探す。settings の一覧（pi list）だけでなく、
    #    pi が管理する npm マニフェストも見る。一覧が綺麗でもマニフェストに残って
    #    いると、`pi update --extensions`（zsh の update() が実行する）で node_modules
    #    ごと再構築されて復活し、subagent ツール名が @teelicht/pi-superagents と
    #    衝突して pigo / pigod が起動できなくなる（#289）。
    legacy_listed=0
    case "$before" in
    *"npm:pi-subagents"*) legacy_listed=1 ;;
    esac
    legacy_manifests="$(grep -l -e '"pi-subagents"' -e '"node_modules/pi-subagents"' \
        "${HOME}/.pi/agent/npm/package.json" \
        "${HOME}/.pi/agent/npm/package-lock.json" 2>/dev/null || true)"
    # 3) 登録済み、またはマニフェストに残っていれば削除する。pi remove は未登録の
    #    とき「No matching package found」で exit 1 を返すが、マニフェストの掃除自体は
    #    行う。そのため成否は終了コードではなく、掃除後の実体で判定する。
    if [ "${legacy_listed}" -eq 1 ] || [ -n "${legacy_manifests}" ]; then
        pi remove npm:pi-subagents 2>&1 || true
        # 存在しないパスが混ざると grep は一致していても exit 2 になるため、
        # 終了コードではなく一致したファイルの有無で判定する。
        legacy_manifests="$(grep -l -e '"pi-subagents"' -e '"node_modules/pi-subagents"' \
            "${HOME}/.pi/agent/npm/package.json" \
            "${HOME}/.pi/agent/npm/package-lock.json" 2>/dev/null || true)"
        if [ -n "${legacy_manifests}" ]; then
            echo "Failed to remove npm:pi-subagents from the pi managed npm manifest under ${HOME}/.pi/agent/npm." >&2
            exit 1
        fi
    fi
    # 4) 新拡張を導入する。導入失敗は対象を明示して打ち切る。
    if ! pi install --approve npm:@teelicht/pi-superagents 2>&1; then
        echo "Failed to install npm:@teelicht/pi-superagents." >&2
        exit 1
    fi
    # 5) 導入後に一覧を再取得し、事後条件（旧拡張が残らず新拡張が存在する）を確認する。
    if ! after="$(pi list 2>&1)"; then
        echo "Failed to re-list pi extensions after install." >&2
        exit 1
    fi
    case "$after" in
    *"npm:pi-subagents"*)
        echo "npm:pi-subagents is still registered after replacement." >&2
        exit 1
        ;;
    esac
    case "$after" in
    *"npm:@teelicht/pi-superagents"*)
        ;;
    *)
        echo "npm:@teelicht/pi-superagents is missing after replacement." >&2
        exit 1
        ;;
    esac
}
install_pi_extensions

step "Removing npm:pi-mcp-adapter (superseded by built-in MCP)"
# pi 1.0 の組み込み MCP 拡張（builtin:mcp）に一本化するため pi-mcp-adapter を外す。
# ~/.pi は bind mount なので、既存環境の settings.json には登録が残っている。
# adapter のインストーラは同じ settings.json に -builtin:mcp を書き込んで組み込み側を
# 無効化するため、パッケージ削除と併せてそのエントリも取り除く。
remove_pi_mcp_adapter() {
    local listed
    local after
    local agent_dir
    local adapter_manifests
    local adapter_listed=0

    # settings と npm マニフェストは同じ agent directory を参照する。
    if ! agent_dir="$(python3 - <<'PYEOF'
import os
from pathlib import Path

print(Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser())
PYEOF
    )"; then
        echo "Failed to resolve the pi agent directory; aborting pi-mcp-adapter removal." >&2
        exit 1
    fi
    if ! listed="$(pi list 2>&1)"; then
        echo "Failed to list pi extensions (pi list); aborting pi-mcp-adapter removal." >&2
        exit 1
    fi
    # 一覧だけでなく pi 管理の npm マニフェストも見る。マニフェストに残っていると
    # pi update --extensions で node_modules ごと再構築されて復活する。
    adapter_manifests="$(grep -l -e '"pi-mcp-adapter"' -e '"node_modules/pi-mcp-adapter"' \
        "${agent_dir}/npm/package.json" \
        "${agent_dir}/npm/package-lock.json" 2>/dev/null || true)"
    case "$listed" in
    *"npm:pi-mcp-adapter"*) adapter_listed=1 ;;
    esac
    if [ "${adapter_listed}" -eq 1 ] || [ -n "${adapter_manifests}" ]; then
        # pi remove は未登録のとき「No matching package found」で exit 1 を返すが、
        # マニフェストの掃除自体は行う。成否は終了コードではなく掃除後の実体で判定する。
        pi remove npm:pi-mcp-adapter 2>&1 || true
        adapter_manifests="$(grep -l -e '"pi-mcp-adapter"' -e '"node_modules/pi-mcp-adapter"' \
            "${agent_dir}/npm/package.json" \
            "${agent_dir}/npm/package-lock.json" 2>/dev/null || true)"
        if [ -n "${adapter_manifests}" ]; then
            echo "Failed to remove npm:pi-mcp-adapter from the pi managed npm manifest under ${agent_dir}/npm." >&2
            exit 1
        fi
        if ! after="$(pi list 2>&1)"; then
            echo "Failed to re-list pi extensions (pi list) after pi-mcp-adapter removal." >&2
            exit 1
        fi
        case "$after" in
        *"npm:pi-mcp-adapter"*)
            echo "npm:pi-mcp-adapter is still registered after removal." >&2
            exit 1
            ;;
        esac
    fi
    # pi remove はパッケージ登録を消すだけで、adapter が書き込んだ extensions の
    # -builtin:mcp は settings.json に残る。そのままだと組み込み MCP が無効のまま
    # なので、該当エントリだけを取り除く。他の extensions エントリは保持する。
    if ! python3 - "$agent_dir" <<'PYEOF'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

settings_path = Path(sys.argv[1]) / "settings.json"
if not settings_path.exists():
    sys.exit(0)
try:
    data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
except (ValueError, UnicodeError) as error:
    print(f"Invalid JSON in {settings_path}: {error}", file=sys.stderr)
    sys.exit(1)
if not isinstance(data, dict):
    sys.exit(0)
extensions = data.get("extensions")
if not isinstance(extensions, list) or "-builtin:mcp" not in extensions:
    sys.exit(0)
data["extensions"] = [entry for entry in extensions if entry != "-builtin:mcp"]
fd, temp_name = tempfile.mkstemp(dir=settings_path.parent, prefix=".settings-", suffix=".tmp")
temp_path = Path(temp_name)
with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
    json.dump(data, temp_file, ensure_ascii=False, indent=2)
    temp_file.write("\n")
os.chmod(temp_path, stat.S_IMODE(settings_path.stat().st_mode))
os.replace(temp_path, settings_path)
PYEOF
    then
        echo "Failed to clean -builtin:mcp from ${agent_dir}/settings.json." >&2
        exit 1
    fi
}
remove_pi_mcp_adapter

step "Removing npm:pi-distill (distillation retired)"
# pi-distill は廃止する（#508）。~/.pi は bind mount なので、インストール対象から
# 外すだけでは既存環境の settings.json 登録は消えない。依存の
# pi-extensions-tool-display / pi-extensions-i18n は通常 pi-distill の npm 依存と
# してのみ入るが、単独登録された環境では一緒に外す。
remove_pi_distill() {
    local agent_dir listed
    local distill_manifests
    local distill_listed=0 helpers_listed=0

    # settings.json の packages と npm マニフェストは同じ agent directory を参照する。
    # pi は PI_CODING_AGENT_DIR の先頭の ~ を展開するが、bash は変数値の ~ を展開しない。
    # remove_pi_mcp_adapter と同じ解決にそろえ、マニフェストの検査先を pi と一致させる。
    if ! agent_dir="$(python3 - <<'PYEOF'
import os
from pathlib import Path

print(Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser())
PYEOF
    )"; then
        echo "Failed to resolve the pi agent directory; aborting pi-distill removal." >&2
        exit 1
    fi
    if ! listed="$(pi list 2>&1)"; then
        echo "Failed to list pi extensions (pi list); aborting pi-distill removal." >&2
        exit 1
    fi
    # 一覧が綺麗でも pi 管理の npm マニフェストに残っていると
    # `pi update --extensions`（zsh の update() が実行する）で node_modules ごと
    # 復活する（pi-subagents 撤去の #289 と同じ穴）。
    distill_manifests="$(grep -l -e '"pi-distill"' -e '"node_modules/pi-distill"' \
        "${agent_dir}/npm/package.json" \
        "${agent_dir}/npm/package-lock.json" 2>/dev/null || true)"
    case "$listed" in
    *"npm:pi-distill"*) distill_listed=1 ;;
    esac
    case "$listed" in
    *"npm:pi-extensions-tool-display"*|*"npm:pi-extensions-i18n"*) helpers_listed=1 ;;
    esac
    if [ "${distill_listed}" -eq 0 ] && [ -z "${distill_manifests}" ] && [ "${helpers_listed}" -eq 0 ]; then
        return 0
    fi
    # pi remove は未登録のとき「No matching package found」で exit 1 を返すが、
    # マニフェストの掃除自体は行う。成否は終了コードではなく掃除後の実体で判定する。
    if [ "${distill_listed}" -eq 1 ] || [ -n "${distill_manifests}" ]; then
        pi remove npm:pi-distill 2>&1 || true
    fi
    case "$listed" in
    *"npm:pi-extensions-tool-display"*)
        pi remove npm:pi-extensions-tool-display 2>&1 || true ;;
    esac
    case "$listed" in
    *"npm:pi-extensions-i18n"*)
        pi remove npm:pi-extensions-i18n 2>&1 || true ;;
    esac
    distill_manifests="$(grep -l -e '"pi-distill"' -e '"node_modules/pi-distill"' \
        "${agent_dir}/npm/package.json" \
        "${agent_dir}/npm/package-lock.json" 2>/dev/null || true)"
    if [ -n "${distill_manifests}" ]; then
        echo "Failed to remove npm:pi-distill from the pi managed npm manifest under ${agent_dir}/npm." >&2
        exit 1
    fi
    if ! listed="$(pi list 2>&1)"; then
        echo "Failed to re-list pi extensions (pi list) after pi-distill removal." >&2
        exit 1
    fi
    case "$listed" in
    *"npm:pi-distill"*|*"npm:pi-extensions-tool-display"*|*"npm:pi-extensions-i18n"*)
        echo "npm:pi-distill or its bundled extensions are still registered after removal." >&2
        exit 1
        ;;
    esac
}
remove_pi_distill

step "Installing the pi extensions"
for extension in \
    npm:@jc4649/notify \
    npm:@upstash/context7-pi \
    npm:pi-safe-search \
    npm:pi-diet-ripgrep \
    git:github.com/obra/superpowers \
    npm:pi-lens \
    npm:pi-simplify \
    npm:@howaboua/pi-skill-skill-creator; do
    if ! pi install --approve "${extension}"; then
        fail "Failed to install the pi extension ${extension}."
    fi
done

step "Placing ~/.pi/agent/models.json"
# models.json は bind mount した ~/.pi 側を正とし、無い場合のみ雛形を配置する。
# 毎回上書きすると、ホスト側で加えたモデル定義の変更がリビルドで失われる。
if ! mkdir -p "${HOME}/.pi/agent"; then
    fail "Failed to create ~/.pi/agent."
fi
if [ ! -f "${HOME}/.pi/agent/models.json" ]; then
    if ! cp .devcontainer/pi-models.json "${HOME}/.pi/agent/models.json"; then
        fail "Failed to place pi models.json."
    fi
fi

step "Placing the launchers (pigo / pigod / piccg / piccd / piga / pigs / pigl / pixg / pigemf / pigemp)"
# Pi ランチャーは ~/.local/bin に置く。ここは bind mount の
# 対象外でリビルドのたびに失われるため、models.json と違い毎回上書きで配置する。
if ! mkdir -p "${HOME}/.local/bin"; then
    fail "Failed to create ~/.local/bin."
fi
if ! install -m 0755 .devcontainer/pigo "${HOME}/.local/bin/pigo"; then
    fail "Failed to place pigo."
fi
if ! install -m 0755 .devcontainer/pigod "${HOME}/.local/bin/pigod"; then
    fail "Failed to place pigod."
fi
if ! install -m 0755 .devcontainer/piccg "${HOME}/.local/bin/piccg"; then
    fail "Failed to place piccg."
fi
if ! install -m 0755 .devcontainer/piccd "${HOME}/.local/bin/piccd"; then
    fail "Failed to place piccd."
fi
if ! install -m 0755 .devcontainer/piga "${HOME}/.local/bin/piga"; then
    fail "Failed to place piga."
fi
if ! install -m 0755 .devcontainer/pigs "${HOME}/.local/bin/pigs"; then
    fail "Failed to place pigs."
fi
if ! install -m 0755 .devcontainer/pigl "${HOME}/.local/bin/pigl"; then
    fail "Failed to place pigl."
fi
if ! install -m 0755 .devcontainer/pixg "${HOME}/.local/bin/pixg"; then
    fail "Failed to place pixg."
fi

if ! install -m 0755 .devcontainer/pigemf "${HOME}/.local/bin/pigemf"; then
    fail "Failed to place pigemf."
fi
if ! install -m 0755 .devcontainer/pigemp "${HOME}/.local/bin/pigemp"; then
    fail "Failed to place pigemp."
fi

step "Writing the pi-superagents launcher configs"
# pi-superagents のランチャー別 model tier 設定を配置する。既存の config は
# 保持する。tier 表を変更したときは、リポジトリのルートで
# zsh scripts/setup_pi_superagents.zsh を実行して反映する。
if ! zsh scripts/setup_pi_superagents.zsh --if-absent; then
    fail "Failed to set up pi-superagents launcher configs."
fi

step "Installing DeepSeek Harness"
# DeepSeek Harness（dsh）は pi と並行して使う（#282）。dsh の plugin 管理は PATH 上の
# pnpm を直接 spawn するため、devcontainer.json の Node feature で pnpm を明示導入
# している（pnpmVersion "none" は導入省略）。
install_deepseek_harness() {
    echo "Installing DeepSeek Harness ${DSH_VERSION}..."
    if ! npm install -g --allow-scripts="${DSH_NATIVE_SCRIPT_ALLOWLIST}" "@deepseek-ai/dsh@${DSH_VERSION}"; then
        echo "DeepSeek Harness installation failed." >&2
        exit 1
    fi
    if ! command -v dsh >/dev/null 2>&1; then
        echo "DeepSeek Harness installation completed without placing dsh on PATH." >&2
        exit 1
    fi
    # 想定と違う版が入ると Developer Preview の仕様差を踏むため fail-loud にする。
    if ! dsh_version="$(dsh --version)"; then
        echo "dsh --version failed." >&2
        exit 1
    fi
    if [ "${dsh_version//[[:space:]]/}" != "${DSH_VERSION}" ]; then
        echo "Unexpected dsh version: ${dsh_version} (expected ${DSH_VERSION})." >&2
        exit 1
    fi
    # ~/.dsh は devcontainer.json で bind mount している。ホスト側に存在しない場合は
    # Docker が root 所有で作成するため、以降の dsh が書き込めるよう所有権を直す。
    ensure_user_writable_dir "${HOME}/.dsh" || {
        echo "Failed to prepare writable directory: ${HOME}/.dsh." >&2
        exit 1
    }
    if [ -f "${HOME}/.dsh/settings.yaml" ]; then
        echo "Warning: ${HOME}/.dsh/settings.yaml is a legacy settings file." >&2
        echo "DeepSeek Harness 0.2.0 imports it into the active profile on first boot." >&2
        echo "See .devcontainer/README.md for migration steps." >&2
    fi
    if ! ensure_dsh_cordis_patch; then
        exit 1
    fi
    # Command Code provider は web と "${DSH_TERMINAL_PROFILE}" の両 profile に導入する。plugin add は導入済みなら
    # lockfile 一致で成功する（冪等）ため、rebuild のたびに実行して欠落を検出する。
    if ! dsh plugin --profile web add "@mars-sea/dsh-commandcode-provider@${DSH_COMMANDCODE_PROVIDER_VERSION}"; then
        echo "Failed to add the Command Code provider to the dsh web profile." >&2
        exit 1
    fi
    # terminal profile は plugin 一覧が独立しているため、TUI と Command Code
    # provider を明示的に追加する。導入済みなら lockfile 一致で成功する（冪等）。
    if ! dsh plugin --profile "${DSH_TERMINAL_PROFILE}" -- add "@xmoon76/dsh-pi-tui@${DSH_PI_TUI_VERSION}"; then
        echo "Failed to add the pi-tui bundle to the dsh ${DSH_TERMINAL_PROFILE} profile." >&2
        exit 1
    fi
    if ! dsh plugin --profile "${DSH_TERMINAL_PROFILE}" -- add "@mars-sea/dsh-commandcode-provider@${DSH_COMMANDCODE_PROVIDER_VERSION}"; then
        echo "Failed to add the Command Code provider to the dsh ${DSH_TERMINAL_PROFILE} profile." >&2
        exit 1
    fi
    # 既に別内容の cordis.patch.yml を持つ環境では無効化が入らない。効いていない
    # 設定を silent に通すと「設定したつもり」を再生産するため、解決後の構成を見て
    # fail-loud にする。
    if ! dsh_config="$(dsh --profile web --dump-config)"; then
        echo "dsh --profile web --dump-config failed." >&2
        exit 1
    fi
    # 当該 entry のブロック内だけを見る。次の top-level entry（行頭の "- "）で
    # 抜けるので、隣の plugin の enabled で通ってしまうことがない。
    if ! printf '%s\n' "${dsh_config}" | awk '
        /^- id: plugin-package-inventory-deepseek$/ { inside = 1; next }
        inside && /^- / { inside = 0 }
        inside && /^[[:space:]]+enabled: false$/ { found = 1 }
        END { exit found ? 0 : 1 }
    '; then
        echo "dsh still enables plugin-package-inventory-deepseek; check ${HOME}/.dsh/cordis.patch.yml." >&2
        exit 1
    fi
    if ! assert_dsh_provider_entries web "${dsh_config}"; then
        exit 1
    fi
    if ! dsh_terminal_config="$(dsh --profile "${DSH_TERMINAL_PROFILE}" --dump-config)"; then
        echo "dsh --profile ${DSH_TERMINAL_PROFILE} --dump-config failed." >&2
        exit 1
    fi
    if ! assert_dsh_terminal_profile_entries "${DSH_TERMINAL_PROFILE}" "${dsh_terminal_config}"; then
        exit 1
    fi
    if ! assert_dsh_provider_entries "${DSH_TERMINAL_PROFILE}" "${dsh_terminal_config}"; then
        exit 1
    fi
}
install_deepseek_harness

# 起動時だけ既定モデルを選ぶ。共有 ~/.dsh の patch / profile は書き換えない。
# dsh-model-env.mjs は実行中 provider / model を DSH_* としてシェルへ渡すプラグイン
# （#577）。ランチャーの patch から絶対パスで読み込むため、~/.local/share へ置く。
install_dsh_launchers() {
    local asset generated
    local dsh_model_env_dir="${HOME}/.local/share/maatlog"
    local dsh_model_env_plugin="${dsh_model_env_dir}/dsh-model-env.mjs"
    if ! mkdir -p "${HOME}/.local/bin" "${dsh_model_env_dir}"; then
        echo "Failed to create the DSH launcher directories." >&2
        return 1
    fi
    for asset in dshccd dshgo dsh-launcher.bash; do
        if ! install -m 0755 ".devcontainer/${asset}" "${HOME}/.local/bin/${asset}"; then
            echo "Failed to place ${asset}." >&2
            return 1
        fi
    done
    for asset in dshccd.patch.yml dshgo.patch.yml; do
        if ! install -m 0644 ".devcontainer/${asset}" "${HOME}/.local/bin/${asset}"; then
            echo "Failed to place ${asset}." >&2
            return 1
        fi
    done
    if ! install -m 0644 ".devcontainer/dsh-model-env.mjs" "${dsh_model_env_plugin}"; then
        echo "Failed to place dsh-model-env.mjs." >&2
        return 1
    fi
    # patch 中の __PLUGIN_PATH__ を実際の配置先へ置換する。途中で切れた patch を
    # 残さないよう、同じディレクトリで組み立ててから置き換える。
    if ! generated="$(mktemp "${HOME}/.local/bin/dsh-model-env.patch.yml.XXXXXX")"; then
        echo "Failed to prepare dsh-model-env.patch.yml." >&2
        return 1
    fi
    if ! sed "s|__PLUGIN_PATH__|${dsh_model_env_plugin}|" ".devcontainer/dsh-model-env.patch.yml" > "${generated}" ||
        ! chmod 0644 "${generated}" ||
        ! mv "${generated}" "${HOME}/.local/bin/dsh-model-env.patch.yml"; then
        rm -f "${generated}"
        echo "Failed to place dsh-model-env.patch.yml." >&2
        return 1
    fi
}
step "Placing the DSH launchers (dshccd / dshgo)"
if ! install_dsh_launchers; then
    fail "Failed to place the DSH launchers."
fi

step "Installing the shared skills (herdr / find-skills)"
if ! npx -y skills add herdrdev/herdr --skill herdr -g -y -a claude-code -a pi -a grok; then
    fail "Failed to install the herdr skill."
fi
if ! npx -y skills add vercel-labs/skills@find-skills -g -y -a claude-code -a pi -a grok; then
    fail "Failed to install the find-skills skill."
fi

step "Installing uv"
if ! (
    set -o pipefail
    curl -LsSf https://astral.sh/uv/install.sh | sh
); then
    fail "uv installation failed."
fi

step "Installing Starship"
if ! (
    set -o pipefail
    curl -fsSL https://starship.rs/install.sh | sh -s -- -y
); then
    fail "Starship installation failed."
fi
if ! mkdir -p "${HOME}/.config"; then
    fail "Failed to create ~/.config."
fi
if ! cp .devcontainer/starship.toml "${HOME}/.config/starship.toml"; then
    fail "Failed to place the Starship config."
fi

step "Placing the Herdr helper scripts in ~/bin"
# herdrstart は PATH 上の herdr-status-updater（拡張子なし）を呼ぶ。
# 各エージェントは PATH 上の herdr-agent-task（拡張子なし）を呼ぶ。
# zshrc.txt がシェル起動のたびに間引いて呼ぶための実体も ~/bin に置く（#146）。
# if ! ( set -e; ... ) では条件式の内側で errexit が無効になるため、
# install_pinned_gitleaks と同じく関数内の || return 1 で失敗を伝える。
place_herdr_helpers() {
    mkdir -p "${HOME}/bin" || return 1
    cp .devcontainer/herdr-git-status.bash "${HOME}/bin/herdr-git-status.bash" || return 1
    chmod +x "${HOME}/bin/herdr-git-status.bash" || return 1
    cp .devcontainer/herdr-status-updater.bash "${HOME}/bin/herdr-status-updater.bash" || return 1
    chmod +x "${HOME}/bin/herdr-status-updater.bash" || return 1
    cp .devcontainer/herdr-status-updater.bash "${HOME}/bin/herdr-status-updater" || return 1
    chmod +x "${HOME}/bin/herdr-status-updater" || return 1
    cp .devcontainer/herdr-agent-task.bash "${HOME}/bin/herdr-agent-task.bash" || return 1
    chmod +x "${HOME}/bin/herdr-agent-task.bash" || return 1
    cp .devcontainer/herdr-agent-task.bash "${HOME}/bin/herdr-agent-task" || return 1
    chmod +x "${HOME}/bin/herdr-agent-task" || return 1
    ln -sfnT -- "${WORKSPACE_ROOT}/.devcontainer/cleanup-workspace.bash" "${HOME}/bin/cleanup-workspace.bash" || return 1
}
if ! place_herdr_helpers; then
    fail "Failed to place the Herdr helper scripts in ~/bin."
fi
if ! mkdir -p "${HOME}/.local/bin"; then
    fail "Failed to create ~/.local/bin."
fi
export PATH="${HOME}/.local/bin:${PATH}"

step "Cleaning up stale workspace debris"
# コンテナ作成時点のディスクを圧迫する使い捨て debris を掃除する（#146。詳細は
# cleanup-workspace.bash を参照）。定期的な再発防止は zshrc.txt 側の間引き実行が
# 担う。失敗しても以降の必須処理は続ける（唯一の意図的なベストエフォート）。
if ! bash .devcontainer/cleanup-workspace.bash; then
    warn "Workspace cleanup reported a problem; continuing."
fi

step "Installing the pinned Gitleaks"
if ! install_pinned_gitleaks; then
    fail "Gitleaks installation failed."
fi

step "Appending the shell setup to ~/.zshrc"
# 各 CLI インストーラが ~/.zshrc へ PATH を追記した後に zshrc.txt を連結する。
# Herdr の導入が失敗しても alias・環境変数・PATH 補助を利用できるよう、
# 失敗時に exit する Herdr block より前で行う。
if ! cat .devcontainer/zshrc.txt >>${HOME}/.zshrc; then
    fail "Failed to append .devcontainer/zshrc.txt to ~/.zshrc."
fi

step "Installing Herdr"
if ! (
    set -o pipefail
    curl -fsSL https://herdr.dev/install.sh | sh
); then
    fail "Herdr installation failed."
fi
if ! mkdir -p "${HOME}/.config/herdr"; then
    fail "Failed to create the Herdr config directory."
fi
if [ ! -f "${HOME}/.config/herdr/config.toml" ]; then
    if ! cp .devcontainer/herdr.toml "${HOME}/.config/herdr/config.toml"; then
        fail "Failed to place the Herdr config."
    fi
fi
if ! command -v herdr >/dev/null 2>&1; then
    fail "Herdr installation completed without placing herdr on PATH."
fi
if ! herdr --version; then
    fail "Herdr version verification failed."
fi

step "Installing the Playwright browsers"
# Playwright のブラウザ／OS 依存は取得サイズが大きくネットワーク失敗しやすい。
# 失敗しても gitleaks 等の必須処理が飛ばないよう、スクリプト末尾で fail-loud にする。
echo "Installing Playwright browsers..."
# Playwright は public/ の locked 依存に対して実行する。親にアプリ用
# pyproject.toml / uv.lock がなくても、アプリ側の lock をそのまま使える。
if ! (cd -- "$WORKSPACE_ROOT/public" && uv run --locked playwright install --with-deps chromium); then
    fail "Playwright chromium install failed."
fi
