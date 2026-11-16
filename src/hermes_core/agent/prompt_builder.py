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

from collections.abc import Collection
from datetime import datetime
import os
from pathlib import Path
from typing import Optional
import unicodedata

from hermes_core.constants import (
    AGENTS_FILE_NAMES,
    CONTEXT_FILE_MAX_CHARS,
    CURSORRULES_FILE_NAME,
    SKILLS_INDEX_DESC_MAX_CHARS,
    SKILLS_INDEX_MAX_CHARS,
    SKILLS_INDEX_MAX_ENTRIES,
    SOUL_FILE_NAME,
)

# =============================================================================
# 정적 프롬프트 상수
# =============================================================================

DEFAULT_AGENT_IDENTITY = (
    "You are an AI agent built by RadiantCitron for learning and experimenting with agent architectures, inspired by Hermes Agent. "
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

SEARCH_FILES_GUIDANCE = (
    "To locate files by name or find text inside files in the workspace, use search_files "
    "before reading with read_file or editing with patch (prefer search_files over terminal find/grep)."
)

SKILLS_GUIDANCE = (
    "You have access to reusable skills (procedural memories) stored as read-only reference files. "
    "Before starting a task, use skills_list to check available skill names and descriptions. "
    "If a relevant skill exists, use skill_view to read its full procedure or attached files and follow its instructions."
)

SKILLS_INDEX_HEADING = "## Available Skills"

SKILLS_INDEX_RULE = (
    "Before answering, scan the skills below. If one is relevant to the task, load it with skill_view(name) and "
    "follow its instructions; if none is relevant, proceed without loading any. This index was taken at session "
    "start; call skills_list for the current set."
)

SUBAGENT_GUIDANCE = (
    "## Subagent Delegation (`delegate_task`)\n"
    "- When to delegate: Use `delegate_task` for independent, exploratory, or multi-step subtasks "
    "(e.g. codebase exploration, deep investigation) whose intermediate tool calls would clutter your context.\n"
    "- When NOT to delegate: Do not delegate tasks that require only a single tool call.\n"
    "- Complete instructions: The subagent has no access to your conversation history. Provide all required details "
    "and constraints in `goal` and `context`.\n"
    "- Verify summary: The subagent returns only a summary. Directly verify critical results when needed."
)

CHILD_AGENT_PROMPT = (
    "You are a focused subagent delegated to accomplish a single specific task.\n"
    "You do not have access to the parent conversation history. Use your available tools "
    "(terminal, file operations, skills) to thoroughly investigate, execute, and verify the task.\n"
    "You cannot ask clarifying questions or delegate tasks further. Once your task is complete, "
    "respond with a concise summary covering: (1) what you did, (2) what you discovered, "
    "(3) files modified or inspected, and (4) any remaining issues or caveats."
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
    for name in AGENTS_FILE_NAMES:
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
    soul_candidate = cwd_path / SOUL_FILE_NAME
    if soul_candidate.exists() and soul_candidate.is_file():
        try:
            raw_text = soul_candidate.read_text(encoding="utf-8")
            truncated = _truncate_content(raw_text, SOUL_FILE_NAME)
            sections.append(f"## {SOUL_FILE_NAME}\n\n{truncated}")
        except Exception:
            pass

    # 3. .cursorrules 탐색
    cursor_candidate = cwd_path / CURSORRULES_FILE_NAME
    if cursor_candidate.exists() and cursor_candidate.is_file():
        try:
            raw_text = cursor_candidate.read_text(encoding="utf-8")
            truncated = _truncate_content(raw_text, CURSORRULES_FILE_NAME)
            sections.append(f"## {CURSORRULES_FILE_NAME}\n\n{truncated}")
        except Exception:
            pass

    if not sections:
        return ""

    header = "# Project Context & Standing Instructions\n\nThe following project files define guidelines for this workspace:"
    return header + "\n\n" + "\n\n".join(sections)


# =============================================================================
# 스킬 인덱스 렌더링 순수 함수 (08 로드맵)
# =============================================================================

def _sanitize_index_text(text: str) -> str:
    """인덱스 항목의 텍스트 정제 (태그 위조 방지, 제어문자 및 공백 정규화).

    1. <, > 태그 문자 제거 (프롬프트 태그 구획 위조 차단).
    2. 공백 문자 및 유니코드 C* 범주(제어·서식 등)를 공백으로 치환.
    3. 연속 공백 압축 및 앞뒤 공백 제거.
    """
    t = text.replace("<", "").replace(">", "")
    chars: list[str] = []
    for ch in t:
        if ch.isspace() or unicodedata.category(ch).startswith("C"):
            chars.append(" ")
        else:
            chars.append(ch)
    return " ".join("".join(chars).split())


def render_skills_index(
    skills: list[dict],
    max_entries: int = SKILLS_INDEX_MAX_ENTRIES,
    desc_max_chars: int = SKILLS_INDEX_DESC_MAX_CHARS,
    max_chars: int = SKILLS_INDEX_MAX_CHARS,
) -> str:
    """스킬 목록을 세션 시작 시 시스템 프롬프트에 주입할 평평한 한 줄 블록으로 렌더링.

    [동작 원리]
    1. 순수 함수: 디스크, 환경, 시계, 외부 모듈에 접근하지 않고 입력을 변경하지 않음.
    2. 항목 정제:
       - dict가 아니거나 정제 후 name이 빈 문자열인 항목은 건너뜀 (초과 안내 N 계산에서 제외).
       - <, > 문자 제거, 제어문자(C*) 및 공백 문자를 단일 공백으로 치환, 연속 공백 압축.
       - description이 desc_max_chars를 초과하면 말줄임표(...)로 절삭.
    3. 상한 판정 (내림차순 검색):
       - k = min(total, max_entries)부터 1까지 내려오며 후보 블록 생성.
       - 후보 블록 길이가 max_chars 이하인 첫 번째 블록을 반환.
       - 하나도 들어맞지 않거나 유효 항목이 0개이면 빈 문자열("") 반환.
    """
    if max_entries < 1:
        raise ValueError(f"max_entries must be >= 1, got {max_entries}")
    if desc_max_chars < 4:
        raise ValueError(f"desc_max_chars must be >= 4, got {desc_max_chars}")
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")

    if not skills:
        return ""

    cleaned_items: list[str] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str):
            continue
        name = _sanitize_index_text(raw_name)
        if not name:
            continue

        raw_desc = item.get("description")
        if not isinstance(raw_desc, str):
            raw_desc = ""
        desc = _sanitize_index_text(raw_desc)
        if len(desc) > desc_max_chars:
            desc = desc[: desc_max_chars - 3].rstrip() + "..."

        if desc:
            cleaned_items.append(f"- {name}: {desc}")
        else:
            cleaned_items.append(f"- {name}")

    total = len(cleaned_items)
    if total == 0:
        return ""

    for k in range(min(total, max_entries), 0, -1):
        candidates = cleaned_items[:k]
        more = total - k
        lines = ["<available_skills>"] + candidates
        if more > 0:
            lines.append(f"(+{more} more; use skills_list to see all)")
        lines.append("</available_skills>")
        block = "\n".join(lines)
        if len(block) <= max_chars:
            return block

    return ""


def format_skills_index_section(block: str) -> str:
    """스킬 인덱스 블록에 제목과 규칙 문장을 덧붙여 구획(section)으로 포맷팅.

    Args:
        block: <available_skills> 태그로 감싸진 스킬 목록 문자열

    Returns:
        비어있거나 공백뿐이면 빈 문자열(""), 아니면 제목 + 규칙 + 블록으로 결합된 구획 문자열.
    """
    if not block or not block.strip():
        return ""
    return f"{SKILLS_INDEX_HEADING}\n{SKILLS_INDEX_RULE}\n\n{block.strip()}"


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
    skills_index_block: Optional[str] = None,
) -> str:
    """모든 계층을 순서대로 병합하여 불변 시스템 프롬프트 조립.

    [계층 조립 순서]
    1. Agent Identity (에이전트 기본 정체성)
    2. Tools Guidance (활성화된 도구 가이던스: Memory, Session Search 등)
    3. Skills Index Block (제목 + 규칙 문장 + 스킬 요약 목록, 활성화 시)
    4. Caller Prompt (사용자/시스템 임시 지시문)
    5. Memory Block (Curated Bounded Memory 스냅샷)
    6. Context Files Block (AGENTS.md, SOUL.md 등 프로젝트 컨텍스트)
    7. Conversation Timestamp (대화 시작 시각 고정)
    8. Platform Hint (플랫폼별 렌더링 힌트)
    """
    parts: list[str] = [agent_identity]

    # 도구 안내문
    if tools_guidance:
        valid_guidance = [g.strip() for g in tools_guidance if g and g.strip()]
        if valid_guidance:
            parts.append("\n\n".join(valid_guidance))

    # 스킬 인덱스 블록 (도구 안내문 뒤, 호출자 지시 앞)
    if skills_index_block and skills_index_block.strip():
        parts.append(format_skills_index_section(skills_index_block))

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


def select_tools_guidance(tool_names: Collection[str]) -> list[str]:
    """등록된 도구 이름 목록에 대응하는 안내문만 순서대로 선택하여 반환.

    선택 순서:
    1. MEMORY_GUIDANCE: 'memory' 포함 시
    2. SESSION_SEARCH_GUIDANCE: 'session_search' 포함 시
    3. SEARCH_FILES_GUIDANCE: 'search_files' 포함 시
    4. SKILLS_GUIDANCE: 'skills_list' 및 'skill_view' 모두 포함 시
    5. SUBAGENT_GUIDANCE: 'delegate_task' 포함 시
    """
    names_set = set(tool_names) if tool_names else set()
    guidance: list[str] = []

    if "memory" in names_set:
        guidance.append(MEMORY_GUIDANCE)
    if "session_search" in names_set:
        guidance.append(SESSION_SEARCH_GUIDANCE)
    if "search_files" in names_set:
        guidance.append(SEARCH_FILES_GUIDANCE)
    if "skills_list" in names_set and "skill_view" in names_set:
        guidance.append(SKILLS_GUIDANCE)
    if "delegate_task" in names_set:
        guidance.append(SUBAGENT_GUIDANCE)

    return guidance


def build_child_system_message(context: Optional[str] = None) -> str:
    """자식 서브에이전트용 시스템 메시지 조립."""
    if context and context.strip():
        return f"{CHILD_AGENT_PROMPT}\n\n## Context from the parent agent\n\n{context.strip()}"
    return CHILD_AGENT_PROMPT

