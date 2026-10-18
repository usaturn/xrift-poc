"""Behavior at the runner's configuration, wire-schema, and adapter boundaries."""

import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest
from pydantic import ValidationError

from tools.agent_runner.adapters import UnsupportedRuntime, load_adapter, module_name, runtime_supported
from tools.agent_runner.config import ConfigError, check_depth, load_config, resolve_config_path
from tools.agent_runner.models import Receipt, Request, Runtime

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
    personal = personal_config(
        tmp_path,
        '[workflows.audit.assignments.inspect]\nagents = ["third"]\nfallback = ["second"]\n',
    )
    config = load_config(template, personal, {"inspect": ["first"]}, "audit")
    assignment = config.workflows["audit"].assignments["inspect"]
    assert assignment.agents == ["first"]
    assert assignment.fallback == ["second"]
    assert load_config(template).workflows["audit"].assignments["inspect"].fallback == ["second", "third"]


def test_assignment_override_can_add_a_defined_role(template: Path) -> None:
    template.write_text(TEMPLATE.replace('[workflows.audit.assignments.implement]\nagents = ["second"]\n', ""))
    config = load_config(template, assignments={"implement": ["third"]}, workflow="audit")
    assert config.workflows["audit"].assignments["implement"].agents == ["third"]


def test_empty_template_can_be_completed_by_personal_config(tmp_path: Path) -> None:
    template = tmp_path / "empty.toml"
    template.write_text("schema_version = 1\n[agents]\n[roles]\n[workflows]\n")
    config = load_config(template, personal_config(tmp_path, TEMPLATE))
    assert config.agents["first"].runtime == "pi"
    assert config.workflows["audit"].assignments["inspect"].fallback == ["second", "third"]


# Missing validation would expose values through exception text or accept unsafe settings.
@pytest.mark.parametrize(
    ("overlay", "category"),
    [
        ("schema_version = 2\n", "schema_version"),
        ("schema_version = true\n", "schema_version"),
        ('schema_version = "1"\n', "schema_version"),
        ('TOP_SECRET_unknown = "TOP_SECRET_value"\n', "unknown_key"),
        ('[agents.first]\nTOP_SECRET_unknown = "TOP_SECRET_value"\n', "unknown_key"),
        ('[roles.inspect]\nTOP_SECRET_unknown = "TOP_SECRET_value"\n', "unknown_key"),
        ('[workflows.audit]\nTOP_SECRET_unknown = "TOP_SECRET_value"\n', "unknown_key"),
        ("[workflows.audit.assignments.inspect]\nTOP_SECRET_unknown = true\n", "unknown_key"),
        ('[agents.first]\ncommand = "TOP_SECRET_command"\n', "invalid_type"),
        ('[agents.first]\nenv = {TOP_SECRET = "TOP_SECRET_value"}\n', "invalid_type"),
        ("[agents.first]\nmodel = 123\n", "invalid_type"),
        ('[roles.inspect]\nwrites = "false"\n', "invalid_type"),
        ('[roles.inspect]\ndeadline_seconds = "60"\n', "invalid_type"),
        ("[roles.inspect]\ndeadline_seconds = true\n", "invalid_type"),
        ("[roles.inspect]\ndeadline_seconds = 0\n", "invalid_value"),
        ("[roles.inspect]\ndeadline_seconds = -1\n", "invalid_value"),
        ("[roles.inspect]\ndeadline_seconds = inf\n", "invalid_value"),
        ('[agents.first]\napproval = "interactive"\n', "invalid_value"),
        ('[agents.first]\nruntime = "TOP_SECRET_runtime"\n', "invalid_value"),
        ("[agents.first]\ncommand = []\n", "invalid_value"),
        ('[agents.first]\ncommand = [""]\n', "invalid_value"),
        ('[agents.first]\ncommand = ["fake", "\\u0000"]\n', "invalid_value"),
        ('[agents.first]\nenv = ["TOP_SECRET=value"]\n', "invalid_value"),
        ('[agents.first]\nmodel = " "\n', "invalid_value"),
        ('[roles.inspect]\nresult_shape = "yaml"\n', "invalid_value"),
    ],
)
def test_config_errors_are_categorized_without_input_or_traceback_leaks(
    template: Path, tmp_path: Path, overlay: str, category: str
) -> None:
    personal = personal_config(tmp_path, overlay)
    with pytest.raises(ConfigError) as raised:
        load_config(template, personal)
    assert raised.value.category == category
    rendered = "".join(traceback.format_exception(raised.value))
    assert "TOP_SECRET" not in str(raised.value)
    assert "TOP_SECRET" not in repr(raised.value)
    assert "TOP_SECRET" not in rendered
    assert str(personal) not in str(raised.value)


def test_read_errors_are_sanitized(template: Path, tmp_path: Path) -> None:
    path = tmp_path / "TOP_SECRET_file.toml"
    with pytest.raises(ConfigError) as raised:
        load_config(template, path)
    assert raised.value.category == "config_io"
    assert "TOP_SECRET" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("content", ['TOP_SECRET = "unterminated', "\xff"])
def test_parse_errors_are_sanitized(template: Path, tmp_path: Path, content: str) -> None:
    path = tmp_path / "TOP_SECRET_file.toml"
    path.write_bytes(content.encode("latin-1"))
    with pytest.raises(ConfigError) as raised:
        load_config(template, path)
    assert raised.value.category == "invalid_toml"
    assert "TOP_SECRET" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize(
    "overlay",
    [
        '[workflows.audit.assignments.inspect]\nagents = ["unknown"]\n',
        '[workflows.audit.assignments.inspect]\nfallback = ["unknown"]\n',
        '[workflows.audit.assignments.unknown]\nagents = ["first"]\n',
        '[workflows.audit.exclude_same_model_as]\ninspect = "unknown"\n',
        '[workflows.audit.exclude_same_model_as]\nunknown = "inspect"\n',
    ],
)
def test_references_must_resolve(template: Path, tmp_path: Path, overlay: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(template, personal_config(tmp_path, overlay))
    assert raised.value.category == "invalid_reference"


@pytest.mark.parametrize(
    ("assignments", "workflow"),
    [({"inspect": ["first"]}, None), ({"inspect": ["first"]}, "unknown"), ({"unknown": ["first"]}, "audit")],
)
def test_assignment_overrides_require_existing_workflow_and_role(
    template: Path, assignments: dict[str, list[str]], workflow: str | None
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(template, assignments=assignments, workflow=workflow)
    assert raised.value.category == "invalid_reference"


@pytest.mark.parametrize(
    "overlay",
    [
        "[workflows.audit.assignments.inspect]\nagents = []\n",
        '[workflows.audit.assignments.inspect]\nagents = ["self", "first"]\n',
        '[workflows.audit.assignments.inspect]\nfallback = ["self"]\n',
    ],
)
def test_self_cannot_be_mixed_or_used_as_fallback(template: Path, tmp_path: Path, overlay: str) -> None:
    with pytest.raises(ConfigError):
        load_config(template, personal_config(tmp_path, overlay))


def test_reserved_self_cannot_hide_a_real_agent_definition(template: Path, tmp_path: Path) -> None:
    personal = personal_config(
        tmp_path,
        '[agents.self]\nruntime = "pi"\ncommand = ["fake"]\nmodel = "hidden-model"\n',
    )
    with pytest.raises(ConfigError) as raised:
        load_config(template, personal)
    assert raised.value.category == "invalid_value"


def test_self_assignment_is_preserved_without_an_agent_definition(template: Path) -> None:
    config = load_config(template, assignments={"inspect": ["self"]}, workflow="audit")
    assert config.workflows["audit"].assignments["inspect"].agents == ["self"]
    assert "self" not in config.agents


@pytest.mark.parametrize(
    "overlay",
    [
        '[agents.second]\nmodel = "model-one"\nprovider = "different-provider"\n',
        '[workflows.audit.assignments.inspect]\nagents = ["first", "first"]\nfallback = []\n',
        '[workflows.audit.assignments.inspect]\nfallback = ["first"]\n',
        '[workflows.audit.assignments.inspect]\nfallback = ["second", "second"]\n',
    ],
)
def test_duplicate_models_are_rejected_across_all_candidates(template: Path, tmp_path: Path, overlay: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(template, personal_config(tmp_path, overlay))
    assert raised.value.category == "duplicate_model"


def test_same_model_is_allowed_in_different_roles_without_constraint(template: Path, tmp_path: Path) -> None:
    personal = personal_config(tmp_path, '[workflows.audit.assignments.implement]\nagents = ["first"]\n')
    config = load_config(template, personal)
    assert config.workflows["audit"].assignments["implement"].agents == ["first"]


def test_diversity_constraint_checks_fallback_candidates(template: Path, tmp_path: Path) -> None:
    personal = personal_config(tmp_path, '[workflows.audit.exclude_same_model_as]\ninspect = "implement"\n')
    with pytest.raises(ConfigError) as raised:
        load_config(template, personal)
    assert raised.value.category == "duplicate_model"


def test_diversity_constraint_accepts_disjoint_candidates(template: Path, tmp_path: Path) -> None:
    personal = personal_config(
        tmp_path,
        '[workflows.audit.assignments.inspect]\nfallback = ["third"]\n'
        '[workflows.audit.exclude_same_model_as]\ninspect = "implement"\n',
    )
    config = load_config(template, personal)
    assert config.workflows["audit"].assignments["inspect"].fallback == ["third"]


def test_after_run_is_empty_unless_configured(template: Path) -> None:
    assert load_config(template).workflows["audit"].after_run == []


@pytest.mark.parametrize(
    ("value", "category"),
    [("[[]]", "invalid_value"), ('[[" "]]', "invalid_value"), ('["not-a-list"]', "invalid_type")],
)
def test_after_run_requires_nonempty_argv_lists(template: Path, tmp_path: Path, value: str, category: str) -> None:
    personal = personal_config(tmp_path, f"[workflows.audit]\nafter_run = {value}\n")
    with pytest.raises(ConfigError) as caught:
        load_config(template, personal)
    assert caught.value.category == category


def test_personal_config_replaces_after_run(template: Path, tmp_path: Path) -> None:
    template.write_text(TEMPLATE + '[workflows.audit]\nafter_run = [["hook", "{result}"]]\n')
    assert load_config(template).workflows["audit"].after_run == [["hook", "{result}"]]
    personal = personal_config(tmp_path, "[workflows.audit]\nafter_run = []\n")
    assert load_config(template, personal).workflows["audit"].after_run == []


@pytest.mark.parametrize(
    ("explicit", "xdg", "relative"),
    [
        ("/explicit/config.toml", "/xdg", "/explicit/config.toml"),
        ("~/personal.toml", "/xdg", "personal.toml"),
        (None, "/xdg", "/xdg/agent-runner/config.toml"),
        (None, "relative-xdg", ".config/agent-runner/config.toml"),
        (None, None, ".config/agent-runner/config.toml"),
    ],
)
def test_personal_path_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: str | None, xdg: str | None, relative: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_RUNNER_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if explicit is not None:
        monkeypatch.setenv("AGENT_RUNNER_CONFIG", explicit)
    if xdg is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    expected = Path(relative) if relative.startswith("/") else tmp_path / relative
    assert resolve_config_path() == expected


@pytest.mark.parametrize(("explicit", "agent"), [("./~/personal.toml", "third"), ("~/personal.toml", "second")])
def test_explicit_path_expands_home_only_when_it_starts_with_tilde(
    monkeypatch: pytest.MonkeyPatch, template: Path, tmp_path: Path, explicit: str, agent: str
) -> None:
    """Issue #44: ./~/ names the ~ directory under the working directory; ~/ is still the home directory."""
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


@pytest.mark.parametrize("depth", ["1", "2", "-1", "TOP_SECRET_invalid"])
def test_nested_or_malformed_depth_is_rejected(monkeypatch: pytest.MonkeyPatch, depth: str) -> None:
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", depth)
    with pytest.raises(ConfigError) as raised:
        check_depth()
    assert raised.value.category == "nested_invocation"
    assert depth not in str(raised.value)


@pytest.mark.parametrize("depth", [None, "0", ""])
def test_top_level_depth_is_allowed(monkeypatch: pytest.MonkeyPatch, depth: str | None) -> None:
    monkeypatch.delenv("AGENT_RUNNER_DEPTH", raising=False)
    if depth is not None:
        monkeypatch.setenv("AGENT_RUNNER_DEPTH", depth)
    check_depth()


def request_data(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "request_id": "request-1",
        "agent_id": "first",
        "role": "inspect",
        "work_dir": tmp_path,
        "prompt": 'Inspect "quoted" input\nand report.\n',
        "deadline_seconds": 30.0,
        "result_shape": "json",
    }


def receipt_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "request_id": "request-1",
        "agent_id": "first",
        "role": "inspect",
        "status": "success",
        "payload": {"findings": [{"severity": "low", "line": 3}], "ok": True},
    }


# Identifiers become artifact path components (request_id names the attempt
# directory), so path-like or empty values must never validate.
@pytest.mark.parametrize(("field", "value"), [("request_id", "../escape"), ("run_id", "")])
def test_request_rejects_invalid_wire_data(tmp_path: Path, field: str, value: object) -> None:
    data = request_data(tmp_path)
    data[field] = value
    with pytest.raises(ValidationError):
        Request.model_validate(data)


@pytest.mark.parametrize(
    "payload",
    [object(), {"nested": object()}, {"nonfinite": float("nan")}, {"key": (1, 2)}, {1: "non-string-key"}],
)
def test_receipt_rejects_non_json_payloads(payload: object) -> None:
    data = receipt_data()
    data["payload"] = payload
    with pytest.raises(ValidationError):
        Receipt.model_validate(data)


def test_receipt_rejects_cyclic_payload() -> None:
    payload: dict[str, object] = {}
    payload["cycle"] = payload
    data = receipt_data()
    data["payload"] = payload
    with pytest.raises(ValidationError):
        Receipt.model_validate(data)


# Arbitrary import paths or leaking import errors would defeat the adapter boundary.
# Modules without ADAPTER stand in for absent/unusable adapters so the test
# stays valid once every fixed runtime ships an implementation.
# Runtimes come from the configuration model, so a runtime without a fixed
# adapter module mapping fails here instead of being silently skipped.
@pytest.mark.parametrize("runtime", [*get_args(Runtime), "unknown"])
def test_absent_adapters_are_unsupported_without_import_error_leaks(
    runtime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if runtime != "unknown":
        name = module_name(runtime)
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    assert not runtime_supported(runtime)
    with pytest.raises(UnsupportedRuntime) as raised:
        load_adapter(runtime)
    assert runtime not in str(raised.value)


def test_unknown_runtime_cannot_select_an_arbitrary_module() -> None:
    with pytest.raises(UnsupportedRuntime) as raised:
        module_name("TOP_SECRET.arbitrary.module")
    assert "TOP_SECRET" not in str(raised.value)


def test_invalid_adapter_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("tools.agent_runner.adapters.pi")
    module.__dict__["ADAPTER"] = object()
    monkeypatch.setitem(sys.modules, "tools.agent_runner.adapters.pi", module)
    with pytest.raises(UnsupportedRuntime):
        load_adapter("pi")
