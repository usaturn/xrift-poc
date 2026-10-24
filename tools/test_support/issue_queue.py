"""Issue queue のCLI試験用GitHub/Herdr。実サービスに接続しない。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.test_support.repos import create_env_repo

REPOSITORY = "usaturn/xrift-poc"
ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / ".agents/skills/issue-queue/scripts/queue.py"
# remoteのcloneには利用者の設定が無いので、commitの名前と署名・remote名・ローカルのcloneを固定する。
GIT_FLAGS = (
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "clone.defaultRemoteName=origin",
    "-c",
    "protocol.file.allow=always",
)


def pane(pane_id: str, session: str, *, name: str = "controller", state: str = "working") -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "agent": "claude",
        "name": name,
        "agent_status": state,
        "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": session},
        "terminal_id": "term_" + pane_id,
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "revision": 1,
    }


@dataclass
class QueueEnv:
    root: Path
    data_file: Path
    env: dict[str, str]
    home: Path
    remote: Path

    def data(self) -> dict[str, Any]:
        return json.loads(self.data_file.read_text())

    def save(self, data: dict[str, Any]) -> None:
        self.data_file.write_text(json.dumps(data))

    def json_file(self, name: str, value: object) -> Path:
        file = self.home / name
        file.write_text(json.dumps(value, ensure_ascii=False))
        return file

    def call(self, *args: str, pane_id: str = "w1:p1", code: int = 0) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=self.root,
            env={**self.env, "HERDR_PANE_ID": pane_id},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == code, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    def init_file(self, *, trial: bool = False, kind: str = "claude", args: list[str] | None = None) -> Path:
        return self.json_file(
            "input.json",
            {
                "issues": [11, 12],
                "policy": {
                    "agent_kind": kind,
                    "agent_args": args or [],
                    "trial": trial,
                    "remote_branches": "keep",
                    "additional_issues": "propose",
                    "authorization": "#11と#12を順に処理。\n外部への報告は含めない。",
                    "exclusions": ["deployment"],
                },
            },
        )

    def init(self, *, trial: bool = False, kind: str = "claude", args: list[str] | None = None) -> str:
        file = self.init_file(trial=trial, kind=kind, args=args)
        return self.call("init", "--input", str(file))["run_dir"]

    def launch(self, run: str) -> dict[str, Any]:
        return self.call("launch", "--run", run)["run"]["attempts"][-1]

    def interrupt_launch(self, run: str, boundary: str) -> None:
        """本来のsaveを実行した直後にプロセスを中断する。保存状態を捏造しない。"""
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from issue_queue.cli import main
from issue_queue.storage import Store
run, boundary = sys.argv[2:]
original = Store.save
def save(self, path, state):
    original(self, path, state)
    attempt = state.current()
    if attempt and attempt.launch == boundary:
        raise KeyboardInterrupt("interrupted after persisted boundary")
Store.save = save
sys.argv = ["queue.py", "launch", "--run", run]
main()
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(CLI.parent), run, boundary],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0 and "KeyboardInterrupt" in result.stderr, (
            result.returncode,
            result.stdout[:1000],
            result.stderr[-1000:],
        )

    def child(self, attempt: dict[str, Any], state: str) -> None:
        data = self.data()
        data["panes"][attempt["pane_id"]]["agent_status"] = state
        self.save(data)

    def ack(self, run: str, attempt: dict[str, Any]) -> dict[str, Any]:
        return self.call("ack", "--run", run, "--attempt", attempt["id"], pane_id=attempt["pane_id"])

    def merge(self, attempt: dict[str, Any], *, marker: bool = True, close: bool = True) -> str:
        data = self.data()
        url = f"https://github.com/{REPOSITORY}/pull/100"
        data["prs"] = [
            {
                "number": 100,
                "url": url,
                "html_url": url,
                "state": "MERGED",
                "merged_at": "2026-10-20T00:00:00Z",
                "body": attempt["marker"] if marker else "old run",
                "createdAt": attempt["created_at"],
                "created_at": attempt["created_at"],
                "baseRefName": "main",
                "headRefName": "feat/issue-11-change",
                "base": {"ref": "main"},
                "head": {"ref": "feat/issue-11-change"},
                "isCrossRepository": False,
                "mergeCommit": {"oid": self.git("rev-parse", "HEAD")},
            }
        ]
        if close:
            data["issues"]["11"]["state"] = "CLOSED"
            data["issues"]["11"]["closedByPullRequestsReferences"] = [{"number": 100, "url": url}]
        self.save(data)
        return url

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def push_main(self, file: str, content: str) -> str:
        """main checkoutを動かさずに、remoteのmainへcommitを1つ足してSHAを返す。別のPRのマージを再現する。"""
        clone = Path(tempfile.mkdtemp(prefix="remote-clone-", dir=self.home))
        git = ["git", *GIT_FLAGS]
        # bareのremoteのHEADはmainを指さないので、mainを明示してcloneする。
        subprocess.run([*git, "clone", "-q", "--branch", "main", str(self.remote), str(clone)], check=True)
        (clone / file).write_text(content)
        subprocess.run([*git, "-C", str(clone), "add", "--", file], check=True)
        subprocess.run([*git, "-C", str(clone), "commit", "-qm", f"update {file}"], check=True)
        subprocess.run([*git, "-C", str(clone), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
        return subprocess.check_output([*git, "-C", str(clone), "rev-parse", "HEAD"], text=True).strip()

    def update_main(self) -> None:
        """司令塔がupdate_mainで行う更新（fetchとdetachedのcheckout）を再現する。失敗はCalledProcessError。"""
        self.git("fetch", "-q", "origin", "main")
        self.git("checkout", "-q", "--detach", "origin/main")

    def result(self, run: str, attempt: dict[str, Any], **overrides: Any) -> Path:
        result: dict[str, Any] = {
            "outcome": "success",
            "summary": "完了しました。\n参照を確認済み。",
            "finalized": True,
            "background_stopped": True,
            "retained": [],
            "urls": [],
            "issues": [],
            "loop_run": None,
        }
        if attempt["phase"] == "implement":
            loop_dir = self.root / "reviews/usaturn/xrift-poc/#100-change/loop/test-run"
            loop_dir.mkdir(parents=True, exist_ok=True)
            loop: dict[str, Any] = {
                "run_id": "test-run",
                "pr": {"repository": REPOSITORY, "number": 100, "url": f"https://github.com/{REPOSITORY}/pull/100"},
                "started_at": attempt["created_at"],
                "state": "ended",
                "ended_at": "2026-10-20T00:00:00Z",
                "result": {"outcome": "no_fix_needed", "ended_by": "outcome", "merged": True},
                "resources": [],
            }
            (loop_dir / "run.json").write_text(json.dumps(loop))
            result["loop_run"] = str(loop_dir / "run.json")
        result.update(overrides)
        return self.json_file(f"result-{attempt['id']}.json", result)

    def report(self, run: str, attempt: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        file = self.result(run, attempt, **overrides)
        return self.call(
            "report", "--run", run, "--attempt", attempt["id"], "--input", str(file), pane_id=attempt["pane_id"]
        )

    def followup(self) -> tuple[str, dict[str, Any]]:
        run = self.init()
        attempt = self.launch(run)
        self.ack(run, attempt)
        self.merge(attempt)
        self.report(run, attempt)
        self.child(attempt, "done")
        self.call("advance", "--run", run)
        followup = self.launch(run)
        self.ack(run, followup)
        return run, followup

    def cleanup(self) -> tuple[str, dict[str, Any]]:
        """持ち越しを終えて片付けの子を起動し、ackまで進める。"""
        run, followup = self.followup()
        self.report(run, followup)
        self.child(followup, "done")
        self.call("advance", "--run", run)
        cleanup = self.launch(run)
        self.ack(run, cleanup)
        return run, cleanup


def make_queue_env(base: Path) -> QueueEnv:
    repo = create_env_repo(base / "repo", copy_paths=())
    remote = base / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo.root), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo.root), "config", f"url.{remote}.insteadOf", f"https://github.com/{REPOSITORY}.git"],
        check=True,
    )
    home = base / "home"
    home.mkdir()
    bin_dir = base / "bin"
    bin_dir.mkdir()
    data_file = base / "services.json"
    data: dict[str, Any] = {
        "panes": {"w1:p1": pane("w1:p1", "controller-session")},
        "calls": [],
        "next_pane": 2,
        "prs": [],
        "comments": [],
        "fail": None,
        "fail_after": None,
        "parents": {},
        "issues": {
            str(n): {
                "number": n,
                "url": f"https://github.com/{REPOSITORY}/issues/{n}",
                "state": "OPEN",
                "body": "independent issue",
                "closedByPullRequestsReferences": [],
            }
            for n in (11, 12)
        },
    }
    data_file.write_text(json.dumps(data))
    for command in ("gh", "herdr"):
        executable = bin_dir / command
        executable.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
        executable.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(home),
        "XRIFT_POC_ROOT": str(repo.root),
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "w1:p1",
        "QUEUE_FAKE_DATA": str(data_file),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    return QueueEnv(repo.root, data_file, environment, home, remote)


FAKE_CLI = r"""
import json, os, sys
from pathlib import Path
file = Path(os.environ["QUEUE_FAKE_DATA"])
data = json.loads(file.read_text())
args = sys.argv[1:]
tool = Path(sys.argv[0]).name
key = tool + " " + " ".join(args[:2])
# failは "herdr agent get" のように操作全体か、"herdr agent get <対象>" のように対象を絞って指定する。
failing = data.get("fail") in (key, tool + " " + " ".join(args[:3]))
data["calls"].append([tool, *args])
def emit(value):
    file.write_text(json.dumps(data))
    if data.get("fail_after") == key:
        print("response lost", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(value))
    sys.exit(0)
def error(code="fake_failure", message="fake failure"):
    # 実Herdr 0.9.5はstderrへ {"error":{"code","message"},"id"} を出してexit 1になる。ghは文章を出す。
    file.write_text(json.dumps(data))
    if failing and data.get("fail_stderr") is not None:
        print(data["fail_stderr"], file=sys.stderr)
    elif tool == "herdr":
        print(json.dumps({"error": {"code": code, "message": message}, "id": "cli:" + ":".join(args[:2])}),
              file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    sys.exit(1)
if failing:
    error()
if tool == "herdr":
    if args[:2] in (["pane", "list"], ["agent", "list"]):
        emit({"result": {"panes" if args[0] == "pane" else "agents": list(data["panes"].values())}})
    if args[:2] in (["pane", "get"], ["agent", "get"]):
        target = args[2]
        p = next((p for p in data["panes"].values() if p["pane_id"] == target or p.get("name") == target), None)
        if p is None: error(args[0] + "_not_found", args[0] + " " + target + " not found")
        emit({"result": {"pane" if args[0] == "pane" else "agent": p}})
    if args[:2] == ["pane", "split"]:
        p = "w1:p" + str(data["next_pane"])
        data["next_pane"] += 1
        data["panes"][p] = {"pane_id": p, "agent_status": "unknown", "agent": None,
                             "terminal_id": "term_" + p}
        emit({"result": {"pane": data["panes"][p]}})
    if args[:2] == ["agent", "start"]:
        p = args[args.index("--pane") + 1]
        kind = args[args.index("--kind") + 1]
        data["panes"][p].update({"name": args[2], "agent": kind, "agent_status": "idle"})
        if kind == "codex":
            # 実CodexはstartとそのgetにHerdrのnative sessionが無く、最初の入力の後で現れる。
            data["panes"][p]["agent_status"] = "done"
        else:
            data["panes"][p]["agent_session"] = {"agent": kind, "kind": "id", "source": "herdr:" + kind,
                                                 "value": "session-" + p}
        emit({"result": {"agent": data["panes"][p], "argv": [kind], "type": "agent_started"}})
    if args[:2] == ["agent", "prompt"]:
        p = next(p for p in data["panes"].values() if p.get("name") == args[2])
        p["agent_status"] = "working"
        p.setdefault("agent_session", {"agent": p["agent"], "kind": "id", "source": "herdr:" + p["agent"],
                                       "value": "session-" + p["pane_id"]})
        emit({"result": {"agent": p}})
    if args[:2] == ["agent", "wait"]:
        if data.get("wait_seconds"):
            # 実Herdrは子がworkingの間ブロックする。開始を示すファイルを置いてから待つ。
            Path(str(file) + ".waiting").touch()
            import time
            time.sleep(data["wait_seconds"])
        emit({"result": {"state": "done"}})
    if args[:2] == ["pane", "close"]:
        data["panes"].pop(args[2], None)
        emit({"result": {}})
if tool == "gh":
    if args[:2] == ["issue", "view"]:
        assert args[args.index("--repo") + 1] == "usaturn/xrift-poc"
        emit(data["issues"][args[2]])
    if args[:2] == ["pr", "view"]:
        assert args[args.index("--repo") + 1] == "usaturn/xrift-poc"
        p = next((p for p in data["prs"] if str(p["number"]) == args[2] or p["url"] == args[2]), None)
        if p is None: error(message="no pull requests found")
        emit(p)
    if args[0] == "api":
        endpoint = next(a for a in args[1:] if a.startswith("repos/"))
        assert endpoint.startswith("repos/usaturn/xrift-poc/")
        if endpoint.endswith("/parent"):
            number = endpoint.split("/")[-2]
            parent_number = data["parents"].get(number)
            if parent_number is None:
                error(message="gh: No parent issue found (HTTP 404)")
            emit(data["issues"][str(parent_number)])
        if "--method" in args:
            assert args[args.index("--method") + 1] == "POST"
            body = json.loads(Path(args[args.index("--input") + 1]).read_text())
            n = 200 + len(data["comments"]) + len(data["issues"])
            if endpoint.endswith("/comments"):
                item = {"id": n, "body": body["body"],
                        "html_url": "https://github.com/usaturn/xrift-poc/issues/100#issuecomment-" + str(n)}
                data["comments"].append(item)
            else:
                item = {"number": n, "state": "OPEN", "body": body["body"], "title": body["title"],
                        "url": "https://github.com/usaturn/xrift-poc/issues/" + str(n),
                        "html_url": "https://github.com/usaturn/xrift-poc/issues/" + str(n)}
                data["issues"][str(n)] = item
            emit(item)
        if endpoint.split("?")[0].endswith("/comments"):
            emit([data["comments"]] if "--slurp" in args else data["comments"])
        if endpoint.endswith("/blocked_by"): emit([[]])
        if "/pulls?" in endpoint:
            prs = data["prs"]
            if "state=open" in endpoint: prs = [p for p in prs if p["state"] == "OPEN"]
            emit([prs] if "--slurp" in args else prs)
        if "/issues?" in endpoint:
            emit([list(data["issues"].values())])
error("unsupported", "fake: unsupported " + key)
"""
