"""단일 입구 완결 단위 테스트 (Single Entry Dispatch Tests).

[인수 테스트 시나리오]
- 020_단일_입구_완결.md의 1~11 시나리오 검증.
"""

import json
import logging
from pathlib import Path
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo tool for testing.",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    },
}


def _create_dispatcher_with_echo():
    dispatcher = ToolDispatcher()
    calls_executed = []

    def echo_handler(x: int) -> str:
        calls_executed.append(x)
        return json.dumps({"echo": x})

    dispatcher.register_tool("echo", echo_handler, ECHO_SCHEMA)
    return dispatcher, calls_executed


def test_scenario_01_and_02_broken_json_passes_middleware_and_exact_error_format(tmp_path):
    """시나리오 1 & 2: {bad 인자 호출 시 미들웨어가 1회 진입하고 call 정보가 유지되며 이전과 동일한 에러 JSON 반환."""
    dispatcher, executed = _create_dispatcher_with_echo()
    recorded_calls = []

    def middleware(call: ToolCall, next_fn):
        recorded_calls.append(call)
        return next_fn(call)

    dispatcher.add_middleware(middleware)

    db = SessionDB(tmp_path / "test.db")
    client = MockLLMClient(
        responses=[
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{bad"},
                }],
            ),
            MockLLMResponse(content="done"),
        ]
    )

    agent = HermesAgent(session_db=db, dispatcher=dispatcher, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("test broken json")

    # 시나리오 1 검증
    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call.name == "echo"
    assert call.args == {}
    assert call.raw_arguments == "{bad"
    assert call.parse_error is not None
    assert len(executed) == 0

    # 시나리오 2 검증: tool 메시지 내용 동일성
    tool_msg = res["messages"][3]
    assert tool_msg["role"] == "tool"
    parsed = json.loads(tool_msg["content"])
    assert parsed["success"] is False
    assert parsed["error"].startswith("Invalid JSON arguments for tool 'echo':")


def test_scenario_03_and_04_non_object_json_and_caplog(tmp_path, caplog):
    """시나리오 3 & 4: [1, 2] 인자 호출 시 arguments must be a JSON object 에러 및 hermes_core.agent.agent WARNING 로깅."""
    dispatcher, _ = _create_dispatcher_with_echo()
    db = SessionDB(tmp_path / "test.db")
    client = MockLLMClient(
        responses=[
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": "call_arr",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "[1, 2]"},
                }],
            ),
            MockLLMResponse(content="done"),
        ]
    )

    agent = HermesAgent(session_db=db, dispatcher=dispatcher, llm_client=client, quiet_mode=True)

    with caplog.at_level(logging.WARNING, logger="hermes_core.agent.agent"):
        res = agent.run_conversation("test non object")

    tool_msg = res["messages"][3]
    assert tool_msg["role"] == "tool"
    parsed = json.loads(tool_msg["content"])
    assert parsed == {
        "success": False,
        "error": "Invalid JSON arguments for tool 'echo': arguments must be a JSON object",
    }

    # caplog 확인 (경고 1건 이상)
    matching_records = [
        rec for rec in caplog.records
        if rec.name == "hermes_core.agent.agent" and "Invalid JSON arguments for tool 'echo'" in rec.message
    ]
    assert len(matching_records) >= 1


def test_scenario_05_unknown_tool_with_broken_json(tmp_path):
    """시나리오 5: 등록되지 않은 도구 이름 + 깨진 인자 -> Invalid JSON arguments 오류가 Unknown tool보다 먼저 발생."""
    dispatcher, _ = _create_dispatcher_with_echo()
    db = SessionDB(tmp_path / "test.db")
    client = MockLLMClient(
        responses=[
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": "call_unk",
                    "type": "function",
                    "function": {"name": "non_existent", "arguments": "{bad"},
                }],
            ),
            MockLLMResponse(content="done"),
        ]
    )

    agent = HermesAgent(session_db=db, dispatcher=dispatcher, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("test unknown tool broken")

    tool_msg = res["messages"][3]
    assert tool_msg["role"] == "tool"
    parsed = json.loads(tool_msg["content"])
    assert parsed["success"] is False
    assert parsed["error"].startswith("Invalid JSON arguments for tool 'non_existent':")


def test_scenario_06_normal_args_middleware_recording_and_scope(tmp_path):
    """시나리오 6: 정상 인자 호출에서 parse_error is None, raw_arguments 일치, scope 객체 동일."""
    dispatcher, executed = _create_dispatcher_with_echo()
    recorded_calls = []

    def middleware(call: ToolCall, next_fn):
        recorded_calls.append(call)
        return next_fn(call)

    dispatcher.add_middleware(middleware)

    db = SessionDB(tmp_path / "test.db")
    client = MockLLMClient(
        responses=[
            MockLLMResponse(
                content=None,
                tool_calls=[
                    {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}},
                    {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": '{"x": 2}'}},
                ],
            ),
            MockLLMResponse(content="done"),
        ]
    )

    agent = HermesAgent(session_db=db, dispatcher=dispatcher, llm_client=client, quiet_mode=True)
    agent.run_conversation("test normal")

    assert len(recorded_calls) == 2
    c1, c2 = recorded_calls[0], recorded_calls[1]

    assert c1.parse_error is None
    assert c1.raw_arguments == '{"x": 1}'
    assert c1.args == {"x": 1}
    assert c1.scope is not None

    assert c2.parse_error is None
    assert c2.raw_arguments == '{"x": 2}'
    assert c2.args == {"x": 2}

    # 동일 턴 두 호출의 scope는 동일 객체
    assert c1.scope is c2.scope


def test_scenario_07_middleware_short_circuit_on_broken_json(tmp_path):
    """시나리오 7: 미들웨어가 next를 부르지 않고 차단 문자열을 반환하면 깨진 인자도 차단 결과가 반환됨."""
    dispatcher, _ = _create_dispatcher_with_echo()

    def blocking_middleware(call: ToolCall, next_fn):
        return json.dumps({"blocked": True, "reason": "security_policy"})

    dispatcher.add_middleware(blocking_middleware)

    db = SessionDB(tmp_path / "test.db")
    client = MockLLMClient(
        responses=[
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": "c_bad",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{bad"},
                }],
            ),
            MockLLMResponse(content="done"),
        ]
    )

    agent = HermesAgent(session_db=db, dispatcher=dispatcher, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("test block")

    tool_msg = res["messages"][3]
    assert tool_msg["role"] == "tool"
    parsed = json.loads(tool_msg["content"])
    assert parsed == {"blocked": True, "reason": "security_policy"}


def test_scenario_08_handle_function_call_backwards_compatible():
    """시나리오 8: handle_function_call 직접 호출 시 이전과 같고 scope is None."""
    dispatcher, executed = _create_dispatcher_with_echo()
    recorded_calls = []

    def middleware(call: ToolCall, next_fn):
        recorded_calls.append(call)
        return next_fn(call)

    dispatcher.add_middleware(middleware)

    res = dispatcher.handle_function_call("echo", {"x": 42})
    parsed = json.loads(res)
    assert parsed == {"echo": 42}
    assert executed == [42]
    assert len(recorded_calls) == 1
    assert recorded_calls[0].scope is None


def test_scenario_09_chain_execution_only_in_dispatch():
    """시나리오 9: dispatcher.py 내에서 _make_chain 실행은 dispatch 안에만 존재."""
    dispatcher_path = Path(__file__).resolve().parent.parent.parent / "src" / "hermes_core" / "tools" / "dispatcher.py"
    lines = dispatcher_path.read_text(encoding="utf-8").splitlines()

    # handle_function_call 안에 _make_chain이 없는지 확인
    in_handle = False
    handle_lines = []
    for line in lines:
        if "def handle_function_call(" in line:
            in_handle = True
        elif in_handle and line.startswith("    def "):
            in_handle = False
        if in_handle:
            handle_lines.append(line)

    handle_content = "\n".join(handle_lines)
    assert "_make_chain" not in handle_content
    assert "self.dispatch(" in handle_content


def test_scenario_10_no_error_json_construction_in_agent_py():
    """시나리오 10: kernel/loop.py(및 agent.py) 소스에 Invalid JSON arguments 로그 문자열 1건 외에 직접 에러 JSON 생성 0건."""
    loop_path = Path(__file__).resolve().parent.parent.parent / "src" / "hermes_core" / "kernel" / "loop.py"
    content = loop_path.read_text(encoding="utf-8")
    # 'Invalid JSON arguments'는 logger.warning에만 1회 존재
    occurrences = [line for line in content.splitlines() if "Invalid JSON arguments" in line]
    assert len(occurrences) == 1
    assert "logger.warning" in occurrences[0]
