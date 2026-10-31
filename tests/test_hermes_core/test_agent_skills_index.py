"""에이전트 스킬 인덱스 연동 및 세션 고정 테스트 (08 로드맵 020).

[인수 테스트 시나리오]
- 020_에이전트_연동과_세션_고정.md 명세의 시나리오 1~12 검증.
- 기본 꺼짐(opt-in), 계층 순서 결합, 세션 중 불변 고정, 스킬 도구 부재/폴더 부재 시 프롬프트 바이트 동일성,
  압축 시 헤드 보존, 서브에이전트(자식) 격리(자식 프롬프트 미주입), 파일 시스템 읽기 전용 불변.
"""

from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    MEMORY_GUIDANCE,
    SESSION_SEARCH_GUIDANCE,
    SKILLS_GUIDANCE,
    build_system_prompt,
)
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import create_default_dispatcher


BLOCK = (
    "<available_skills>\n"
    "- deploy-guide: Deploy steps\n"
    "- release-procedure: Team release procedure\n"
    "</available_skills>"
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 29, 12, 0, 0)


@pytest.fixture(autouse=True)
def freeze_time(monkeypatch):
    """프롬프트의 Conversation started: 일시 분 경계 흔들림 방지."""
    monkeypatch.setattr("hermes_core.agent.prompt_builder.datetime", FixedDateTime)


@pytest.fixture
def skills_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """공통 준비: 스킬 2개(release, deploy)가 존재하는 격리 환경."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    skills_dir = home / "skills"
    release_dir = skills_dir / "release"
    release_dir.mkdir(parents=True)
    (release_dir / "SKILL.md").write_text(
        "---\nname: release-procedure\ndescription: Team release procedure\n---\n# Content\n",
        encoding="utf-8",
    )

    deploy_dir = skills_dir / "deploy"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "SKILL.md").write_text(
        "---\nname: deploy-guide\ndescription: Deploy steps\n---\n# Content\n",
        encoding="utf-8",
    )

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.chdir(ws)

    return {"home": home, "skills_dir": skills_dir, "workspace": ws}


def test_scenario_01_signature_inspection():
    """시나리오 1: HermesAgent.__init__ 및 build_system_prompt의 매개변수 위치 및 기본값."""
    sig_agent = inspect.signature(HermesAgent.__init__)
    assert "skills_index" in sig_agent.parameters
    skills_index_param = sig_agent.parameters["skills_index"]
    assert skills_index_param.name == "skills_index"
    assert skills_index_param.default is False

    sig_bsp = inspect.signature(build_system_prompt)
    params_bsp = list(sig_bsp.parameters.values())
    last_bsp_param = params_bsp[-1]
    assert last_bsp_param.name == "skills_index_block"
    assert last_bsp_param.default is None


def test_scenario_02_default_disabled_byte_identical(skills_env, tmp_path, monkeypatch):
    """시나리오 2: skills_index=False(기본값)일 때 스킬 이름 미주입 및 폴더 없는 환경과 바이트 동일."""
    # 스킬 있는 환경에서 인자 없이 생성
    agent_with_skills = HermesAgent(quiet_mode=True)
    prompt_with_skills = agent_with_skills._build_system_prompt()

    assert "<available_skills>" not in prompt_with_skills
    assert "deploy-guide" not in prompt_with_skills
    assert "release-procedure" not in prompt_with_skills
    assert SKILLS_GUIDANCE in prompt_with_skills

    # 스킬 폴더가 아예 없는 별도 환경에서 인자 없이 생성
    empty_home = tmp_path / "empty_hermes"
    empty_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(empty_home))

    agent_no_skills = HermesAgent(quiet_mode=True)
    prompt_no_skills = agent_no_skills._build_system_prompt()

    assert prompt_with_skills.encode("utf-8") == prompt_no_skills.encode("utf-8")


def test_scenario_03_enabled_skills_index_injected_exactly_once(skills_env):
    """시나리오 3: skills_index=True일 때 BLOCK이 정확히 1번 주입됨."""
    agent = HermesAgent(skills_index=True, quiet_mode=True)
    prompt = agent._build_system_prompt()
    assert prompt.count(BLOCK) == 1


def test_scenario_04_layer_ordering(skills_env):
    """시나리오 4: 계층 순서 검증 (IDENTITY < MEMORY < SESSION < SKILLS < BLOCK < CALLER < CONTEXT)."""
    ws = skills_env["workspace"]
    agents_md = ws / "AGENTS.md"
    agents_md.write_text("CONTEXT_MARK", encoding="utf-8")

    agent = HermesAgent(
        skills_index=True,
        system_message="CALLER_MARK",
        quiet_mode=True,
    )
    prompt = agent._build_system_prompt()

    assert prompt.startswith(DEFAULT_AGENT_IDENTITY)

    idx_identity = prompt.index(DEFAULT_AGENT_IDENTITY)
    idx_memory = prompt.index(MEMORY_GUIDANCE)
    idx_session = prompt.index(SESSION_SEARCH_GUIDANCE)
    idx_skills = prompt.index(SKILLS_GUIDANCE)
    idx_block = prompt.index(BLOCK)
    idx_caller = prompt.index("CALLER_MARK")
    idx_context = prompt.index("CONTEXT_MARK")

    assert (
        idx_identity
        < idx_memory
        < idx_session
        < idx_skills
        < idx_block
        < idx_caller
        < idx_context
    )


def test_scenario_05_session_db_persisted_system_prompt(skills_env):
    """시나리오 5: session_db에 저장된 system_prompt와 반환된 프롬프트가 일치하고 BLOCK 포함."""
    agent = HermesAgent(skills_index=True, quiet_mode=True)
    prompt = agent._build_system_prompt()

    row = agent.session_db.get_session(agent.session_id)
    assert row is not None
    assert row["system_prompt"] == prompt
    assert BLOCK in row["system_prompt"]


def test_scenario_06_session_frozen_snapshot(skills_env):
    """시나리오 6: 첫 조립 후 스킬이 추가되어도 다시 읽지 않고(세션 중 고정) caller_prompt만 반영."""
    agent = HermesAgent(skills_index=True, quiet_mode=True)
    prompt1 = agent._build_system_prompt()
    assert BLOCK in prompt1

    # 첫 조립 이후 신규 스킬 추가
    late_dir = skills_env["skills_dir"] / "late"
    late_dir.mkdir(parents=True)
    (late_dir / "SKILL.md").write_text(
        "---\nname: late-skill\ndescription: Late added skill\n---\n# Content\n",
        encoding="utf-8",
    )

    # ① 인자 없이 재호출 -> 첫 결과와 동일
    prompt2 = agent._build_system_prompt()
    assert prompt2 == prompt1
    assert "late-skill" not in prompt2

    # ② system_message를 넘겨 재조립 유도 -> BLOCK은 유지되고 late-skill은 없으며 새 지시문 반영
    prompt3 = agent._build_system_prompt(system_message="NEW_INSTRUCTION")
    assert BLOCK in prompt3
    assert "late-skill" not in prompt3
    assert "NEW_INSTRUCTION" in prompt3


def test_scenario_07_no_skills_dir_creates_no_dir_and_byte_identical(tmp_path, monkeypatch):
    """시나리오 7: skills_index=True여도 skills 폴더가 없으면 미생성 원칙 준수 및 바이트 동일."""
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(empty_home))

    agent_enabled = HermesAgent(skills_index=True, quiet_mode=True)
    prompt_enabled = agent_enabled._build_system_prompt()

    agent_disabled = HermesAgent(skills_index=False, quiet_mode=True)
    prompt_disabled = agent_disabled._build_system_prompt()

    # 프롬프트 바이트 동일성
    assert prompt_enabled.encode("utf-8") == prompt_disabled.encode("utf-8")

    # skills/ 디렉터리가 생성되지 않았음을 확인
    assert not (empty_home / "skills").exists()


def test_scenario_08_empty_skills_dir_byte_identical(tmp_path, monkeypatch):
    """시나리오 8: skills 폴더에 유효한 SKILL.md가 없으면 켜지 않은 것과 바이트 동일."""
    home = tmp_path / "home_with_empty_skills"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    # 빈 하위 폴더
    (skills_dir / "empty_sub").mkdir()
    # 루트 바로 아래 SKILL.md (스킬로 인식되지 않음)
    (skills_dir / "SKILL.md").write_text("---\nname: invalid-root\n---\n", encoding="utf-8")

    agent_enabled = HermesAgent(skills_index=True, quiet_mode=True)
    prompt_enabled = agent_enabled._build_system_prompt()

    agent_disabled = HermesAgent(skills_index=False, quiet_mode=True)
    prompt_disabled = agent_disabled._build_system_prompt()

    assert "<available_skills>" not in prompt_enabled
    assert prompt_enabled.encode("utf-8") == prompt_disabled.encode("utf-8")


def test_scenario_09_dispatcher_without_skills_tools_no_injection(skills_env):
    """시나리오 9: dispatcher에 skills_list 또는 skill_view가 없으면 인덱스 미주입 및 바이트 동일."""
    # ① terminal만 있는 dispatcher
    d1_enabled = create_default_dispatcher(include=["terminal"])
    d1_disabled = create_default_dispatcher(include=["terminal"])
    agent1_enabled = HermesAgent(dispatcher=d1_enabled, skills_index=True, quiet_mode=True)
    agent1_disabled = HermesAgent(dispatcher=d1_disabled, skills_index=False, quiet_mode=True)

    p1_enabled = agent1_enabled._build_system_prompt()
    p1_disabled = agent1_disabled._build_system_prompt()

    assert "<available_skills>" not in p1_enabled
    assert p1_enabled.encode("utf-8") == p1_disabled.encode("utf-8")

    # ② terminal과 skills_list만 있고 skill_view는 없는 dispatcher
    d2_enabled = create_default_dispatcher(include=["terminal", "skills_list"])
    d2_disabled = create_default_dispatcher(include=["terminal", "skills_list"])
    agent2_enabled = HermesAgent(dispatcher=d2_enabled, skills_index=True, quiet_mode=True)
    agent2_disabled = HermesAgent(dispatcher=d2_disabled, skills_index=False, quiet_mode=True)

    p2_enabled = agent2_enabled._build_system_prompt()
    p2_disabled = agent2_disabled._build_system_prompt()

    assert "<available_skills>" not in p2_enabled
    assert p2_enabled.encode("utf-8") == p2_disabled.encode("utf-8")


def test_scenario_10_compression_preserves_skills_index_header_bytes(skills_env):
    """시나리오 10: 컨텍스트 압축 발동 시에도 BLOCK이 포함된 시스템 메시지 헤드 바이트가 100% 동일."""
    mock_llm = MockLLMClient()
    mock_llm.queue_response(content="Turn 1 answer")

    agent = HermesAgent(
        llm_client=mock_llm,
        skills_index=True,
        context_window_tokens=1000,
        compression_threshold_ratio=0.5,
        quiet_mode=True,
    )

    res1 = agent.run_conversation("Hello turn 1")
    m1 = res1["messages"]
    sys_msg1 = m1[0]
    assert sys_msg1["role"] == "system"
    assert BLOCK in sys_msg1["content"]
    orig_bytes = sys_msg1["content"].encode("utf-8")

    # 대화 이력 확장 (임계값 500토큰 초과 유도)
    history = list(m1)
    for i in range(8):
        history.append({"role": "user", "content": f"User question {i}: " + "X" * 250})
        history.append({"role": "assistant", "content": f"Assistant answer {i}: " + "Y" * 250})

    # 2턴 요약 및 응답 큐잉
    mock_llm.queue_response(content="Summary of previous turns")
    mock_llm.queue_response(content="Turn 2 final answer")

    res2 = agent.run_conversation("Trigger turn 2", conversation_history=history)
    m2 = res2["messages"]

    assert m2[0]["role"] == "system"
    assert m2[0]["content"].encode("utf-8") == orig_bytes


def test_scenario_11_subagent_child_isolation(skills_env):
    """시나리오 11: 부모는 skills_index=True이나 위임된 자식 프롬프트에는 BLOCK이 미주입됨."""
    parent_client = MockLLMClient()
    child_clients: list[MockLLMClient] = []

    def child_factory():
        c = MockLLMClient([MockLLMResponse(content="Child subtask summary.")])
        child_clients.append(c)
        return c

    parent = HermesAgent(
        llm_client=parent_client,
        skills_index=True,
        quiet_mode=True,
    )
    register_delegate_tool(
        parent.dispatcher,
        session_db=parent.session_db,
        parent_session_id=parent.session_id,
        llm_client_factory=child_factory,
    )

    # 부모: 1턴 delegate_task 호출 -> 2턴 최종 응답
    parent_client.queue_response(
        tool_calls=[
            {
                "id": "call_delegate_01",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "Investigate release procedure"}),
                },
            }
        ]
    )
    parent_client.queue_response(content="Parent finished after delegation.")

    res = parent.run_conversation("Please run subtask")
    assert res["final_response"] == "Parent finished after delegation."

    # 부모 클라이언트의 첫 호출 메시지에 BLOCK 포함 확인
    parent_first_system = parent_client.call_history[0]["messages"][0]["content"]
    assert BLOCK in parent_first_system

    # 자식 클라이언트의 첫 호출 메시지에 <available_skills> 미포함 확인
    assert len(child_clients) == 1
    child_first_system = child_clients[0].call_history[0]["messages"][0]["content"]
    assert "<available_skills>" not in child_first_system


def test_scenario_12_read_only_skills_filesystem_immutability(skills_env):
    """시나리오 12: 조립 전후 skills 디렉터리 내 파일 해시 및 디렉터리 구조 불변 검증."""
    skills_dir = skills_env["skills_dir"]

    def snapshot_dir(dir_path: Path):
        files = {}
        for p in sorted(dir_path.rglob("*")):
            if p.is_file():
                rel = p.relative_to(dir_path).as_posix()
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                files[rel] = h
        return files

    before_snap = snapshot_dir(skills_dir)

    # 에이전트 생성 및 조립
    agent = HermesAgent(skills_index=True, quiet_mode=True)
    prompt = agent._build_system_prompt()
    assert BLOCK in prompt

    after_snap = snapshot_dir(skills_dir)
    assert before_snap == after_snap
