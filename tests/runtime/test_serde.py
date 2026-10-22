import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from lab.runtime.serde import LosslessJsonPlusSerializer


@pytest.mark.parametrize(
    "value",
    [
        "nul\0surrogate\ud800",
        {"messages": [{"content": "\ud800"}]},
        {"encoding": "json-ascii-v1", "payload": "literal \ud800"},
    ],
)
def test_checkpoint_codec_restores_original_json_native_value(value):
    serializer = LosslessJsonPlusSerializer()
    encoded = serializer.dumps_typed(value)
    assert encoded[0] == "arl-json-ascii-v1"
    assert serializer.loads_typed(encoded) == value


def test_normal_checkpoint_values_keep_library_serializer():
    serializer = LosslessJsonPlusSerializer()
    value = {"content": "normal Unicode é", "binary": b"bytes", "tuple": (1, 2)}
    encoded = serializer.dumps_typed(value)
    assert encoded[0] == "msgpack"
    original = JsonPlusSerializer()
    assert encoded == original.dumps_typed(value)
    assert serializer.loads_typed(encoded) == original.loads_typed(encoded)


def test_unsupported_surrogate_container_refuses_silent_loss():
    with pytest.raises(TypeError, match="JSON-native"):
        LosslessJsonPlusSerializer().dumps_typed(("\ud800",))
