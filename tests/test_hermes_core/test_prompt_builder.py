"""시스템 프롬프트 조립기 및 컨텍스트 로더 단위 테스트."""

from datetime import datetime

from hermes_core.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    MEMORY_GUIDANCE,
    PLATFORM_HINTS,
    SESSION_SEARCH_GUIDANCE,
    _truncate_content,
    build_context_files_prompt,
    build_system_prompt,
)


def test_truncate_content_within_limit():
    """제한 글자수 이내인 경우 내용이 전혀 잘리지 않는지 검증."""
    short_content = "This is a short guideline file."
    result = _truncate_content(short_content, "test.md", max_chars=100)
    assert result == short_content


def test_truncate_content_exceeding_limit():
    """25,000자 텍스트 주입 시 70%(14,000자) Head + 마커 + 20%(4,000자) Tail로 정밀 축약되는지 검증."""
    limit = 20_000
    # Head와 Tail을 구분할 수 있는 긴 문자열 생성
    content = "H" * 15_000 + "M" * 5_000 + "T" * 5_000  # 총 25,000자

    truncated = _truncate_content(content, "AGENTS.md", max_chars=limit)

    head_chars = int(limit * 0.7)  # 14,000
    tail_chars = int(limit * 0.2)  # 4,000

    assert truncated.startswith("H" * head_chars)
    assert truncated.endswith("T" * tail_chars)
    assert "[...truncated AGENTS.md: kept 14000+4000 of 25000 chars." in truncated


def test_build_context_files_prompt(tmp_path):
    """임시 작업 디렉터리에 생성된 AGENTS.md 및 SOUL.md가 정상 탐색 및 조립되는지 검증."""
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("# Repo Rules\n- Rule 1\n- Rule 2", encoding="utf-8")

    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("# Persona\nBe direct and helpful.", encoding="utf-8")

    context_prompt = build_context_files_prompt(cwd=str(tmp_path))

    assert "# Project Context & Standing Instructions" in context_prompt
    assert "## AGENTS.md" in context_prompt
    assert "- Rule 1" in context_prompt
    assert "## SOUL.md" in context_prompt
    assert "Be direct and helpful." in context_prompt


def test_build_context_files_prompt_empty(tmp_path):
    """컨텍스트 파일이 전혀 없는 디렉터리에서는 빈 문자열을 반환하는지 검증."""
    assert build_context_files_prompt(cwd=str(tmp_path)) == ""


def test_build_system_prompt_all_layers():
    """7개 계층이 정확한 순서로 시스템 프롬프트에 결합되는지 검증."""
    fixed_time = datetime(2026, 10, 28, 14, 30)
    memory_mock = "<memory>\n- User prefers Python\n</memory>"
    context_mock = "## AGENTS.md\n- Strict TDD"
    caller_mock = "Current task: Build Hermes Core"

    full_prompt = build_system_prompt(
        agent_identity=DEFAULT_AGENT_IDENTITY,
        tools_guidance=[MEMORY_GUIDANCE, SESSION_SEARCH_GUIDANCE],
        caller_prompt=caller_mock,
        memory_block=memory_mock,
        context_files_block=context_mock,
        platform="cli",
        start_time=fixed_time,
    )

    # 계층 순서 검증
    idx_identity = full_prompt.find(DEFAULT_AGENT_IDENTITY)
    idx_guidance = full_prompt.find(MEMORY_GUIDANCE)
    idx_caller = full_prompt.find(caller_mock)
    idx_memory = full_prompt.find(memory_mock)
    idx_context = full_prompt.find(context_mock)
    idx_timestamp = full_prompt.find("Conversation started: Wednesday, October 28, 2026 02:30 PM")
    idx_platform = full_prompt.find(PLATFORM_HINTS["cli"])

    assert 0 <= idx_identity < idx_guidance < idx_caller < idx_memory < idx_context < idx_timestamp < idx_platform
