"""확장 게이트와 차단기 인수 테스트 (시나리오 1~9).

근거:
- ADR-0019 D-06, D-07
- 로드맵 19 구현지시서 030 §3-1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_core.agent.agent import (
    ExtensionStatus,
    HermesAgent,
    MockLLMClient,
    MockLLMResponse,
)
from hermes_core.agent.events import (
    EVENT_EXTENSION_ERROR,
    EVENT_EXTENSION_TRIPPED,
    AgentEvent,
)
from hermes_core.assembly import create_agent
from hermes_core.control import (
    ExtensionView,
    build_snapshot,
    load_settings,
    render_snapshot,
)
from hermes_core.kernel.contract import MiddlewarePhase
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo input value.",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        },
    },
}

ADD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
        },
    },
}


def _create_test_dispatcher() -> ToolDispatcher:
    dispatcher = ToolDispatcher()
    dispatcher.register_tool(
        "echo",
        lambda x=0: json.dumps({"success": True, "x": x}),
        ECHO_SCHEMA,
    )
    dispatcher.register_tool(
        "add",
        lambda a=0, b=0: json.dumps({"success": True, "sum": a + b}),
        ADD_SCHEMA,
    )
    return dispatcher


def test_scenario_01_disabled_middleware_gate_bypass(tmp_path: Path) -> None:
    """시나리오 1: 꺼진 도구 미들웨어(GUARD·POLICY) 게이트 통과.

    호출 횟수를 세고 POLICY에서 거부하거나 GUARD에서 기록하는 확장 2개를 설치한 뒤
    set_extension_enabled로 끄면 본문이 전혀 호출되지 않고 도구가 정상 실행된다.
    """
    db = SessionDB(tmp_path / "s1.db")
    dispatcher = _create_test_dispatcher()

    guard_called = 0
    policy_called = 0

    class GuardExt:
        name = "ext_guard"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                nonlocal guard_called
                guard_called += 1
                return next_fn(call)

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    class PolicyExt:
        name = "ext_policy"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                nonlocal policy_called
                policy_called += 1
                return json.dumps({"success": False, "error": "denied by policy"})

            host.add_tool_middleware(MiddlewarePhase.POLICY, mw)

    mock_llm = MockLLMClient([
        MockLLMResponse(
            content=None,
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "echo", "arguments": json.dumps({"x": 42})},
            }],
        ),
        MockLLMResponse(content="도구 실행 완료"),
    ])

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=mock_llm,
        extensions=[GuardExt(), PolicyExt()],
    )

    agent.set_extension_enabled("ext_guard", False)
    agent.set_extension_enabled("ext_policy", False)

    res = agent.run_conversation("테스트 요청")
    assert res["stopped_reason"] == "completed"
    assert guard_called == 0
    assert policy_called == 0

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    tool_content = json.loads(tool_msg["content"])
    assert tool_content.get("success") is True
    assert tool_content.get("x") == 42

    statuses = {st.name: st for st in agent.extension_status()}
    assert statuses["ext_guard"] == ExtensionStatus(
        name="ext_guard",
        enabled=False,
        consecutive_failures=0,
        tripped=False,
    )
    assert statuses["ext_policy"] == ExtensionStatus(
        name="ext_policy",
        enabled=False,
        consecutive_failures=0,
        tripped=False,
    )


def test_scenario_02_disabled_turn_hook_gate_bypass(tmp_path: Path) -> None:
    """시나리오 2: 꺼진 턴 훅(before_llm, after_tool_calls) 게이트 통과.

    before_llm에서 대화 중단을 요청하고 after_tool_calls에서 횟수를 세는 확장을
    설치한 뒤 꺼두면 훅 본문이 호출되지 않고 대화가 completed로 끝난다.
    """
    db = SessionDB(tmp_path / "s2.db")
    dispatcher = _create_test_dispatcher()

    before_calls = 0
    after_calls = 0

    class StopperExt:
        name = "stopper"

        def install(self, host: Any) -> None:
            def before_hook(ctx: Any) -> None:
                nonlocal before_calls
                before_calls += 1
                ctx.request_stop("halted")

            def after_hook(ctx: Any) -> None:
                nonlocal after_calls
                after_calls += 1

            host.add_turn_hook("before_llm", before_hook)
            host.add_turn_hook("after_tool_calls", after_hook)

    mock_llm = MockLLMClient([
        MockLLMResponse(
            content=None,
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "echo", "arguments": json.dumps({"x": 10})},
            }],
        ),
        MockLLMResponse(content="정상 완료"),
    ])

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=mock_llm,
        extensions=[StopperExt()],
    )

    agent.set_extension_enabled("stopper", False)

    res = agent.run_conversation("테스트")
    assert res["stopped_reason"] == "completed"
    assert before_calls == 0
    assert after_calls == 0


def test_scenario_03_circuit_breaker_tripped_and_isolated(tmp_path: Path) -> None:
    """시나리오 3: 임계값 N에서 자동 차단 및 이후 턴 격리 (WHY 가정 E).

    (a) breaker_threshold = 3에서 3회 실패 시 차단 이벤트가 방출되고
        4번째 턴에서는 해당 확장이 호출되지 않아 실패가 추가되지 않는다.
    (b) breaker_threshold = 1 경계값에서 첫 실패 즉시 차단된다.
    """
    # (a) breaker_threshold = 3
    db_a = SessionDB(tmp_path / "s3a.db")
    dispatcher_a = _create_test_dispatcher()
    events_a: list[AgentEvent] = []

    hook_calls_a = 0
    mw_calls_a = 0

    class FlakyHook:
        name = "flaky_hook"

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                nonlocal hook_calls_a
                hook_calls_a += 1
                raise RuntimeError("hook boom")

            host.add_turn_hook("before_llm", hook)

    class FlakyMw:
        name = "flaky_mw"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                nonlocal mw_calls_a
                mw_calls_a += 1
                raise RuntimeError("mw boom")

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    # 4턴 실행할 LLM 응답: 매 턴마다 echo 도구 호출 1회 후 완료 1회 (총 8개)
    responses_a = []
    for i in range(4):
        responses_a.append(
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": f"c_{i}",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"x": i})},
                }],
            )
        )
        responses_a.append(MockLLMResponse(content=f"턴 {i+1} 완료"))

    mock_llm_a = MockLLMClient(responses_a)

    settings = load_settings().settings  # 기본 breaker_threshold == 3
    agent_a = create_agent(
        settings,
        session_db=db_a,
        dispatcher=dispatcher_a,
        llm_client=mock_llm_a,
        extensions=[FlakyHook(), FlakyMw()],
        event_sink=lambda e: events_a.append(e),
    )

    # 1~3턴 실행: 매 턴마다 두 확장 실패 누적
    for t in range(3):
        res = agent_a.run_conversation(f"턴 {t+1}")
        assert res["stopped_reason"] == "completed"

    assert hook_calls_a == 3
    assert mw_calls_a == 3

    # 3턴 끝난 시점에 tripped 이벤트가 확장마다 정확히 1회 방출되었는지 확인
    tripped_events = [e for e in events_a if e.kind == EVENT_EXTENSION_TRIPPED]
    assert len(tripped_events) == 2
    hook_trip = next(e for e in tripped_events if e.data.get("name") == "flaky_hook")
    mw_trip = next(e for e in tripped_events if e.data.get("name") == "flaky_mw")
    assert hook_trip.data.get("failures") == 3
    assert mw_trip.data.get("failures") == 3

    st_map = {s.name: s for s in agent_a.extension_status()}
    assert st_map["flaky_hook"] == ExtensionStatus(
        name="flaky_hook",
        enabled=False,
        consecutive_failures=3,
        tripped=True,
    )
    assert st_map["flaky_mw"] == ExtensionStatus(
        name="flaky_mw",
        enabled=False,
        consecutive_failures=3,
        tripped=True,
    )

    # 4번째 턴 실행: 두 확장이 게이트에서 꺼져 있으므로 호출되지 않음
    res_4 = agent_a.run_conversation("턴 4")
    assert res_4["stopped_reason"] == "completed"
    assert hook_calls_a == 3  # 호출 증가 없음
    assert mw_calls_a == 3    # 호출 증가 없음

    # 추가 tripped 이벤트 및 error 이벤트가 발생하지 않음
    tripped_after = [e for e in events_a if e.kind == EVENT_EXTENSION_TRIPPED]
    assert len(tripped_after) == 2
    hook_errors = [e for e in events_a if e.kind == EVENT_EXTENSION_ERROR and e.data.get("name") == "flaky_hook"]
    mw_errors = [e for e in events_a if e.kind == EVENT_EXTENSION_ERROR and e.data.get("name") == "flaky_mw"]
    assert len(hook_errors) == 3
    assert len(mw_errors) == 3

    # (b) breaker_threshold = 1 경계값
    db_b = SessionDB(tmp_path / "s3b.db")
    dispatcher_b = _create_test_dispatcher()
    events_b: list[AgentEvent] = []
    b_calls = 0

    class ImmediateFlaky:
        name = "immediate_flaky"

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                nonlocal b_calls
                b_calls += 1
                raise RuntimeError("immediate boom")

            host.add_turn_hook("before_llm", hook)

    mock_llm_b = MockLLMClient([
        MockLLMResponse(content="턴 1"),
        MockLLMResponse(content="턴 2"),
    ])
    agent_b = HermesAgent(
        session_db=db_b,
        dispatcher=dispatcher_b,
        llm_client=mock_llm_b,
        extensions=[ImmediateFlaky()],
        event_sink=lambda e: events_b.append(e),
    )
    agent_b.set_extension_breaker(1)

    agent_b.run_conversation("턴 1 실행")
    assert b_calls == 1
    st_b = agent_b.extension_status()[0]
    assert st_b.tripped is True
    assert st_b.enabled is False
    assert st_b.consecutive_failures == 1

    trip_b = [e for e in events_b if e.kind == EVENT_EXTENSION_TRIPPED]
    assert len(trip_b) == 1

    agent_b.run_conversation("턴 2 실행")
    assert b_calls == 1  # 2번째 턴에 호출되지 않음


def test_scenario_04_consecutive_failures_reset_on_success(tmp_path: Path) -> None:
    """시나리오 4: 실패 사이 정상 반환 시 연속 실패 수 0 복귀.

    호출마다 [실패, 실패, 성공, 실패, 실패] 순서로 동작하는 미들웨어, 턴 훅,
    그리고 다중 슬롯 확장(before_llm 실패 후 GUARD 정상 반환)에서
    정상 반환 즉시 consecutive_failures가 0으로 복귀하고 차단되지 않는다.
    """
    events: list[AgentEvent] = []

    # 1. 미들웨어 확장: 호출마다 [실패, 실패, 성공, 실패, 실패]
    mw_step = 0

    class FlakyMw:
        name = "seq_mw"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                nonlocal mw_step
                mw_step += 1
                if mw_step in (1, 2, 4, 5):
                    raise RuntimeError(f"mw step {mw_step} fail")
                return next_fn(call)

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    # 5회 도구 호출 대화 실행 (매 턴 도구 1회 호출 후 완료)
    db_mw = SessionDB(tmp_path / "s4_mw.db")
    mw_responses = []
    for i in range(5):
        mw_responses.append(
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"x": i})},
                }],
            )
        )
        mw_responses.append(MockLLMResponse(content=f"턴 {i+1} 응답"))

    agent_mw = HermesAgent(
        session_db=db_mw,
        dispatcher=_create_test_dispatcher(),
        llm_client=MockLLMClient(mw_responses),
        extensions=[FlakyMw()],
        event_sink=lambda e: events.append(e),
    )
    agent_mw.set_extension_breaker(3)

    # 호출 1: 실패 -> consecutive == 1
    agent_mw.run_conversation("mw 1")
    assert agent_mw.extension_status()[0].consecutive_failures == 1

    # 호출 2: 실패 -> consecutive == 2
    agent_mw.run_conversation("mw 2")
    assert agent_mw.extension_status()[0].consecutive_failures == 2

    # 호출 3: 성공 -> consecutive == 0 리셋
    agent_mw.run_conversation("mw 3")
    assert agent_mw.extension_status()[0].consecutive_failures == 0
    assert next(r for r in agent_mw.extensions if r.name == "seq_mw").failures == 2

    # 호출 4, 5: 실패 -> consecutive == 2, total == 4
    agent_mw.run_conversation("mw 4")
    agent_mw.run_conversation("mw 5")
    st_mw = agent_mw.extension_status()[0]
    assert st_mw.consecutive_failures == 2
    assert st_mw.enabled is True
    assert st_mw.tripped is False
    assert next(r for r in agent_mw.extensions if r.name == "seq_mw").failures == 4

    # 2. 턴 훅 확장: 호출마다 [실패, 실패, 성공, 실패, 실패]
    hook_step = 0

    class FlakyHook:
        name = "seq_hook"

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                nonlocal hook_step
                hook_step += 1
                if hook_step in (1, 2, 4, 5):
                    raise RuntimeError(f"hook step {hook_step} fail")

            host.add_turn_hook("before_llm", hook)

    # 도구 호출 없는 5턴 대화 (매 턴 before_llm 1회 호출)
    db_hook = SessionDB(tmp_path / "s4_hook.db")
    hook_responses = [MockLLMResponse(content=f"턴 {i+1}") for i in range(5)]

    agent_hook = HermesAgent(
        session_db=db_hook,
        dispatcher=_create_test_dispatcher(),
        llm_client=MockLLMClient(hook_responses),
        extensions=[FlakyHook()],
        event_sink=lambda e: events.append(e),
    )
    agent_hook.set_extension_breaker(3)

    # 턴 1: 실패 -> consecutive == 1
    agent_hook.run_conversation("hook 1")
    assert agent_hook.extension_status()[0].consecutive_failures == 1

    # 턴 2: 실패 -> consecutive == 2
    agent_hook.run_conversation("hook 2")
    assert agent_hook.extension_status()[0].consecutive_failures == 2

    # 턴 3: 성공 -> consecutive == 0 리셋
    agent_hook.run_conversation("hook 3")
    assert agent_hook.extension_status()[0].consecutive_failures == 0
    assert next(r for r in agent_hook.extensions if r.name == "seq_hook").failures == 2

    # 턴 4, 5: 실패 -> consecutive == 2, total == 4
    agent_hook.run_conversation("hook 4")
    agent_hook.run_conversation("hook 5")
    st_hook = agent_hook.extension_status()[0]
    assert st_hook.consecutive_failures == 2
    assert st_hook.enabled is True
    assert st_hook.tripped is False
    assert next(r for r in agent_hook.extensions if r.name == "seq_hook").failures == 4

    # 3. 다중 슬롯 확장: before_llm 실패 2회 뒤 GUARD 정상 반환 시 0으로 복귀
    class MultiSlotExt:
        name = "multi_slot"

        def __init__(self) -> None:
            self.hook_fail = True

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                if self.hook_fail:
                    raise RuntimeError("multi hook fail")

            def mw(call: ToolCall, next_fn: Any) -> Any:
                return next_fn(call)

            host.add_turn_hook("before_llm", hook)
            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    db_multi = SessionDB(tmp_path / "s4_multi.db")
    multi_ext = MultiSlotExt()
    agent_multi = HermesAgent(
        session_db=db_multi,
        dispatcher=_create_test_dispatcher(),
        llm_client=MockLLMClient([
            MockLLMResponse(content="턴 1"),
            MockLLMResponse(content="턴 2"),
        ]),
        extensions=[multi_ext],
        event_sink=lambda e: events.append(e),
    )
    agent_multi.set_extension_breaker(3)

    # 턴 1, 2: before_llm 훅 실패 (총 2회 실패)
    agent_multi.run_conversation("턴 1")
    agent_multi.run_conversation("턴 2")
    assert agent_multi.extension_status()[0].consecutive_failures == 2

    # 이제 hook_fail = False로 전환하고 도구 호출 대화 실행 (GUARD 미들웨어 정상 통과)
    multi_ext.hook_fail = False
    agent_multi.llm_client = MockLLMClient([
        MockLLMResponse(
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
        ),
        MockLLMResponse(content="턴 3 완료"),
    ])
    agent_multi.run_conversation("다중 슬롯 대화")
    st_multi = agent_multi.extension_status()[0]
    assert st_multi.consecutive_failures == 0
    assert st_multi.enabled is True
    assert st_multi.tripped is False

    # 전체 과정에서 tripped 이벤트 0건
    trips = [e for e in events if e.kind == EVENT_EXTENSION_TRIPPED]
    assert len(trips) == 0


def test_scenario_05_inner_exception_neutrality_and_tampered_hook_tripping(tmp_path: Path) -> None:
    """시나리오 5: 안쪽 예외 중립성 및 불법 변조 훅 차단 (ROADMAP §4 위험 1·2).

    (a) 안쪽 POLICY 미들웨어에서 예외가 발생했을 때 바깥 GUARD 확장의
        consecutive_failures는 증가하지도 않고 0으로 초기화되지도 않는다.
    (b) ctx.messages를 직접 변조하는 불법 턴 훅은 _extension_succeeded가
        불리지 않아 consecutive_failures가 정상 누적되어 차단된다.
    """
    # (a) 안쪽 예외 중립성
    db_a = SessionDB(tmp_path / "s5a.db")
    dispatcher_a = _create_test_dispatcher()

    outer_calls = 0

    class OuterGuard:
        name = "outer_guard"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                nonlocal outer_calls
                outer_calls += 1
                if outer_calls == 1:
                    raise RuntimeError("outer fail on first call")
                return next_fn(call)

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    class FailingInnerPolicy:
        name = "failing_policy"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                # 안쪽 정책 미들웨어에서 예외 발생
                raise RuntimeError("inner policy boom")

            host.add_tool_middleware(MiddlewarePhase.POLICY, mw)

    mock_llm_a = MockLLMClient([
        MockLLMResponse(
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
        ),
        MockLLMResponse(content="턴 1 완료"),
        MockLLMResponse(
            content=None,
            tool_calls=[{"id": "c2", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
        ),
        MockLLMResponse(content="턴 2 완료"),
    ])

    agent_a = HermesAgent(
        session_db=db_a,
        dispatcher=dispatcher_a,
        llm_client=mock_llm_a,
        extensions=[OuterGuard(), FailingInnerPolicy()],
    )
    agent_a.set_extension_breaker(3)

    # 1회차: outer_guard 자체 실패 (consec == 1, total == 1)
    agent_a.run_conversation("첫 대화")
    st_outer = next(s for s in agent_a.extension_status() if s.name == "outer_guard")
    assert st_outer.consecutive_failures == 1
    assert next(r for r in agent_a.extensions if r.name == "outer_guard").failures == 1

    # 2회차: outer_guard는 정상적으로 next_fn 호출, 그러나 안쪽 failing_policy에서 예외 발생
    agent_a.run_conversation("두번째 대화")
    st_outer_2 = next(s for s in agent_a.extension_status() if s.name == "outer_guard")
    # outer_guard의 consecutive_failures는 1로 그대로 유지되어야 함 (중립성)
    assert st_outer_2.consecutive_failures == 1
    assert next(r for r in agent_a.extensions if r.name == "outer_guard").failures == 1

    # (b) 불법 변조 훅 차단
    db_b = SessionDB(tmp_path / "s5b.db")
    dispatcher_b = _create_test_dispatcher()

    class TamperingHookExt:
        name = "tampering_ext"

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                # replace_transcript를 통하지 않고 messages를 직접 수정
                ctx.messages.append({"role": "user", "content": "tampered content"})

            host.add_turn_hook("before_llm", hook)

    mock_llm_b = MockLLMClient([
        MockLLMResponse(content="턴 1 완료"),
        MockLLMResponse(content="턴 2 완료"),
    ])

    agent_b = HermesAgent(
        session_db=db_b,
        dispatcher=dispatcher_b,
        llm_client=mock_llm_b,
        extensions=[TamperingHookExt()],
    )
    agent_b.set_extension_breaker(2)

    # 턴 1 실행: 변조 감지로 hook error 발생, consecutive_failures는 1이 됨
    agent_b.run_conversation("턴 1")
    st_t1 = agent_b.extension_status()[0]
    assert st_t1.consecutive_failures == 1
    assert st_t1.tripped is False

    # 턴 2 실행: 또다시 변조 감지, consecutive_failures == 2에 도달하여 차단됨
    agent_b.run_conversation("턴 2")
    st_t2 = agent_b.extension_status()[0]
    assert st_t2.consecutive_failures == 2
    assert st_t2.tripped is True
    assert st_t2.enabled is False


def test_scenario_06_library_default_breaker_zero_never_trips(tmp_path: Path) -> None:
    """시나리오 6: 라이브러리 기본값(breaker_threshold == 0) 무차단 보장 (ADR-0019 D-07).

    HermesAgent를 직접 생성했을 때 breaker_threshold는 0이며,
    10회 연속 실패해도 차단되지 않고 누적 실패 수만 집계된다.
    """
    db = SessionDB(tmp_path / "s6.db")
    dispatcher = _create_test_dispatcher()
    events: list[AgentEvent] = []

    class FailingExt:
        name = "always_failing"

        def install(self, host: Any) -> None:
            host.add_turn_hook("before_llm", lambda ctx: None)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=MockLLMClient(),
        extensions=[FailingExt()],
        event_sink=lambda e: events.append(e),
    )

    # 직접 10회 에러 발생
    for i in range(10):
        agent._on_extension_error("always_failing", "before_llm", RuntimeError(f"error {i}"))

    st = agent.extension_status()[0]
    assert st.name == "always_failing"
    assert st.enabled is True
    assert st.tripped is False
    assert st.consecutive_failures == 10

    rec = agent.extensions[0]
    assert rec.failures == 10

    tripped_events = [e for e in events if e.kind == EVENT_EXTENSION_TRIPPED]
    assert len(tripped_events) == 0


def test_scenario_07_manual_reenable_and_snapshot_integration(tmp_path: Path) -> None:
    """시나리오 7: 수동 재활성화(set_extension_enabled(True))와 스냅샷 연동.

    차단된 확장의 스냅샷 표현 확인 후, 수동으로 켜면 consecutive_failures와 tripped가
    초기화되고 다음 턴에 다시 정상 실행된다.
    """
    db = SessionDB(tmp_path / "s7.db")
    dispatcher = _create_test_dispatcher()

    hook_calls = 0

    class BrokenThenFixedHook:
        name = "recoverable_hook"

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                nonlocal hook_calls
                hook_calls += 1

            host.add_turn_hook("before_llm", hook)

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=MockLLMClient(),
        extensions=[BrokenThenFixedHook()],
    )
    agent.set_extension_breaker(2)

    # 2회 연속 에러 발생시켜 차단
    agent._on_extension_error("recoverable_hook", "before_llm", RuntimeError("err 1"))
    agent._on_extension_error("recoverable_hook", "before_llm", RuntimeError("err 2"))

    st = agent.extension_status()[0]
    assert st.enabled is False
    assert st.consecutive_failures == 2
    assert st.tripped is True

    loaded = load_settings()
    snap_a = build_snapshot(agent, loaded)
    ext_view_a = snap_a.extensions[0]
    assert ext_view_a == ExtensionView(
        name="recoverable_hook",
        kinds=("turn_hook",),
        slots=("before_llm",),
        total_failures=2,
        enabled=False,
        consecutive_failures=2,
        tripped=True,
    )

    rendered_a = render_snapshot(snap_a)
    assert "recoverable_hook" in rendered_a
    assert "enabled=False" in rendered_a
    assert "consecutive=2" in rendered_a
    assert "tripped=True" in rendered_a

    # 수동 재활성화
    agent.set_extension_enabled("recoverable_hook", True)

    st_after = agent.extension_status()[0]
    assert st_after.enabled is True
    assert st_after.consecutive_failures == 0
    assert st_after.tripped is False

    snap_b = build_snapshot(agent, loaded)
    ext_view_b = snap_b.extensions[0]
    assert ext_view_b == ExtensionView(
        name="recoverable_hook",
        kinds=("turn_hook",),
        slots=("before_llm",),
        total_failures=2,
        enabled=True,
        consecutive_failures=0,
        tripped=False,
    )

    # 다음 턴 실행 시 정상 호출 확인
    mock_llm = MockLLMClient([MockLLMResponse(content="재활성화 대화 완료")])
    agent.llm_client = mock_llm
    res = agent.run_conversation("테스트")
    assert res["stopped_reason"] == "completed"
    assert hook_calls == 1


def test_scenario_08_invalid_argument_validations(tmp_path: Path) -> None:
    """시나리오 8: 잘못된 인자 검증 (ValueError).

    (a) 없는 확장 이름 끄기/켜기
    (b) enabled에 bool이 아닌 타입 전달
    (c) breaker threshold에 음수 전달
    (d) breaker threshold에 bool 타입 전달
    """
    db = SessionDB(tmp_path / "s8.db")
    agent = HermesAgent(
        session_db=db,
        dispatcher=_create_test_dispatcher(),
        llm_client=MockLLMClient(),
    )

    with pytest.raises(ValueError, match="Unknown extension 'no_such_ext'."):
        agent.set_extension_enabled("no_such_ext", False)

    with pytest.raises(ValueError, match="enabled must be a bool."):
        agent.set_extension_enabled("compression", "yes")  # type: ignore

    with pytest.raises(ValueError, match="threshold must be an integer >= 0."):
        agent.set_extension_breaker(-1)

    with pytest.raises(ValueError, match="threshold must be an integer >= 0."):
        agent.set_extension_breaker(True)  # bool은 int 하위 클래스이나 금지


def test_scenario_09_ledger_tool_pair_integrity_after_breaker_tripped(tmp_path: Path) -> None:
    """시나리오 9: 차단 뒤 대화 기록 및 원장 도구 쌍 무결성.

    한 턴에 도구 호출 2개(c1, c2)를 반환하는 응답에서, breaker_threshold = 1인 에이전트의
    GUARD 확장이 c1 실행 중 예외를 던져 즉시 차단되고 after_tool_calls 훅도 예외를 던져 차단된 뒤,
    c2 호출 시에는 이미 차단된 GUARD 확장이 호출되지 않으며
    원장의 messages 쌍(assistant.tool_calls <-> tool)이 온전히 보존된다.
    """
    db = SessionDB(tmp_path / "s9.db")
    dispatcher = _create_test_dispatcher()

    guard_calls = 0
    hook_calls = 0

    class GuardFailOnFirst:
        name = "guard_c1_fail"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                nonlocal guard_calls
                guard_calls += 1
                if call.name == "echo":
                    raise RuntimeError("guard fail on echo (c1)")
                return next_fn(call)

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    class HookFailOnce:
        name = "hook_fail_once"

        def install(self, host: Any) -> None:
            def hook(ctx: Any) -> None:
                nonlocal hook_calls
                hook_calls += 1
                raise RuntimeError("hook fail")

            host.add_turn_hook("after_tool_calls", hook)

    mock_llm = MockLLMClient([
        MockLLMResponse(
            content=None,
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": json.dumps({"x": 100})}},
                {"id": "c2", "type": "function", "function": {"name": "add", "arguments": json.dumps({"a": 3, "b": 7})}},
            ],
        ),
        MockLLMResponse(content="도구 두 개 실행 후 최종 답변"),
    ])

    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=mock_llm,
        extensions=[GuardFailOnFirst(), HookFailOnce()],
    )
    agent.set_extension_breaker(1)

    res = agent.run_conversation("두 도구 동시 실행 요청")
    assert res["stopped_reason"] == "completed"

    # c1(echo) 실행 시 guard가 1회 실패하고 즉시 차단되었으므로, c2(add) 실행 시에는 guard가 불리지 않아야 함
    assert guard_calls == 1
    assert hook_calls == 1

    st_guard = next(s for s in agent.extension_status() if s.name == "guard_c1_fail")
    assert st_guard.tripped is True
    assert st_guard.enabled is False

    st_hook = next(s for s in agent.extension_status() if s.name == "hook_fail_once")
    assert st_hook.tripped is True
    assert st_hook.enabled is False

    # 메모리 messages 검증: assistant의 tool_calls 뒤에 각각 c1, c2 tool 메시지가 와야 함
    msgs = res["messages"]
    asst_idx = next(i for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls"))
    tool_c1 = msgs[asst_idx + 1]
    tool_c2 = msgs[asst_idx + 2]

    assert tool_c1["role"] == "tool"
    assert tool_c1["tool_call_id"] == "c1"
    assert json.loads(tool_c1["content"]).get("x") == 100

    assert tool_c2["role"] == "tool"
    assert tool_c2["tool_call_id"] == "c2"
    assert json.loads(tool_c2["content"]).get("sum") == 10

    # DB 원장 검증
    db_msgs = db.get_messages_as_conversation(agent.session_id)
    db_asst_idx = next(i for i, m in enumerate(db_msgs) if m.get("role") == "assistant" and m.get("tool_calls"))
    db_tool_c1 = db_msgs[db_asst_idx + 1]
    db_tool_c2 = db_msgs[db_asst_idx + 2]

    assert db_tool_c1["role"] == "tool"
    assert db_tool_c1["tool_call_id"] == "c1"
    assert db_tool_c2["role"] == "tool"
    assert db_tool_c2["tool_call_id"] == "c2"
