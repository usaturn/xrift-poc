"""check-options が、start と同じく既存の個人設定を重ねることを確かめる。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

COPY_AGENT = """
[agents.delta]
runtime = "pi"
command = ["fake-delta"]
model = "delta-model"
"""


def check_options(loop: Any, capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, Any]]:
    capsys.readouterr()
    code = loop.main(["check-options", *arguments])
    return code, json.loads(capsys.readouterr().out)


def test_check_options_reads_the_personal_config_when_not_given(
    loop: Any, env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--config` が無いときに既存の個人設定を重ねなくなると、start・review も同じ `personal_config` で
    個人設定（担当・after_run）を黙って無視し、雛形の担当で修正・レビューを進める。"""
    personal = tmp_path / "personal.toml"
    personal.write_text(COPY_AGENT, encoding="utf-8")
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", str(personal))

    code, output = check_options(loop, capsys, "--assign", "implement=delta", "--template", str(env.template))

    assert code == 0, output
    assert output["assignments"]["fix"]["implement"]["agents"] == ["delta"]
