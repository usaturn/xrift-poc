"""設定役割の実行・回数制限・fix-check受取を実Gitとfake adapterで確かめる。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import Config, ConfigError
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.models import Agent, Assignment, Identity, Receipt, Role, Runtime, Workflow

pytest_plugins = ("tools.test_support.plugin",)
# 偽の担当だけを起動するので、runner の担当（AGENT_RUNNER_DEPTH=1）の中でもテストの中だけ depth を外す
pytestmark = pytest.mark.usefixtures("outside_runner")

HELPER = Path(__file__).resolve().parents[1] / "scripts/run_roles.py"
SPEC = importlib.util.spec_from_file_location("planning_roles", HELPER)
assert SPEC is not None and SPEC.loader is not None
mod: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def test_broken_personal_config_never_falls_back_to_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """壊れた個人設定リンクを無視すると、意図しない担当やafter_runの設定で修正が進む。"""
    personal = tmp_path / "個人 設定.toml"
    personal.symlink_to(tmp_path / "missing.toml")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(personal))
    request = tmp_path / "request.json"
    request.write_text("{}")

    def no_run(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("壊れた個人設定のまま担当を起動しようとした")

    monkeypatch.setattr(mod, "run", no_run)
    assert mod.main(["--phase", "implement", "--request", str(request), "--output-dir", str(tmp_path / "output")]) != 0


PROGRAM = """
import json, pathlib, sys
request = json.load(sys.stdin)
receipt = {key: request[key] for key in ('schema_version','run_id','request_id','agent_id','role')}
receipt.update(status='success', payload={'role': request['role']})
pathlib.Path(sys.argv[1]).write_text(json.dumps(receipt))
"""


class FakeAdapter:
    capabilities = Capabilities()

    def __call__(self, runtime: str) -> FakeAdapter:
        return self

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        return LaunchSpec(
            argv=(*context.agent.command, str(context.receipt_path)),
            stdin=context.request.model_dump_json().encode(),
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        return AdapterOutcome(
            Receipt.model_validate_json(context.receipt_path.read_bytes()),
            Identity(model=context.agent.model, session_id="fake-session"),
        )


class RecordingAdapter(FakeAdapter):
    def __init__(self, *, read_only: bool = False) -> None:
        self.capabilities = Capabilities(enforces_read_only=read_only)
        self.prepared: list[str] = []

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        self.prepared.append(context.request.agent_id)
        return super().prepare(context)


def config(self_review: str = "self", fix_check: str = "self") -> Config:
    """pi の担当を fake adapter で動かす。"""
    runtimes: dict[str, Runtime] = {"a": "pi", "b": "pi"}
    return Config(
        agents={
            name: Agent(runtime=runtime, command=[sys.executable, "-c", PROGRAM], model=name)
            for name, runtime in runtimes.items()
        },
        roles={
            name: Role(writes=name == "implement", deadline_seconds=20.0, result_shape="json")
            for name in ("implement", "self-review", "fix-check")
        },
        workflows={
            "fix": Workflow(
                assignments={
                    "implement": Assignment(agents=["a"]),
                    "self-review": Assignment(agents=[self_review]),
                    "fix-check": Assignment(agents=[fix_check]),
                }
            )
        },
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fixture")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "commit", "-qm", "base", "--allow-empty")
    source = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-qm", "fix", "--allow-empty")
    target = git(repo, "rev-parse", "HEAD")
    request = {
        "repository": str(repo),
        "source_head": source,
        "revision": target,
        "pr": {"repository": "usaturn/xrift-poc", "number": 900},
        "prompt": {
            name: f"{name}: inspect all assigned committed input" for name in ("implement", "self-review", "fix-check")
        },
    }
    actor: dict[str, Any] = {
        "model": "fixture",
        "runtime": "pi",
        "provenance": {
            "agent_id": "self",
            "role": "decide",
            "provider": "unknown",
            "effort": "unknown",
            "session_id": "unknown",
            "cli_version": "unknown",
            "run_id": "unknown",
            "request_id": "unknown",
            "skills": [],
        },
    }
    planning = tmp_path / "planning.json"
    planning.write_text(
        json.dumps(
            {
                "schema": "review-findings/v2",
                "kind": "planning",
                "pr": request["pr"],
                "reviewed_head": source,
                "planner": actor,
                "verified_file": f"verified/{source[:12]}.json",
                "stages": {"decide_seconds": None, "fix_seconds": None, "fix_check_seconds": []},
                "fix_checks": [],
                "outcome": "stopped",
                "comment_id": None,
                "activities": [],
                "rework": [],
            }
        )
    )
    review_dir = tmp_path / "fix-staging"
    review_dir.mkdir()
    verified_file = review_dir / "verified-input.json"
    verified_file.write_text(
        json.dumps(
            {
                "schema": "review-findings/v1",
                "kind": "verified",
                "mode": "cycle",
                "pr": request["pr"],
                "reviewed_head": source,
                "base": source,
                "verifier": {"model": "fixture", "runtime": "pi"},
                "skill": {"path": ".agents/skills/review-verify/SKILL.md", "sha256": "0" * 64},
                "timing": {"started_at": "2026-10-08T06:00:00Z", "finished_at": "2026-10-08T06:00:00Z", "seconds": 0},
                "inputs": [],
                "coverage": {"closed": [], "remaining": []},
                "findings": [],
                "accuracy": {"reviewers": [], "find_seconds": {}, "verify_seconds": 0},
            }
        )
    )
    fix_input = {
        "repository": str(repo),
        "from": source,
        "to": target,
        "fix_ids": ["fixture#H-1@" + source[:12]],
        "fix_sections": "## Fix\nCorrect the introduced defect; relevant checks passed.",
        "verified_file": str(verified_file),
        "review_dir": str(review_dir),
    }
    return request, planning, fix_input


def launch(
    inputs: tuple[dict[str, Any], Path, dict[str, Any]], tmp_path: Path, selected: Config, **kwargs: Any
) -> Any:
    request, planning, fix_input = inputs
    return mod.run(
        selected, "self-review", request, tmp_path / "run", planning_file=planning, fix_input=fix_input, **kwargs
    )


@pytest.fixture
def parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """親の main worktree。fixture の repository とは別の Git ディレクトリ（プロジェクトの PR と同じ形）。"""
    root = tmp_path / "parent"
    root.mkdir()
    git(root, "init", "-q")
    monkeypatch.setattr(mod.review_target, "default_parent_root", lambda: root)
    return root


@pytest.mark.parametrize(
    "assigned",
    [pytest.param(("self", "self"), id="both-self"), pytest.param(("a", "self"), id="external-review-self-check")],
)
def test_single_call_uses_both_review_roles_and_retains_self_instructions(
    inputs: tuple[dict[str, Any], Path, dict[str, Any]], tmp_path: Path, parent: Path, assigned: tuple[str, str]
) -> None:
    """self の役割が pending_self から落ちる・別の役割の success を fix-check の記録にすると、未確認で push に進む。"""
    batch = launch(inputs, tmp_path, config(*assigned), adapter_loader=FakeAdapter())
    saved = json.loads((batch.artifact_dir / "roles.json").read_text())
    assert set(saved["self_prompts"]) == {
        role for role, agent in zip(("self-review", "fix-check"), assigned, strict=True) if agent == "self"
    }
    receipt = mod.verifier.read_dispatch(batch.artifact_dir / "fix-check-receipt")
    assert len(receipt["batch"]["results"]) == 1
    assert receipt["batch"]["state"] == "self"


@pytest.mark.parametrize(
    ("role", "fallback"),
    [
        pytest.param("self-review", False, id="self-review-primary"),
        pytest.param("fix-check", False, id="fix-check-primary"),
        pytest.param("self-review", True, id="read-only-fallback"),
    ],
)
def test_read_only_review_agent_is_rejected_before_reservation(
    inputs: tuple[dict[str, Any], Path, dict[str, Any]],
    tmp_path: Path,
    parent: Path,
    role: str,
    fallback: bool,
) -> None:
    """readonly の一次・fallback 担当を予約後に拒否すると、実行できないレビューで戻せない 1 回枠を消費する。"""
    selected = config()
    selected.agents["b"].runtime = "codex"
    selected.workflows["fix"].assignments[role] = Assignment(
        agents=["a"] if fallback else ["b"], fallback=["b"] if fallback else []
    )
    read_only = RecordingAdapter(read_only=True)
    writable = RecordingAdapter()

    def adapters(runtime: str) -> FakeAdapter:
        return read_only if runtime == "codex" else writable

    before = json.loads(inputs[1].read_text())["activities"]
    with pytest.raises(mod.RoleInputError):
        try:
            launch(inputs, tmp_path, selected, adapter_loader=adapters)
        finally:
            # 後段 runner の例外でも、先に消費した予約を見逃さない。
            assert json.loads(inputs[1].read_text())["activities"] == before
            assert not (tmp_path / "run").exists()
            assert not read_only.prepared
            assert not writable.prepared


def test_verified_input_mismatch_never_consumes_the_only_review(inputs: Any, tmp_path: Path) -> None:
    """別の HEAD の確認済み一覧で fix-check を動かすと、dispatch.accept は一致を見ないので誤った基準で受け取る。"""
    path = Path(inputs[2]["verified_file"])
    value = json.loads(path.read_text())
    value["reviewed_head"] = "f" * 40
    path.write_text(json.dumps(value))
    with pytest.raises(mod.RoleInputError):
        launch(inputs, tmp_path, config())
    assert not (tmp_path / "run").exists()
    assert json.loads(inputs[1].read_text())["activities"] == []


def test_second_review_is_refused_even_when_all_assignments_are_self(inputs: Any, tmp_path: Path) -> None:
    """2 回目の予約を許すと、自己レビューは 1 回だけの規則が黙って破れ、通るまでレビューし直せる。"""
    launch(inputs, tmp_path, config())
    with pytest.raises(mod.RoleInputError):
        mod.run(config(), "self-review", inputs[0], tmp_path / "second", planning_file=inputs[1], fix_input=inputs[2])
    assert not (tmp_path / "second").exists()


def test_existing_lock_does_not_start_or_erase_another_call(inputs: Any, tmp_path: Path) -> None:
    """他のセッションのロックを無視・削除すると、並行した呼び出しが両方とも予約し、1 回の記録で 2 回レビューする。"""
    lock = inputs[1].with_suffix(".json.review-lock")
    lock.write_text("other-session")
    with pytest.raises(mod.RoleInputError):
        launch(inputs, tmp_path, config())
    assert lock.read_text() == "other-session"
    assert json.loads(inputs[1].read_text())["activities"] == []


def test_role_projection_rejects_lost_constraints() -> None:
    """投影の外を参照する同モデルの除外を黙って捨てると、実装と同じモデルが自分の修正をレビューする。"""
    selected = config("b", "self")
    selected.workflows["fix"].exclude_same_model_as = {"self-review": "implement"}
    with pytest.raises(ConfigError):
        mod.project(selected, "self-review")


def test_review_directory_tampering_is_not_accepted_or_cleaned(inputs: Any, tmp_path: Path, parent: Path) -> None:
    """review_snapshot を起動の後に取ると、実行中に他者が書いた検証結果を accept が受け取る。"""

    class Tampering(FakeAdapter):
        def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
            Path(inputs[2]["review_dir"], "external-change").write_text("do not erase")
            return super().collect(context, stdout_path, stderr_path)

    batch = launch(inputs, tmp_path, config("a", "b"), adapter_loader=Tampering())
    saved = json.loads((batch.artifact_dir / "roles.json").read_text())
    assert saved["review_changes"] == ["external-change"]
    receipt = mod.verifier.read_dispatch(batch.artifact_dir / "fix-check-receipt")
    assert receipt["review_changes"] == ["external-change"]
    assert Path(inputs[2]["review_dir"], "external-change").read_text() == "do not erase"


def test_unconfirmed_stop_blocks_fix_check_accept_and_keeps_budget(
    inputs: Any, tmp_path: Path, parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止を確かめられない fix-check を success と記録すると accept が受け取り、予約を戻すと 2 回目が可能になる。"""
    original = mod.run_workflow

    def no_stop(*args: Any, **kwargs: Any) -> Any:
        batch = original(*args, **kwargs)
        fix_check = next(result for result in batch.results if result.role == "fix-check")
        fix_check.attempts[-1].stop_confirmed = False
        return batch

    monkeypatch.setattr(mod, "run_workflow", no_stop)
    batch = launch(inputs, tmp_path, config("a", "b"), adapter_loader=FakeAdapter())
    assert json.loads((batch.artifact_dir / "roles.json").read_text())["review_changes"] is None
    receipt = mod.verifier.read_dispatch(batch.artifact_dir / "fix-check-receipt")
    assert receipt["batch"]["state"] != "success"
    assert len(json.loads(inputs[1].read_text())["activities"]) == 1
