"""시스템 프롬프트 조립기 (System Prompt Assembly & Caching Support).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/agent/prompt_builder.py 및 hermes-agent/run_agent.py (AIAgent._build_system_prompt)
- 핵심 원리:
  1. 계층형 시스템 프롬프트 조립 (Identity -> Guidance -> Ephemeral -> Memory -> Context Files -> Platform Hint).
  2. 세션별 프롬프트 캐싱 보존(Prompt Caching is Sacred):
     턴마다 시스템 프롬프트를 재생성하지 않고 세션 시작 시 1회 조립하여 캐싱함으로써,
     LLM의 Prefix Cache 히트율을 극대화하고 비용과 레이턴시를 절감함.
  3. 컨텍스트 파일 헤드/테일(70/20) 안전 절단 가드(_truncate_content).
"""

from datetime import datetime
import os
from pathlib import Path
from typing import Optional

from hermes_core.constants import CONTEXT_FILE_MAX_CHARS

# =============================================================================
# 정적 프롬프트 상수
# =============================================================================

DEFAULT_AGENT_IDENTITY = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below."
)

MEMORY_GUIDANCE = (
    "You have persistent memory carried across sessions. Use the memory tool "
    "to remember durable facts about the user and environment. Memory has a hard "
    "character budget: keep entries concise and replace or consolidate stale entries."
)

SESSION_SEARCH_GUIDANCE = (
    "When the user references something from a past conversation or you suspect relevant "
    "cross-session context exists, use session_search to recall it before asking them to repeat themselves."
)

PLATFORM_HINTS = {
    "cli": "You are a CLI AI Agent. Try not to use markdown but simple text renderable inside a terminal.",
    "discord": "You are in a Discord server communicating with your user.",
    "telegram": "You are on Telegram. Do not use markdown that does not render.",
    "whatsapp": "You are on WhatsApp. Do not use markdown that does not render.",
}

CONTEXT_TRUNCATE_HEAD_RATIO = 0.70
CONTEXT_TRUNCATE_TAIL_RATIO = 0.20


# =============================================================================
# 컨텍스트 파일 절단 및 탐색 함수
# =============================================================================

def _truncate_content(
    content: str, filename: str, max_chars: int = CONTEXT_FILE_MAX_CHARS
) -> str:
    """컨텍스트 파일이 max_chars를 초과할 경우 Head 70%, Tail 20%를 남기고 중간을 절단.

    [작동 원리]
    - 파일의 앞부분(핵심 설정/규약)과 끝부분(최신 추가 사항)을 보존.
    - 잘려나간 사실을 모델이 인지할 수 있도록 명시적 안내 마커 삽입.
    """
    if len(content) <= max_chars:
        return content

    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    head = content[:head_chars]
    tail = content[-tail_chars:]

    marker = (
        f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of {len(content)} chars. "
        "Use file tools to read the full file.]\n\n"
    )
    return head + marker + tail


def build_context_files_prompt(cwd: Optional[str] = None) -> str:
    """작업 디렉터리의 프로젝트 컨텍스트 파일(AGENTS.md, SOUL.md, .cursorrules)을 탐색하여 조립.

    Args:
        cwd: 검색 기준 디렉터리 (None일 경우 현재 작업 디렉터리 사용)

    Returns:
        조립된 컨텍스트 마크다운 문자열 (없을 경우 빈 문자열)
    """
    if cwd is None:
        cwd = os.getcwd()

    cwd_path = Path(cwd).resolve()
    sections: list[str] = []

    # 1. AGENTS.md 탐색
    for name in ["AGENTS.md", "agents.md"]:
        candidate = cwd_path / name
        if candidate.exists() and candidate.is_file():
            try:
                raw_text = candidate.read_text(encoding="utf-8")
                truncated = _truncate_content(raw_text, name)
                sections.append(f"## {name}\n\n{truncated}")
            except Exception:
                pass
            break

    # 2. SOUL.md 탐색 (작업 디렉터리 우선)
    soul_candidate = cwd_path / "SOUL.md"
    if soul_candidate.exists() and soul_candidate.is_file():
        try:
            raw_text = soul_candidate.read_text(encoding="utf-8")
            truncated = _truncate_content(raw_text, "SOUL.md")
            sections.append(f"## SOUL.md\n\n{truncated}")
        except Exception:
            pass

    # 3. .cursorrules 탐색
    cursor_candidate = cwd_path / ".cursorrules"
    if cursor_candidate.exists() and cursor_candidate.is_file():
        try:
            raw_text = cursor_candidate.read_text(encoding="utf-8")
            truncated = _truncate_content(raw_text, ".cursorrules")
            sections.append(f"## .cursorrules\n\n{truncated}")
        except Exception:
            pass

    if not sections:
        return ""

    header = "# Project Context & Standing Instructions\n\nThe following project files define guidelines for this workspace:"
    return header + "\n\n" + "\n\n".join(sections)


# =============================================================================
# 계층형 시스템 프롬프트 조립 메인 함수
# =============================================================================

def build_system_prompt(
    agent_identity: str = DEFAULT_AGENT_IDENTITY,
    tools_guidance: Optional[list[str]] = None,
    caller_prompt: Optional[str] = None,
    memory_block: Optional[str] = None,
    context_files_block: Optional[str] = None,
    platform: str = "cli",
    start_time: Optional[datetime] = None,
) -> str:
    """모든 계층을 순서대로 병합하여 불변 시스템 프롬프트 조립.

    [계층 조립 순서]
    1. Agent Identity (에이전트 기본 정체성)
    2. Tools Guidance (활성화된 도구 가이던스: Memory, Session Search 등)
    3. Caller Prompt (사용자/시스템 임시 지시문)
    4. Memory Block (Curated Bounded Memory 스냅샷)
    5. Context Files Block (AGENTS.md, SOUL.md 등 프로젝트 컨텍스트)
    6. Conversation Timestamp (대화 시작 시각 고정)
    7. Platform Hint (플랫폼별 렌더링 힌트)
    """
    parts: list[str] = [agent_identity]

    # 도구 안내문
    if tools_guidance:
        valid_guidance = [g.strip() for g in tools_guidance if g and g.strip()]
        if valid_guidance:
            parts.append("\n\n".join(valid_guidance))

    # 호출자 지정 추가 프롬프트
    if caller_prompt and caller_prompt.strip():
        parts.append(caller_prompt.strip())

    # 바운디드 메모리 스냅샷 (MEMORY.md / USER.md)
    if memory_block and memory_block.strip():
        parts.append(memory_block.strip())

    # 프로젝트 컨텍스트 파일
    if context_files_block and context_files_block.strip():
        parts.append(context_files_block.strip())

    # 대화 시작 일시 (프롬프트 캐싱을 위해 세션 시작 시점 고정)
    now = start_time or datetime.now()
    timestamp_str = f"Conversation started: {now.strftime('%A, %B %d, %Y %I:%M %p')}"
    parts.append(timestamp_str)

    # 플랫폼 포맷팅 힌트
    platform_key = (platform or "").lower().strip()
    if platform_key in PLATFORM_HINTS:
        parts.append(PLATFORM_HINTS[platform_key])

    return "\n\n".join(parts)
