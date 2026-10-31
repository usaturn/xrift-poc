#
# pi-superagents のランチャー別 model tier 設定を Pi に見せるための共通ヘルパ。
#
# pi-superagents は設定を os.homedir()/.pi/agent/extensions/subagent/config.json
# から読む。このパスは拡張のソースに直接書かれており、環境変数による override が
# 無い。そこで bwrap の mount namespace を使い、ランチャー固有のディレクトリを
# その共有パスへプロセスツリー単位で重ねる。
#
#   - HOME を変えないので git / ssh の設定の見え方には影響しない
#   - 共有ファイルをコピー・上書きしないので、同時起動でも競合しない
#   - mount namespace はプロセスツリー固有なので、別ランチャーからは見えない
#   - 子 Pi は namespace を継承するので、親子で同じ tier を見る
#
# ただし bwrap は no_new_privs を立てるため、このヘルパ経由で起動した Pi の
# 配下では sudo / su などの setuid バイナリが使えない。
#
# tier config の bind 先は必ず HOME から組み立てる。PI_CODING_AGENT_DIR は Pi 本体の設定
# ディレクトリを移すだけで、拡張の読み先は動かないため、ここで使うと bind 先が
# 実際の読み先とずれる。
#
# 前提が欠けたときは共有 config へ暗黙にフォールバックせず、起動を止める。
# 黙って落ちると tier が警告なく混線するため。
#
# PI_SUPERAGENTS_LAUNCHER_DRY_RUN=1 のときは exec せず、実行予定の argv を
# 1 行 1 要素で出力する。テスト用。
#

pi_openai_codex_require_oauth() {
    local agent_dir="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
    if [[ "${agent_dir}" == "~" ]]; then
        agent_dir="${HOME}"
    elif [[ "${agent_dir}" == "~/"* ]]; then
        agent_dir="${HOME}/${agent_dir#'~/'}"
    fi
    local auth_file="${agent_dir}/auth.json"

    if [[ -f "${auth_file}" ]] && node -e '
const fs = require("node:fs");
let credential;
try {
    credential = JSON.parse(fs.readFileSync(process.argv[1], "utf8"))["openai-codex"];
} catch {
    process.exit(1);
}
process.exit(credential?.type === "oauth" &&
    typeof credential.access === "string" && credential.access.length > 0 &&
    typeof credential.refresh === "string" && credential.refresh.length > 0 &&
    typeof credential.expires === "number" ? 0 : 1);
' "${auth_file}" >/dev/null 2>&1; then
        return 0
    fi

    print -ru2 -- "OpenAI Codex OAuth authentication is not configured."
    print -ru2 -- ""
    print -ru2 -- "Run:"
    print -ru2 -- "  pi"
    print -ru2 -- ""
    print -ru2 -- "and execute:"
    print -ru2 -- "  /login openai-codex"
    return 1
}

# fuzzy な CLI 検索ではなく、Pi と同じ実効一覧で provider / ID の完全一致を確かめる。
# カタログ更新・認証更新・!command の実行はしない。キャッシュは読み取り専用の
# snapshot として渡し、FileModelsStore が読み取り時に作るファイルや lock も残さない。
pi_require_exact_model() {
    local provider="$1" model="$2"
    if ! node --input-type=module -e '
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
try {
    let dir = path.dirname(fs.realpathSync(process.argv[1]));
    while (true) {
        const file = path.join(dir, "package.json");
        if (fs.existsSync(file) && JSON.parse(fs.readFileSync(file, "utf8")).name === "@earendil-works/pi-coding-agent") break;
        const parent = path.dirname(dir);
        if (parent === dir) throw new Error();
        dir = parent;
    }
    const load = (file) => import(pathToFileURL(path.join(dir, file)).href);
    const { ModelRuntime } = await load("dist/core/model-runtime.js");
    const { AuthStorage } = await load("dist/core/auth-storage.js");
    const { getAgentDir } = await load("dist/config.js");
    let cache = {};
    try {
        cache = JSON.parse(fs.readFileSync(path.join(getAgentDir(), "models-store.json"), "utf8").replace(/^\uFEFF/, ""));
    } catch (error) {
        if (error.code !== "ENOENT") throw error;
    }
    const runtime = await ModelRuntime.create({
        credentials: AuthStorage.inMemory(),
        refreshOnCreate: false,
        modelsStore: {
            read: async (provider) => cache[provider],
            write: async () => { throw new Error(); },
            delete: async () => { throw new Error(); },
        },
    });
    const [provider, id] = process.argv.slice(2);
    const result = await runtime.refresh({ providers: [provider], allowNetwork: false });
    if (result.errors.size || runtime.getError() ||
        !runtime.getModels(provider).some((model) => model.provider === provider && model.id === id)) {
        throw new Error();
    }
} catch {
    // パーサーや SDK の例外には個人設定の値が含まれ得るため表示しない。
    process.exit(1);
}
' "$(command -v pi)" "${provider}" "${model}" >/dev/null 2>&1; then
        print -ru2 -- "ERROR: ${provider}/${model} の完全一致登録を実効モデル一覧で確認できません。起動を中止します。"
        print -ru2 -- "利用者自身で pi update --models を実行し、provider / model ID の登録を確認してください。"
        return 1
    fi
}

pi_xai_require_oauth() {
    local agent_dir="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
    if [[ "${agent_dir}" == "~" ]]; then
        agent_dir="${HOME}"
    elif [[ "${agent_dir}" == "~/"* ]]; then
        agent_dir="${HOME}/${agent_dir#'~/'}"
    fi
    local auth_file="${agent_dir}/auth.json"

    if [[ -f "${auth_file}" ]] && node -e '
const fs = require("node:fs");
let credential;
try {
    credential = JSON.parse(fs.readFileSync(process.argv[1], "utf8"))["xai"];
} catch {
    process.exit(1);
}
process.exit(credential?.type === "oauth" &&
    typeof credential.access === "string" && credential.access.length > 0 &&
    typeof credential.refresh === "string" && credential.refresh.length > 0 &&
    typeof credential.expires === "number" ? 0 : 1);
' "${auth_file}" >/dev/null 2>&1; then
        return 0
    fi

    print -ru2 -- "xAI subscription authentication is not configured."
    print -ru2 -- ""
    print -ru2 -- "Run:"
    print -ru2 -- "  pi"
    print -ru2 -- ""
    print -ru2 -- "and execute:"
    print -ru2 -- "  /login xai"
    print -ru2 -- ""
    print -ru2 -- "Then sign in with:"
    print -ru2 -- "  SuperGrok or X Premium"
    return 1
}

# Vertex ランチャーの一覧と、起動前に bundled catalog で確かめる exact model ID は
# ここだけで定義する。ID は親 Pi の --model と、.devcontainer/pi-superagents/<launcher>.json
# の tier が使う google-vertex のモデルに合わせる。Vertex ランチャーなら ID を reply に
# 入れて 0 を返し、それ以外は 1 を返す。呼び出し側は local -a reply を宣言しておく。
pi_google_vertex_models() {
    case "$1" in
        pigemf) reply=(gemini-3.8-flash) ;;
        pigemp) reply=(gemini-3.8-flash gemini-3.1-pro-preview) ;;
        pigema) reply=(gemini-3.8-flash gemini-4-argon) ;;
        *) return 1 ;;
    esac
}

# メタデータだけを読む。Pi CLI / provider availability API は認証を調べるため
# 使わない。npm で配置された Pi の実体から bundled catalog を探す。
pi_google_vertex_require_models() {
    local launcher="$1"
    local -a reply
    pi_google_vertex_models "${launcher}" || return 1
    if ! node -e '
const fs = require("node:fs");
const path = require("node:path");
try {
    let dir = path.dirname(fs.realpathSync(process.argv[1]));
    let catalog;
    while (true) {
        const file = path.join(dir, "node_modules/@earendil-works/pi-ai/dist/providers/data/google-vertex.json");
        if (fs.existsSync(file)) {
            catalog = JSON.parse(fs.readFileSync(file, "utf8"))["google-vertex"];
            break;
        }
        const parent = path.dirname(dir);
        if (parent === dir) throw new Error();
        dir = parent;
    }
    for (const id of process.argv.slice(2)) {
        const model = catalog?.[`chat:${id}`];
        if (model?.id !== id || model.provider !== "google-vertex" || model.api !== "google-vertex") {
            throw new Error();
        }
    }
} catch {
    process.exit(1);
}
' "$(command -v pi)" "${reply[@]}" >/dev/null 2>&1; then
        print -ru2 -- "[${launcher}] ERROR: Vertex model catalog が無いか、必要な exact model ID がありません。Pi を更新してください。"
        return 1
    fi
}

# auth.json は設定としてだけ読む。credential の検証、!command の実行、ADC の
# 探索はしない。出力は呼び出し元が変数に取り込み、pipe FD で bwrap に渡す。
pi_google_vertex_auth_overlay() {
    node -e '
const fs = require("node:fs");
try {
    let auth = {};
    try {
        auth = JSON.parse(fs.readFileSync(process.argv[1], "utf8").replace(/^\uFEFF/, ""));
    } catch (error) {
        if (error.code !== "ENOENT") throw error;
        // bwrap は存在しない mount 先を host に空ファイルとして作る。そのままだと
        // 次の素の Pi が JSON を読めないため、未作成時だけ空の設定を一括で置く。
        // link は既存ファイルを置き換えず、同時起動でも先に作られた設定を尊重する。
        const path = require("node:path");
        const dir = path.dirname(process.argv[1]);
        fs.mkdirSync(dir, { recursive: true });
        const staging = fs.mkdtempSync(path.join(dir, ".pi-vertex-mount-"));
        try {
            const empty = path.join(staging, "auth.json");
            fs.writeFileSync(empty, "{}\n", { mode: 0o600 });
            try {
                fs.linkSync(empty, process.argv[1]);
            } catch (linkError) {
                if (linkError.code !== "EEXIST") throw linkError;
            }
        } finally {
            fs.rmSync(staging, { recursive: true, force: true });
        }
        auth = JSON.parse(fs.readFileSync(process.argv[1], "utf8").replace(/^\uFEFF/, ""));
    }
    if (!auth || typeof auth !== "object" || Array.isArray(auth)) throw new Error();
    const adcPath = auth["google-vertex"]?.env?.GOOGLE_APPLICATION_CREDENTIALS;
    // Pi は起動時やモデル切替時にも OAuth を更新する。readonly の snapshot では
    // 更新後の token を保存できないため、期限にかかわらず OAuth entry を外す。
    for (const [provider, credential] of Object.entries(auth)) {
        if (credential?.type === "oauth") delete auth[provider];
    }
    // Pi native google-vertex API はこの marker を API key ではなく ADC と扱う。
    auth["google-vertex"] = { type: "api_key", key: "gcp-vertex-credentials" };
    if (typeof adcPath === "string" && adcPath.length > 0) {
        auth["google-vertex"].env = { GOOGLE_APPLICATION_CREDENTIALS: adcPath };
    }
    process.stdout.write(JSON.stringify(auth));
} catch {
    // JSON parser の例外には秘密が含まれる可能性があるので表示しない。
    process.stderr.write("ERROR: auth.json の設定を読み取れません。起動を中止します。\n");
    process.exit(1);
}
' "$1"
}

pi_superagents_exec() {
    local launcher="$1"
    shift

    local shared_dir="${HOME}/.pi/agent/extensions/subagent"
    local launcher_dir="${HOME}/.pi/agent/extensions/subagent.d/${launcher}"
    local config="${launcher_dir}/config.json"

    if ! command -v bwrap >/dev/null 2>&1; then
        print -ru2 -- "[${launcher}] ERROR: bwrap (bubblewrap) が見つかりません。"
        print -ru2 -- ""
        print -ru2 -- "pi-superagents の model tier をランチャーごとに分離するために必要です。"
        print -ru2 -- ""
        print -ru2 -- "  sudo apt install -y bubblewrap"
        return 1
    fi

    if [[ ! -f "${config}" ]]; then
        print -ru2 -- "[${launcher}] ERROR: pi-superagents の設定が見つかりません:"
        print -ru2 -- "  ${config}"
        print -ru2 -- ""
        print -ru2 -- "リポジトリのルートで setup スクリプトを一度実行してください:"
        print -ru2 -- ""
        print -ru2 -- "  zsh scripts/setup_pi_superagents.zsh"
        return 1
    fi

    # 注: zsh では $@ は特殊配列 argv に他ならないため、ローカル変数を argv と
    # 名付けると位置パラメータを影(shadow)にしてしまう。args_ などの別名で
    # 宣言して、pi への引数引き継ぎ(下の "$@")が空にならないようにする。
    local -a args_
    args_=(
        bwrap
        --dev-bind / /
        --die-with-parent
    )

    local agent_dir auth_file vertex=0
    local -a reply
    if pi_google_vertex_models "${launcher}"; then
        vertex=1
        agent_dir="${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}"
        if [[ "${agent_dir}" == "~" ]]; then
            agent_dir="${HOME}"
        elif [[ "${agent_dir}" == "~/"* ]]; then
            agent_dir="${HOME}/${agent_dir#'~/'}"
        fi
        agent_dir="${agent_dir:A}"
        # 子 Pi の cwd が変わっても同じ設定と overlay を読むよう絶対パスにする。
        export PI_CODING_AGENT_DIR="${agent_dir}"
        auth_file="${agent_dir}/auth.json"

        # auth のファイルだけを重ねる。settings / sessions とそれらの lock は
        # 元のディレクトリに残し、素の Pi や別ランチャーとの永続化・排他を保つ。
        args_+=(--ro-bind-data 3 "${auth_file}")
    fi

    args_+=(
        --bind "${launcher_dir}" "${shared_dir}"
        --
        # Pi 1.0.0 の fullscreen 既定を避け、Herdr の通常のスクロール履歴を残す。
        pi --tui-mode regular "$@"
    )

    if [[ "${PI_SUPERAGENTS_LAUNCHER_DRY_RUN:-}" == "1" ]]; then
        printf '%s\n' "${args_[@]}"
        return 0
    fi

    if (( vertex )); then
        pi_google_vertex_require_models "${launcher}" || return 1
        local auth_overlay
        auth_overlay="$(pi_google_vertex_auth_overlay "${auth_file}")" || return 1
        if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            local saved_adc
            # marker の key 分岐は saved env を SDK に渡さないため実効環境へ移す。
            # 同じ snapshot を stdin で読み、末尾の改行も失わないよう点を付ける。
            saved_adc="$(printf '%s' "${auth_overlay}" | node -e '
const fs = require("node:fs");
try {
    const auth = JSON.parse(fs.readFileSync(0, "utf8"));
    process.stdout.write((auth["google-vertex"].env?.GOOGLE_APPLICATION_CREDENTIALS ?? "") + ".");
} catch {
    process.stderr.write("ERROR: ADC パスの設定を読み取れません。起動を中止します。\n");
    process.exit(1);
}
')" || return 1
            saved_adc="${saved_adc%.}"
            if [[ -n "${saved_adc}" ]]; then
                export GOOGLE_APPLICATION_CREDENTIALS="${saved_adc}"
            fi
        fi
        # <(...) は pipe。here-string / 一時ファイルや argv に秘密を置かず、
        # Pi の TUI 用 stdin は元の端末のままにする。FD 3 は bwrap が消費する。
        exec 3< <(printf '%s' "${auth_overlay}")
        unset auth_overlay
    fi

    # bwrap は bind 先が存在することを要求する。dry-run に副作用を残さないよう、
    # 実際に起動するときだけ用意する。
    mkdir -p "${shared_dir}"

    exec "${args_[@]}"
}
