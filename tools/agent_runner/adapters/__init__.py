"""Adapter protocol and fixed-name discovery; no concrete runtime is bundled."""

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Protocol, runtime_checkable

from tools.agent_runner.models import Agent, Diagnosis, Identity, Receipt, Request, Role, State


@dataclass(frozen=True, slots=True)
class AdapterContext:
    request: Request
    agent: Agent
    role: Role
    work_dir: Path
    receipt_path: Path


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    argv: tuple[str, ...]
    stdin: bytes
    env: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class Capabilities:
    noninteractive: bool = True
    stdin: bool = True
    # writes=False roles run in a runtime-enforced read-only state (codex -s read-only),
    # so caller-authorized write exceptions cannot be performed. Not a launch condition.
    enforces_read_only: bool = False


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    receipt: Receipt | None = None
    identity: Identity = field(default_factory=Identity)
    state: State | None = None
    diagnosis: Diagnosis | None = None


@runtime_checkable
class Adapter(Protocol):
    capabilities: Capabilities

    def prepare(self, context: AdapterContext) -> LaunchSpec: ...

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome: ...


_MODULES = {
    "claude-code": "claude_code",
    "devin": "devin",
    "pi": "pi",
    "codex": "codex",
    "cursor": "cursor",
    "grok-build": "grok_build",
}


class UnsupportedRuntime(Exception):
    """Safe error for unknown, absent, or unusable adapters."""

    def __init__(self) -> None:
        super().__init__("agent-runner runtime is unsupported")


def module_name(runtime: str) -> str:
    """Return only a known full module name; runtime cannot supply an import path."""
    suffix = _MODULES.get(runtime)
    if suffix is None:
        raise UnsupportedRuntime()
    return f"tools.agent_runner.adapters.{suffix}"


def load_adapter(runtime: str) -> Adapter:
    """Load a module's ADAPTER, hiding import diagnostics that may contain secrets."""
    name = module_name(runtime)
    try:
        module = import_module(name)
        adapter: object = getattr(module, "ADAPTER", None)
        capabilities: object = getattr(adapter, "capabilities", None)
        if (
            not isinstance(capabilities, Capabilities)
            or not isinstance(adapter, Adapter)
            or not callable(adapter.prepare)
            or not callable(adapter.collect)
        ):
            raise UnsupportedRuntime()
    except Exception:
        raise UnsupportedRuntime() from None
    return adapter


def runtime_supported(runtime: str) -> bool:
    """Check/list helper: whether a usable adapter is currently installed."""
    try:
        load_adapter(runtime)
    except UnsupportedRuntime:
        return False
    return True
