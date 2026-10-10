from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL = REPO_ROOT / ".agents/skills/pi-model-name/SKILL.md"
SCRIPT = REPO_ROOT / ".agents/skills/pi-model-name/scripts/print-pi-model.sh"


def read_skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_frontmatter() -> None:
    text = read_skill()
    assert text.startswith("---\n")
    front = text.split("---\n", 2)[1]
    assert "name: pi-model-name" in front
    assert "description:" in front
    assert len(front.encode("utf-8")) < 1024


def run_script(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # DSH_SHELL はこのテスト自身が DSH 配下で走るときに継承される。ランタイム
    # ごとの分岐を固定するため、pi / DSH のどちらの変数も明示された分だけにする。
    for key in (
        "PI_PROVIDER",
        "PI_MODEL",
        "PI_SESSION_ID",
        "PI_SESSION_FILE",
        "PI_SUBAGENT_SESSION",
        "PI_REASONING_LEVEL",
        "DSH_SHELL",
        "DSH_PROVIDER",
        "DSH_MODEL",
        "DSH_REASONING_EFFORT",
        "DSH_SESSION_ID",
        "DSH_PROFILE",
    ):
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def dsh_env(**extra: str) -> dict[str, str]:
    return {
        "DSH_SHELL": "1",
        "DSH_PROVIDER": "opencode-go",
        "DSH_MODEL": "deepseek-v4.1-flash",
        "DSH_SESSION_ID": "dsh-session",
        "DSH_REASONING_EFFORT": "max",
        **extra,
    }


def test_prints_runtime_model() -> None:
    proc = run_script([], {"PI_PROVIDER": "opencode-go", "PI_MODEL": "muse-spark-1.3-contributor"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "opencode-go/muse-spark-1.3-contributor"


def test_model_only_flag() -> None:
    proc = run_script(
        ["--model"],
        {"PI_PROVIDER": "opencode-go", "PI_MODEL": "muse-spark-1.3-contributor"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "muse-spark-1.3-contributor"


def test_json_provenance_runtime() -> None:
    proc = run_script(
        ["--json"],
        {"PI_PROVIDER": "opencode-go", "PI_MODEL": "muse-spark-1.3-contributor"},
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["provenance"] == "runtime"
    assert payload["model"] == "muse-spark-1.3-contributor"


def test_strict_fails_closed_without_runtime() -> None:
    proc = run_script([])
    assert proc.returncode == 2
    assert "PI_PROVIDER/PI_MODEL" in proc.stderr
    assert "session" in proc.stderr.lower() or "history" in proc.stderr.lower()


def test_dsh_runtime_prints_provider_and_model() -> None:
    proc = run_script([], dsh_env())
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "opencode-go/deepseek-v4.1-flash\n"
    assert proc.stderr == ""


@pytest.mark.parametrize(
    "flag,expected",
    [("--model", "deepseek-v4.1-flash\n"), ("--provider", "opencode-go\n")],
)
def test_dsh_runtime_partial_flags(flag: str, expected: str) -> None:
    proc = run_script([flag], dsh_env())
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected


def test_dsh_json_provenance_and_key_set() -> None:
    proc = run_script(["--json"], dsh_env())
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["provenance"] == "dsh-runtime"
    assert payload["ok"] is True
    assert (payload["provider"], payload["model"]) == ("opencode-go", "deepseek-v4.1-flash")
    assert payload["session_file"] is None
    assert payload["conflicts"] == []
    pi_payload = json.loads(
        run_script(["--json"], {"PI_PROVIDER": "opencode-go", "PI_MODEL": "deepseek-v4.1-flash"}).stdout
    )
    assert set(payload) == set(pi_payload)


def test_dsh_session_mode_reports_session_facts() -> None:
    proc = run_script(["--session"], dsh_env())
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "provider=opencode-go\nmodel=deepseek-v4.1-flash\nsession_id=dsh-session\nsession_file=\nreasoning=max\n"
    )


def test_pi_runtime_wins_over_dsh_facts() -> None:
    proc = run_script([], dsh_env(PI_PROVIDER="commandcode", PI_MODEL="deepseek/deepseek-v4.1-flash"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "commandcode/deepseek/deepseek-v4.1-flash\n"


@pytest.mark.parametrize(
    "facts",
    [{"DSH_SHELL": "1"}, {"DSH_SHELL": "1", "DSH_PROVIDER": "opencode-go"}, {"DSH_SHELL": "1", "DSH_MODEL": "m"}],
)
def test_dsh_without_complete_facts_fails_closed(facts: dict[str, str]) -> None:
    proc = run_script([], facts)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "DSH_PROVIDER/DSH_MODEL" in proc.stderr
    assert "history" in proc.stderr.lower()


def test_dsh_rejects_explicit_session_file(tmp_path: Path) -> None:
    session = write_session(tmp_path / "sess.jsonl", [model_change("m1", None)])
    proc = run_script(["--session-file", str(session)], dsh_env())
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "--session-file applies to pi sessions only" in proc.stderr


def test_dsh_does_not_use_session_history_without_flag(tmp_path: Path) -> None:
    session = write_session(tmp_path / "sess.jsonl", [model_change("m1", None, model="model-b")])
    proc = run_script([], {"DSH_SHELL": "1", "PI_SESSION_FILE": str(session)})
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "DSH_PROVIDER/DSH_MODEL" in proc.stderr


def test_dsh_keeps_history_informational_path_behind_the_flag(tmp_path: Path) -> None:
    session = write_session(tmp_path / "sess.jsonl", [model_change("m1", None, model="model-b")])
    proc = run_script(["--allow-session-history", "--model"], {"DSH_SHELL": "1", "PI_SESSION_FILE": str(session)})
    assert proc.returncode == 0
    assert proc.stdout == "model-b\n"
    assert proc.stderr.splitlines() == ["provenance=session-history (informational only, NOT formal MODEL_NAME)"]


def test_history_allowed_only_with_flag(tmp_path: Path) -> None:
    session = tmp_path / "sess.jsonl"
    session.write_text(
        '{"type": "model_change", "id": "a", "provider": "opencode-go", "modelId": "muse-spark-1.3-contributor"}\n',
        encoding="utf-8",
    )
    strict = run_script([], {"PI_SESSION_FILE": str(session)})
    assert strict.returncode == 2
    allowed = run_script(
        ["--allow-session-history", "--model"],
        {"PI_SESSION_FILE": str(session)},
    )
    assert allowed.returncode == 0
    assert allowed.stdout.strip() == "muse-spark-1.3-contributor"
    assert allowed.stderr.splitlines() == ["provenance=session-history (informational only, NOT formal MODEL_NAME)"]


def test_history_prefers_explicit_file_over_runtime_file(tmp_path: Path) -> None:
    explicit = write_session(
        tmp_path / "explicit.jsonl",
        [model_change("explicit", None, provider="chosen", model="model-a")],
    )
    runtime = write_session(
        tmp_path / "runtime.jsonl",
        [model_change("runtime", None, provider="other", model="model-b")],
    )
    result = run_script(
        ["--allow-session-history", "--session-file", str(explicit)],
        {"PI_SESSION_FILE": str(runtime)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "chosen/model-a\n"


def test_history_uses_explicit_file_without_runtime_file(tmp_path: Path) -> None:
    explicit = write_session(tmp_path / "explicit.jsonl", [model_change("m1", None)])
    result = run_script(["--allow-session-history", "--session-file", str(explicit)])
    assert result.returncode == 0, result.stderr
    assert result.stdout == "opencode-go/model-a\n"


def test_history_uses_subagent_file_without_runtime_file(tmp_path: Path) -> None:
    subagent = write_session(tmp_path / "subagent.jsonl", [model_change("m1", None)])
    result = run_script(["--allow-session-history"], {"PI_SUBAGENT_SESSION": str(subagent)})
    assert result.returncode == 0, result.stderr
    assert result.stdout == "opencode-go/model-a\n"


def test_history_does_not_fall_back_from_unreadable_explicit_file(tmp_path: Path) -> None:
    runtime = write_session(tmp_path / "runtime.jsonl", [model_change("m1", None)])
    result = run_script(
        ["--allow-session-history", "--session-file", str(tmp_path / "missing.jsonl")],
        {"PI_SESSION_FILE": str(runtime)},
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_history_invalid_utf8_fails_without_traceback(tmp_path: Path) -> None:
    session = tmp_path / "invalid.jsonl"
    session.write_bytes(b'{"type":"model_change","id":"m1","provider":"p","modelId":"m"}\n\xff\xfe\n')
    result = run_script(["--allow-session-history"], {"PI_SESSION_FILE": str(session)})
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Stop and ask the user" in result.stderr
    assert "Traceback" not in result.stderr


def write_session(path: Path, entries: list[dict[str, object]], session_id: str = "current") -> Path:
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-09-22T12:00:00.000Z",
        "cwd": str(path.parent),
    }
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in [header, *entries]),
        encoding="utf-8",
    )
    return path


def model_change(
    entry_id: str,
    parent_id: str | None,
    provider: str = "opencode-go",
    model: str = "model-a",
) -> dict[str, object]:
    return {
        "type": "model_change",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": "2026-09-22T12:01:00.000Z",
        "provider": provider,
        "modelId": model,
    }


def assistant(
    entry_id: str,
    parent_id: str,
    provider: str = "opencode-go",
    model: str = "model-a",
    response_model: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "assistant",
        "provider": provider,
        "model": model,
    }
    if response_model is not None:
        message["responseModel"] = response_model
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": "2026-09-22T12:02:00.000Z",
        "message": message,
    }


def runtime_env(path: Path, **extra: str) -> dict[str, str]:
    return {
        "PI_PROVIDER": "opencode-go",
        "PI_MODEL": "model-a",
        "PI_SESSION_ID": "current",
        "PI_SESSION_FILE": str(path),
        **extra,
    }


def test_session_crosscheck_reports_matching_selected_and_observed(
    tmp_path: Path,
) -> None:
    path = write_session(
        tmp_path / "current.jsonl",
        [model_change("m1", None), assistant("a1", "m1", response_model="backend-b")],
    )
    result = run_script(["--json"], runtime_env(path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provenance"] == "runtime"
    assert payload["selected"]["model"] == "model-a"
    assert payload["observed"]["model"] == "model-a"
    assert payload["observed"]["response_model"] == "backend-b"
    assert payload["conflicts"] == []


def test_runtime_and_selected_conflict_suppresses_text_answer(tmp_path: Path) -> None:
    path = write_session(tmp_path / "current.jsonl", [model_change("m1", None, model="model-b")])
    result = run_script([], runtime_env(path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert "conflict:" in result.stderr


@pytest.mark.parametrize("observed", [("other-provider", "model-a"), ("opencode-go", "model-b")])
def test_newer_observed_identity_conflicts(tmp_path: Path, observed: tuple[str, str]) -> None:
    path = write_session(
        tmp_path / "current.jsonl",
        [model_change("m1", None), assistant("a1", "m1", *observed)],
    )
    result = run_script(["--json"], runtime_env(path))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["conflicts"]


def test_observed_before_model_switch_is_warning(tmp_path: Path) -> None:
    path = write_session(
        tmp_path / "current.jsonl",
        [
            model_change("m1", None, model="old"),
            assistant("a1", "m1", model="old"),
            model_change("m2", "a1"),
        ],
    )
    result = run_script(["--json"], runtime_env(path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["conflicts"] == []
    assert any("predates" in warning for warning in payload["warnings"])


def test_crosscheck_ignores_abandoned_branch(tmp_path: Path) -> None:
    path = write_session(
        tmp_path / "current.jsonl",
        [
            model_change("m1", None),
            model_change("abandoned", "m1", model="model-b"),
            assistant("current-leaf", "m1"),
        ],
    )
    result = run_script(["--json"], runtime_env(path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["selected"]["model"] == "model-a"
    assert payload["observed"]["model"] == "model-a"


def test_explicit_session_file_precedes_env_sources(tmp_path: Path) -> None:
    selected = write_session(tmp_path / "selected.jsonl", [model_change("m1", None)])
    other = write_session(tmp_path / "other.jsonl", [model_change("m2", None, model="model-b")])
    result = run_script(
        ["--session-file", str(selected), "--json"],
        runtime_env(other, PI_SUBAGENT_SESSION=str(other)),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected"]["model"] == "model-a"


def test_pi_session_file_precedes_subagent_session(tmp_path: Path) -> None:
    selected = write_session(tmp_path / "selected.jsonl", [model_change("m1", None)])
    other = write_session(tmp_path / "other.jsonl", [model_change("m2", None, model="model-b")])
    result = run_script(["--json"], runtime_env(selected, PI_SUBAGENT_SESSION=str(other)))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected"]["model"] == "model-a"


def test_wrong_session_id_does_not_overrule_runtime_env(tmp_path: Path) -> None:
    path = write_session(
        tmp_path / "other.jsonl",
        [model_change("m1", None, model="model-b")],
        session_id="other",
    )
    automatic = run_script(["--json"], runtime_env(path))
    assert automatic.returncode == 0, automatic.stderr
    payload = json.loads(automatic.stdout)
    assert payload["selected"] is None
    assert payload["warnings"]
    explicit = run_script(["--session-file", str(path), "--json"], runtime_env(path))
    assert explicit.returncode == 2
    assert json.loads(explicit.stdout)["ok"] is False


def test_unreadable_session_paths_follow_source_policy(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    automatic = run_script(["--json"], runtime_env(missing))
    assert automatic.returncode == 0, automatic.stderr
    assert json.loads(automatic.stdout)["warnings"]
    explicit = run_script(["--session-file", str(missing), "--json"], runtime_env(missing))
    assert explicit.returncode == 2
    assert json.loads(explicit.stdout)["ok"] is False


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("--model", "model-a\n"),
        ("--provider", "opencode-go\n"),
        ("--session", "provider=opencode-go\nmodel=model-a\nsession_id=current\n"),
    ],
)
def test_crosschecked_text_modes(tmp_path: Path, flag: str, expected: str) -> None:
    path = write_session(tmp_path / "current.jsonl", [model_change("m1", None)])
    result = run_script([flag], runtime_env(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(expected)


def test_json_without_session_path_reports_unchecked(tmp_path: Path) -> None:
    result = run_script(["--json"], {"PI_PROVIDER": "opencode-go", "PI_MODEL": "model-a"})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["selected"] is None
    assert payload["observed"] is None


def test_runtime_json_has_same_keys_with_and_without_session_file(tmp_path: Path) -> None:
    session = write_session(tmp_path / "current.jsonl", [model_change("m1", None)])
    checked = run_script(["--json"], runtime_env(session))
    unchecked = run_script(["--json"], {"PI_PROVIDER": "opencode-go", "PI_MODEL": "model-a"})
    assert checked.returncode == 0, checked.stderr
    assert unchecked.returncode == 0, unchecked.stderr
    checked_payload = json.loads(checked.stdout)
    unchecked_payload = json.loads(unchecked.stdout)
    assert set(unchecked_payload) == set(checked_payload)
    assert unchecked_payload["session_file"] is None


def test_runtime_env_stays_required_with_explicit_session_file(tmp_path: Path) -> None:
    path = write_session(tmp_path / "current.jsonl", [model_change("m1", None)])
    result = run_script(["--session-file", str(path)], {"PI_SESSION_FILE": str(path)})
    assert result.returncode == 2
    assert "PI_PROVIDER/PI_MODEL" in result.stderr
