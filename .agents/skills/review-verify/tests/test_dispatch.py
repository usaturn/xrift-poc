"""fix-check だけを起動し、self・外部の失敗を取り違えない。"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import Config, ConfigError
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.models import Identity

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/dispatch.py"


@pytest.fixture
def dispatch() -> Any:
    spec = importlib.util.spec_from_file_location("review_verify_dispatch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verification_input(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "x").write_text("one\n")
    subprocess.run(["git", "-C", str(root), "add", "x"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    verified = tmp_path / "verified.json"
    verified.write_text('{"kind": "verified", "findings": []}')
    return {
        "repository": str(root),
        "from": head,
        "to": head,
        "fix_ids": ["F1"],
        "fix_sections": "F1: Keep the original contents; verify by reading committed x.",
        "verified_file": str(verified),
        "review_dir": str(tmp_path / "review"),
    }


def config(agent: str = "self", deadline_seconds: int = 30) -> Config:
    return Config.model_validate(
        {
            "agents": {"probe": {"runtime": "pi", "command": [sys.executable], "model": "requested-not-actual"}},
            "roles": {
                "fix-check": {"writes": False, "deadline_seconds": deadline_seconds, "result_shape": "json"},
                "implement": {"writes": True, "deadline_seconds": 30, "result_shape": "json"},
            },
            "workflows": {
                "fix": {
                    "assignments": {
                        "fix-check": {"agents": [agent]},
                        "implement": {"agents": ["probe"]},
                    }
                }
            },
        }
    )


class ReceiptAdapter:
    capabilities = Capabilities()

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        code = (
            "import json,pathlib,sys; r=json.load(sys.stdin); "
            "receipt={k:r[k] for k in ('schema_version','run_id','request_id','agent_id','role')}; "
            "receipt.update(status='success',payload={'checked':r['role']}); "
            f"pathlib.Path(sys.argv[1]).write_text(json.dumps(receipt)); sys.exit({self.exit_code})"
        )
        return LaunchSpec(
            (sys.executable, "-c", code, str(context.receipt_path)), context.request.model_dump_json().encode()
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        return AdapterOutcome(identity=Identity(model="observed-model", provider="observed-provider"))


def test_self_returns_complete_input_without_launching_implementation(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    def no_launch(runtime: str) -> Any:
        raise AssertionError("self must not load adapters")

    result = dispatch.dispatch(config(), verification_input, tmp_path / "run", adapter_loader=no_launch)
    assert result["batch"]["state"] == "self"
    assert result["batch"]["assignments"] == {"fix-check": ["self"]}
    assert result["request"]["fix_sections"] == verification_input["fix_sections"]
    assert result["verified"]["findings"] == []
    assert result["batch"]["results"][0]["attempts"] == []
    assert result["verifier"] is None


@pytest.mark.parametrize("code,state", [(0, "success"), (1, "failed")])
def test_external_runs_only_fix_check_and_preserves_observed_identity(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, code: int, state: str
) -> None:
    def adapter_loader(runtime: str) -> ReceiptAdapter:
        return ReceiptAdapter(code)

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["batch"]["state"] == state
    assert result["batch"]["assignments"] == {"fix-check": ["probe"]}
    attempt = result["batch"]["results"][0]["attempts"][0]
    assert attempt["identity"]["model"] == "observed-model"
    assert attempt["stop_confirmed"] is True
    assert attempt["exit_code"] == code
    assert attempt["payload"]["checked"] == "fix-check"
    assert Path(result["batch"]["artifact_dir"], "dispatch.json").is_file()
    if state == "success":
        assert result["verifier"]["model"] == "observed-model"
        assert result["verifier"]["provenance"]["role"] == "fix_check"
        assert result["verifier"]["provenance"]["cli_version"] == "unknown"
        assert result["verifier"]["provenance"]["request_id"] == result["batch"]["results"][0]["request_id"]
    else:
        assert result["verifier"] is None


@pytest.mark.parametrize("mutation", ["multi", "writes", "text"])
def test_invalid_fix_check_assignment_is_rejected_before_launch(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, mutation: str
) -> None:
    cfg = config("probe")
    if mutation == "multi":
        cfg.workflows["fix"].assignments["fix-check"].agents.append("self")
    elif mutation == "writes":
        cfg.roles["fix-check"].writes = True
    else:
        cfg.roles["fix-check"].result_shape = "text"
    with pytest.raises(ConfigError):
        dispatch.dispatch(cfg, verification_input, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_prefixed_named_skill_digest_is_not_recorded_as_a_fake_path(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    class NamedSkillsAdapter(ReceiptAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            return AdapterOutcome(identity=Identity(skill_versions={"not-a-file": "sha256:" + "a" * 64}))

    def adapter_loader(runtime: str) -> NamedSkillsAdapter:
        return NamedSkillsAdapter()

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["verifier"]["provenance"]["skills"][0] == {"path": "unknown", "sha256": "a" * 64}
    assert result["batch"]["results"][0]["attempts"][0]["identity"]["skill_versions"] == {
        "not-a-file": "sha256:" + "a" * 64
    }


def test_multiple_unlocated_skills_keep_raw_observations_without_duplicate_paths(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    versions = {"first": "sha256:" + "a" * 64, "second": "sha256:" + "b" * 64}

    class NamedSkillsAdapter(ReceiptAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            return AdapterOutcome(identity=Identity(skill_versions=versions))

    def adapter_loader(runtime: str) -> NamedSkillsAdapter:
        return NamedSkillsAdapter()

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["verifier"]["provenance"]["skills"][0] == {"path": "unknown", "sha256": "unknown"}
    assert result["batch"]["results"][0]["attempts"][0]["identity"]["skill_versions"] == versions


def test_unobserved_identity_is_unknown_not_requested(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    class UnobservedAdapter(ReceiptAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            return AdapterOutcome(identity=Identity())

    def adapter_loader(runtime: str) -> UnobservedAdapter:
        return UnobservedAdapter()

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["verifier"]["model"] == "unknown"
    assert result["verifier"]["provenance"]["provider"] == "unknown"
    assert result["verifier"]["provenance"]["skills"] == [result["skill"]]


@pytest.mark.parametrize(
    "field,bad", [("to", "HEAD"), ("from", "0" * 40), ("fix_sections", ""), ("fix_ids", []), ("extra", True)]
)
def test_incomplete_or_unresolvable_request_is_rejected_before_output(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, field: str, bad: object
) -> None:
    verification_input[field] = bad
    with pytest.raises(ValueError):
        dispatch.dispatch(config(), verification_input, tmp_path / "run")
    assert not (tmp_path / "run").exists()


STARTED = datetime(2026, 10, 8, 1, 2, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    "deadline,stop_new_checks_at,deadline_at,reserve",
    [
        (1800, "2026-10-08T01:27:03Z", "2026-10-08T01:32:03Z", 300),
        (3600, "2026-10-08T01:27:03Z", "2026-10-08T01:32:03Z", 300),
        (600, "2026-10-08T01:07:03Z", "2026-10-08T01:12:03Z", 300),
        (400, "2026-10-08T01:05:23Z", "2026-10-08T01:08:43Z", 200),
    ],
)
def test_time_limits_follow_the_projected_deadline(
    dispatch: Any,
    verification_input: dict[str, Any],
    tmp_path: Path,
    deadline: int,
    stop_new_checks_at: str,
    deadline_at: str,
    reserve: int,
) -> None:
    result = dispatch.dispatch(
        config(deadline_seconds=deadline), verification_input, tmp_path / "run", clock=lambda: STARTED
    )
    projected = min(deadline, 1800)
    assert result["time_limits"] == {
        "started_at": "2026-10-08T01:02:03Z",
        "stop_new_checks_at": stop_new_checks_at,
        "deadline_at": deadline_at,
        "deadline_seconds": projected,
        "reserve_seconds": reserve,
    }
    saved = json.loads(Path(result["batch"]["artifact_dir"], "dispatch.json").read_text(encoding="utf-8"))
    assert saved["time_limits"] == result["time_limits"]


def test_self_prompt_embeds_the_same_time_limits(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    result = dispatch.dispatch(config(), verification_input, tmp_path / "run", clock=lambda: STARTED)
    head, _, _ = result["prompt"].partition("\n\nApplied skill:\n")
    embedded = json.loads(head[head.index("{") :])
    assert embedded["time_limits"] == result["time_limits"]


def test_dispatcher_skill_digest_is_the_hash_of_the_file_bytes(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    result = dispatch.dispatch(config(), verification_input, tmp_path / "run")
    assert result["skill"]["sha256"] == hashlib.sha256(dispatch.SKILL.read_bytes()).hexdigest()


def test_observed_hash_conflicting_with_the_dispatched_skill_is_unknown(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    path = ".agents/skills/review-verify/SKILL.md"

    class ConflictingAdapter(ReceiptAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            return AdapterOutcome(identity=Identity(skill_versions={path: "sha256:" + "c" * 64}))

    def adapter_loader(runtime: str) -> ConflictingAdapter:
        return ConflictingAdapter()

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["verifier"]["provenance"]["skills"] == [{"path": path, "sha256": "unknown"}]


def test_observed_hash_matching_the_dispatched_skill_is_kept(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    path = ".agents/skills/review-verify/SKILL.md"
    digest = hashlib.sha256(dispatch.SKILL.read_bytes()).hexdigest()

    class MatchingAdapter(ReceiptAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            return AdapterOutcome(identity=Identity(skill_versions={path: "sha256:" + digest}))

    def adapter_loader(runtime: str) -> MatchingAdapter:
        return MatchingAdapter()

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["verifier"]["provenance"]["skills"] == [{"path": path, "sha256": digest}]


# --- 外部担当の結果を受け取る accept と、失敗の分類 -----------------------------------------------------------------

FINDINGS_TESTS = Path(__file__).resolve().parents[3] / "scripts/tests/test_review_findings.py"


@pytest.fixture(scope="module")
def findings_helpers() -> Any:
    """有効な v2 fix_check の文書を組み立てる既存テストの helper を流用する。"""
    spec = importlib.util.spec_from_file_location("review_findings_test_helpers", FINDINGS_TESTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(root: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


@dataclass
class Scenario:
    root: Path
    review_dir: Path
    base: str
    fix: str
    request: dict[str, Any]
    inventory: dict[str, Any]
    document: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"verified": copy.deepcopy(self.document), "inventory": copy.deepcopy(self.inventory)}


@pytest.fixture
def scenario(tmp_path: Path, findings_helpers: Any) -> Scenario:
    """道具が候補を 1 件出す、実際の Git repository と、それを判定し終えた有効な v2 fix_check。"""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    base = commit_all(root, {"tool.py": "a = 1\n", "README.md": "LIMIT_VALUE を守る\n"}, "base")
    fix = commit_all(root, {"tool.py": "a = LIMIT_VALUE\n"}, "fix")
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    verified = tmp_path / "verified.json"
    verified.write_text('{"kind": "verified", "findings": []}')
    h = findings_helpers
    fix_id = f"claude-opus-5-5#H-1@{base[:12]}"
    claims = {
        "fix_ids": [fix_id],
        "claims": [
            {"id": "C-1", "fix_id": fix_id, "claim": "LIMIT_VALUE を使う", "source": "Spec Fix F1", "kind": "static"}
        ],
    }
    inventory = h.review_findings.statement_candidates(
        root, base, fix, generated_at="2026-10-08T00:00:00Z", claims=claims
    )
    assert [c["path"] for c in inventory["candidates"]] == ["README.md:1"]

    document = h.document_v2("verified")
    shape = h.fix_check()
    for key in ("mode", "reviewed_head", "fix_check", "findings", "statement_candidates"):
        document[key] = shape[key]
    # 既存 helper の固定値を、実際の repository の値へ置き換える。
    text = json.dumps(document)
    for old, new in ((h.HEAD, base), (h.FIX, fix)):
        text = text.replace(old, new)
    document = json.loads(text)
    document["fix_check"]["fix_ids"] = [fix_id]
    document["statement_candidates"] = {
        "file": f"{fix[:12]}.statement-candidates.json",
        "judgments": [{"ids": ["S-1"], "result": "unrelated", "basis": "別の説明"}],
    }
    document["findings"][0]["statement_sites"] = []
    check = {
        "command": "git show HEAD:tool.py",
        "head": fix,
        "mode": "executed",
        "exit_code": 0,
        "result": "LIMIT_VALUE を使う",
        "artifact": "tool.py",
    }
    document["fix_evidence"] = {
        "claims": [{"id": "C-1", "result": "holds", "basis": "読んだ", "checks": [check], "findings": []}],
        "files": [
            {
                "path": "tool.py",
                "result": "passed",
                "rule": "AGENTS.md: lint",
                "basis": "lint 実行",
                "checks": [check],
                "findings": [],
            }
        ],
    }
    h.review_findings.validate_document(document)
    h.review_findings.check_statement_judgments(document, inventory)
    request = {
        "repository": str(root),
        "from": base,
        "to": fix,
        "fix_ids": [fix_id],
        "fix_sections": "F1: LIMIT_VALUE を使う。",
        "verified_file": str(verified),
        "review_dir": str(review_dir),
    }
    return Scenario(root, review_dir, base, fix, request, inventory, document)


class PayloadAdapter(ReceiptAdapter):
    def __init__(self, payload: Any, exit_code: int = 0, touch: Path | None = None) -> None:
        super().__init__(exit_code)
        self.payload = payload
        self.touch = touch

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        code = (
            "import json,pathlib,sys; r=json.load(sys.stdin); "
            "receipt={k:r[k] for k in ('schema_version','run_id','request_id','agent_id','role')}; "
            f"receipt.update(status='success',payload=json.loads({json.dumps(json.dumps(self.payload))})); "
            f"touch={str(self.touch)!r}; touch and pathlib.Path(touch).write_text('x'); "
            f"pathlib.Path(sys.argv[1]).write_text(json.dumps(receipt)); sys.exit({self.exit_code})"
        )
        return LaunchSpec(
            (sys.executable, "-c", code, str(context.receipt_path)), context.request.model_dump_json().encode()
        )


def run_external(dispatch: Any, scenario: Scenario, tmp_path: Path, payload: Any, **adapter_args: Any) -> Path:
    """偽の adapter で実際に dispatch し、dispatch.json のある保存先を返す。"""
    if isinstance(payload.get("verified"), dict):
        # 担当は渡された skill をそのまま一覧に書く。
        digest = hashlib.sha256(dispatch.SKILL.read_bytes()).hexdigest()
        payload["verified"]["skill"] = {"path": ".agents/skills/review-verify/SKILL.md", "sha256": digest}
    result = dispatch.dispatch(
        config("probe"),
        scenario.request,
        tmp_path / "run",
        adapter_loader=loader_for(PayloadAdapter(payload, **adapter_args)),
    )
    return Path(result["batch"]["artifact_dir"])


def loader_for(adapter: ReceiptAdapter) -> Callable[[str], ReceiptAdapter]:
    def load(runtime: str) -> ReceiptAdapter:
        return adapter

    return load


def written(review_dir: Path) -> list[str]:
    return sorted(str(path.relative_to(review_dir)) for path in review_dir.rglob("*") if path.is_file())


def accept(dispatch: Any, run_dir: Path) -> int:
    return int(dispatch.main(["accept", "--run-dir", str(run_dir)]))


def test_accept_saves_the_tool_output_unchanged_with_the_observed_verifier(
    dispatch: Any, scenario: Scenario, tmp_path: Path, findings_helpers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    capsys.readouterr()
    assert accept(dispatch, run_dir) == 0
    out = json.loads(capsys.readouterr().out)
    stem = scenario.fix[:12]
    verified_dir = scenario.review_dir / "verified"
    assert written(scenario.review_dir) == [
        f"verified/{stem}.fix-check.json",
        f"verified/{stem}.statement-candidates.json",
    ]
    assert sorted(out.values()) == sorted(str(path) for path in verified_dir.iterdir())
    saved = json.loads((verified_dir / f"{stem}.fix-check.json").read_text(encoding="utf-8"))
    observed = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))["verifier"]
    assert saved["verifier"] == observed
    assert saved["verifier"] != scenario.document["verifier"]
    assert saved["findings"] == scenario.document["findings"]
    sidecar = json.loads((verified_dir / f"{stem}.statement-candidates.json").read_text(encoding="utf-8"))
    assert sidecar == scenario.inventory
    assert findings_helpers.review_findings.main(["validate", str(verified_dir / f"{stem}.fix-check.json")]) == 0
    assert not [path for path in verified_dir.iterdir() if path.name.startswith(".")]


def drop_changed_file(payload: dict[str, Any]) -> None:
    payload["inventory"]["changed_files"].pop()


def rewrite_tool_candidate(payload: dict[str, Any]) -> None:
    payload["inventory"]["candidates"][0]["text"] = "別の本文"


def drop_tool_candidate(payload: dict[str, Any]) -> None:
    payload["inventory"]["candidates"].clear()
    payload["verified"]["statement_candidates"]["judgments"].clear()


def drop_tool_term(payload: dict[str, Any]) -> None:
    payload["inventory"]["terms"].clear()


def drop_claim(payload: dict[str, Any]) -> None:
    payload["inventory"]["claims"].pop()


def different_fix_ids(payload: dict[str, Any]) -> None:
    payload["inventory"]["fix_ids"].append("other#H-1@abcdef012345")
    payload["verified"]["fix_check"]["fix_ids"].append("other#H-1@abcdef012345")


def missing_key(payload: dict[str, Any]) -> None:
    del payload["inventory"]


def extra_key(payload: dict[str, Any]) -> None:
    payload["notes"] = "余分"


def unjudged_candidate(payload: dict[str, Any]) -> None:
    payload["verified"]["statement_candidates"]["judgments"].clear()


@pytest.mark.parametrize(
    "mutation",
    [
        drop_changed_file,
        rewrite_tool_candidate,
        drop_tool_candidate,
        drop_tool_term,
        drop_claim,
        different_fix_ids,
        missing_key,
        extra_key,
        unjudged_candidate,
    ],
)
def test_accept_rejects_a_payload_that_differs_from_the_tool_output(
    dispatch: Any,
    scenario: Scenario,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = scenario.payload()
    mutation(payload)
    run_dir = run_external(dispatch, scenario, tmp_path, payload)
    capsys.readouterr()
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_accept_allows_candidates_and_terms_the_verifier_appended(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    payload = scenario.payload()
    inventory = payload["inventory"]
    inventory["terms"].append({"term": "EXTRA_TERM", "origins": ["docs/extra.rst:1"]})
    inventory["candidates"].append(
        {"id": "S-2", "path": "docs/extra.rst:1", "terms": ["EXTRA_TERM"], "text": "EXTRA_TERM"}
    )
    payload["verified"]["statement_candidates"]["judgments"].append(
        {"ids": ["S-2"], "result": "unrelated", "basis": "同名の別物"}
    )
    run_dir = run_external(dispatch, scenario, tmp_path, payload)
    assert accept(dispatch, run_dir) == 0
    sidecar = scenario.review_dir / "verified" / f"{scenario.fix[:12]}.statement-candidates.json"
    assert [c["id"] for c in json.loads(sidecar.read_text(encoding="utf-8"))["candidates"]] == ["S-1", "S-2"]


def test_accept_rejects_a_verifier_candidate_inserted_before_the_tool_candidates(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    payload = scenario.payload()
    inventory = payload["inventory"]
    inventory["candidates"].insert(
        0, {"id": "S-1", "path": "docs/extra.rst:1", "terms": ["LIMIT_VALUE"], "text": "先頭に足した候補"}
    )
    inventory["candidates"][1]["id"] = "S-2"
    run_dir = run_external(dispatch, scenario, tmp_path, payload)
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_accept_rejects_outside_changes_and_dispatch_exits_1_for_them(
    dispatch: Any, scenario: Scenario, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 開始前から登録されていた checkout（呼び出し元の repository）へ外部担当が書き込む。
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload(), touch=scenario.root / "stray")
    saved = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    assert saved["batch"]["state"] == "success"
    assert saved["batch"]["outside_changes"]
    capsys.readouterr()
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_dispatch_main_exits_1_and_names_outside_changes(
    dispatch: Any, scenario: Scenario, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = main_argv(tmp_path, scenario.request, "probe")
    code = dispatch.main(
        argv, adapter_loader=loader_for(PayloadAdapter(scenario.payload(), touch=scenario.root / "stray"))
    )
    line = json.loads(capsys.readouterr().out)
    assert code == 1
    assert line["state"] == "success"
    assert line["outside_changes"]


def test_accept_rejects_an_unconfirmed_stop(dispatch: Any, scenario: Scenario, tmp_path: Path) -> None:
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    saved = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    saved["batch"]["results"][0]["attempts"][-1]["stop_confirmed"] = False
    (run_dir / "dispatch.json").write_text(json.dumps(saved), encoding="utf-8")
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_accept_rejects_a_failed_run(dispatch: Any, scenario: Scenario, tmp_path: Path) -> None:
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload(), exit_code=1)
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_accept_rejects_self(dispatch: Any, scenario: Scenario, tmp_path: Path) -> None:
    result = dispatch.dispatch(config("self"), scenario.request, tmp_path / "run")
    assert accept(dispatch, Path(result["batch"]["artifact_dir"])) == 1
    assert written(scenario.review_dir) == []


def test_accept_exits_2_when_the_review_directory_is_missing(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    scenario.review_dir.rmdir()
    assert accept(dispatch, run_dir) == 2
    assert not scenario.review_dir.exists()


def test_accept_exits_2_without_dispatch_json(dispatch: Any, tmp_path: Path) -> None:
    assert accept(dispatch, tmp_path) == 2


def toml_config(path: Path) -> Path:
    path.write_text(
        """
[agents.probe]
runtime = "pi"
command = ["python"]
model = "requested-not-actual"

[roles.fix-check]
writes = false
deadline_seconds = 30
result_shape = "json"

[roles.implement]
writes = true
deadline_seconds = 30
result_shape = "json"

[workflows.fix.assignments.fix-check]
agents = ["self"]

[workflows.fix.assignments.implement]
agents = ["probe"]
""",
        encoding="utf-8",
    )
    return path


def main_argv(tmp_path: Path, request: dict[str, Any], assign: str | None = None) -> list[str]:
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    personal = tmp_path / "personal.toml"
    personal.write_text("", encoding="utf-8")
    argv = [
        "--template",
        str(toml_config(tmp_path / "template.toml")),
        "--config",
        str(personal),
        "--request",
        str(request_file),
        "--output-dir",
        str(tmp_path / "main-run"),
    ]
    return argv + ["--assign", assign] if assign else argv


def test_main_exits_0_for_self(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dispatch.main(main_argv(tmp_path, verification_input)) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "self"


def test_main_exits_1_for_a_failed_run(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = dispatch.main(
        main_argv(tmp_path, verification_input, "probe"), adapter_loader=loader_for(ReceiptAdapter(1))
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["state"] == "failed"


def test_main_names_the_invalid_request_field_without_its_value(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    verification_input["to"] = "HEAD-secret-value"
    assert dispatch.main(main_argv(tmp_path, verification_input)) == 2
    err = capsys.readouterr().err
    assert "to" in err
    assert "secret-value" not in err


def test_main_names_a_config_error_category(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dispatch.main(main_argv(tmp_path, verification_input, "no-such-agent")) == 2
    assert "invalid_reference" in capsys.readouterr().err


def test_main_names_an_unresolvable_revision(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    verification_input["from"] = "0" * 40
    assert dispatch.main(main_argv(tmp_path, verification_input)) == 2
    assert "repository/from/to cannot be resolved" in capsys.readouterr().err


@pytest.mark.parametrize("location", ["ignored", "outside"])
@pytest.mark.parametrize("mutation", ["create", "overwrite", "delete", "rename", "chmod", "symlink"])
@pytest.mark.parametrize("valid_payload", [True, False])
def test_review_directory_changes_are_rejected_before_publishing(
    dispatch: Any,
    scenario: Scenario,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    location: str,
    mutation: str,
    valid_payload: bool,
) -> None:
    if location == "ignored":
        scenario.review_dir = scenario.root / "reviews/probe"
        scenario.review_dir.mkdir(parents=True)
        (scenario.root / ".git/info/exclude").write_text("/reviews/\n")
        scenario.request["review_dir"] = str(scenario.review_dir)
    target = scenario.review_dir / "previous\nresult.json"
    if mutation != "create":
        target.write_text("previous accepted contents")
    payload = scenario.payload() if valid_payload else {"invalid": True}
    actions = {
        "create": "p.write_text('unvalidated contents')",
        "overwrite": "p.write_text('unvalidated contents')",
        "delete": "p.unlink()",
        "rename": "p.rename(p.with_name('moved.json'))",
        "chmod": "p.chmod(0o700)",
        "symlink": "p.unlink(); p.symlink_to('missing')",
    }

    class MutatingAdapter(PayloadAdapter):
        def prepare(self, context: AdapterContext) -> LaunchSpec:
            spec = super().prepare(context)
            argv = list(spec.argv)
            argv[2] = argv[2].replace("receipt={", f"p=pathlib.Path({str(target)!r}); {actions[mutation]}; receipt={{")
            return LaunchSpec(tuple(argv), spec.stdin, spec.env)

    code = dispatch.main(
        main_argv(tmp_path, scenario.request, "probe"), adapter_loader=loader_for(MutatingAdapter(payload))
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["state"] == "success"
    assert output["outside_changes"] == []
    assert "previous\nresult.json" in output["review_changes"]
    run_dir = Path(output["artifact_dir"])
    assert accept(dispatch, run_dir) == 1
    error = capsys.readouterr().err
    assert len(error.strip().splitlines()) == 1
    assert "previous\\nresult.json" in error
    assert not (scenario.review_dir / "verified").exists()
    # 観測だけで書込み元を断定しない。他セッションの更新を自動削除・復元しない。
    if mutation in {"create", "overwrite"}:
        assert target.read_text() == "unvalidated contents"


def test_accept_checks_changes_since_dispatch_even_with_an_invalid_payload(
    dispatch: Any, scenario: Scenario, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = run_external(dispatch, scenario, tmp_path, {"invalid": True})
    path = scenario.review_dir / "late.json"
    path.write_text("late change")
    assert accept(dispatch, run_dir) == 1
    assert "late.json" in capsys.readouterr().err
    assert not (scenario.review_dir / "verified").exists()
    assert path.read_text() == "late change"


def test_unchanged_existing_results_and_temporary_probe_outputs_are_allowed(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    previous = scenario.review_dir / "verified/previous.json"
    previous.parent.mkdir()
    previous.write_text("previous accepted contents")
    probe = tmp_path / "generated.txt"
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload(), touch=probe)
    saved = json.loads((run_dir / "dispatch.json").read_text())
    assert saved["review_changes"] == []
    assert accept(dispatch, run_dir) == 0
    assert previous.read_text() == "previous accepted contents"
    assert probe.read_text() == "x"


@pytest.mark.parametrize("entry", ["review_dir", "verified"])
def test_symlink_publication_directories_are_rejected_without_writing_referents(
    dispatch: Any, scenario: Scenario, tmp_path: Path, entry: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    if entry == "review_dir":
        scenario.review_dir.rmdir()
        scenario.review_dir.symlink_to(outside, target_is_directory=True)
    else:
        (scenario.review_dir / "verified").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        dispatch.dispatch(config("probe"), scenario.request, tmp_path / "run")
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "run").exists()


def test_accept_rejects_a_late_verified_directory_symlink(dispatch: Any, scenario: Scenario, tmp_path: Path) -> None:
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    outside = tmp_path / "outside"
    outside.mkdir()
    (scenario.review_dir / "verified").symlink_to(outside, target_is_directory=True)
    assert accept(dispatch, run_dir) == 1
    assert list(outside.iterdir()) == []


def test_dispatch_does_not_read_the_review_directory_without_stop_confirmation(
    dispatch: Any, scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = dispatch.snapshot_review
    workflow = dispatch.run_workflow
    calls = 0

    def count_snapshot(root: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return snapshot(root)

    def unconfirmed(*args: Any, **kwargs: Any) -> Any:
        batch = workflow(*args, **kwargs)
        batch.results[0].attempts[-1].stop_confirmed = False
        batch.state = "failed"
        return batch

    monkeypatch.setattr(dispatch, "snapshot_review", count_snapshot)
    monkeypatch.setattr(dispatch, "run_workflow", unconfirmed)
    result = dispatch.dispatch(
        config("probe"),
        scenario.request,
        tmp_path / "run",
        adapter_loader=loader_for(PayloadAdapter(scenario.payload())),
    )
    assert calls == 1
    assert result["review_changes"] is None
    assert accept(dispatch, Path(result["batch"]["artifact_dir"])) == 1
    assert calls == 1


def test_unavailable_review_audit_never_means_unchanged(
    dispatch: Any,
    scenario: Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = dispatch.snapshot_review
    calls = 0

    def unavailable_after_launch(root: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("private contents must not reach stderr")
        return snapshot(root)

    monkeypatch.setattr(dispatch, "snapshot_review", unavailable_after_launch)
    code = dispatch.main(main_argv(tmp_path, scenario.request, "probe"), adapter_loader=loader_for(ReceiptAdapter()))
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["review_changes"]
    assert accept(dispatch, Path(output["artifact_dir"])) == 1
    error = capsys.readouterr().err
    assert "private contents" not in error
    assert not (scenario.review_dir / "verified").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read directories regardless of their permissions")
def test_initial_snapshot_cannot_silently_skip_an_unreadable_subdirectory(dispatch: Any, scenario: Scenario) -> None:
    hidden = scenario.review_dir / "hidden"
    hidden.mkdir()
    (hidden / "previous.json").write_text("previous accepted contents")
    hidden.chmod(0)
    try:
        with pytest.raises(PermissionError):
            dispatch.snapshot_review(scenario.review_dir)
    finally:
        hidden.chmod(0o700)
