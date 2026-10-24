"""Configuration overlays, references, and personal path resolution."""

import traceback
from pathlib import Path

import pytest

from tools.agent_runner.config import ConfigError, load_config, resolve_config_path

# A shallow merge or array append loses inherited fields or keeps removed agents.
TEMPLATE = """schema_version = 1
[agents.first]
runtime = "pi"
command = ["fake-cli", "--original"]
model = "model-one"
approval = "noninteractive"
skill_sources = ["/template/skills"]
args = ["--template"]
env = ["HOME", "PATH"]
[agents.second]
runtime = "codex"
command = ["fake-cli-two"]
model = "model-two"
[agents.third]
runtime = "devin"
command = ["fake-cli-three"]
model = "model-three"
[roles.inspect]
skills = ["inspect"]
writes = false
deadline_seconds = 60
result_shape = "json"
[roles.implement]
skills = ["implement"]
writes = true
deadline_seconds = 120
result_shape = "text"
[workflows.audit.assignments.inspect]
agents = ["first"]
fallback = ["second", "third"]
[workflows.audit.assignments.implement]
agents = ["second"]
"""


@pytest.fixture
def template(tmp_path: Path) -> Path:
    path = tmp_path / "template.toml"
    path.write_text(TEMPLATE)
    return path


def personal_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "personal.toml"
    path.write_text(content)
    return path


def test_recursive_overlay_replaces_arrays_and_preserves_other_fields(template: Path, tmp_path: Path) -> None:
    """配列を連結すると、個人設定で外した env（資格情報の変数名）・args・after_run が雛形から黙って残る。"""
    personal = personal_config(
        tmp_path,
        """[agents.first]
command = ["personal-cli"]
args = []
env = ["PATH"]
[roles.inspect]
deadline_seconds = 30.5
[workflows.audit.assignments.inspect]
agents = ["third"]
fallback = ["second"]
""",
    )
    config = load_config(template, personal)
    assert config.agents["first"].command == ["personal-cli"]
    assert config.agents["first"].model == "model-one"
    assert config.agents["first"].skill_sources == ["/template/skills"]
    assert config.agents["first"].args == []
    assert config.agents["first"].env == ["PATH"]
    assert config.roles["inspect"].deadline_seconds == 30.5
    assert config.roles["inspect"].skills == ["inspect"]
    assert config.workflows["audit"].assignments["inspect"].agents == ["third"]
    assert config.workflows["audit"].assignments["inspect"].fallback == ["second"]


def test_assignment_override_is_last_and_keeps_explicit_fallback(template: Path, tmp_path: Path) -> None:
    """--assign・--implement が個人設定に負けると、指定と違う担当（利用者が選んでいない provider）で黙って動く。"""
    personal = personal_config(
        tmp_path,
        '[workflows.audit.assignments.inspect]\nagents = ["third"]\nfallback = ["second"]\n',
    )
    config = load_config(template, personal, {"inspect": ["first"]}, "audit")
    assignment = config.workflows["audit"].assignments["inspect"]
    assert assignment.agents == ["first"]
    assert assignment.fallback == ["second"]


def test_config_errors_never_carry_input_values(template: Path, tmp_path: Path) -> None:
    """検証の失敗が pydantic の入力の値を連れて出ると、設定に誤って書いた資格情報がエラーと traceback に黙って出る。"""
    personal = personal_config(tmp_path, '[agents.first]\nenv = {TOP_SECRET = "TOP_SECRET_value"}\n')
    with pytest.raises(ConfigError) as raised:
        load_config(template, personal)
    rendered = "".join(traceback.format_exception(raised.value))
    assert "TOP_SECRET" not in str(raised.value)
    assert "TOP_SECRET" not in repr(raised.value)
    assert "TOP_SECRET" not in rendered


def test_duplicate_models_are_rejected_across_all_candidates(template: Path, tmp_path: Path) -> None:
    """provider だけ違う同じモデルを通すと、並行のレビュアーが同じモデルになり、独立したレビューが黙って減る。"""
    personal = personal_config(tmp_path, '[agents.second]\nmodel = "model-one"\nprovider = "different-provider"\n')
    with pytest.raises(ConfigError):
        load_config(template, personal)


def test_duplicate_agent_names_are_rejected(template: Path, tmp_path: Path) -> None:
    """同じ担当を 2 回通すと、同じモデルの結果が 2 人分として数えられ独立したレビューが黙って減る。"""
    personal = personal_config(
        tmp_path, '[workflows.audit.assignments.inspect]\nagents = ["first", "first"]\nfallback = []\n'
    )
    with pytest.raises(ConfigError):
        load_config(template, personal)


def test_diversity_constraint_checks_fallback_candidates(template: Path, tmp_path: Path) -> None:
    """exclude_same_model_as が fallback を見ないと、実装担当と同じモデルが self-review を黙って担う。"""
    personal = personal_config(tmp_path, '[workflows.audit.exclude_same_model_as]\ninspect = "implement"\n')
    with pytest.raises(ConfigError):
        load_config(template, personal)


def test_explicit_personal_path_wins_over_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AGENT_RUNNER_CONFIG が効かないと、指定した設定の代わりに利用者の個人設定（後処理を含む）で黙って動く。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", "/explicit/config.toml")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert resolve_config_path() == Path("/explicit/config.toml")


@pytest.mark.parametrize(("explicit", "agent"), [("./~/personal.toml", "third"), ("~/personal.toml", "second")])
def test_explicit_path_expands_home_only_when_it_starts_with_tilde(
    monkeypatch: pytest.MonkeyPatch, template: Path, tmp_path: Path, explicit: str, agent: str
) -> None:
    """./~/ を家と読む（#44: 終了コード 0 のまま別の設定を採用した）、~/ を展開しないと別の・無い設定で黙って動く。"""
    home = tmp_path / "home"
    work = tmp_path / "work"
    (work / "~").mkdir(parents=True)
    home.mkdir()
    overlay = '[workflows.audit.assignments.inspect]\nagents = ["{}"]\nfallback = []\n'
    (work / "~" / "personal.toml").write_text(overlay.format("third"))
    (home / "personal.toml").write_text(overlay.format("second"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    monkeypatch.setenv("AGENT_RUNNER_CONFIG", explicit)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    config = load_config(template, resolve_config_path())

    assert config.workflows["audit"].assignments["inspect"].agents == [agent]
