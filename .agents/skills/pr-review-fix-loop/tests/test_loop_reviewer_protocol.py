"""レビュアーが受け取る依頼（runner の作業範囲の指示 + loop の依頼文）と、実際の pr-review helper をつないで動かす。

レビュアーは、指示を字義どおりに読む偽の Pi CLI（runner が起動するサブプロセス）。``writes=False`` のもとで
helper の置き場所（``tool_root``）への例外が指示に無ければ、helper の ``fail`` で終える（Issue #644 で観測した失敗）。
例外があれば、依頼文の値で実際の helper の ``prepare`` → draft の記入 → ``complete`` を呼ぶ。
PR は bare remote と偽の ``gh`` 実行ファイルで、GitHub には触れない。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")

REPO_ROOT = Path(__file__).resolve().parents[4]

# 偽の gh。``api -i repos/<repo>/pulls/<N>`` にだけ、bare remote の PR branch の head で答える。
FAKE_GH = r"""
import json, os, subprocess, sys
state = json.loads(open(os.environ["FAKE_GH_STATE"], encoding="utf-8").read())
with open(state["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
endpoint = f"repos/{state['repository']}/pulls/{state['number']}"
if sys.argv[1:] != ["api", "-i", endpoint]:
    print(f"fake gh does not support {sys.argv[1:]}", file=sys.stderr)
    sys.exit(1)
head = subprocess.run(
    ["git", "-C", state["remote"], "rev-parse", f"refs/heads/{state['branch']}"],
    check=True, capture_output=True, text=True,
).stdout.strip()
body = {
    "number": state["number"],
    "html_url": f"https://github.com/{state['repository']}/pull/{state['number']}",
    "state": "open",
    "merged": False,
    "head": {"sha": head, "ref": state["branch"], "repo": {"full_name": state["repository"]}},
    "base": {"ref": "main", "repo": {"full_name": state["repository"], "clone_url": state["clone_url"]}},
}
print("HTTP/2 200 OK\n\n" + json.dumps(body))
"""

# 偽の Pi CLI。stdin の Request を読み、最初の空行までを runner の作業範囲の指示として字義どおりに解釈する。
LITERAL_REVIEWER = r"""
import json, os, pathlib, re, subprocess, sys
request = json.load(sys.stdin)
receipt_path, scenario = pathlib.Path(sys.argv[1]), json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
copies = pathlib.Path(scenario["copies"])
(copies / "request.json").write_text(json.dumps(request), encoding="utf-8")
prompt = request["prompt"]
scope = prompt.split("\n\n", 1)[0].splitlines()
parameters = json.loads(re.search(r"```json\n(.*?)\n```", prompt, re.DOTALL).group(1))
tool_root, helper = parameters["tool_root"], parameters["helper"]
granted = [line[2:].split(": ", 1)[0] for line in scope if line.startswith("- /")]
read_only = any(line.startswith("writes=False") for line in scope)
request_file = str(pathlib.Path.cwd().parent / "request.json")
env = {
    "PATH": scenario["fake_bin"] + os.pathsep + os.defpath,
    "HOME": scenario["home"],
    "TMPDIR": scenario["session_tmp"],
    "PYTHONDONTWRITEBYTECODE": "1",
    "PI_PROVIDER": "fake-provider",
    "PI_MODEL": scenario["model"],
    "FAKE_GH_STATE": scenario["gh_state"],
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": f"url.{scenario['remote_uri']}.insteadOf",
    "GIT_CONFIG_VALUE_0": scenario["clone_url"],
}

def helper_call(*args):
    # 指定された source（tool_root）の helper だけを使う。起動した cwd（runner の worktree）の版は使わない
    assert pathlib.Path(helper).is_relative_to(tool_root) and not pathlib.Path.cwd().is_relative_to(tool_root)
    result = subprocess.run([sys.executable, helper, *args], env=env, capture_output=True, text=True)
    with (copies / "helper.log").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"args": list(args), "exit": result.returncode, "stderr": result.stderr}) + "\n")
    return json.loads(result.stdout)

if read_only and tool_root not in granted:
    payload = helper_call("fail", "--request", request_file, "--reason", "writes=False forbids the helper's writes")
else:
    payload = helper_call(
        "prepare", "--pr", parameters["pr_url"], "--runtime", parameters["runtimes"][request["agent_id"]],
        "--request", request_file, "--records-comments", parameters["records_comments"],
    )
    if payload.get("status") == "prepared":
        head = payload["reviewed_head"][:12]
        pathlib.Path(payload["draft"]).write_text(
            f"- 対象: PR #{scenario['number']} / head {head} / 2026-10-16\n"
            f"- レビュアー: {scenario['model']}（ランタイム: pi）\n\n"
            "## Review history\n\n| Reviewed at (UTC) | HEAD | Mode |\n|---|---|---|\n"
            f"| 2026-10-16T00:00:00Z | `{head}` | Initial |\n\n## Summary\n\n全体差分を確認した。\n",
            encoding="utf-8",
        )
        findings = pathlib.Path(payload["findings_draft"])
        document = json.loads(findings.read_text(encoding="utf-8"))
        document["coverage"]["read"] = ["PR 全体の差分と change.txt の全文"]
        findings.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        payload = helper_call("complete", "--state", payload["state_file"])
status = subprocess.run(["git", "status", "--porcelain", "--ignored"], capture_output=True, text=True, check=True)
(copies / "status.txt").write_text(status.stdout, encoding="utf-8")
receipt = {key: request[key] for key in ("schema_version", "run_id", "request_id", "agent_id", "role")}
receipt.update(status="success", payload=payload)
receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
"""


class LiteralReviewerAdapter:
    """tools.agent_runner の Adapter。LITERAL_REVIEWER を起動する。"""

    def __init__(self, scenario: Path) -> None:
        from tools.agent_runner.adapters import Capabilities

        self.capabilities = Capabilities()
        self.scenario = scenario

    def __call__(self, runtime: str) -> LiteralReviewerAdapter:
        return self

    def prepare(self, context: Any) -> Any:
        from tools.agent_runner.adapters import LaunchSpec

        return LaunchSpec(
            argv=(sys.executable, "-c", LITERAL_REVIEWER, str(context.receipt_path), str(self.scenario)),
            stdin=context.request.model_dump_json().encode(),
        )

    def collect(self, context: Any, stdout_path: Path, stderr_path: Path) -> Any:
        from tools.agent_runner.adapters import AdapterOutcome
        from tools.agent_runner.models import Identity, Receipt

        return AdapterOutcome(
            Receipt.model_validate_json(context.receipt_path.read_bytes()),
            Identity(model=context.agent.model, provider="fake-provider", session_id="fake-session"),
        )


@dataclass
class Protocol:
    adapter: LiteralReviewerAdapter
    copies: Path
    session_tmp: Path
    auxiliary: tuple[Path, ...]


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("tests", "evals", "__pycache__"))


@pytest.fixture
def protocol(env: Any, kit: Any, tmp_path: Path) -> Protocol:
    """tool_root（env.parent）に実際の helper とその依存を置き、偽の gh・レビュアーの設定を作る。"""
    parent = env.parent
    _copy_tree(REPO_ROOT / ".agents/skills/pr-review", parent / ".agents/skills/pr-review")
    (parent / ".agents/scripts").mkdir(parents=True)
    for script in (REPO_ROOT / ".agents/scripts").glob("*.py"):
        shutil.copyfile(script, parent / ".agents/scripts" / script.name)
    (parent / "tools").mkdir()
    shutil.copyfile(REPO_ROOT / "tools/__init__.py", parent / "tools/__init__.py")
    _copy_tree(REPO_ROOT / "tools/agent_runner", parent / "tools/agent_runner")
    # 実際の親と同じく、helper の bytecode・スクラッチ・仮想環境は Git に無視される
    with (parent / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("__pycache__/\ntmp\n.venv/\n")
    kit.git(parent, "add", "-A")
    kit.git(parent, "commit", "-qm", "pr-review helper")
    kit.git(env.remote, "update-ref", f"refs/pull/{kit.number}/head", env.gh.head())

    fake_bin, copies, session_tmp, home = (tmp_path / name for name in ("fake-bin", "copies", "session-tmp", "home"))
    for directory in (fake_bin, copies, session_tmp, home):
        directory.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
    gh.chmod(0o755)
    clone_url = f"https://github.com/{kit.repository}.git"
    gh_state = tmp_path / "gh-state.json"
    gh_state.write_text(
        json.dumps(
            {
                "log": str(copies / "gh.log"),
                "repository": kit.repository,
                "number": kit.number,
                "remote": str(env.remote),
                "branch": kit.branch,
                "clone_url": clone_url,
            }
        ),
        encoding="utf-8",
    )
    scenario = tmp_path / "literal-reviewer.json"
    scenario.write_text(
        json.dumps(
            {
                "copies": str(copies),
                "fake_bin": str(fake_bin),
                "home": str(home),
                "session_tmp": str(session_tmp),
                "gh_state": str(gh_state),
                "remote_uri": env.remote.as_uri(),
                "clone_url": clone_url,
                "model": "alpha-model",
                "number": kit.number,
            }
        ),
        encoding="utf-8",
    )
    return Protocol(LiteralReviewerAdapter(scenario), copies, session_tmp, (fake_bin, home))


def _tree_state(root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """root の下の全ファイル（symlink を含む。たどらない）の lstat の要約。"""
    state: dict[Path, tuple[int, int, int, int]] = {}
    for directory, _subdirectories, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            state[path] = (info.st_size, info.st_mtime_ns, info.st_ino, info.st_mode)
    return state


def _declared(scope: list[str], tool_root: str) -> list[str]:
    """作業範囲の指示の例外のうち、先頭がパスのもの（読むだけの tool_root を除く）。

    ``*`` を含むパスは ``*`` より前を名前の接頭辞とし、それ以外はそのディレクトリ自身と配下を表す。
    """
    paths = [line[2:].split(": ", 1)[0] for line in scope if line.startswith("- /")]
    return [path.split("*", 1)[0] if "*" in path else path + "/" for path in paths if path != tool_root]


def _covered(path: Path, prefixes: list[str]) -> bool:
    return any(f"{path}/".startswith(prefix) for prefix in prefixes)


def test_literal_reviewer_completes_the_real_helper_within_the_declared_exceptions(
    loop: Any, env: Any, protocol: Protocol
) -> None:
    run_dir = Path(env.start(assign=["inspect=alpha"])["run_dir"])
    loop.cycle_begin(run_dir, github=env.github)
    before = _tree_state(env.root)
    reviewed = loop.review(run_dir, github=env.github, adapter_loader=protocol.adapter, tmp_root=env.tmp_root)
    after = _tree_state(env.root)

    assert [item["agent"] for item in reviewed["accepted"]] == ["alpha"], reviewed["rejected"]
    request = json.loads((protocol.copies / "request.json").read_text(encoding="utf-8"))
    candidates = json.loads(Path(loop.load_cycles(run_dir)[-1].review.reviewers[0].findings_file).read_text())
    provenance = candidates["reviewer"]["provenance"]
    assert {key: provenance[key] for key in ("run_id", "request_id", "agent_id", "role")} == {
        key: request[key] for key in ("run_id", "request_id", "agent_id", "role")
    }
    assert candidates["reviewed_head"] == env.gh.head()
    # レビュー対象・追跡ファイル・runner の worktree は変わらない
    assert reviewed["outside_changes"] == []
    assert (protocol.copies / "status.txt").read_text(encoding="utf-8") == ""
    # ファイルシステム上の変更は、指示が宣言した例外・runner の出力・セッション用の一時領域・テストの補助の中だけ
    scope = request["prompt"].split("\n\n", 1)[0].splitlines()
    tool_root = json.loads(request["prompt"].rsplit("```json\n", 1)[1].split("\n```", 1)[0])["tool_root"]
    allowed = [
        *_declared(scope, tool_root),
        *(f"{path}/" for path in (env.tmp_root, protocol.session_tmp, protocol.copies, *protocol.auxiliary)),
    ]
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    assert changed, "helper が何も書かなかった（レビューが行われていない）"
    assert [path for path in changed if not _covered(path, allowed)] == []
