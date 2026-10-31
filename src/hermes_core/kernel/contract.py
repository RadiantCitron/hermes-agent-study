"""확장 계약 및 턴/세션 스코프 정의 (Extensions Contract).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/model_tools.py (_post_tool_call_hook_suppressed)
- 차이점: 원본은 훅 억제 상태 등을 ContextVar 전역으로 두지만,
  여기서는 TurnScope 문맥을 인자로 명시적 전달하여 상태를 격리하고 단일 진입로를 유지한다.
- 불변 원칙: 이 모듈은 표준 라이브러리만 import하며, hermes_core.agent나 hermes_core.tools를 참조하지 않는다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

RESERVED_RESULT_KEYS: frozenset[str] = frozenset({
    "final_response",
    "messages",
    "iterations",
    "session_id",
    "stopped_reason",
    "error",
})


class MiddlewarePhase(Enum):
    """도구 미들웨어 실행 단계 (값이 작을수록 바깥)."""

    GUARD = 1
    POLICY = 2
    TRANSACTION = 3


class FailurePolicy(Enum):
    """미들웨어 실패 정책."""

    OPEN = "open"
    CLOSED = "closed"


PHASE_FAILURE_POLICY: dict[MiddlewarePhase, FailurePolicy] = {
    MiddlewarePhase.GUARD: FailurePolicy.OPEN,
    MiddlewarePhase.POLICY: FailurePolicy.CLOSED,
    MiddlewarePhase.TRANSACTION: FailurePolicy.CLOSED,
}


@runtime_checkable
class ExtensionHost(Protocol):
    """확장이 코어에 장치를 등록할 때 사용하는 좁은 호스트 인터페이스."""

    def add_tool_middleware(self, phase: MiddlewarePhase, middleware: Callable[..., Any]) -> None:
        ...

    def add_turn_hook(self, point: str, hook: Callable[..., Any]) -> None:
        ...

    def emit(self, kind: str, **data: Any) -> None:
        ...


@runtime_checkable
class Extension(Protocol):
    """코어 확장 단위 프로토콜."""

    name: str

    def install(self, host: ExtensionHost) -> None:
        ...


@dataclass(frozen=True)
class ExtensionRecord:
    """등록된 확장의 읽기 전용 기록."""

    name: str
    kind: str  # "tool_middleware" | "turn_hook"
    slot: str  # 단계 이름("GUARD" 등) 또는 훅 지점("before_llm" 등)
    failures: int


@dataclass
class TurnScope:
    """단일 턴의 확장 상태와 세션 공유 상태, 결과 보고를 관리하는 데이터 클래스."""

    session_id: str
    iteration: int = 0
    stop_reason: Optional[str] = None
    turn_slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: dict[str, Any] = field(default_factory=dict)
    on_extension_error: Optional[Callable[[str, str, Exception], None]] = None

    def report_extension_error(self, name: str, slot: str, exc: Exception) -> None:
        """확장 실행 중 오류를 보고한다."""
        if self.on_extension_error is not None:
            self.on_extension_error(name, slot, exc)

    def request_stop(self, reason: str) -> None:
        """턴 루프 중단을 요청한다. 먼저 요청된 이유가 유지된다."""
        if self.stop_reason is None:
            self.stop_reason = reason

    def state(self, name: str) -> dict[str, Any]:
        """턴 전용 슬롯 반환 (없으면 빈 사전 생성)."""
        if not name:
            raise ValueError("State slot name cannot be empty.")
        if name not in self.turn_slots:
            self.turn_slots[name] = {}
        return self.turn_slots[name]

    def session_state(self, name: str) -> dict[str, Any]:
        """세션 공유 슬롯 반환 (없으면 빈 사전 생성)."""
        if not name:
            raise ValueError("Session slot name cannot be empty.")
        if name not in self.session_slots:
            self.session_slots[name] = {}
        return self.session_slots[name]

    def report(self, key: str, value: Any) -> None:
        """턴 실행 결과에 포함할 항목 보고. 코어 예약 키는 보고할 수 없다."""
        if key in RESERVED_RESULT_KEYS:
            raise ValueError(f"Cannot report reserved result key: '{key}'.")
        self.reports[key] = value
