"""pr-review の行動確認（evals）に使う架空の PR を、fixture の定義から組み立てる。

使い方:
  uv run --no-project python .agents/skills/pr-review/evals/build_fixture.py <fixture 名> <空の出力ディレクトリ>

出力ディレクトリに次を作り、パスの一覧を JSON で標準出力に書く（同じ内容を build.json にも保存する）。

- worktree/: PR の head を checkout した git リポジトリ（base ブランチと head ブランチを持つ）
- pr.json: `gh pr view --json` と同じ形の PR の情報（number・title・body・baseRefName・headRefName・
  headRefOid・url・additions・deletions・changedFiles）
- pr.diff: PR 全体の差分（base ブランチとの merge-base から head まで）
- pr-comments.json: `gh api --paginate --slurp repos/<owner>/<repo>/issues/<N>/comments` と同じ形
- previous-review.md: 自モデルの前回レビュー（fixture が定義する場合だけ）

fixture のソースのうち名前が `.txt` で終わるファイルは、`.txt` を除いた名前で書き出す。リポジトリの
ruff・pyright・pytest の対象から外すためである。commit の作者と時刻を固定し、SHA を毎回同じにする。
判断の記録を含むコメントは、`.agents/scripts/review_records.py` の `compose_comment` で組み立てる。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
REVIEW_RECORDS = EVALS.parents[2] / "scripts" / "review_records.py"
AUTHOR = "Eval Author"
EMAIL = "eval@example.com"
EPOCH = 1_780_000_000


class FixtureError(Exception):
    """fixture の定義・出力先・git の失敗。"""


def fixture_names() -> list[str]:
    return sorted(path.parent.name for path in FIXTURES.glob("*/fixture.json"))


def _review_records() -> Any:
    spec = importlib.util.spec_from_file_location("review_records", REVIEW_RECORDS)
    if spec is None or spec.loader is None:
        raise FixtureError(f"cannot load {REVIEW_RECORDS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_env(step: int) -> dict[str, str]:
    stamp = f"@{EPOCH + step * 60} +0000"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": AUTHOR,
            "GIT_AUTHOR_EMAIL": EMAIL,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": AUTHOR,
            "GIT_COMMITTER_EMAIL": EMAIL,
            "GIT_COMMITTER_DATE": stamp,
        }
    )
    return env


def _git(repo: Path, args: list[str], *, step: int = 0) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, env=_git_env(step), check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise FixtureError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _switch(repo: Path, branch: str, base_branch: str) -> None:
    current = _git(repo, ["symbolic-ref", "--short", "HEAD"]).strip()
    if current == branch:
        return
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        env=_git_env(0),
        check=False,
        capture_output=True,
    )
    if exists.returncode == 0:
        _git(repo, ["switch", "-q", branch])
    else:
        _git(repo, ["switch", "-q", "-c", branch, base_branch])


def _apply_tree(source: Path, repo: Path, delete: list[str]) -> None:
    if not source.is_dir():
        raise FixtureError(f"missing tree: {source}")
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = repo / relative.parent / relative.name.removesuffix(".txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    for item in delete:
        (repo / item).unlink()


def _substitute(text: str, heads: dict[str, str]) -> str:
    for label, sha in heads.items():
        text = text.replace("{{" + label + ":12}}", sha[:12]).replace("{{" + label + "}}", sha)
    if "{{" in text:
        raise FixtureError("unresolved placeholder in a fixture template")
    return text


def _comments(fixture: Path, entries: list[dict[str, Any]], heads: dict[str, str]) -> list[list[dict[str, Any]]]:
    records = _review_records()
    page: list[dict[str, Any]] = []
    for entry in entries:
        if "record" in entry:
            record = json.loads(_substitute((fixture / str(entry["record"])).read_text(encoding="utf-8"), heads))
            body = _substitute((fixture / str(entry["body"])).read_text(encoding="utf-8"), heads)
            text = cast(str, records.compose_comment(record, body))
        else:
            text = str(entry["text"])
        page.append(
            {
                "id": int(entry["id"]),
                "body": text,
                "user": {"login": str(entry["author"])},
                "author_association": str(entry["association"]),
            }
        )
    return [page]


def build(name: str, out_dir: Path) -> dict[str, Any]:
    """fixture を out_dir に組み立て、build.json と同じ内容の辞書を返す。"""
    fixture = FIXTURES / name
    definition_path = fixture / "fixture.json"
    if not definition_path.is_file():
        raise FixtureError(f"unknown fixture: {name}")
    definition = cast(dict[str, Any], json.loads(definition_path.read_text(encoding="utf-8")))
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FixtureError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()
    repo = out_dir / "worktree"
    repo.mkdir()
    base_branch = str(definition["base_branch"])
    head_branch = str(definition["head_branch"])
    steps = cast(list[dict[str, Any]], definition["steps"])
    if not steps or steps[0].get("branch") != "base" or "tree" not in steps[0]:
        raise FixtureError("the first step must add a tree to the base branch")
    _git(repo, ["init", "-q", "-b", base_branch])
    heads: dict[str, str] = {}
    for index, step in enumerate(steps, start=1):
        branch = base_branch if step["branch"] == "base" else head_branch
        _switch(repo, branch, base_branch)
        if "merge" in step:
            source = base_branch if step["merge"] == "base" else head_branch
            _git(repo, ["merge", "-q", "--no-ff", "-m", str(step["message"]), source], step=index)
        else:
            _apply_tree(fixture / "trees" / str(step["tree"]), repo, cast(list[str], step.get("delete", [])))
            _git(repo, ["add", "-A"], step=index)
            _git(repo, ["commit", "-q", "-m", str(step["message"])], step=index)
        heads[str(step["label"])] = _git(repo, ["rev-parse", "HEAD"]).strip()
    _switch(repo, head_branch, base_branch)
    head_sha = _git(repo, ["rev-parse", "HEAD"]).strip()
    base_sha = _git(repo, ["rev-parse", base_branch]).strip()
    merge_base = _git(repo, ["merge-base", base_branch, head_branch]).strip()
    diff = _git(repo, ["diff", f"{base_branch}...{head_branch}"])
    additions = deletions = changed = 0
    for line in _git(repo, ["diff", "--numstat", f"{base_branch}...{head_branch}"]).splitlines():
        added, removed, _path = line.split("\t", 2)
        additions += int(added)
        deletions += int(removed)
        changed += 1
    repository = str(definition["repository"])
    number = int(definition["number"])
    url = f"https://github.com/{repository}/pull/{number}"
    pr = {
        "number": number,
        "title": str(definition["title"]),
        "body": str(definition["body"]),
        "baseRefName": base_branch,
        "headRefName": head_branch,
        "headRefOid": head_sha,
        "url": url,
        "additions": additions,
        "deletions": deletions,
        "changedFiles": changed,
    }
    (out_dir / "pr.json").write_text(json.dumps(pr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "pr.diff").write_text(diff, encoding="utf-8")
    comments = _comments(fixture, cast(list[dict[str, Any]], definition.get("comments", [])), heads)
    (out_dir / "pr-comments.json").write_text(
        json.dumps(comments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    previous_review: str | None = None
    if definition.get("previous_review"):
        template = (fixture / str(definition["previous_review"])).read_text(encoding="utf-8")
        target = out_dir / "previous-review.md"
        target.write_text(_substitute(template, heads), encoding="utf-8")
        previous_review = str(target)
    manifest: dict[str, Any] = {
        "fixture": name,
        "repository": repository,
        "number": number,
        "pr_url": url,
        "worktree": str(repo),
        "pr_json": str(out_dir / "pr.json"),
        "pr_diff": str(out_dir / "pr.diff"),
        "pr_comments": str(out_dir / "pr-comments.json"),
        "previous_review": previous_review,
        "heads": heads,
        "base_sha": base_sha,
        "merge_base": merge_base,
        "head_sha": head_sha,
    }
    (out_dir / "build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pr-review の evals の fixture を組み立てる")
    parser.add_argument("fixture", choices=fixture_names())
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = build(cast(str, args.fixture), cast(Path, args.out_dir))
    except FixtureError as error:
        print(f"build_fixture: {error}", file=sys.stderr)
        return 65
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
