"""Resolve an open GitHub Issue without modifying Git or GitHub state."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPOSITORIES = frozenset({"usaturn/maatlog-poc", "usaturn/maatlog"})
ISSUE_URL = re.compile(r"https://github\.com/(usaturn/maatlog(?:-poc)?)/issues/([1-9][0-9]*)/?")
NUMBER = re.compile(r"#([1-9][0-9]*)")
ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:)(usaturn/maatlog(?:-poc)?)(?:\.git)?/?")


class Issue(BaseModel):
    model_config = ConfigDict(strict=True)

    number: int = Field(gt=0)
    title: str
    body: str
    state: Literal["OPEN", "CLOSED", "MERGED"]
    url: str


def run_readonly(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(f"{args[0]} の読み取りに失敗: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_target(argument: str) -> tuple[str, int]:
    if match := ISSUE_URL.fullmatch(argument):
        return match[1], int(match[2])
    if match := NUMBER.fullmatch(argument):
        origin = run_readonly("git", "remote", "get-url", "origin")
        remote = ORIGIN.fullmatch(origin)
        if not remote or remote[1] not in REPOSITORIES:
            raise ValueError("現在の checkout の origin が対象リポジトリではありません")
        return remote[1], int(match[1])
    raise ValueError("対象リポジトリの https://github.com/.../issues/N または #N を指定してください（PR は対象外）")


def resolve_issue(argument: str) -> dict[str, str | int]:
    repo, number = resolve_target(argument)
    response = run_readonly(
        "gh", "issue", "view", str(number), "--repo", repo, "--json", "number,title,body,state,url"
    )
    issue = Issue.model_validate_json(response)
    expected_url = f"https://github.com/{repo}/issues/{number}"
    if issue.number != number or issue.url != expected_url:
        raise ValueError("応答は指定した Issue ではありません（PR または URL/番号の不一致）")
    if issue.state != "OPEN":
        raise ValueError(f"Issue は closed です: {issue.url}")
    return {"repo": repo, "number": number, "url": issue.url, "title": issue.title, "body": issue.body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue", help="Issue URL または現在の checkout の #番号")
    args = parser.parse_args()
    try:
        payload = resolve_issue(args.issue)
    except ValidationError:
        print("issue-to-pr: GitHub 応答の形式が不正です", file=sys.stderr)
        return 2
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"issue-to-pr: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
