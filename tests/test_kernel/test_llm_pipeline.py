"""tests/test_kernel/test_llm_pipeline.py — LLM 파이프라인 및 루프 연동 테스트.

시나리오 1~11.
tests/test_kernel 격리 규칙: 표준 라이브러리, pytest, hermes_core.kernel.*만 import.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

import pytest

from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.llm_pipeline import (
    LLMMiddleware,
    LLMNext,
    LLMRequest,
    build_llm_chain,
)
from hermes_core.kernel.loop import TokenEstimate, run_turn
from hermes_core.kernel.ports import LLMError, LLMPort, LLMReply
from hermes_core.kernel.tool_call import ToolCall
from hermes_core.kernel.turn_hooks import HookRegistry


# =============================================================================
# 가짜 포트 구현 (test_kernel 격리 준수)
# =============================================================================


class FakeReply:
    def __init__(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        usage: Optional[Any] = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage


class FakeLLM:
    def __init__(self, replies: Optional[list[Any]] = None) -> None:
        self.replies: list[Any] = list(replies) if replies else []
        self.calls: list[dict[str, Any]] = []

    def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMReply:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.replies:
            return FakeReply(content="")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeStore:
    def __init__(self) -> None:
        self.records: list[tuple[Any, ...]] = []
        self._seq = 0

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        self._seq += 1
        self.records.append((role, session_id, content, tool_calls, tool_call_id, tool_name))
        return self._seq

    def archive_and_compact(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        summary_index: Optional[int] = None,
    ) -> None:
        self.records.append(("archive", session_id, new_messages, summary_index))

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.records.append(("tokens", session_id, input_tokens, output_tokens))


class FakeTools:
    def __init__(self) -> None:
        self.dispatched: list[ToolCall] = []

    def get_tools_schema(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": "echo"}}]

    def dispatch(self, call: ToolCall) -> str:
        self.dispatched.append(call)
        return "ok"


# =============================================================================
# 3-1. 체인 단위 테스트 (시나리오 1~5)
# =============================================================================


def test_scenario_01_empty_chain_calls_port_directly() -> None:
    """시나리오 1: 빈 체인은 포트를 그대로 부른다."""
    fake_reply = FakeReply(content="hello")
    llm = FakeLLM([fake_reply])
    msgs = [{"role": "user", "content": "hi"}]
    req = LLMRequest(
        messages=msgs,
        tools=[{"x": 1}],
        scope=TurnScope(session_id="s1"),
    )

    chain = build_llm_chain(llm, ())
    res = chain(req)

    assert res is fake_reply
    assert len(llm.calls) == 1
    assert llm.calls[0]["messages"] is msgs
    assert llm.calls[0]["tools"] == [{"x": 1}]

    with pytest.raises(dataclasses.FrozenInstanceError):
        req.tools = None  # type: ignore[misc]


def test_scenario_02_middleware_order() -> None:
    """시나리오 2: 순서 검증 (m1 -> m2 -> llm -> m2 -> m1)."""
    order: list[str] = []

    def m1(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        order.append("m1>")
        reply = next_fn(req)
        order.append("<m1")
        return reply

    def m2(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        order.append("m2>")
        reply = next_fn(req)
        order.append("<m2")
        return reply

    class LoggingLLM(FakeLLM):
        def chat_completion(
            self,
            *,
            messages: list[dict[str, Any]],
            tools: Optional[list[dict[str, Any]]] = None,
        ) -> LLMReply:
            order.append("llm")
            return FakeReply(content="ok")

    llm = LoggingLLM()
    chain = build_llm_chain(llm, [m1, m2])
    req = LLMRequest(
        messages=[],
        tools=None,
        scope=TurnScope(session_id="s1"),
    )
    chain(req)

    assert order == ["m1>", "m2>", "llm", "<m2", "<m1"]


def test_scenario_03_short_circuit() -> None:
    """시나리오 3: 단락 — m1이 next를 부르지 않고 자기 응답 반환."""
    custom_reply = FakeReply(content="short_circuit")

    def m1(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        return custom_reply

    llm = FakeLLM([FakeReply(content="llm")])
    chain = build_llm_chain(llm, [m1])
    req = LLMRequest(
        messages=[],
        tools=None,
        scope=TurnScope(session_id="s1"),
    )
    res = chain(req)

    assert res is custom_reply
    assert len(llm.calls) == 0


def test_scenario_04_exceptions_pass_through() -> None:
    """시나리오 4: 예외는 그대로 올라간다."""
    # (a) LLMError
    seen: list[Exception] = []
    e = LLMError("timeout", "t")
    llm_a = FakeLLM([e])

    def m1_a(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        try:
            return next_fn(req)
        except Exception as x:
            seen.append(x)
            raise

    chain_a = build_llm_chain(llm_a, [m1_a])
    req = LLMRequest(messages=[], tools=None, scope=TurnScope(session_id="s1"))
    with pytest.raises(LLMError) as exc_info:
        chain_a(req)
    assert exc_info.value is e
    assert seen == [e]

    # (b) RuntimeError in middleware
    def m1_b(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        raise RuntimeError("mw")

    llm_b = FakeLLM([FakeReply(content="ok")])
    chain_b = build_llm_chain(llm_b, [m1_b])
    with pytest.raises(RuntimeError, match="mw"):
        chain_b(req)
    assert len(llm_b.calls) == 0


def test_scenario_05_middlewares_frozen_at_build() -> None:
    """시나리오 5: 목록은 만들 때 고정된다."""
    m2_called = False

    def m1(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        return next_fn(req)

    def m2(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        nonlocal m2_called
        m2_called = True
        return next_fn(req)

    mw_list: list[LLMMiddleware] = [m1]
    chain = build_llm_chain(FakeLLM([FakeReply(content="ok")]), mw_list)
    mw_list.append(m2)

    req = LLMRequest(messages=[], tools=None, scope=TurnScope(session_id="s1"))
    chain(req)

    assert not m2_called


# =============================================================================
# 3-2. 루프와 체인 (시나리오 6~11)
# =============================================================================


def test_scenario_06_loop_calls_via_chain() -> None:
    """시나리오 6: 루프는 체인으로 부른다 (도구 1회 후 완료)."""
    captured_requests: list[LLMRequest] = []

    def m(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        captured_requests.append(req)
        return next_fn(req)

    reply1 = FakeReply(
        tool_calls=[{"id": "call_1", "function": {"name": "echo", "arguments": "{}"}}]
    )
    reply2 = FakeReply(content="done")
    llm = FakeLLM([reply1, reply2])
    store = FakeStore()
    tools = FakeTools()
    sc = TurnScope(session_id="s1")
    messages: list[dict[str, Any]] = []

    res = run_turn(
        messages=messages,
        user_message="hi",
        llm=llm,
        tools=tools,
        store=store,
        hooks=HookRegistry(),
        scope=sc,
        estimate=TokenEstimate(),
        max_iterations=5,
        llm_middlewares=[m],
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert len(captured_requests) == 2
    for req in captured_requests:
        assert req.scope is sc
        assert req.messages is res["messages"]
        assert req.tools == tools.get_tools_schema()
    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "done"


def test_scenario_07_iteration_visible_in_scope() -> None:
    """시나리오 7: 바퀴 번호가 보인다."""
    iterations: list[int] = []

    def m(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        iterations.append(req.scope.iteration)
        return next_fn(req)

    reply1 = FakeReply(
        tool_calls=[{"id": "call_1", "function": {"name": "echo", "arguments": "{}"}}]
    )
    reply2 = FakeReply(content="done")
    llm = FakeLLM([reply1, reply2])
    sc = TurnScope(session_id="s1")

    run_turn(
        messages=[],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=FakeStore(),
        hooks=HookRegistry(),
        scope=sc,
        estimate=TokenEstimate(),
        max_iterations=5,
        llm_middlewares=[m],
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert iterations == [1, 2]


def test_scenario_08_kernel_without_middleware_does_not_record_usage() -> None:
    """시나리오 8: 미들웨어 없이 부른 커널은 사용량을 기록하지 않는다."""
    reply = FakeReply(
        content="done",
        usage={"input_tokens": 100, "output_tokens": 7},
    )
    llm = FakeLLM([reply])
    store = FakeStore()
    est = TokenEstimate()

    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=store,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert not any(r[0] == "tokens" for r in store.records)
    assert est == TokenEstimate(100, 2)


def test_scenario_09_recording_order_preserved() -> None:
    """시나리오 9: 기록 순서는 지금과 같다 (user -> mw -> assistant)."""
    store = FakeStore()

    def m(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        reply = next_fn(req)
        store.records.append(("mw",))
        return reply

    llm = FakeLLM([FakeReply(content="reply")])
    run_turn(
        messages=[],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=store,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        llm_middlewares=[m],
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    roles = [r[0] for r in store.records]
    assert roles == ["user", "mw", "assistant"]


def test_scenario_10_middleware_llm_error_treated_as_llm_error() -> None:
    """시나리오 10: 미들웨어가 낸 LLMError는 LLM 오류다."""
    def m(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        raise LLMError("rate_limited", "slow")

    llm = FakeLLM([FakeReply(content="should not be called")])
    store = FakeStore()

    res = run_turn(
        messages=[],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=store,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        llm_middlewares=[m],
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert res["stopped_reason"] == "llm_error"
    assert res["error"] == {"kind": "rate_limited", "message": "slow"}
    assert not any(r[0] == "assistant" for r in store.records)
    assert len(llm.calls) == 0


def test_scenario_11_short_circuit_handled_as_llm_reply() -> None:
    """시나리오 11: 단락 응답도 LLM 응답으로 다룬다."""
    def m(req: LLMRequest, next_fn: LLMNext) -> LLMReply:
        return FakeReply(
            content="short",
            tool_calls=None,
            usage={"input_tokens": 40},
        )

    llm = FakeLLM([FakeReply(content="should not be called")])
    est = TokenEstimate()

    res = run_turn(
        messages=[],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=FakeStore(),
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est,
        max_iterations=5,
        llm_middlewares=[m],
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert res["final_response"] == "short"
    assert est.input_tokens == 40
    assert len(llm.calls) == 0
