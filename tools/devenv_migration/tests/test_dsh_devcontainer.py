"""Issue #282 / #516 の仕様テスト: Dev Container に dsh と対話用 pi-tui を追加する。

`.devcontainer/` の次の仕様を固定する。

- `initialize.bash` が、`devcontainer.json` の bind mount の source をホスト側に作る
- `.devcontainer/dsh-cordis.patch.yml` の雛形が、installer の事後確認（inventory の無効化と
  provider の entry）を満たす
- `install_beforehand.bash` が dsh / pi-tui / Command Code plugin を宣言した版で導入し、
  web / pi-tui の解決後の provider 設定と terminal bundle を検証する
- 既存 patch の利用者 entry とコメントを保持し、managed block は一度だけ追加する
- 旧 settings.yaml は警告だけを出して保持する
- README の移行手順・バックアップと復旧の手順・確認手順は、実際に実行して確かめる

`install_deepseek_harness()` は fake の npm / dsh を PATH に置いて挙動を検証する。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

DEVCONTAINER_DIR = Path(__file__).resolve().parents[3] / ".devcontainer"
DEVCONTAINER_JSON = DEVCONTAINER_DIR / "devcontainer.json"
INITIALIZE_SCRIPT = DEVCONTAINER_DIR / "initialize.bash"
INSTALL_SCRIPT = DEVCONTAINER_DIR / "install_beforehand.bash"
DSH_CORDIS_PATCH = DEVCONTAINER_DIR / "dsh-cordis.patch.yml"
README = DEVCONTAINER_DIR / "README.md"


def _script_constant(name: str) -> str:
    match = re.search(rf'(?m)^readonly {name}="([^"]+)"$', INSTALL_SCRIPT.read_text())
    assert match is not None, f"{name} must be declared in install_beforehand.bash"
    return match.group(1)


# 版と profile の固定値は install_beforehand.bash の宣言から読む（テストに写さない）。
DSH_VERSION = _script_constant("DSH_VERSION")
DSH_PI_TUI_VERSION = _script_constant("DSH_PI_TUI_VERSION")
COMMANDCODE_PROVIDER_VERSION = _script_constant("DSH_COMMANDCODE_PROVIDER_VERSION")
DSH_NATIVE_SCRIPT_ALLOWLIST = _script_constant("DSH_NATIVE_SCRIPT_ALLOWLIST")
DSH_TERMINAL_PROFILE = _script_constant("DSH_TERMINAL_PROFILE")


# --- initialize.bash ---------------------------------------------------------


def _home_bind_mount_sources() -> list[str]:
    """devcontainer.json の bind mount のうち、ホストの HOME 配下にある source（HOME からの相対パス）。"""
    sources = re.findall(r"source=\$\{localEnv:HOME\}/([^,]+),", DEVCONTAINER_JSON.read_text())
    assert sources, "no ${localEnv:HOME} bind mount found in devcontainer.json"
    return sources


# docker run --mount は source が無いと失敗するため、initializeCommand の initialize.bash が
# ホスト側に作る。mount の一覧はテストに写さず、devcontainer.json から読む。
@pytest.mark.parametrize("source", _home_bind_mount_sources())
def test_initialize_creates_every_home_bind_mount_source(tmp_path: Path, source: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["/bin/bash", str(INITIALIZE_SCRIPT)],
        env={"HOME": str(home), "PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (home / source).exists(), f"initialize.bash does not create ~/{source}"


# --- dsh-cordis.patch.yml ----------------------------------------------------


def test_cordis_patch_template_passes_the_installer_checks(tmp_path: Path) -> None:
    # 雛形をそのまま web profile の解決後の構成とみなし、installer の事後確認（inventory の
    # 無効化と provider の entry）を通ることを確かめる。片方だけを変えると失敗する。
    result, _, _ = _run_install_function(tmp_path, dump_config_web=DSH_CORDIS_PATCH.read_text())
    assert result.returncode == 0, result.stderr


def test_install_script_warns_about_a_legacy_settings_file(tmp_path: Path) -> None:
    sentinel = "llm-deepseek:\n  baseURL: https://opencode.ai/zen/go/v1\n"
    result, home, _ = _run_install_function(tmp_path, existing_settings=sentinel)
    assert result.returncode == 0, result.stderr
    assert "settings.yaml" in result.stderr
    assert "migration" in result.stderr.lower() or "移行" in result.stderr
    assert (home / ".dsh/settings.yaml").read_text() == sentinel


def test_install_function_appends_the_managed_block_to_an_existing_patch(tmp_path: Path) -> None:
    existing = "# user comment\n- id: llm-deepseek\n  config:\n    streamIdleTimeoutMs: 1000\n"
    result, home, _ = _run_install_function(tmp_path, existing_cordis_patch=existing)
    assert result.returncode == 0, result.stderr
    text = (home / ".dsh/cordis.patch.yml").read_text()
    assert text.startswith(existing)
    assert text.count("# BEGIN maatlog managed provider config") == 1
    assert "- id: llm-commandcode" in text


def test_install_function_does_not_append_the_managed_block_twice(tmp_path: Path) -> None:
    existing = (
        "# BEGIN maatlog managed provider config\n- id: llm-commandcode\n  config:\n"
        "    apiKeyEnv: COMMANDCODE_API_KEY\n# END maatlog managed provider config\n"
    )
    result, home, _ = _run_install_function(tmp_path, existing_cordis_patch=existing)
    assert result.returncode == 0, result.stderr
    text = (home / ".dsh/cordis.patch.yml").read_text()
    assert text.count("# BEGIN maatlog managed provider config") == 1
    assert text == existing


def test_install_function_replaces_an_empty_patch_with_the_template(tmp_path: Path) -> None:
    result, home, _ = _run_install_function(tmp_path, existing_cordis_patch="[]\n")
    assert result.returncode == 0, result.stderr
    assert (home / ".dsh/cordis.patch.yml").read_text() == DSH_CORDIS_PATCH.read_text()


def test_install_function_replaces_empty_patch_spellings_with_the_template(tmp_path: Path) -> None:
    # "[]" 以外の空の表記（空白・inline comment・null・~）も追記ではなく雛形で置換する。
    # 追記すると root node の後に block sequence が続く壊れた YAML になり、
    # marker 検出で次回以降 short-circuit して修復不能になる。
    for index, existing in enumerate(("[] # empty\n", "[ ]\n", "null\n", "~\n", "{}\n", "{ }\n")):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        result, home, _ = _run_install_function(case_dir, existing_cordis_patch=existing)
        assert result.returncode == 0, result.stderr
        assert (home / ".dsh/cordis.patch.yml").read_text() == DSH_CORDIS_PATCH.read_text(), (
            f"{existing!r} must be replaced by the template, not appended to"
        )


def test_install_function_keeps_comments_when_initializing_an_empty_patch(tmp_path: Path) -> None:
    for index, existing in enumerate(("# my notes\n# keep me\n", "# keep me", "\n# keep me\n\n")):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        result, home, _ = _run_install_function(case_dir, existing_cordis_patch=existing)
        assert result.returncode == 0, result.stderr
        text = (home / ".dsh/cordis.patch.yml").read_text()
        assert text == existing + "\n" + DSH_CORDIS_PATCH.read_text()
        assert text.count("# BEGIN maatlog managed provider config") == 1
        assert "- id: plugin-package-inventory-deepseek\n  config:\n    enabled: false" in text
        rebuild_dir = case_dir / "rebuild"
        rebuild_dir.mkdir()
        result, rebuilt_home, _ = _run_install_function(rebuild_dir, existing_cordis_patch=text)
        assert result.returncode == 0, result.stderr
        assert (rebuilt_home / ".dsh/cordis.patch.yml").read_text() == text


@pytest.mark.parametrize("empty_root", ("[]", "[ ]", "null", "~", "{}", "{ }"))
def test_install_function_preserves_comments_with_empty_patch_spellings(tmp_path: Path, empty_root: str) -> None:
    existing = f"# first note\n{empty_root}\n  # last note"
    result, home, _ = _run_install_function(tmp_path, existing_cordis_patch=existing)
    assert result.returncode == 0, result.stderr
    text = (home / ".dsh/cordis.patch.yml").read_text()
    assert text == "# first note\n  # last note\n" + DSH_CORDIS_PATCH.read_text()
    assert text.count("# BEGIN maatlog managed provider config") == 1
    rebuild_dir = tmp_path / "rebuild"
    rebuild_dir.mkdir()
    result, rebuilt_home, _ = _run_install_function(rebuild_dir, existing_cordis_patch=text)
    assert result.returncode == 0, result.stderr
    assert (rebuilt_home / ".dsh/cordis.patch.yml").read_text() == text


@pytest.mark.parametrize("patch_command_failure", ("mktemp", "cat", "mv"))
def test_install_function_keeps_original_patch_when_comment_initialization_fails(
    tmp_path: Path, patch_command_failure: str
) -> None:
    existing = "# keep me\n[]\n"
    result, home, _ = _run_install_function(
        tmp_path, existing_cordis_patch=existing, patch_command_failure=patch_command_failure
    )
    assert result.returncode != 0
    assert (home / ".dsh/cordis.patch.yml").read_text() == existing
    assert sorted(path.name for path in (home / ".dsh").iterdir()) == ["cordis.patch.yml"]


def test_cordis_patch_is_a_top_level_patch_list() -> None:
    # cordis の patch 層は loader patch entry の配列である。map にすると読み込みが失敗する。
    body = [line for line in DSH_CORDIS_PATCH.read_text().splitlines() if line and not line.startswith("#")]
    assert body, "cordis.patch.yml must not be comments only"
    assert body[0].startswith("- "), f"cordis.patch.yml must start with a list entry: {body[0]!r}"


# --- install_beforehand.bash -------------------------------------------------


def _install_text() -> str:
    return INSTALL_SCRIPT.read_text()


def _dsh_function_text() -> str:
    match = re.search(r"^install_deepseek_harness\(\) \{\n.*?^\}\n", _install_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, "install_deepseek_harness() not found in install_beforehand.bash"
    return match.group(0)


def _writable_dir_helper_text() -> str:
    match = re.search(
        r"^ensure_user_writable_dir\(\) \{\n.*?^\}\n",
        _install_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "ensure_user_writable_dir() not found in install_beforehand.bash"
    return match.group(0)


def _terminal_assert_helper_text() -> str:
    match = re.search(
        r"^assert_dsh_terminal_profile_entries\(\) \{\n.*?^\}\n",
        _install_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "assert_dsh_terminal_profile_entries() not found in install_beforehand.bash"
    return match.group(0)


def _ensure_helper_text() -> str:
    match = re.search(
        r"^ensure_dsh_cordis_patch\(\) \{\n.*?^\}\n",
        _install_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "ensure_dsh_cordis_patch() not found in install_beforehand.bash"
    return match.group(0)


def _provider_assert_helper_text() -> str:
    match = re.search(
        r"^assert_dsh_provider_entries\(\) \{\n.*?^\}\n",
        _install_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "assert_dsh_provider_entries() not found in install_beforehand.bash"
    return match.group(0)


# --- install_deepseek_harness() behaviour ------------------------------------


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


DISABLED_DUMP_CONFIG = """\
- id: agent
  name: '@deepseek-ai/dsh-agent'
- id: plugin-package-inventory-deepseek
  name: '@deepseek-ai/dsh-plugin-package-inventory-deepseek'
  config:
    enabled: false
- id: llm-deepseek
  name: '@deepseek-ai/dsh-llm-deepseek'
"""

# 当該 entry に config が無く、直後の別 entry が enabled: false を持つ形。
# ブロック境界を見ずに後続行を拾う実装だと、ここで誤って合格してしまう。
ENABLED_DUMP_CONFIG = """\
- id: agent
  name: '@deepseek-ai/dsh-agent'
- id: plugin-package-inventory-deepseek
  name: '@deepseek-ai/dsh-plugin-package-inventory-deepseek'
- id: some-other-plugin
  name: '@deepseek-ai/dsh-some-other-plugin'
  config:
    enabled: false
- id: llm-deepseek
  name: '@deepseek-ai/dsh-llm-deepseek'
"""


PROVIDER_DUMP_CONFIG_WEB = """\
- id: agent
  name: '@deepseek-ai/dsh-agent'
- id: plugin-package-inventory-deepseek
  name: '@deepseek-ai/dsh-plugin-package-inventory-deepseek'
  config:
    enabled: false
- id: llm-pi-ai
  config:
    providers:
      opencode-go:
        apiKeyEnv: OPENCODE_API_KEY
        models:
          - id: deepseek-v4.1-flash
- id: llm-commandcode
  config:
    apiKeyEnv: COMMANDCODE_API_KEY
    apiBase: https://api.commandcode.ai
    webSearch: false
- id: agent-default-model
  config:
    provider: opencode-go
    model: deepseek-v4.1-flash
    reasoningEffort: max
"""


PROVIDER_DUMP_CONFIG_PI_TUI = """\
- id: plugin-package-inventory-deepseek
  name: '@deepseek-ai/dsh-plugin-package-inventory-deepseek'
  config:
    enabled: false
# == @xmoon76/dsh-pi-tui
- id: tui-app
  name: '@xmoon76/dsh-pi-tui'
# == @mars-sea/dsh-commandcode-provider
- id: llm-commandcode
  name: '@mars-sea/dsh-commandcode-provider'
  config:
    apiKeyEnv: COMMANDCODE_API_KEY
    apiBase: https://api.commandcode.ai
    webSearch: false
- id: llm-pi-ai
  config:
    providers:
      opencode-go:
        apiKeyEnv: OPENCODE_API_KEY
        models:
          - id: deepseek-v4.1-flash
- id: agent-default-model
  config:
    provider: opencode-go
    model: deepseek-v4.1-flash
    reasoningEffort: max
"""


def _run_install_function(
    tmp_path: Path,
    *,
    reported_version: str = DSH_VERSION,
    npm_exit: int = 0,
    plugin_exit: int = 0,
    dump_config_web: str = PROVIDER_DUMP_CONFIG_WEB,
    dump_config_pi_tui: str = PROVIDER_DUMP_CONFIG_PI_TUI,
    dump_config_exit: int = 0,
    existing_settings: str | None = None,
    existing_cordis_patch: str | None = None,
    patch_command_failure: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    if existing_settings is not None or existing_cordis_patch is not None:
        dsh_dir = home / ".dsh"
        dsh_dir.mkdir()
        if existing_settings is not None:
            (dsh_dir / "settings.yaml").write_text(existing_settings)
        if existing_cordis_patch is not None:
            (dsh_dir / "cordis.patch.yml").write_text(existing_cordis_patch)
    dump_web = tmp_path / "dump-config-web.yml"
    dump_web.write_text(dump_config_web)
    dump_pi_tui = tmp_path / "dump-config-pi-tui.yml"
    dump_pi_tui.write_text(dump_config_pi_tui)
    log = tmp_path / "calls.log"
    _write_executable(
        bin_dir / "npm",
        f"""#!/bin/bash
printf 'npm %s\\n' "$*" >> "$FAKE_LOG"
exit {npm_exit}
""",
    )
    _write_executable(
        bin_dir / "dsh",
        f"""#!/bin/bash
printf 'dsh %s\\n' "$*" >> "$FAKE_LOG"
if [ "${{1:-}}" = "--version" ]; then
    printf '%s\\n' '{reported_version}'
    exit 0
fi
profile=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--profile" ]; then
        profile="$arg"
    fi
    prev="$arg"
done
for arg in "$@"; do
    if [ "$arg" = "--dump-config" ]; then
        if [ "$profile" = "{DSH_TERMINAL_PROFILE}" ]; then
            cat "$FAKE_DUMP_CONFIG_PI_TUI"
        else
            cat "$FAKE_DUMP_CONFIG_WEB"
        fi
        exit {dump_config_exit}
    fi
done
exit {plugin_exit}
""",
    )
    if patch_command_failure is not None:
        assert patch_command_failure in {"mktemp", "cat", "mv"}
        _write_executable(
            bin_dir / patch_command_failure,
            f"""#!/bin/bash
if [[ "$*" == *cordis.patch.yml* ]]; then
    exit 1
fi
exec /usr/bin/{patch_command_failure} "$@"
""",
        )
    fragment = tmp_path / "fragment.bash"
    constants = [
        line
        for line in _install_text().splitlines()
        if re.match(
            r"readonly (DSH_VERSION|DSH_PI_TUI_VERSION|DSH_COMMANDCODE_PROVIDER_VERSION|"
            r"DSH_NATIVE_SCRIPT_ALLOWLIST|DSH_TERMINAL_PROFILE|DSH_MANAGED_BLOCK_BEGIN|"
            r"DSH_MANAGED_BLOCK_END)=",
            line,
        )
    ]
    assert len(constants) == 7, "expected the DSH pins and managed block markers"
    fragment.write_text(
        "\n".join(
            [
                *["set -u", *constants],
                _writable_dir_helper_text(),
                _terminal_assert_helper_text(),
                _ensure_helper_text(),
                _provider_assert_helper_text(),
                _dsh_function_text(),
                "install_deepseek_harness",
                "",
            ]
        )
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_DUMP_CONFIG_WEB": str(dump_web),
        "FAKE_DUMP_CONFIG_PI_TUI": str(dump_pi_tui),
    }
    result = subprocess.run(
        ["bash", str(fragment)], cwd=DEVCONTAINER_DIR.parent, env=env, capture_output=True, text=True
    )
    return result, home, log


def test_install_function_installs_the_pinned_versions_and_plugin(tmp_path: Path) -> None:
    result, home, log = _run_install_function(tmp_path)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert f"npm install -g --allow-scripts={DSH_NATIVE_SCRIPT_ALLOWLIST} @deepseek-ai/dsh@{DSH_VERSION}" in calls
    assert f"dsh plugin --profile web add @mars-sea/dsh-commandcode-provider@{COMMANDCODE_PROVIDER_VERSION}" in calls
    assert "dsh --profile web --dump-config" in calls
    assert not (home / ".dsh/settings.yaml").exists()
    assert (home / ".dsh/cordis.patch.yml").read_text() == DSH_CORDIS_PATCH.read_text()


def test_install_function_adds_the_pi_tui_plugins_and_checks_both_profiles(tmp_path: Path) -> None:
    result, _, log = _run_install_function(tmp_path)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert f"dsh plugin --profile {DSH_TERMINAL_PROFILE} -- add @xmoon76/dsh-pi-tui@{DSH_PI_TUI_VERSION}" in calls
    assert (
        f"dsh plugin --profile {DSH_TERMINAL_PROFILE} -- add "
        f"@mars-sea/dsh-commandcode-provider@{COMMANDCODE_PROVIDER_VERSION}"
    ) in calls
    assert "dsh --profile web --dump-config" in calls
    assert f"dsh --profile {DSH_TERMINAL_PROFILE} --dump-config" in calls


def test_install_function_fails_loudly_when_the_terminal_profile_is_missing_the_tui(tmp_path: Path) -> None:
    result, _, _ = _run_install_function(tmp_path, dump_config_pi_tui=DISABLED_DUMP_CONFIG)
    assert result.returncode != 0
    assert "@xmoon76/dsh-pi-tui" in result.stderr


def test_install_function_keeps_an_existing_settings_file(tmp_path: Path) -> None:
    sentinel = "llm-deepseek:\n  reasoningEffort: low\n"
    result, home, _ = _run_install_function(tmp_path, existing_settings=sentinel)
    assert result.returncode == 0, result.stderr
    assert (home / ".dsh/settings.yaml").read_text() == sentinel


def test_install_function_fails_loudly_when_the_package_inventory_stays_enabled(tmp_path: Path) -> None:
    # 既存の cordis.patch.yml に無効化が入っていない環境を模す。
    result, _, _ = _run_install_function(
        tmp_path,
        dump_config_web=ENABLED_DUMP_CONFIG,
        existing_cordis_patch="[]\n",
    )
    assert result.returncode != 0
    assert "plugin-package-inventory-deepseek" in result.stderr


def test_install_function_fails_loudly_when_dump_config_fails(tmp_path: Path) -> None:
    result, _, _ = _run_install_function(tmp_path, dump_config_exit=1)
    assert result.returncode != 0
    assert "dump-config" in result.stderr


def test_install_function_fails_loudly_on_version_mismatch(tmp_path: Path) -> None:
    result, _, _ = _run_install_function(tmp_path, reported_version="0.0.0")
    assert result.returncode != 0
    assert "0.0.0" in result.stderr


def test_install_function_fails_loudly_when_npm_fails(tmp_path: Path) -> None:
    result, _, _ = _run_install_function(tmp_path, npm_exit=1)
    assert result.returncode != 0
    assert "DeepSeek Harness" in result.stderr


def test_install_function_fails_loudly_when_the_plugin_add_fails(tmp_path: Path) -> None:
    result, _, _ = _run_install_function(tmp_path, plugin_exit=1)
    assert result.returncode != 0
    assert "Command Code" in result.stderr


@pytest.mark.parametrize("profile", ("web", "pi-tui"))
def test_install_function_accepts_a_custom_default_model(tmp_path: Path, profile: str) -> None:
    web = PROVIDER_DUMP_CONFIG_WEB
    terminal = PROVIDER_DUMP_CONFIG_PI_TUI
    if profile == "web":
        web = web.replace("provider: opencode-go", "provider: commandcode").replace(
            "model: deepseek-v4.1-flash", "model: deepseek/deepseek-v4.1-flash"
        )
    else:
        terminal = terminal.replace("provider: opencode-go", "provider: commandcode").replace(
            "model: deepseek-v4.1-flash", "model: deepseek/deepseek-v4.1-flash"
        )
    result, _, _ = _run_install_function(tmp_path, dump_config_web=web, dump_config_pi_tui=terminal)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("profile", ("web", "pi-tui"))
def test_install_function_accepts_a_custom_commandcode_api_base(tmp_path: Path, profile: str) -> None:
    web = PROVIDER_DUMP_CONFIG_WEB
    terminal = PROVIDER_DUMP_CONFIG_PI_TUI
    if profile == "web":
        web = web.replace("apiBase: https://api.commandcode.ai", "apiBase: https://proxy.example.invalid")
    else:
        terminal = terminal.replace("apiBase: https://api.commandcode.ai", "apiBase: https://proxy.example.invalid")
    result, _, _ = _run_install_function(tmp_path, dump_config_web=web, dump_config_pi_tui=terminal)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("profile", ("web", "pi-tui"))
def test_install_function_rejects_a_missing_default_model_entry(tmp_path: Path, profile: str) -> None:
    web = PROVIDER_DUMP_CONFIG_WEB
    terminal = PROVIDER_DUMP_CONFIG_PI_TUI
    if profile == "web":
        web = web.replace("- id: agent-default-model\n", "- id: unrelated-entry\n")
    else:
        terminal = terminal.replace("- id: agent-default-model\n", "- id: unrelated-entry\n")
    result, _, _ = _run_install_function(tmp_path, dump_config_web=web, dump_config_pi_tui=terminal)
    assert result.returncode != 0
    assert "agent-default-model" in result.stderr


def test_install_function_fails_loudly_when_the_opencode_go_route_is_missing(tmp_path: Path) -> None:
    result, _, _ = _run_install_function(tmp_path, dump_config_web=DISABLED_DUMP_CONFIG)
    assert result.returncode != 0
    assert "llm-pi-ai" in result.stderr


# --- README ------------------------------------------------------------------


def _readme_bash_block(section: str, needle: str) -> str:
    text = README.read_text().split(f"### {section}\n", 1)[1].split("\n### ", 1)[0]
    blocks = [
        block for block in re.findall(r"^```bash\n(.*?)\n```$", text, re.MULTILINE | re.DOTALL) if needle in block
    ]
    assert len(blocks) == 1, f"expected one {section!r} block containing {needle!r}"
    return blocks[0]


def _run_readme_migration(home: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    script = _readme_bash_block("旧 settings.yaml からの移行", "# BEGIN maatlog managed provider config")
    return subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=DEVCONTAINER_DIR.parent,
        env={**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )


def test_readme_migration_initializes_empty_patch_spellings(tmp_path: Path) -> None:
    cases = ("[]\n", "[ ]\n", "null\n", "~\n", "{}\n", "{ }\n", "[] # empty\n", "", " \n", None)
    for index, existing in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        bin_dir = case_dir / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "dsh", "#!/bin/bash\nprintf '%s\\n' '- id: llm-pi-ai'\n")
        home = case_dir / "home"
        target = home / ".dsh/cordis.patch.yml"
        target.parent.mkdir(parents=True)
        if existing is not None:
            target.write_text(existing)
        result = _run_readme_migration(home, bin_dir)
        assert result.returncode == 0, result.stderr
        text = target.read_text()
        assert text == DSH_CORDIS_PATCH.read_text(), f"invalid initialization for {existing!r}"
        backup = target.with_suffix(".yml.bak")
        assert backup.exists() == (existing is not None)
        if existing is not None:
            assert backup.read_text() == existing
        result = _run_readme_migration(home, bin_dir)
        assert result.returncode == 0, result.stderr
        assert target.read_text() == text
        assert backup.exists() == (existing is not None)
        if existing is not None:
            assert backup.read_text() == existing


@pytest.mark.parametrize("empty_root", ("[]", "[ ]", "null", "~", "{}", "{ }"))
@pytest.mark.parametrize("trailing_newline", ("", "\n"), ids=("no-final-newline", "final-newline"))
def test_readme_migration_preserves_comments_with_empty_patch_spellings(
    tmp_path: Path, empty_root: str, trailing_newline: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "dsh", "#!/bin/bash\nprintf '%s\\n' '- id: llm-pi-ai'\n")
    home = tmp_path / "home"
    target = home / ".dsh/cordis.patch.yml"
    target.parent.mkdir(parents=True)
    existing = f"# first note\n{empty_root}\n  # last note{trailing_newline}"
    target.write_text(existing)

    result = _run_readme_migration(home, bin_dir)
    assert result.returncode == 0, result.stderr
    text = target.read_text()
    assert text == "# first note\n  # last note\n" + DSH_CORDIS_PATCH.read_text()
    assert text.count("# BEGIN maatlog managed provider config") == 1
    backup = target.with_suffix(".yml.bak")
    assert backup.read_text() == existing
    assert sorted(path.name for path in target.parent.iterdir()) == ["cordis.patch.yml", "cordis.patch.yml.bak"]

    result = _run_readme_migration(home, bin_dir)
    assert result.returncode == 0, result.stderr
    assert target.read_text() == text
    assert backup.read_text() == existing


@pytest.mark.parametrize("patch_command_failure", ("mktemp", "cat", "mv"))
def test_readme_migration_keeps_original_patch_when_comment_initialization_fails(
    tmp_path: Path, patch_command_failure: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "dsh", "#!/bin/bash\nprintf '%s\\n' '- id: llm-pi-ai'\n")
    _write_executable(
        bin_dir / patch_command_failure,
        f"""#!/bin/bash
if [[ "$*" == *cordis.patch.yml* ]]; then
    exit 1
fi
exec /usr/bin/{patch_command_failure} "$@"
""",
    )
    home = tmp_path / "home"
    target = home / ".dsh/cordis.patch.yml"
    target.parent.mkdir(parents=True)
    existing = "# keep me\n[]\n"
    target.write_text(existing)

    result = _run_readme_migration(home, bin_dir)
    assert result.returncode != 0
    assert "Failed to" in result.stderr
    assert target.read_text() == existing
    assert target.with_suffix(".yml.bak").read_text() == existing
    assert sorted(path.name for path in target.parent.iterdir()) == ["cordis.patch.yml", "cordis.patch.yml.bak"]


def test_readme_migration_keeps_existing_content_on_repeated_application(tmp_path: Path) -> None:
    managed = "# BEGIN maatlog managed provider config\n- id: llm-commandcode\n# END maatlog managed provider config\n"
    for index, existing in enumerate(("- id: user-entry\n  config: {}\n", managed, "# keep me\n", "# keep me")):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        bin_dir = case_dir / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "dsh", "#!/bin/bash\nprintf '%s\\n' '- id: llm-pi-ai'\n")
        home = case_dir / "home"
        target = home / ".dsh/cordis.patch.yml"
        target.parent.mkdir(parents=True)
        target.write_text(existing)
        result = _run_readme_migration(home, bin_dir)
        assert result.returncode == 0, result.stderr
        text = target.read_text()
        assert text.startswith(existing)
        assert text.count("# BEGIN maatlog managed provider config") == 1
        if existing.startswith("# keep me"):
            assert text == existing + "\n" + DSH_CORDIS_PATCH.read_text()
        if existing == managed:
            assert text == existing
        backup = target.with_suffix(".yml.bak")
        assert backup.exists() == (existing != managed)
        result = _run_readme_migration(home, bin_dir)
        assert result.returncode == 0, result.stderr
        assert target.read_text() == text
        assert backup.exists() == (existing != managed)
        if existing != managed:
            assert backup.read_text() == existing


@dataclass(frozen=True)
class _BlockRun:
    status: int
    stdout: str
    stderr: str
    variables: dict[str, str]


def _run_readme_block(block: str, *, env: dict[str, str], variables: tuple[str, ...] = ()) -> _BlockRun:
    """README の block を、利用者がシェルへ貼り付けたときと同じく、そのまま続きの入力として実行する。

    block の後に同じシェルで後続の行を実行し、block がシェルを終了させたり errexit・nounset・
    pipefail を残したりしていないこと（貼り付けた端末を壊さないこと）を確かめる。
    block の終了コードと、block の後にシェルに残った変数の値を返す。
    """
    report = "\n".join(
        [
            "block_status=$?",
            "pipefail=off",
            "shopt -qo pipefail && pipefail=on",
            'printf \'\\n__after_block flags=%s pipefail=%s\\n\' "$-" "$pipefail"',
            *(f"printf '__var {name}=%s\\n' \"${{{name}-}}\"" for name in variables),
            'exit "$block_status"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", f"{block}\n{report}\n"],
        cwd=DEVCONTAINER_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    stdout, marker, after = result.stdout.partition("\n__after_block ")
    assert marker, f"the block ended the shell:\n{result.stderr}"
    flags_line, *variable_lines = after.splitlines()
    shell_state = re.fullmatch(r"flags=(\S*) pipefail=(on|off)", flags_line)
    assert shell_state is not None, flags_line
    flags, pipefail = shell_state.groups()
    assert "e" not in flags and "u" not in flags and pipefail == "off", f"the block changed the shell: {flags_line}"
    values: dict[str, str] = {}
    for line in variable_lines:
        name, _, value = line.removeprefix("__var ").partition("=")
        values[name] = value
    return _BlockRun(result.returncode, stdout, result.stderr, values)


def _write_failing_command(bin_dir: Path, name: str, condition: str) -> None:
    """bash の条件式 condition が真のときだけ失敗し、それ以外は本物の name を実行する fake を置く。"""
    real = shutil.which(name)
    assert real is not None, f"{name} is required"
    _write_executable(
        bin_dir / name,
        f"""#!/bin/bash
if {condition}; then
    exit 1
fi
exec {shlex.quote(real)} "$@"
""",
    )


# --- README: 更新前のバックアップと設定の復旧 ---------------------------------


def _backup_block() -> str:
    return _readme_bash_block("更新と復旧", ".dsh-backup-")


def _restore_block() -> str:
    return _readme_bash_block("更新と復旧", ".dsh-before-restore-")


def _make_dsh_tree(root: Path, marker: str) -> None:
    """認証情報・非 ASCII のファイル名・相対リンク・コメント付きの patch を持つ設定を作る。"""
    (root / "profiles/pi-tui").mkdir(parents=True)
    credential = root / "credentials.yaml"
    credential.write_text(f"token: {marker}\n")
    credential.chmod(0o600)
    (root / "認証 メモ.txt").write_text(f"{marker} の設定\n")
    (root / "profiles/pi-tui/cordis.patch.yml").write_text(f"# {marker} のコメント\n- id: user-entry\n")
    (root / "current-credentials").symlink_to("credentials.yaml")


def _tree_snapshot(root: Path) -> dict[str, tuple[str, str | None, int]]:
    snapshot: dict[str, tuple[str, str | None, int]] = {}
    for path in sorted(root.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[key] = ("link", os.readlink(path), 0)
        elif path.is_dir():
            snapshot[key] = ("dir", None, mode)
        else:
            snapshot[key] = ("file", path.read_text() if os.access(path, os.R_OK) else None, mode)
    return snapshot


def _home_env(home: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in {"DSH_BACKUP", "DSH_REPLACED"}}
    return {**env, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", **extra}


def _setup_home(tmp_path: Path) -> tuple[Path, Path]:
    # 空白と日本語を含む HOME で実行する。
    home = tmp_path / "利用者 home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return home, bin_dir


_NON_ROOT_ONLY = pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 files")


def test_readme_backup_and_restore_keep_the_settings(tmp_path: Path) -> None:
    home, bin_dir = _setup_home(tmp_path)
    _make_dsh_tree(home / ".dsh", "before")
    before = _tree_snapshot(home / ".dsh")

    backup = _run_readme_block(_backup_block(), env=_home_env(home, bin_dir), variables=("DSH_BACKUP",))
    assert backup.status == 0, backup.stderr
    backup_dir = Path(backup.variables["DSH_BACKUP"])
    assert f"DSH backup: {backup_dir}\n" in backup.stdout
    assert _tree_snapshot(backup_dir / "dsh") == before

    (home / ".dsh/credentials.yaml").write_text("token: after\n")
    (home / ".dsh/session.json").write_text("{}\n")
    after = _tree_snapshot(home / ".dsh")

    restore = _run_readme_block(
        _restore_block(), env=_home_env(home, bin_dir, DSH_BACKUP=str(backup_dir)), variables=("DSH_REPLACED",)
    )
    assert restore.status == 0, restore.stderr
    replaced_dir = Path(restore.variables["DSH_REPLACED"])
    assert f"Previous DSH settings: {replaced_dir}\n" in restore.stdout
    assert _tree_snapshot(home / ".dsh") == before
    assert _tree_snapshot(replaced_dir / "dsh") == after
    assert _tree_snapshot(backup_dir / "dsh") == before


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param("unreadable", marks=_NON_ROOT_ONLY),
        "missing",
        "mktemp",
        "chmod",
        "cp",
    ),
)
def test_readme_backup_fails_without_reporting_an_incomplete_copy(tmp_path: Path, failure: str) -> None:
    home, bin_dir = _setup_home(tmp_path)
    if failure != "missing":
        _make_dsh_tree(home / ".dsh", "before")
    if failure == "unreadable":
        unreadable = home / ".dsh/unreadable.txt"
        unreadable.write_text("secret\n")
        unreadable.chmod(0)
    if failure in {"mktemp", "chmod", "cp"}:
        _write_failing_command(bin_dir, failure, '[[ "$*" == *.dsh-backup-* ]]')
    before = _tree_snapshot(home)

    result = _run_readme_block(_backup_block(), env=_home_env(home, bin_dir), variables=("DSH_BACKUP",))
    assert result.status != 0
    assert "DSH backup:" not in result.stdout
    assert result.variables["DSH_BACKUP"] == ""
    # 途中までの複製（認証情報を含む）を残さず、元の設定は変えない。
    assert _tree_snapshot(home) == before


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param("unreadable", marks=_NON_ROOT_ONLY),
        "unset-backup",
        "missing-backup",
        "missing-current",
        "mktemp",
        "cp",
        "mv-current",
    ),
)
def test_readme_restore_keeps_current_settings_when_preparation_fails(tmp_path: Path, failure: str) -> None:
    home, bin_dir = _setup_home(tmp_path)
    backup_dir = tmp_path / "backup"
    _make_dsh_tree(backup_dir / "dsh", "backup")
    if failure != "missing-current":
        _make_dsh_tree(home / ".dsh", "current")
    if failure == "unreadable":
        unreadable = backup_dir / "dsh/unreadable.txt"
        unreadable.write_text("secret\n")
        unreadable.chmod(0)
    if failure == "missing-backup":
        shutil.rmtree(backup_dir / "dsh")
    if failure in {"mktemp", "cp"}:
        _write_failing_command(bin_dir, failure, '[[ "$*" == *.dsh-before-restore-* ]]')
    if failure == "mv-current":
        _write_failing_command(bin_dir, "mv", '[ "$1" = "$HOME/.dsh" ]')
    extra = {} if failure == "unset-backup" else {"DSH_BACKUP": str(backup_dir)}
    current = _tree_snapshot(home / ".dsh")
    backup = _tree_snapshot(backup_dir)

    result = _run_readme_block(_restore_block(), env=_home_env(home, bin_dir, **extra), variables=("DSH_REPLACED",))
    assert result.status != 0
    assert "Previous DSH settings:" not in result.stdout
    assert result.variables["DSH_REPLACED"] == ""
    assert _tree_snapshot(home / ".dsh") == current
    assert (home / ".dsh").exists() == (failure != "missing-current")
    assert _tree_snapshot(backup_dir) == backup
    # 途中までの複製（認証情報を含む）を HOME に残さない。
    assert sorted(path.name for path in home.iterdir()) == ([] if failure == "missing-current" else [".dsh"])


def test_readme_restore_reports_the_previous_settings_when_the_swap_fails(tmp_path: Path) -> None:
    home, bin_dir = _setup_home(tmp_path)
    backup_dir = tmp_path / "backup"
    _make_dsh_tree(backup_dir / "dsh", "backup")
    _make_dsh_tree(home / ".dsh", "current")
    current = _tree_snapshot(home / ".dsh")
    _write_failing_command(bin_dir, "mv", '[ "${!#}" = "$HOME/.dsh" ]')

    result = _run_readme_block(
        _restore_block(), env=_home_env(home, bin_dir, DSH_BACKUP=str(backup_dir)), variables=("DSH_REPLACED",)
    )
    assert result.status != 0
    assert "Previous DSH settings:" not in result.stdout
    assert result.variables["DSH_REPLACED"] == ""
    # 退避した現在の設定は 1 か所に残り、その場所をエラー出力で知らせる。
    moved = [path for path in home.glob(".dsh-before-restore-*/dsh") if _tree_snapshot(path) == current]
    assert len(moved) == 1
    assert str(moved[0]) in result.stderr


# --- README: 隔離環境の統合確認 -----------------------------------------------


COMPLETE_DUMPS = {"web": PROVIDER_DUMP_CONFIG_WEB, DSH_TERMINAL_PROFILE: PROVIDER_DUMP_CONFIG_PI_TUI}


@dataclass(frozen=True)
class _IsolatedCheck:
    run: _BlockRun
    user_home: Path
    calls: list[list[str]]


def _run_readme_isolated_check(
    tmp_path: Path, *, dumps: dict[str, str] | None = None, failing_call: str | None = None
) -> _IsolatedCheck:
    """隔離確認の block を、fake の npm / dsh と、/tmp の代わりの一時ディレクトリで実行する。

    block は `env -i` で環境変数を落として fake を起動するため、fake の設定は script に埋め込む。
    failing_call は失敗させる呼び出しの先頭（例: "dsh --profile web --dump-config"）で、
    fake は通常どおり出力してから終了コード 1 で終わる。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    user_home = tmp_path / "user home"
    user_home.mkdir()
    for profile, text in (COMPLETE_DUMPS | (dumps or {})).items():
        (fake_dir / f"dump-{profile}.yml").write_text(text)
    log = fake_dir / "calls.log"
    fail_check = f'[[ "$call" == {shlex.quote(failing_call)}* ]] && exit 1' if failing_call else ""
    record = (
        'printf \'%s\\t\' "$PWD" "$HOME" "${DSH_HOME-}" "${TMPDIR-}" "${npm_config_cache-}" '
        f">> {shlex.quote(str(log))}\nprintf '%s\\n' \"$call\" >> {shlex.quote(str(log))}"
    )
    _write_executable(bin_dir / "npm", f'#!/bin/bash\ncall="npm $*"\n{record}\n{fail_check}\nexit 0\n')
    _write_executable(
        bin_dir / "dsh",
        f"""#!/bin/bash
call="dsh $*"
{record}
if [ "${{1:-}}" = "--version" ]; then
    printf '%s\\n' {shlex.quote(DSH_VERSION)}
elif [ "${{1:-}}" = "plugin" ]; then
    mkdir -p "$DSH_HOME"
elif [ "${{1:-}}" = "--profile" ] && [ "${{3:-}}" = "--dump-config" ]; then
    cat {shlex.quote(str(fake_dir))}/dump-"$2".yml
fi
{fail_check}
exit 0
""",
    )
    # block の mktemp -d /tmp/... を tmp_path の下へ向ける。
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    _write_executable(
        bin_dir / "mktemp",
        f"""#!/bin/bash
args=("$@")
case "${{args[-1]}}" in
/tmp/*) args[-1]={shlex.quote(str(temp_root))}/"${{args[-1]#/tmp/}}" ;;
esac
exec {shlex.quote(real_mktemp)} "${{args[@]}}"
""",
    )
    env = {**os.environ, "HOME": str(user_home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("TMP_DSH", None)
    run = _run_readme_block(_readme_bash_block("検証", 'TMP_DSH="$(mktemp'), env=env, variables=("TMP_DSH",))
    calls = [line.split("\t") for line in log.read_text().splitlines()] if log.exists() else []
    if run.variables["TMP_DSH"]:
        assert Path(run.variables["TMP_DSH"]).parent == temp_root
    return _IsolatedCheck(run, user_home, calls)


def test_readme_isolated_check_accepts_complete_configs(tmp_path: Path) -> None:
    check = _run_readme_isolated_check(tmp_path)
    assert check.run.status == 0, check.run.stderr
    tmp_dsh = Path(check.run.variables["TMP_DSH"])
    # 確認後に一時ディレクトリを調べ、消せるよう、同じシェルにパスが残る。
    assert tmp_dsh.is_dir()
    # 利用者の HOME には触れず、npm / dsh は隔離した HOME・cwd・cache で動く。
    assert list(check.user_home.iterdir()) == []
    for cwd, home, dsh_home, tmpdir, cache, _ in check.calls:
        assert (cwd, home, dsh_home, tmpdir, cache) == (
            str(tmp_dsh / "cwd"),
            str(tmp_dsh / "home"),
            str(tmp_dsh / "home/.dsh"),
            str(tmp_dsh / "tmp"),
            str(tmp_dsh / "npm-cache"),
        )
    commands = [call[-1] for call in check.calls]
    assert any(
        command.startswith("npm install ") and f"--allow-scripts={DSH_NATIVE_SCRIPT_ALLOWLIST} " in command
        for command in commands
    )
    for profile in COMPLETE_DUMPS:
        assert f"dsh --profile {profile} --dump-config" in commands


@pytest.mark.parametrize(
    "failing_call",
    (
        "npm install ",
        "dsh --version",
        "dsh plugin --profile web ",
        f"dsh plugin --profile {DSH_TERMINAL_PROFILE} -- add @xmoon76/dsh-pi-tui@",
        f"dsh plugin --profile {DSH_TERMINAL_PROFILE} -- add @mars-sea/dsh-commandcode-provider@",
        "dsh --profile web --dump-config",
        f"dsh --profile {DSH_TERMINAL_PROFILE} --dump-config",
    ),
)
def test_readme_isolated_check_fails_when_a_command_fails(tmp_path: Path, failing_call: str) -> None:
    check = _run_readme_isolated_check(tmp_path, failing_call=failing_call)
    assert check.run.status != 0
    assert any(call[-1].startswith(failing_call) for call in check.calls)


def _run_readme_dump_check(tmp_path: Path, profile: str, dump_config: str) -> _BlockRun:
    """profile の解決後の構成だけを dump_config にし、他の profile は完全な構成で隔離確認を実行する。"""
    return _run_readme_isolated_check(tmp_path, dumps={profile: dump_config}).run


@pytest.mark.parametrize("profile", tuple(COMPLETE_DUMPS))
def test_readme_dump_check_accepts_complete_config(tmp_path: Path, profile: str) -> None:
    result = _run_readme_dump_check(tmp_path, profile, COMPLETE_DUMPS[profile])
    assert result.status == 0, result.stderr


@pytest.mark.parametrize("profile", tuple(COMPLETE_DUMPS))
def test_readme_dump_check_rejects_incomplete_config(tmp_path: Path, profile: str) -> None:
    needles = (
        "id: llm-pi-ai",
        "opencode-go",
        "id: llm-commandcode",
        "apiBase: https://api.commandcode.ai",
        "provider: opencode-go",
        "enabled: false",
    )
    cases = ["", "opencode-go\n"]
    cases.extend(
        "\n".join(line for line in COMPLETE_DUMPS[profile].splitlines() if needle not in line) + "\n"
        for needle in needles
    )
    for index, dump_config in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        result = _run_readme_dump_check(case_dir, profile, dump_config)
        assert result.status != 0, f"incomplete {profile} dump was accepted: {dump_config!r}"


@pytest.mark.parametrize("profile", tuple(COMPLETE_DUMPS))
@pytest.mark.parametrize(
    "inventory_entry",
    (
        "- id: plugin-package-inventory-deepseek\n  config:\n    enabled: true\n",
        "- id: plugin-package-inventory-deepseek\n  config: {}\n",
        "- id: plugin-package-inventory-deepseek\n",
        "",
    ),
    ids=("enabled", "no-enabled-setting", "no-config", "missing-entry"),
)
def test_readme_dump_check_rejects_inventory_disabled_only_in_another_entry(
    tmp_path: Path, inventory_entry: str, profile: str
) -> None:
    other_entries = re.sub(
        r"(?ms)^- id: plugin-package-inventory-deepseek\n.*?(?=^- |^# )", "", COMPLETE_DUMPS[profile], count=1
    )
    assert "plugin-package-inventory-deepseek" not in other_entries
    dump_config = inventory_entry + other_entries + "- id: some-other-plugin\n  config:\n    enabled: false\n"
    result = _run_readme_dump_check(tmp_path, profile, dump_config)
    assert result.status != 0
    assert "plugin-package-inventory-deepseek" in result.stderr
