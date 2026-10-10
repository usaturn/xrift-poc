// dshccd / dshgo の pi-tui セッションで、実行中の provider / model を
// モデル向けシェル（bash / pwsh）へ DSH_* として渡す Cordis プラグイン（#577）。
//
// 値は dsh-shell-env の contributor としてシェル呼び出しごとに解決する。参照先は
// 呼び出し元エージェントのセッションが持つ現在のリクエストヘッダで、DSH 本体の
// dsh-mcp-client / dsh-tool-fs / dsh-spill-policy も同じ経路でその実行の経路モデルを
// 読む。固定値・保存設定・セッション履歴は使わないため、/model の切替、セッション
// 再開、subagent のいずれでも呼び出し元エージェントの経路が返る。
//
// 事実が解決できない場合（agent を持たない実行、リクエストヘッダ未確定）は何も
// 登録しない。pi-model-name 側はそのとき推測せず停止する。
//
// 依存パッケージを持たない単一ファイル。install_beforehand.bash が
// ~/.local/share/maatlog/dsh-model-env.mjs へ配置し、dshccd / dshgo が
// dsh-model-env.patch.yml から絶対パスで読み込む。

export const name = 'maatlog-dsh-model-env'

// dsh-base が host plane に置く shell 環境レジストリを injected service として要求する。
export const inject = ['shellEnv']

const VARIABLES = {
  DSH_PROVIDER: {
    description: 'Registered provider route this shell call runs under.',
  },
  DSH_MODEL: {
    description: 'Provider-owned model id this shell call runs under.',
  },
  DSH_REASONING_EFFORT: {
    description: 'Reasoning effort in force for this shell call, when the route declares one.',
  },
}

/** 呼び出し元エージェントの現在の経路を DSH_* の値へ写す。 */
function resolveModelEnv(execution) {
  const config = execution.agent?.session?.requestHeader?.()?.config
  if (config === undefined || typeof config !== 'object' || config === null) return {}
  const provider = config.provider
  const model = config.model
  if (typeof provider !== 'string' || provider === '' || typeof model !== 'string' || model === '') return {}
  const reasoningEffort = config.reasoningEffort
  return {
    DSH_PROVIDER: provider,
    DSH_MODEL: model,
    ...typeof reasoningEffort === 'string' && reasoningEffort !== '' ? { DSH_REASONING_EFFORT: reasoningEffort } : {},
  }
}

export function apply(ctx) {
  ctx.shellEnv.register({
    name,
    variables: VARIABLES,
    resolve: resolveModelEnv,
  })
}
