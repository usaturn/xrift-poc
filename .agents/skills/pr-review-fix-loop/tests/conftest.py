"""PR 修正ループの helper を、一時 Git・ローカルの bare remote・偽の gh で動かすための fixture。

テストの途中で計画担当（review-fix-planning）の代わりに、記録の投稿・commit の push・マージを模す。
実際のエージェントと GitHub は使わない。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
REPOSITORY = "usaturn/xrift-poc"
NUMBER = 900
BRANCH = "feature/demo"
PR_URL = f"https://github.com/{REPOSITORY}/pull/{NUMBER}"
PLANNER_MODEL = "claude-opus-5-5"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LOOP: Any = _load("pr_review_fix_loop_helper", SKILL_DIR / "scripts/loop.py")
RECORDS: Any = _load("pr_review_fix_loop_records", REPO_ROOT / ".agents/scripts/review_records.py")
FINDINGS: Any = _load("pr_review_fix_loop_findings", REPO_ROOT / ".agents/scripts/review_findings.py")


def candidates_document(
    head: str, base: str, model: str, review_file: str, *, findings: int = 0, agent_id: str = "unknown"
) -> dict[str, Any]:
    """review_findings.validate_document を通る v2 の候補一覧（pr-review の complete が保存する形）。"""
    items = [
        {
            "id": f"H-{index}",
            "severity": "High",
            "title": f"テストの指摘 {index}",
            "locations": [f"change.txt:{index}"],
            "detected_head": head[:12],
            "confidence": "verified",
            "pr_relation": {"value": "introduced", "basis": "この PR が足した行"},
            "property": "テストの性質",
            "variants": [],
            "statement_sites": [],
        }
        for index in range(1, findings + 1)
    ]
    return {
        "schema": "review-findings/v2",
        "kind": "candidates",
        "source": "reviewer",
        "pr": {"repository": REPOSITORY, "number": NUMBER},
        "reviewed_head": head,
        "base": base,
        "reviewer": {
            "model": model,
            "runtime": "pi",
            "review_file": review_file,
            "provenance": {
                "agent_id": agent_id,
                "role": "inspect",
                "provider": "unknown",
                "effort": "unknown",
                "session_id": "unknown",
                "cli_version": "unknown",
                "run_id": "unknown",
                "request_id": "unknown",
                "skills": [{"path": ".agents/skills/pr-review/SKILL.md", "sha256": "0" * 64}],
            },
        },
        "skill": {"path": ".agents/skills/pr-review/SKILL.md", "sha256": "0" * 64},
        "timing": {
            "started_at": "2026-10-08T15:00:00Z",
            "finished_at": "2026-10-08T15:01:00Z",
            "seconds": 60,
            "waiting_seconds": None,
        },
        "coverage": {"read": ["PR 全体の差分"], "checks": [], "declined": []},
        "findings": items,
        "unverified_candidates": [],
    }


def review_markdown(head: str, model: str, *, findings: int = 0) -> str:
    """candidates_document と check_pair で一致する本文。"""
    sections = "".join(
        f"## High\n\n### H-{index}: テストの指摘 {index}【検証済み】\n\n"
        f"- 位置: `change.txt:{index}`\n- 検出時 HEAD: `{head[:12]}`\n- 内容: テスト\n\n"
        for index in range(1, findings + 1)
    )
    return (
        f"- 対象: PR #{NUMBER} / head {head[:12]} / 2026-10-08\n"
        f"- レビュアー: {model}（ランタイム: pi）\n\n"
        "## Review history\n\n| Reviewed at (UTC) | HEAD | Mode |\n|---|---|---|\n"
        f"| 2026-10-08T15:00:00Z | `{head[:12]}` | Initial |\n\n"
        f"## Summary\n\nテスト\n\n{sections}"
    )


def write_pair(
    review_dir: Path, model: str, head: str, base: str, *, commit: bool = False, findings: int = 0
) -> tuple[Path, Path]:
    """レビューディレクトリに成果 2 件を置く。commit=True は指定 commit のレビューの保存名。"""
    stem = f"{model}-commit-{head}" if commit else model
    review = review_dir / f"{stem}.md"
    candidates = review_dir / "findings" / f"{stem}-{head[:12]}.json"
    candidates.parent.mkdir(parents=True, exist_ok=True)
    document = candidates_document(head, base, model, review.name, findings=findings)
    FINDINGS.validate_document(document)
    markdown = review_markdown(head, model, findings=findings)
    FINDINGS.check_pair(markdown, document)
    review.write_text(markdown, encoding="utf-8")
    candidates.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return review, candidates


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")


class FakeGitHub:
    """PR の状態・コメント・マージを持つ偽の gh。head は bare remote の branch から読む。"""

    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.state = "open"
        self.merged = False
        self.merged_head: str | None = None
        self.comments: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []
        self._next_id = 1000

    def head(self) -> str:
        if self.merged_head is not None:
            return self.merged_head
        return git(self.remote, "rev-parse", f"refs/heads/{BRANCH}")

    def post_record(self, record: dict[str, Any], body: str = "### レビュー状況\n記録のテスト\n") -> int:
        self._next_id += 1
        self.comments.append(
            {
                "id": self._next_id,
                "body": RECORDS.compose_comment(record, body),
                "user": {"login": "usaturn"},
                "author_association": "OWNER",
            }
        )
        return self._next_id

    def merge(self) -> None:
        self.merged_head = self.head()
        self.state = "closed"
        self.merged = True

    def runner(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        pulls = f"repos/{REPOSITORY}/pulls/{NUMBER}"
        comments = f"repos/{REPOSITORY}/issues/{NUMBER}/comments"
        # GitHub の API はリポジトリ名の大文字小文字を区別せず、応答には正式な名前を返す
        if args[:2] == ["gh", "api"] and args[-1].lower() == pulls.lower() and len(args) == 3:
            body = {
                "number": NUMBER,
                "html_url": PR_URL,
                "state": self.state,
                "merged": self.merged,
                "head": {"sha": self.head(), "ref": BRANCH},
                "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if (
            args[:2] == ["gh", "api"]
            and args[-1].lower() == comments.lower()
            and "--paginate" in args
            and "--slurp" in args
        ):
            return subprocess.CompletedProcess(args, 0, json.dumps([self.comments]), "")
        return subprocess.CompletedProcess(args, 2, "", f"fake gh does not support {args}")


@dataclass
class LoopEnv:
    root: Path
    parent: Path
    remote: Path
    work: Path
    tmp_root: Path
    github: Any
    gh: FakeGitHub
    run_dirs: list[Path] = field(default_factory=list[Path])

    @property
    def review_dir(self) -> Path:
        return self.parent / "reviews" / "usaturn" / "xrift-poc" / f"#{NUMBER}-feature-demo"

    def push(self, message: str) -> str:
        """PR の branch に commit を 1 つ足して bare remote に push する。"""
        (self.work / "change.txt").write_text(message + "\n", encoding="utf-8")
        git(self.work, "add", "change.txt")
        git(self.work, "commit", "-qm", message)
        git(self.work, "push", "-q", str(self.remote), f"HEAD:refs/heads/{BRANCH}")
        return git(self.work, "rev-parse", "HEAD")

    def base(self) -> str:
        return git(self.remote, "rev-parse", "refs/heads/main")

    def place_review(
        self, head: str, model: str = "alpha", *, commit: bool = False, findings: int = 0
    ) -> tuple[Path, Path]:
        return write_pair(self.review_dir, model, head, self.base(), commit=commit, findings=findings)

    @property
    def template(self) -> Path:
        return self.root / "agent-runner.toml"

    def reviewers(self, behaviors: dict[str, str | list[str]], *, findings: int = 0) -> ReviewerAdapter:
        """現在の HEAD を対象にした偽のレビュアー（agent_id → success・failed・crash、または起動ごとの列）。"""
        head = self.gh.head()
        agents: dict[str, Any] = {}
        for agent, behavior in behaviors.items():
            model = f"{agent}-model"
            review = self.review_dir / f"{model}.md"
            agents[agent] = {
                "behavior": behavior,
                "review_file": str(review),
                "findings_file": str(self.review_dir / "findings" / f"{model}-{head[:12]}.json"),
                "document": candidates_document(head, self.base(), model, review.name, findings=findings),
                "markdown": review_markdown(head, model, findings=findings),
            }
        scenario = self.root / f"scenario-{head[:12]}.json"
        scenario.write_text(
            json.dumps(
                {"repository": REPOSITORY, "number": NUMBER, "copies": str(self.root / "copies"), "agents": agents},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return ReviewerAdapter(scenario)

    def start(self, **options: Any) -> dict[str, Any]:
        result: dict[str, Any] = LOOP.start(
            f"#{NUMBER}",
            controller_model=PLANNER_MODEL,
            controller_runtime="claude-code",
            github=self.github,
            parent_root=self.parent,
            tool_root=self.parent,
            **{"template": self.template, **options},
        )
        self.run_dirs.append(Path(result["run_dir"]))
        return result


def seat(head: str, name: str = "alpha", *, rerun: bool = True) -> dict[str, Any]:
    return {
        "review_file": name,
        "model": name,
        "runtime": "pi",
        "file_head": head[:12],
        "mode": "Initial",
        "rerun": rerun,
        "merge_recommendation": "Request changes",
    }


def decision(ref: str, state: str, change: str, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": ref,
        "sources": [ref],
        "summary": f"{ref} の判断",
        "locations": ["change.txt:1"],
        "state": state,
        "change": change,
        "severity": "High" if state in {"fix", "resolved"} else "Medium",
        "pr_relation": "introduced",
        "reason": "H: 現行で成立する" if state == "fix" else "テストの判断",
    }
    if state == "skip":
        value["revisit_when"] = "利用経路が変わったとき"
    value.update(extra)
    return value


def round_record(
    round_: int,
    head: str,
    outcome: str,
    decisions: list[dict[str, Any]],
    *,
    commit: str | None = None,
    stop_reason: str | None = None,
    seats: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": "review-records/v1",
        "pr": {"repository": REPOSITORY, "number": NUMBER},
        "round": round_,
        "reviewed_head": head,
        "recorded_at": "2026-10-08T15:00:00Z",
        "planner": {
            "model": PLANNER_MODEL,
            "runtime": "claude-code",
            "agent_id": RECORDS.agent_id_for(PLANNER_MODEL),
        },
        "seats": seats if seats is not None else [seat(head)],
        "decisions": decisions,
        "outcome": outcome,
    }
    if commit is not None:
        record["commit"] = commit
    if stop_reason is not None:
        record["stop_reason"] = stop_reason
    RECORDS.validate_record(record)
    return record


def finalize_result(path: Path, outcome: str, *, merged: bool, stop_reason: str | None = None) -> Path:
    value: dict[str, Any] = {"outcome": outcome, "merged": merged, "issues": []}
    if stop_reason is not None:
        value["stop_reason"] = stop_reason
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


# 偽のレビュアー。runner が起動する実行ファイルで、scenario の指示どおりに pr-review の complete と同じ形の
# payload と成果 2 件（または失敗の payload）を書く。依頼文の JSON ブロックから records_comments を読み、写しを残す。
REVIEWER_PROGRAM = r"""
import json, pathlib, re, shutil, sys
request = json.load(sys.stdin)
receipt_path, scenario_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
agent = request["agent_id"]
block = re.search(r"```json\n(.*?)\n```", request["prompt"], re.DOTALL)
parameters = json.loads(block.group(1))
copies = pathlib.Path(scenario["copies"])
copies.mkdir(exist_ok=True)
shutil.copyfile(parameters["records_comments"], copies / f"{agent}.json")
spec = scenario["agents"][agent]
behavior = spec["behavior"]
if isinstance(behavior, list):
    # 起動ごとに次の振る舞い（最後の値を使い続ける）。再実行の 1 回目と 2 回目を分ける
    counter = copies / f"{agent}.launches"
    launches = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(launches + 1))
    behavior = behavior[min(launches, len(behavior) - 1)]
if behavior == "crash":
    sys.exit(3)  # receipt を書かずに異常終了する（runner の失敗）
payload = {key: request[key] for key in ("run_id", "request_id", "agent_id", "role")}
payload.update(schema_version=1, repository=scenario["repository"], pr_number=scenario["number"],
               pr_head=parameters["pr_head"], reviewed_head=parameters["pr_head"])
if behavior == "success":
    review = pathlib.Path(spec["review_file"])
    candidates = pathlib.Path(spec["findings_file"])
    candidates.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(spec["markdown"], encoding="utf-8")
    candidates.write_text(json.dumps(spec["document"], ensure_ascii=False), encoding="utf-8")
    payload.update(status="success", review_file=str(review), findings_file=str(candidates),
                   active_findings=len(spec["document"]["findings"]), cleanup="success", cleanup_details={},
                   retained_paths=None, reason=None, reviewer=spec["document"]["reviewer"],
                   timing=spec["document"]["timing"], save_seconds=0.1, cleanup_seconds=0.1, total_seconds=60.2)
else:
    payload.update(status="failed", review_file=None, findings_file=None, active_findings=None,
                   cleanup="not_attempted", cleanup_details={}, retained_paths=None,
                   reason="テスト: 依頼の途中で止まった", reviewer=None, timing=None,
                   save_seconds=None, cleanup_seconds=None, total_seconds=None)
receipt = {key: request[key] for key in ("schema_version", "run_id", "request_id", "agent_id", "role")}
receipt.update(status="success", payload=payload)
receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
"""


class ReviewerAdapter:
    """tools.agent_runner の Adapter。REVIEWER_PROGRAM を起動する。"""

    def __init__(self, scenario: Path) -> None:
        from tools.agent_runner.adapters import Capabilities

        self.capabilities = Capabilities()
        self.scenario = scenario
        self.launched: list[str] = []

    def __call__(self, runtime: str) -> ReviewerAdapter:
        return self

    def prepare(self, context: Any) -> Any:
        from tools.agent_runner.adapters import LaunchSpec

        self.launched.append(context.request.agent_id)
        return LaunchSpec(
            argv=(sys.executable, "-c", REVIEWER_PROGRAM, str(context.receipt_path), str(self.scenario)),
            stdin=context.request.model_dump_json().encode(),
        )

    def collect(self, context: Any, stdout_path: Path, stderr_path: Path) -> Any:
        from tools.agent_runner.adapters import AdapterOutcome
        from tools.agent_runner.models import Identity, Receipt

        return AdapterOutcome(
            Receipt.model_validate_json(context.receipt_path.read_bytes()),
            Identity(model=context.agent.model, session_id=f"fake-{context.request.agent_id}"),
        )


TEST_TEMPLATE = """
schema_version = 1

[agents.alpha]
runtime = "pi"
command = ["fake-alpha"]
model = "alpha-model"

[agents.beta]
runtime = "claude-code"
command = ["fake-beta"]
model = "beta-model"

[roles.inspect]
writes = false
deadline_seconds = 60
result_shape = "json"

[roles.implement]
writes = true
deadline_seconds = 60
result_shape = "json"

[roles.self-review]
writes = false
deadline_seconds = 60
result_shape = "json"

[roles.fix-check]
writes = false
deadline_seconds = 60
result_shape = "json"

[workflows.review.assignments.inspect]
agents = ["alpha", "beta"]

[workflows.fix.assignments.implement]
agents = ["alpha"]

[workflows.fix.assignments.self-review]
agents = ["self"]

[workflows.fix.assignments.fix-check]
agents = ["self"]
"""


@dataclass(frozen=True)
class Kit:
    """テストから使う補助（importlib モードでは conftest を名前で import できないので fixture で渡す）。"""

    repository: str = REPOSITORY
    number: int = NUMBER
    branch: str = BRANCH
    pr_url: str = PR_URL
    records: Any = RECORDS

    @staticmethod
    def git(root: Path, *args: str) -> str:
        return git(root, *args)

    @staticmethod
    def seat(head: str, name: str = "alpha", *, rerun: bool = True) -> dict[str, Any]:
        return seat(head, name, rerun=rerun)

    @staticmethod
    def decision(ref: str, state: str, change: str, **extra: Any) -> dict[str, Any]:
        return decision(ref, state, change, **extra)

    @staticmethod
    def round_record(
        round_: int, head: str, outcome: str, decisions: list[dict[str, Any]], **extra: Any
    ) -> dict[str, Any]:
        return round_record(round_, head, outcome, decisions, **extra)

    @staticmethod
    def write_pair(review_dir: Path, model: str, head: str, base: str, **extra: Any) -> tuple[Path, Path]:
        return write_pair(review_dir, model, head, base, **extra)

    @staticmethod
    def finalize_result(path: Path, outcome: str, *, merged: bool, stop_reason: str | None = None) -> Path:
        return finalize_result(path, outcome, merged=merged, stop_reason=stop_reason)


@pytest.fixture
def kit() -> Kit:
    return Kit()


@pytest.fixture
def loop() -> Any:
    return LOOP


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LoopEnv:
    # 利用者の個人設定（~/.config/agent-runner/config.toml）を読ませない。存在しないパスは「個人設定なし」になる
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(tmp_path / "no-personal-config.toml"))
    parent = tmp_path / "parent"
    init_repo(parent)
    git(parent, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    (parent / ".gitignore").write_text("/reviews/\n", encoding="utf-8")
    (parent / "README.md").write_text("tool root\n", encoding="utf-8")
    git(parent, "add", ".gitignore", "README.md")
    git(parent, "commit", "-qm", "tool root")
    remote = tmp_path / "pr.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "pr-work"
    init_repo(work)
    (work / "change.txt").write_text("base\n", encoding="utf-8")
    git(work, "add", "change.txt")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", str(remote), "HEAD:refs/heads/main")
    git(work, "switch", "-q", "-c", "feature")
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    gh = FakeGitHub(remote)
    environment = LoopEnv(
        root=tmp_path,
        parent=parent,
        remote=remote,
        work=work,
        tmp_root=tmp_root,
        github=LOOP.GitHub(gh.runner),
        gh=gh,
    )
    environment.push("PR の変更")
    (tmp_path / "agent-runner.toml").write_text(TEST_TEMPLATE, encoding="utf-8")
    return environment
