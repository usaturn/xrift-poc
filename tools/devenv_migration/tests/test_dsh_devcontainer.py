"""Issue #282 / #516 の仕様テスト: Dev Container に dsh と対話用 pi-tui を追加する。

`.devcontainer/` の次の仕様を固定する。

- `initialize.bash` が、`devcontainer.json` の bind mount の source をホスト側に作る
- `.devcontainer/dsh-cordis.patch.yml` の雛形が、installer の事後確認（inventory の無効化と
  provider の entry）を満たす
- `install_beforehand.bash` が dsh / pi-tui / Command Code plugin を宣言した版で導入し、
  web / pi-tui の解決後の provider 設定と terminal bundle を検証する
- 既存 patch の利用者 entry とコメントを保持し、managed block は一度だけ追加する
- 旧 settings.yaml は警告だけを出して保持する
- README の移行手順と確認手順は、実際に実行して確かめる

`install_deepseek_harness()` は fake の npm / dsh を PATH に置いて挙動を検証する。
"""

from __future__ import annotations

import os
import re
import subprocess
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


def _run_readme_dump_check(tmp_path: Path, dump_config: str) -> subprocess.CompletedProcess[str]:
    block = _readme_bash_block("検証", 'TMP_DSH="$(mktemp')
    script = block.split('dsh --profile pi-tui --dump-config > "$TMP_DSH/dump.yml"\n', 1)[1]
    (tmp_path / "dump.yml").write_text(dump_config)
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "TMP_DSH": str(tmp_path)},
        capture_output=True,
        text=True,
    )


def test_readme_dump_check_accepts_complete_config(tmp_path: Path) -> None:
    result = _run_readme_dump_check(tmp_path, PROVIDER_DUMP_CONFIG_PI_TUI)
    assert result.returncode == 0, result.stderr


def test_readme_dump_check_rejects_incomplete_config(tmp_path: Path) -> None:
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
        "\n".join(line for line in PROVIDER_DUMP_CONFIG_PI_TUI.splitlines() if needle not in line) + "\n"
        for needle in needles
    )
    for index, dump_config in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        result = _run_readme_dump_check(case_dir, dump_config)
        assert result.returncode != 0, f"incomplete dump was accepted: {dump_config!r}"


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
    tmp_path: Path, inventory_entry: str
) -> None:
    terminal_entries = PROVIDER_DUMP_CONFIG_PI_TUI.split("# == @xmoon76/dsh-pi-tui\n", 1)[1]
    dump_config = inventory_entry + terminal_entries + "- id: some-other-plugin\n  config:\n    enabled: false\n"
    result = _run_readme_dump_check(tmp_path, dump_config)
    assert result.returncode != 0
    assert "plugin-package-inventory-deepseek" in result.stderr
