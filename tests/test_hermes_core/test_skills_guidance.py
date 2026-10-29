"""06 로드맵 040 단계: 스킬 안내문 및 에이전트 연동 단위/인수 테스트.

[인수 테스트 시나리오]
- 1: SKILLS_GUIDANCE 상수 검증 (skills_list, skill_view 포함, 600자 이하)
- 2: build_system_prompt 결합 순서 (MEMORY -> SESSION_SEARCH -> SKILLS)
- 3: 에이전트 _build_system_prompt() 및 session_db 저장값 일치
- 4: 세션 프롬프트 캐싱 불변성 (스킬 추가 후에도 프롬프트 불변)
- 5: 스킬 이름 프롬프트 비포함 검증 (목록 주입 없음)
- 6: 에이전트 도구 스키마 8종 전수 등록 확인
- 7: 컨텍스트 압축 발동 시 시스템 프롬프트 바이트 동일 보존 (헤드 보존)
"""

from pathlib import Path
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient
from hermes_core.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    MEMORY_GUIDANCE,
    SESSION_SEARCH_GUIDANCE,
    SKILLS_GUIDANCE,
    build_system_prompt,
)
from hermes_core.session_db import SessionDB


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch):
    """모든 시험에서 레포의 AGENTS.md 및 호스트의 .hermes 격리."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.chdir(work_dir)

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


def test_scenario_01_skills_guidance_constant():
    assert isinstance(SKILLS_GUIDANCE, str)
    assert len(SKILLS_GUIDANCE.strip()) > 0
    assert "skills_list" in SKILLS_GUIDANCE
    assert "skill_view" in SKILLS_GUIDANCE
    assert len(SKILLS_GUIDANCE) <= 600


def test_scenario_02_build_system_prompt_ordering():
    prompt = build_system_prompt(
        agent_identity=DEFAULT_AGENT_IDENTITY,
        tools_guidance=[MEMORY_GUIDANCE, SESSION_SEARCH_GUIDANCE, SKILLS_GUIDANCE],
    )
    idx_mem = prompt.find(MEMORY_GUIDANCE)
    idx_search = prompt.find(SESSION_SEARCH_GUIDANCE)
    idx_skills = prompt.find(SKILLS_GUIDANCE)

    assert 0 <= idx_mem < idx_search < idx_skills


def test_scenario_03_agent_system_prompt_matches_db(tmp_path: Path):
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)

    agent = HermesAgent(session_db=session_db, quiet_mode=True)
    prompt = agent._build_system_prompt()

    assert SKILLS_GUIDANCE in prompt

    session = session_db.get_session(agent.session_id)
    assert session is not None
    assert session["system_prompt"] == prompt


def test_scenario_04_session_prompt_caching_immutable(tmp_path: Path):
    agent = HermesAgent(quiet_mode=True)
    prompt1 = agent._build_system_prompt()

    # 스킬 폴더에 새 스킬 파일 추가
    skills_dir = Path(agent.session_db.db_path).parent / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    new_skill = skills_dir / "new_skill"
    new_skill.mkdir(parents=True, exist_ok=True)
    (new_skill / "SKILL.md").write_text("---\nname: new_skill\n---\nBody")

    prompt2 = agent._build_system_prompt()
    assert prompt1 == prompt2


def test_scenario_05_skills_list_not_injected_into_prompt(tmp_path: Path):
    db1 = SessionDB(tmp_path / "db1.db")
    agent_no_skills = HermesAgent(session_db=db1, quiet_mode=True)
    prompt_no_skills = agent_no_skills._build_system_prompt()

    # 고유 이름을 가진 스킬 생성
    skills_dir = tmp_path / "hermes_home" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    s = skills_dir / "zz-unique-skill-name"
    s.mkdir()
    (s / "SKILL.md").write_text("# Unique Skill")

    db2 = SessionDB(tmp_path / "db2.db")
    agent_with_skills = HermesAgent(session_db=db2, quiet_mode=True)
    prompt_with_skills = agent_with_skills._build_system_prompt()

    assert prompt_no_skills == prompt_with_skills
    assert "zz-unique-skill-name" not in prompt_with_skills


def test_scenario_06_agent_tools_schema_eight_tools():
    agent = HermesAgent(quiet_mode=True)
    schemas = agent.dispatcher.get_tools_schema()
    names = {s["function"]["name"] for s in schemas}

    expected_eight = {
        "memory",
        "session_search",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "skills_list",
        "skill_view",
    }
    assert expected_eight.issubset(names)
    assert len(names) == 8


def test_scenario_07_compression_preserves_system_prompt_bytes(tmp_path: Path):
    mock_llm = MockLLMClient()
    # 1턴: 인사
    mock_llm.queue_response(content="Turn 1 answer")

    agent = HermesAgent(
        llm_client=mock_llm,
        context_window_tokens=1000,  # 낮은 임계값 (50% = 500 토큰)
        compression_threshold_ratio=0.5,
        quiet_mode=True,
    )

    res1 = agent.run_conversation("Hello turn 1")
    m1 = res1["messages"]
    sys_msg = m1[0]
    assert sys_msg["role"] == "system"
    assert SKILLS_GUIDANCE in sys_msg["content"]
    orig_bytes = sys_msg["content"].encode("utf-8")

    # 대화 이력 확장 (임계값 500토큰을 훌쩍 넘김)
    history = list(m1)
    for i in range(8):
        history.append({"role": "user", "content": f"User question {i}: " + "X" * 250})
        history.append({"role": "assistant", "content": f"Assistant answer {i}: " + "Y" * 250})

    # 2턴: 요약 생성 및 최종 응답
    mock_llm.queue_response(content="Summary of previous turns")
    mock_llm.queue_response(content="Turn 2 final answer")

    res2 = agent.run_conversation("Trigger turn 2", conversation_history=history)
    m2 = res2["messages"]

    assert m2[0]["role"] == "system"
    assert SKILLS_GUIDANCE in m2[0]["content"]
    assert m2[0]["content"].encode("utf-8") == orig_bytes
