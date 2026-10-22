import json

import pytest


@pytest.mark.parametrize(
    "value",
    [
        {"key": "a\x00b", "nested": ["\ud800", "\\u0000", None]},
        {"encoding": "json-ascii-v1", "payload": '{"claimed":"envelope"}'},
        [1, 2.5, True, False, None, "你好"],
        "string",
    ],
)
def test_evidence_round_trip_keeps_unstorable_and_envelope_shaped_input(value):
    from lab.protocol.evidence import pack_evidence, unpack_evidence

    packed = pack_evidence(value)
    assert packed["encoding"] == "json-ascii-v1"
    assert packed["payload"].isascii()
    assert "\x00" not in packed["payload"]
    assert unpack_evidence(json.loads(json.dumps(packed))) == value


@pytest.mark.parametrize(
    "envelope",
    [
        {"encoding": "unrecognized", "payload": "{}"},
        {"encoding": "json-ascii-v1", "payload": "{}", "extra": "bad"},
        {"encoding": "json-ascii-v1", "payload": 3},
        {"encoding": "json-ascii-v1", "payload": "{"},
        {},
    ],
)
def test_bad_evidence_envelopes_are_not_silently_accepted(envelope):
    from lab.protocol.evidence import unpack_evidence

    with pytest.raises(ValueError):
        unpack_evidence(envelope)


@pytest.mark.parametrize("value", ["\x00\udfff", "\\u0000", "hello", "你好"])
def test_text_encoding_round_trips_without_unstorable_database_characters(value):
    from lab.protocol.evidence import pack_text, unpack_text

    encoded = pack_text(value)
    assert encoded.isascii() and "\x00" not in encoded
    assert unpack_text(encoded) == value


def test_text_decoder_refuses_json_that_is_not_a_string():
    from lab.protocol.evidence import unpack_text

    with pytest.raises(ValueError):
        unpack_text("{}")
