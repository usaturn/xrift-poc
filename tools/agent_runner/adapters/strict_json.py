"""Decode answers without silently overwriting keys or accepting non-JSON constants."""

import json


def _constant(value: str) -> object:
    raise ValueError("nonfinite JSON")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


_DECODER = json.JSONDecoder(parse_constant=_constant, object_pairs_hook=_object)


def decode(text: str) -> object:
    """Decode a whole document; duplicate keys and nonfinite constants raise ValueError."""
    return _DECODER.decode(text)


def raw_decode(text: str) -> tuple[object, int]:
    """Decode a leading value with the same checks, returning its end offset."""
    return _DECODER.raw_decode(text)
