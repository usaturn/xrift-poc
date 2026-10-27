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
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import Config
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


def test_broken_personal_config_never_falls_back_to_template(
    dispatch: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """壊れた個人設定リンクを無視すると、個人設定と違う担当で修正確認が進む。"""
    personal = tmp_path / "個人 設定.toml"
    personal.symlink_to(tmp_path / "missing.toml")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(personal))
    request = tmp_path / "request.json"
    request.write_text("{}")

    def no_dispatch(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("壊れた個人設定のまま担当を起動しようとした")

    monkeypatch.setattr(dispatch, "dispatch", no_dispatch)
    assert dispatch.main(["--request", str(request), "--output-dir", str(tmp_path / "output")]) != 0


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
    """fix-check だけへの射影が外れると、確認のはずの dispatch が fix workflow の implement（writes=true）の担当まで
    起動してリポジトリを書き換え、self に担当の成功記録を作る。"""

    def no_launch(runtime: str) -> Any:
        raise AssertionError("self must not load adapters")

    result = dispatch.dispatch(config(), verification_input, tmp_path / "run", adapter_loader=no_launch)
    assert result["batch"]["assignments"] == {"fix-check": ["self"]}
    assert result["verifier"] is None


def test_unobserved_identity_is_unknown_not_requested(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path
) -> None:
    """観測できない担当の識別を設定で要求したモデルで埋めると、実際とは別のモデルが確認したことが verified と
    状況コメントに黙って残る。"""

    class UnobservedAdapter(ReceiptAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            return AdapterOutcome(identity=Identity())

    def adapter_loader(runtime: str) -> UnobservedAdapter:
        return UnobservedAdapter()

    result = dispatch.dispatch(config("probe"), verification_input, tmp_path / "run", adapter_loader=adapter_loader)
    assert result["verifier"]["model"] == "unknown"


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


def set_skill_digest(dispatch: Any, payload: dict[str, Any]) -> None:
    """担当は渡された skill をそのまま一覧に書く。"""
    if isinstance(payload.get("verified"), dict):
        digest = hashlib.sha256(dispatch.SKILL.read_bytes()).hexdigest()
        payload["verified"]["skill"] = {"path": ".agents/skills/review-verify/SKILL.md", "sha256": digest}


def run_external(dispatch: Any, scenario: Scenario, tmp_path: Path, payload: Any, **adapter_args: Any) -> Path:
    """偽の adapter で実際に dispatch し、dispatch.json のある保存先を返す。"""
    set_skill_digest(dispatch, payload)
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


def test_input_error_does_not_echo_request_secrets(
    dispatch: Any, verification_input: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """入力検証エラーがrequestの値を表示すると、自由記述や秘密がstderrへ漏れる。"""
    secret = "private-191"
    free_text = f"{secret} 日本語の自由記述\n  次の行にも空白あり"
    verification_input["fix_sections"] = free_text
    verification_input["fix_ids"] = [secret, secret]

    assert dispatch.main(main_argv(tmp_path, verification_input)) != 0

    output = capsys.readouterr()
    assert output.err
    for fragment in (secret, "日本語の自由記述", "次の行にも空白あり"):
        assert fragment not in output.err + output.out


def test_accept_saves_the_tool_output_unchanged_with_the_observed_verifier(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    """担当が名乗った verifier を dispatch の実測値で置き換えずに保存すると、確認したモデルを偽った記録が
    verified/ に残り、道具の出力（sidecar）を変えて保存すると判定の根拠が黙って変わる。"""
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    assert accept(dispatch, run_dir) == 0
    verified_dir = scenario.review_dir / "verified"
    stem = scenario.fix[:12]
    saved = json.loads((verified_dir / f"{stem}.fix-check.json").read_text(encoding="utf-8"))
    observed = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))["verifier"]
    assert saved["verifier"] == observed
    assert saved["verifier"] != scenario.document["verifier"]
    sidecar = json.loads((verified_dir / f"{stem}.statement-candidates.json").read_text(encoding="utf-8"))
    assert sidecar == scenario.inventory


def drop_changed_file(payload: dict[str, Any], request: dict[str, Any]) -> None:
    # 検査する変更ファイルを担当が一覧から消すと、確かめていない変更ファイルを確かめたことにする。
    # inventory と fix_evidence.files の両方から消す（片方だけなら validate が止めるため）。
    payload["inventory"]["changed_files"].pop()
    payload["verified"]["fix_evidence"]["files"].pop()


def rewrite_tool_candidate(payload: dict[str, Any], request: dict[str, Any]) -> None:
    # 道具の候補の本文を書き換えると、書き換えた本文で判定した結果を受け取り、文書の記述が確かめられないまま残る。
    payload["inventory"]["candidates"][0]["text"] = "別の本文"


def drop_tool_candidate(payload: dict[str, Any], request: dict[str, Any]) -> None:
    # 道具の候補を消すと、その候補が判定されないまま保存される。
    payload["inventory"]["candidates"].clear()
    payload["verified"]["statement_candidates"]["judgments"].clear()


def missing_requested_fix(payload: dict[str, Any], request: dict[str, Any]) -> None:
    # 依頼の Fix の一部しか確かめていない結果を受け取ると、確かめていない Fix の修正を push へ進める。
    request["fix_ids"].append("other#H-1@abcdef012345")


def unjudged_candidate(payload: dict[str, Any], request: dict[str, Any]) -> None:
    # 判定していない文書の候補を受け取ると、記述の確認を黙って飛ばす。
    payload["verified"]["statement_candidates"]["judgments"].clear()


@pytest.mark.parametrize(
    "mutation",
    [drop_changed_file, rewrite_tool_candidate, drop_tool_candidate, missing_requested_fix, unjudged_candidate],
)
def test_accept_rejects_a_payload_that_differs_from_the_tool_output(
    dispatch: Any,
    scenario: Scenario,
    tmp_path: Path,
    mutation: Callable[[dict[str, Any], dict[str, Any]], None],
) -> None:
    """道具の出力・依頼と食い違う結果を受け取ると、確かめていない変更ファイル・文書・Fix を確かめたことにして
    push の判断に渡す（ケースごとの損害はコメント）。"""
    payload = scenario.payload()
    mutation(payload, scenario.request)
    run_dir = run_external(dispatch, scenario, tmp_path, payload)
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_accept_does_not_attribute_an_unauditable_checkout_to_content_changes(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    """実行中に監査できなくなった既存の checkout を変更なしとして受け取ると、外部担当が他の checkout を消した・
    書き換えた run を合格として保存する。"""
    from tools.test_support.repos import create_owned_worktree

    external = create_owned_worktree(scenario.root, tmp_path, name="external")

    class Unauditable(PayloadAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            (external / ".git").write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
            return super().collect(context, stdout_path, stderr_path)

    payload = scenario.payload()
    set_skill_digest(dispatch, payload)
    result = dispatch.dispatch(
        config("probe"),
        scenario.request,
        tmp_path / "run",
        adapter_loader=loader_for(Unauditable(payload)),
    )
    assert result["batch"]["checkout_audit"][0]["kind"] == "unavailable_after"
    assert accept(dispatch, tmp_path / "run") == 1
    assert written(scenario.review_dir) == []


def test_agent_write_to_existing_checkout_is_blocked_before_accept(
    dispatch: Any, scenario: Scenario, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """担当が開始前の checkout に書けると、並行変更を許可した後に無断変更を受け付ける。"""
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload(), touch=scenario.root / "stray")
    saved = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    assert saved["batch"]["state"] == "failed"
    assert not (scenario.root / "stray").exists()
    capsys.readouterr()
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_accept_rejects_an_unconfirmed_stop(dispatch: Any, scenario: Scenario, tmp_path: Path) -> None:
    """停止を確認できない担当の結果を受け取ると、受け取った後もその担当が保存先を書き換えうる
    （保存先の照合は accept の時点の 1 回だけ）。"""
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    saved = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    saved["batch"]["results"][0]["attempts"][-1]["stop_confirmed"] = False
    (run_dir / "dispatch.json").write_text(json.dumps(saved), encoding="utf-8")
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


def test_accept_rejects_a_failed_run(dispatch: Any, scenario: Scenario, tmp_path: Path) -> None:
    """runner が失敗とした run の payload を、形が正しいというだけで確認済みとして保存する。"""
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload(), exit_code=1)
    assert accept(dispatch, run_dir) == 1
    assert written(scenario.review_dir) == []


@pytest.mark.parametrize(
    "location,mutation",
    [
        # 前に受け取った結果を外部担当が書き換えた run を受け取ると、検証していない内容を前の結果として扱う。
        # Gitignore の保存先は runner の outside_changes に出ない。
        pytest.param("ignored", "overwrite", id="ignored-overwrite"),
        # 先の結果の消失・改名・権限変更は、内容の上書きとは別の監査条件を通る。
        pytest.param("ignored", "delete", id="ignored-delete"),
        pytest.param("ignored", "rename", id="ignored-rename"),
        pytest.param("ignored", "chmod", id="ignored-chmod"),
        # 保存先に担当が作ったファイル（偽の verified の一覧など）を前の結果として扱う。
        pytest.param("outside", "create", id="outside-create"),
    ],
)
def test_review_directory_changes_are_rejected_before_publishing(
    dispatch: Any,
    scenario: Scenario,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    location: str,
    mutation: str,
) -> None:
    """保存先が起動前と違う run を受け取ると、検証していない内容を前に受け取った結果として扱う
    （ケースごとの損害はコメント）。"""
    if location == "ignored":
        scenario.review_dir = scenario.root / "reviews/probe"
        scenario.review_dir.mkdir(parents=True)
        (scenario.root / ".git/info/exclude").write_text("/reviews/\n")
        scenario.request["review_dir"] = str(scenario.review_dir)
    target = scenario.review_dir / "previous\nresult.json"
    previous = "previous accepted contents"
    renamed = target.with_name("renamed\nresult.json")
    if mutation != "create":
        target.write_text(previous)
    original_mode = target.stat().st_mode if target.exists() else None
    payload = scenario.payload()
    set_skill_digest(dispatch, payload)

    class MutatingAdapter(PayloadAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            if mutation == "delete":
                target.unlink()
            elif mutation == "rename":
                target.rename(renamed)
            elif mutation == "chmod":
                target.chmod(target.stat().st_mode ^ 0o100)
            else:
                target.write_text("unvalidated contents")
            return super().collect(context, stdout_path, stderr_path)

    dispatch.main(main_argv(tmp_path, scenario.request, "probe"), adapter_loader=loader_for(MutatingAdapter(payload)))
    output = json.loads(capsys.readouterr().out)
    assert accept(dispatch, Path(output["artifact_dir"])) == 1
    assert not (scenario.review_dir / "verified").exists()
    # 観測だけで書込み元を断定しない。他セッションの更新を自動削除・復元しない。
    if mutation == "delete":
        assert not target.exists()
    elif mutation == "rename":
        assert not target.exists() and renamed.read_text() == previous
    elif mutation == "chmod":
        assert target.read_text() == previous and target.stat().st_mode != original_mode
    else:
        assert target.read_text() == "unvalidated contents"


def test_accept_never_writes_through_a_verified_directory_symlink(
    dispatch: Any, scenario: Scenario, tmp_path: Path
) -> None:
    """accept が verified の symlink をたどると、確認済みの一覧を保存先の外（リンク先）へ書く。"""
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    outside = tmp_path / "outside"
    outside.mkdir()
    (scenario.review_dir / "verified").symlink_to(outside, target_is_directory=True)
    # symlink が起動前からあった形にして保存先の照合を通し、accept 内の verified の検査だけが止める状況にする。
    saved = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    saved["review_snapshot"] = dispatch.snapshot_review(scenario.review_dir)
    (run_dir / "dispatch.json").write_text(json.dumps(saved), encoding="utf-8")
    assert accept(dispatch, run_dir) == 1
    assert list(outside.iterdir()) == []


def test_unavailable_review_audit_never_means_unchanged(
    dispatch: Any, scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動の後に保存先を監査できないときに変更なしとして扱うと、外部担当の書き込みを検出できないまま受け取る。"""
    snapshot = dispatch.snapshot_review
    calls = 0

    def unavailable_after_launch(root: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("private contents must not reach stderr")
        return snapshot(root)

    monkeypatch.setattr(dispatch, "snapshot_review", unavailable_after_launch)
    run_dir = run_external(dispatch, scenario, tmp_path, scenario.payload())
    assert accept(dispatch, run_dir) == 1
    assert not (scenario.review_dir / "verified").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read directories regardless of their permissions")
def test_initial_snapshot_cannot_silently_skip_an_unreadable_subdirectory(dispatch: Any, scenario: Scenario) -> None:
    """読めないサブディレクトリを黙って飛ばすと、その中の変更を前後とも観測できないまま受け取る。"""
    hidden = scenario.review_dir / "hidden"
    hidden.mkdir()
    (hidden / "previous.json").write_text("previous accepted contents")
    hidden.chmod(0)
    try:
        with pytest.raises(PermissionError):
            dispatch.snapshot_review(scenario.review_dir)
    finally:
        hidden.chmod(0o700)
