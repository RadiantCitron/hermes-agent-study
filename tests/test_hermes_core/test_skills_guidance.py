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

