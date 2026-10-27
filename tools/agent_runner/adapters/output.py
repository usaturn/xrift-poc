"""Runtime CLI output shared by the adapters: files, JSON events, answers, and receipts."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from tools.agent_runner.adapters.strict_json import decode, raw_decode
from tools.agent_runner.models import Payload, Receipt, ReceiptStatus, Request, ResultShape

_AUTH = re.compile(
    r"(?:not logged in|not authenticated|sign[ -]?in|log ?in|unauthorized|forbidden"
    r"|authentication|invalid api key|missing api key|no api key|api key is invalid"
    r"|oauth|\b40[13]\b|credentials? (?:missing|invalid|expired))",
    re.IGNORECASE,
)
_APPROVAL = re.compile(
    r"(?:workspace trust|trust this|awaiting approval|requires? approval"
    r"|permission (?:denied|required)|grant access|confirm(?:ed)? (?:to )?continue)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```(?:json|JSON)\b[^\n]*\n(.*?)```", re.DOTALL)


def read_text(path: Path) -> str:
    """Decode a file as UTF-8 with replacement and no newline translation; unreadable files read as empty."""
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def json_event(line: str) -> dict[str, object] | None:
    """Parse one line as a JSON object.

    Only ``JSONDecodeError`` is swallowed; other ``ValueError``s (an integer over
    the conversion limit) propagate, unlike ``ndjson_events``.
    """
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def ndjson_events(path: Path) -> Iterator[dict[str, object]]:
    """Yield the JSON objects among a file's lines, skipping every line that is not one.

    Lines are stripped and must start with ``{``; any ``ValueError`` skips the
    line, unlike ``json_event``.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield cast(dict[str, object], value)


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def iter_strings(node: object) -> Iterator[str]:
    """Every string value nested in dicts and lists (dict keys are not visited)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in cast(dict[str, object], node).values():
            yield from iter_strings(value)
    elif isinstance(node, list):
        for value in cast(list[object], node):
            yield from iter_strings(value)


def fenced_object(text: str) -> dict[str, object] | None:
    """The whole answer as a JSON object, else the first ```json fence that is one.

    Text around an unfenced object is not searched, unlike ``embedded_object``.
    """
    candidates = [text.strip()]
    candidates.extend(block.strip() for block in _FENCE.findall(text))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = decode(candidate)
        except json.JSONDecodeError:
            continue
        except ValueError:
            # An invalid answer must not be rescued by a later fenced object.
            return None
        if isinstance(value, dict):
            return cast(dict[str, object], value)
    return None


def embedded_object(text: str) -> dict[str, object] | None:
    """The whole answer as a non-empty JSON object, else the first non-empty object starting at a ``{``.

    Fences are not special: their object is found by the ``{`` scan, unlike ``fenced_object``.
    """
    stripped = text.strip()
    try:
        whole = decode(stripped)
    except json.JSONDecodeError:
        pass
    except ValueError:
        return None
    else:
        if isinstance(whole, dict) and whole:
            return cast(dict[str, object], whole)
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        except ValueError:
            # Do not salvage a nested object after rejecting the outer answer.
            return None
        if isinstance(value, dict) and value:
            return cast(dict[str, object], value)
    return None


def answer_payload(text: str, shape: ResultShape) -> tuple[ReceiptStatus, Payload]:
    if shape == "text":
        return ("success", text) if text.strip() else ("response_failed", None)
    parsed = fenced_object(text)
    return ("success", parsed) if parsed is not None else ("response_failed", None)


def failure_status(*texts: str) -> ReceiptStatus | None:
    combined = "\n".join(text for text in texts if text)
    if not combined.strip():
        return None
    if _AUTH.search(combined):
        return "awaiting_authentication"
    if _APPROVAL.search(combined):
        return "awaiting_approval"
    return None


def receipt_for(request: Request, status: ReceiptStatus, payload: Payload = None) -> Receipt:
    return Receipt(
        run_id=request.run_id,
        request_id=request.request_id,
        agent_id=request.agent_id,
        role=request.role,
        status=status,
        payload=payload,
    )
