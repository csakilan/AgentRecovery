import copy

from lab.agent.openrouter import parse_completion, to_openai_messages, to_openai_tools
from lab.agent.tools import tool_schemas
from lab.protocol.task import Profile


def test_tool_schema_is_converted_to_openai_function_shape():
    src = tool_schemas(Profile.CONTROLLED, status_available=True)
    out = to_openai_tools(src)
    assert len(out) == len(src)
    charge = next(t for t in out if t["function"]["name"] == "charge")
    assert charge["type"] == "function"
    # input_schema becomes parameters; name and description carried over verbatim.
    assert charge["function"]["parameters"] == next(
        t["input_schema"] for t in src if t["name"] == "charge"
    )
    assert charge["function"]["description"] == next(
        t["description"] for t in src if t["name"] == "charge"
    )
    # Conversion does not mutate the caller's list.
    assert "input_schema" in src[0] and "function" not in src[0]


def test_assistant_and_tool_messages_become_openai_wire_shape():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "name": "charge", "arguments": {"amount": 49.99}}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "charge", "content": "Charge succeeded."},
    ]
    out = to_openai_messages(msgs)
    assert out[0] == {"role": "system", "content": "s"}
    tc = out[2]["tool_calls"][0]
    assert tc == {
        "id": "c1",
        "type": "function",
        "function": {"name": "charge", "arguments": '{"amount":49.99}'},
    }
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "Charge succeeded."}


def test_parse_completion_builds_a_model_turn():
    body = {
        "choices": [
            {
                "message": {
                    "content": "ok",
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {
                                "name": "report_done",
                                "arguments": '{"outcome":"succeeded"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    turn = parse_completion(body)
    assert turn.malformed is False
    assert turn.text == "ok"
    assert turn.tool_calls[0].name == "report_done"
    assert turn.tool_calls[0].arguments == {"outcome": "succeeded"}
    assert turn.input_tokens == 11 and turn.output_tokens == 3


def test_parse_completion_marks_unparseable_arguments_malformed_without_raising():
    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "charge", "arguments": "{not json"},
                        }
                    ],
                }
            }
        ]
    }
    turn = parse_completion(body)
    assert turn.malformed is True
    assert turn.tool_calls == ()


def test_parse_completion_marks_missing_choices_malformed():
    turn = parse_completion({"error": {"message": "upstream"}})
    assert turn.malformed is True
    assert turn.raw == {"error": {"message": "upstream"}}


def test_converted_parameters_are_a_deep_copy_of_the_source_schema():
    src = tool_schemas(Profile.CONTROLLED, status_available=True)
    before = copy.deepcopy(src)
    out = to_openai_tools(src)
    charge = next(t for t in out if t["function"]["name"] == "charge")
    charge["function"]["parameters"]["properties"]["injected"] = {"type": "string"}
    assert src == before


def test_non_dict_body_is_malformed_and_wrapped_under_raw():
    for body in (["not", "a", "dict"], 42):
        turn = parse_completion(body)
        assert turn.malformed is True
        assert turn.tool_calls == ()
        assert turn.text is None
        assert turn.raw == {"unparseable_body": body}


def test_deeply_nested_arguments_are_malformed_without_raising():
    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "charge", "arguments": "[" * 100000},
                        }
                    ],
                }
            }
        ]
    }
    turn = parse_completion(body)
    assert turn.malformed is True
    assert turn.tool_calls == ()
