"""도구 통로의 호출 전 인자 스키마 검증 테스트 (로드맵 02 / 구현지시서 010 인수 시나리오)."""

import json

import pytest

from hermes_core.tools.dispatcher import ToolDispatcher, create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore

BODY_RESULT = "body-called"

TEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "t",
        "description": "test tool",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove"]},
                "count": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
}


@pytest.fixture
def counted():
    """본체 호출 횟수를 세는 시험용 도구 `t`가 등록된 디스패처."""
    calls = []

    def body(**kwargs):
        calls.append(kwargs)
        return BODY_RESULT

    dispatcher = ToolDispatcher()
    dispatcher.register_tool("t", body, TEST_SCHEMA)
    return dispatcher, calls


def _rejected(dispatcher, calls, args, expected_word):
    raw = dispatcher.handle_function_call("t", args)
    result = json.loads(raw)
    assert result["success"] is False
    assert expected_word in result["error"]
    assert calls == []
    return result


def test_missing_required_argument_is_rejected(counted):  # 시나리오 1
    dispatcher, calls = counted
    _rejected(dispatcher, calls, {}, "action")


def test_value_outside_enum_is_rejected(counted):  # 시나리오 2
    dispatcher, calls = counted
    _rejected(dispatcher, calls, {"action": "zzz"}, "action")


def test_wrong_type_is_rejected(counted):  # 시나리오 3
    dispatcher, calls = counted
    _rejected(dispatcher, calls, {"action": "add", "count": "3"}, "count")


def test_bool_is_not_accepted_as_integer(counted):  # 시나리오 4
    dispatcher, calls = counted
    _rejected(dispatcher, calls, {"action": "add", "count": True}, "count")


def test_undefined_argument_is_rejected(counted):  # 시나리오 5
    dispatcher, calls = counted
    _rejected(dispatcher, calls, {"action": "add", "extra": 1}, "extra")


def test_valid_call_reaches_body_and_returns_its_result_unchanged(counted):  # 시나리오 6
    dispatcher, calls = counted
    out = dispatcher.handle_function_call("t", {"action": "add", "count": 2})
    assert out == BODY_RESULT
    assert len(calls) == 1


def test_empty_schema_skips_validation():  # 시나리오 7
    calls = []

    def body(**kwargs):
        calls.append(kwargs)
        return BODY_RESULT

    dispatcher = ToolDispatcher()
    dispatcher.register_tool("t", body, {})
    out = dispatcher.handle_function_call("t", {"a": 1})
    assert out == BODY_RESULT
    assert calls == [{"a": 1}]


def test_unknown_tool_is_reported_before_validation(counted):  # 시나리오 8
    dispatcher, calls = counted
    result = json.loads(dispatcher.handle_function_call("nope", {}))
    assert result["success"] is False
    assert "Unknown tool" in result["error"]
    assert calls == []


def test_default_memory_tool_normal_call_passes_validation(tmp_path):  # 시나리오 9 (회귀)
    store = MemoryStore(memory_dir=tmp_path / "memories")
    store.load_from_disk()
    dispatcher = create_default_dispatcher(memory_store=store)
    raw = dispatcher.handle_function_call(
        "memory", {"action": "add", "target": "memory", "content": "x"}
    )
    assert json.loads(raw)["success"] is True


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"action": "zzz"},
        {"action": "add", "count": "3"},
        {"action": "add", "count": True},
        {"action": "add", "extra": 1},
    ],
)
def test_rejection_is_json_with_only_success_and_error_keys(counted, args):  # 시나리오 10
    dispatcher, _ = counted
    result = json.loads(dispatcher.handle_function_call("t", args))
    assert set(result) == {"success", "error"}
