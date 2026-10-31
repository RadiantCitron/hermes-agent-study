"""도구 미들웨어 체인 테스트 (구현지시서 020 시나리오 1~13)."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import (
    ToolCall,
    ToolDispatcher,
    create_child_dispatcher,
)

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo back integer x.",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    },
}


def _create_echo_dispatcher(tracker: list[int] | None = None) -> ToolDispatcher:
    disp = ToolDispatcher()

    def _echo_handler(x: int) -> str:
        if tracker is not None:
            tracker.append(x)
        return json.dumps({"success": True, "result": x})

    disp.register_tool("echo", _echo_handler, ECHO_SCHEMA)
    return disp


def test_scenario_01_no_middleware_behavior_identical():
    """시나리오 1: 미들웨어 없이 handle_function_call 호출 시 기존과 같은 결과/오류 반환."""
    disp = _create_echo_dispatcher()

    # 정상 호출
    res1 = json.loads(disp.handle_function_call("echo", {"x": 10}))
    assert res1["success"] is True
    assert res1["result"] == 10

    # 알 수 없는 도구 호출
    res_unknown = json.loads(disp.handle_function_call("unknown_tool", {}))
    assert res_unknown["success"] is False
    assert "Unknown tool 'unknown_tool'." in res_unknown["error"]

    # 필수 인자 누락 호출
    res_missing = json.loads(disp.handle_function_call("echo", {}))
    assert res_missing["success"] is False
    assert "missing required argument 'x'" in res_missing["error"]


def test_scenario_02_multiple_middleware_onion_order():
    """시나리오 2: 미들웨어 A, B 등록 시 진입/복귀 순서(양파 구조) 검증."""
    disp = _create_echo_dispatcher()
    log: list[str] = []

    def mw_a(call: ToolCall, next_fn) -> str:
        log.append("A:in")
        res = next_fn(call)
        log.append("A:out")
        return res

    def mw_b(call: ToolCall, next_fn) -> str:
        log.append("B:in")
        log.append("tool")  # 핸들러 직전 기록 대용
        res = next_fn(call)
        log.append("B:out")
        return res

    disp.add_middleware(mw_a)
    disp.add_middleware(mw_b)

    raw_res = disp.handle_function_call("echo", {"x": 1})
    res = json.loads(raw_res)
    assert res["success"] is True
    assert res["result"] == 1
    assert log == ["A:in", "B:in", "tool", "B:out", "A:out"]


def test_scenario_03_middleware_short_circuit():
    """시나리오 3: 미들웨어가 next를 호출하지 않고 차단 시 핸들러 미호출 및 반환값 전달 검증."""
    tracker: list[int] = []
    disp = _create_echo_dispatcher(tracker)

    def blocking_mw(call: ToolCall, next_fn) -> str:
        return json.dumps({"success": False, "error": "blocked"})

    disp.add_middleware(blocking_mw)

    raw_res = disp.handle_function_call("echo", {"x": 1})
    res = json.loads(raw_res)
    assert res["success"] is False
    assert res["error"] == "blocked"
    assert len(tracker) == 0


def test_scenario_04_middleware_modifies_arguments():
    """시나리오 4: 미들웨어가 replace(call, args=...)로 인자를 변경하여 next 호출 시 핸들러에 전달 검증."""
    tracker: list[int] = []
    disp = _create_echo_dispatcher(tracker)

    def mutating_mw(call: ToolCall, next_fn) -> str:
        new_call = replace(call, args={"x": 2})
        return next_fn(new_call)

    disp.add_middleware(mutating_mw)

    raw_res = disp.handle_function_call("echo", {"x": 1})
    res = json.loads(raw_res)
    assert res["success"] is True
    assert res["result"] == 2
    assert tracker == [2]


def test_scenario_05_middleware_invalid_args_rejected_by_inner_validation():
    """시나리오 5: 미들웨어가 스키마에 어긋나는 인자로 변경 시 핸들러 미호출 및 스키마 검증 오류 반환."""
    tracker: list[int] = []
    disp = _create_echo_dispatcher(tracker)

    def bad_args_mw(call: ToolCall, next_fn) -> str:
        new_call = replace(call, args={"x": "문자"})
        return next_fn(new_call)

    disp.add_middleware(bad_args_mw)

    raw_res = disp.handle_function_call("echo", {"x": 1})
    res = json.loads(raw_res)
    assert res["success"] is False
    assert "Invalid arguments for tool 'echo'" in res["error"]
    assert len(tracker) == 0


def test_scenario_06_middleware_exception_caught_as_tool_failure():
    """시나리오 6: 미들웨어가 예외를 던지면 포획되어 success=false JSON으로 변환."""
    disp = _create_echo_dispatcher()

    def exploding_mw(call: ToolCall, next_fn) -> str:
        raise ValueError("boom")

    disp.add_middleware(exploding_mw)

    raw_res = disp.handle_function_call("echo", {"x": 1})
    res = json.loads(raw_res)
    assert res["success"] is False
    assert "Tool 'echo' failed" in res["error"]
    assert "boom" in res["error"]


def test_scenario_07_middleware_non_string_result_handled():
    """시나리오 7: 미들웨어가 str이 아닌 None 또는 dict를 반환할 때 적절한 오류 JSON 반환."""
    # 1. None 반환
    disp1 = _create_echo_dispatcher()
    disp1.add_middleware(lambda call, next_fn: None)  # type: ignore[return-value]
    raw_res1 = disp1.handle_function_call("echo", {"x": 1})
    res1 = json.loads(raw_res1)
    assert res1["success"] is False
    assert "middleware returned a non-string result" in res1["error"]

    # 2. dict 반환
    disp2 = _create_echo_dispatcher()
    disp2.add_middleware(lambda call, next_fn: {"not": "string"})  # type: ignore[return-value]
    raw_res2 = disp2.handle_function_call("echo", {"x": 1})
    res2 = json.loads(raw_res2)
    assert res2["success"] is False
    assert "middleware returned a non-string result" in res2["error"]


def test_scenario_08_middleware_modifies_result_string():
    """시나리오 8: 미들웨어가 next 반환 문자열을 후가공하여 반환 검증."""
    disp = _create_echo_dispatcher()

    def decorating_mw(call: ToolCall, next_fn) -> str:
        raw = next_fn(call)
        data = json.loads(raw)
        data["decorated"] = True
        return json.dumps(data)

    disp.add_middleware(decorating_mw)

    raw_res = disp.handle_function_call("echo", {"x": 1})
    res = json.loads(raw_res)
    assert res["success"] is True
    assert res["result"] == 1
    assert res["decorated"] is True


def test_scenario_09_middleware_observes_unknown_tool():
    """시나리오 9: 미들웨어는 알 수 없는 도구 호출도 먼저 수신하며, 안쪽에서 Unknown tool 오류 수신."""
    disp = _create_echo_dispatcher()
    log: list[str] = []

    def tracking_mw(call: ToolCall, next_fn) -> str:
        log.append(f"in:{call.name}")
        res = next_fn(call)
        log.append(f"out:{call.name}")
        return res

    disp.add_middleware(tracking_mw)

    raw_res = disp.handle_function_call("non_existent_tool", {})
    res = json.loads(raw_res)
    assert res["success"] is False
    assert "Unknown tool 'non_existent_tool'." in res["error"]
    assert log == ["in:non_existent_tool", "out:non_existent_tool"]


def test_scenario_10_task_id_passed_to_tool_call():
    """시나리오 10: handle_function_call에 전달된 task_id가 ToolCall.task_id로 미들웨어에 전달."""
    disp = _create_echo_dispatcher()
    captured_task_id: list[str | None] = []

    def task_id_mw(call: ToolCall, next_fn) -> str:
        captured_task_id.append(call.task_id)
        return next_fn(call)

    disp.add_middleware(task_id_mw)

    disp.handle_function_call("echo", {"x": 1}, task_id="t1")
    assert captured_task_id == ["t1"]


def test_scenario_11_child_dispatcher_has_no_parent_middleware():
    """시나리오 11: 부모 디스패처에 미들웨어를 등록해도 create_child_dispatcher()로 만든 자식은 미들웨어 없음."""
    parent_disp = _create_echo_dispatcher()
    parent_log: list[str] = []
    parent_disp.add_middleware(lambda call, next_fn: (parent_log.append("parent"), next_fn(call))[1])

    child_disp = create_child_dispatcher()
    # 자식 디스패처의 terminal 등 호출 (잘못된 인자여도 디스패처 체인은 탐)
    child_disp.handle_function_call("terminal", {})
    assert parent_log == []


def test_scenario_12_agent_integration_with_middleware(tmp_path: Path):
    """시나리오 12: 미들웨어가 장착된 디스패처로 HermesAgent 턴 실행 시 미들웨어가 정확히 1회 진입하고 tool 메시지 기록."""
    db = SessionDB(tmp_path / "test.db")
    mw_log: list[str] = []

    disp = _create_echo_dispatcher()

    def agent_mw(call: ToolCall, next_fn) -> str:
        mw_log.append(call.name)
        return next_fn(call)

    disp.add_middleware(agent_mw)

    tool_call_payload = [
        {"id": "call_1", "function": {"name": "echo", "arguments": '{"x": 42}'}}
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done", tool_calls=None),
    ]
    client = MockLLMClient(responses)

    agent = HermesAgent(
        session_db=db,
        dispatcher=disp,
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("hello")
    assert res["stopped_reason"] == "completed"
    assert mw_log == ["echo"]

    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"])["result"] == 42


def test_scenario_13_handlers_access_single_entry_point():
    """시나리오 13: src/hermes_core 전체에서 _handlers를 읽거나 쓰는 곳은 dispatcher.py 단일 파일 내뿐임을 검증."""
    repo_root = Path(__file__).resolve().parents[2]
    src_core = repo_root / "src" / "hermes_core"
    matched_files: list[tuple[str, int, str]] = []

    for py_file in src_core.rglob("*.py"):
        rel_path = py_file.relative_to(repo_root).as_posix()
        for idx, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if "_handlers" in line:
                matched_files.append((rel_path, idx, line.strip()))

    for rel_path, line_no, content in matched_files:
        assert rel_path == "src/hermes_core/tools/dispatcher.py"

    assert len(matched_files) == 3
