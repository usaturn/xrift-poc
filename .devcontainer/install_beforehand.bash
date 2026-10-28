#!/bin/bash

set -u

readonly GITLEAKS_VERSION="8.30.1"
readonly GITLEAKS_ARCHIVE_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
# 2026-10-10 に npm の latest で確認した版。更新は README の手順で確かめてから変える。
readonly XRIFT_CLI_VERSION="0.25.0"
# 2026-10-20 に公表された eMudhra の中間CA 8 件の秘密鍵の侵害に対する、Ubuntu 26.04 の修正版（#221）。
# これより古いと、侵害された中間CA（EM DV TLS CA - G3B）につながる emSign ECC Root CA - G3 を
# システムのトラストストアが信頼したままになる。更新は README の手順で確かめてから変える。
readonly CA_CERTIFICATES_MIN_VERSION="20260816ubuntu0.26.04.1"

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
# `.devcontainer/...` への相対参照を同一 root で行う。
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
# ~/.xrift は XRift CLI の認証情報（config.json）の保存先で、bind mount している。
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
    "${HOME}/.pi" \
    "${HOME}/.cursor" \
    "${HOME}/.xrift"; do
    if ! ensure_user_writable_dir "$dir"; then
        fail "Failed to prepare writable directory: ${dir}"
    fi
done

if ! place_agent_runner_config; then
    fail "Failed to place the agent-runner config."
fi

# 制作物の作業場所。Git では無視する（.gitignore の /items/・/worlds/）。既存の中身には触らない。
# シンボリックリンクは /items/（ディレクトリにだけ一致する）で無視されず、未追跡として表示されるので受け付けない。
prepare_work_directories() {
    local dir
    for dir in "${WORKSPACE_ROOT}/items" "${WORKSPACE_ROOT}/worlds"; do
        if [ -L "$dir" ] || { [ -e "$dir" ] && [ ! -d "$dir" ]; }; then
            echo "${dir} exists but is not a plain directory; leaving it unchanged." >&2
            return 1
        fi
        mkdir -p -- "$dir" || return 1
    done
}
step "Preparing the work directories (items/ and worlds/)"
if ! prepare_work_directories; then
    fail "Failed to prepare items/ and worlds/ under ${WORKSPACE_ROOT}."
fi

step "Rewriting the apt sources to HTTPS and updating the package index"
if ! sudo perl -pi -e 's@http://archive\.ubuntu\.com@https://archive.ubuntu.com@g; s@http://security\.ubuntu\.com@https://security.ubuntu.com@g' /etc/apt/sources.list.d/ubuntu.sources; then
    fail "Failed to rewrite /etc/apt/sources.list.d/ubuntu.sources."
fi
if ! sudo apt update; then
    fail "apt update failed."
fi

# システムのトラストストア（/etc/ssl/certs）は curl・git・Python などが使う。base image の版には頼らず、
# 以降の curl での取得より前に修正版へ更新する。届かなければ、侵害された中間CAを信頼したまま進まないよう止める（#221）。
upgrade_ca_certificates() {
    local version
    sudo apt install -y --only-upgrade ca-certificates || return 1
    version="$(dpkg-query -W -f='${Version}' ca-certificates)" || return 1
    if ! dpkg --compare-versions "$version" ge "$CA_CERTIFICATES_MIN_VERSION"; then
        echo "ca-certificates ${version:-<empty>} is older than ${CA_CERTIFICATES_MIN_VERSION} after the upgrade." >&2
        return 1
    fi
}
step "Upgrading ca-certificates to the fixed version"
if ! upgrade_ca_certificates; then
    fail "ca-certificates must be ${CA_CERTIFICATES_MIN_VERSION} or later."
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
# CI もこの定数を読む。更新は README の手順で検証してから行う。
readonly PI_VERSION="1.4.0"
# ~/.pi は devcontainer.json で bind mount している。ホスト側に存在しない場合は
# Docker が root 所有で作成するため、以降の pi install が書き込めるよう所有権を直す。
if ! ensure_user_writable_dir "${HOME}/.pi"; then
    fail "Failed to prepare writable directory: ${HOME}/.pi."
fi
if ! npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"; then
    fail "pi installation failed."
fi

# 廃止済みの pi 拡張が共有の ~/.pi に残っていないかを確かめる。撤去は ~/.pi を共有するほかの
# リポジトリ（maatlog-poc・agent-workspace）のセットアップか利用者が行い、ここでは削除の手順を
# 警告として表示するだけにする（usaturn/xrift-poc#1、#16）。
# npm:pi-subagents が残っていると、@teelicht/pi-superagents と subagent ツール名が衝突し、
# pigo などのランチャーが起動できない。
warn_retired_pi_extensions() {
    local agent_dir listed manifests package
    if ! agent_dir="$(python3 - <<'PYEOF'
import os
from pathlib import Path

print(Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser())
PYEOF
    )"; then
        warn "Cannot resolve the pi agent directory; skipping the retired pi extension check."
        return 0
    fi
    if ! listed="$(pi list 2>&1)"; then
        warn "pi list failed; skipping the retired pi extension check."
        return 0
    fi
    for package in pi-subagents pi-mcp-adapter pi-distill pi-extensions-tool-display pi-extensions-i18n pi-lens; do
        manifests="$(grep -l -e "\"${package}\"" -e "\"node_modules/${package}\"" \
            "${agent_dir}/npm/package.json" "${agent_dir}/npm/package-lock.json" 2>/dev/null || true)"
        case "${listed}" in
        *"npm:${package}"*) ;;
        *) [ -n "${manifests}" ] || continue ;;
        esac
        warn "Retired pi extension npm:${package} remains under ${agent_dir}. After stopping pi in the other containers that share it, remove it with: pi remove npm:${package}"
        if [ "${package}" = pi-subagents ]; then
            warn "Launchers such as pigo fail while npm:pi-subagents remains, because it conflicts with @teelicht/pi-superagents."
        fi
    done
    if [ -f "${agent_dir}/settings.json" ] && grep -qF -- '"-builtin:mcp"' "${agent_dir}/settings.json"; then
        warn "${agent_dir}/settings.json disables the built-in MCP extension. Remove \"-builtin:mcp\" from \"extensions\"."
    fi
    return 0
}

# @teelicht/pi-superagents を導入し、導入後の一覧に現れることを確かめる。
# 関数化は隔離テスト（test-pi-superagents.bash）が fake pi で実行できるようにするため。
install_pi_extensions() {
    local after
    if ! pi install --approve npm:@teelicht/pi-superagents 2>&1; then
        echo "Failed to install npm:@teelicht/pi-superagents." >&2
        return 1
    fi
    if ! after="$(pi list 2>&1)"; then
        echo "Failed to list pi extensions after installing npm:@teelicht/pi-superagents." >&2
        return 1
    fi
    case "$after" in
    *"npm:@teelicht/pi-superagents"*) ;;
    *)
        echo "npm:@teelicht/pi-superagents is missing after install." >&2
        return 1
        ;;
    esac
}

step "Checking for retired pi extensions"
warn_retired_pi_extensions

step "Installing @teelicht/pi-superagents"
if ! install_pi_extensions; then
    fail "Failed to install @teelicht/pi-superagents."
fi

step "Installing the pi extensions"
# Context7 は ctx7 CLI と find-docs スキルで使い、npm:@upstash/context7-pi は導入しない（#49）。
# 共有の ~/.pi からも外したので、.pi/settings.json で読み込みを止める項目も外した（#54）。
for extension in \
    npm:@jc4649/notify \
    npm:@plannotator/pi-extension \
    npm:pi-safe-search \
    npm:pi-diet-ripgrep \
    git:github.com/obra/superpowers \
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

step "Placing the launchers (pigo / pigod / piccg / piccd / piga / pigs / pigl / pixg / pigemf / pigemp / pigema)"
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
if ! install -m 0755 .devcontainer/pigema "${HOME}/.local/bin/pigema"; then
    fail "Failed to place pigema."
fi

step "Writing the pi-superagents launcher configs"
# pi-superagents のランチャー別 model tier 設定を配置する。既存の config は
# 保持する。tier 表を変更したときは、リポジトリのルートで
# zsh scripts/setup_pi_superagents.zsh を実行して反映する。
if ! zsh scripts/setup_pi_superagents.zsh --if-absent; then
    fail "Failed to set up pi-superagents launcher configs."
fi

step "Installing the shared skills (herdr / find-skills)"
if ! npx -y skills add herdrdev/herdr --skill herdr -g -y -a claude-code -a pi; then
    fail "Failed to install the herdr skill."
fi
if ! npx -y skills add vercel-labs/skills@find-skills -g -y -a claude-code -a pi; then
    fail "Failed to install the find-skills skill."
fi

# XRift CLI は版を固定して導入する。zsh の update() では更新しない（README の更新手順を参照）。
install_xrift_cli() {
    local version
    if ! npm install -g "@xrift/cli@${XRIFT_CLI_VERSION}"; then
        echo "npm install -g @xrift/cli@${XRIFT_CLI_VERSION} failed." >&2
        return 1
    fi
    if ! command -v xrift >/dev/null 2>&1; then
        echo "xrift is not on PATH after the install." >&2
        return 1
    fi
    version="$(xrift --version)" || return 1
    if [ "${version//[[:space:]]/}" != "${XRIFT_CLI_VERSION}" ]; then
        echo "Unexpected xrift version: ${version} (expected ${XRIFT_CLI_VERSION})." >&2
        return 1
    fi
}
step "Installing the XRift CLI"
if ! install_xrift_cli; then
    fail "XRift CLI installation failed."
fi

# Context7 CLI（find-docs スキルが使う）は、実行した時点の npm の latest を入れる。zsh の update() も
# latest に上げる（#220）。latest は 1 回だけ解決し、その版を入れて照合する。engines を満たさない版は
# --engine-strict で入れない。ctx7 setup は共有の ~/.claude・~/.claude.json・~/.codex などへ
# MCP 設定やスキルを書き込み、find-docs とも重複するため実行しない（#49）。
install_ctx7_cli() {
    local latest prefix ctx7_path version
    if ! latest="$(npm view ctx7@latest version)" || [ -z "${latest}" ]; then
        echo "npm view ctx7@latest version failed." >&2
        return 1
    fi
    if ! npm install -g --engine-strict "ctx7@${latest}"; then
        echo "npm install -g --engine-strict ctx7@${latest} failed. If npm reported EBADENGINE, Node.js $(node --version 2>/dev/null) does not satisfy the engines of ctx7 ${latest}." >&2
        return 1
    fi
    if ! prefix="$(npm prefix -g)" || [ -z "${prefix}" ]; then
        echo "npm prefix -g failed." >&2
        return 1
    fi
    if ! ctx7_path="$(command -v ctx7)"; then
        echo "ctx7 is not on PATH after the install." >&2
        return 1
    fi
    # PATH に nvm の current/bin のような symlink のディレクトリがあっても、実体で比べる
    if [ "$(readlink -f "${ctx7_path}")" != "$(readlink -f "${prefix}/bin/ctx7")" ]; then
        echo "ctx7 on PATH is ${ctx7_path}, not the npm global install (${prefix}/bin/ctx7)." >&2
        return 1
    fi
    if ! version="$(ctx7 --version)"; then
        echo "ctx7 --version failed after installing ctx7@${latest}." >&2
        return 1
    fi
    if [ "${version//[[:space:]]/}" != "${latest}" ]; then
        echo "Unexpected ctx7 version: ${version} (installed ${latest})." >&2
        return 1
    fi
}
step "Installing the Context7 CLI (ctx7)"
if ! install_ctx7_cli; then
    fail "Context7 CLI installation failed."
fi

step "Installing the XRift skills"
if ! bash scripts/install_xrift_skills.bash; then
    fail "XRift skills installation failed."
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

step "Writing the shell setup block into ~/.zshrc"
# 各 CLI インストーラが ~/.zshrc へ PATH を追記した後に、zshrc.txt を管理ブロックとして書く。
# 再実行してもブロックは 1 つのままで、ブロックの外（利用者の設定・インストーラの追記）は変えない。
# Herdr の導入が失敗しても alias・環境変数・PATH 補助を利用できるよう、
# 失敗時に exit する Herdr block より前で行う。
if ! python3 scripts/write_shell_block.py --source .devcontainer/zshrc.txt --target "${HOME}/.zshrc"; then
    fail "Failed to write the shell setup block into ~/.zshrc."
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
# 制作物が無くても入れられるよう、開発環境の tools/devenv の locked 依存から導入する。
if ! (cd -- "$WORKSPACE_ROOT" && uv run --project tools/devenv --locked playwright install --with-deps chromium); then
    fail "Playwright chromium install failed."
fi
