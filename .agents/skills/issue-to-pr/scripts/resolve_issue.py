"""Resolve an open GitHub Issue and issue-to-pr arguments without modifying Git or GitHub state."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)/issues/([1-9][0-9]*)/?")
NUMBER = re.compile(r"#([1-9][0-9]*)")


# 利用者の引数で受け付けるオプション（名前 → 種類）。merge は値を取らない。loop はループ用オプションで、名前・値・
# 意味は pr-review-fix-loop の start と同じ（loop.py check-options が start と同じ関数で検査する）。implement は
# Plan の Task の実装の担当（implement_task.py check が検査する）。ここでは語の並び（どの語がどのオプションの値か）
# だけを決める。新しい引数はこの表に足す
Kind = Literal["merge", "loop", "implement"]
OPTIONS: dict[str, Kind] = {
    "--merge": "merge",
    "--max-cycles": "loop",
    "--hold-action": "loop",
    "--assign": "loop",
    "--template": "loop",
    "--config": "loop",
    "--implement": "implement",
}
# 起動したときの cwd を基準に絶対パスにするオプション（引数はシェルを通らないので、先頭の ~ も展開する）
PATH_OPTIONS = frozenset({"--template", "--config"})
# start にはあるが issue-to-pr では受け付けないオプションと、その理由
REFUSED_OPTIONS = {"--fresh-review": "PR を作ったばかりで、再利用するレビュー結果が無い"}


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


class ParsedInput(BaseModel):
    model_config = ConfigDict(strict=True)

    reference: str
    loop_args: list[str] | None
    implement: str | None


def parse_input(text: str, cwd: Path) -> ParsedInput:
    """利用者が書いた引数の文字列を、Issue の参照・ループ用オプションの列・--implement の値に分ける。

    ループ用オプションは書いた順のまま返す（--opt=value は 2 語に分け、パスは絶対パスにする）。
    --merge が無ければ loop_args は None。--implement は位置に関係なくループ用オプションに入れない。
    """
    try:
        words = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"引数を語に分けられません（{exc}）") from exc
    references: list[str] = []
    merge = 0
    loop_args: list[str] = []
    implement: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if not word.startswith("-"):
            references.append(word)
            continue
        name, separator, value = word.partition("=")
        if name in REFUSED_OPTIONS:
            raise ValueError(f"{name} は使えません（{REFUSED_OPTIONS[name]}）")
        if name not in OPTIONS:
            raise ValueError(f"未知のオプションです: {word}")
        kind = OPTIONS[name]
        if kind == "merge":
            if separator:
                raise ValueError(f"{name} は値を取りません: {word}")
            merge += 1
            continue
        if not separator:
            if index == len(words) or words[index].startswith("--"):
                raise ValueError(f"{name} の値がありません")
            value = words[index]
            index += 1
        if not value:
            raise ValueError(f"{name} の値が空です")
        if kind == "implement":
            implement.append(value)
            continue
        if name in PATH_OPTIONS:
            # Path() は先頭の ./ を落とすので、展開するかは書かれた文字列で決める（./~/x は cwd の ~ でホームではない）
            path = Path(value).expanduser() if value.startswith("~") else Path(value)
            value = str((cwd / path).resolve())
        loop_args += [name, value]
    if merge > 1:
        raise ValueError("--merge は 1 回だけ指定してください")
    if len(implement) > 1:
        raise ValueError("--implement は 1 回だけ指定してください")
    if loop_args and not merge:
        raise ValueError(f"{loop_args[0]} などのループ用オプションは、--merge と一緒のときだけ指定できます")
    if len(references) != 1:
        raise ValueError(f"Issue を 1 つだけ指定してください（指定された語: {references or 'なし'}）")
    return ParsedInput(
        reference=references[0],
        loop_args=loop_args if merge else None,
        implement=implement[0] if implement else None,
    )


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
    # GitHub のリポジトリ名は大文字小文字を区別しないため、照合は小文字で行い、返す名前は応答の URL のものにする
    prefix = "https://github.com/"
    expected_url = f"{prefix}{repo}/issues/{number}"
    if issue.number != number or issue.url.lower() != expected_url.lower():
        raise ValueError("応答は指定した Issue ではありません（PR または URL/番号の不一致）")
    if issue.state != "OPEN":
        raise ValueError(f"Issue は closed です: {issue.url}")
    canonical = issue.url.removeprefix(prefix).removesuffix(f"/issues/{number}")
    return {"repo": canonical, "number": number, "url": issue.url, "title": issue.title, "body": issue.body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        help="利用者の引数の先頭から最後のIssue参照・オプションまでを切り出した文字列（後ろの自然文は含めない）。"
        "IssueはURLまたは#N。オプションは--implement AGENTと、--mergeとループ用オプション。"
        "文字列を1つ渡し、-で始まりうるので前に--を置く",
    )
    args = parser.parse_args()
    try:
        parsed = parse_input(args.input, Path.cwd())
        payload: dict[str, object] = {
            **resolve_issue(parsed.reference),
            "merge": None if parsed.loop_args is None else {"loop_args": parsed.loop_args},
            "implement": parsed.implement,
        }
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
