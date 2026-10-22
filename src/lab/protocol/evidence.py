"""Lossless ASCII envelopes for JSON values that PostgreSQL cannot directly store.

Only versioned storage boundaries use this codec. Model messages stay unchanged;
legacy records must not be decoded by guessing whether they resemble an envelope.
"""

import json
from typing import Any

_ENCODING = "json-ascii-v1"


def pack_evidence(value: Any) -> dict[str, str]:
    return {
        "encoding": _ENCODING,
        "payload": json.dumps(value, ensure_ascii=True, separators=(",", ":")),
    }


def unpack_evidence(envelope: dict[str, str]) -> Any:
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"encoding", "payload"}
        or envelope["encoding"] != _ENCODING
        or not isinstance(envelope["payload"], str)
    ):
        raise ValueError("invalid evidence envelope")
    return json.loads(envelope["payload"])


def pack_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def unpack_text(value: str) -> str:
    decoded = json.loads(value)
    if not isinstance(decoded, str):
        raise ValueError("encoded text must contain a JSON string")
    return decoded
