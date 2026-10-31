"""커널 루프 단위 시험 (시나리오 1~20)."""

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any, Optional

import pytest

from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.events import AgentEvent
from hermes_core.kernel.loop import TokenEstimate, run_turn
from hermes_core.kernel.ports import LLMError
from hermes_core.kernel.tool_call import ToolCall
from hermes_core.kernel.turn_hooks import BEFORE_LLM, HookRegistry, TurnContext


# =============================================================================
# 가짜 포트 (Fake Ports)
# =============================================================================

@dataclass
class FakeReply:
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


@dataclass
class FakeReplyWithUsage:
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    usage: Any = None


class FakeLLM:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.recorded_calls: list[dict[str, Any]] = []

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> Any:
        self.recorded_calls.append({
            "messages": [dict(m) for m in messages],
            "tools": list(tools) if tools is not None else None,
        })
        if not self._responses:
            raise RuntimeError("FakeLLM ran out of responses")
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


class FakeStore:
    def __init__(
        self,
        *,
        archive_exception: Optional[BaseException] = None,
        append_exception_roles: Optional[set[str]] = None,
    ) -> None:
        self.records: list[tuple[Any, ...]] = []
        self._archive_exception = archive_exception
        self._append_exception_roles = append_exception_roles or set()

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
        kw = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
            **kwargs,
        }
        self.records.append(("append", kw))
        if role in self._append_exception_roles:
            raise RuntimeError(f"FakeStore append failure for role {role}")
        return len(self.records)

    def archive_and_compact(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        summary_index: Optional[int] = None,
    ) -> None:
        if self._archive_exception is not None:
            raise self._archive_exception
        self.records.append(("archive", session_id, new_messages, summary_index))

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.records.append(("tokens", session_id, input_tokens, output_tokens))


class FakeTools:
    def __init__(
        self,
        schema: Optional[list[dict[str, Any]]] = None,
        dispatch_fn: Optional[Callable[[ToolCall], str]] = None,
    ) -> None:
        self.schema = schema if schema is not None else [
            {"type": "function", "function": {"name": "echo"}}
        ]
        self._dispatch_fn = dispatch_fn
        self.dispatched_calls: list[ToolCall] = []
        self.schema_calls_count = 0
        self.event_counts_at_dispatch: list[int] = []
        self.event_list_ref: Optional[list[AgentEvent]] = None

    def get_tools_schema(self) -> list[dict[str, Any]]:
        self.schema_calls_count += 1
        return self.schema

    def dispatch(self, call: ToolCall) -> str:
        self.dispatched_calls.append(call)
        if self.event_list_ref is not None:
            self.event_counts_at_dispatch.append(len(self.event_list_ref))
        if self._dispatch_fn:
            return self._dispatch_fn(call)
        return "R:" + call.name


# =============================================================================
# 단위 시험 시나리오 1~20
# =============================================================================

def test_scenario_1_normal_completion() -> None:
    """시나리오 1: 정상 종료."""
    messages = [{"role": "system", "content": "S"}]
    store = FakeStore()
    tools = FakeTools()
    llm = FakeLLM([FakeReply(content="done")])
    hooks = HookRegistry()
    events: list[AgentEvent] = []
    hook_errors: list[Any] = []
    scope = TurnScope(session_id="s1")
    estimate = TokenEstimate()
    logger = logging.getLogger("test.kernel")

    result = run_turn(
        messages=messages,
        user_message="hi",
        llm=llm,
        tools=tools,
        store=store,
        hooks=hooks,
        scope=scope,
        estimate=estimate,
        max_iterations=5,
        event_sink=events.append,
        on_hook_error=lambda p, h, e: hook_errors.append((p, h, e)),
        logger=logger,
    )

    assert set(result.keys()) == {
        "final_response",
        "messages",
        "iterations",
        "session_id",
        "stopped_reason",
    }
    assert result["final_response"] == "done"
    assert result["messages"] is messages
    assert result["iterations"] == 1
    assert result["session_id"] == "s1"
    assert result["stopped_reason"] == "completed"

    assert messages == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]

    assert len(store.records) == 2
    rec1 = store.records[0]
    rec2 = store.records[1]
    assert rec1 == ("append", {
        "session_id": "s1",
        "role": "user",
        "content": "hi",
        "tool_name": None,
        "tool_calls": None,
        "tool_call_id": None,
    })
    assert rec2 == ("append", {
        "session_id": "s1",
        "role": "assistant",
        "content": "done",
        "tool_name": None,
        "tool_calls": None,
        "tool_call_id": None,
    })


def test_scenario_2_tool_call_and_completion() -> None:
    """시나리오 2: 도구 1회 뒤 최종 응답."""
    messages = [{"role": "system", "content": "S"}]
    store = FakeStore()
    tools = FakeTools()
    llm = FakeLLM([
        FakeReply(
            tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}]
        ),
        FakeReply(content="ok"),
    ])
    hooks = HookRegistry()
    events: list[AgentEvent] = []
    hook_errors: list[Any] = []
    scope = TurnScope(session_id="s1")
    estimate = TokenEstimate()

    result = run_turn(
        messages=messages,
        user_message="hi",
        llm=llm,
        tools=tools,
        store=store,
        hooks=hooks,
        scope=scope,
        estimate=estimate,
        max_iterations=5,
        event_sink=events.append,
        on_hook_error=lambda p, h, e: hook_errors.append((p, h, e)),
        logger=logging.getLogger("test.kernel"),
    )

    # store 기록: user -> assistant(tool_calls) -> tool -> assistant("ok")
    assert len(store.records) == 4
    assert store.records[0][1]["role"] == "user"
    assert store.records[1][1]["role"] == "assistant"
    assert store.records[1][1]["content"] is None
    assert store.records[1][1]["tool_calls"] == [
        {"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}
    ]
    assert store.records[2][1]["role"] == "tool"
    assert store.records[2][1]["content"] == "R:echo"
    assert store.records[2][1]["tool_call_id"] == "c1"
    assert store.records[2][1]["tool_name"] == "echo"
    assert store.records[3][1]["role"] == "assistant"
    assert store.records[3][1]["content"] == "ok"

    # dispatch 검증
    assert len(tools.dispatched_calls) == 1
    call = tools.dispatched_calls[0]
    assert call.name == "echo"
    assert call.args == {"x": 1}
    assert call.task_id == "c1"
    assert call.scope is scope
    assert call.raw_arguments == '{"x": 1}'
    assert call.parse_error is None

    # messages 순서 및 첫 assistant content 없음
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert "content" not in messages[2]
    assert result["iterations"] == 2
    assert result["stopped_reason"] == "completed"


def test_scenario_3_tools_schema_passed() -> None:
    """시나리오 3: 도구 스키마 전달."""
    # (a) 시나리오 2를 다시 돌려 LLM이 받은 tools 확인
    llm_a = FakeLLM([
        FakeReply(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}]),
        FakeReply(content="ok"),
    ])
    tools_a = FakeTools()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm_a,
        tools=tools_a,
        store=FakeStore(),
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert len(llm_a.recorded_calls) == 2
    assert llm_a.recorded_calls[0]["tools"] == tools_a.schema
    assert llm_a.recorded_calls[1]["tools"] == tools_a.schema
    assert tools_a.schema_calls_count == 2

    # (b) FakeTools 스키마가 빈 목록일 때
    llm_b = FakeLLM([FakeReply(content="ok")])
    tools_b = FakeTools(schema=[])
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm_b,
        tools=tools_b,
        store=FakeStore(),
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert len(llm_b.recorded_calls) == 1
    assert llm_b.recorded_calls[0]["tools"] is None


def test_scenario_4_max_iterations(caplog: pytest.LogCaptureFixture) -> None:
    """시나리오 4: 최대 횟수 초과."""
    # (a) 매 응답 도구 호출, max_iterations=3
    llm_a = FakeLLM([
        FakeReply(tool_calls=[{"id": f"c{i}", "function": {"name": "echo", "arguments": "{}"}}])
        for i in range(5)
    ])
    logger = logging.getLogger("test.kernel.scenario4")
    with caplog.at_level(logging.WARNING, logger="test.kernel.scenario4"):
        res_a = run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=llm_a,
            tools=FakeTools(),
            store=FakeStore(),
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=3,
            event_sink=None,
            on_hook_error=lambda p, h, e: None,
            logger=logger,
        )
    assert res_a["stopped_reason"] == "max_iterations"
    assert res_a["iterations"] == 3
    assert res_a["final_response"] == ""
    assert len(llm_a.recorded_calls) == 3
    assert any("max_iterations=3" in r.message for r in caplog.records if r.name == "test.kernel.scenario4")

    # (b) max_iterations=0
    store_b = FakeStore()
    llm_b = FakeLLM([FakeReply(content="ok")])
    res_b = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm_b,
        tools=FakeTools(),
        store=store_b,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=0,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert len(llm_b.recorded_calls) == 0
    assert res_b["iterations"] == 0
    assert res_b["stopped_reason"] == "max_iterations"
    assert len(store_b.records) == 1
    assert store_b.records[0][1]["role"] == "user"


def test_scenario_5_llm_error(caplog: pytest.LogCaptureFixture) -> None:
    """시나리오 5: LLM 오류."""
    logger = logging.getLogger("test.kernel.scenario5")

    # (a) 첫 호출 timeout
    store_a = FakeStore()
    with caplog.at_level(logging.WARNING, logger="test.kernel.scenario5"):
        res_a = run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=FakeLLM([LLMError("timeout", "slow")]),
            tools=FakeTools(),
            store=store_a,
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=5,
            event_sink=None,
            on_hook_error=lambda p, h, e: None,
            logger=logger,
        )
    assert res_a["stopped_reason"] == "llm_error"
    assert res_a["final_response"] == ""
    assert res_a["iterations"] == 1
    assert res_a["error"] == {"kind": "timeout", "message": "slow"}
    assert all(r[1].get("role") != "assistant" for r in store_a.records)
    assert any("LLM call failed with error" in r.message for r in caplog.records if r.name == "test.kernel.scenario5")

    # (b) 도구 1회 뒤 둘째 호출 rate_limited
    store_b = FakeStore()
    res_b = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([
            FakeReply(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}]),
            LLMError("rate_limited", "429"),
        ]),
        tools=FakeTools(),
        store=store_b,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
        logger=logger,
    )
    roles = [r[1]["role"] for r in store_b.records]
    assert roles == ["user", "assistant", "tool"]
    assert res_b["iterations"] == 2

    # (c) RuntimeError 전파
    with pytest.raises(RuntimeError, match="boom"):
        run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=FakeLLM([RuntimeError("boom")]),
            tools=FakeTools(),
            store=FakeStore(),
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=5,
            event_sink=None,
            on_hook_error=lambda p, h, e: None,
        )


def test_scenario_6_before_llm_stop() -> None:
    """시나리오 6: before_llm 정지."""
    hooks = HookRegistry()
    hooks.register(BEFORE_LLM, lambda ctx: ctx.request_stop("halt"))
    store = FakeStore()
    llm = FakeLLM([FakeReply(content="done")])

    res = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=store,
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert len(llm.recorded_calls) == 0
    assert res["stopped_reason"] == "halt"
    assert res["iterations"] == 1
    assert res["final_response"] == ""
    assert len(store.records) == 1
    assert store.records[0][1]["role"] == "user"


def test_scenario_7_batch_tool_stop_and_pairs() -> None:
    """시나리오 7: 배치 중 정지 요청과 도구 쌍."""
    def dispatch_c2_stop(call: ToolCall) -> str:
        if call.task_id == "c2":
            call.scope.request_stop("stuck_loop")
        return "R:" + call.name

    tools = FakeTools(dispatch_fn=dispatch_c2_stop)
    hooks = HookRegistry()
    after_counts = 0

    def after_hook(ctx: TurnContext) -> None:
        nonlocal after_counts
        after_counts += 1

    hooks.register("after_tool_calls", after_hook)

    llm = FakeLLM([
        FakeReply(tool_calls=[
            {"id": "c1", "function": {"name": "echo", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "echo", "arguments": "{}"}},
            {"id": "c3", "function": {"name": "echo", "arguments": "{}"}},
        ]),
        FakeReply(content="next"),
    ])
    store = FakeStore()
    messages = [{"role": "system", "content": "S"}]

    res = run_turn(
        messages=messages,
        user_message="hi",
        llm=llm,
        tools=tools,
        store=store,
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert len(tools.dispatched_calls) == 3
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    tool_records = [r for r in store.records if r[1].get("role") == "tool"]
    assert len(tool_messages) == 3
    assert len(tool_records) == 3
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2", "c3"]
    assert [r[1]["tool_call_id"] for r in tool_records] == ["c1", "c2", "c3"]
    assert after_counts == 1
    assert len(llm.recorded_calls) == 1
    assert res["stopped_reason"] == "stuck_loop"
    assert res["final_response"] == ""


def test_scenario_8_stop_reason_precedence() -> None:
    """시나리오 8: 정지 사유 선착순."""
    hooks = HookRegistry()
    hooks.register(BEFORE_LLM, lambda ctx: ctx.request_stop("first"))
    hooks.register(BEFORE_LLM, lambda ctx: ctx.request_stop("second"))

    res = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReply(content="ok")]),
        tools=FakeTools(),
        store=FakeStore(),
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert res["stopped_reason"] == "first"


def test_scenario_9_hook_exception_isolation() -> None:
    """시나리오 9: 훅 예외 격리."""
    hooks = HookRegistry()
    err_hook = lambda ctx: (_ for _ in ()).throw(RuntimeError("bad"))
    second_counts = 0
    def ok_hook(ctx: TurnContext) -> None:
        nonlocal second_counts
        second_counts += 1

    hooks.register(BEFORE_LLM, err_hook)
    hooks.register(BEFORE_LLM, ok_hook)

    hook_errors: list[tuple[str, Any, Exception]] = []
    res = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReply(content="ok")]),
        tools=FakeTools(),
        store=FakeStore(),
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: hook_errors.append((p, h, e)),
    )
    assert len(hook_errors) == 1
    assert hook_errors[0][0] == BEFORE_LLM
    assert isinstance(hook_errors[0][2], RuntimeError)
    assert second_counts == 1
    assert res["stopped_reason"] == "completed"


def test_scenario_10_hook_direct_modification_detection() -> None:
    """시나리오 10: 훅의 직접 변조 감지."""
    # (a) messages append
    hooks_a = HookRegistry()
    hooks_a.register(BEFORE_LLM, lambda ctx: ctx.messages.append({"role": "user", "content": "x"}))
    errs_a: list[tuple[str, Any, Exception]] = []
    llm_a = FakeLLM([FakeReply(content="ok")])
    messages_a = [{"role": "system", "content": "S"}]

    res_a = run_turn(
        messages=messages_a,
        user_message="hi",
        llm=llm_a,
        tools=FakeTools(),
        store=FakeStore(),
        hooks=hooks_a,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: errs_a.append((p, h, e)),
    )
    assert len(errs_a) == 1
    assert "transcript modified outside replace_transcript" in str(errs_a[0][2])
    assert len(llm_a.recorded_calls[0]["messages"]) == 2
    assert res_a["messages"][0]["content"] == "S"

    # (b) messages[0] 변조
    hooks_b = HookRegistry()
    hooks_b.register(BEFORE_LLM, lambda ctx: ctx.messages.__setitem__(0, {"role": "system", "content": "T"}))
    errs_b: list[tuple[str, Any, Exception]] = []
    llm_b = FakeLLM([FakeReply(content="ok")])
    messages_b = [{"role": "system", "content": "S"}]

    res_b = run_turn(
        messages=messages_b,
        user_message="hi",
        llm=llm_b,
        tools=FakeTools(),
        store=FakeStore(),
        hooks=hooks_b,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: errs_b.append((p, h, e)),
    )
    assert len(errs_b) == 1
    assert "system message modified by hook" in str(errs_b[0][2])
    assert len(llm_b.recorded_calls[0]["messages"]) == 2
    assert res_b["messages"][0]["content"] == "S"


def test_scenario_11_replace_transcript_success() -> None:
    """시나리오 11: 대화 교체 성공."""
    estimate = TokenEstimate(999, 10)
    hooks = HookRegistry()

    def replace_hook(ctx: TurnContext) -> None:
        ctx.replace_transcript(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "요약"}],
            summary_index=0,
            usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    hooks.register(BEFORE_LLM, replace_hook)
    store = FakeStore()
    llm = FakeLLM([FakeReply(content="ok")])
    hook_errors: list[Any] = []

    res = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=store,
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=estimate,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: hook_errors.append((p, h, e)),
    )

    # 기록 순서: user -> archive -> tokens -> assistant
    assert len(store.records) == 4
    assert store.records[0] == ("append", {
        "session_id": "s1", "role": "user", "content": "hi",
        "tool_name": None, "tool_calls": None, "tool_call_id": None
    })
    assert store.records[1] == ("archive", "s1", [{"role": "user", "content": "요약"}], 0)
    assert store.records[2] == ("tokens", "s1", 5, 2)
    assert store.records[3][0] == "append" and store.records[3][1]["role"] == "assistant"

    assert llm.recorded_calls[0]["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "요약"},
    ]
    assert estimate.input_tokens is None
    assert estimate.message_count is None
    assert len(hook_errors) == 0


def test_scenario_12_replace_transcript_store_failure() -> None:
    """시나리오 12: 대화 교체 원장 실패."""
    estimate = TokenEstimate(555, 3)
    store = FakeStore(archive_exception=RuntimeError("db compact error"))
    hooks = HookRegistry()

    def replace_hook(ctx: TurnContext) -> None:
        ctx.replace_transcript(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "요약"}],
            summary_index=0,
        )

    hooks.register(BEFORE_LLM, replace_hook)
    hook_errors: list[Any] = []
    llm = FakeLLM([FakeReply(content="ok")])

    res = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=store,
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=estimate,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: hook_errors.append((p, h, e)),
    )

    assert len(hook_errors) == 1
    assert "db compact error" in str(hook_errors[0][2])
    assert llm.recorded_calls[0]["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
    ]
    assert not any(r[0] == "tokens" for r in store.records)
    assert estimate.input_tokens == 555


def test_scenario_13_replace_transcript_rejected() -> None:
    """시나리오 13: 대화 교체 거부."""
    # (a) 시스템 메시지 변경
    hooks_a = HookRegistry()
    hooks_a.register(
        BEFORE_LLM,
        lambda ctx: ctx.replace_transcript([{"role": "system", "content": "NEW"}]),
    )
    errs_a: list[Any] = []
    store_a = FakeStore()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReply(content="ok")]),
        tools=FakeTools(),
        store=store_a,
        hooks=hooks_a,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: errs_a.append((p, h, e)),
    )
    assert len(errs_a) == 1
    assert isinstance(errs_a[0][2], ValueError)
    assert "system message modified by replace_transcript" in str(errs_a[0][2])
    assert not any(r[0] == "archive" for r in store_a.records)

    # (b) 빈 목록
    hooks_b = HookRegistry()
    hooks_b.register(BEFORE_LLM, lambda ctx: ctx.replace_transcript([]))
    errs_b: list[Any] = []
    store_b = FakeStore()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReply(content="ok")]),
        tools=FakeTools(),
        store=store_b,
        hooks=hooks_b,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: errs_b.append((p, h, e)),
    )
    assert len(errs_b) == 1
    assert isinstance(errs_b[0][2], ValueError)
    assert "new_messages cannot be empty." in str(errs_b[0][2])
    assert not any(r[0] == "archive" for r in store_b.records)


def test_scenario_14_scope_report_and_reserved_keys() -> None:
    """시나리오 14: 결과 보고 병합과 예약 키 보호."""
    hooks = HookRegistry()

    def report_hook(ctx: TurnContext) -> None:
        ctx.scope.report("k", 1)
        ctx.scope.report("messages", "x")

    hooks.register(BEFORE_LLM, report_hook)
    hook_errors: list[Any] = []

    res = run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReply(content="ok")]),
        tools=FakeTools(),
        store=FakeStore(),
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: hook_errors.append((p, h, e)),
    )
    assert res["k"] == 1
    assert isinstance(res["messages"], list)
    assert len(hook_errors) == 1
    assert isinstance(hook_errors[0][2], ValueError)
    assert "messages" in str(hook_errors[0][2])


def test_scenario_15_token_estimate_update_rules() -> None:
    """시나리오 15: 토큰 추정값 갱신 규칙."""
    # (a) input_tokens 100, output_tokens 7
    store_a = FakeStore()
    est_a = TokenEstimate()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReplyWithUsage(content="ok", usage={"input_tokens": 100, "output_tokens": 7})]),
        tools=FakeTools(),
        store=store_a,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est_a,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert any(r == ("tokens", "s1", 100, 7) for r in store_a.records)
    assert est_a == TokenEstimate(100, 2)

    # (b) input 0, output 0
    store_b = FakeStore()
    est_b = TokenEstimate()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReplyWithUsage(content="ok", usage={"input_tokens": 0, "output_tokens": 0})]),
        tools=FakeTools(),
        store=store_b,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est_b,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert not any(r[0] == "tokens" for r in store_b.records)
    assert est_b == TokenEstimate()

    # (c) output 4
    store_c = FakeStore()
    est_c = TokenEstimate()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReplyWithUsage(content="ok", usage={"output_tokens": 4})]),
        tools=FakeTools(),
        store=store_c,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est_c,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert any(r == ("tokens", "s1", 0, 4) for r in store_c.records)
    assert est_c == TokenEstimate()

    # (d) usage 속성 없음
    store_d = FakeStore()
    est_d = TokenEstimate()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReply(content="ok")]),
        tools=FakeTools(),
        store=store_d,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est_d,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert not any(r[0] == "tokens" for r in store_d.records)
    assert est_d == TokenEstimate()

    # (e) usage 문자열
    store_e = FakeStore()
    est_e = TokenEstimate()
    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([FakeReplyWithUsage(content="ok", usage="not a dict")]),
        tools=FakeTools(),
        store=store_e,
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=est_e,
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )
    assert not any(r[0] == "tokens" for r in store_e.records)
    assert est_e == TokenEstimate()


def test_scenario_16_estimate_in_place_immediate() -> None:
    """시나리오 16: 추정값 즉시 반영."""
    est = TokenEstimate()
    store = FakeStore(append_exception_roles={"tool"})
    llm = FakeLLM([
        FakeReplyWithUsage(
            tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}],
            usage={"input_tokens": 50},
        )
    ])

    with pytest.raises(RuntimeError, match="FakeStore append failure for role tool"):
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

    assert est.input_tokens == 50


def test_scenario_17_arguments_parsing_and_tool_call(caplog: pytest.LogCaptureFixture) -> None:
    """시나리오 17: 인자 해석과 ToolCall."""
    logger = logging.getLogger("test.kernel.scenario17")
    tools = FakeTools()
    llm = FakeLLM([
        FakeReply(
            tool_calls=[
                {"id": "c1", "function": {"name": "echo", "arguments": "{bad"}},
                {"id": "c2", "function": {"name": "echo", "arguments": "[1, 2]"}},
                {"id": "c3", "function": {"name": "echo", "arguments": ""}},
                {"id": "c4", "function": {"name": "echo", "arguments": None}},
                {"id": "c5", "function": {"name": "echo", "arguments": {"y": 2}}},
                {"name": "nameonly"},
            ]
        ),
        FakeReply(content="ok"),
    ])
    store = FakeStore()

    with caplog.at_level(logging.WARNING, logger="test.kernel.scenario17"):
        run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=llm,
            tools=tools,
            store=store,
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=5,
            event_sink=None,
            on_hook_error=lambda p, h, e: None,
            logger=logger,
        )

    calls = tools.dispatched_calls
    assert len(calls) == 6

    # (1) {bad
    assert calls[0].args == {}
    assert calls[0].parse_error is not None
    assert calls[0].raw_arguments == "{bad"

    # (2) [1, 2]
    assert calls[1].args == {}
    assert calls[1].parse_error == "arguments must be a JSON object"

    # (3) ""
    assert calls[2].args == {}
    assert calls[2].parse_error is None

    # (4) None
    assert calls[3].args == {}
    assert calls[3].parse_error is None

    # (5) {"y": 2}
    assert calls[4].args == {"y": 2}
    assert calls[4].raw_arguments is None

    # (6) nameonly
    assert calls[5].name == "nameonly"
    assert bool(calls[5].task_id)
    # tool 메시지와 일치
    tool_rec_6 = [r for r in store.records if r[1].get("role") == "tool"][5]
    assert tool_rec_6[1]["tool_call_id"] == calls[5].task_id

    # 로거 WARNING 2건
    warns = [r for r in caplog.records if r.name == "test.kernel.scenario17" and "Invalid JSON arguments for tool 'echo'" in r.message]
    assert len(warns) == 2


def test_scenario_18_events(caplog: pytest.LogCaptureFixture) -> None:
    """시나리오 18: 이벤트 방출 및 예외 격리."""
    # (a) 정상 방출
    events_a: list[AgentEvent] = []
    tools_a = FakeTools()
    tools_a.event_list_ref = events_a

    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=FakeLLM([
            FakeReply(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}]),
            FakeReply(content="ok"),
        ]),
        tools=tools_a,
        store=FakeStore(),
        hooks=HookRegistry(),
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=events_a.append,
        on_hook_error=lambda p, h, e: None,
        logger=logging.getLogger("test.kernel"),
    )
    assert len(events_a) == 1
    assert events_a[0].kind == "tool_call"
    assert events_a[0].data == {"name": "echo", "arguments": '{"x": 1}'}
    assert tools_a.event_counts_at_dispatch == [1]

    # (b) sink 예외 격리
    def bad_sink(ev: AgentEvent) -> None:
        raise RuntimeError("sink")

    logger = logging.getLogger("test.kernel.scenario18")
    store_b = FakeStore()
    with caplog.at_level(logging.WARNING, logger="test.kernel.scenario18"):
        res_b = run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=FakeLLM([
                FakeReply(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}]),
                FakeReply(content="ok"),
            ]),
            tools=FakeTools(),
            store=store_b,
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=5,
            event_sink=bad_sink,
            on_hook_error=lambda p, h, e: None,
            logger=logger,
        )
    assert res_b["stopped_reason"] == "completed"
    assert any(r[1].get("role") == "tool" for r in store_b.records)
    assert any("EventSink failed for event 'tool_call': sink" in r.message for r in caplog.records if r.name == "test.kernel.scenario18")


def test_scenario_19_logger_injection(caplog: pytest.LogCaptureFixture) -> None:
    """시나리오 19: 로거 주입."""
    # (a) 주입된 로거
    injected_logger = logging.getLogger("x.injected")
    with caplog.at_level(logging.WARNING):
        run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=FakeLLM([FakeReply(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}]) for _ in range(5)]),
            tools=FakeTools(),
            store=FakeStore(),
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=2,
            event_sink=None,
            on_hook_error=lambda p, h, e: None,
            logger=injected_logger,
        )
    warn_a = [r for r in caplog.records if "Agent loop stopped at max_iterations" in r.message]
    assert len(warn_a) >= 1
    assert warn_a[-1].name == "x.injected"

    # (b) logger 생략 시 hermes_core.kernel.loop
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        run_turn(
            messages=[{"role": "system", "content": "S"}],
            user_message="hi",
            llm=FakeLLM([FakeReply(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}]) for _ in range(5)]),
            tools=FakeTools(),
            store=FakeStore(),
            hooks=HookRegistry(),
            scope=TurnScope(session_id="s1"),
            estimate=TokenEstimate(),
            max_iterations=2,
            event_sink=None,
            on_hook_error=lambda p, h, e: None,
            logger=None,
        )
    warn_b = [r for r in caplog.records if "Agent loop stopped at max_iterations" in r.message]
    assert len(warn_b) >= 1
    assert warn_b[-1].name == "hermes_core.kernel.loop"


def test_scenario_20_iteration_and_estimate_exposure() -> None:
    """시나리오 20: 바퀴 번호와 추정값 노출."""
    history: list[tuple[int, int, Optional[int], Optional[int]]] = []
    hooks = HookRegistry()
    hooks.register(
        BEFORE_LLM,
        lambda ctx: history.append((
            ctx.iteration,
            ctx.scope.iteration,
            ctx.last_input_tokens,
            ctx.last_message_count,
        )),
    )

    llm = FakeLLM([
        FakeReplyWithUsage(
            tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}],
            usage={"input_tokens": 30},
        ),
        FakeReply(content="done"),
    ])

    run_turn(
        messages=[{"role": "system", "content": "S"}],
        user_message="hi",
        llm=llm,
        tools=FakeTools(),
        store=FakeStore(),
        hooks=hooks,
        scope=TurnScope(session_id="s1"),
        estimate=TokenEstimate(),
        max_iterations=5,
        event_sink=None,
        on_hook_error=lambda p, h, e: None,
    )

    assert history == [(1, 1, None, None), (2, 2, 30, 2)]
