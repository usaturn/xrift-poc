"""Bounded, non-following reads of machine receipts after process shutdown."""

import os
import stat
from pathlib import Path

from tools.agent_runner.models import Receipt

MAX_RECEIPT_BYTES = 16 * 1024 * 1024


def read_receipt(path: Path) -> Receipt | None:
    """Reject links, special files and excessive content before invoking adapters."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT_BYTES:
            raise ValueError("invalid receipt")
        content = stream.read(MAX_RECEIPT_BYTES + 1)
        if len(content) > MAX_RECEIPT_BYTES:
            raise ValueError("invalid receipt")
    return Receipt.model_validate_json(content)
