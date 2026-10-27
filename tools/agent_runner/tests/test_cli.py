"""CLI selection, retained run records, and after_run outcomes against fake agents."""

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.agent_runner import cli, hooks
from tools.agent_runner.models import BatchResult, Identity
from tools.agent_runner.tests.test_runner import FakeAdapter, configuration
from tools.test_support.repos import create_env_repo

pytest_plugins = ("tools.test_support.plugin",)


@pytest.mark.usefixtures("outside_runner")
def test_cli_assignment_keeps_fallback_and_observed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI の担当選択・fallbackを落とすと別providerへ送り、実測identityを落とすと実行記録を黙って誤る。"""
    repository = create_env_repo(tmp_path / "fixture", copy_paths=()).root
    config = configuration()
    template_agent, personal_agent, chosen_agent = config.agents
    template = tmp_path / "template.toml"
    template.write_text(
        "\n".join(
            f'[agents.{name}]\nruntime = "{agent.runtime}"\ncommand = {json.dumps(agent.command)}\n'
            f'model = "{agent.model}"\n'
            for name, agent in config.agents.items()
        )
        + '[roles.inspect]\nwrites = false\ndeadline_seconds = 10\nresult_shape = "json"\n'
        + f'[workflows.audit.assignments.inspect]\nagents = ["{template_agent}"]\nfallback = ["{personal_agent}"]\n'
    )
    personal = tmp_path / "personal.toml"
    personal.write_text(
        f'[workflows.audit.assignments.inspect]\nagents = ["{personal_agent}"]\nfallback = ["{template_agent}"]\n'
    )
    prompt = " \t確認する依頼 '引用' \"二重引用\" $HOME; $(echo forbidden)\n二行目 & <>\n末尾 \t\n"
    prompt_file = tmp_path / "依頼 with spaces.txt"
    prompt_file.write_text(prompt)
    observed = Identity(
        model="observed-model",
        provider="observed-provider",
        effort="observed-effort",
        session_id="observed-session",
        cli_version="observed-version",
    )
    batches: list[BatchResult] = []
    original = cli.run_workflow

    def with_fake(*args: Any, **kwargs: Any) -> BatchResult:
        batch = original(
            *args,
            **kwargs,
            adapter_loader=lambda _: FakeAdapter({chosen_agent: {"missing": True}}, identity=observed),
        )
        batches.append(batch)
        return batch

    monkeypatch.setattr(cli, "run_workflow", with_fake)
    status = cli.main(
        [
            "--template",
            str(template),
            "--config",
            str(personal),
            "run",
            "--workflow",
            "audit",
            "--repository",
            str(repository),
            "--prompt-file",
            str(prompt_file),
            "--output-dir",
            str(tmp_path / "run"),
            "--assign",
            f"inspect={chosen_agent}",
        ]
    )
    assert status == 0
    summary = json.loads(capsys.readouterr().out)
    recorded = json.loads(Path(summary["result_path"]).read_bytes())
    result = batches[0].results[0]
    assert result.requested_agent == chosen_agent
    assert result.actual_agent == template_agent
    assert [item.agent_id for item in result.attempts] == [chosen_agent, template_agent]
    successful = result.attempts[-1]
    assert successful.identity == observed
    assert recorded["results"][0]["attempts"][-1].get("identity") == observed.model_dump()
    assert isinstance(successful.payload, dict)
    received = successful.payload["prompt"]
    assert isinstance(received, str) and received.endswith(prompt)


def test_failed_after_run_is_never_reported_as_success(tmp_path: Path) -> None:
    """失敗した後処理（attach-runner）を success と報告すると、loop が成功を見て黙って進む。"""
    reports = hooks.run_after_run(
        [[sys.executable, "-c", "raise SystemExit(3)"]],
        result_path=tmp_path / "result.json",
        cwd=tmp_path,
        cancel=threading.Event(),
    )
    assert reports[0]["stop_confirmed"] is True
    assert reports[0]["state"] != "success"
