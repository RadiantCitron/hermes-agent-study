"""시스템 공통 상수 및 격리 환경 설정.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/hermes_constants.py 및 hermes-agent/hermes_cli/config.py
- 연구/격리 차이점:
  1. 원본의 get_hermes_home()은 호스트의 `~/.hermes`를 기본값으로 사용하지만,
     본 학습용 패키지는 호스트 환경 오염을 방지하기 위해 로컬 프로젝트 내부의 `.hermes`를 기본값으로 격리함.
  2. 메모리 및 컨텍스트 파일 제한 상수를 단일 모듈에 집약하여 순환 참조 없이 참조 가능하게 함.
"""

import os
from pathlib import Path

# =============================================================================
# 상태 저장소 및 홈 디렉터리 경로 격리
# =============================================================================

# 프로젝트 루트 디렉터리 (src/hermes_core/constants.py 기준 상위 2단계)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 학습 샌드박스용 기본 로컬 격리 디렉터리 (.hermes/)
DEFAULT_LOCAL_HERMES_HOME = PROJECT_ROOT / ".hermes"


def get_hermes_home() -> Path:
    """Hermes 상태 데이터(메모리, 세션 DB)가 저장되는 홈 디렉터리 반환.

    - 우선순위 1: 환경 변수 `HERMES_HOME`이 명시된 경우 해당 경로 사용.
    - 우선순위 2: 호스트 시스템(~/.hermes) 오염을 방지하기 위해 로컬 격리 경로(.hermes/) 사용.
    """
    env_home = os.getenv("HERMES_HOME")
    if env_home:
        path = Path(env_home).expanduser().resolve()
    else:
        path = DEFAULT_LOCAL_HERMES_HOME

    path.mkdir(parents=True, exist_ok=True)
    return path


# =============================================================================
# 메모리 및 컨텍스트 바운디드(Bounded) 제한 상수
# =============================================================================

# Curated Memory 항목 구분 기호
ENTRY_DELIMITER = "\n§\n"

# MEMORY.md (사실/환경 기억) 최대 글자수 상한
MEMORY_MAX_CHARS = 2200

# USER.md (사용자 성향/선호 기억) 최대 글자수 상한
USER_MAX_CHARS = 1375

# 프로젝트 컨텍스트 파일(AGENTS.md 등) 1개당 최대 주입 글자수 상한
CONTEXT_FILE_MAX_CHARS = 20_000

# 지시문 파일 이름 상수 (시스템 프롬프트 주입 및 쓰기 금지 대상)
AGENTS_FILE_NAMES = ("AGENTS.md", "agents.md")
SOUL_FILE_NAME = "SOUL.md"
CURSORRULES_FILE_NAME = ".cursorrules"
INSTRUCTION_FILE_NAMES = (*AGENTS_FILE_NAMES, SOUL_FILE_NAME, CURSORRULES_FILE_NAME)


# =============================================================================
# 파일 도구 제한 상수
# =============================================================================

# 한 번에 읽는 최대 줄 수 (limit 기본값 및 최댓값)
FILE_READ_MAX_LINES = 2000

# 줄 번호가 붙은 출력 합계 상한 글자수
FILE_READ_MAX_CHARS = 100_000

# 파일 쓰기 내용 상한 글자수
FILE_WRITE_MAX_CHARS = 100_000

# 읽기·수정 대상 파일 크기 상한 (바이트)
FILE_MAX_BYTES = 1_000_000


# =============================================================================
# 외부 LLM 엔드포인트 기본값 (OpenRouter 호환)
# =============================================================================

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"
