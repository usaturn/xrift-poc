"""``print-pi-model.sh`` と ``check_session.py`` の出力と照合。

出力は ``MODEL_NAME`` としてレビュー・状況コメント・ファイル名に記録され、誤った値はどこでもエラーにならない。
照合は、実行中の変数が当てにならない場面で記録を止める安全網で、誤った証拠を読むと食い違いを黙って見逃す。
情報表示の経路（``--allow-session-history``・``--session``）と、壊れると exit 2 で止まる方向は確かめない。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / ".agents/skills/pi-model-name/scripts/print-pi-model.sh"


def run_script(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key in (
        "PI_PROVIDER",
        "PI_MODEL",
        "PI_SESSION_ID",
        "PI_SESSION_FILE",
        "PI_SUBAGENT_SESSION",
        "PI_REASONING_LEVEL",
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


def test_prints_runtime_model() -> None:
    """セッションファイルの無い pi で provider/model の組み立てを誤ると、誤った MODEL_NAME を黙って記録する。"""
    proc = run_script([], {"PI_PROVIDER": "opencode-go", "PI_MODEL": "muse-spark-1.3-contributor"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "opencode-go/muse-spark-1.3-contributor"


def test_model_only_flag() -> None:
    """``--model`` の枝が別の値を出すと、MODEL_NAME に使う model ID を黙って取り違える。"""
    proc = run_script(
        ["--model"],
        {"PI_PROVIDER": "opencode-go", "PI_MODEL": "muse-spark-1.3-contributor"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "muse-spark-1.3-contributor"


def test_json_reports_the_runtime_model() -> None:
    """``--json`` の model の欄を取り違えると、JSON を読む側が誤った MODEL_NAME を黙って記録する。"""
    proc = run_script(
        ["--json"],
        {"PI_PROVIDER": "opencode-go", "PI_MODEL": "muse-spark-1.3-contributor"},
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["model"] == "muse-spark-1.3-contributor"


def test_strict_fails_closed_without_runtime() -> None:
    """変数もセッションファイルも無いときに値（保存設定など）を出すと、推測のモデル名が正式値として記録される。"""
    proc = run_script([])
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_history_allowed_only_with_flag(tmp_path: Path) -> None:
    """変数が無くセッションファイルがあるとき、フラグなしで履歴を正式値にすると、推測のモデル名を記録する。"""
    session = tmp_path / "sess.jsonl"
    session.write_text(
        '{"type": "model_change", "id": "a", "provider": "opencode-go", "modelId": "muse-spark-1.3-contributor"}\n',
        encoding="utf-8",
    )
    strict = run_script([], {"PI_SESSION_FILE": str(session)})
    assert strict.returncode != 0
    assert strict.stdout == ""
    # 対照: 同じファイルの履歴は読める（上の拒否が、履歴を読めないことによる空振りでない）
    allowed = run_script(
        ["--allow-session-history", "--model"],
        {"PI_SESSION_FILE": str(session)},
    )
    assert allowed.returncode == 0
    assert allowed.stdout.strip() == "muse-spark-1.3-contributor"


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
) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": "2026-09-22T12:02:00.000Z",
        "message": {"role": "assistant", "provider": provider, "model": model},
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
    """直近の応答との食い違いのテストの対照。応答の記録があれば常に食い違いにする実装でも、食い違いのテストが通るのを防ぐ。"""
    path = write_session(
        tmp_path / "current.jsonl",
        [model_change("m1", None), assistant("a1", "m1")],
    )
    result = run_script(["--json"], runtime_env(path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["selected"]["model"] == "model-a"
    assert payload["observed"]["model"] == "model-a"
    assert payload["conflicts"] == []


def test_runtime_and_selected_conflict_suppresses_text_answer(tmp_path: Path) -> None:
    """実行中の値とセッションの選択が食い違うのにテキストを出すと、疑わしい値を MODEL_NAME として記録する。"""
    path = write_session(tmp_path / "current.jsonl", [model_change("m1", None, model="model-b")])
    result = run_script([], runtime_env(path))
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("observed", [("other-provider", "model-a"), ("opencode-go", "model-b")])
def test_newer_observed_identity_conflicts(tmp_path: Path, observed: tuple[str, str]) -> None:
    """直近の応答が別の provider・model なのに一致と判定すると、実際に応答したモデルと違う名前を記録する。"""
    path = write_session(
        tmp_path / "current.jsonl",
        [model_change("m1", None), assistant("a1", "m1", *observed)],
    )
    result = run_script(["--json"], runtime_env(path))
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["conflicts"]


def test_crosscheck_ignores_abandoned_branch(tmp_path: Path) -> None:
    """現在の枝でなくファイル全体を読むと、放棄した枝の選択で照合し、食い違いを黙って見逃しうる。"""
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
    """明示したファイルより環境変数のファイルを先に読むと、引き継いだ別セッションの証拠で照合しうる。"""
    selected = write_session(tmp_path / "selected.jsonl", [model_change("m1", None)])
    other = write_session(tmp_path / "other.jsonl", [model_change("m2", None, model="model-b")])
    result = run_script(
        ["--session-file", str(selected), "--json"],
        runtime_env(other, PI_SUBAGENT_SESSION=str(other)),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected"]["model"] == "model-a"


def test_pi_session_file_precedes_subagent_session(tmp_path: Path) -> None:
    """``PI_SESSION_FILE`` と ``PI_SUBAGENT_SESSION`` の順を誤ると、別のセッションの証拠で照合しうる。"""
    selected = write_session(tmp_path / "selected.jsonl", [model_change("m1", None)])
    other = write_session(tmp_path / "other.jsonl", [model_change("m2", None, model="model-b")])
    result = run_script(["--json"], runtime_env(selected, PI_SUBAGENT_SESSION=str(other)))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected"]["model"] == "model-a"


def test_wrong_session_id_does_not_overrule_runtime_env(tmp_path: Path) -> None:
    """``PI_SESSION_ID`` と違うセッションの証拠を使うと、別のセッションで照合しうる。"""
    path = write_session(
        tmp_path / "other.jsonl",
        [model_change("m1", None, model="model-b")],
        session_id="other",
    )
    automatic = run_script(["--json"], runtime_env(path))
    assert automatic.returncode == 0, automatic.stderr
    assert json.loads(automatic.stdout)["selected"] is None


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("--model", "model-a\n"),
        ("--provider", "opencode-go\n"),
    ],
)
def test_crosschecked_text_modes(tmp_path: Path, flag: str, expected: str) -> None:
    """セッションファイルのある通常の pi では出力が ``render_text`` から出る。枝ごとの取り違えを黙って記録する。"""
    path = write_session(tmp_path / "current.jsonl", [model_change("m1", None)])
    result = run_script([flag], runtime_env(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(expected)
