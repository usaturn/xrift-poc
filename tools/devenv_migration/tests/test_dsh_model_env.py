"""#577: DSH のシェルへ実行中 provider / model を渡すプラグインの仕様テスト。

`dsh-model-env.mjs` は dsh-shell-env の contributor として、呼び出し元
エージェントの現在のリクエストヘッダから provider / model を解決する。ここでは
導入済みの実 dsh と実 `@deepseek-ai/dsh-shell-env` を、依存を持たない一時 profile
で起動し、登録・解決・取りこぼし時の挙動を確認する。モデルの API 呼び出しは不要。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

DEVCONTAINER = Path(__file__).resolve().parents[3] / ".devcontainer"
PLUGIN = DEVCONTAINER / "dsh-model-env.mjs"
PROFILE = "model-env-probe"

pytestmark = pytest.mark.skipif(
    shutil.which("dsh") is None, reason="dsh is not installed; the registry mount needs the real harness"
)

# 起動したハーネスの中で shellEnv.collect() を実行し、結果を 1 行で出すだけの探針。
# collect は同期 API なので、書き込みの完了を待ってから終了する。
PROBE = """
export const name = 'model-env-probe'
export const inject = ['shellEnv']

export function apply(ctx) {
  const route = (config) => ({ session: { header: { id: 's-1' }, requestHeader: () => config } })
  const facts = (execution) => {
    const collected = ctx.shellEnv.collect(execution)
    return {
      provider: collected.DSH_PROVIDER ?? null,
      model: collected.DSH_MODEL ?? null,
      reasoningEffort: collected.DSH_REASONING_EFFORT ?? null,
      sessionId: collected.DSH_SESSION_ID ?? null,
    }
  }
  const payload = {
    listed: ctx.shellEnv.list().filter((entry) => entry.contributor === 'maatlog-dsh-model-env'),
    withAgent: facts({
      name: 'bash',
      agent: route({ config: { provider: 'opencode-go', model: 'deepseek-v4.1-flash', reasoningEffort: 'max' } }),
    }),
    withoutEffort: facts({
      name: 'bash',
      agent: route({ config: { provider: 'commandcode', model: 'deepseek/deepseek-v4.1-flash' } }),
    }),
    withoutAgent: facts({ name: 'bash' }),
    withoutHeader: facts({ name: 'bash', agent: route(undefined) }),
    withoutModel: facts({ name: 'bash', agent: route({ config: { provider: 'opencode-go' } }) }),
  }
  process.stdout.write('PROBE_RESULT=' + JSON.stringify(payload) + '\\n', () => process.exit(0))
}
"""


def run_probe(tmp_path: Path) -> dict[str, object]:
    home = tmp_path / "home"
    dsh_home = tmp_path / "dsh-home"
    profile = dsh_home / "profiles" / PROFILE
    profile.mkdir(parents=True)
    home.mkdir()
    (profile / "package.json").write_text(
        json.dumps({"name": PROFILE, "private": True, "dsh": {"profile": {"bundles": []}}}) + "\n"
    )
    (profile / "cordis.yml").write_text("[]\n")
    probe = tmp_path / "probe.mjs"
    probe.write_text(PROBE)
    patch = tmp_path / "probe.patch.yml"
    patch.write_text(
        "- insert:\n"
        "    - id: shell-env\n"
        "      name: '@deepseek-ai/dsh-shell-env'\n"
        "    - id: maatlog-dsh-model-env\n"
        f"      name: '{PLUGIN}'\n"
        "    - id: model-env-probe\n"
        f"      name: '{probe}'\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith(("DSH_", "PI_"))}
    env.update({"HOME": str(home), "DSH_HOME": str(dsh_home)})
    result = subprocess.run(
        ["dsh", "--profile", PROFILE, "--patch", str(patch)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_RESULT=")]
    assert len(lines) == 1, f"expected exactly one probe result, got: {result.stdout}"
    return cast(dict[str, object], json.loads(lines[0].removeprefix("PROBE_RESULT=")))


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    # 実 dsh の起動は 1 回にまとめ、同じ観測結果を各テストで検証する。
    return run_probe(tmp_path_factory.mktemp("dsh-model-env"))


def test_plugin_declares_the_model_facts(probe: dict[str, object]) -> None:
    listed = cast(list[dict[str, str]], probe["listed"])
    assert {entry["key"] for entry in listed} == {"DSH_MODEL", "DSH_PROVIDER", "DSH_REASONING_EFFORT"}
    assert all(entry["description"] for entry in listed)
    assert all(entry["contributor"] == "maatlog-dsh-model-env" for entry in listed)


def test_shell_call_reports_the_calling_agents_route(probe: dict[str, object]) -> None:
    assert probe["withAgent"] == {
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "reasoningEffort": "max",
        "sessionId": "s-1",
    }
    # reasoning effort を宣言しない経路では、その事実だけが欠ける。
    assert probe["withoutEffort"] == {
        "provider": "commandcode",
        "model": "deepseek/deepseek-v4.1-flash",
        "reasoningEffort": None,
        "sessionId": "s-1",
    }


@pytest.mark.parametrize("case", ["withoutAgent", "withoutHeader", "withoutModel"])
def test_unresolvable_route_registers_no_model_facts(probe: dict[str, object], case: str) -> None:
    facts = cast(dict[str, object], probe[case])
    assert facts["provider"] is None and facts["model"] is None and facts["reasoningEffort"] is None
