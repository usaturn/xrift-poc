"""gh・Herdr・gitとの境界。失敗や未知の形式を空の成功結果にしない。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .models import Identity
from .storage import QueueError

TOOL_ROOT = Path(__file__).resolve().parents[5]
REPOSITORY = "usaturn/xrift-poc"
CHILD_BOARD_MARKER = re.compile(r"<!-- planning-parallel-issues board:[A-Za-z0-9_-]+ child:[A-Za-z0-9_-]+ -->")
PARENT_BOARD_MARKER = re.compile(r"<!-- planning-board:[A-Za-z0-9_-]+:begin -->")


class CommandError(QueueError):
    """外部コマンドが非0で終了した。stderrは構造化されたエラーの判定に使う。"""

    def __init__(self, tool: str, returncode: int, stderr: str):
        super().__init__(f"{tool}がexit {returncode}: {stderr[-600:]}")
        self.stderr = stderr


def command(argv: list[str], *, cwd: Path | None = None, timeout: int = 40) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QueueError(f"{argv[0]}の実行結果を確認できません: {type(error).__name__}") from error
    if result.returncode:
        raise CommandError(argv[0], result.returncode, result.stderr)
    return result.stdout.strip()


def object_json(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise QueueError("オブジェクト形式の応答ではありません")
    return cast(dict[str, Any], value)


def herdr_error_code(stderr: str) -> str | None:
    """Herdrのstderrの {"error":{"code":...}} を読む。JSONでなければNone。"""
    try:
        value = json.loads(stderr)
    except ValueError:
        return None
    error = cast(dict[str, Any], value).get("error") if isinstance(value, dict) else None
    code = cast(dict[str, Any], error).get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def agent_args(kind: str, args: list[str]) -> list[str]:
    """herdr agent startへ渡す子の引数。利用者のargvは順序を保ち、加えるのはCodexの--no-daemonだけ。

    Codexの共有app-server daemonは、起動したpaneの環境(HERDR_PANE_IDなど)でツールとフックを動かす。
    子ごとのpaneで識別するため、共有daemonを使わずに起動し、共有のapp-serverへ接続する--remoteは受け付けない。
    """
    if kind != "codex":
        return list(args)
    if any(arg == "--remote" or arg.startswith("--remote=") for arg in args):
        raise QueueError("Codexの子に--remoteは指定できません。共有のapp-serverではpaneごとの環境を保てません")
    return list(args) if "--no-daemon" in args else ["--no-daemon", *args]


def identity(pane: dict[str, Any]) -> Identity:
    session = pane.get("agent_session")
    if not isinstance(session, dict) or not cast(dict[str, Any], session).get("value"):
        raise QueueError("Herdrのnative sessionを確認できません")
    return Identity(
        pane_id=pane["pane_id"], terminal_id=pane["terminal_id"], session=json.dumps(session, sort_keys=True)
    )


class External:
    def __init__(self, parent: Path):
        self.parent = parent

    def validate_parent(self) -> None:
        route = object_json(
            command(
                [
                    sys.executable,
                    str(TOOL_ROOT / ".agents/scripts/route_target.py"),
                    str(self.parent),
                ]
            )
        )
        if route["kind"] != "env" or route["git_root"] != str(self.parent):
            raise QueueError("親main checkoutを指定してください")
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(common).resolve().parent != self.parent:
            raise QueueError("feature worktreeを保存先にはできません")

    def git(self, *args: str) -> str:
        return command(["git", "-C", str(self.parent), *args])

    def herdr(self, *args: str) -> dict[str, Any]:
        if os.environ.get("HERDR_ENV") != "1":
            raise QueueError("Herdr管理下のセッションから実行してください")
        result = object_json(command(["herdr", *args]))
        if "error" in result or not isinstance(result.get("result"), dict):
            raise QueueError("Herdrの応答を確認できません")
        return result["result"]

    def caller_agent(self) -> dict[str, Any]:
        pane_id = os.environ.get("HERDR_PANE_ID")
        if not pane_id:
            raise QueueError("現在のpaneが不明です")
        return self.herdr("agent", "get", pane_id)["agent"]

    def caller(self) -> Identity:
        return identity(self.caller_agent())

    def agents(self) -> list[dict[str, Any]]:
        agents = self.herdr("agent", "list")["agents"]
        if not isinstance(agents, list):
            raise QueueError("Herdr agent一覧が不正です")
        return cast(list[dict[str, Any]], agents)

    def pane(self, pane_id: str) -> dict[str, Any] | None:
        # 不在はHerdrの構造化エラーのcodeの完全一致だけで判定する。接続失敗・JSONでない応答・
        # 別のcodeは不在にしない。一覧に無い場合もgetで別workspaceを確認する。
        try:
            return self.herdr("pane", "get", pane_id)["pane"]
        except CommandError as error:
            if herdr_error_code(error.stderr) == "pane_not_found":
                return None
            raise

    def issue(self, number: int) -> dict[str, Any]:
        value = object_json(
            command(
                [
                    "gh",
                    "issue",
                    "view",
                    str(number),
                    "--repo",
                    REPOSITORY,
                    "--json",
                    "number,url,state,body,closedByPullRequestsReferences",
                ]
            )
        )
        if value["url"] != f"https://github.com/{REPOSITORY}/issues/{number}":
            raise QueueError("Issueの対象が一致しません")
        return value

    def is_board_child(self, issue: dict[str, Any]) -> bool:
        body = issue.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise QueueError("Issue本文の形式が不正です")
        if body and CHILD_BOARD_MARKER.fullmatch(body.splitlines()[0]):
            return True

        number = issue["number"]
        try:
            parent = object_json(command(["gh", "api", f"repos/{REPOSITORY}/issues/{number}/parent"]))
        except CommandError as error:
            if error.stderr.strip() == "gh: No parent issue found (HTTP 404)":
                return False
            raise

        repository = parent.get("repository")
        if not isinstance(repository, dict) or cast(dict[str, Any], repository).get("full_name") != REPOSITORY:
            raise QueueError("同repositoryの親Issueを確認できません")
        parent_number = parent.get("number")
        if not isinstance(parent_number, int) or parent.get("html_url") != (
            f"https://github.com/{REPOSITORY}/issues/{parent_number}"
        ):
            raise QueueError("親Issueの対象が一致しません")
        parent_body = parent.get("body")
        if parent_body is None:
            parent_body = ""
        if not isinstance(parent_body, str):
            raise QueueError("親Issue本文の形式が不正です")
        return bool(parent_body and PARENT_BOARD_MARKER.fullmatch(parent_body.splitlines()[0]))

    def pr(self, url: str) -> dict[str, Any]:
        if not re.fullmatch(rf"https://github.com/{REPOSITORY}/pull/[1-9]\d*", url):
            raise QueueError("同repositoryのPR URLを指定してください")
        return object_json(
            command(
                [
                    "gh",
                    "pr",
                    "view",
                    url,
                    "--repo",
                    REPOSITORY,
                    "--json",
                    "number,url,state,body,baseRefName,headRefName,createdAt,mergeCommit,isCrossRepository",
                ]
            )
        )

    def get(self, endpoint: str) -> dict[str, Any]:
        """同repositoryの1件（Issue・コメントなど）を読む。"""
        return object_json(command(["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]))

    def pages(self, endpoint: str) -> list[dict[str, Any]]:
        pages = json.loads(command(["gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/{endpoint}"]))
        if not isinstance(pages, list):
            raise QueueError("GitHub一覧の形式が不正です")
        pages = cast(list[Any], pages)
        if any(not isinstance(page, list) for page in pages):
            raise QueueError("GitHub一覧のページが不正です")
        return [item for page in pages for item in page]

    def worktrees(self) -> list[dict[str, str]]:
        """git worktree list --porcelain -z の各worktreeの欄（worktree・HEAD・branch・detachedなど）。"""
        records: list[dict[str, str]] = []
        for block in self.git("worktree", "list", "--porcelain", "-z").split("\0\0"):
            record: dict[str, str] = {}
            for line in block.split("\0"):
                if line:
                    key, _, value = line.partition(" ")
                    record[key] = value
            if record:
                records.append(record)
        return records

    def inventory(self) -> dict[str, Any]:
        records = self.worktrees()
        remote_main = self.git("ls-remote", "--heads", "origin", "refs/heads/main").split()
        if len(remote_main) != 2:
            raise QueueError("originのmainを確認できません")
        return {
            "head": self.git("rev-parse", "HEAD"),
            "branch": self.git("branch", "--show-current"),
            "origin_main": self.git("rev-parse", "origin/main"),
            "remote_main": remote_main[0],
            "worktrees": records,
            # 照合に使う番号とHEADだけを残す。応答の全体（本文・_linksなど）はobservationに保存しない。
            "open_prs": [
                {"number": p["number"], "head_sha": p["head"]["sha"], "head_ref": p["head"]["ref"]}
                for p in self.pages("pulls?state=open&per_page=100")
            ],
            "claims": self.git("ls-remote", "--heads", "origin", "refs/heads/agent-claims/*"),
            "local_claims": self.git("for-each-ref", "--format=%(refname)", "refs/heads/agent-claims"),
        }

    def version(self) -> dict[str, str]:
        paths = [
            TOOL_ROOT / ".agents/skills/issue-queue",
            *(
                self.parent / f".agents/skills/{skill}"
                for skill in (
                    "issue-to-pr",
                    "review-carry-over-followup",
                    "repo-cleanup-after-merge",
                    "monitoring-long-tasks",
                )
            ),
        ]
        digest = hashlib.sha256()
        for directory in paths:
            for file in sorted(directory.rglob("*")):
                if (
                    file.is_file()
                    and file.suffix in {".py", ".md"}
                    and not {"__pycache__", "tests", "evals"}.intersection(file.relative_to(directory).parts)
                ):
                    digest.update(str(file.relative_to(directory)).encode())
                    digest.update(file.read_bytes())
        return {
            "main_commit": self.git("rev-parse", "HEAD"),
            "helper_skills_sha256": digest.hexdigest(),
            "tool_root": str(TOOL_ROOT),
        }
