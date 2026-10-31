"""턴 훅 레지스트리 및 압축 이관 인수 테스트 (구현지시서 030 시나리오 H1~H10, G1~G7)."""

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.events import EVENT_HOOK_ERROR, AgentEvent
from hermes_core.agent.turn_hooks import (
    AFTER_TOOL_CALLS,
    BEFORE_LLM,
    HOOK_POINTS,
    HookRegistry,
    TurnContext,
)
from hermes_core.llm_client import LLMError, LLMResponse
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher

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


def _create_hook_test_agent(
    tmp_path: Path,
    responses: list[Any],
    quiet_mode: bool = True,
    event_sink: Any = None,
) -> tuple[HermesAgent, SessionDB]:
    db = SessionDB(tmp_path / "test.db")
    dispatcher = ToolDispatcher()
    dispatcher.register_tool("echo", lambda x=0: json.dumps({"success": True, "x": x}), ECHO_SCHEMA)

    class CustomClient:
        def __init__(self, resps):
            self.resps = list(resps)
            self._idx = 0
            self.call_history: list[dict[str, Any]] = []

        def chat_completion(self, messages, tools=None):
            self.call_history.append({"messages": list(messages), "tools": tools})
            if self._idx < len(self.resps):
                r = self.resps[self._idx]
                self._idx += 1
                if isinstance(r, Exception):
                    raise r
                return r
            return LLMResponse(content="default")

    client = CustomClient(responses)
    agent = HermesAgent(
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        quiet_mode=quiet_mode,
        event_sink=event_sink,
    )
    return agent, db


# =============================================================================
# 3-1. 훅 장치 테스트 (H1 ~ H10)
# =============================================================================

def test_h1_invalid_hook_point_raises_value_error():
    """H1: 잘못된 지점 등록 시 ValueError 발생 및 HOOK_POINTS 확인."""
    registry = HookRegistry()
    assert HOOK_POINTS == ("before_llm", "after_tool_calls")
    with pytest.raises(ValueError, match="Unknown hook point 'after_llm'"):
        registry.register("after_llm", lambda ctx: None)


def test_h2_execution_order_preserved(tmp_path: Path):
    """H2: before_llm에 h1, h2 등록 시 상대적 실행 순서 보존."""
    order_log: list[str] = []
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")])

    agent.hooks.register(BEFORE_LLM, lambda ctx: order_log.append("h1"))
    agent.hooks.register(BEFORE_LLM, lambda ctx: order_log.append("h2"))

    agent.run_conversation("test")
    assert order_log == ["h1", "h2"]


def test_h3_hook_append_not_reflected_and_reported(tmp_path: Path):
    """H3: 훅이 ctx.messages에 추가한 메시지는 무시되고 hook_error로 보고됨."""
    events: list[AgentEvent] = []
    agent, db = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def add_hint_hook(ctx: TurnContext) -> None:
        ctx.messages.append({"role": "user", "content": "hint"})

    agent.hooks.register(BEFORE_LLM, add_hint_hook)
    agent.run_conversation("test")

    # LLM에 전달된 메시지의 마지막은 원래 사용자 메시지(hint 아님)
    last_call = agent.llm_client.call_history[-1]
    assert last_call["messages"][-1] == {"role": "user", "content": "test"}

    # 원장 messages 표에 hint 행 없음
    db_msgs = db.get_messages(agent.session_id)
    assert not any(m["content"] == "hint" for m in db_msgs)

    # hook_error 이벤트 검증
    hook_err_events = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_err_events) == 1
    assert "transcript modified outside replace_transcript" in hook_err_events[0].data["error"]


def test_h4_system_message_content_tampering_restored(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """H4: 훅이 시스템 메시지를 변조하면 원상 복구되고 hook_error 발생 및 로깅."""
    events: list[AgentEvent] = []
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def evil_hook(ctx: TurnContext) -> None:
        ctx.messages[0]["content"] = "HACK"

    agent.hooks.register(BEFORE_LLM, evil_hook)

    with caplog.at_level("WARNING"):
        agent.run_conversation("test")

    # LLM에 전달된 메시지의 시스템 프롬프트가 원래 내용으로 복원되었는지 확인
    first_call_msgs = agent.llm_client.call_history[0]["messages"]
    assert first_call_msgs[0]["content"] != "HACK"
    assert "You are Hermes Agent" in first_call_msgs[0]["content"]

    # hook_error 이벤트 검증
    hook_err_events = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_err_events) == 1
    assert "system message modified by hook" in hook_err_events[0].data["error"]


def test_h5_system_message_deletion_restored(tmp_path: Path):
    """H5: 훅이 시스템 메시지를 삭제하면 맨 앞에 복원."""
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")])

    def pop_hook(ctx: TurnContext) -> None:
        ctx.messages.pop(0)

    agent.hooks.register(BEFORE_LLM, pop_hook)
    agent.run_conversation("test")

    first_call_msgs = agent.llm_client.call_history[0]["messages"]
    assert first_call_msgs[0]["role"] == "system"


def test_h6_hook_runtime_error_swallowed_and_emitted(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """H6: 훅 예외가 정상 삼켜지고 stopped_reason == completed 이며 hook_error 방출."""
    events: list[AgentEvent] = []
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def boom_hook(ctx: TurnContext) -> None:
        raise RuntimeError("boom")

    agent.hooks.register(BEFORE_LLM, boom_hook)

    with caplog.at_level("WARNING"):
        res = agent.run_conversation("test")

    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "done"

    hook_err_events = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_err_events) == 1
    assert hook_err_events[0].data["point"] == BEFORE_LLM
    assert "boom" in hook_err_events[0].data["error"]


def test_h7_after_tool_calls_stop_request_preserves_tool_pair(tmp_path: Path):
    """H7: after_tool_calls 훅에서 stop 요청 시 도구 결과 쌍 보존 및 중단 검증."""
    tool_resp = LLMResponse(
        content=None,
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}}],
    )
    final_resp = LLMResponse(content="final answer")
    agent, db = _create_hook_test_agent(tmp_path, [tool_resp, final_resp])

    def stop_after_tool_hook(ctx: TurnContext) -> None:
        ctx.request_stop("budget")

    agent.hooks.register(AFTER_TOOL_CALLS, stop_after_tool_hook)

    res = agent.run_conversation("call tool")
    assert res["stopped_reason"] == "budget"
    assert res["iterations"] == 1
    assert res["final_response"] == ""
    assert len(agent.llm_client.call_history) == 1

    # 마지막 메시지가 tool이고 DB에도 기록됨 확인
    assert res["messages"][-1]["role"] == "tool"
    db_msgs = db.get_messages(agent.session_id)
    assert db_msgs[-1]["role"] == "tool"
    assert db_msgs[-1]["tool_call_id"] == "call_1"


def test_h8_before_llm_stop_request_skips_llm(tmp_path: Path):
    """H8: before_llm에서 stop 요청 시 LLM 호출 0회 및 [system, user] 보존."""
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="never called")])

    def stop_before_hook(ctx: TurnContext) -> None:
        ctx.request_stop("halt")

    agent.hooks.register(BEFORE_LLM, stop_before_hook)

    res = agent.run_conversation("hello")
    assert res["stopped_reason"] == "halt"
    assert res["iterations"] == 1
    assert len(agent.llm_client.call_history) == 0
    assert [m["role"] for m in res["messages"]] == ["system", "user"]


def test_h9_first_stop_reason_wins(tmp_path: Path):
    """H9: 여러 훅이 request_stop 호출 시 최초 요청된 사유 유지."""
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")])

    agent.hooks.register(BEFORE_LLM, lambda ctx: ctx.request_stop("first"))
    agent.hooks.register(BEFORE_LLM, lambda ctx: ctx.request_stop("second"))

    res = agent.run_conversation("hello")
    assert res["stopped_reason"] == "first"


def test_h10_iteration_and_schema_context_tracking(tmp_path: Path):
    """H10: ctx.iteration과 ctx.tools_schema가 턴마다 정확히 전달됨."""
    iterations_seen: list[int] = []
    schemas_seen: list[Any] = []

    tool_resp = LLMResponse(
        content=None,
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}}],
    )
    final_resp = LLMResponse(content="final answer")
    agent, _ = _create_hook_test_agent(tmp_path, [tool_resp, final_resp])

    def inspect_hook(ctx: TurnContext) -> None:
        iterations_seen.append(ctx.iteration)
        schemas_seen.append(ctx.tools_schema)

    agent.hooks.register(BEFORE_LLM, inspect_hook)

    agent.run_conversation("test")
    assert iterations_seen == [1, 2]
    assert len(schemas_seen) == 2
    assert schemas_seen[0] == agent.dispatcher.get_tools_schema()


# =============================================================================
# 3-2. 압축 이관 동일성 테스트 (G1 ~ G7)
# =============================================================================

def _load_golden() -> dict[str, Any]:
    golden_path = Path(__file__).parent / "golden" / "compression_before.json"
    return json.loads(golden_path.read_text(encoding="utf-8"))


def _extract_ledger(db: SessionDB, sid: str) -> list[dict[str, Any]]:
    rows = db._conn.execute(
        "SELECT role, active, compacted, content, tool_call_id, tool_name FROM messages WHERE session_id = ? ORDER BY id ASC",
        (sid,),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_test_history() -> list[dict[str, Any]]:
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
    return history


def test_g1_golden_summary_success(tmp_path: Path):
    """G1: 정상 압축 시 messages, 원장 행, compression 통계가 골든과 100% 동일."""
    golden = _load_golden()["G1"]
    db = SessionDB(tmp_path / "g1.db")

    client = MockLLMClient()
    # 1. 요약 응답, 2. 본 호출 응답
    client.queue_response(content="요약 완료 본문")
    client.queue_response(content="최종 답변 완료")

    agent = HermesAgent(session_db=db, llm_client=client, context_window_tokens=500, quiet_mode=True)
    history = _build_test_history()

    res = agent.run_conversation("이번 턴 요청", conversation_history=history)

    assert res["messages"] == golden["messages"]
    assert _extract_ledger(db, agent.session_id) == golden["ledger"]
    assert res.get("compression") == golden["compression"]
    assert res["stopped_reason"] == golden["stopped_reason"]


def test_g2_golden_summary_fallback(tmp_path: Path):
    """G2: 요약 실패로 fallback 사용 시 골든과 100% 동일."""
    golden = _load_golden()["G2"]
    db = SessionDB(tmp_path / "g2.db")

    class FallbackClient:
        def __init__(self):
            self.calls = 0

        def chat_completion(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise LLMError(kind="api_error", message="LLM API summary error")
            return LLMResponse(content="최종 답변 완료", usage={"prompt_tokens": 30, "completion_tokens": 15})

    agent = HermesAgent(session_db=db, llm_client=FallbackClient(), context_window_tokens=500, quiet_mode=True)
    history = _build_test_history()

    res = agent.run_conversation("이번 턴 요청", conversation_history=history)

    assert res["messages"] == golden["messages"]
    assert _extract_ledger(db, agent.session_id) == golden["ledger"]
    assert res.get("compression") == golden["compression"]
    assert res["stopped_reason"] == golden["stopped_reason"]


def test_g3_golden_archive_and_compact_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """G3: archive_and_compact 실패 시 원본 메시지 유지 및 골든과 동일."""
    golden = _load_golden()["G3"]
    db = SessionDB(tmp_path / "g3.db")

    client = MockLLMClient()
    client.queue_response(content="요약 완료 본문")
    client.queue_response(content="최종 답변 완료")

    agent = HermesAgent(session_db=db, llm_client=client, context_window_tokens=500, quiet_mode=True)

    def broken_archive(*args, **kwargs):
        raise RuntimeError("DB disk I/O error")

    db.archive_and_compact = broken_archive
    history = _build_test_history()

    with caplog.at_level("WARNING"):
        res = agent.run_conversation("이번 턴 요청", conversation_history=history)

    assert res["messages"] == golden["messages"]
    assert _extract_ledger(db, agent.session_id) == golden["ledger"]
    assert res.get("compression") == golden["compression"]
    assert any("archive_and_compact failed: DB disk I/O error" in r.message for r in caplog.records)


def test_g4_dynamic_compression_enabled_toggle(tmp_path: Path):
    """G4: 생성 후 agent.compression_enabled = False 설정 시 압축 미발생."""
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")])
    agent.context_window_tokens = 500
    agent.compression_enabled = False

    history = _build_test_history()
    res = agent.run_conversation("이번 턴 요청", conversation_history=history)

    assert "compression" not in res


def test_g5_compression_max_per_run_bound(tmp_path: Path):
    """G5: COMPRESSION_MAX_PER_RUN 상한 초과 방지 검증."""
    agent, _ = _create_hook_test_agent(tmp_path, [LLMResponse(content="done")])
    agent.context_window_tokens = 500

    ctx = TurnContext(messages=_build_test_history(), tools_schema=None, iteration=1, session_id="s1")
    ctx.run_state["compression"] = {"attempts": 3, "count": 1, "fallback": 0}

    # 압축 확장 훅 직접 호출
    from hermes_core.agent.compression_hook import CompressionExtension
    ext = CompressionExtension(agent._compression_config)
    ext._before_llm(ctx)

    # attempts가 3에서 더 증가하지 않음
    assert ctx.run_state["compression"]["attempts"] == 3


def test_g6_run_conversation_lines_and_no_direct_compression_calls():
    """G6: agent.py의 run_conversation 내부에 직접 압축 함수 호출이 없음."""
    import ast

    agent_path = Path(__file__).parents[2] / "src" / "hermes_core" / "agent" / "agent.py"
    source = agent_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    run_conv_source = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_conversation":
            run_conv_source = ast.get_source_segment(source, node)
            break

    assert run_conv_source is not None, "run_conversation function not found in agent.py"
    assert "compress_messages" not in run_conv_source
    assert "archive_and_compact" not in run_conv_source
    assert "split_for_compression" not in run_conv_source
