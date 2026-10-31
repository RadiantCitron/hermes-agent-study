"""구현지시서 030 인수 테스트: 대화 무결성 (훅 읽기 전용, replace_transcript)."""
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import pytest

from hermes_core.agent.agent import HermesAgent
from hermes_core.agent.events import AgentEvent, EVENT_HOOK_ERROR
from hermes_core.agent.turn_hooks import (
    TurnContext,
    BEFORE_LLM,
    AFTER_TOOL_CALLS,
)
from hermes_core.llm_client import LLMResponse
from hermes_core.session_db import SessionDB


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


def _create_agent(tmp_path: Path, responses: list[Any], event_sink=None):
    db = SessionDB(tmp_path / "test.db")
    client = MockLLMClient(responses)
    agent = HermesAgent(
        model="test-model",
        llm_client=client,
        session_db=db,
        session_id="test_session",
        event_sink=event_sink,
        quiet_mode=True,
    )
    return agent, db, client


def test_scenario_2_hook_modifies_system_message_content(tmp_path: Path):
    """시나리오 2: 훅이 ctx.messages[0]['content'] = 'HACK' -> LLM 입력 시스템 메시지는 원래 내용, hook_error의 error에 'system message modified by hook'."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def tamper_sys(ctx: TurnContext):
        ctx.messages[0]["content"] = "HACK"

    agent.hooks.register(BEFORE_LLM, tamper_sys)
    agent.run_conversation("hello")

    last_call = client.call_history[-1]
    assert last_call["messages"][0]["content"] != "HACK"

    hook_errors = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_errors) == 1
    assert "system message modified by hook" in hook_errors[0].data["error"]


def test_scenario_3_hook_pops_system_message(tmp_path: Path):
    """시나리오 3: 훅이 ctx.messages.pop(0) -> LLM 입력 맨 앞이 원래 시스템 메시지."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def pop_sys(ctx: TurnContext):
        ctx.messages.pop(0)

    agent.hooks.register(BEFORE_LLM, pop_sys)
    agent.run_conversation("hello")

    last_call = client.call_history[-1]
    assert last_call["messages"][0]["role"] == "system"
    assert last_call["messages"][0]["content"] != ""

    hook_errors = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_errors) == 1
    assert "system message modified by hook" in hook_errors[0].data["error"]


def test_scenario_4_hook_modifies_user_message(tmp_path: Path):
    """시나리오 4: 훅이 ctx.messages[-1]['content'] = '변조'(사용자 메시지) -> LLM 입력의 사용자 메시지는 원래 내용, hook_error 1건."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def tamper_user(ctx: TurnContext):
        ctx.messages[-1]["content"] = "변조"

    agent.hooks.register(BEFORE_LLM, tamper_user)
    agent.run_conversation("hello")

    last_call = client.call_history[-1]
    assert last_call["messages"][-1]["content"] == "hello"

    hook_errors = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_errors) == 1
    assert "transcript modified outside replace_transcript" in hook_errors[0].data["error"]


def test_scenario_5_and_8_replace_transcript_success(tmp_path: Path):
    """시나리오 5 & 8: 훅이 ctx.replace_transcript([system, summary], summary_index=0)."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    agent._last_input_tokens = 999
    agent._last_message_count = 10

    def replace_hook(ctx: TurnContext):
        sys_msg = ctx.messages[0]
        ctx.replace_transcript([sys_msg, {"role": "user", "content": "요약"}], summary_index=0)

    agent.hooks.register(BEFORE_LLM, replace_hook)
    agent.run_conversation("hello")

    # LLM 입력이 [system, 요약]
    last_call = client.call_history[-1]
    assert len(last_call["messages"]) == 2
    assert last_call["messages"][1]["content"] == "요약"

    # 원장 활성 행이 요약 + 응답(done) 2건, 그 중 이전 행(hello)은 active=0
    conv = db.get_messages_as_conversation(agent.session_id)
    assert any(m["content"] == "요약" and m["role"] == "user" for m in conv)
    assert any(m["content"] == "done" and m["role"] == "assistant" for m in conv)
    assert not any(m["content"] == "hello" for m in conv)

    # 이전 행(hello)은 active=0
    all_msgs = db.get_messages(agent.session_id)
    hello_msgs = [m for m in all_msgs if m["content"] == "hello"]
    assert len(hello_msgs) == 1
    assert hello_msgs[0]["active"] == 0

    # hook_error 0건
    hook_errors = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_errors) == 0

    # 시나리오 8: agent._last_input_tokens is None, agent._last_message_count is None
    assert agent._last_input_tokens is None
    assert agent._last_message_count is None


def test_scenario_6_replace_transcript_tampered_system_message(tmp_path: Path):
    """시나리오 6: 훅이 맨 앞을 다른 시스템 메시지로 바꾼 목록으로 replace_transcript -> ValueError, 대화/원장 무변경."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    def invalid_replace(ctx: TurnContext):
        ctx.replace_transcript([{"role": "system", "content": "NEW SYS"}, {"role": "user", "content": "요약"}])

    agent.hooks.register(BEFORE_LLM, invalid_replace)
    agent.run_conversation("hello")

    last_call = client.call_history[-1]
    assert last_call["messages"][0]["content"] != "NEW SYS"
    assert last_call["messages"][1]["content"] == "hello"

    # 원장 무변경(hello는 여전히 active=1)
    conv = db.get_messages_as_conversation(agent.session_id)
    assert any(m["content"] == "hello" and m["role"] == "user" for m in conv)

    hook_errors = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_errors) == 1
    assert "system message modified by replace_transcript" in hook_errors[0].data["error"]


def test_scenario_7_replace_transcript_db_failure_rolls_back(tmp_path: Path):
    """시나리오 7: archive_and_compact가 예외를 던지게 주입하고 훅이 replace_transcript 호출(훅은 예외를 잡음) -> 대화/원장 무변경, _last_input_tokens 유지."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    agent._last_input_tokens = 555
    db.archive_and_compact = MagicMock(side_effect=RuntimeError("db error"))

    def catch_replace(ctx: TurnContext):
        try:
            sys_msg = ctx.messages[0]
            ctx.replace_transcript([sys_msg, {"role": "user", "content": "요약"}], summary_index=0)
        except Exception:
            pass

    agent.hooks.register(BEFORE_LLM, catch_replace)
    agent.run_conversation("hello")

    last_call = client.call_history[-1]
    assert last_call["messages"][-1]["content"] == "hello"
    assert agent._last_input_tokens == 555


def test_scenario_9_replace_transcript_token_usage_update(tmp_path: Path):
    """시나리오 9: usage={"prompt_tokens": 10, "completion_tokens": 5}로 실행 시 세션 토큰 합계가 입력 +10, 출력 +5."""
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")])

    def replace_with_usage(ctx: TurnContext):
        sys_msg = ctx.messages[0]
        ctx.replace_transcript(
            [sys_msg, {"role": "user", "content": "요약"}],
            summary_index=0,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    agent.hooks.register(BEFORE_LLM, replace_with_usage)
    agent.run_conversation("hello")

    meta = db.get_session(agent.session_id)
    assert meta["input_tokens"] == 10
    assert meta["output_tokens"] == 5


def test_scenario_10_subsequent_hook_receives_new_transcript(tmp_path: Path):
    """시나리오 10: 같은 지점에 훅 h1(replace_transcript 성공), h2(아무것도 안 함) -> h2는 새 대화의 복사본을 받고, hook_error 0건."""
    events: list[AgentEvent] = []
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")], event_sink=events.append)

    h2_received_msgs = []

    def h1(ctx: TurnContext):
        sys_msg = ctx.messages[0]
        ctx.replace_transcript([sys_msg, {"role": "user", "content": "새대화"}], summary_index=0)

    def h2(ctx: TurnContext):
        h2_received_msgs.extend(ctx.messages)

    agent.hooks.register(BEFORE_LLM, h1)
    agent.hooks.register(BEFORE_LLM, h2)
    agent.run_conversation("hello")

    assert len(h2_received_msgs) == 2
    assert h2_received_msgs[1]["content"] == "새대화"

    hook_errors = [e for e in events if e.kind == EVENT_HOOK_ERROR]
    assert len(hook_errors) == 0


def test_scenario_12_resume_session_matches_memory_transcript(tmp_path: Path):
    """시나리오 12: 압축이 일어난 세션을 HermesAgent(resume=True)로 다시 열어 load_conversation_history() -> 메모리의 마지막 messages[1:]와 같다."""
    agent, db, client = _create_agent(tmp_path, [LLMResponse(content="done")])

    def replace_hook(ctx: TurnContext):
        sys_msg = ctx.messages[0]
        ctx.replace_transcript([sys_msg, {"role": "user", "content": "요약"}], summary_index=0)

    agent.hooks.register(BEFORE_LLM, replace_hook)
    agent.run_conversation("hello")

    # 동일한 session_id로 resume=True 에이전트 생성
    agent2 = HermesAgent(
        model="test-model",
        llm_client=MockLLMClient([LLMResponse(content="second done")]),
        session_db=db,
        session_id=agent.session_id,
        resume=True,
        quiet_mode=True,
    )
    loaded = agent2.load_conversation_history()
    # load_conversation_history()는 [system, ...] 반환하므로 system을 뺀 [1:] 대조
    assert loaded[1:] == [{"role": "user", "content": "요약"}, {"role": "assistant", "content": "done"}]
