"""Cross-check pi's runtime model against its explicitly identified session."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class Entry:
    id: str
    parent_id: str | None
    kind: str
    provider: str | None
    model: str | None
    response_model: str | None
    timestamp: str | None


def string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def record(line: str) -> dict[str, object] | None:
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def read_path(path: Path) -> tuple[str | None, list[Entry], str | None]:
    """Return the header ID and active path, or a reason it cannot be read."""
    try:
        with path.open(encoding="utf-8") as handle:
            header = record(handle.readline())
            if header is None or header.get("type") != "session":
                return None, [], "session header is missing or invalid"
            header_id = string(header.get("id"))
            if header_id is None:
                return None, [], "session header has no id"
            entries: dict[str, Entry] = {}
            leaf_id: str | None = None
            for line in handle:
                if not line.strip():
                    continue
                item = record(line)
                if item is None:
                    return header_id, [], "session entry is invalid JSON"
                entry_id = string(item.get("id"))
                parent = item.get("parentId")
                if entry_id is None or "parentId" not in item or (parent is not None and not isinstance(parent, str)):
                    return header_id, [], "session entry has no valid id/parentId"
                if entry_id in entries:
                    return header_id, [], "session has duplicate entry ids"
                kind = string(item.get("type")) or ""
                provider = model = response_model = None
                if kind == "model_change":
                    provider = string(item.get("provider"))
                    model = string(item.get("modelId"))
                elif kind == "message":
                    message = item.get("message")
                    if isinstance(message, dict):
                        message = cast("dict[str, object]", message)
                        if message.get("role") == "assistant":
                            kind = "assistant"
                            provider = string(message.get("provider"))
                            model = string(message.get("model"))
                            response_model = string(message.get("responseModel"))
                entries[entry_id] = Entry(
                    entry_id,
                    parent,
                    kind,
                    provider,
                    model,
                    response_model,
                    string(item.get("timestamp")),
                )
                leaf_id = entry_id
    except (OSError, UnicodeError) as exc:
        return None, [], f"session file cannot be read: {exc}"

    if leaf_id is None:
        return header_id, [], "session has no entries"
    active: list[Entry] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen or current not in entries:
            return header_id, [], "session parent chain is missing or cyclic"
        seen.add(current)
        entry = entries[current]
        active.append(entry)
        current = entry.parent_id
    active.reverse()
    return header_id, active, None


def evidence(
    active: list[Entry],
) -> tuple[dict[str, str | None] | None, dict[str, str | None] | None, bool]:
    selected: dict[str, str | None] | None = None
    observed: dict[str, str | None] | None = None
    selected_index = observed_index = -1
    for index, entry in enumerate(active):
        if entry.kind == "model_change" and entry.provider and entry.model:
            selected = {
                "provider": entry.provider,
                "model": entry.model,
                "timestamp": entry.timestamp,
            }
            selected_index = index
        elif entry.kind == "assistant" and entry.provider and entry.model:
            observed = {
                "provider": entry.provider,
                "model": entry.model,
                "response_model": entry.response_model,
                "timestamp": entry.timestamp,
            }
            observed_index = index
    return (
        selected,
        observed,
        observed_index >= 0 and selected_index >= 0 and observed_index < selected_index,
    )


def render_text(mode: str, provider: str, model: str, session_file: str) -> str:
    if mode == "model":
        return model
    if mode == "provider":
        return provider
    if mode == "session":
        return (
            f"provider={provider}\nmodel={model}\nsession_id={os.environ.get('PI_SESSION_ID', '')}"
            f"\nsession_file={session_file}\nreasoning={os.environ.get('PI_REASONING_LEVEL', '')}"
        )
    return f"{provider}/{model}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("full", "model", "provider", "json", "session"),
        required=True,
    )
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--source", choices=("explicit", "runtime", "subagent"), required=True)
    args = parser.parse_args()
    provider = os.environ.get("PI_PROVIDER", "")
    model = os.environ.get("PI_MODEL", "")
    if not provider or not model:
        print("PI_PROVIDER/PI_MODEL is incomplete", file=sys.stderr)
        return 2

    selected: dict[str, str | None] | None = None
    observed: dict[str, str | None] | None = None
    conflicts: list[str] = []
    warnings: list[str] = []
    header_id, active, reason = read_path(Path(args.session_file))
    runtime_id = os.environ.get("PI_SESSION_ID", "")
    if reason is None and runtime_id and header_id != runtime_id:
        reason = f"session id {header_id} does not match PI_SESSION_ID {runtime_id}"
    if reason is not None:
        (conflicts if args.source == "explicit" else warnings).append(reason)
    else:
        selected, observed, stale = evidence(active)
        if selected is None:
            warnings.append("session has no selected model on the active branch")
        elif (selected["provider"], selected["model"]) != (provider, model):
            conflicts.append(
                f"PI_PROVIDER/PI_MODEL is {provider}/{model} but session selected "
                f"{selected['provider']}/{selected['model']}"
            )
        if stale:
            warnings.append("last assistant reply predates the current model selection")
        elif (
            selected is not None
            and observed is not None
            and (observed["provider"], observed["model"]) != (selected["provider"], selected["model"])
        ):
            conflicts.append(
                f"session selected {selected['provider']}/{selected['model']} but last assistant reply "
                f"used {observed['provider']}/{observed['model']}"
            )

    ok = not conflicts
    if args.mode == "json":
        payload: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "provenance": "runtime",
            "ok": ok,
            "session_file": args.session_file,
            "selected": selected,
            "observed": observed,
            "conflicts": conflicts,
            "warnings": warnings,
        }
        print(json.dumps(payload, ensure_ascii=False))
    elif ok:
        print(render_text(args.mode, provider, model, args.session_file))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for conflict in conflicts:
        print(f"conflict: {conflict}", file=sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
