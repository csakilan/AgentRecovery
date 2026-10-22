"""Preserve unpaired surrogates in the graph's JSON-native channel values.

The pinned JsonPlusSerializer deliberately replaces them with '?'. Keep its normal
serialization for other values; use the existing ASCII codec only where needed.
The type tag belongs to the saver, never to a model-controlled dictionary.
"""

from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from lab.protocol.evidence import pack_evidence, unpack_evidence

_ASCII_TYPE = "arl-json-ascii-v1"


def _has_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    if isinstance(value, dict):
        return any(_has_surrogate(k) or _has_surrogate(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_surrogate(item) for item in value)
    return False


def _json_native(value: Any) -> bool:
    if type(value) in (str, int, float, bool, type(None)):
        return True
    if type(value) is list:
        return all(_json_native(item) for item in value)
    if type(value) is dict:
        return all(type(k) is str and _json_native(v) for k, v in value.items())
    return False


class LosslessJsonPlusSerializer(JsonPlusSerializer):
    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        if _has_surrogate(obj):
            if not _json_native(obj):
                raise TypeError("surrogate-containing graph values must be JSON-native")
            return _ASCII_TYPE, pack_evidence(obj)["payload"].encode("ascii")
        return super().dumps_typed(obj)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if data[0] == _ASCII_TYPE:
            return unpack_evidence(
                {"encoding": "json-ascii-v1", "payload": data[1].decode("ascii")}
            )
        return super().loads_typed(data)
