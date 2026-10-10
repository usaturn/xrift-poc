"""TOML overlays and semantic validation without exposing configuration values."""

import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from tools.agent_runner.models import Config

_CATEGORIES = frozenset(
    {
        "schema_version",
        "unknown_key",
        "invalid_type",
        "invalid_value",
        "invalid_reference",
        "duplicate_model",
        "config_io",
        "invalid_toml",
        "nested_invocation",
        "invalid_config",
    }
)


class ConfigError(Exception):
    """A stable category, never a path, value, validation detail, or original error."""

    def __init__(self, category: str) -> None:
        self.category = category if category in _CATEGORIES else "invalid_config"
        super().__init__(f"agent-runner configuration error: {self.category}")


def resolve_config_path() -> Path:
    """Explicit path, absolute XDG_CONFIG_HOME, then the user's .config directory."""
    explicit = os.environ.get("AGENT_RUNNER_CONFIG", "")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "agent-runner/config.toml"
    return Path.home() / ".config/agent-runner/config.toml"


def check_depth() -> None:
    """Fail closed for nested or malformed depth; the runner sets child depth to 1."""
    depth = os.environ.get("AGENT_RUNNER_DEPTH", "")
    if not depth:
        return
    try:
        nested = int(depth) != 0
    except ValueError:
        raise ConfigError("nested_invocation") from None
    if nested:
        raise ConfigError("nested_invocation")


def _read(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError, UnicodeDecodeError:
        raise ConfigError("invalid_toml") from None
    except OSError:
        raise ConfigError("config_io") from None


def _merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        inherited = merged.get(key)
        if isinstance(inherited, dict) and isinstance(value, dict):
            merged[key] = _merge(cast(dict[str, object], inherited), cast(dict[str, object], value))
        else:
            merged[key] = deepcopy(value)
    return merged


def _override(data: dict[str, object], assignments: dict[str, list[str]], workflow: str | None) -> None:
    workflows = data.get("workflows")
    roles = data.get("roles")
    if (
        workflow is None
        or not isinstance(workflows, dict)
        or workflow not in workflows
        or not isinstance(roles, dict)
        or any(role not in roles for role in assignments)
    ):
        raise ConfigError("invalid_reference")
    workflows = cast(dict[str, object], workflows)
    selected = workflows[workflow]
    if not isinstance(selected, dict):
        raise ConfigError("invalid_type")
    selected = cast(dict[str, object], selected)
    existing = selected.setdefault("assignments", {})
    if not isinstance(existing, dict):
        raise ConfigError("invalid_type")
    existing = cast(dict[str, object], existing)
    for role, agents in assignments.items():
        previous = existing.get(role, {})
        if not isinstance(previous, dict):
            raise ConfigError("invalid_type")
        replacement = deepcopy(cast(dict[str, object], previous))
        replacement["agents"] = deepcopy(agents)
        existing[role] = replacement


def _validation_category(error: ValidationError) -> str:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if any(detail["loc"] == ("schema_version",) for detail in details):
        return "schema_version"
    if any(detail["type"] == "extra_forbidden" for detail in details):
        return "unknown_key"
    if any(detail["type"].endswith("_type") or detail["type"] == "missing" for detail in details):
        return "invalid_type"
    return "invalid_value"


def _validate_references(config: Config) -> None:
    for workflow in config.workflows.values():
        models_by_role: dict[str, set[str]] = {}
        for role, assignment in workflow.assignments.items():
            if role not in config.roles:
                raise ConfigError("invalid_reference")
            models: set[str] = set()
            for agent_name in assignment.agents + assignment.fallback:
                if agent_name == "self":
                    continue
                agent = config.agents.get(agent_name)
                if agent is None:
                    raise ConfigError("invalid_reference")
                if agent.model in models:
                    raise ConfigError("duplicate_model")
                models.add(agent.model)
            models_by_role[role] = models
        for role, other in workflow.exclude_same_model_as.items():
            if role not in models_by_role or other not in models_by_role:
                raise ConfigError("invalid_reference")
            if models_by_role[role] & models_by_role[other]:
                raise ConfigError("duplicate_model")


def validate_config(config: Config, workflow: str | None = None) -> Config:
    """Revalidate mutable API instances and all semantic references."""
    try:
        validated = Config.model_validate(config.model_dump(warnings=False))
    except ValidationError:
        raise ConfigError("invalid_config") from None
    if workflow is not None and workflow not in validated.workflows:
        raise ConfigError("invalid_reference")
    _validate_references(validated)
    return validated


def load_config(
    template: Path,
    personal: Path | None = None,
    assignments: dict[str, list[str]] | None = None,
    workflow: str | None = None,
) -> Config:
    """Load template < personal < assignment overrides, then validate the final form.

    None means no personal overlay; callers resolve/choose that path explicitly so
    check/list and tests need not touch a live user's configuration.
    """
    data = _read(template)
    if personal is not None:
        data = _merge(data, _read(personal))
    if assignments:
        _override(data, assignments, workflow)
    try:
        config = Config.model_validate(data)
    except ValidationError as error:
        raise ConfigError(_validation_category(error)) from None
    return validate_config(config, workflow)
