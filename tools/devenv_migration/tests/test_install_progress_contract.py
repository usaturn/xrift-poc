"""インストーラの進捗ログと fail-loud の仕様テスト。

Dev Container の rebuild 中に VS Code のウィンドウを閉じるとコンテナが停止し、
postCreate のプロセスはその場で kill される。次に同じコンテナを開いても
postCreate は再実行されないため、どのステップまで進んだのかが出力に残っていないと
復旧の手掛かりが無くなる（2026-09-13 の実インシデント）。

`.devcontainer/install_beforehand.bash` について次を固定する。

- 各ステップの開始が `[install_beforehand] [n/total] <label>` の1行で出る
- 失敗したステップは `ERROR` 行を残して exit 非ゼロで打ち切り、後続ステップを走らせない
- 外部コマンドを呼ぶステップは必ず失敗判定を持つ（失敗を成功扱いにする経路を残さない）

完走と打ち切りの両方は、`sudo` / `apt` / `npm` / `curl` / `pi` などを
fake に差し替えた一時 HOME で実際にスクリプトを走らせて検証する。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.test_support.xrift import SKILLS_NPX_FAKE

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


def _pinned_version(name: str) -> str:
    """スクリプトが `readonly <name>="..."` で固定した版（版の値はテストに写さない）。"""
    match = re.search(rf'(?m)^readonly {name}="([^"]+)"$', _install_text())
    assert match is not None, f"{name} must be declared"
    return match.group(1)


def _declared_step_labels() -> list[str]:
    """スクリプトの `step "..."` 呼び出しの順に並んだ手順名（手順名と総数はテストに写さない）。"""
    labels = re.findall(r'(?m)^step "([^"]+)"$', _install_text())
    assert labels, "install_beforehand.bash must announce its steps with step"
    return labels


def _launchers() -> list[Path]:
    """`.devcontainer/` にある拡張子の無い実行ファイル（pi のランチャー）。"""
    launchers = sorted(
        path for path in DEVCONTAINER_DIR.iterdir() if path.is_file() and not path.suffix and os.access(path, os.X_OK)
    )
    assert launchers, "no launcher found in .devcontainer"
    return launchers


# --- 実行の検証（fake ツール + 一時 HOME） ------------------------------------


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


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

NPX = "#!/bin/bash\n" + FAIL_HOOK + SKILLS_NPX_FAKE.removeprefix("#!/bin/bash\n")

ZSH = "#!/bin/bash\n" + FAIL_HOOK + "exit 0\n"

CAT = "#!/bin/bash\n" + FAIL_HOOK + 'exec /bin/cat "$@"\n'

PI = (
    "#!/bin/bash\n"
    + FAIL_HOOK
    + """\
printf 'pi %s\\n' "$*" >> "${FAKE_CALLS}"
if [ "${1:-}" = "list" ]; then
    printf '%s' "${FAKE_PI_LIST}"
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

XRIFT = """\
#!/bin/bash
printf 'xrift %s\\n' "$*" >> "${FAKE_CALLS}"
for arg in "$@"; do
    case "${arg}" in
    --version)
        printf '%s\\n' "${FAKE_XRIFT_VERSION}"
        exit 0
        ;;
    esac
done
exit 0
"""

CTX7 = """\
#!/bin/bash
printf 'ctx7 %s\\n' "$*" >> "${FAKE_CALLS}"
for arg in "$@"; do
    case "${arg}" in
    --version)
        printf '%s\\n' "${FAKE_CTX7_VERSION}"
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
PYTHON3 = "#!/bin/bash\n" + FAIL_HOOK + f'exec {shutil.which("python3")} "$@"\n'


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
    home: Path | None = None,
    pi_list: str | None = None,
    pi_agent_dir: str | None = None,
    ctx7_version: str | None = None,
    hide_ctx7: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if root is None:
        root = _make_root(tmp_path)
    if install_script is None:
        install_script = root / ".devcontainer" / "install_beforehand.bash"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    if home is None:
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
        ("xrift", XRIFT),
        ("cp", CP),
        ("python3", PYTHON3),
    ):
        _write_executable(bin_dir / name, body)
    base_path = os.environ["PATH"]
    if hide_ctx7:
        # 開発環境では実物の ctx7 が PATH の後ろにある。ctx7 のあるディレクトリごと外して「PATH に無い」を再現する。
        base_path = os.pathsep.join(
            entry for entry in base_path.split(os.pathsep) if not os.access(Path(entry) / "ctx7", os.X_OK)
        )
    else:
        _write_executable(bin_dir / "ctx7", CTX7)
    for name, tool in (
        ("claude.sh", "claude"),
        ("grok.sh", "grok"),
        ("cursor.sh", "cursor-agent"),
        ("devin.sh", "devin"),
        ("herdr.sh", "herdr"),
    ):
        (stub_dir / name).write_text(_stub(tool))

    env = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": pi_agent_dir if pi_agent_dir is not None else str(home / ".pi" / "agent"),
        # fake の `pi list` の出力。既定は pi-superagents だけが登録済みの状態。
        "FAKE_PI_LIST": pi_list if pi_list is not None else "npm:@teelicht/pi-superagents\n",
        # agent-runner の雛形の配置先を一時 HOME に固定する（呼び出し元の XDG_CONFIG_HOME へ書かない）。
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{bin_dir}:{base_path}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_STUB_DIR": str(stub_dir),
        "FAKE_XRIFT_VERSION": _pinned_version("XRIFT_CLI_VERSION"),
        # fake の `ctx7 --version` の出力。既定はスクリプトが固定した版。
        "FAKE_CTX7_VERSION": ctx7_version if ctx7_version is not None else _pinned_version("CTX7_CLI_VERSION"),
        # workspace cleanup は dry-run に固定し、テストから実ファイルを消させない。
        "XRIFT_POC_CLEANUP_TMP_ROOT": str(tmp_path / "cleanup"),
        "XRIFT_POC_CLEANUP_MIN_AGE_DAYS": "0",
        "XRIFT_POC_CLEANUP_DRY_RUN": "1",
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
    assert (root / "items").is_dir() and (root / "worlds").is_dir()


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
    "npm install -g @xrift/cli",
    "npm install -g ctx7@",
    "https://github.com/WebXR-JP/xrift-skills/tree/",
    "https://astral.sh/",
    "https://starship.rs/",
    "cp .devcontainer/starship.toml",
    "cp .devcontainer/herdr-git-status.bash",
    "scripts/write_shell_block.py",
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


def test_run_installs_the_pinned_ctx7_without_running_its_setup(tmp_path: Path) -> None:
    result, _ = _run_installer(tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert f"npm install -g ctx7@{_pinned_version('CTX7_CLI_VERSION')}" in calls
    # ctx7 setup は共有の ~/.claude・~/.claude.json・~/.codex などへ MCP 設定やスキルを書き込む。
    # セットアップは導入した版の確認にだけ ctx7 を呼ぶ。
    assert [line for line in calls if line.startswith("ctx7 ")] == ["ctx7 --version"]


@pytest.mark.parametrize(
    ("hide_ctx7", "ctx7_version"),
    [(True, None), (False, "0.0.0-unexpected")],
    ids=["not-on-path", "another-version"],
)
def test_run_stops_when_ctx7_is_missing_or_another_version(
    tmp_path: Path, hide_ctx7: bool, ctx7_version: str | None
) -> None:
    # npm install が成功しても、ctx7 が PATH に無い・固定した版でないときは、ERROR 行を残して打ち切る。
    result, _ = _run_installer(tmp_path, hide_ctx7=hide_ctx7, ctx7_version=ctx7_version, merge_output=True)
    assert result.returncode != 0, result.stdout
    lines = result.stdout.splitlines()
    error = next((index for index, line in enumerate(lines) if line.startswith("[install_beforehand] ERROR: ")), None)
    assert error is not None, result.stdout
    assert _announced_steps("\n".join(lines[error + 1 :])) == [], result.stdout
    step_start = max(index for index, line in enumerate(lines[:error]) if _announced_steps(line))
    # 打ち切った手順の出力が、原因として ctx7 を挙げている。
    assert "ctx7" in "\n".join(lines[step_start + 1 : error + 1]), result.stdout
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert f"npm install -g ctx7@{_pinned_version('CTX7_CLI_VERSION')}" in calls


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


def test_installer_rerun_keeps_work_and_a_single_shell_block(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    world = root / "worlds" / "my-world"
    world.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(world)], check=True)
    (world / "draft.txt").write_text("未コミットの作業\n", encoding="utf-8")
    zshrc = tmp_path / "home" / ".zshrc"
    zshrc.parent.mkdir(parents=True)
    zshrc.write_text("# 利用者の設定\n", encoding="utf-8")

    for attempt in ("first", "second"):
        run_dir = tmp_path / attempt
        run_dir.mkdir()
        result, _ = _run_installer(run_dir, root=root, home=tmp_path / "home")
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    assert (root / "items").is_dir()
    assert (world / "draft.txt").read_text(encoding="utf-8") == "未コミットの作業\n"
    text = zshrc.read_text(encoding="utf-8")
    assert text.startswith("# 利用者の設定\n")
    assert text.count("# >>> xrift-poc shell setup >>>") == 1


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_installer_stops_when_a_work_directory_is_not_a_directory(tmp_path: Path, kind: str) -> None:
    root = _make_root(tmp_path)
    if kind == "file":
        (root / "items").write_text("x\n", encoding="utf-8")
    else:
        (tmp_path / "elsewhere").mkdir()
        (root / "items").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    result, _ = _run_installer(tmp_path, root=root)
    assert result.returncode != 0
    assert "[install_beforehand] ERROR: " in result.stderr
    assert str(root / "items") in result.stderr


def test_retired_pi_extensions_are_reported_but_not_removed(tmp_path: Path) -> None:
    agent_dir = tmp_path / "home" / ".pi" / "agent"
    (agent_dir / "npm").mkdir(parents=True)
    manifest = agent_dir / "npm" / "package.json"
    manifest.write_text('{"dependencies": {"pi-distill": "^1.9.2"}}\n', encoding="utf-8")
    settings = agent_dir / "settings.json"
    settings.write_text('{"extensions":["-builtin:mcp"]}\n', encoding="utf-8")

    result, _ = _run_installer(tmp_path, pi_list="npm:pi-subagents\nnpm:@teelicht/pi-superagents\n")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi remove npm:pi-subagents" in result.stderr
    assert "pi remove npm:pi-distill" in result.stderr
    assert "-builtin:mcp" in result.stderr
    assert result.stderr.count("conflicts with @teelicht/pi-superagents") == 1
    # 共有の ~/.pi から消すと、ほかのコンテナで動いている pi からも拡張のファイルが消える。
    # 削除の案内には、先に止めることを必ず添える。
    removal_hints = [line for line in result.stderr.splitlines() if "pi remove npm:" in line]
    assert removal_hints
    assert all("stopping pi in the other containers" in line for line in removal_hints), removal_hints
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "pi remove" not in calls
    assert manifest.read_text(encoding="utf-8") == '{"dependencies": {"pi-distill": "^1.9.2"}}\n'
    assert settings.read_text(encoding="utf-8") == '{"extensions":["-builtin:mcp"]}\n'


def test_pigo_conflict_note_is_only_for_pi_subagents(tmp_path: Path) -> None:
    # pigo が起動できなくなるのは pi-subagents の衝突だけ。ほかの廃止した拡張や -builtin:mcp だけでは注記を出さない。
    agent_dir = tmp_path / "home" / ".pi" / "agent"
    (agent_dir / "npm").mkdir(parents=True)
    (agent_dir / "npm" / "package.json").write_text('{"dependencies": {"pi-distill": "^1.9.2"}}\n', encoding="utf-8")
    (agent_dir / "settings.json").write_text('{"extensions":["-builtin:mcp"]}\n', encoding="utf-8")

    result, _ = _run_installer(tmp_path)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi remove npm:pi-distill" in result.stderr
    assert "-builtin:mcp" in result.stderr
    assert "conflicts with @teelicht/pi-superagents" not in result.stderr


def test_retired_pi_check_expands_a_tilde_agent_dir(tmp_path: Path) -> None:
    # pi は PI_CODING_AGENT_DIR の先頭の ~ をホームへ展開する。確認も同じ場所を見る。
    npm_dir = tmp_path / "home" / ".pi" / "agent" / "npm"
    npm_dir.mkdir(parents=True)
    (npm_dir / "package-lock.json").write_text('{"packages": {"node_modules/pi-subagents": {}}}\n', encoding="utf-8")
    result, _ = _run_installer(tmp_path, pi_agent_dir="~/.pi/agent")
    assert result.returncode == 0, result.stderr
    assert "pi remove npm:pi-subagents" in result.stderr


def test_rerun_on_a_shared_pi_does_not_report_extensions_the_setup_installed(tmp_path: Path) -> None:
    # 共有の ~/.pi では、前回のセットアップが入れた拡張が次のセットアップの `pi list` に載る。
    # 自分で入れる拡張を廃止扱いにしていると、作り直すたびに再登録と警告を繰り返す（usaturn/xrift-poc#16）。
    root = _make_root(tmp_path)
    home = tmp_path / "home"
    first = tmp_path / "first"
    first.mkdir()
    result, _ = _run_installer(first, root=root, home=home)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    installed = re.findall(r"(?m)^pi install --approve (\S+)$", (first / "calls.log").read_text(encoding="utf-8"))
    assert installed

    second = tmp_path / "second"
    second.mkdir()
    result, _ = _run_installer(second, root=root, home=home, pi_list="".join(f"{name}\n" for name in installed))
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Retired pi extension" not in result.stderr
