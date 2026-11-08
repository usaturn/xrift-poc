"""正規化 JSON のバイト列とハッシュ（Hjson・Hfile）。"""

import hashlib
import json


def canonical_bytes(value: object) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8")


def hjson(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def hfile(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
