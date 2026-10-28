"""에이전트 루프 신호 테스트: 인자 읽기 실패 처리와 종료 사유 (로드맵 02 / 구현지시서 020 인수 시나리오)."""

import json
import logging

import pytest

from hermes_core.agent import HermesAgent, MockLLMClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher
from hermes_core.tools.memory_tool import MemoryStore

AGENT_LOGGER = "hermes_core.agent.agent"
FINAL_TEXT = "마무리"


def _tool_call(call_id, arguments):
    return {"id": call_id, "function": {"name": "t", "arguments": arguments}}


def _build(tmp_path, client, max_iterations=50):
    """가짜 모델, 메모리 DB, 임시 기억 폴더, 본체 호출 횟수를 세는 스키마 없는 도구 `t`."""
    calls = []

    def body(**kwargs):
        calls.append(kwargs)
        return "ok"

    dispatcher = ToolDispatcher()
    dispatcher.register_tool("t", body, {})
    db = SessionDB(":memory:")
    agent = HermesAgent(
        session_db=db,
        memory_store=MemoryStore(memory_dir=tmp_path / "memories"),
        dispatcher=dispatcher,
        llm_client=client,
        max_iterations=max_iterations,
        quiet_mode=True,
    )
    return agent, db, calls


def _tool_messages(result):
    return [m for m in result["messages"] if m["role"] == "tool"]


@pytest.mark.parametrize("bad_arguments", ["{bad json", "[1, 2]"])  # 시나리오 1, 2
def test_unreadable_arguments_are_returned_to_model_without_calling_tool(tmp_path, caplog, bad_arguments):
    client = MockLLMClient()
    client.queue_response(tool_calls=[_tool_call("c1", bad_arguments)])
    client.queue_response(content=FINAL_TEXT)
    agent, _, calls = _build(tmp_path, client)

    with caplog.at_level(logging.WARNING, logger=AGENT_LOGGER):
        result = agent.run_conversation("hi")

    assert calls == []
    tool_msg = _tool_messages(result)[0]
    body = json.loads(tool_msg["content"])
    assert body["success"] is False
    assert "JSON" in body["error"]
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert result["final_response"] == FINAL_TEXT


@pytest.mark.parametrize("blank_arguments", ["", "   ", None])  # 시나리오 3, 4, 9
def test_blank_arguments_mean_no_arguments(tmp_path, blank_arguments):
    client = MockLLMClient()
    client.queue_response(tool_calls=[_tool_call("c1", blank_arguments)])
    client.queue_response(content=FINAL_TEXT)
    agent, _, calls = _build(tmp_path, client)

    agent.run_conversation("hi")

    assert calls == [{}]


def test_every_tool_request_has_exactly_one_matching_tool_message(tmp_path):  # 시나리오 5
    client = MockLLMClient()
    client.queue_response(tool_calls=[_tool_call("c1", "{bad json"), _tool_call("c2", "[1, 2]")])
    client.queue_response(content=FINAL_TEXT)
    agent, db, _ = _build(tmp_path, client)

    result = agent.run_conversation("hi")

    tool_msgs = _tool_messages(result)
    assert sorted(m["tool_call_id"] for m in tool_msgs) == ["c1", "c2"]
    ledger_tool_msgs = [m for m in db.get_messages(agent.session_id) if m["role"] == "tool"]
    assert len(ledger_tool_msgs) == len(tool_msgs) == 2


def test_iteration_limit_is_reported_and_logged(tmp_path, caplog):  # 시나리오 6
    client = MockLLMClient()
    for i in range(5):
        client.queue_response(tool_calls=[_tool_call(f"c{i}", "{}")])
    agent, _, _ = _build(tmp_path, client, max_iterations=3)

    with caplog.at_level(logging.WARNING, logger=AGENT_LOGGER):
        result = agent.run_conversation("hi")

    assert result["stopped_reason"] == "max_iterations"
    assert result["iterations"] == 3
    assert result["final_response"] == ""
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_text_response_is_reported_as_completed(tmp_path):  # 시나리오 7
    client = MockLLMClient()
    client.queue_response(content=FINAL_TEXT)
    agent, _, _ = _build(tmp_path, client)

    result = agent.run_conversation("hi")

    assert result["stopped_reason"] == "completed"
    assert result["iterations"] == 1


@pytest.mark.parametrize("limit_reached", [True, False])  # 시나리오 8
def test_result_keeps_existing_keys(tmp_path, limit_reached):
    client = MockLLMClient()
    if limit_reached:
        for i in range(5):
            client.queue_response(tool_calls=[_tool_call(f"c{i}", "{}")])
    else:
        client.queue_response(content=FINAL_TEXT)
    agent, _, _ = _build(tmp_path, client, max_iterations=3)

    result = agent.run_conversation("hi")

    assert {"final_response", "messages", "iterations", "session_id"} <= set(result)
