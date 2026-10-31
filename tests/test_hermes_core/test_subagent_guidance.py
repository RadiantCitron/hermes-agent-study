"""위임 안내문 및 자식 프롬프트 테스트 (로드맵 07 / 구현지시서 040 인수 시나리오)."""

from datetime import datetime
import json
from pathlib import Path

import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.prompt_builder import (
    CHILD_AGENT_PROMPT,
    MEMORY_GUIDANCE,
    SEARCH_FILES_GUIDANCE,
    SESSION_SEARCH_GUIDANCE,
    SKILLS_GUIDANCE,
    SUBAGENT_GUIDANCE,
    build_child_system_message,
    build_system_prompt,
    select_tools_guidance,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import ToolDispatcher
from hermes_core.tools.memory_tool import MemoryStore


def test_scenario_01_subagent_guidance_constants():
    """시나리오 1: SUBAGENT_GUIDANCE 필수 구절, 글자수 상한(600자), 줄바꿈 트리밍 검증."""
    assert SUBAGENT_GUIDANCE
    assert "delegate_task" in SUBAGENT_GUIDANCE
    assert "goal" in SUBAGENT_GUIDANCE
    assert "context" in SUBAGENT_GUIDANCE
    assert len(SUBAGENT_GUIDANCE) <= 600
    assert not SUBAGENT_GUIDANCE.startswith("\n")
    assert not SUBAGENT_GUIDANCE.endswith("\n")


def test_scenario_02_select_tools_guidance_rules():
    """시나리오 2: select_tools_guidance의 등록 도구별 조건부 선택 규칙 6종 검증."""
    default_8 = ["memory", "session_search", "terminal", "read_file", "write_file", "patch", "skills_list", "skill_view"]
    child_6 = ["terminal", "read_file", "write_file", "patch", "skills_list", "skill_view"]

    # (a) 기본 8종 이름 -> [MEMORY, SESSION_SEARCH, SKILLS] 순서
    assert select_tools_guidance(default_8) == [MEMORY_GUIDANCE, SESSION_SEARCH_GUIDANCE, SKILLS_GUIDANCE]

    # (b) 8종 + delegate_task -> [MEMORY, SESSION_SEARCH, SKILLS, SUBAGENT] 순서
    assert select_tools_guidance(default_8 + ["delegate_task"]) == [
        MEMORY_GUIDANCE,
        SESSION_SEARCH_GUIDANCE,
        SKILLS_GUIDANCE,
        SUBAGENT_GUIDANCE,
    ]

    # (c) 자식 6종 -> [SKILLS]만
    assert select_tools_guidance(child_6) == [SKILLS_GUIDANCE]

    # (d) skills_list만 (skill_view 누락) -> []
    assert select_tools_guidance(["skills_list"]) == []

    # (e) memory만 -> [MEMORY]
    assert select_tools_guidance(["memory"]) == [MEMORY_GUIDANCE]

    # (f) 빈 집합 -> []
    assert select_tools_guidance([]) == []


def test_scenario_03_default_parent_system_prompt_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 3: 위임 미등록 기본 에이전트 프롬프트가 기존 조립 방식과 바이트 단위로 동일하며 delegate_task 미포함 검증."""
    monkeypatch.chdir(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    store = MemoryStore(memory_dir=tmp_path / "memories")
    agent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    agent_prompt = agent._build_system_prompt()
    assert "delegate_task" not in agent_prompt

    # 동일 시각 기준 분리 조립 비교 (17 로드맵: SEARCH_FILES_GUIDANCE 포함)
    now = datetime.now()
    oracle_prompt = build_system_prompt(
        tools_guidance=[MEMORY_GUIDANCE, SESSION_SEARCH_GUIDANCE, SEARCH_FILES_GUIDANCE, SKILLS_GUIDANCE],
        memory_block=store.format_for_system_prompt("memory"),
        context_files_block=None,
        platform="cli",
        start_time=now,
    )

    def _strip_timestamp(p: str) -> str:
        return "\n".join(line for line in p.splitlines() if not line.startswith("Conversation started:"))

    assert _strip_timestamp(agent_prompt) == _strip_timestamp(oracle_prompt)


def test_scenario_04_registered_delegate_tool_in_first_run(tmp_path: Path):
    """시나리오 4: 첫 run_conversation 전에 등록 시 SUBAGENT_GUIDANCE 반영 및 DB 저장값 일치 검증."""
    db = SessionDB(tmp_path / "state.db")
    store = MemoryStore(memory_dir=tmp_path / "memories")
    client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=client, quiet_mode=True)

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
    )

    parent.run_conversation("hello")
    first_sys_msg = client.call_history[0]["messages"][0]["content"]

    assert SUBAGENT_GUIDANCE in first_sys_msg
    assert SKILLS_GUIDANCE in first_sys_msg
    # SUBAGENT_GUIDANCE가 SKILLS_GUIDANCE보다 뒤에 위치
    assert first_sys_msg.index(SUBAGENT_GUIDANCE) > first_sys_msg.index(SKILLS_GUIDANCE)

    sess_row = db.get_session(parent.session_id)
    assert sess_row["system_prompt"] == first_sys_msg


def test_scenario_05_register_after_first_run_does_not_change_prompt(tmp_path: Path):
    """시나리오 5: 첫 run_conversation 이후 등록 시 세션 중 프롬프트 불변(Prompt Caching 보존) 검증."""
    db = SessionDB(tmp_path / "state.db")
    store = MemoryStore(memory_dir=tmp_path / "memories")
    client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=client, quiet_mode=True)

    # 1번째 턴 (위임 미등록 상태)
    parent.run_conversation("turn 1")
    first_sys_msg = client.call_history[0]["messages"][0]["content"]
    assert "delegate_task" not in first_sys_msg

    # 1번째 턴 이후 위임 도구 등록
    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
    )

    # 2번째 턴
    parent.run_conversation("turn 2")
    second_sys_msg = client.call_history[1]["messages"][0]["content"]

    assert first_sys_msg == second_sys_msg
    assert "delegate_task" not in second_sys_msg


def test_scenario_06_child_system_prompt_content_and_isolation(tmp_path: Path):
    """시나리오 6: 자식이 전달받는 첫 호출 시스템 메시지의 구조, focused subagent 구절, 부모 문맥 전달 및 도구 격리 검증."""
    db = SessionDB(tmp_path / "state.db")
    store = MemoryStore(memory_dir=tmp_path / "memories")
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    child_captured_client = []

    def factory():
        c = MockLLMClient([MockLLMResponse(content="child completed")])
        child_captured_client.append(c)
        return c

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p1",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "G1", "context": "C1"}),
                },
            }
        ]
    )
    parent_client.queue_response(content="Parent final.")

    parent.run_conversation("start")

    assert len(child_captured_client) == 1
    child_client = child_captured_client[0]
    first_child_call = child_client.call_history[0]
    child_sys_msg = first_child_call["messages"][0]["content"]

    # 1. focused subagent 핵심 구절 및 C1 문맥 블록 확인
    assert "focused subagent" in child_sys_msg
    assert "## Context from the parent agent\n\nC1" in child_sys_msg

    # 2. SKILLS_GUIDANCE는 포함
    assert SKILLS_GUIDANCE in child_sys_msg

    # 3. 제외 대상 안내문 및 delegate_task 미포함 검증
    assert MEMORY_GUIDANCE not in child_sys_msg
    assert SESSION_SEARCH_GUIDANCE not in child_sys_msg
    assert SUBAGENT_GUIDANCE not in child_sys_msg
    assert "delegate_task" not in child_sys_msg

    # 4. G1은 시스템 메시지에 없고 마지막 user 메시지에만 위치
    assert "G1" not in child_sys_msg
    user_msgs = [m for m in first_child_call["messages"] if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "G1"


def test_scenario_07_build_child_system_message_empty_context():
    """시나리오 7: context가 None, '', 공백일 때 CHILD_AGENT_PROMPT만 반환하고 None 글자 미누출 검증."""
    for empty_val in (None, "", "   \n"):
        res = build_child_system_message(empty_val)
        assert res == CHILD_AGENT_PROMPT
        assert "Context from the parent agent" not in res
        assert "None" not in res


def test_scenario_08_build_child_system_message_trimmed_context():
    """시나리오 8: context의 앞뒤 공백이 제거되고 정확히 1회 마커 뒤에 배치됨을 검증."""
    res = build_child_system_message("  C2\n")
    expected = f"{CHILD_AGENT_PROMPT}\n\n## Context from the parent agent\n\nC2"
    assert res == expected
    assert res.count("C2") == 1


def test_scenario_09_dispatcher_without_tools_omits_all_guidance(tmp_path: Path):
    """시나리오 9: 도구 없는 ToolDispatcher로 생성 시 3대 안내문이 모두 생략됨을 검증."""
    db = SessionDB(tmp_path / "state.db")
    store = MemoryStore(memory_dir=tmp_path / "memories")
    empty_dispatcher = ToolDispatcher()

    agent = HermesAgent(
        session_db=db,
        memory_store=store,
        dispatcher=empty_dispatcher,
        quiet_mode=True,
    )
    prompt = agent._build_system_prompt()

    assert MEMORY_GUIDANCE not in prompt
    assert SESSION_SEARCH_GUIDANCE not in prompt
    assert SKILLS_GUIDANCE not in prompt
    assert SUBAGENT_GUIDANCE not in prompt
    assert "You are Hermes Agent" in prompt
