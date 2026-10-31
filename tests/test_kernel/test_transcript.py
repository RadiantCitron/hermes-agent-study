"""대화 규칙 및 메시지 조립 순수 함수 시험 (시나리오 21~23)."""

from typing import Any

from hermes_core.kernel.transcript import (
    assistant_message,
    first_message_unchanged,
    parse_tool_arguments,
    tool_result_message,
)


def test_scenario_21_first_message_unchanged() -> None:
    """시나리오 21: first_message_unchanged."""
    s = {"role": "system", "content": "S"}
    u = {"role": "user", "content": "hi"}

    assert first_message_unchanged([], []) is True
    assert first_message_unchanged([s], []) is False
    assert first_message_unchanged([], [s]) is False
    assert first_message_unchanged([s], [s, u]) is True
    assert first_message_unchanged([s], [{"role": "system", "content": "T"}]) is False
    assert first_message_unchanged([s], [{"role": "user", "content": "S"}]) is False


def test_scenario_22_parse_tool_arguments() -> None:
    """시나리오 22: parse_tool_arguments."""
    # None -> ({}, None)
    args, err = parse_tool_arguments(None)
    assert args == {} and err is None

    # "" -> ({}, None)
    args, err = parse_tool_arguments("")
    assert args == {} and err is None

    # "  " -> ({}, None)
    args, err = parse_tool_arguments("  ")
    assert args == {} and err is None

    # '{"a": 1}' -> ({"a": 1}, None)
    args, err = parse_tool_arguments('{"a": 1}')
    assert args == {"a": 1} and err is None

    # {"a": 1} -> ({"a": 1}, None)
    args, err = parse_tool_arguments({"a": 1})
    assert args == {"a": 1} and err is None

    # "{bad" -> (None, non-empty error string)
    args, err = parse_tool_arguments("{bad")
    assert args is None and bool(err)

    # "[1]" -> (None, "arguments must be a JSON object")
    args, err = parse_tool_arguments("[1]")
    assert args is None and err == "arguments must be a JSON object"

    # "3" -> (None, "arguments must be a JSON object")
    args, err = parse_tool_arguments("3")
    assert args is None and err == "arguments must be a JSON object"


def test_scenario_23_message_shapes() -> None:
    """시나리오 23: assistant_message 및 tool_result_message 형태."""
    tc: dict[str, Any] = {"id": "c1", "function": {"name": "f", "arguments": "{}"}}

    # assistant_message("t", None)
    m1 = assistant_message("t", None)
    assert m1 == {"role": "assistant", "content": "t"}

    # assistant_message(None, [tc])
    m2 = assistant_message(None, [tc])
    assert m2 == {"role": "assistant", "tool_calls": [tc]}

    # assistant_message("", [])
    m3 = assistant_message("", [])
    assert m3 == {"role": "assistant"}

    # tool_result_message("c", "r")
    m4 = tool_result_message("c", "r")
    assert m4 == {"role": "tool", "tool_call_id": "c", "content": "r"}
