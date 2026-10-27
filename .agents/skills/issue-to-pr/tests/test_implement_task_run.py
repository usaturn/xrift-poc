"""Run implement_task.py's runner path against real Git worktrees with a scripted fake agent."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import Config
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.models import Agent, Assignment, BatchResult, Identity, Receipt, Role, Workflow

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当（PROGRAM）だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "implement_task.py"
SPEC = importlib.util.spec_from_file_location("issue_implement_task_run", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
mod: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

# 偽の担当。runner の worktree でシナリオの手順を実行し、TaskResult を receipt の payload にする。
# commit の "HEAD"・"HEAD~1" は、手順の後の worktree のその commit に置き換える
PROGRAM = """
import json, pathlib, subprocess, sys
request = json.load(sys.stdin)
scenario = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
work = pathlib.Path(request['work_dir'])
def git(*args):
    return subprocess.run(['git', '-C', str(work), *args], check=True, capture_output=True, text=True).stdout.strip()
for step in scenario.get('steps', []):
    kind = step['kind']
    if kind == 'write':
        path = work / step['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step.get('content', 'changed\\n'), encoding='utf-8')
    elif kind == 'delete':
        (work / step['path']).unlink()
    elif kind == 'move':
        (work / step['to']).parent.mkdir(parents=True, exist_ok=True)
        git('mv', step['from'], step['to'])
    elif kind == 'commit':
        git('add', '-A')
        git('-c', 'user.name=Fake', '-c', 'user.email=fake@example.invalid', 'commit', '-qm', 'fake')
    elif kind == 'git':
        git(*step['args'])
    elif kind == 'outside':
        pathlib.Path(step['path']).write_text('outside\\n', encoding='utf-8')
payload = dict(scenario['payload'])
if payload.get('commit') in ('HEAD', 'HEAD~1'):
    payload['commit'] = git('rev-parse', payload['commit'])
receipt = {key: request[key] for key in ('schema_version', 'run_id', 'request_id', 'agent_id', 'role')}
receipt.update(status='success', payload=payload)
pathlib.Path(sys.argv[2]).write_text(json.dumps(receipt), encoding='utf-8')
"""


class FakeAdapter:
    capabilities = Capabilities()

    def __call__(self, runtime: str) -> FakeAdapter:
        return self

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        return LaunchSpec(
            argv=(*context.agent.command, str(context.receipt_path)),
            stdin=context.request.model_dump_json().encode(),
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        if not context.receipt_path.exists():
            return AdapterOutcome(None, Identity(), "response_failed")
        return AdapterOutcome(
            Receipt.model_validate_json(context.receipt_path.read_bytes()),
            Identity(model=context.agent.model, session_id=f"session-{context.agent.model}"),
        )


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.main = tmp_path / "repo"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "config", "user.name", "Fixture")
        git(self.main, "config", "user.email", "fixture@example.invalid")
        for name, content in {
            ".gitignore": "ignored/\n",
            "src/a.py": "a = 1\n",
            "src/b.py": "b = 1\n",
            "docs/x.md": "x\n",
        }.items():
            path = self.main / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-qm", "base")
        self.feature = tmp_path / "feature"
        git(self.main, "worktree", "add", "-q", "-b", "feature", str(self.feature))
        self.revision = git(self.feature, "rev-parse", "HEAD")
        self.output = tmp_path / "output"

    def scenario(self, name: str, payload: dict[str, object] | None = None, **fields: object) -> Path:
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps({"payload": payload, **fields}), encoding="utf-8")
        return path

    def config(self, *agents: tuple[str, Path]) -> Config:
        """agents の先頭が担当。"""
        names = [name for name, _ in agents]
        return Config(
            agents={
                name: Agent(runtime="pi", command=[sys.executable, "-c", PROGRAM, str(path)], model=name, env=["PATH"])
                for name, path in agents
            },
            roles={"implement": Role(writes=True, deadline_seconds=60.0, result_shape="json")},
            workflows={"issue": Workflow(assignments={"implement": Assignment(agents=[names[0]])})},
        )

    def request(self, **overrides: object) -> Any:
        return mod.TaskRequest.model_validate(
            {
                "repository": str(self.feature),
                "revision": self.revision,
                "task": "Task 1: src を直す",
                "allowed_files": ["src/a.py", "src/new/", "文書/"],
                "commit_message": "feat: src を直す\n\nCo-Authored-By: Fixture <fixture@example.invalid>",
                "prompt": "src/a.py を直す。\n関連テスト: pytest\n",
                **overrides,
            }
        )

    def run(self, config: Config, request: Any = None) -> dict[str, Any]:
        return mod.run(config, request or self.request(), self.output, adapter_loader=FakeAdapter())


def done(commit: str | None = "HEAD", files: list[str] | None = None, status: str = "DONE") -> dict[str, object]:
    return {
        "status": status,
        "commit": commit,
        "changed_files": files if files is not None else ["src/a.py"],
        "commands": [{"argv": ["pytest", "-q"], "exit_code": 0}],
        "concerns": [],
        "incomplete": [],
        "retained_paths": [],
    }


WRITE_A = {"kind": "write", "path": "src/a.py", "content": "a = 2\n"}
COMMIT = {"kind": "commit"}


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_commit_with_leftover_changes_is_rejected(fx: Fixture) -> None:
    """commit の後に残った未 commit の変更を無視して取り込むと、担当が commit し忘れた変更が黙って欠ける。"""
    agent = fx.scenario("a", done(), steps=[WRITE_A, COMMIT, {"kind": "write", "path": "src/new/left.py"}])

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "rejected"
    assert any("src/new/left.py" in reason for reason in summary["reasons"])


def test_merge_commit_in_range_is_rejected(fx: Fixture) -> None:
    """merge commit の変更は git log --name-only に出ないので、直線でない範囲を拒まないと担当外の変更を取り込む。"""
    # revision の子の commit を feature で作ってから戻し、担当にその commit を merge させる（どちらも担当ファイルの中）
    (fx.feature / "src/new").mkdir(parents=True)
    (fx.feature / "src/new/side.py").write_text("side\n", encoding="utf-8")
    git(fx.feature, "add", "-A")
    git(fx.feature, "commit", "-qm", "side")
    side = git(fx.feature, "rev-parse", "HEAD")
    git(fx.feature, "reset", "-q", "--hard", fx.revision)
    identity = ["-c", "user.name=Fake", "-c", "user.email=fake@example.invalid"]
    merge = {"kind": "git", "args": [*identity, "merge", "-q", "--no-ff", "--no-edit", side]}
    agent = fx.scenario("a", done(files=["src/a.py", "src/new/side.py"]), steps=[WRITE_A, COMMIT, merge])

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "rejected"
    head = git(Path(summary["work_dir"]), "rev-parse", "HEAD")
    assert git(Path(summary["work_dir"]), "rev-list", "--parents", "-n", "1", head).count(" ") == 2
    assert any(head in reason for reason in summary["reasons"]), summary["reasons"]


def test_reported_intermediate_commit_is_rejected(fx: Fixture) -> None:
    """担当が最後でない commit を報告すると、後の commit を黙って取り込まず、cleanup が worktree ごと消して失う。"""
    steps = [WRITE_A, COMMIT, {"kind": "write", "path": "src/new/c.py"}, COMMIT]
    agent = fx.scenario("a", done(commit="HEAD~1", files=["src/a.py", "src/new/c.py"]), steps=steps)

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "rejected"
    with pytest.raises(mod.AcceptRefused):
        mod.accept(fx.output)
    assert git(fx.feature, "rev-parse", "HEAD") == fx.revision


def test_agent_write_to_existing_checkout_is_blocked(fx: Fixture) -> None:
    """登録済みの checkout に担当が書けると、監査は他のセッションの変更として通し、無断の変更を黙って受け入れる。"""
    agent = fx.scenario(
        "a", done(), steps=[WRITE_A, COMMIT, {"kind": "outside", "path": str(fx.feature / "stray.txt")}]
    )

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "failed"
    assert not (fx.feature / "stray.txt").exists()


@pytest.mark.parametrize("status", ["BLOCKED", "NEEDS_CONTEXT"])
def test_blocked_and_needs_context_ask_for_retry(fx: Fixture, status: str) -> None:
    """BLOCKED・NEEDS_CONTEXT を完了と同じに扱うと、途中までの変更を完了として取り込み、残りの作業を捨てる。"""
    agent = fx.scenario("a", done(status=status), steps=[WRITE_A, COMMIT])

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "retry"
    assert summary["result"]["status"] == status


def test_unconfirmed_stop_fails(fx: Fixture) -> None:
    """停止を確かめられない担当の結果を取り込むと、担当が動いたまま次の Task に進む。"""
    agent = fx.scenario("a", done(), steps=[WRITE_A, COMMIT])
    fx.run(fx.config(("a", agent)))
    record = json.loads((fx.output / "task.json").read_text(encoding="utf-8"))
    batch = BatchResult.model_validate_json(json.dumps(record["batch"]))
    batch.results[0].attempts[0].stop_confirmed = False

    assessment = mod.assess(fx.request(), batch)

    assert assessment.verdict == "failed"


def test_run_refuses_main_checkout(fx: Fixture) -> None:
    """linked worktree の判定が壊れると、accept が親の main checkout に --ff-only で取り込み、黙って編集する。"""
    with pytest.raises(mod.TaskInputError):
        fx.run(fx.config(("a", fx.scenario("a", done()))), fx.request(repository=str(fx.main)))


def test_committed_rename_out_of_disallowed_path_is_rejected(fx: Fixture) -> None:
    """リネームを 1 つの変更として読むと、担当外から中へ移したときに外のファイルの削除を見落として取り込む。"""
    agent = fx.scenario(
        "a",
        done(files=["docs/x.md", "src/new/x.md"]),
        steps=[{"kind": "move", "from": "docs/x.md", "to": "src/new/x.md"}, COMMIT],
    )

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "rejected"
    assert any("docs/x.md" in reason for reason in summary["reasons"]), summary["reasons"]


def test_uncommitted_rename_out_of_disallowed_path_is_rejected(fx: Fixture) -> None:
    """未 commit のリネーム元を確認しないと、helper の commit に担当外ファイルの削除が黙って入る。"""
    original = (fx.feature / "docs/x.md").read_text(encoding="utf-8")
    agent = fx.scenario(
        "a",
        done(commit=None, files=["docs/x.md", "src/new/x.md"]),
        steps=[{"kind": "move", "from": "docs/x.md", "to": "src/new/x.md"}],
    )

    summary = fx.run(fx.config(("a", agent)))

    assert summary["verdict"] == "rejected"
    with pytest.raises(mod.AcceptRefused):
        mod.accept(fx.output)
    assert git(fx.feature, "rev-parse", "HEAD") == fx.revision
    assert (fx.feature / "docs/x.md").read_text(encoding="utf-8") == original
    assert not (fx.feature / "src/new/x.md").exists()


def test_uncommitted_change_missing_from_changed_files_is_rejected(fx: Fixture) -> None:
    """未 commit の変更と報告を照合しないと、報告にない変更まで helper が黙って commit して取り込む。"""
    paths = ["src/a.py", "src/b.py"]
    original = {path: (fx.feature / path).read_text(encoding="utf-8") for path in paths}
    agent = fx.scenario(
        "a", done(commit=None), steps=[WRITE_A, {"kind": "write", "path": "src/b.py", "content": "b = 2\n"}]
    )

    summary = fx.run(fx.config(("a", agent)), fx.request(allowed_files=paths))

    assert summary["verdict"] == "rejected"
    with pytest.raises(mod.AcceptRefused):
        mod.accept(fx.output)
    assert git(fx.feature, "rev-parse", "HEAD") == fx.revision
    assert {path: (fx.feature / path).read_text(encoding="utf-8") for path in paths} == original


# --- accept と cleanup ----------------------------------------------------------------------------------------------


def registered(repo: Path) -> list[str]:
    output = git(repo, "worktree", "list", "--porcelain")
    return [line.removeprefix("worktree ") for line in output.splitlines() if line.startswith("worktree ")]


def test_uncommitted_changes_are_committed_by_accept(fx: Fixture) -> None:
    """accept が未 commit の変更を commit せずに完了扱いすると、担当の変更が feature の履歴に残らず失われる。"""
    agent = fx.scenario("a", done(commit=None), steps=[WRITE_A])

    summary = fx.run(fx.config(("a", agent)))
    assert summary["verdict"] == "importable"
    assert summary["helper_commit"] is True

    accepted = mod.accept(fx.output)

    assert accepted["helper_commit"] is True
    assert accepted["commit"] != fx.revision
    assert git(fx.feature, "rev-parse", "HEAD") == accepted["commit"]
    assert git(fx.feature, "show", "HEAD:src/a.py") == WRITE_A["content"].rstrip("\n")


def test_accept_refuses_non_importable(fx: Fixture) -> None:
    """担当ファイルの外を commit した結果を取り込むと、外の変更が feature branch に入り、PR で push される。"""
    agent = fx.scenario("a", done(files=["docs/x.md"]), steps=[{"kind": "write", "path": "docs/x.md"}, COMMIT])
    fx.run(fx.config(("a", agent)))

    with pytest.raises(mod.AcceptRefused, match="docs/x.md"):
        mod.accept(fx.output)

    assert git(fx.feature, "rev-parse", "HEAD") == fx.revision


# 担当ファイルの外を途中の commit で足し、次の commit で消す。最終 tree には残らないが、--ff-only で履歴に残る
REVERTED_SECRET = [
    {"kind": "write", "path": "docs/secret.txt", "content": "SECRET-VALUE\n"},
    COMMIT,
    {"kind": "delete", "path": "docs/secret.txt"},
    WRITE_A,
    COMMIT,
]


def test_accept_refuses_range_with_reverted_outside_change(fx: Fixture) -> None:
    """途中の commit で外のファイル（秘密を含む）を足して消すと、最終の tree に無くても履歴に入って push される。"""
    fx.run(fx.config(("a", fx.scenario("a", done(), steps=REVERTED_SECRET))))

    with pytest.raises(mod.AcceptRefused, match="docs/secret.txt"):
        mod.accept(fx.output)

    assert git(fx.feature, "rev-parse", "HEAD") == fx.revision


def test_cleanup_retains_dirty_worktree(fx: Fixture) -> None:
    """未 commit の変更がある runner の worktree を消すと、担当の作業が黙って消える。"""
    agent = fx.scenario("a", done(), steps=[WRITE_A, COMMIT, {"kind": "write", "path": "src/new/left.py"}])
    summary = fx.run(fx.config(("a", agent)))

    cleaned = mod.cleanup(fx.output)

    assert [item["path"] for item in cleaned["retained"]] == [summary["work_dir"]]
    assert cleaned["removed_run_dir"] is False
    assert fx.output.is_dir()
    assert summary["work_dir"] in registered(fx.main)


@pytest.mark.parametrize("field", ["stop_confirmed", "sensitive_paths"])
def test_cleanup_retains_unstopped_or_sensitive_attempt(fx: Fixture, field: str) -> None:
    """停止を確かめられない試行や秘密が残りうる試行を消すと、動いている担当の領域や漏れの手がかりを報告前に失う。"""
    summary = fx.run(fx.config(("a", fx.scenario("a", done(), steps=[WRITE_A, COMMIT]))))
    task = fx.output / "task.json"
    record = json.loads(task.read_text(encoding="utf-8"))
    attempt = record["batch"]["results"][0]["attempts"][0]
    if field == "stop_confirmed":
        attempt["stop_confirmed"] = False
    else:
        attempt["sensitive_paths"] = [str(Path(summary["work_dir"]).parent / "receipt.json")]
    task.write_text(json.dumps(record), encoding="utf-8")

    cleaned = mod.cleanup(fx.output)

    assert len(cleaned["retained"]) == 1
    assert cleaned["removed_worktrees"] == []
    assert fx.output.is_dir()
    assert summary["work_dir"] in registered(fx.main)
