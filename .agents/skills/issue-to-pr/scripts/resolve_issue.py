"""Resolve an open GitHub Issue without modifying Git or GitHub state."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)/issues/([1-9][0-9]*)/?")
NUMBER = re.compile(r"#([1-9][0-9]*)")


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


def _load_route_target() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "route_target.py"
    spec = importlib.util.spec_from_file_location("route_target", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"route_target.py を読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_target(argument: str, cwd: Path) -> tuple[str, int]:
    route_target = _load_route_target()
    try:
        current = route_target.route(cwd, route_target.resolve_env_root())
    except route_target.RouteError as error:
        raise ValueError(f"操作先を判定できません: {error}") from None
    env_repository = cast(str, route_target.ENV_REPOSITORY)
    product = cast("str | None", current.repository) if current.kind == "product" else None
    allowed: set[str] = {env_repository}
    if product:
        allowed.add(product)
    if match := ISSUE_URL.fullmatch(argument):
        repository = match[1]
        if repository.lower() not in {name.lower() for name in allowed}:
            raise ValueError(
                f"{repository} は、現在の場所から扱える Issue のリポジトリではありません（{current.reason}）"
            )
        return repository, int(match[2])
    if match := NUMBER.fullmatch(argument):
        if current.kind == "env":
            return env_repository, int(match[1])
        if product:
            return product, int(match[1])
        raise ValueError(f"#番号を解決できません: {current.reason}")
    raise ValueError("https://github.com/<owner>/<repo>/issues/N または #N を指定してください（PR は対象外）")


def resolve_issue(argument: str) -> dict[str, str | int]:
    repo, number = resolve_target(argument, Path.cwd())
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
    parser.add_argument("issue", help="Issue URL または現在の場所の #番号")
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
