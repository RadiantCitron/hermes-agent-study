"""구현지시서 040 인수 테스트: 확장 단위·단계·실패 정책과 압축 이관."""
import json
from pathlib import Path
from typing import Any
import pytest

from hermes_core.agent.agent import HermesAgent
from hermes_core.agent.events import EVENT_EXTENSION_ERROR, AgentEvent
from hermes_core.agent.turn_hooks import AFTER_TOOL_CALLS, TurnContext
from hermes_core.extensions.contract import (
    Extension,
    ExtensionHost,
    MiddlewarePhase,
    TurnScope,
)
from hermes_core.llm_client import LLMResponse
from hermes_core.session_db import SessionDB
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


def test_scenario_1_and_2_middleware_phase_and_registration_order():
    """시나리오 1 & 2: TRANSACTION, GUARD, POLICY 순 등록 시 진입 순서는 GUARD -> POLICY -> TRANSACTION."""
    dispatcher = ToolDispatcher()
    execution_order = []

    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    def mw_t(call: ToolCall, next_fn):
        execution_order.append("T")
        return next_fn(call)

    def mw_g(call: ToolCall, next_fn):
        execution_order.append("G")
        return next_fn(call)

    def mw_p1(call: ToolCall, next_fn):
        execution_order.append("P1")
        return next_fn(call)

    def mw_p2(call: ToolCall, next_fn):
        execution_order.append("P2")
        return next_fn(call)

    dispatcher.add_middleware(mw_t, phase=MiddlewarePhase.TRANSACTION)
    dispatcher.add_middleware(mw_g, phase=MiddlewarePhase.GUARD)
    dispatcher.add_middleware(mw_p1, phase=MiddlewarePhase.POLICY)
    dispatcher.add_middleware(mw_p2, phase=MiddlewarePhase.POLICY)

    res = dispatcher.dispatch(ToolCall(name="echo", args={"x": 1}))
    assert json.loads(res)["success"] is True
    assert execution_order == ["G", "P1", "P2", "T"]


def test_scenario_3_guard_exception_before_next(tmp_path: Path):
    """시나리오 3: GUARD 미들웨어가 next 호출 전에 예외 -> 도구 1회 실행, 정상 도구 결과, extension_error 이벤트 1건."""
    events: list[AgentEvent] = []
    db = SessionDB(tmp_path / "test.db")
    dispatcher = ToolDispatcher()
    tool_call_count = 0

    def echo_handler(x=0):
        nonlocal tool_call_count
        tool_call_count += 1
        return json.dumps({"success": True, "x": x})

    dispatcher.register_tool("echo", echo_handler, ECHO_SCHEMA)

    class GuardFailBeforeExtension:
        name = "guard_fail_before"

        def install(self, host: ExtensionHost) -> None:
            def mw(call: ToolCall, next_fn):
                raise RuntimeError("guard boom before next")
            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    client = MockLLMClient([
        LLMResponse(
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 10}'}}],
        ),
        LLMResponse(content="done"),
    ])
    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        event_sink=events.append,
        quiet_mode=True,
    )
    agent.install_extension(GuardFailBeforeExtension())

    res = agent.run_conversation("run echo")
    assert res["stopped_reason"] == "completed"
    assert tool_call_count == 1

    # 이벤트 검증
    ext_errs = [e for e in events if e.kind == EVENT_EXTENSION_ERROR]
    assert len(ext_errs) == 1
    assert ext_errs[0].data["name"] == "guard_fail_before"
    assert ext_errs[0].data["slot"] == "GUARD"

    # agent.extensions 검증
    target_record = next(r for r in agent.extensions if r.name == "guard_fail_before")
    assert target_record.failures == 1


def test_scenario_4_guard_exception_after_next(tmp_path: Path):
    """시나리오 4: GUARD 미들웨어가 next 호출 뒤에 예외 -> 도구 1회만 실행, 결과는 도구 결과, extension_error 1건."""
    events: list[AgentEvent] = []
    db = SessionDB(tmp_path / "test.db")
    dispatcher = ToolDispatcher()
    tool_call_count = 0

    def echo_handler(x=0):
        nonlocal tool_call_count
        tool_call_count += 1
        return json.dumps({"success": True, "x": x})

    dispatcher.register_tool("echo", echo_handler, ECHO_SCHEMA)

    class GuardFailAfterExtension:
        name = "guard_fail_after"

        def install(self, host: ExtensionHost) -> None:
            def mw(call: ToolCall, next_fn):
                next_fn(call)
                raise RuntimeError("guard boom after next")
            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    client = MockLLMClient([
        LLMResponse(
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 20}'}}],
        ),
        LLMResponse(content="done"),
    ])
    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        event_sink=events.append,
        quiet_mode=True,
    )
    agent.install_extension(GuardFailAfterExtension())

    res = agent.run_conversation("run echo")
    assert res["stopped_reason"] == "completed"
    # 도구 1회만 실행 (재실행되지 않음)
    assert tool_call_count == 1

    ext_errs = [e for e in events if e.kind == EVENT_EXTENSION_ERROR]
    assert len(ext_errs) == 1
    assert ext_errs[0].data["name"] == "guard_fail_after"


def test_scenario_5_policy_exception_before_next(tmp_path: Path):
    """시나리오 5: POLICY 미들웨어가 next 호출 전에 예외 -> 도구 0회, 결과는 Tool failed 오류 JSON, extension_error 1건."""
    events: list[AgentEvent] = []
    db = SessionDB(tmp_path / "test.db")
    dispatcher = ToolDispatcher()
    tool_call_count = 0

    def echo_handler(x=0):
        nonlocal tool_call_count
        tool_call_count += 1
        return json.dumps({"success": True, "x": x})

    dispatcher.register_tool("echo", echo_handler, ECHO_SCHEMA)

    class PolicyFailExtension:
        name = "policy_fail"

        def install(self, host: ExtensionHost) -> None:
            def mw(call: ToolCall, next_fn):
                raise RuntimeError("policy blocked")
            host.add_tool_middleware(MiddlewarePhase.POLICY, mw)

    client = MockLLMClient([
        LLMResponse(
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 30}'}}],
        ),
        LLMResponse(content="done"),
    ])
    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        event_sink=events.append,
        quiet_mode=True,
    )
    agent.install_extension(PolicyFailExtension())

    res = agent.run_conversation("run echo")
    assert tool_call_count == 0

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    tool_content = json.loads(tool_msg["content"])
    assert tool_content["success"] is False
    assert "Tool 'echo' failed: policy blocked" in tool_content["error"]

    ext_errs = [e for e in events if e.kind == EVENT_EXTENSION_ERROR]
    assert len(ext_errs) == 1
    assert ext_errs[0].data["name"] == "policy_fail"


def test_scenario_6_transaction_exception_before_or_after():
    """시나리오 6: TRANSACTION 미들웨어가 예외 -> 오류 JSON 반환, 예외가 루프로 새지 않음."""
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    def tx_fail(call: ToolCall, next_fn):
        raise RuntimeError("db tx aborted")

    dispatcher.add_middleware(tx_fail, phase=MiddlewarePhase.TRANSACTION)
    res = dispatcher.dispatch(ToolCall(name="echo", args={"x": 1}))
    parsed = json.loads(res)
    assert parsed["success"] is False
    assert "Tool 'echo' failed: db tx aborted" in parsed["error"]


def test_scenario_8_duplicate_extension_raises_value_error(tmp_path: Path):
    """시나리오 8: 같은 이름 확장을 두 번 install_extension -> ValueError."""
    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(session_db=db, llm_client=MockLLMClient([]), quiet_mode=True)

    class DummyExt:
        name = "dummy"

        def install(self, host: ExtensionHost) -> None:
            pass

    agent.install_extension(DummyExt())
    with pytest.raises(ValueError, match="Extension 'dummy' is already installed"):
        agent.install_extension(DummyExt())


def test_scenario_9_extension_records_reflection(tmp_path: Path):
    """시나리오 9: 확장 E가 GUARD 미들웨어 1개와 after_tool_calls 훅 1개 설치 -> agent.extensions에 compression과 E의 기록 2건 반영."""
    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(session_db=db, llm_client=MockLLMClient([]), quiet_mode=True)

    class ComplexExt:
        name = "complex_ext"

        def install(self, host: ExtensionHost) -> None:
            host.add_tool_middleware(MiddlewarePhase.GUARD, lambda c, n: n(c))
            host.add_turn_hook(AFTER_TOOL_CALLS, lambda ctx: None)

    agent.install_extension(ComplexExt())

    exts = agent.extensions
    # compression 1건 + complex_ext 2건 = 3건
    assert any(r.name == "compression" and r.kind == "turn_hook" and r.slot == "before_llm" for r in exts)
    assert any(r.name == "complex_ext" and r.kind == "tool_middleware" and r.slot == "GUARD" for r in exts)
    assert any(r.name == "complex_ext" and r.kind == "turn_hook" and r.slot == "after_tool_calls" for r in exts)


def test_scenario_10_extension_slot_isolation_across_turns_and_agents(tmp_path: Path):
    """시나리오 10: 미들웨어가 call.scope.state(name)에 호출 횟수를 쌓는 확장, 두 턴 실행 시 각 턴에서 1부터 다시 세고 에이전트 간 격리."""
    db = SessionDB(tmp_path / "test.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    class CounterExt:
        name = "counter"

        def install(self, host: ExtensionHost) -> None:
            def mw(call: ToolCall, next_fn):
                if call.scope is not None:
                    st = call.scope.state("counter")
                    st["calls"] = st.get("calls", 0) + 1
                    call.scope.report("turn_calls", st["calls"])
                return next_fn(call)

            host.add_tool_middleware(MiddlewarePhase.POLICY, mw)

    client1 = MockLLMClient([
        LLMResponse(content=None, tool_calls=[{"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]),
        LLMResponse(content="turn 1 done"),
        LLMResponse(content=None, tool_calls=[{"id": "2", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]),
        LLMResponse(content="turn 2 done"),
    ])
    agent1 = HermesAgent(session_id="s1", session_db=db, dispatcher=dispatcher, llm_client=client1, quiet_mode=True)
    agent1.install_extension(CounterExt())

    r1 = agent1.run_conversation("t1")
    assert r1["turn_calls"] == 1

    r2 = agent1.run_conversation("t2")
    assert r2["turn_calls"] == 1  # 턴 칸이므로 새 턴에서 다시 1

    # 두 번째 에이전트 생성 (공유 dispatcher)
    client2 = MockLLMClient([
        LLMResponse(content=None, tool_calls=[{"id": "3", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]),
        LLMResponse(content="agent 2 done"),
    ])
    agent2 = HermesAgent(session_id="s2", session_db=db, dispatcher=dispatcher, llm_client=client2, quiet_mode=True)
    r3 = agent2.run_conversation("a2")
    assert r3["turn_calls"] == 1


def test_scenario_11_guard_failure_without_scope(caplog: pytest.LogCaptureFixture):
    """시나리오 11: scope=None인 직접 handle_function_call에서 GUARD 미들웨어가 예외 -> 도구 1회 실행, 경고 로그만."""
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    def guard_fail(call: ToolCall, next_fn):
        raise RuntimeError("boom")

    dispatcher.add_middleware(guard_fail, phase=MiddlewarePhase.GUARD, name="test_guard")
    res = dispatcher.handle_function_call("echo", {"x": 99})
    assert json.loads(res)["success"] is True
    assert any("GUARD middleware 'test_guard' failed in phase GUARD: boom" in r.message for r in caplog.records)


def test_scenario_15_contract_imports():
    """시나리오 15: extensions/contract.py는 agent 및 tools를 import하지 않는다."""
    import ast
    contract_path = Path(__file__).parents[2] / "src" / "hermes_core" / "extensions" / "contract.py"
    source = contract_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "agent" not in alias.name
                assert "tools" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "agent" not in mod
            assert "tools" not in mod
