"""#524 / #577: DSH launcher boundaries, installation, and boot-free patch composition.

The DSH stub replaces the interactive/network boundary only. Real dsh composition
is also exercised when dsh is installed, using an isolated, dependency-free profile.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

DEVCONTAINER = Path(__file__).resolve().parents[3] / ".devcontainer"
LAUNCHERS = ("dshccd", "dshgo")
ASSETS = (*LAUNCHERS, "dsh-launcher.bash", "dshccd.patch.yml", "dshgo.patch.yml")
KEYS = {"dshccd": "COMMANDCODE_API_KEY", "dshgo": "OPENCODE_API_KEY"}
PI_METADATA = (
    "PI_PROVIDER",
    "PI_MODEL",
    "PI_SESSION_ID",
    "PI_SESSION_FILE",
    "PI_SUBAGENT_SESSION",
    "PI_REASONING_LEVEL",
)
MODEL_SCRIPT = DEVCONTAINER.parent / ".agents/skills/pi-model-name/scripts/print-pi-model.sh"
# 実行中 provider / model をシェルへ渡すプラグイン（#577）。patch は配置時に
# プレースホルダを実際の配置先へ置換するため、資産そのものは byte 比較できない。
MODEL_ENV_PLUGIN = ".local/share/xrift-poc/dsh-model-env.mjs"
MODEL_ENV_PATCH = "dsh-model-env.patch.yml"
MODEL_ENV_PLACEHOLDER = "__PLUGIN_PATH__"


def model_env_patch_text(home: Path) -> str:
    template = (DEVCONTAINER / MODEL_ENV_PATCH).read_text()
    assert MODEL_ENV_PLACEHOLDER in template, "the launcher patch template must keep its placeholder"
    return template.replace(MODEL_ENV_PLACEHOLDER, str(home / MODEL_ENV_PLUGIN))


def _environment(home: Path, bin_dir: Path) -> dict[str, str]:
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    for key in (*KEYS.values(), "DSH_HOME"):
        env.pop(key, None)
    return env


def _install_launchers(home: Path) -> subprocess.CompletedProcess[str]:
    text = (DEVCONTAINER / "install_beforehand.bash").read_text()
    match = re.search(r"^install_dsh_launchers\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "install_dsh_launchers must place launchers and their runtime assets"
    return subprocess.run(
        ["bash", "-eu", "-c", match.group(0) + "\ninstall_dsh_launchers"],
        cwd=DEVCONTAINER.parent,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home with spaces"
    result = _install_launchers(home)
    assert result.returncode == 0, result.stderr
    bin_dir = home / ".local/bin"
    stub = bin_dir / "dsh"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "patches = [Path(args[index + 1]).read_text() for index, arg in enumerate(args) if arg == '--patch']\n"
        "print(json.dumps({'args': args, 'cwd': os.getcwd(), 'home': os.environ['HOME'],\n"
        "'patches': patches,\n"
        f"'pi_metadata_present': {{key: key in os.environ for key in {PI_METADATA!r}}},\n"
        "'custom_pi_setting_inherited': os.environ.get('PI_CUSTOM_SETTING') == 'keep',\n"
        "'commandcode_key_inherited': os.environ.get('COMMANDCODE_API_KEY') == 'test-key',\n"
        "'opencode_key_inherited': os.environ.get('OPENCODE_API_KEY') == 'test-key'}))\n"
        "sys.exit(int(os.environ.get('TEST_DSH_EXIT', '0')))\n"
    )
    stub.chmod(0o755)
    cwd = tmp_path / "unrelated working directory"
    cwd.mkdir()
    return home, bin_dir, cwd


def _run(
    installed: tuple[Path, Path, Path],
    launcher: str,
    args: tuple[str, ...] = (),
    *,
    key: bool = True,
    exit_code: int = 0,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home, bin_dir, cwd = installed
    env = _environment(home, bin_dir)
    if key:
        env[KEYS[launcher]] = "test-key"
    env["TEST_DSH_EXIT"] = str(exit_code)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([launcher, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=15)


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(result.stdout))


def _patches(result: subprocess.CompletedProcess[str]) -> list[str]:
    patches = _payload(result)["patches"]
    assert isinstance(patches, list)
    return cast(list[str], patches)


def _model_env_patch(result: subprocess.CompletedProcess[str]) -> str:
    return _patches(result)[0]


def _launcher_patch(result: subprocess.CompletedProcess[str]) -> str:
    return _patches(result)[1]


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_installed_launcher_preserves_arguments_environment_cwd_and_exit(
    installed: tuple[Path, Path, Path], launcher: str
) -> None:
    home, bin_dir, cwd = installed
    args = ("--session", "session with spaces", "--preset", "preset with spaces")
    result = _run(installed, launcher, args, exit_code=37)
    assert result.returncode == 37, result.stderr
    payload = _payload(result)
    assert payload["args"] == [
        "--profile",
        "pi-tui",
        "--patch",
        str(bin_dir / MODEL_ENV_PATCH),
        "--patch",
        str(bin_dir / f"{launcher}.patch.yml"),
        *args,
    ]
    assert _model_env_patch(result) == model_env_patch_text(home)
    assert str(home / MODEL_ENV_PLUGIN) in _model_env_patch(result)
    assert payload["cwd"] == str(cwd)
    assert payload["home"] == str(home)
    assert payload["commandcode_key_inherited"] == (launcher == "dshccd")
    assert payload["opencode_key_inherited"] == (launcher == "dshgo")
    assert "test-key" not in result.stdout + result.stderr


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("args", ((), ("--help",), ("--dump-default-config",)))
def test_launchers_drop_parent_pi_metadata_at_every_exit(
    installed: tuple[Path, Path, Path], launcher: str, args: tuple[str, ...]
) -> None:
    inherited = {name: "outer-pi-metadata" for name in PI_METADATA}
    result = _run(installed, launcher, args, key=not args, env_extra={**inherited, "PI_CUSTOM_SETTING": "keep"})
    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    present = cast(dict[str, bool], payload["pi_metadata_present"])
    assert not any(present.values()), present
    assert payload["custom_pi_setting_inherited"] is True


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("pi_state", ("none", "partial", "complete"))
@pytest.mark.parametrize("has_dsh_facts", (False, True))
def test_launcher_boundary_keeps_model_identity_scoped_to_dsh(
    installed: tuple[Path, Path, Path], launcher: str, pi_state: str, has_dsh_facts: bool
) -> None:
    _, bin_dir, _ = installed
    # Replace only the TUI/network boundary. The installed launcher and helper
    # remain real; the stub injects the request-scoped facts DSH would provide.
    facts = {"DSH_SHELL": "1"}
    if has_dsh_facts:
        facts.update({"DSH_PROVIDER": "calling-dsh-provider", "DSH_MODEL": 'calling-model/"日本語"'})
    (bin_dir / "dsh").write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "for key in tuple(os.environ):\n"
        "    if key.startswith('DSH_'):\n"
        "        del os.environ[key]\n"
        f"os.environ.update({facts!r})\n"
        f"os.execv({str(MODEL_SCRIPT)!r}, [{str(MODEL_SCRIPT)!r}, '--json'])\n"
    )
    inherited = {name: "outer-pi-metadata" for name in PI_METADATA}
    if pi_state != "complete":
        inherited["PI_MODEL"] = ""
    if pi_state == "none":
        inherited["PI_PROVIDER"] = ""
    result = _run(installed, launcher, env_extra=inherited)
    if has_dsh_facts:
        assert result.returncode == 0, result.stderr
        payload = _payload(result)
        assert payload["provenance"] == "dsh-runtime"
        assert (payload["provider"], payload["model"]) == ("calling-dsh-provider", 'calling-model/"日本語"')
    else:
        assert result.returncode == 2
        assert result.stdout == ""
        assert "DSH_PROVIDER/DSH_MODEL" in result.stderr


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("args", ((), ("--session", "existing-session")))
def test_interactive_start_requires_the_selected_key_without_fallback(
    installed: tuple[Path, Path, Path], launcher: str, args: tuple[str, ...]
) -> None:
    home, bin_dir, cwd = installed
    env = _environment(home, bin_dir)
    other_key = "OPENCODE_API_KEY" if launcher == "dshccd" else "COMMANDCODE_API_KEY"
    env[other_key] = "test-key"
    result = subprocess.run([launcher, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert KEYS[launcher] in result.stderr
    assert not result.stdout, "must reject before executing dsh even when the other provider has a key"


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize(
    "option", ("--help", "-h", "--version", "-V", "--dump-config", "--dump-config-schema", "--dump-default-config")
)
def test_offline_operations_need_no_key(installed: tuple[Path, Path, Path], launcher: str, option: str) -> None:
    result = _run(installed, launcher, (option,), key=False)
    assert result.returncode == 0, result.stderr
    args = _payload(result)["args"]
    assert isinstance(args, list)
    assert args[-1] == option
    assert ("--patch" in args) == (option != "--dump-default-config")


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("args", (("--preset", "--help"), ("--session", "--dump-config"), ("--", "--dump-config")))
def test_option_values_and_delimited_app_args_do_not_bypass_authentication(
    installed: tuple[Path, Path, Path], launcher: str, args: tuple[str, ...]
) -> None:
    result = _run(installed, launcher, args, key=False)
    assert result.returncode != 0
    assert KEYS[launcher] in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("patch", ("{launcher}.patch.yml", MODEL_ENV_PATCH))
def test_missing_patch_fails_before_dsh(installed: tuple[Path, Path, Path], launcher: str, patch: str) -> None:
    _, bin_dir, _ = installed
    (bin_dir / patch.format(launcher=launcher)).unlink()
    result = _run(installed, launcher)
    assert result.returncode != 0
    assert "patch" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_missing_dsh_fails_with_installation_guidance(installed: tuple[Path, Path, Path], launcher: str) -> None:
    home, _, cwd = installed
    commands = cwd / "commands"
    commands.mkdir()
    for name in ("bash", "readlink", "dirname"):
        command = shutil.which(name)
        assert command is not None
        (commands / name).symlink_to(command)
    env = _environment(home, commands)
    env["PATH"] = str(commands)
    env[KEYS[launcher]] = "test-key"
    result = subprocess.run(
        [str(home / ".local/bin" / launcher)], cwd=cwd, env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 127
    assert "dsh is not on PATH" in result.stderr
    assert "README.md" in result.stderr
    assert not result.stdout


def test_installation_is_repeatable_and_does_not_touch_dsh_or_pi_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sentinels = {
        ".dsh/cordis.patch.yml": "# custom configuration\n",
        ".dsh/profiles/pi-tui/cordis.patch.yml": "# custom profile\n",
        ".dsh/sessions/saved.json": '{"model":"saved"}\n',
        ".pi/agent/auth.json": '{"custom":"keep"}\n',
    }
    for name, content in sentinels.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for _ in range(2):
        result = _install_launchers(home)
        assert result.returncode == 0, result.stderr
        for asset in ASSETS:
            installed = home / ".local/bin" / asset
            assert installed.read_bytes() == (DEVCONTAINER / asset).read_bytes()
            assert bool(installed.stat().st_mode & 0o111) == (not asset.endswith(".yml"))
        # 生成物は 2 つだけ byte 比較できない。patch は配置先の絶対パスへ置換され、
        # plugin は ~/.local/share 側に置かれる。
        generated = home / ".local/bin" / MODEL_ENV_PATCH
        assert generated.read_text() == model_env_patch_text(home)
        assert bool(generated.stat().st_mode & 0o111) is False
        plugin = home / MODEL_ENV_PLUGIN
        assert plugin.read_bytes() == (DEVCONTAINER / "dsh-model-env.mjs").read_bytes()
        assert bool(plugin.stat().st_mode & 0o111) is False
        for name, content in sentinels.items():
            assert (home / name).read_text() == content


def test_concurrent_and_alternating_launches_use_immutable_separate_patches(
    installed: tuple[Path, Path, Path],
) -> None:
    home, bin_dir, _ = installed
    state = home / ".dsh/cordis.patch.yml"
    state.parent.mkdir()
    state.write_text("# existing selection remains unchanged\n")
    watched = (state, *(bin_dir / asset for asset in ASSETS), bin_dir / MODEL_ENV_PATCH, home / MODEL_ENV_PLUGIN)
    before = {path: path.read_bytes() for path in watched}

    def launch(name: str) -> subprocess.CompletedProcess[str]:
        return _run(installed, name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, ("dshccd", "dshgo", "dshgo", "dshccd")))
    for name, result in zip(("dshccd", "dshgo", "dshgo", "dshccd"), results, strict=True):
        assert result.returncode == 0, result.stderr
        assert _launcher_patch(result) == (bin_dir / f"{name}.patch.yml").read_text()
        assert _model_env_patch(result) == model_env_patch_text(home)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.skipif(shutil.which("dsh") is None, reason="dsh is not installed; no network installation in tests")
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_real_dsh_dump_composes_the_overlay_without_changing_saved_defaults(tmp_path: Path, launcher: str) -> None:
    home = tmp_path / "home"
    result = _install_launchers(home)
    assert result.returncode == 0, result.stderr
    dsh_home = home / ".dsh"
    profile = dsh_home / "profiles/pi-tui"
    profile.mkdir(parents=True)
    # No plugins or package installation are needed to exercise the actual loader.
    (profile / "package.json").write_text('{"name":"launcher-test","private":true,"dsh":{"profile":{"bundles":[]}}}\n')
    profile_patch = profile / "cordis.patch.yml"
    profile_patch.write_text(
        "- insert:\n"
        "    - id: agent-default-model\n"
        "      config: {provider: saved-provider, model: saved-model, reasoningEffort: low}\n"
        "    - id: llm-pi-ai\n"
        "    - id: llm-commandcode\n"
        "    - id: plugin-package-inventory-deepseek\n"
    )
    home_patch = dsh_home / "cordis.patch.yml"
    shutil.copyfile(DEVCONTAINER / "dsh-cordis.patch.yml", home_patch)
    before = {path: path.read_bytes() for path in (profile_patch, home_patch)}
    env = _environment(home, home / ".local/bin")
    env["DSH_HOME"] = str(dsh_home)
    result = subprocess.run(
        [launcher, "--dump-config"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r"(?m)^- id: agent-default-model\n(?:(?!^- ).*\n)*", result.stdout)
    assert match is not None
    # ランチャーの patch の config 行（既定モデル）が、保存済みの既定より優先される。
    overlay = [
        line for line in (DEVCONTAINER / f"{launcher}.patch.yml").read_text().splitlines() if line.startswith("    ")
    ]
    assert overlay
    for line in overlay:
        assert line in match.group(0).splitlines(), line
    # #577: モデル事実を渡すプラグインの行も、ランチャーの patch から合成される。
    plugin_row = re.search(r"(?m)^- id: xrift-poc-dsh-model-env\n(?:(?!^- ).*\n)*", result.stdout)
    assert plugin_row is not None
    assert (home / MODEL_ENV_PLUGIN).as_uri() in plugin_row.group(0)
    assert "apiKeyEnv: COMMANDCODE_API_KEY" in result.stdout
    assert "apiKeyEnv: OPENCODE_API_KEY" in result.stdout
    assert "apiBase: https://api.commandcode.ai" in result.stdout
    assert "baseURL:" not in result.stdout
    assert {path: path.read_bytes() for path in before} == before
