"""확장 계약 및 TurnScope/세션 칸 단위 테스트 (Extension Scope Tests).

[인수 테스트 시나리오]
- 010_중립_계약과_턴_세션_칸.md의 1~14 시나리오 검증.
"""

import ast
import json
from pathlib import Path
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.compression_hook import CompressionHook
from hermes_core.agent.turn_hooks import TurnContext
from hermes_core.extensions.contract import RESERVED_RESULT_KEYS, TurnScope
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolCall


def test_scenario_01_contract_imports_only_stdlib():
    """시나리오 1: contract.py 소스를 읽어 hermes_core.agent/tools import가 0건인지 검사."""
    contract_path = Path(__file__).resolve().parent.parent.parent / "src" / "hermes_core" / "extensions" / "contract.py"
    tree = ast.parse(contract_path.read_text(encoding="utf-8"))
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.append(node.module)

    for mod in imported_modules:
        assert not mod.startswith("hermes_core.agent"), f"Forbidden import found: {mod}"
        assert not mod.startswith("hermes_core.tools"), f"Forbidden import found: {mod}"


def test_scenario_02_turn_scope_request_stop_first_wins():
    """시나리오 2: s.request_stop('a'); s.request_stop('b') -> s.stop_reason == 'a'."""
    s = TurnScope(session_id="s")
    s.request_stop("a")
    s.request_stop("b")
    assert s.stop_reason == "a"


def test_scenario_03_turn_scope_state_isolation():
    """시나리오 3: state('x')와 state('y')는 독립적인 dict."""
    s = TurnScope(session_id="s")
    s.state("x")["k"] = 1
    assert s.state("x")["k"] == 1
    assert s.state("y") == {}
    assert s.state("x") is not s.state("y")


def test_scenario_04_turn_scope_empty_state_name_raises():
    """시나리오 4: s.state('') -> ValueError."""
    s = TurnScope(session_id="s")
    with pytest.raises(ValueError, match="State slot name cannot be empty"):
        s.state("")
    with pytest.raises(ValueError, match="Session slot name cannot be empty"):
        s.session_state("")


def test_scenario_05_session_slots_sharing():
    """시나리오 5: 같은 session_slots를 넘긴 두 TurnScope 간 session_state 공유 확인."""
    session_slots = {}
    s1 = TurnScope(session_id="s", session_slots=session_slots)
    s2 = TurnScope(session_id="s", session_slots=session_slots)

    s1.session_state("x")["n"] = 1
    assert s2.session_state("x")["n"] == 1

    s1.state("x")["m"] = 10
    assert "m" not in s2.state("x")


def test_scenario_06_reserved_result_keys_raise():
    """시나리오 6: s.report('final_response', ...) -> ValueError."""
    s = TurnScope(session_id="s")
    with pytest.raises(ValueError, match="Cannot report reserved result key"):
        s.report("final_response", "x")
    with pytest.raises(ValueError, match="Cannot report reserved result key"):
        s.report("messages", [])


def test_scenario_07_turn_hook_scope_report(tmp_path):
    """시나리오 7: before_llm 훅이 ctx.scope.report('custom', 42) 시 result['custom'] == 42."""
    db_path = str(tmp_path / "test_session.db")
    db = SessionDB(db_path)
    client = MockLLMClient(responses=[MockLLMResponse(content="Hello world")])
    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)

    def hook(ctx: TurnContext) -> None:
        ctx.scope.report("custom", 42)

    agent.hooks.register("before_llm", hook)
    result = agent.run_conversation("test")
    assert result.get("custom") == 42


def test_scenario_08_run_state_aliased_to_turn_slots(tmp_path):
    """시나리오 8: run_state와 턴 칸이 동일 저장소를 공유하는지 검증."""
    ctx = TurnContext(messages=[], tools_schema=None, iteration=1, session_id="s")
    ctx.run_state["probe"] = {"v": 1}
    assert ctx.scope.state("probe")["v"] == 1


def test_scenario_09_multi_turn_state_reset_and_session_state_preserved(tmp_path):
    """시나리오 9: 두 번 run_conversation 시 state는 리셋, session_state는 보존."""
    db_path = str(tmp_path / "test_session.db")
    db = SessionDB(db_path)
    client = MockLLMClient(responses=[
        MockLLMResponse(content="Response 1"),
        MockLLMResponse(content="Response 2"),
    ])
    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)

    recorded = []

    def hook(ctx: TurnContext) -> None:
        recorded.append({
            "turn_t": dict(ctx.scope.state("t")),
            "sess_t": dict(ctx.scope.session_state("t")),
        })
        ctx.scope.state("t")["n"] = 1
        ctx.scope.session_state("t")["n"] = 1

    agent.hooks.register("before_llm", hook)

    agent.run_conversation("turn 1")
    agent.run_conversation("turn 2")

    assert len(recorded) == 2
    # 첫 번째 턴 시작 전: 둘 다 비어있음
    assert recorded[0]["turn_t"] == {}
    assert recorded[0]["sess_t"] == {}
    # 두 번째 턴 시작 전: turn_t는 리셋(빈 dict), sess_t는 이전 턴의 기록 보존
    assert recorded[1]["turn_t"] == {}
    assert recorded[1]["sess_t"] == {"n": 1}


def test_scenario_10_different_agents_session_slots_isolation(tmp_path):
    """시나리오 10: 서로 다른 두 에이전트 인스턴스 간 session_state 격리."""
    db_path = str(tmp_path / "test_session.db")
    db = SessionDB(db_path)

    agent1 = HermesAgent(session_db=db, llm_client=MockLLMClient([MockLLMResponse(content="R1")]), quiet_mode=True)
    agent2 = HermesAgent(session_db=db, llm_client=MockLLMClient([MockLLMResponse(content="R2")]), quiet_mode=True)

    def hook1(ctx: TurnContext) -> None:
        ctx.scope.session_state("t")["agent"] = 1

    def hook2(ctx: TurnContext) -> None:
        ctx.scope.session_state("t")["agent"] = 2

    agent1.hooks.register("before_llm", hook1)
    agent2.hooks.register("before_llm", hook2)

    agent1.run_conversation("hello")
    agent2.run_conversation("world")

    assert agent1._session_slots["t"]["agent"] == 1
    assert agent2._session_slots["t"]["agent"] == 2


def test_scenario_11_compression_report_preservation(tmp_path):
    """시나리오 11: 압축이 일어났을 때 result['compression'] == {'count': 1, 'fallback': 0} 보고."""
    db = SessionDB(tmp_path / "g1.db")

    client = MockLLMClient()
    # 1. 요약 응답, 2. 본 호출 응답
    client.queue_response(content="요약 완료 본문")
    client.queue_response(content="최종 답변 완료")

    agent = HermesAgent(session_db=db, llm_client=client, context_window_tokens=500, quiet_mode=True)

    history = [
        {"role": "system", "content": "System Prompt"},
        {"role": "user", "content": "헤드 유저 메시지"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "echo", "content": '{"result": 1}'},
        {"role": "assistant", "content": "중간 답변"},
    ]
    for i in range(10):
        history.append({"role": "user", "content": f"반복 유저 {i} " + ("텍스트 " * 20)})
        history.append({"role": "assistant", "content": f"반복 답변 {i}"})

    res = agent.run_conversation("이번 턴 요청", conversation_history=history)

    assert "compression" in res
    assert res["compression"] == {"count": 1, "fallback": 0}


def test_scenario_12_no_direct_compression_key_in_agent_run_state():
    """시나리오 12: agent.py 소스에 run_state.get('compression') 또는 'compression' 하드코딩 없음 검증."""
    agent_path = Path(__file__).resolve().parent.parent.parent / "src" / "hermes_core" / "agent" / "agent.py"
    content = agent_path.read_text(encoding="utf-8")
    assert 'run_state.get("compression")' not in content
    # result["compression"] 직접 할당 없음 확인
    assert 'result["compression"]' not in content


def test_scenario_13_tool_call_scope_field():
    """시나리오 13: ToolCall 생성 시 기본 scope is None, 위치 인자 호환."""
    tc = ToolCall("echo", {"x": 1})
    assert tc.name == "echo"
    assert tc.args == {"x": 1}
    assert tc.task_id is None
    assert tc.scope is None

    s = TurnScope(session_id="s")
    tc2 = ToolCall("echo", {"x": 2}, "t-1", scope=s)
    assert tc2.scope is s
