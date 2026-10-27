"""導入スクリプト（`.devcontainer/install_beforehand.bash`）が、黙って損害を残さない仕様のテスト。

導入は `set -e` を付けず、外部コマンドを 1 つずつ `if ! ...; then fail` で包む。包みが外れると、
失敗しても postCreate が成功として終わる。ここでは、欠けたまま黙って進むと損害が残る手順
（XRift スキルの導入）と、導入がホストのデータ・制作物の作業・利用者の ~/.zshrc と models.json を
壊さないことを確かめる。

`sudo` / `apt` / `npm` / `curl` / `pi` などを fake に差し替えた一時 HOME で、実際にスクリプトを走らせる。
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
if [ "${1:-}" = "chown" ]; then
    # 実権限の代わりに、所有者が変わるパス（再帰なら配下も）を FAKE_CHOWNED に記録し、
    # FAKE_SUDO_FIX_CHOWN なら chown 成功がもたらす「書き込み可能」状態を再現する。
    shift
    recursive=0
    owner=""
    targets=()
    for arg in "$@"; do
        case "${arg}" in
        --recursive) recursive=1 ;;
        --*) ;;
        -*) [[ "${arg}" == *R* ]] && recursive=1 ;;
        *)
            if [ -z "${owner}" ]; then
                owner="${arg}"
            else
                targets+=("${arg}")
            fi
            ;;
        esac
    done
    for target in "${targets[@]}"; do
        if [ -n "${FAKE_CHOWNED:-}" ]; then
            realpath -- "${target}" >> "${FAKE_CHOWNED}"
            if [ "${recursive}" = 1 ]; then
                find "${target}" -mindepth 1 -exec realpath -- {} + >> "${FAKE_CHOWNED}"
            fi
        fi
        if [ -n "${FAKE_SUDO_FIX_CHOWN:-}" ]; then
            chmod 0755 "${target}"
        fi
    done
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

# uv（Playwright のブラウザの導入に使う）。呼び出しの cwd と引数を記録する。
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
    fail_on: str | None = None,
    sudo_fix_chown: bool = False,
    home: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if root is None:
        root = _make_root(tmp_path)
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
        ("ctx7", CTX7),
    ):
        _write_executable(bin_dir / name, body)
    for name, tool in (
        ("claude.sh", "claude"),
        ("cursor.sh", "cursor-agent"),
        ("devin.sh", "devin"),
        ("herdr.sh", "herdr"),
    ):
        (stub_dir / name).write_text(_stub(tool))

    env = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
        # fake の `pi list` の出力。pi-superagents だけが登録済みの状態。
        "FAKE_PI_LIST": "npm:@teelicht/pi-superagents\n",
        # agent-runner の雛形の配置先を一時 HOME に固定する（呼び出し元の XDG_CONFIG_HOME へ書かない）。
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_CHOWNED": str(tmp_path / "chowned.log"),
        "FAKE_STUB_DIR": str(stub_dir),
        "FAKE_XRIFT_VERSION": _pinned_version("XRIFT_CLI_VERSION"),
        # fake の `ctx7 --version` の出力。スクリプトが固定した版。
        "FAKE_CTX7_VERSION": _pinned_version("CTX7_CLI_VERSION"),
        # workspace cleanup は dry-run に固定し、テストから実ファイルを消させない。
        "XRIFT_POC_CLEANUP_TMP_ROOT": str(tmp_path / "cleanup"),
        "XRIFT_POC_CLEANUP_MIN_AGE_DAYS": "0",
        "XRIFT_POC_CLEANUP_DRY_RUN": "1",
    }
    if fail_on is not None:
        env["FAKE_FAIL_ON"] = fail_on
    if sudo_fix_chown:
        env["FAKE_SUDO_FIX_CHOWN"] = "1"
    result = subprocess.run(
        ["bash", str(root / ".devcontainer" / "install_beforehand.bash")],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return result, root


def test_run_stops_when_the_xrift_skills_install_fails(tmp_path: Path) -> None:
    """XRift スキルの導入の失敗を握りつぶすと、固定していない版や欠けたスキルのまま postCreate が成功で終わる。"""
    result, _ = _run_installer(tmp_path, fail_on="https://github.com/WebXR-JP/xrift-skills/tree/")
    # 注入が実際に起きたこと（fake の出力）。起きなければ、別の理由で止まっても通ってしまう。
    assert "injected failure: " in result.stderr, result.stderr
    assert result.returncode != 0, result.stdout


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="test -w returns true regardless of mode under euid 0",
)
def test_run_recovers_a_non_writable_local_share(tmp_path: Path) -> None:
    """chown が再帰する・別の場所に及ぶと、~/.local/share の下に bind mount したホストのデータの所有者を
    黙って変える。"""
    share = tmp_path / "home" / ".local" / "share"
    # bind mount したホストのデータの代わり
    host_data = share / "host-mount" / "data.json"
    host_data.parent.mkdir(parents=True)
    host_data.write_text("{}\n")
    share.chmod(0o555)
    try:
        result, _ = _run_installer(tmp_path, sudo_fix_chown=True)
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        # 所有者が変わるのは書き込めなかったディレクトリだけで、配下のホストのデータを含まない
        chowned = set((tmp_path / "chowned.log").read_text().splitlines())
        assert chowned == {str(share.resolve())}
    finally:
        share.chmod(0o755)


def test_installer_rerun_keeps_product_work_and_user_zshrc(tmp_path: Path) -> None:
    """items/・worlds/ を作り直す・~/.zshrc や共有の ~/.pi の models.json を書き直す変更で、
    制作物の未コミットの作業や利用者の設定を黙って失う。"""
    root = _make_root(tmp_path)
    world = root / "worlds" / "my-world"
    world.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(world)], check=True)
    (world / "draft.txt").write_text("未コミットの作業\n", encoding="utf-8")
    zshrc = tmp_path / "home" / ".zshrc"
    zshrc.parent.mkdir(parents=True)
    zshrc.write_text("# 利用者の設定\n", encoding="utf-8")
    # 導入は無いときだけ雛形を置く。bind mount した ~/.pi の、利用者が足した provider の定義
    models = tmp_path / "home" / ".pi" / "agent" / "models.json"
    models.parent.mkdir(parents=True)
    user_models = '{"providers": {"利用者の provider": {}}}\n'
    models.write_text(user_models, encoding="utf-8")

    # 導入はコンテナを作るたびに実 checkout で走る。2 回目はリビルドに当たる。
    for attempt in ("first", "second"):
        run_dir = tmp_path / attempt
        run_dir.mkdir()
        result, _ = _run_installer(run_dir, root=root, home=tmp_path / "home")
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    assert (world / "draft.txt").read_text(encoding="utf-8") == "未コミットの作業\n"
    assert zshrc.read_text(encoding="utf-8").startswith("# 利用者の設定\n")
    assert models.read_text(encoding="utf-8") == user_models
