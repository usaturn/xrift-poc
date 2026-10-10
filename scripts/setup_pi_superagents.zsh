#!/usr/bin/env zsh

set -euo pipefail

#
# pi-superagents のランチャー別 model tier 設定をセットアップする
#
# 冪等なセットアップスクリプト
#
# 作成するもの:
#   ~/.pi/agent/extensions/subagent/               bwrap の bind 先（空のまま）
#   ~/.pi/agent/extensions/subagent.d/<launcher>/  ランチャー別の config.json
#   ~/.local/bin/pi-superagents-exec.zsh           ランチャー共通ヘルパ
#
# リポジトリの .devcontainer/pi-superagents/<launcher>.json が正。tier 表を
# 変更したときは、このスクリプトを実行して実環境へ反映する。
# install_beforehand.bash は --if-absent で呼ぶため、rebuild だけでは既存の
# config は更新されない。
#
# 使い方:
#   zsh scripts/setup_pi_superagents.zsh              置き直す（既存はバックアップ）
#   zsh scripts/setup_pi_superagents.zsh --if-absent  無いものだけ置く
#

readonly REPO_ROOT="${0:A:h}/.."
readonly SOURCE_DIR="${REPO_ROOT}/.devcontainer/pi-superagents"
readonly HELPER_SOURCE="${REPO_ROOT}/.devcontainer/pi-superagents-exec.zsh"

#
# bind 先は必ず HOME から組み立てる。pi-superagents は os.homedir() を直接
# 使っており PI_CODING_AGENT_DIR を参照しないため、ここで PI_CODING_AGENT_DIR を
# 使うと配置先が拡張の読み先とずれる。
#
readonly EXT_DIR="${HOME}/.pi/agent/extensions"
readonly SHARED_DIR="${EXT_DIR}/subagent"
readonly LAUNCHER_ROOT="${EXT_DIR}/subagent.d"
readonly BIN_DIR="${HOME}/.local/bin"

readonly LAUNCHERS=(pigo pigod piccg piccd piga pigs pigl pixg pigemf pigemp)

typeset if_absent=0
if [[ "${1:-}" == "--if-absent" ]]; then
    if_absent=1
elif [[ -n "${1:-}" ]]; then
    print -ru2 -- "usage: setup_pi_superagents.zsh [--if-absent]"
    exit 2
fi

if ! command -v bwrap >/dev/null 2>&1; then
    print -ru2 -- "ERROR: bwrap (bubblewrap) が見つかりません。"
    print -ru2 -- ""
    print -ru2 -- "ランチャーごとの設定分離に必要です:"
    print -ru2 -- ""
    print -ru2 -- "  sudo apt install -y bubblewrap"
    exit 1
fi

mkdir -p "${SHARED_DIR}" "${BIN_DIR}"

install -m 0644 "${HELPER_SOURCE}" "${BIN_DIR}/pi-superagents-exec.zsh"
print -r -- "helper: ${BIN_DIR}/pi-superagents-exec.zsh"

typeset launcher source dest
for launcher in "${LAUNCHERS[@]}"; do
    source="${SOURCE_DIR}/${launcher}.json"
    dest="${LAUNCHER_ROOT}/${launcher}/config.json"

    if [[ ! -f "${source}" ]]; then
        print -ru2 -- "ERROR: ${source} が見つかりません。"
        exit 1
    fi

    mkdir -p "${LAUNCHER_ROOT}/${launcher}"

    if [[ -f "${dest}" ]]; then
        if (( if_absent )); then
            print -r -- "${launcher}: 既存の設定を保持しました"
            continue
        fi
        if cmp -s "${source}" "${dest}"; then
            print -r -- "${launcher}: 変更なし"
            continue
        fi
        # 退避名の基本形は従来どおり <dest>.bak.YYYYMMDD-HHMMSS。同一秒に再実行
        # して基本名が既に存在する場合は、既存世代を上書きしないよう .1, .2, …
        # と空いている名前まで連番を増やす。
        typeset base backup i=0
        base="${dest}.bak.$(date +%Y%m%d-%H%M%S)"
        backup="${base}"
        while [[ -e "${backup}" ]]; do
            i=$(( i + 1 ))
            backup="${base}.${i}"
        done
        cp "${dest}" "${backup}"
        print -r -- "${launcher}: 既存の設定を ${backup:t} へ退避しました"
    fi

    install -m 0644 "${source}" "${dest}"
    print -r -- "${launcher}: ${dest} を配置しました"
done

print -r -- "shared bind target: ${SHARED_DIR}"

if command -v pi >/dev/null 2>&1; then
    typeset installed
    installed="$(pi list 2>/dev/null || true)"
    if print -r -- "${installed}" | grep -q "pi-subagents"; then
        print -ru2 -- ""
        print -ru2 -- "WARNING: npm:pi-subagents がまだ登録されています。"
        print -ru2 -- "pi-superagents の subagent tool と競合するため、削除してください:"
        print -ru2 -- ""
        print -ru2 -- "  pi remove npm:pi-subagents"
    fi
    if ! print -r -- "${installed}" | grep -q "@teelicht/pi-superagents"; then
        print -ru2 -- ""
        print -ru2 -- "WARNING: npm:@teelicht/pi-superagents が未導入です:"
        print -ru2 -- ""
        print -ru2 -- "  pi install --approve npm:@teelicht/pi-superagents"
    fi
fi
