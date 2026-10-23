"""Synchronous workflows with isolated retained attempts and role-scoped deadlines.

The role deadline starts after acquiring the concurrency slot and covers worktree
creation, preparation, and every explicit fallback. Process teardown may exceed
it. Adapters are trusted integrations, not a security sandbox.
"""

import asyncio
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypedDict, cast

from tools.agent_runner.adapters import Adapter, AdapterContext, LaunchSpec, load_adapter
from tools.agent_runner.config import check_depth, validate_config
from tools.agent_runner.environ import literal_environ, secret_values
from tools.agent_runner.models import (
    DIAGNOSIS_TEXT_CHARS,
    Attempt,
    BatchResult,
    Config,
    Diagnosis,
    Identity,
    Payload,
    Receipt,
    Request,
    Result,
    State,
)
from tools.agent_runner.process import execute, redact_file
from tools.agent_runner.receipts import read_receipt
from tools.agent_runner.workspace import (
    WorkspaceError,
    audit_checkouts,
    prepare_worktree,
    resolve_revision,
    snapshot_checkouts,
)


class _Paths(TypedDict, total=False):
    work_dir: Path
    stdout_path: Path
    stderr_path: Path
    request_path: Path


class RunnerError(Exception):
    """An operation category with no input values or underlying diagnostics."""

    def __init__(self, category: str = "invalid_input") -> None:
        self.category = (
            category if category in {"invalid_input", "output_io", "workspace", "runner_failed"} else "runner_failed"
        )
        super().__init__(f"agent-runner error: {self.category}")


def _scrub(value: object, secrets: set[str]) -> object:
    if isinstance(value, str):
        values = sorted(secrets, key=len, reverse=True)
        if not values:
            return value
        return re.sub("|".join(re.escape(secret) for secret in values), "[REDACTED]", value)
    if isinstance(value, dict):
        return {
            cast(str, _scrub(key, secrets)): _scrub(child, secrets)
            for key, child in cast(dict[str, object], value).items()
        }
    if isinstance(value, list):
        return [_scrub(child, secrets) for child in cast(list[object], value)]
    return value


EXCEPTIONS_HEADING = "Caller-authorized exceptions to the rules above (only these; everything else stays forbidden):"


def _single_line(value: object) -> bool:
    """One trimmed line without control characters, so a value cannot add or reshape work-scope lines.

    Non-ASCII spaces (U+3000, NBSP) stay valid: review directories take them from branch names.
    """
    return (
        type(value) is str
        and value != ""
        and value == value.strip()
        and value.splitlines() == [value]
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def _collect(
    adapter: Adapter,
    context: AdapterContext,
    stdout: Path,
    stderr: Path,
    payload_check: Callable[[Payload], bool] | None,
) -> tuple[State, Identity, Payload, Diagnosis | None]:
    try:
        # Validate regular, bounded file content before a collector can open it.
        receipt = read_receipt(context.receipt_path)
        outcome = adapter.collect(context, stdout, stderr)
        identity = Identity.model_validate(outcome.identity.model_dump(warnings=False))
        diagnosis = (
            Diagnosis.model_validate(outcome.diagnosis.model_dump(warnings=False))
            if outcome.diagnosis is not None
            else None
        )
        failure = outcome.state
        if failure not in {
            None,
            "success",
            "awaiting_authentication",
            "awaiting_approval",
            "response_failed",
            "interrupted",
        }:
            return "invalid_result", identity, None, diagnosis
        if receipt is None:
            if outcome.receipt is not None:
                receipt = Receipt.model_validate(outcome.receipt.model_dump(warnings=False))
            else:
                state = "missing_result" if failure is None or failure == "success" else failure
                return state, identity, None, diagnosis
        request = context.request
        if (receipt.run_id, receipt.request_id, receipt.agent_id, receipt.role) != (
            request.run_id,
            request.request_id,
            request.agent_id,
            request.role,
        ):
            return "invalid_result", identity, None, diagnosis
        if receipt.status == "success":
            payload = receipt.payload
            valid = (
                isinstance(payload, dict) and bool(payload)
                if context.role.result_shape == "json"
                else isinstance(payload, str) and bool(payload.strip())
            )
            if not valid:
                return "invalid_result", identity, None, diagnosis
            if payload_check is not None:
                # A caller refusal is not an adapter failure: keep the observed
                # identity, but never expose the check's exception text.
                try:
                    accepted = payload_check(payload) is True
                except Exception:
                    accepted = False
                if not accepted:
                    return "invalid_result", identity, None, diagnosis
        state = receipt.status if failure is None or failure == "success" else failure
        return state, identity, receipt.payload, diagnosis
    except FileNotFoundError:
        return "missing_result", Identity(), None, None
    except Exception:
        # Includes adapter parsing/validation failures; never expose exception text.
        return "invalid_result", Identity(), None, None


def _bounded_diagnosis(diagnosis: Diagnosis, secrets: set[str]) -> Diagnosis:
    """Redact first, then cut every string: a cut made first could keep a secret's head at the boundary."""
    value = cast(dict[str, object], _scrub(diagnosis.model_dump(), secrets))
    return Diagnosis.model_validate(
        {key: item[:DIAGNOSIS_TEXT_CHARS] if isinstance(item, str) else item for key, item in value.items()}
    )


def read_only_assignments(
    config: Config, workflow: str, adapter_loader: Callable[[str], Adapter] = load_adapter
) -> list[tuple[str, str]]:
    """(role, agent) for the workflow's writes=False roles whose agent, primary or fallback (never self),
    has an adapter declaring enforces_read_only: such an agent cannot perform caller-authorized writes.

    An adapter that cannot be loaded is left out; its launch reports unsupported as before.
    """
    found: list[tuple[str, str]] = []
    for role, assignment in config.workflows[workflow].assignments.items():
        if config.roles[role].writes:
            continue
        for name in dict.fromkeys([*assignment.agents, *assignment.fallback]):
            if name == "self":
                continue
            try:
                enforced = adapter_loader(config.agents[name].runtime).capabilities.enforces_read_only is True
            except Exception:
                continue
            if enforced:
                found.append((role, name))
    return found


def run_workflow(
    config: Config,
    workflow: str,
    prompt: str,
    repository: Path,
    *,
    revision: str = "HEAD",
    concurrency: int = 2,
    output_dir: Path | None = None,
    cancel: threading.Event | None = None,
    adapter_loader: Callable[[str], Adapter] = load_adapter,
    write_exceptions: Sequence[str] = (),
    payload_check: Callable[[Payload], bool] | None = None,
) -> BatchResult:
    """Execute a batch synchronously; keep every artifact/worktree, even on failure.

    Only named environment variables are inherited (excluding Git routing), with
    os.defpath as the PATH default. Adapter additions are explicit; child depth
    is always forced to 1. Caller-chosen paths and request prompts are intentional
    artifacts; environment values never enter request.json or error diagnostics.
    write_exceptions are caller-authorized exceptions listed verbatim under the
    work-scope lines, one per line; the runner neither interprets nor enforces them,
    but refuses them before creating anything when a writes=False role is assigned
    an agent that cannot perform them (read_only_assignments).
    payload_check, when given, also checks each well-shaped success payload (before
    redaction); anything but True, or an exception, is invalid_result and moves on
    to the next explicit fallback.
    """
    started = time.monotonic()
    check_depth()
    config = validate_config(config, workflow)
    # A bare string is also a Sequence[str]; never list it one character per line.
    exceptions = () if isinstance(write_exceptions, str) else tuple(write_exceptions)
    if (
        type(concurrency) is not int
        or concurrency < 1
        or type(prompt) is not str
        or isinstance(write_exceptions, str)
        or not all(_single_line(item) for item in exceptions)
    ):
        raise RunnerError()
    if exceptions and read_only_assignments(config, workflow, adapter_loader):
        raise RunnerError()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RunnerError()
    try:
        repository = repository.absolute()
        commit = resolve_revision(repository, revision)
        before = snapshot_checkouts(repository)
    except WorkspaceError:
        raise RunnerError("workspace") from None
    try:
        if output_dir is None:
            artifact_dir = Path(tempfile.mkdtemp(prefix="xrift-poc-agent-runner-", dir="/tmp"))
        else:
            artifact_dir = output_dir.absolute()
            # mkdir without exist_ok rejects existing directories AND dangling links.
            artifact_dir.mkdir(mode=0o700)
    except OSError:
        raise RunnerError("output_io") from None
    run_id = uuid.uuid4().hex
    event = cancel if cancel is not None else threading.Event()
    selected = config.workflows[workflow]
    # Checkout helpers inherit the safe Git environment, not the agent's
    # allowlist. Children inherit the literal exec environment; ambient
    # credentials from either source are scrubbed from retained artifacts.
    ambient = literal_environ()
    secrets = secret_values(dict(os.environ)) | secret_values(ambient)

    async def attempt(role_name: str, agent_id: str, request_id: str, number: int, until: float) -> Attempt:
        attempt_started = time.monotonic()
        paths: _Paths = {}

        def finished(state: State, *, stop: bool = True, exit_code: int | None = None) -> Attempt:
            return Attempt(
                agent_id=agent_id,
                state=state,
                elapsed_seconds=time.monotonic() - attempt_started,
                stop_confirmed=stop,
                exit_code=exit_code,
                **paths,
            )

        if event.is_set():
            return finished("interrupted")
        if time.monotonic() >= until:
            return finished("timed_out")
        agent = config.agents[agent_id]
        role = config.roles[role_name]
        try:
            adapter = adapter_loader(agent.runtime)
            if adapter.capabilities.noninteractive is not True or adapter.capabilities.stdin is not True:
                return finished("unsupported")
        except Exception:
            return finished("unsupported")
        directory = artifact_dir / request_id / f"attempt-{number}"
        work_dir = directory / "worktree"
        stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
        request_path = directory / "request.json"
        prep_stdout, prep_stderr = directory / "worktree.stdout.log", directory / "worktree.stderr.log"
        paths.update(
            work_dir=work_dir,
            stdout_path=prep_stdout,
            stderr_path=prep_stderr,
            request_path=request_path,
        )
        try:
            directory.mkdir(parents=True, mode=0o700)
            preparation = await prepare_worktree(
                repository,
                commit,
                work_dir,
                deadline_seconds=until - time.monotonic(),
                cancel=event,
                stdout_path=prep_stdout,
                stderr_path=prep_stderr,
                secrets=tuple(secrets),
            )
            if preparation.state is not None or not preparation.stop_confirmed or preparation.exit_code != 0:
                state = preparation.state or "response_failed"
                return finished(state, stop=preparation.stop_confirmed, exit_code=preparation.exit_code)
            remaining = until - time.monotonic()
            if event.is_set():
                return finished("interrupted")
            if remaining <= 0:
                return finished("timed_out")
            paths.update(stdout_path=stdout_path, stderr_path=stderr_path)
            instructions = (
                f"Work in this worktree only: {work_dir}. Do not modify other checkouts.\n"
                f"writes={role.writes}: {'changes are permitted here' if role.writes else 'do not modify files'}. "
                "This is an instruction, not a sandbox.\n"
            )
            if exceptions:
                instructions += EXCEPTIONS_HEADING + "\n" + "".join(f"- {item}\n" for item in exceptions)
            instructions += "\n"
            request = Request(
                run_id=run_id,
                request_id=request_id,
                agent_id=agent_id,
                role=role_name,
                work_dir=work_dir,
                prompt=instructions + prompt,
                deadline_seconds=remaining,
                result_shape=role.result_shape,
            )
            request_path.write_text(request.model_dump_json(), encoding="utf-8")
            env = {"PATH": os.defpath}
            env.update({name: ambient[name] for name in agent.env if name in ambient and not name.startswith("GIT_")})
            context = AdapterContext(
                request,
                agent,
                role,
                work_dir,
                directory / "receipt.json",
            )
            spec = adapter.prepare(context)
            if context.receipt_path.exists() or context.receipt_path.is_symlink():
                return finished("invalid_result")
            if not spec.argv or any(argument == prompt or request.prompt in argument for argument in spec.argv):
                return finished("launch_failed")
            env.update({name: value for name, value in spec.env.items() if not name.startswith("GIT_")})
            secrets.update(secret_values(spec.env))
            env["AGENT_RUNNER_DEPTH"] = "1"
            # execute merges spec.env last; consume additions here to forbid a depth override.
            safe_spec = LaunchSpec(spec.argv, spec.stdin)
        except Exception:
            return finished("launch_failed")
        if event.is_set():
            return finished("interrupted")
        remaining = until - time.monotonic()
        if remaining <= 0:
            return finished("timed_out")
        process = await execute(
            safe_spec,
            cwd=work_dir,
            env=env,
            deadline_seconds=remaining,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cancel=event,
            secrets=tuple(secrets),
        )
        sensitive_paths: list[Path] = []
        export_path = context.receipt_path.parent / "export.json" if agent.runtime == "devin" else None
        export_unsafe = False
        if process.stop_confirmed:
            # Devin leaves an ATIF export next to the receipt; inherited values can
            # be repeated in the final answer, history, and tool arguments. Redact
            # it like the receipt, before any collection reads it.
            if export_path is not None:
                export_unsafe = not redact_file(export_path, tuple(secrets))
            if export_path is not None and export_unsafe:
                # Only a provably redacted managed file may reach a collector.
                sensitive_paths.append(export_path)
                state, identity, payload, diagnosis = "response_failed", Identity(), None, None
            else:
                state, identity, payload, diagnosis = _collect(
                    adapter, context, stdout_path, stderr_path, payload_check
                )
            if not redact_file(context.receipt_path, tuple(secrets)):
                sensitive_paths.append(context.receipt_path)
        else:
            # A live writer may hold a FIFO or replace the receipt. Never read or
            # rewrite its files before independent stop evidence is available.
            state, identity, payload, diagnosis = "response_failed", Identity(), None, None
            sensitive_paths.append(context.receipt_path)
            if export_path is not None:
                sensitive_paths.append(export_path)
        if process.state in {"timed_out", "launch_failed"}:
            state = process.state
        elif event.is_set() or process.state == "interrupted":
            state = "interrupted"
        elif not process.stop_confirmed or (sensitive_paths and state == "success"):
            state = "response_failed"
        elif process.state is not None or process.exit_code != 0:
            if state not in {"awaiting_authentication", "awaiting_approval", "interrupted"}:
                state = "response_failed"
        return Attempt(
            agent_id=agent_id,
            state=state,
            identity=identity,
            diagnosis=diagnosis,
            payload=payload,
            elapsed_seconds=time.monotonic() - attempt_started,
            exit_code=process.exit_code,
            stop_confirmed=process.stop_confirmed,
            sensitive_paths=sensitive_paths,
            **paths,
        )

    async def batch() -> list[Result]:
        semaphore = asyncio.Semaphore(concurrency)
        claimed_fallbacks: dict[str, set[str]] = {role: set() for role in selected.assignments}

        async def assignment(role_name: str, primary: str, fallback: list[str]) -> Result:
            request_id = uuid.uuid4().hex
            async with semaphore:
                attempts: list[Attempt] = []
                if primary == "self":
                    return Result(
                        request_id=request_id,
                        role=role_name,
                        requested_agent=primary,
                        actual_agent=primary,
                        state="self",
                        attempts=[],
                    )
                until = time.monotonic() + config.roles[role_name].deadline_seconds
                exhausted = False
                for number, agent_id in enumerate([primary, *fallback], 1):
                    if number > 1:
                        if agent_id in claimed_fallbacks[role_name]:
                            exhausted = True
                            continue
                        # No await between membership and claim: each fallback
                        # can fill at most one role slot, even across failures.
                        claimed_fallbacks[role_name].add(agent_id)
                    current = await attempt(role_name, agent_id, request_id, number, until)
                    attempts.append(current)
                    if current.state in {"success", "interrupted", "timed_out"} or not current.stop_confirmed:
                        break
                    if event.is_set():
                        current.state = "interrupted"
                        break
                last = attempts[-1]
                return Result(
                    request_id=request_id,
                    role=role_name,
                    requested_agent=primary,
                    actual_agent=last.agent_id,
                    state=last.state,
                    attempts=attempts,
                    fallback_exhausted=exhausted and last.state != "success",
                )

        return list(
            await asyncio.gather(
                *(
                    assignment(role, primary, assigned.fallback)
                    for role, assigned in selected.assignments.items()
                    for primary in assigned.agents
                )
            )
        )

    results = asyncio.run(batch())
    # Adapter-added secrets from any attempt are scrubbed across the entire batch.
    for result in results:
        for item in result.attempts:
            item.identity = Identity.model_validate(_scrub(item.identity.model_dump(), secrets))
            item.payload = cast(Payload, _scrub(item.payload, secrets))
            if item.diagnosis is not None:
                item.diagnosis = _bounded_diagnosis(item.diagnosis, secrets)
    successful = sum(result.state in {"success", "self"} for result in results)
    state = (
        "self"
        if all(result.state == "self" for result in results)
        else ("success" if successful == len(results) else "partial_success" if successful else "failed")
    )
    checkout_audit = audit_checkouts(before)
    return BatchResult(
        run_id=run_id,
        state=state,
        assignments={role: list(assigned.agents) for role, assigned in selected.assignments.items()},
        results=results,
        outside_changes=[finding.path for finding in checkout_audit],
        checkout_audit=checkout_audit,
        elapsed_seconds=time.monotonic() - started,
        artifact_dir=artifact_dir,
    )
