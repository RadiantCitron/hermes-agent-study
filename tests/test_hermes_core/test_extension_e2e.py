"""구현지시서 050 통합 검증: OpenRouterClient + MockTransport 및 위임 중첩 환경 계약 검증."""
import json
from pathlib import Path
from typing import Any
import httpx
import pytest

from hermes_core.agent.agent import HermesAgent
from hermes_core.agent.events import EVENT_EXTENSION_ERROR, AgentEvent
from hermes_core.constants import DEFAULT_REAL_MODEL
from hermes_core.extensions.contract import (
    ExtensionHost,
    MiddlewarePhase,
    TurnScope,
)
from hermes_core.llm_client import LLMResponse, OpenRouterClient


class MockLLMClient:
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self._idx = 0
        self.call_history: list[dict[str, Any]] = []

    def chat_completion(self, messages, tools=None):
        self.call_history.append({"messages": list(messages), "tools": tools})
        if self._idx < len(self.responses):
            resp = self.responses[self._idx]
            self._idx += 1
            if isinstance(resp, Exception):
                raise resp
            return resp
        return LLMResponse(content="default mock response")
from hermes_core.session_db import SessionDB
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo back x.",
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
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "default"}}]})
        resp = self.responses.pop(0)
        return httpx.Response(200, json=resp)


class CountingGuard:
    name = "counting_guard"

    def __init__(self, fail_all: bool = False):
        self.fail_all = fail_all
        self.scopes_seen: list[TurnScope] = []

    def install(self, host: ExtensionHost) -> None:
        def mw(call: ToolCall, next_fn):
            if call.scope is not None:
                self.scopes_seen.append(call.scope)
                st = call.scope.state("counting_guard")
                st["calls"] = st.get("calls", 0) + 1
            if self.fail_all:
                raise RuntimeError("guard fail before next")
            return next_fn(call)

        host.add_tool_middleware(MiddlewarePhase.GUARD, mw)


class DenyPolicy:
    name = "deny_policy"

    def __init__(self, deny_names: list[str]):
        self.deny_names = set(deny_names)

    def install(self, host: ExtensionHost) -> None:
        def mw(call: ToolCall, next_fn):
            if call.name in self.deny_names:
                return json.dumps({"success": False, "error": "denied"})
            return next_fn(call)

        host.add_tool_middleware(MiddlewarePhase.POLICY, mw)


def test_scenario_e1_real_client_transport_compression_and_extension(tmp_path: Path):
    """E1: 실제 OpenRouterClient+MockTransport, 작은 context_window_tokens로 압축이 1회 일어나는 대화 + CountingGuard·DenyPolicy 설치."""
    db = SessionDB(tmp_path / "e1.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)
    dispatcher.register_tool("deny_me", lambda: json.dumps({"success": True}), {"type": "function", "function": {"name": "deny_me", "parameters": {"type": "object"}}})

    summary_resp = {
        "id": "gen_summary",
        "choices": [{"message": {"role": "assistant", "content": "요약 완료 본문"}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 15},
    }
    tool_resp = {
        "id": "gen_tools",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}},
                        {"id": "c2", "type": "function", "function": {"name": "deny_me", "arguments": "{}"}},
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }
    final_resp = {
        "id": "gen_final",
        "choices": [{"message": {"role": "assistant", "content": "최종 완료"}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }
    transport = SequentialMockTransport([summary_resp, tool_resp, final_resp])
    client = OpenRouterClient(api_key="sk-or-v1-fake-test-key", model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        context_window_tokens=500,
        quiet_mode=True,
    )
    guard = CountingGuard()
    policy = DenyPolicy(deny_names=["deny_me"])
    agent.install_extension(guard)
    agent.install_extension(policy)

    # 긴 이전 대화로 압축 유도
    history = [
        {"role": "system", "content": "System Prompt"},
        {"role": "user", "content": "헤드 메시지"},
    ]
    for i in range(8):
        history.append({"role": "user", "content": f"질문 {i}: " + ("데이터 " * 20)})
        history.append({"role": "assistant", "content": f"답변 {i}"})

    res = agent.run_conversation("test e1", conversation_history=history)

    assert res["stopped_reason"] == "completed"
    assert res.get("compression", {}).get("count", 0) >= 1

    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    echo_msg = next(m for m in tool_msgs if m.get("tool_call_id") == "c1")
    deny_msg = next(m for m in tool_msgs if m.get("tool_call_id") == "c2")

    assert json.loads(echo_msg["content"])["success"] is True
    assert json.loads(deny_msg["content"])["error"] == "denied"

    # 원장 일치 검증
    active_ledger = db.get_messages_as_conversation(agent.session_id)
    mem_messages = res["messages"][1:]
    assert len(active_ledger) == len(mem_messages)
    for db_m, mem_m in zip(active_ledger, mem_messages):
        assert db_m["role"] == mem_m["role"]
        assert db_m.get("content") == mem_m.get("content")


def test_scenario_e2_guard_all_fail_open_real_client(tmp_path: Path):
    """E2: E1에서 CountingGuard가 모든 호출에서 next 전 예외 -> 도구는 각각 1회 실행, completed 종료, extension_error 집계."""
    events: list[AgentEvent] = []
    db = SessionDB(tmp_path / "e2.db")
    dispatcher = ToolDispatcher()
    echo_calls = 0

    def echo_fn(x=0):
        nonlocal echo_calls
        echo_calls += 1
        return json.dumps({"success": True, "x": x})

    dispatcher.register_tool("echo", echo_fn, ECHO_SCHEMA)

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
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }
    final_resp = {
        "id": "gen_final",
        "choices": [{"message": {"role": "assistant", "content": "완료"}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }
    transport = SequentialMockTransport([tool_resp, final_resp])
    client = OpenRouterClient(api_key="sk-or-v1-fake-test-key", model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        event_sink=events.append,
        quiet_mode=True,
    )
    guard = CountingGuard(fail_all=True)
    agent.install_extension(guard)

    res = agent.run_conversation("test e2")
    assert res["stopped_reason"] == "completed"
    assert echo_calls == 2

    ext_errs = [e for e in events if e.kind == EVENT_EXTENSION_ERROR]
    assert len(ext_errs) == 2

    guard_record = next(r for r in agent.extensions if r.name == "counting_guard")
    assert guard_record.failures == 2


def test_scenario_e3_parent_child_delegate_scope_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """E3: 부모 디스패처에 CountingGuard, 자식 디스패처에도 CountingGuard 장착. 부모 턴 칸과 자식 턴 칸이 독립 격리 유지."""
    import hermes_core.tools.delegate_tool as dt_module

    db = SessionDB(tmp_path / "e3.db")
    parent_dispatcher = ToolDispatcher()
    parent_guard = CountingGuard()
    child_guard = CountingGuard()

    parent_dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    # CountingGuard 인스턴스를 직접 parent_agent에 설치할 것이므로 dispatcher 직접 조작 대신 install_extension 사용
    # 자식 디스패처 생성 몽키패치
    orig_create_child_dispatcher = dt_module.create_child_dispatcher

    def custom_child_dispatcher(*args, **kwargs):
        cd = orig_create_child_dispatcher(*args, **kwargs)
        cd.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

        def child_mw(call: ToolCall, next_fn):
            if call.scope is not None:
                child_guard.scopes_seen.append(call.scope)
                st = call.scope.state("counting_guard")
                st["calls"] = st.get("calls", 0) + 1
            return next_fn(call)

        cd.add_middleware(child_mw, phase=MiddlewarePhase.GUARD, name="counting_guard")
        return cd

    monkeypatch.setattr(dt_module, "create_child_dispatcher", custom_child_dispatcher)

    parent_sid = "parent_sess"
    # 자식 클라이언트: echo 2회 호출 후 완료
    child_client = MockLLMClient([
        LLMResponse(content=None, tool_calls=[{"id": "k1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}}]),
        LLMResponse(content=None, tool_calls=[{"id": "k2", "type": "function", "function": {"name": "echo", "arguments": '{"x": 2}'}}]),
        LLMResponse(content="child finished"),
    ])

    def client_factory():
        return child_client

    # 부모 클라이언트: echo 1회, delegate_task 1회 호출 후 완료
    parent_client = MockLLMClient([
        LLMResponse(
            content=None,
            tool_calls=[
                {"id": "p1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 100}'}},
                {"id": "p2", "type": "function", "function": {"name": "delegate_task", "arguments": '{"goal": "run subtask"}'}},
            ],
        ),
        LLMResponse(content="parent finished"),
    ])

    parent_agent = HermesAgent(
        session_id=parent_sid,
        session_db=db,
        dispatcher=parent_dispatcher,
        llm_client=parent_client,
        quiet_mode=True,
    )
    parent_agent.install_extension(parent_guard)

    register_delegate_tool(
        parent_dispatcher,
        session_db=db,
        parent_session_id=parent_sid,
        llm_client_factory=client_factory,
    )

    res = parent_agent.run_conversation("start parent task")
    assert res["stopped_reason"] == "completed"

    # 부모 턴 칸 카운트 확인 (echo 1 + delegate_task 1 = 2)
    assert len(parent_guard.scopes_seen) == 2
    assert parent_guard.scopes_seen[0] is parent_guard.scopes_seen[1]
    assert parent_guard.scopes_seen[0].state("counting_guard")["calls"] == 2

    # 자식 턴 칸 카운트 확인 (echo 2 = 2)
    assert len(child_guard.scopes_seen) == 2
    assert child_guard.scopes_seen[0] is child_guard.scopes_seen[1]
    assert child_guard.scopes_seen[0].state("counting_guard")["calls"] == 2

    # 부모 scope와 자식 scope는 완전히 다른 객체
    assert parent_guard.scopes_seen[0] is not child_guard.scopes_seen[0]


def test_scenario_e4_resume_session_preserves_extensions(tmp_path: Path):
    """E4: E1 세션을 HermesAgent(resume=True)로 다시 열고 한 턴 더 -> compression 정상 등록 및 원장 정합."""
    db = SessionDB(tmp_path / "e4.db")
    dispatcher = ToolDispatcher()

    # 1. 초기 세션
    c1 = MockLLMClient([LLMResponse(content="turn 1 done")])
    a1 = HermesAgent(session_id="s_res", session_db=db, dispatcher=dispatcher, llm_client=c1, quiet_mode=True)
    a1.run_conversation("t1")

    # 2. 재개 세션
    c2 = MockLLMClient([LLMResponse(content="turn 2 done")])
    a2 = HermesAgent(session_id="s_res", session_db=db, dispatcher=dispatcher, llm_client=c2, resume=True, quiet_mode=True)
    assert a2.extensions[0].name == "compression"

    conv = a2.load_conversation_history()
    res = a2.run_conversation("t2", conversation_history=conv)
    assert res["stopped_reason"] == "completed"

    # 원장과 메모리 정합
    ledger = db.get_messages_as_conversation(a2.session_id)
    assert len(ledger) == len(res["messages"][1:])


def test_scenario_e5_unextended_agent_baseline(tmp_path: Path):
    """E5: 확장 하나도 추가 설치하지 않은 대조 에이전트로 기본 실행 -> hook_error, extension_error 0건."""
    events: list[AgentEvent] = []
    db = SessionDB(tmp_path / "e5.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    client = MockLLMClient([
        LLMResponse(content=None, tool_calls=[{"id": "1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 5}'}}]),
        LLMResponse(content="all done"),
    ])
    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        event_sink=events.append,
        quiet_mode=True,
    )
    res = agent.run_conversation("baseline")
    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "all done"

    errors = [e for e in events if e.kind in (EVENT_EXTENSION_ERROR, "hook_error")]
    assert len(errors) == 0
