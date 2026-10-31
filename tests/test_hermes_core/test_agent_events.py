"""에이전트 이벤트 출력 계층 테스트 (구현지시서 010 시나리오 1~8)."""

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.events import (
    EVENT_TOOL_CALL,
    AgentEvent,
    ConsoleEventSink,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher


def _create_test_agent(
    tmp_path: Path,
    responses: list[MockLLMResponse],
    quiet_mode: bool = False,
    event_sink: Any = None,
) -> tuple[HermesAgent, SessionDB]:
    """테스트용 에코 도구가 등록된 HermesAgent와 SessionDB 인스턴스 생성."""
    db = SessionDB(tmp_path / "test.db")
    dispatcher = ToolDispatcher()

    # echo 도구 등록
    echo_schema = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo back arguments.",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
            },
        },
    }

    def _echo_handler(x: int = 0) -> str:
        return json.dumps({"success": True, "x": x})

    dispatcher.register_tool("echo", _echo_handler, echo_schema)
    client = MockLLMClient(responses)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        quiet_mode=quiet_mode,
        event_sink=event_sink,
    )
    return agent, db


def test_scenario_01_default_console_event_sink_output(tmp_path: Path, capsys: pytest.CaptureFixture):
    """시나리오 1: 기본값(quiet_mode=False, event_sink 없음)으로 도구 호출 1건 capsys 실행 시 이전과 동일한 문자열 출력."""
    tool_call_payload = [
        {"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done", tool_calls=None),
    ]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=False, event_sink=None)

    agent.run_conversation("hello")
    captured = capsys.readouterr()
    assert captured.out == '  [Tool Call] echo({"x": 1})\n'


def test_scenario_02_quiet_mode_default_no_output(tmp_path: Path, capsys: pytest.CaptureFixture):
    """시나리오 2: quiet_mode=True, event_sink 없음으로 같은 턴 실행 시 표준출력이 빈 문자열."""
    tool_call_payload = [
        {"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done", tool_calls=None),
    ]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=True, event_sink=None)

    agent.run_conversation("hello")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_scenario_03_explicit_event_sink_receives_in_quiet_mode(tmp_path: Path, capsys: pytest.CaptureFixture):
    """시나리오 3: quiet_mode=True, event_sink=events.append 로 실행 시 명시된 sink가 이벤트를 정상 수신."""
    events: list[AgentEvent] = []
    tool_call_payload = [
        {"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done", tool_calls=None),
    ]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=True, event_sink=events.append)

    agent.run_conversation("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert events == [AgentEvent("tool_call", {"name": "echo", "arguments": '{"x": 1}'})]


def test_scenario_04_multiple_tool_calls_order_preserved(tmp_path: Path):
    """시나리오 4: 같은 응답에 도구 호출 2건 담고 event_sink=events.append 시 순서대로 2건 방출."""
    events: list[AgentEvent] = []
    tool_call_payload = [
        {"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}},
        {"id": "c2", "function": {"name": "echo", "arguments": '{"x": 2}'}},
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done", tool_calls=None),
    ]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=True, event_sink=events.append)

    agent.run_conversation("hello")
    assert len(events) == 2
    assert events[0] == AgentEvent("tool_call", {"name": "echo", "arguments": '{"x": 1}'})
    assert events[1] == AgentEvent("tool_call", {"name": "echo", "arguments": '{"x": 2}'})


def test_scenario_05_sink_exception_swallowed_and_warning_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """시나리오 5: event_sink가 항상 RuntimeError를 던져도 정상 완료되고 DB/메시지 정상이며 로그에 경고 남음."""
    def _broken_sink(event: AgentEvent) -> None:
        raise RuntimeError("Sink error!")

    tool_call_payload = [
        {"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done", tool_calls=None),
    ]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=False, event_sink=_broken_sink)

    with caplog.at_level("WARNING"):
        res = agent.run_conversation("hello")

    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "done"

    # messages에 tool 메시지 존재 확인
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"])["success"] is True

    # DB 원장 확인
    db_msgs = db.get_messages(agent.session_id)
    db_tool_msgs = [m for m in db_msgs if m.get("role") == "tool"]
    assert len(db_tool_msgs) == 1

    # caplog 경고 확인
    assert any("EventSink failed for event 'tool_call': Sink error!" in record.message for record in caplog.records)


def test_scenario_06_no_tool_calls_no_output(tmp_path: Path, capsys: pytest.CaptureFixture):
    """시나리오 6: 도구 호출 없이 텍스트만 답하는 턴, 기본값 capsys 실행 시 출력 없음."""
    responses = [MockLLMResponse(content="just text response", tool_calls=None)]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=False, event_sink=None)

    agent.run_conversation("hello")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_scenario_07_invalid_json_arguments_emits_before_parsing(tmp_path: Path):
    """시나리오 7: 인자가 올바른 JSON이 아닌 경우에도 이벤트가 원본 그대로 방출되고 도구 메시지는 오류 담음."""
    events: list[AgentEvent] = []
    tool_call_payload = [
        {"id": "c1", "function": {"name": "echo", "arguments": "{bad"}}
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_call_payload),
        MockLLMResponse(content="done after error", tool_calls=None),
    ]
    agent, db = _create_test_agent(tmp_path, responses, quiet_mode=True, event_sink=events.append)

    res = agent.run_conversation("hello")
    assert len(events) == 1
    assert events[0].kind == EVENT_TOOL_CALL
    assert events[0].data["name"] == "echo"
    assert events[0].data["arguments"] == "{bad"

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["success"] is False
    assert "Invalid JSON arguments" in payload["error"]


def test_scenario_08_console_event_sink_ignores_unknown_kind(capsys: pytest.CaptureFixture):
    """시나리오 8: ConsoleEventSink()(AgentEvent("other", {})) 호출 시 출력 없음 및 예외 없음."""
    sink = ConsoleEventSink()
    sink(AgentEvent("other", {"foo": "bar"}))
    captured = capsys.readouterr()
    assert captured.out == ""
