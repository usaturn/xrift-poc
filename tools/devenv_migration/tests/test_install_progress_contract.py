"""インストーラの進捗ログと fail-loud の仕様テスト。

Dev Container の rebuild 中に VS Code のウィンドウを閉じるとコンテナが停止し、
postCreate のプロセスはその場で kill される。次に同じコンテナを開いても
postCreate は再実行されないため、どのステップまで進んだのかが出力に残っていないと
復旧の手掛かりが無くなる（2026-09-13 の実インシデント）。

`.devcontainer/install_beforehand.bash` について次を固定する。

- 各ステップの開始が `[install_beforehand] [n/total] <label>` の1行で出る
- 失敗したステップは `ERROR` 行を残して exit 非ゼロで打ち切り、後続ステップを走らせない
- 外部コマンドを呼ぶステップは必ず失敗判定を持つ（失敗を成功扱いにする経路を残さない）

完走と打ち切りの両方は、`sudo` / `apt` / `npm` / `curl` / `pi` / `dsh` などを
fake に差し替えた一時 HOME で実際にスクリプトを走らせて検証する。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEVCONTAINER_DIR = Path(__file__).resolve().parents[3] / ".devcontainer"
REPO_ROOT = DEVCONTAINER_DIR.parent
INSTALL_SCRIPT = DEVCONTAINER_DIR / "install_beforehand.bash"


def _make_root(tmp_path: Path) -> Path:
    """インストーラを実行する、実 checkout の必要な部分だけを写した Git リポジトリ。

    実 checkout で実行すると、`items/`・`worlds/` の作成や XRift スキルの導入が
    作業中の checkout を書き換えるため、複製で実行する。
    """
    root = tmp_path / "root"
    root.mkdir()
    shutil.copytree(DEVCONTAINER_DIR, root / ".devcontainer", symlinks=True)
    shutil.copytree(REPO_ROOT / "scripts", root / "scripts", symlinks=True)
    shutil.copy2(REPO_ROOT / ".gitignore", root / ".gitignore")
    (root / "tools" / "devenv").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "tools" / "devenv" / "pyproject.toml", root / "tools" / "devenv" / "pyproject.toml")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _install_text() -> str:
    return INSTALL_SCRIPT.read_text()


def _declared_step_labels() -> list[str]:
    """スクリプトの `step "..."` 呼び出しの順に並んだ手順名（手順名と総数はテストに写さない）。"""
    labels = re.findall(r'(?m)^step "([^"]+)"$', _install_text())
    assert labels, "install_beforehand.bash must announce its steps with step"
    return labels


def _launchers() -> list[Path]:
    """`.devcontainer/` にある拡張子の無い実行ファイル（pi / DSH のランチャー）。"""
    launchers = sorted(
        path for path in DEVCONTAINER_DIR.iterdir() if path.is_file() and not path.suffix and os.access(path, os.X_OK)
    )
    assert launchers, "no launcher found in .devcontainer"
    return launchers


# --- 実行の検証（fake ツール + 一時 HOME） ------------------------------------


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


# inventory は無効のまま、provider 設定と terminal bundle は解決済みという
# happy-path の dump。install script の fail-loud 検証を通過させる。
DUMP_CONFIG_DISABLED = """\
- id: agent
  name: '@deepseek-ai/dsh-agent'
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

# FAKE_FAIL_ON に「コマンド名 引数」の一部を指定すると、一致した呼び出しだけを失敗させる。
FAIL_HOOK = """\
if [ -n "${FAKE_FAIL_ON:-}" ] && [[ "${0##*/} $*" == *"${FAKE_FAIL_ON}"* ]]; then
    printf 'injected failure: %s %s\\n' "${0##*/}" "$*" >&2
    exit 1
fi
"""

SUDO = (
    "#!/bin/bash\n"
    + FAIL_HOOK
    + """\
printf 'sudo %s\\n' "$*" >> "${FAKE_CALLS}"
if [ "${1:-}" = "chown" ] && [ -n "${FAKE_SUDO_FIX_CHOWN:-}" ]; then
    # 実権限の代わりに、chown 成功がもたらす「書き込み可能」状態を再現する。
    chmod 0755 "${3:-}"
    exit 0
fi
exec "$@"
"""
)

NOOP = """\
#!/bin/bash
exit 0
"""

NPM = (
    "#!/bin/bash\n"
    + FAIL_HOOK
    + """\
printf 'npm %s\\n' "$*" >> "${FAKE_CALLS}"
exit 0
"""
)

NPX = "#!/bin/bash\n" + FAIL_HOOK + "exit 0\n"

ZSH = "#!/bin/bash\n" + FAIL_HOOK + "exit 0\n"

CAT = "#!/bin/bash\n" + FAIL_HOOK + 'exec /bin/cat "$@"\n'

PI = (
    "#!/bin/bash\n"
    + FAIL_HOOK
    + """\
printf 'pi %s\\n' "$*" >> "${FAKE_CALLS}"
if [ "${1:-}" = "list" ]; then
    printf 'npm:@teelicht/pi-superagents\\n'
fi
exit 0
"""
)

# Playwright 導入は tools/devenv の locked 環境で行う仕様のため、uv の呼び出し cwd と引数を記録する。
UV = (
    "#!/bin/bash\n"
    + FAIL_HOOK
    + """\
printf 'uv cwd=%s args=%s\\n' "$PWD" "$*" >> "${FAKE_CALLS}"
exit 0
"""
)

DSH = """\
#!/bin/bash
printf 'dsh %s\\n' "$*" >> "${FAKE_CALLS}"
for arg in "$@"; do
    case "${arg}" in
    --version)
        printf '%s\\n' "${FAKE_DSH_VERSION}"
        exit 0
        ;;
    --dump-config)
        cat "${FAKE_DSH_DUMP_CONFIG}"
        exit 0
        ;;
    esac
done
exit 0
"""

# curl はインストーラ本体を stdout へ流す。実インストーラの代わりに、必要な
# 実行ファイルだけを作るスタブを流す。
CURL = (
    "#!/bin/bash\n"
    + FAIL_HOOK
    + """\
output=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
    -o)
        shift
        output="$1"
        ;;
    http*)
        url="$1"
        ;;
    esac
    shift
done
case "${url}" in
*claude.ai*) cat "${FAKE_STUB_DIR}/claude.sh" ;;
*x.ai*) cat "${FAKE_STUB_DIR}/grok.sh" ;;
*cursor.com*) cat "${FAKE_STUB_DIR}/cursor.sh" ;;
*devin.ai*) cat "${FAKE_STUB_DIR}/devin.sh" > "${output}" ;;
*herdr.dev*) cat "${FAKE_STUB_DIR}/herdr.sh" ;;
*)
    if [ -n "${output}" ]; then
        printf 'dummy archive\\n' > "${output}"
    fi
    ;;
esac
exit 0
"""
)

# gitleaks の tarball 展開だけは本物の tar を通せないので、中身の代わりに
# `gitleaks version` が成功するスクリプトを置く。
TAR = """\
#!/bin/bash
directory="."
while [ "$#" -gt 0 ]; do
    case "$1" in
    -C)
        shift
        directory="$1"
        ;;
    esac
    shift
done
printf '#!/bin/bash\\nprintf "0.0.0\\\\n"\\n' > "${directory}/gitleaks"
exit 0
"""

CP = "#!/bin/bash\n" + FAIL_HOOK + 'exec /bin/cp "$@"\n'


def _stub(tool: str) -> str:
    # インストーラが置く CLI にも失敗注入を付ける（例: `claude plugin install`）。
    return (
        'mkdir -p "${HOME}/.local/bin"\n'
        f"cat > \"${{HOME}}/.local/bin/{tool}\" <<'STUB'\n"
        "#!/bin/bash\n" + FAIL_HOOK + "exit 0\nSTUB\n"
        f'chmod +x "${{HOME}}/.local/bin/{tool}"\n'
    )


def _run_installer(
    tmp_path: Path,
    *,
    root: Path | None = None,
    install_script: Path | None = None,
    cwd: Path | None = None,
    fail_on: str | None = None,
    sudo_fix_chown: bool = False,
    merge_output: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if root is None:
        root = _make_root(tmp_path)
    if install_script is None:
        install_script = root / ".devcontainer" / "install_beforehand.bash"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    for name, body in (
        ("sudo", SUDO),
        ("apt", NOOP),
        ("perl", NOOP),
        ("locale-gen", NOOP),
        ("curl", CURL),
        ("tar", TAR),
        ("sha256sum", NOOP),
        ("npx", NPX),
        ("cat", CAT),
        ("uv", UV),
        ("zsh", ZSH),
        ("claude", NOOP),
        ("npm", NPM),
        ("pi", PI),
        ("dsh", DSH),
        ("cp", CP),
    ):
        _write_executable(bin_dir / name, body)
    for name, tool in (
        ("claude.sh", "claude"),
        ("grok.sh", "grok"),
        ("cursor.sh", "cursor-agent"),
        ("devin.sh", "devin"),
        ("herdr.sh", "herdr"),
    ):
        (stub_dir / name).write_text(_stub(tool))

    dump_config = tmp_path / "dump-config.yml"
    dump_config.write_text(DUMP_CONFIG_DISABLED)
    dsh_version = re.search(r'(?m)^readonly DSH_VERSION="([^"]+)"$', _install_text())
    assert dsh_version is not None, "DSH_VERSION must be declared"

    env = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
        # agent-runner の雛形の配置先を一時 HOME に固定する（呼び出し元の XDG_CONFIG_HOME へ書かない）。
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_STUB_DIR": str(stub_dir),
        "FAKE_DSH_VERSION": dsh_version.group(1),
        "FAKE_DSH_DUMP_CONFIG": str(dump_config),
        # workspace cleanup は dry-run に固定し、テストから実ファイルを消させない。
        "MAATLOG_CLEANUP_TMP_ROOT": str(tmp_path / "cleanup"),
        "MAATLOG_CLEANUP_MIN_AGE_DAYS": "0",
        "MAATLOG_CLEANUP_DRY_RUN": "1",
    }
    if fail_on is not None:
        env["FAKE_FAIL_ON"] = fail_on
    if sudo_fix_chown:
        env["FAKE_SUDO_FIX_CHOWN"] = "1"
    # merge_output では stderr を stdout へ合流させ、手順の開始行と失敗の出力の前後関係を残す。
    result = subprocess.run(
        ["bash", str(install_script)],
        cwd=str(cwd) if cwd is not None else str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
        text=True,
    )
    return result, root


def _announced_steps(stdout: str) -> list[tuple[int, int, str]]:
    pattern = r"(?m)^\[install_beforehand\] \[\s*(\d+)/(\d+)\] (.+)$"
    return [(int(index), int(total), label) for index, total, label in re.findall(pattern, stdout)]


def test_run_from_other_cwd_installs_playwright_from_the_devenv_project(tmp_path: Path) -> None:
    # 制作物も public/ も無いルートで完走し、Playwright は tools/devenv の locked 環境から入れる。
    root = _make_root(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result, _ = _run_installer(tmp_path, root=root, cwd=elsewhere)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not (root / "public").exists()
    calls = (tmp_path / "calls.log").read_text()
    assert (f"uv cwd={root} args=run --project tools/devenv --locked playwright install --with-deps chromium") in calls


def test_run_announces_every_step_and_completes(tmp_path: Path) -> None:
    # リビルド前の古いランチャーを置いておき、毎回上書きで配置されることも確かめる。
    local_bin = tmp_path / "home/.local/bin"
    local_bin.mkdir(parents=True)
    for launcher in _launchers():
        (local_bin / launcher.name).write_text("# stale launcher\n")

    result, root = _run_installer(tmp_path)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    labels = _declared_step_labels()
    assert _announced_steps(result.stdout) == [
        (index, len(labels), label) for index, label in enumerate(labels, start=1)
    ]
    assert "ERROR:" not in result.stderr
    # 拡張子の無い実行ファイルをランチャーとみなし、すべてが同じ内容で配置される（一覧は写さない）。
    for launcher in _launchers():
        placed = local_bin / launcher.name
        assert placed.read_bytes() == launcher.read_bytes(), launcher.name
        assert os.access(placed, os.X_OK), launcher.name
    # 掃除は checkout のスクリプトへの symlink から、指定した root に対して実行される。
    cleanup = tmp_path / "home/bin/cleanup-workspace.bash"
    assert cleanup.is_symlink()
    assert cleanup.resolve() == (root / ".devcontainer" / "cleanup-workspace.bash").resolve()
    assert f"(root={tmp_path / 'cleanup'})" in result.stdout


def test_run_isolates_pi_agent_dir_from_the_caller_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # install 中に agent dir を書き換える処理（pi-mcp-adapter 撤去で settings.json の
    # -builtin:mcp を取り除く掃除）は、テストが隔離した PI_CODING_AGENT_DIR 側へ
    # 向かい、呼び出し元が指していた同名ファイルは無改変でなければならない。
    caller_agent_dir = tmp_path / "caller-agent"
    caller_settings = caller_agent_dir / "settings.json"
    caller_agent_dir.mkdir(parents=True)
    original = '{"extensions":["-builtin:mcp"],"keep":"caller"}\n'
    caller_settings.write_text(original)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(caller_agent_dir))

    isolated_agent_dir = tmp_path / "home/.pi/agent"
    isolated_agent_dir.mkdir(parents=True)
    isolated_settings = isolated_agent_dir / "settings.json"
    isolated_settings.write_text('{"extensions":["-builtin:mcp"]}\n')

    result, _ = _run_installer(tmp_path)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert caller_settings.read_text() == original
    assert json.loads(isolated_settings.read_text()) == {"extensions": []}


# 外部コマンドの呼び出しごとに 1 か所ずつ失敗を注入する。値は fake が受け取る「コマンド名 引数」の一部。
FAIL_LOUD_INJECTIONS = (
    "sudo apt install",
    "sudo locale-gen",
    "https://claude.ai/",
    "claude plugin marketplace add",
    "claude plugin install",
    "npm install -g @openai/codex",
    "https://x.ai/",
    "https://cursor.com/",
    "npm install -g --ignore-scripts @earendil-works/pi-coding-agent",
    "pi install --approve git:",
    # 引数まで含めて照合し、--if-absent（利用者が編集した tier 設定を上書きしない）も確かめる。
    "zsh scripts/setup_pi_superagents.zsh --if-absent",
    "herdrdev/herdr",
    "vercel-labs/skills@find-skills",
    "https://astral.sh/",
    "https://starship.rs/",
    "cp .devcontainer/starship.toml",
    "cp .devcontainer/herdr-git-status.bash",
    "cat .devcontainer/zshrc.txt",
    "cp .devcontainer/herdr.toml",
    "playwright install",
)


@pytest.mark.parametrize("fail_on", FAIL_LOUD_INJECTIONS)
def test_run_stops_at_the_step_whose_command_fails(tmp_path: Path, fail_on: str) -> None:
    # 失敗を成功扱いにせず、ERROR 行を残して打ち切り、後続の手順を始めない。
    result, _ = _run_installer(tmp_path, fail_on=fail_on, merge_output=True)
    lines = result.stdout.splitlines()
    injected = next((index for index, line in enumerate(lines) if line.startswith("injected failure: ")), None)
    assert injected is not None, f"no call matched {fail_on!r}:\n{result.stdout}"
    assert result.returncode != 0, result.stdout
    after = "\n".join(lines[injected + 1 :])
    assert _announced_steps(after) == [], result.stdout
    assert "[install_beforehand] ERROR: " in after, result.stdout


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="test -w returns true regardless of mode under euid 0",
)
def test_run_stops_when_local_share_is_not_writable(tmp_path: Path) -> None:
    share = tmp_path / "home" / ".local" / "share"
    share.mkdir(parents=True)
    share.chmod(0o555)
    try:
        result, _ = _run_installer(tmp_path)
        assert result.returncode != 0
        announced = _announced_steps(result.stdout)
        assert [index for index, _, _ in announced] == [1]
        assert announced[0][2] == _declared_step_labels()[0]
        assert "ERROR:" in result.stderr
        assert str(share) in result.stderr
    finally:
        share.chmod(0o755)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="test -w returns true regardless of mode under euid 0",
)
def test_run_recovers_a_non_writable_local_share(tmp_path: Path) -> None:
    share = tmp_path / "home" / ".local" / "share"
    share.mkdir(parents=True)
    share.chmod(0o555)
    try:
        result, _ = _run_installer(tmp_path, sudo_fix_chown=True)
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        announced = _announced_steps(result.stdout)
        total = len(_declared_step_labels())
        assert [(index, step_total) for index, step_total, _ in announced] == [
            (index, total) for index in range(1, total + 1)
        ]
        calls = (tmp_path / "calls.log").read_text().splitlines()
        chown_calls = [line for line in calls if line.startswith("sudo chown")]
        assert chown_calls == [f"sudo chown {os.getuid()}:{os.getgid()} {share}"]
        assert not any("chown -R" in line or "chown --recursive" in line for line in calls)
    finally:
        share.chmod(0o755)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="test -w returns true regardless of mode under euid 0",
)
def test_run_recovers_non_writable_mount_targets(tmp_path: Path) -> None:
    # bind mount target 自体（~/.claude や ~/.config/gh 等）がホスト側で root 所有
    # として残っている場合も、step 1 が非再帰 chown で復旧して完走することを見る。
    home = tmp_path / "home"
    targets = [
        home / ".config" / "gh",
        home / ".claude",
    ]
    for target in targets:
        target.mkdir(parents=True)
        target.chmod(0o555)
    try:
        result, _ = _run_installer(tmp_path, sudo_fix_chown=True)
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        announced = _announced_steps(result.stdout)
        total = len(_declared_step_labels())
        assert [(index, step_total) for index, step_total, _ in announced] == [
            (index, total) for index in range(1, total + 1)
        ]
        calls = (tmp_path / "calls.log").read_text().splitlines()
        chown_calls = [line for line in calls if line.startswith("sudo chown")]
        # step 1 のリスト順（.config/gh が .claude より先）に chown される。
        assert chown_calls == [f"sudo chown {os.getuid()}:{os.getgid()} {target}" for target in targets]
        assert not any("chown -R" in line or "chown --recursive" in line for line in calls)
    finally:
        for target in targets:
            target.chmod(0o755)


# --- pi-distill 撤去（関数単体 + fake pi） -------------------------------------

# settings に未登録（一覧は空）の状態を再現する。remove は実物と同じく、管理マニフェスト
# から記載を取り除いたうえで「No matching package found」相当の exit 1 を返す。
DISTILL_PI = """\
#!/bin/bash
printf 'pi %s\\n' "$*" >> "${FAKE_CALLS}"
if [ "${1:-}" = "remove" ]; then
    for manifest in "${FAKE_NPM_DIR}/package.json" "${FAKE_NPM_DIR}/package-lock.json"; do
        [ -f "${manifest}" ] || continue
        grep -v -- "${2#npm:}" "${manifest}" > "${manifest}.tmp" || true
        mv "${manifest}.tmp" "${manifest}"
    done
    exit 1
fi
exit 0
"""


def test_remove_pi_distill_expands_tilde_agent_dir(tmp_path: Path) -> None:
    # pi は PI_CODING_AGENT_DIR の先頭の ~ をホームへ展開する。installer が展開せずに
    # マニフェストを探すと、マニフェストにだけ残った pi-distill を見落として
    # `pi update --extensions` での復活を許す。関数だけを取り出して検証する。
    # test-pi-superagents.bash の awk と同じく、定義行から最初の行頭 } までを取り出す。
    definition = re.search(r"(?ms)^remove_pi_distill\(\) \{$.*?^\}", _install_text())
    assert definition is not None, "remove_pi_distill() must be defined in install_beforehand.bash"
    fragment = tmp_path / "remove-pi-distill.bash"
    fragment.write_text(f"{definition.group(0)}\nremove_pi_distill\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "pi", DISTILL_PI)

    home = tmp_path / "home"
    npm_dir = home / ".pi" / "agent" / "npm"
    npm_dir.mkdir(parents=True)
    package_json = npm_dir / "package.json"
    package_lock = npm_dir / "package-lock.json"
    package_json.write_text('{\n  "dependencies": {\n    "pi-distill": "^1.9.2"\n  }\n}\n')
    package_lock.write_text('{\n  "packages": {\n    "node_modules/pi-distill": {}\n  }\n}\n')
    calls = tmp_path / "calls.log"

    # cwd にはリテラルの ~ ディレクトリが無いので、展開しなければマニフェストは見つからない。
    result = subprocess.run(
        ["bash", str(fragment)],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(home),
            "PI_CODING_AGENT_DIR": "~/.pi/agent",
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_CALLS": str(calls),
            "FAKE_NPM_DIR": str(npm_dir),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi remove npm:pi-distill" in calls.read_text().splitlines()
    assert "pi-distill" not in package_json.read_text()
    assert "pi-distill" not in package_lock.read_text()
