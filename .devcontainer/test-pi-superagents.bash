#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="${REPO_ROOT}/.devcontainer/pi-superagents-exec.zsh"
TEST_ROOT="$(mktemp -d)"
ZSH_BIN="$(command -v zsh)"
PI_PACKAGE="$(dirname "$(dirname "$(dirname "$(readlink -f "$(command -v pi)")")")")"
trap 'rm -rf "${TEST_ROOT:?}"' EXIT

# 呼び出し側の PI_CODING_AGENT_DIR で結果が変わらないよう、スイート全体で
# 無効化する。必要なテストは呼び出し単位で明示的に設定する。
unset PI_CODING_AGENT_DIR

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    [ "$actual" = "$expected" ] || fail "$msg: expected='$expected' actual='$actual'"
}

# ランチャー用の一時 HOME を作る。without-config ではランチャーのディレクトリだけを作り、
# config.json を置かない（利用者が config.json だけを消した状態）。
make_home() {
    local name="$1" with_config="$2"
    local home="${TEST_ROOT}/${name}"
    mkdir -p "${home}/.pi/agent/extensions/subagent" \
        "${home}/.pi/agent/extensions/subagent.d/${name}"
    if [ "$with_config" = "with-config" ]; then
        printf '{"superagents":{"modelTiers":{}}}\n' \
            > "${home}/.pi/agent/extensions/subagent.d/${name}/config.json"
    fi
    printf '%s' "$home"
}

# OAuth で起動前に確認するランチャー（openai-codex・xai）をまとめて起動できる auth.json を置く。
write_oauth_auth() {
    local home="$1"
    mkdir -p "${home}/.pi/agent"
    printf '%s\n' \
        '{"openai-codex":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999},"xai":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "${home}/.pi/agent/auth.json"
}

# ヘルパを source して pi_superagents_exec を dry-run で呼ぶ。
run_helper() {
    local home="$1" launcher="$2"; shift 2
    env HOME="$home" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec '${launcher}' \"\$@\"" _ "$@"
}

# dry-run の出力（1 行 1 要素の argv）から、--bind の直後の 2 要素（bind 元・重ね先）を 1 行ずつ出す。
# 部分一致だと重ね先が bind 元の部分文字列に化けて通るため、位置で取り出す。
bind_pair_of() {
    local -a argv
    local i
    mapfile -t argv <<< "$1"
    for i in "${!argv[@]}"; do
        if [ "${argv[$i]}" = "--bind" ]; then
            printf '%s\n' "${argv[$((i + 1))]}" "${argv[$((i + 2))]}"
            return 0
        fi
    done
    return 1
}

# このテストが無いと、重ね先を PI_CODING_AGENT_DIR から組み立てる変更が入ったときに、それを設定している利用者だけ tier 設定が黙って効かなくなる。
test_bind_target_ignores_pi_coding_agent_dir() {
    # 拡張は os.homedir() を直接見るので、PI_CODING_AGENT_DIR を設定しても
    # bind 先は HOME 基準でなければならない。
    local home out pair
    home="$(make_home pigo with-config)"
    out="$(env PI_CODING_AGENT_DIR="${TEST_ROOT}/elsewhere" \
        HOME="$home" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec pigo")"
    pair="$(bind_pair_of "$out")" || fail "helper must pass --bind to bwrap"
    assert_eq "$(printf '%s\n' "$pair" | sed -n 2p)" \
        "${home}/.pi/agent/extensions/subagent" "bind dst must equal HOME-based shared dir"
}

# このテストが無いと、ランチャーのディレクトリに config.json だけが無いとき、bwrap が空のディレクトリを重ねて、同梱の既定の tier で黙って起動する。
test_missing_config_fails_closed() {
    local home status
    home="$(make_home pigod without-config)"
    set +e
    run_helper "$home" pigod >/dev/null 2>&1
    status=$?
    set -e
    assert_eq "$status" "1" "missing config must exit 1"
}

# ------------------------------------------------------------------ dry-run と実際の起動
# PI_SUPERAGENTS_LAUNCHER_DRY_RUN が文字列 "1" のときだけ argv 表示になり、
# それ以外は本当に bwrap を起動する。本当の bwrap を起動しないよう、PATH 先頭に
# 呼び出しを記録する fake bwrap を置く。
BWRAP_SHIM_DIR="${TEST_ROOT}/bwrapbin"
FAKE_BWRAP_LOG="${TEST_ROOT}/bwrap-state/log"

make_bwrap_shim() {
    mkdir -p "$BWRAP_SHIM_DIR" "${TEST_ROOT}/bwrap-state"
    cat > "${BWRAP_SHIM_DIR}/bwrap" <<'BWRAP_SHIM_EOF'
#!/bin/bash
printf 'bwrap invoked with %s args\n' "$#" >> "${FAKE_BWRAP_LOG:?}"
printf '%s\n' "$@" >> "${FAKE_BWRAP_LOG:?}"
BWRAP_SHIM_EOF
    chmod +x "${BWRAP_SHIM_DIR}/bwrap"
}

# ヘルパを指定の dry-run 値で呼ぶ。PATH 先頭に fake bwrap を載せ、実環境の
# bwrap に影響を与えない。
run_helper_dry_run() {
    local home="$1" launcher="$2" drv="$3"; shift 3
    env HOME="$home" PATH="${BWRAP_SHIM_DIR}:${PATH}" FAKE_BWRAP_LOG="$FAKE_BWRAP_LOG" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN="$drv" \
        "${ZSH_BIN}" -c "source '${HELPER}'; pi_superagents_exec '${launcher}' \"\$@\"" _ "$@"
}

# このテストが無いと、dry-run と実際の exec の argv がずれても、dry-run で bind・overlay を確かめる他のテストが実際の起動と違うものを確かめたまま通る。
test_actual_and_dry_run_argv_match() {
    local home out expected
    local -a command_args
    home="$(make_home argv-match with-config)"
    make_bwrap_shim
    rm -f "$FAKE_BWRAP_LOG"
    out="$(run_helper "$home" argv-match --session 'session with spaces.jsonl' -- 'message with spaces')"
    mapfile -t command_args <<< "$out"
    expected="$(printf 'bwrap invoked with %s args\n' "$((${#command_args[@]} - 1))"; \
        printf '%s\n' "${command_args[@]:1}")"
    run_helper_dry_run "$home" argv-match 0 --session 'session with spaces.jsonl' -- 'message with spaces'
    assert_eq "$(<"$FAKE_BWRAP_LOG")" "$expected" "actual and dry-run must use identical argv"
}

CONFIG_DIR="${REPO_ROOT}/.devcontainer/pi-superagents"

# ランチャーは .devcontainer/pi-superagents/<launcher>.json の tier 設定と 1 対 1 に対応する。
# 一覧をテストへ書き写さず、設定ファイルから導く。
mapfile -t LAUNCHERS < <(find "$CONFIG_DIR" -maxdepth 1 -name '*.json' -printf '%f\n' \
    | sed 's/\.json$//' | LC_ALL=C sort)
[ "${#LAUNCHERS[@]}" -gt 0 ] || fail "no launcher configs in ${CONFIG_DIR}"

# ランチャーは install 後 ~/.local/bin に置かれるが、テストはリポジトリ内の
# 元ファイルを直接叩く。ヘルパの探索がリポジトリ相対で解決できることも同時に見る。
run_launcher() {
    local home="$1" launcher="$2"; shift 2
    env HOME="$home" \
        PI_CODING_AGENT_DIR="${home}/.pi/agent" \
        COMMANDCODE_API_KEY="dummy-for-dry-run" \
        OPENCODE_API_KEY="dummy-for-dry-run" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 \
        zsh "${REPO_ROOT}/.devcontainer/${launcher}" "$@"
}

# このテストが無いと、両方の key があるときに旧い OPENCODE_GO_API_KEY が優先されても気づけず、別のアカウントの key で黙って起動する。
test_pigs_opencode_key_reaches_child_process() {
    local home="${TEST_ROOT}/pigs-opencode-key" shim_dir="${TEST_ROOT}/pigs-opencode-bin"
    local output
    mkdir -p "${home}/.pi/agent/extensions/subagent.d/pigs" "$shim_dir"
    cp "${CONFIG_DIR}/pigs.json" "${home}/.pi/agent/extensions/subagent.d/pigs/config.json"
    printf '%s\n' \
        '{"openai-codex":{"type":"oauth","access":"test-access","refresh":"test-refresh","expires":9999999999999}}' \
        > "${home}/.pi/agent/auth.json"

    # 実ランチャーから exec された bwrap の子で、export された値を観測する。
    # 実際の mount namespace や Pi の API 呼び出しはこのテストには不要。
    cat > "${shim_dir}/bwrap" <<'BWRAP_ENV_EOF'
#!/bin/bash
bash -c 'printf "%s\n" "${OPENCODE_API_KEY-<unset>}"'
BWRAP_ENV_EOF
    chmod +x "${shim_dir}/bwrap"

    output="$(env \
        OPENCODE_API_KEY=dummy-primary OPENCODE_GO_API_KEY=dummy-legacy HOME="$home" \
        PI_CODING_AGENT_DIR="${home}/.pi/agent" PATH="${shim_dir}:${PATH}" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN=0 \
        "${ZSH_BIN}" "${REPO_ROOT}/.devcontainer/pigs")"
    assert_eq "$output" "dummy-primary" "pigs child must prefer OPENCODE_API_KEY over OPENCODE_GO_API_KEY"
}

# dry-run 出力から pi へ渡る引数だけを取り出す（"--" の次の "pi" より後ろ）。
pi_args_of() {
    printf '%s\n' "$1" | awk 'seen { print } $0 == "pi" { seen = 1 }'
}

# このテストが無いと、ランチャーの bind 元・重ね先が逆や別のランチャー名になっても、tier 設定が黙って効かないまま起動する。
test_launchers_wrap_pi_with_their_own_config() {
    local launcher home out pair
    for launcher in "${LAUNCHERS[@]}"; do
        home="$(make_home "$launcher" with-config)"
        write_oauth_auth "$home"
        out="$(run_launcher "$home" "$launcher")"
        assert_eq "$(printf '%s\n' "$out" | sed -n 1p)" "bwrap" "${launcher} argv[0]"
        pair="$(bind_pair_of "$out")" || fail "${launcher} must pass --bind to bwrap"
        assert_eq "$(printf '%s\n' "$pair" | sed -n 1p)" \
            "${home}/.pi/agent/extensions/subagent.d/${launcher}" "${launcher} bind src"
        assert_eq "$(printf '%s\n' "$pair" | sed -n 2p)" \
            "${home}/.pi/agent/extensions/subagent" "${launcher} bind dst"
    done
}

# <option> <args...>: args の中で option の直後にある値を出す。
option_value() {
    local option="$1"
    shift
    while [ "$#" -gt 1 ]; do
        if [ "$1" = "$option" ]; then
            printf '%s\n' "$2"
            return 0
        fi
        shift
    done
    return 1
}

# このテストが無いと、google-vertex を使うランチャーが auth.json の overlay を付け忘れたときに、保存済みの Vertex key・OAuth が overlay なしで Pi に渡り、別の資格情報で黙って動く。
test_only_vertex_launchers_overlay_auth() {
    local launcher home out provider overlay
    local -a own
    for launcher in "${LAUNCHERS[@]}"; do
        home="$(make_home "$launcher" with-config)"
        write_oauth_auth "$home"
        out="$(run_launcher "$home" "$launcher")"
        mapfile -t own < <(pi_args_of "$out")
        provider="$(option_value --provider "${own[@]}")" || provider=""
        overlay=0
        if printf '%s\n' "$out" | awk '$0 == "pi" { exit } $0 == "--ro-bind-data" { found = 1 } END { exit !found }'; then
            overlay=1
        fi
        if [ "$provider" = google-vertex ]; then
            assert_eq "$overlay" 1 "${launcher} passes google-vertex to Pi and must overlay auth.json"
        else
            # 対照: overlay の検出が常に真になる空振りを防ぐ
            assert_eq "$overlay" 0 "${launcher} does not use google-vertex and must not overlay auth.json"
        fi
    done
}

# このテストが無いと、pixg が xai の API key（type: api_key）を受け付けたときに、SuperGrok のサブスクリプションではなく API key の従量課金で黙って起動する。
test_pixg_rejects_non_oauth_xai_credentials() {
    local home status
    home="$(make_home pixg with-config)"
    mkdir -p "${home}/.pi/agent"
    printf '%s\n' '{"xai":{"type":"api_key","key":"dummy"}}' > "${home}/.pi/agent/auth.json"
    set +e
    run_launcher "$home" pixg >/dev/null 2>&1
    status=$?
    set -e
    assert_eq "$status" "1" "pixg must exit 1 for non-oauth xai credentials"

    # 対照: xAI OAuth があれば起動する
    write_oauth_auth "$home"
    assert_eq "$(run_launcher "$home" pixg | sed -n 1p)" "bwrap" "valid xAI OAuth must reach argv"
}

# このテストが無いと、親モデルが未登録でも ID・表示名の部分一致や別 provider の同じ ID で別モデルを黙って起動する。
test_pigt_requires_exact_effective_catalog_model() {
    local root="${TEST_ROOT}/pigt-catalog" home agent package bin out provider model status source
    local -a own
    home="${root}/home with spaces"
    package="${root}/pi-package"
    bin="${root}/bin"
    mkdir -p "$bin" "${home}/.pi/agent/extensions/subagent.d/pigt"
    cp "${CONFIG_DIR}/pigt.json" "${home}/.pi/agent/extensions/subagent.d/pigt/config.json"
    write_oauth_auth "$home"
    out="$(run_launcher "$home" pigt)"
    mapfile -t own < <(pi_args_of "$out")
    provider="$(option_value --provider "${own[@]}")"
    model="$(option_value --model "${own[@]}")"

    # モデル解決は実際の Pi SDK を使う。TUI/API と mount だけを記録用 shim に置き換え、
    # npm package のコピーのカタログを編集する（インストール済みファイルには触れない）。
    cp -a --reflink=auto "$PI_PACKAGE" "$package"
    rm -f -- "${package:?}/dist/bundle/cli.js"
    printf '#!/bin/bash\nprintf "%%s\\0" "$@" > "${FAKE_PI_ARGV:?}"\n' > "${package}/dist/bundle/cli.js"
    chmod +x "${package}/dist/bundle/cli.js"
    ln -s "${package}/dist/bundle/cli.js" "${bin}/pi"
    cat > "${bin}/bwrap" <<'CATALOG_BWRAP_EOF'
#!/bin/bash
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
exec "$@"
CATALOG_BWRAP_EOF
    chmod +x "${bin}/bwrap"
    # 対照の forward テストは既定の agent dir。こちらは ~ 展開と空白を含む別ディレクトリ。
    agent="${home}/.catalog agent"
    mkdir -p "$agent"
    cp "${home}/.pi/agent/auth.json" "${agent}/auth.json"

    python3 - "$package" "$provider" "$model" <<'PY_CATALOG'
import json
import sys
from pathlib import Path

package, provider, model = sys.argv[1:]
data = Path(package) / "node_modules/@earendil-works/pi-ai/dist/providers/data"
path = data / f"{provider}.json"
catalog = json.loads(path.read_text())
api, entries = next(iter(catalog.items()))
example = next(iter(entries.values()))
# 部分一致する ID と表示名だけを残す。同じ ID も別 provider にしか存在しない。
partial = {**example, "id": model + "-preview", "name": model}
path.write_text(json.dumps({api: {f"chat:{partial['id']}": partial}}))
other = data / "openai.json"
other_catalog = json.loads(other.read_text())
other_entries = next(entries for entries in other_catalog.values()
                     if any(entry.get("type", "chat") == "chat" for entry in entries.values()))
other_example = next(entry for entry in other_entries.values() if entry.get("type", "chat") == "chat")
other_entries[f"chat:{model}"] = {**other_example, "id": model}
other.write_text(json.dumps(other_catalog))
(Path(package).parent / "exact-model.json").write_text(json.dumps({**example, "id": model, "name": model}))
PY_CATALOG

    set +e
    env HOME="$home" PI_CODING_AGENT_DIR="~/.catalog agent" PATH="${bin}:${PATH}" \
        PI_SUPERAGENTS_LAUNCHER_DRY_RUN=0 FAKE_PI_ARGV="${root}/argv" \
        "${ZSH_BIN}" "${REPO_ROOT}/.devcontainer/pigt" >"${root}/stdout" 2>"${root}/stderr"
    status=$?
    set -e
    [ ! -e "${root}/argv" ] || fail "pigt must not start a partial-match model or a model from another provider"
    [ "$status" -ne 0 ] || fail "pigt must reject a missing exact provider/model"

    # 対照: キャッシュだけ／models.json だけで完全一致登録した場合も実効一覧から受け付ける。
    for source in cache config; do
        python3 - "$agent" "$provider" "${root}/exact-model.json" "$source" <<'PY_EXACT'
import json
import sys
from pathlib import Path

directory, provider, fixture, source = sys.argv[1:]
agent = Path(directory)
model = json.loads(Path(fixture).read_text())
if source == "cache":
    content = {provider: {"models": [model], "checkedAt": 4102444800000, "lastModified": 4102444800000}}
    (agent / "models-store.json").write_text(json.dumps(content))
else:
    (agent / "models-store.json").unlink()
    model.pop("provider")
    model.pop("type", None)
    (agent / "models.json").write_text(json.dumps({"providers": {provider: {"models": [model]}}}))
PY_EXACT
        env HOME="$home" PI_CODING_AGENT_DIR="~/.catalog agent" PATH="${bin}:${PATH}" \
            PI_SUPERAGENTS_LAUNCHER_DRY_RUN=0 FAKE_PI_ARGV="${root}/argv" \
            "${ZSH_BIN}" "${REPO_ROOT}/.devcontainer/pigt" \
            || fail "pigt must accept an exact model from ${source}"
        [ -f "${root}/argv" ] || fail "pigt did not reach Pi with an exact model from ${source}"
        rm -f -- "${root:?}/argv"
    done
}

# このテストが無いと、ランチャーかヘルパが利用者の引数（空文字・改行・空白・日本語を含む）を落とす・分割する変更が入っても、Pi が別の引数のまま黙って起動する。
test_launchers_forward_user_arguments_to_pi() {
    local root="${TEST_ROOT}/forward-pi" launcher home record i offset
    local bin="${root}/bin"
    local -a user_args got vertex_ids
    user_args=(--session '日本語 のセッション.jsonl' -- $'  1 行目\n2 行目 末尾の空白  ' '')
    mkdir -p "$bin"
    # bwrap の指定（最初の -- まで）を読み飛ばし、残り（pi とその引数）をそのまま実行する
    cat > "${bin}/bwrap" <<'FORWARD_BWRAP_EOF'
#!/bin/bash
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
    shift
done
shift
exec "$@"
FORWARD_BWRAP_EOF
    # 受け取った引数を NUL 区切りで記録する（行区切りでは改行を含む引数を区別できない）
    cat > "${bin}/pi" <<'FORWARD_PI_EOF'
#!/bin/bash
printf '%s\0' "$@" > "${FAKE_PI_ARGV:?}"
FORWARD_PI_EOF
    chmod +x "${bin}/bwrap" "${bin}/pi"
    # pigt の事前検査には実際の SDK を使い、TUI/API だけを上の shim に置き換える。
    printf '{"name":"@earendil-works/pi-coding-agent"}\n' > "${bin}/package.json"
    mkdir -p "${bin}/dist"
    ln -s "${PI_PACKAGE}/dist/core" "${bin}/dist/core"
    ln -s "${PI_PACKAGE}/dist/config.js" "${bin}/dist/config.js"

    # Vertex のランチャーは起動の前に、pi の実体の近くの bundled catalog で model ID を確かめる。
    # ID はヘルパの定義から求め、テストに写さない
    mapfile -t vertex_ids < <("${ZSH_BIN}" -c "source '${HELPER}'; typeset -a reply; for l in \"\$@\"; do pi_google_vertex_models \"\$l\" && print -rl -- \"\${reply[@]}\"; done; true" _ "${LAUNCHERS[@]}" | LC_ALL=C sort -u)
    [ "${#vertex_ids[@]}" -gt 0 ] || fail "pi_google_vertex_models must define Vertex model IDs"
    mkdir -p "${bin}/node_modules/@earendil-works/pi-ai/dist/providers/data"
    python3 - "${bin}/node_modules/@earendil-works/pi-ai/dist/providers/data/google-vertex.json" "${vertex_ids[@]}" <<'PY'
import json
import sys

path, *ids = sys.argv[1:]
catalog = {f"chat:{id_}": {"id": id_, "provider": "google-vertex", "api": "google-vertex"} for id_ in ids}
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"google-vertex": catalog}, handle)
PY

    record="${root}/argv"
    for launcher in "${LAUNCHERS[@]}"; do
        home="${root}/home-${launcher}"
        mkdir -p "${home}/.pi/agent/extensions/subagent.d/${launcher}"
        printf '{"superagents":{"modelTiers":{}}}\n' \
            > "${home}/.pi/agent/extensions/subagent.d/${launcher}/config.json"
        write_oauth_auth "$home"
        if [ "$launcher" = pigt ]; then
            local out provider model
            local -a own
            out="$(run_launcher "$home" "$launcher")"
            mapfile -t own < <(pi_args_of "$out")
            provider="$(option_value --provider "${own[@]}")"
            model="$(option_value --model "${own[@]}")"
            python3 - "${home}/.pi/agent/models.json" "$provider" "$model" "$PI_PACKAGE" <<'PY_PARENT'
import json
import sys
from pathlib import Path

file, provider, model, package = sys.argv[1:]
catalog = json.loads((Path(package) / f"node_modules/@earendil-works/pi-ai/dist/providers/data/{provider}.json").read_text())
example = next(iter(next(iter(catalog.values())).values()))
parent = {**example, "id": model}
parent.pop("provider")
parent.pop("type", None)
Path(file).write_text(json.dumps({"providers": {provider: {"models": [parent]}}}))
PY_PARENT
        fi
        rm -f "$record"
        env HOME="$home" PI_CODING_AGENT_DIR="${home}/.pi/agent" \
            COMMANDCODE_API_KEY=dummy-for-forward OPENCODE_API_KEY=dummy-for-forward \
            PATH="${bin}:${PATH}" PI_SUPERAGENTS_LAUNCHER_DRY_RUN=0 FAKE_PI_ARGV="$record" \
            "${ZSH_BIN}" "${REPO_ROOT}/.devcontainer/${launcher}" "${user_args[@]}" \
            || fail "${launcher} must start pi"
        # 対照: fake pi が実際に起動した
        [ -f "$record" ] || fail "${launcher} did not reach pi"
        mapfile -d '' -t got < "$record"
        [ "${#got[@]}" -ge "${#user_args[@]}" ] || fail "${launcher} dropped user arguments"
        offset=$(( ${#got[@]} - ${#user_args[@]} ))
        for i in "${!user_args[@]}"; do
            assert_eq "${got[$((offset + i))]}" "${user_args[$i]}" "${launcher} user argument #${i}"
        done
    done
}

SETUP="${REPO_ROOT}/scripts/setup_pi_superagents.zsh"

run_setup() {
    local home="$1"; shift
    env HOME="$home" zsh "$SETUP" "$@"
}

# このテストが無いと、手で setup を実行して tier 表を置き直すときに、退避が壊れて利用者が編集した設定を黙って失う。
test_setup_backs_up_modified_config() {
    local home config edited backup found=0
    home="${TEST_ROOT}/setup-backup"
    mkdir -p "$home"
    run_setup "$home" >/dev/null
    config="${home}/.pi/agent/extensions/subagent.d/pigo/config.json"
    edited='{"superagents":{"modelTiers":{}}}'
    printf '%s\n' "$edited" > "$config"
    run_setup "$home" >/dev/null
    for backup in "${config}".bak.*; do
        if [ -f "$backup" ] && [ "$(<"$backup")" = "$edited" ]; then
            found=1
        fi
    done
    [ "$found" = 1 ] || fail "setup must back up the user's edited config"
    diff -q "${CONFIG_DIR}/pigo.json" "$config" >/dev/null \
        || fail "setup must restore the repository content"
}

# このテストが無いと、リビルドのたびに導入が --if-absent で呼ぶ setup が既存の設定を置き換えて利用者が編集した tier 設定を黙って失う、または README が案内する --if-absent の更新が成功したまま古い共通ヘルパを残す。
test_setup_if_absent_preserves_user_edits() {
    local home config helper
    home="${TEST_ROOT}/setup-if-absent"
    mkdir -p "$home"
    run_setup "$home" >/dev/null
    config="${home}/.pi/agent/extensions/subagent.d/piccd/config.json"
    helper="${home}/.local/bin/pi-superagents-exec.zsh"
    printf '{"superagents":{"modelTiers":{"cheap":{"model":"x/y","thinking":"max"}}}}\n' > "$config"
    # 前の版のヘルパが配置済みの環境（リビルドしない既存環境）
    printf '%s\n' '# stale helper' > "$helper"
    run_setup "$home" --if-absent >/dev/null
    grep -q '"x/y"' "$config" || fail "--if-absent must not overwrite an existing config"
    cmp -s "$HELPER" "$helper" || fail "--if-absent must refresh the shared helper from the repository"
}

INSTALL_SCRIPT="${REPO_ROOT}/.devcontainer/install_beforehand.bash"

# ---------------------------------------------------------------- pi 拡張の導入の事後確認
# install_beforehand.bash 全体は sudo apt 等を含みテストで実行できないため、
# pi-superagents の導入だけを関数 install_pi_extensions() として取り出し、
# fake pi を PATH に載せた独立スクリプトで実行する。fake pi は TEST_ROOT 配下の
# registry を一覧として返し、FAKE_PI_MISSING_NEW=1 なら導入したものを一覧から外す。
FAKE_PI="${TEST_ROOT}/bin/pi"
FAKE_STATE="${TEST_ROOT}/pi-state/registry"

make_fake_pi() {
    mkdir -p "${TEST_ROOT}/bin" "${TEST_ROOT}/pi-state" "${TEST_ROOT}/fake-home"
    cat > "$FAKE_PI" <<'FAKE_PI_EOF'
#!/bin/bash
set -u
state="${PI_FAKE_STATE:?}"
cmd="${1:-}"
shift || true
case "$cmd" in
    list)
        if [ "${FAKE_PI_MISSING_NEW:-0}" = 1 ]; then
            [ -f "$state" ] && grep -v '^npm:@teelicht/pi-superagents$' "$state" || true
            exit 0
        fi
        [ -f "$state" ] && cat "$state" || true
        ;;
    install)
        touch "$state"
        if ! grep -qx "$2" "$state"; then
            printf '%s\n' "$2" >> "$state"
        fi
        ;;
    *)
        echo "fake pi: unexpected subcommand: ${cmd}" >&2
        exit 1
        ;;
esac
FAKE_PI_EOF
    chmod +x "$FAKE_PI"
}

# install_beforehand.bash の install_pi_extensions() 関数だけを取り出して実行する。
run_install_fragment() {
    local src="${TEST_ROOT}/fragment.bash"
    awk '/^install_pi_extensions\(\) \{/{p=1} p{print} p && /^\}/{exit}' "$INSTALL_SCRIPT" > "$src"
    printf 'install_pi_extensions\n' >> "$src"
    env HOME="${TEST_ROOT}/fake-home" PATH="${TEST_ROOT}/bin:${PATH}" \
        PI_FAKE_STATE="$FAKE_STATE" \
        bash "$src"
}

# このテストが無いと、pi install が 0 で終わっても登録されないときに、導入が pi-superagents の無い環境を成功として作り終える。
test_install_fails_when_new_missing_after() {
    local status
    # 対照: 導入され一覧に現れれば成功する
    rm -f "$FAKE_STATE"
    run_install_fragment >/dev/null 2>&1 || fail "install_pi_extensions must succeed when the extension is listed"

    rm -f "$FAKE_STATE"
    set +e
    FAKE_PI_MISSING_NEW=1 run_install_fragment >/dev/null 2>&1
    status=$?
    set -e
    [ "$status" -ne 0 ] || fail "install_pi_extensions must fail when the extension is missing after install"
}

test_bind_target_ignores_pi_coding_agent_dir
test_missing_config_fails_closed
test_actual_and_dry_run_argv_match
test_launchers_wrap_pi_with_their_own_config
test_only_vertex_launchers_overlay_auth
test_pigs_opencode_key_reaches_child_process
test_pixg_rejects_non_oauth_xai_credentials
test_launchers_forward_user_arguments_to_pi
test_pigt_requires_exact_effective_catalog_model
test_setup_backs_up_modified_config
test_setup_if_absent_preserves_user_edits
make_fake_pi
test_install_fails_when_new_missing_after
printf 'OK test-pi-superagents\n'
