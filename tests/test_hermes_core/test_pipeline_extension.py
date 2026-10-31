"""코어 확장 파이프라인 통합 검증 (구현지시서 040 시나리오 E1~E5)."""

import json
from pathlib import Path
from typing import Any

import pytest

import httpx
from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.events import EVENT_TOOL_CALL, AgentEvent
from hermes_core.agent.turn_hooks import (
    AFTER_TOOL_CALLS,
    BEFORE_LLM,
    TurnContext,
)
from hermes_core.constants import DEFAULT_REAL_MODEL
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo back integer x.",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        },
    },
}


class SequentialMockTransport(httpx.BaseTransport):
    """지정된 순서대로 응답하는 MockTransport."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.captured_requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.captured_requests.append(request)
        if not self.responses:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "fallback done"}}]})
        item = self.responses.pop(0)
        return httpx.Response(200, json=item)


def _setup_pipeline_extension(dispatcher: ToolDispatcher, agent: HermesAgent, events: list[AgentEvent], limit: int = 2):
    """도구 예산 기능(미들웨어 + 훅 + 이벤트 sink) 장착 헬퍼."""
    call_counts: dict[str, int] = {}
    blocked_this_turn: list[bool] = [False]

    def budget_middleware(call: ToolCall, next_fn) -> str:
        current = call_counts.get(call.name, 0)
        if current >= limit:
            blocked_this_turn[0] = True
            return json.dumps({"success": False, "error": "budget exceeded"})
        call_counts[call.name] = current + 1
        return next_fn(call)

    def budget_stop_hook(ctx: TurnContext) -> None:
        if blocked_this_turn[0]:
            ctx.request_stop("budget")

    dispatcher.add_middleware(budget_middleware)
    agent.hooks.register(AFTER_TOOL_CALLS, budget_stop_hook)


def test_scenario_e1_e3_budget_extension_behavior_and_ledger_invariant(tmp_path: Path):
    """E1, E3: 도구 예산 확장(미들웨어+훅+sink) 적용 시 한도 초과 차단, 중단 및 원장 불변식 검증."""
    db = SessionDB(tmp_path / "e1.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    events: list[AgentEvent] = []
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}},
        {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": '{"x": 2}'}},
        {"id": "c3", "type": "function", "function": {"name": "echo", "arguments": '{"x": 3}'}},
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_calls),
        MockLLMResponse(content="should not be reached", tool_calls=None),
    ]
    client = MockLLMClient(responses)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        quiet_mode=True,
        event_sink=events.append,
    )
    _setup_pipeline_extension(dispatcher, agent, events, limit=2)

    res = agent.run_conversation("test call 3 times")

    # E1 검증: 첫 2건 성공, 3번째는 차단
    assert res["stopped_reason"] == "budget"
    assert res["iterations"] == 1
    assert res["final_response"] == ""

    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert json.loads(tool_msgs[0]["content"])["success"] is True
    assert json.loads(tool_msgs[1]["content"])["success"] is True
    assert json.loads(tool_msgs[2]["content"])["success"] is False
    assert json.loads(tool_msgs[2]["content"])["error"] == "budget exceeded"

    # 이벤트 확인
    tool_events = [e for e in events if e.kind == EVENT_TOOL_CALL]
    assert len(tool_events) == 3

    # E3 검증: 원장의 활성 메시지가 메모리의 messages[1:] (시스템 프롬프트 제외)와 정확히 일치
    ledger_active = db.get_messages_as_conversation(agent.session_id)
    mem_messages = res["messages"][1:]
    assert len(ledger_active) == len(mem_messages)
    for db_m, mem_m in zip(ledger_active, mem_messages):
        assert db_m["role"] == mem_m["role"]
        assert db_m.get("content") == mem_m.get("content")
        assert db_m.get("tool_call_id") == mem_m.get("tool_call_id")
        assert db_m.get("tool_calls") == mem_m.get("tool_calls")


def test_scenario_e2_unextended_control_runs_cleanly(tmp_path: Path):
    """E2: 확장을 붙이지 않은 대조군은 3건 모두 정상 실행되고 completed로 종료."""
    db = SessionDB(tmp_path / "e2.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}},
        {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": '{"x": 2}'}},
        {"id": "c3", "type": "function", "function": {"name": "echo", "arguments": '{"x": 3}'}},
    ]
    responses = [
        MockLLMResponse(content=None, tool_calls=tool_calls),
        MockLLMResponse(content="done all three", tool_calls=None),
    ]
    client = MockLLMClient(responses)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("test call 3 times unextended")
    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "done all three"
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert all(json.loads(m["content"])["success"] is True for m in tool_msgs)


def test_scenario_e4_real_client_transport_compression_and_extension_integration(tmp_path: Path):
    """E4: OpenRouterClient(MockTransport) 경로에서 압축과 확장이 동시 작동하며 원장 일치 검증."""
    db = SessionDB(tmp_path / "e4.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    events: list[AgentEvent] = []

    # 1. 요약 응답 페이로드
    summary_resp = {
        "id": "gen_summary",
        "choices": [{"message": {"role": "assistant", "content": "요약 완료 본문"}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 15},
    }
    # 2. 본 호출 응답 페이로드 (도구 호출 3건 포함)
    tool_resp = {
        "id": "gen_tools",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}},
                        {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": '{"x": 2}'}},
                        {"id": "c3", "type": "function", "function": {"name": "echo", "arguments": '{"x": 3}'}},
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }
    transport = SequentialMockTransport([summary_resp, tool_resp])
    client = OpenRouterClient(api_key="sk-or-v1-fake-test-key", model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        context_window_tokens=500,
        quiet_mode=True,
        event_sink=events.append,
    )
    _setup_pipeline_extension(dispatcher, agent, events, limit=2)

    # 긴 이전 대화 히스토리 구성 (압축 트리거용)
    history = [
        {"role": "system", "content": "System Prompt"},
        {"role": "user", "content": "헤드 메시지"},
    ]
    for i in range(8):
        history.append({"role": "user", "content": f"질문 {i}: " + ("데이터 " * 20)})
        history.append({"role": "assistant", "content": f"답변 {i}"})

    res = agent.run_conversation("트리거 턴", conversation_history=history)

    # 압축 발생 확인
    assert res.get("compression") is not None
    assert res["compression"]["count"] >= 1

    # 확장 동작(차단 및 중단) 확인
    assert res["stopped_reason"] == "budget"
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert json.loads(tool_msgs[2]["content"])["error"] == "budget exceeded"

    # 원장 불변식 검증
    ledger_active = db.get_messages_as_conversation(agent.session_id)
    mem_messages = res["messages"][1:]
    assert len(ledger_active) == len(mem_messages)
    for db_m, mem_m in zip(ledger_active, mem_messages):
        assert db_m["role"] == mem_m["role"]
        assert db_m.get("content") == mem_m.get("content")
        assert db_m.get("tool_call_id") == mem_m.get("tool_call_id")
        assert db_m.get("tool_calls") == mem_m.get("tool_calls")


def test_scenario_e5_session_resume_registers_compression_hook(tmp_path: Path):
    """E5: resume=True로 세션 재개 시에도 hooks에 압축 훅이 정상 등록되어 작동."""
    db = SessionDB(tmp_path / "e5.db")
    sid = "sess_resume_test"

    # 1. 초기 세션 생성 및 대화
    client1 = MockLLMClient([MockLLMResponse(content="turn 1 finished")])
    agent1 = HermesAgent(session_id=sid, session_db=db, llm_client=client1, quiet_mode=True)
    agent1.run_conversation("turn 1")

    # 2. resume=True로 재개
    client2 = MockLLMClient([MockLLMResponse(content="turn 2 finished")])
    agent2 = HermesAgent(session_id=sid, session_db=db, llm_client=client2, resume=True, quiet_mode=True)

    # 압축 확장 등록 여부 확인
    assert len(agent2.extensions) >= 1
    assert agent2.extensions[0].name == "compression"
    assert agent2.extensions[0].slot == "before_llm"

    # 대화 재개 실행
    conv_history = agent2.load_conversation_history()
    res2 = agent2.run_conversation("turn 2", conversation_history=conv_history)
    assert res2["stopped_reason"] == "completed"
    assert res2["final_response"] == "turn 2 finished"
