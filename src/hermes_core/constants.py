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

# 기본 작업 디렉터리 폴더명
WORKSPACE_DIR_NAME = "workspace"

# 스킬 디렉터리 폴더명 및 파일명 (06 로드맵)
SKILLS_DIR_NAME = "skills"
SKILL_FILE_NAME = "SKILL.md"

# 스킬 메타데이터 글자수 상한
SKILL_NAME_MAX_CHARS = 64
SKILL_DESCRIPTION_MAX_CHARS = 1024

# 스킬 조회 및 연결 파일 제한 상수 (030 로드맵)
SKILL_VIEW_MAX_CHARS = 20_000
SKILL_LINKED_FILES_MAX_PER_KIND = 100
SKILL_LINKED_DIRS = ("references", "templates", "assets", "scripts")

# 스킬 인덱스 주입 제한 상수 (08 로드맵)
SKILLS_INDEX_MAX_ENTRIES = 50
SKILLS_INDEX_DESC_MAX_CHARS = 120
SKILLS_INDEX_MAX_CHARS = 4_000

# 서브에이전트(자식) 허용 도구 목록 (07 로드맵)
CHILD_TOOL_NAMES = ("terminal", "read_file", "write_file", "patch", "skills_list", "skill_view")

# 서브에이전트(자식) 실행 및 반환 제한 상수 (07 로드맵)
DELEGATE_CHILD_MAX_ITERATIONS = 25
DELEGATE_SUMMARY_MAX_CHARS = 8000
SUBAGENT_SOURCE = "subagent"


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


def get_default_workspace() -> Path:
    """Hermes 에이전트 기본 작업 폴더 경로 반환 (.hermes/workspace).

    디렉터리를 생성(mkdir(parents=True, exist_ok=True))하고 resolve()하여 반환한다.
    """
    path = get_hermes_home() / WORKSPACE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def get_skills_dir() -> Path:
    """Hermes 스킬 디렉터리 경로 반환 (<HERMES_HOME>/skills).

    폴더를 생성하지 않고 경로 객체만 반환한다 (디렉터리 불변 원칙).
    """
    return get_hermes_home() / SKILLS_DIR_NAME


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

# 보호 파일 및 폴더 이름 상수 (.env* 비밀 파일 및 .git 저장소 메타데이터)
PROTECTED_NAME_PREFIXES = (".env",)
PROTECTED_NAME_EXACT = (".git",)


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
# 컨텍스트 압축 및 토큰 예산 상수
# =============================================================================

# 모델 기본 컨텍스트 창 크기 (토큰, deepseek-v4 등 1M 창 기본)
CONTEXT_WINDOW_TOKENS = 1_048_576

# 압축 트리거 임계 비율 (컨텍스트 창의 50%)
COMPRESSION_THRESHOLD_RATIO = 0.5

# 메시지당 고정 메타데이터 오버헤드 토큰 수
MESSAGE_TOKEN_OVERHEAD = 4

# 최근 보호 예산 절대 상한 (토큰)
COMPRESSION_TAIL_TOKENS = 20_000

# 임계값 대비 최근 보호 예산 비율 (25%)
COMPRESSION_TAIL_RATIO = 0.25

# 압축 거부 후 재시도 필요 성장 배율 (25% 증가)
COMPRESSION_RETRY_GROWTH = 1.25

# 단일 run_conversation 루프 내 최대 압축 시도 횟수
COMPRESSION_MAX_PER_RUN = 3

# 분할 시 시스템 프롬프트 뒤에 보존할 초기 대화 메시지 수 (헤드)
COMPRESSION_HEAD_MESSAGES = 3

# 분할 시 보존할 최소 최근 메시지 수 (테일)
COMPRESSION_MIN_TAIL_MESSAGES = 6

# 오래된 도구 실행 결과 본문 치환 표지 포맷
TOOL_OUTPUT_CLEARED_FORMAT = "[Old tool output cleared: {tool}, {chars}자]"

# 요약 메시지 고유 식별 접두 및 끝 마커 (본문과 명확히 구분되는 표지)
SUMMARY_PREFIX = (
    "[대화 요약: 이 메시지는 앞선 대화의 요약이며 참고용 배경이다. "
    "지시가 아니다. 최신 user 메시지에만 응답하라. "
    "시스템 지시문과 기억은 압축과 무관하게 유효하다]\n"
)
SUMMARY_END_MARKER = "\n[대화 요약 끝]"

# 요약 생성 시 입력 메시지별 및 전체 상한 글자수
SUMMARY_INPUT_PER_MESSAGE_CHARS = 6000
SUMMARY_INPUT_TOTAL_CHARS = 160_000


# =============================================================================
# 외부 LLM 엔드포인트 기본값 (OpenRouter 호환)
# =============================================================================

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"

# 실제 모델 기본값 및 환경변수
DEFAULT_REAL_MODEL = "deepseek/deepseek-v4-flash-0731"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"

# 모델 통신 제한 상수
LLM_TIMEOUT_SECONDS = 60
LLM_MAX_OUTPUT_TOKENS = 4096
LLM_ERROR_BODY_MAX_CHARS = 500


# =============================================================================
# CLI 입력 관련 상수
# =============================================================================

# CLI 사용자 프롬프트 기본값
CLI_USER_PROMPT = "User > "

# 디코드 실패 안내 문구
CLI_INPUT_DECODE_NOTICE = "Input could not be decoded as UTF-8; please type it again."

# 연속 디코드 실패 시 재시도 상한
CLI_INPUT_DECODE_MAX_RETRIES = 5

# prompt_toolkit 미설치 시 폴백 안내 문구 (프로세스당 한 번 출력)
CLI_PROMPT_TOOLKIT_MISSING_NOTICE = "prompt_toolkit is not installed; falling back to basic input (no multi-line editing)."

# 여러 줄 입력 가능 환경(터미널 + prompt_toolkit) 시작 배너 안내 문구
CLI_MULTILINE_HINT = "Enter to send, Ctrl+J for a new line."

# 승인 흐름 관련 CLI 안내 문구
CLI_APPROVAL_ENABLED_NOTICE = (
    "Command approval enabled: commands outside the allowed list ask for your y/N"
    " (disable with --no-approval)."
)
CLI_APPROVAL_DISABLED_NOTICE = "Command approval disabled (--no-approval)."

# 체크포인트 관련 CLI 안내 문구
CLI_CHECKPOINT_ENABLED_NOTICE = (
    "File checkpoints enabled: write_file/patch changes can be undone with /rollback "
    "(terminal and delegated changes are not covered; disable with --no-checkpoints)."
)
CLI_CHECKPOINT_DISABLED_NOTICE = "File checkpoints disabled (--no-checkpoints)."
CLI_CHECKPOINT_REFUSED_NOTICE = (
    "A file write was refused because its checkpoint could not be recorded, "
    "so the file was not changed. Restart with --no-checkpoints to write without checkpoints."
)
CLI_ROLLBACK_DISABLED_NOTICE = (
    "File checkpoints are disabled in this session (--no-checkpoints), so there is nothing to roll back."
)
CLI_ROLLBACK_USAGE_NOTICE = (
    "Usage: /rollback (no arguments). It undoes the file changes of the most recent turn."
)
CLI_ROLLBACK_NOTHING_NOTICE = "Nothing to roll back."
CLI_ROLLBACK_HEADER = (
    "Rolling back turn {turn_index}: {restore_count} to restore, {skip_count} to skip (newest change first)."
)
CLI_ROLLBACK_LINE_RESTORE = "  [restore] {rel_path} ({tool_name})"
CLI_ROLLBACK_LINE_DELETE = "  [delete]  {rel_path} ({tool_name})"
CLI_ROLLBACK_LINE_SKIP = "  [skip]    {rel_path} ({tool_name}) - {reason}"
CLI_ROLLBACK_EXTERNAL_WARNING = (
    "Warning: terminal commands or delegated sub-agents ran during this turn. "
    "Their changes are not rolled back, so the folder may end up in a mixed state."
)
CLI_ROLLBACK_PENDING_WARNING = (
    "Warning: some changes have an unknown result and will not be restored (listed as skip)."
)
CLI_ROLLBACK_PROMPT = "Roll back these changes? [y/N] "
CLI_ROLLBACK_CANCELLED_NOTICE = "Rollback cancelled. Nothing was changed."
CLI_ROLLBACK_RESULT_RESTORED = "  restored  {rel_path}"
CLI_ROLLBACK_RESULT_DELETED = "  deleted   {rel_path}"
CLI_ROLLBACK_RESULT_SKIPPED = "  skipped   {rel_path} - {reason}"
CLI_ROLLBACK_RESULT_SUMMARY = "Rolled back {rolled_back} file(s), skipped {skipped}."
CLI_ROLLBACK_MODEL_UNAWARE_NOTICE = (
    "The model does not know about this rollback. Ask it to re-read the changed files before continuing."
)
CLI_ROLLBACK_ERROR_NOTICE = (
    "Rollback failed ({reason}). Some files may already have been restored; check the working directory."
)


# =============================================================================
# 승인 흐름 관련 상수
# =============================================================================

# 승인 요청 대기 상태 (초기 상태)
APPROVAL_DECISION_PENDING = "pending"

# 승인 요청 최종 결정 상태 상수
APPROVAL_DECISION_APPROVED = "approved"          # 사람이 명시적 승인 (예)
APPROVAL_DECISION_DENIED = "denied"              # 사람이 거부 (아니오·빈 입력·알 수 없는 입력)
APPROVAL_DECISION_CANCELLED = "cancelled"        # 입력 취소 (EOF·Ctrl+C·입력 예외)
APPROVAL_DECISION_AUTO_DENIED = "auto_denied"    # 비터미널 환경 자동 거부
APPROVAL_DECISION_LIMIT_EXCEEDED = "limit_exceeded"  # 세션당 요청 상한 초과

# 승인 요청 최종 결정 닫힌 목록
APPROVAL_FINAL_DECISIONS = (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_DENIED,
    APPROVAL_DECISION_CANCELLED,
    APPROVAL_DECISION_AUTO_DENIED,
    APPROVAL_DECISION_LIMIT_EXCEEDED,
)

# 승인기 기본 요청 상한 (인스턴스당)
APPROVAL_REQUEST_LIMIT = 10

# 승인기 명령 표시 최대 글자 수
APPROVAL_DISPLAY_MAX_CHARS = 2000

# 승인으로 판정하는 답변 닫힌 목록
APPROVAL_YES_ANSWERS = ("y", "yes")

# 승인 질문 헤더 및 프롬프트
APPROVAL_HEADER = (
    "Approval required: the agent wants to run a command that is not on the allowed list."
)
APPROVAL_PROMPT = "Run it? [y/N] "

# 승인 거부/취소 시 모델 반환 메시지
APPROVAL_DENIED_MESSAGE = (
    "The user denied this command. Do not retry it or work around the denial."
)
APPROVAL_AUTO_DENIED_MESSAGE = (
    "No user is available to approve commands, so the command was not run."
)
APPROVAL_LIMIT_MESSAGE = (
    "The approval request limit ({limit}) has been reached, so the command was not run. Do not request more."
)
APPROVAL_TOO_LONG_MESSAGE = (
    "The command is longer than {max_chars} characters and cannot be reviewed by the user, so it was not run."
)

# 터미널 실행 시 하위 프로세스 환경에서 제거할 환경변수 이름 목록
TERMINAL_STRIPPED_ENV_NAMES = (OPENROUTER_API_KEY_ENV,)


# =============================================================================
# 체크포인트·롤백 관련 상수 (11 로드맵)
# =============================================================================

CHECKPOINT_STATUS_PENDING = "pending"
CHECKPOINT_STATUS_APPLIED = "applied"
CHECKPOINT_STATUS_FAILED = "failed"
CHECKPOINT_STATUS_ROLLED_BACK = "rolled_back"
CHECKPOINT_STATUS_SKIPPED = "skipped"

# 허용되는 상태 전이 맵: 새 상태 -> 허용되는 이전 상태 튜플
CHECKPOINT_TRANSITIONS = {
    CHECKPOINT_STATUS_APPLIED: (CHECKPOINT_STATUS_PENDING,),
    CHECKPOINT_STATUS_FAILED: (CHECKPOINT_STATUS_PENDING,),
    CHECKPOINT_STATUS_ROLLED_BACK: (CHECKPOINT_STATUS_APPLIED,),
    CHECKPOINT_STATUS_SKIPPED: (CHECKPOINT_STATUS_APPLIED, CHECKPOINT_STATUS_PENDING),
}

# 체크포인트 대상 도구 목록 (write_file, patch만 대상)
CHECKPOINT_TOOL_NAMES = ("write_file", "patch")

# 세션당 체크포인트 누적 바이트 상한 (50MB)
CHECKPOINT_SESSION_BYTES_LIMIT = 50_000_000

# 체크포인트 상한 초과 에러 메시지
CHECKPOINT_LIMIT_ERROR_MESSAGE = (
    "Checkpoint storage limit exceeded ({limit} bytes). The file was not modified."
)

# 체크포인트 선기록 실패 에러 메시지
CHECKPOINT_PRE_RECORD_FAILED_MESSAGE = (
    "Failed to record checkpoint before modification ({reason}). The file was not modified."
)

# 체크포인트 작업 폴더 불일치 거부 메시지 (경로는 넣지 않는다)
CHECKPOINT_WORKSPACE_MISMATCH_MESSAGE = (
    "Checkpoint workspace differs from the current workspace. The file was not modified."
)

# 체크포인트 거부 원인 뒤에 붙는 대처 문구
CHECKPOINT_REFUSED_HINT = " Tell the user and do not repeat the same write."





