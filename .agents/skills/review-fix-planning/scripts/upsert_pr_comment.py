#!/usr/bin/env python3
"""Create or update this agent's review-fix-planning status comment on a PR.

With ``--records-file`` the comment is one round's status comment carrying a
review-records v1 record (``.agents/review-records.md`` 7.4). Without it the
comment is this agent's single record-less comment; the PR review-fix loop
controller posts its progress this way through ``upsert()``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from urllib.parse import urlparse

RECORDS_MODULE = Path(__file__).resolve().parents[4] / ".agents/scripts/review_records.py"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
REPOSITORY_URL_PARTS = 4
RECORD_ONLY_MARKERS = ("a round marker", "a records block marker")


def _load_records() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_records", RECORDS_MODULE)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {RECORDS_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


records = _load_records()


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class CommentError(RuntimeError):
    """Fail closed without posting a malformed or ambiguous comment."""


def _record_problems(error: Exception) -> CommentError:
    problems = cast(list[str], getattr(error, "problems", [str(error)]))
    return CommentError("\n".join(problems))


def sanitize_agent_id(value: str) -> str:
    try:
        return cast(str, records.agent_id_for(value))
    except records.RecordError as error:
        raise CommentError("model identifier is not a safe agent id") from error


def marker_for(agent_id: str) -> str:
    return f"{records.AGENT_MARKER_PREFIX}{sanitize_agent_id(agent_id)} -->"


def _lines(body: str) -> list[str]:
    return body.replace("\r\n", "\n").split("\n")


def _trusted(comment: Mapping[str, Any]) -> bool:
    return comment.get("author_association") in records.TRUSTED_ASSOCIATIONS


def compose_body(model_name: str, markdown: str) -> str:
    """Body of a record-less comment: this agent's marker, then the markdown."""
    text = markdown.replace("\r\n", "\n").strip()
    if not text:
        raise CommentError("comment body is empty")
    kinds = [kind for kind in records.record_markers_in(text) if kind in RECORD_ONLY_MARKERS]
    if kinds:
        raise CommentError("comment body must not contain " + " or ".join(kinds) + "; only a records post has them")
    agent_id = sanitize_agent_id(model_name)
    own = marker_for(agent_id)
    lines = text.split("\n")
    rest = "\n".join(lines[1:] if lines[0] == own else lines)
    foreign = [
        match.group("agent_id")
        for match in records.AGENT_MARKER_PATTERN.finditer(rest)
        if match.group("agent_id") != agent_id
    ]
    if foreign:
        raise CommentError(
            "comment body already has a review-fix-planning marker for another agent: " + ", ".join(foreign)
        )
    if records.AGENT_MARKER_PREFIX in rest:
        raise CommentError("own review-fix-planning marker must be the first line")
    return text if lines[0] == own else f"{own}\n{text}"


def _single(matches: Sequence[int], what: str) -> int | None:
    if len(matches) > 1:
        raise CommentError(f"multiple comments carry {what}: " + ", ".join(str(item) for item in matches))
    return matches[0] if matches else None


def find_own_comment_id(comments: Sequence[Mapping[str, Any]], model_name: str) -> int | None:
    """The record-less comment: own marker on line 1 and no round marker on line 2."""
    own = marker_for(model_name)
    matches: list[int] = []
    for comment in comments:
        lines = _lines(cast(str, comment["body"]))
        if not _trusted(comment) or lines[0] != own:
            continue
        if len(lines) > 1 and records.ROUND_MARKER_PATTERN.fullmatch(lines[1]) is not None:
            continue
        matches.append(cast(int, comment["id"]))
    return _single(matches, own)


def find_round_comment_id(comments: Sequence[Mapping[str, Any]], record: Mapping[str, Any]) -> int | None:
    """This round's comment: own marker on line 1 and the same round marker on line 2."""
    own = f"{records.AGENT_MARKER_PREFIX}{record['planner']['agent_id']} -->"
    round_line = cast(str, records.round_marker(record["round"], record["reviewed_head"]))
    matches = [
        cast(int, comment["id"])
        for comment in comments
        if _trusted(comment) and _lines(cast(str, comment["body"]))[:2] == [own, round_line]
    ]
    return _single(matches, f"{own} {round_line}")


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def validate_repo(repo: str) -> str:
    if REPOSITORY.fullmatch(repo) is None:
        raise CommentError(f"repository must be owner/name: {repo}")
    return repo


def repository_from_pr_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < REPOSITORY_URL_PARTS or parts[2] != "pull":
        raise CommentError(f"cannot parse PR url: {url}")
    try:
        number = int(parts[3])
    except ValueError as error:
        raise CommentError(f"cannot parse PR url: {url}") from error
    return f"{parts[0]}/{parts[1]}", number


class GhRunner:
    def __call__(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )


def run_gh(
    args: Sequence[str],
    *,
    runner: Runner,
    input_text: str | None = None,
) -> str:
    command = ["gh", *args]
    completed = runner(command, input_text=input_text)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "gh failed"
        raise CommentError(detail)
    return completed.stdout


def load_json(text: str, what: str) -> object:
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise CommentError(f"invalid JSON from {what}") from error
    return loaded


def view_pr(pr: int, repo: str, runner: Runner, fields: Sequence[str] = ("url", "number")) -> dict[str, object]:
    """``gh pr view`` pinned to ``repo``; the URL must name the same repository and number."""
    expected = validate_repo(repo)
    raw = load_json(
        run_gh(
            ["pr", "view", str(pr), "--repo", expected, "--json", ",".join(fields)],
            runner=runner,
        ),
        "gh pr view",
    )
    parsed = _json_object(raw)
    if parsed is None:
        raise CommentError("unexpected gh pr view payload")
    url = parsed.get("url")
    number = parsed.get("number")
    if not isinstance(url, str) or not isinstance(number, int):
        raise CommentError("gh pr view did not return url and number")
    parsed_repo, parsed_number = repository_from_pr_url(url)
    if parsed_number != number or number != pr:
        raise CommentError("PR number from gh pr view does not match")
    if parsed_repo != expected:
        raise CommentError(f"gh pr view resolved {parsed_repo}, not the requested {expected}")
    return parsed


def resolve_pr(pr: int, repo: str, runner: Runner) -> tuple[str, int]:
    parsed = view_pr(pr, repo, runner)
    return repo, cast(int, parsed["number"])


def list_comments(repo: str, pr: int, runner: Runner) -> list[dict[str, Any]]:
    raw = load_json(
        run_gh(
            ["api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments"],
            runner=runner,
        ),
        "gh api comments",
    )
    try:
        return cast(list[dict[str, Any]], records.comments_from_api(raw))
    except records.RecordError as error:
        raise _record_problems(error) from error


def _checked_record(record: Mapping[str, Any], model_name: str, markdown: str) -> tuple[dict[str, Any], str]:
    """Validate the record on its own and compose the full comment before any network call."""
    try:
        checked = cast(dict[str, Any], records.validate_record(dict(record)))
        body = cast(str, records.compose_comment(checked, markdown))
    except records.RecordError as error:
        raise _record_problems(error) from error
    if checked["planner"]["model"] != model_name:
        raise CommentError(f"planner.model {checked['planner']['model']!r} does not match --model-name {model_name!r}")
    return checked, body


def _check_pull_request(checked: Mapping[str, Any], repo: str, pr: int, runner: Runner) -> None:
    """The record names this PR, the PR is open, and its head is the HEAD the round ends at."""
    view = view_pr(pr, repo, runner, ("url", "number", "headRefOid", "state"))
    if checked["pr"] != {"repository": repo, "number": view["number"]}:
        raise CommentError(f"the record is for another pull request: {checked['pr']}")
    if view.get("state") != "OPEN":
        raise CommentError(f"pull request {repo}#{pr} is not open")
    outcome = checked["outcome"]
    expected = checked["commit"] if outcome == "fixes_pushed" else checked["reviewed_head"]
    if view.get("headRefOid") != expected:
        head = view.get("headRefOid")
        raise CommentError(f"the PR head is {head}, but a {outcome} round must be posted at {expected}")


def _round_target(checked: Mapping[str, Any], comments: Sequence[Mapping[str, Any]]) -> int | None:
    """Find this round's comment and check the record against every other record."""
    existing = find_round_comment_id(comments, checked)
    excluded = () if existing is None else (existing,)
    try:
        prior = records.read_state(comments, expected_pr=checked["pr"], exclude_comment_ids=excluded)
        records.check_against(checked, prior)
    except records.RecordError as error:
        raise _record_problems(error) from error
    return existing


def upsert(
    pr: int,
    repo: str,
    model_name: str,
    markdown: str,
    *,
    runner: Runner | None = None,
    record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active = GhRunner() if runner is None else runner
    if record is None:
        body = compose_body(model_name, markdown)
        repo, pr_number = resolve_pr(pr, repo, active)
        existing = find_own_comment_id(list_comments(repo, pr_number, active), model_name)
    else:
        checked, body = _checked_record(record, model_name, markdown)
        _check_pull_request(checked, repo, pr, active)
        pr_number = pr
        existing = _round_target(checked, list_comments(repo, pr_number, active))
    payload = json.dumps({"body": body}, ensure_ascii=False)
    if existing is None:
        raw = run_gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--input",
                "-",
            ],
            runner=active,
            input_text=payload,
        )
        action = "created"
    else:
        raw = run_gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/comments/{existing}",
                "--input",
                "-",
            ],
            runner=active,
            input_text=payload,
        )
        action = "updated"
    created = _json_object(load_json(raw, "gh api upsert"))
    if created is None or not isinstance(created.get("id"), int):
        raise CommentError("upsert response did not include a comment id")
    url = created.get("html_url")
    return {
        "action": action,
        "comment_id": created["id"],
        "url": url if isinstance(url, str) else "",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument(
        "--repo",
        required=True,
        help="owner/name of the repository holding the PR. Never inferred from cwd.",
    )
    parser.add_argument("--model-name", required=True)
    parser.add_argument(
        "--body-file",
        help="Markdown body without the markers. Defaults to stdin.",
    )
    parser.add_argument(
        "--records-file",
        help="review-records v1 round record (JSON). Posts this round's status comment with the record.",
    )
    return parser.parse_args(argv)


def read_body(path: str | None) -> str:
    if path is None:
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def read_record(path: str) -> Mapping[str, Any]:
    try:
        value: object = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise CommentError(f"{path}: {error.strerror or type(error).__name__}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CommentError(f"{path}: not a JSON record ({error})") from error
    mapping = _json_object(value)
    if mapping is None:
        raise CommentError(f"{path}: the record must be a JSON object")
    return mapping


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        record = None if args.records_file is None else read_record(args.records_file)
        result = upsert(args.pr, args.repo, args.model_name, read_body(args.body_file), record=record)
    except CommentError as error:
        print(str(error), file=sys.stderr)
        return 64
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
