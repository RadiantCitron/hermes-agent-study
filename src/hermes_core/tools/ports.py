"""도구 계층(tools/*)이 요구하는 외부 저장소 및 자식 실행기 포트(Protocol) 정의.

도구 계층 모듈은 구체 SQLite 구현체(SessionDB)나 상위 에이전트 구현체(HermesAgent)를
직접 import하지 않고 본 모듈의 프로토콜에 의존한다 (ADR-0020 D-02, D-04).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class ApprovalStorePort(Protocol):
    """Approver가 사용하는 승인 요청 선기록 및 결정 갱신 저장소 포트."""

    def record_approval_request(
        self,
        session_id: str,
        argv: list[str],
        tool_name: str = "terminal",
    ) -> int: ...

    def resolve_approval_request(
        self,
        request_id: int,
        decision: str,
        reason: Optional[str] = None,
    ) -> bool: ...


@runtime_checkable
class CheckpointStorePort(Protocol):
    """Checkpointer가 사용하는 파일 체크포인트·승인 이력·자식 세션 수 조회 포트."""

    def record_file_checkpoint(
        self,
        session_id: str,
        turn_index: int,
        tool_name: str,
        workspace: str,
        rel_path: str,
        before_exists: bool,
        before_content: Optional[str] = None,
    ) -> int: ...

    def update_file_checkpoint(
        self,
        checkpoint_id: int,
        status: str,
        *,
        after_sha256: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool: ...

    def get_file_checkpoints(
        self,
        session_id: str,
        turn_index: Optional[int] = None,
    ) -> list[dict[str, Any]]: ...

    def get_approval_requests(self, session_id: str) -> list[dict[str, Any]]: ...

    def count_child_sessions(
        self,
        parent_session_id: str,
        *,
        started_at_min: Optional[float] = None,
        started_at_max: Optional[float] = None,
    ) -> int: ...


@runtime_checkable
class DelegateStorePort(Protocol):
    """register_delegate_tool이 자식 세션 마감 및 토큰 조회에 사용하는 저장소 포트."""

    def end_session(self, session_id: str, end_reason: str = "completed") -> None: ...

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]: ...


@runtime_checkable
class SessionSearchPort(Protocol):
    """session_search_tool 및 create_default_dispatcher가 사용하는 과거 대화 검색 포트."""

    def search_messages(
        self,
        query: str,
        source_filter: Optional[list[str]] = None,
        role_filter: Optional[list[str]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class ChildRunnerPort(Protocol):
    """delegate_task가 자식 에이전트 세션을 실행할 때 호출하는 콜백 포트 (020에서 연결)."""

    def __call__(
        self,
        goal: str,
        context: Optional[str] = None,
        *,
        session_db: Any,
        parent_session_id: str,
        llm_client: Any,
        max_iterations: int,
        dispatcher: Optional[Any] = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ApprovalOutcome:
    """승인 요청 결과 객체 (ADR-0021 D-04).

    Attributes:
        approved: 명령 실행이 승인되었는지 여부.
        decision: 원장에 남은 최종 결정 상태 (APPROVAL_FINAL_DECISIONS 중 하나).
        message: 거부·취소·초과 시 에러 메시지 (승인 시 빈 문자열).
    """

    approved: bool
    decision: str
    message: str = ""


@runtime_checkable
class ApproverPort(Protocol):
    """터미널 도구가 사용자나 외부 정책 엔진에 명령 승인을 요청하는 포트 (ADR-0021 D-04)."""

    def request(self, argv: list[str]) -> ApprovalOutcome: ...


@runtime_checkable
class CommandExecutorPort(Protocol):
    """작업 디렉터리 내에서 서브프로세스 또는 샌드박스 명령을 실행하는 포트 (ADR-0021 D-04).

    서브프로세스 격리(Docker 컨테이너, Firecracker 마이크로VM, 원격 gRPC 실행기 등)
    구현체가 이 프로토콜을 구현하여 terminal_tool에 주입된다.
    """

    def __call__(self, args: list[str], cwd: Path, timeout: int) -> str: ...


@runtime_checkable
class ControlAuditPort(Protocol):
    """제어 명령 검증·반영 및 차단기 동작 감사 기록 저장소 포트 (ADR-0021 D-04)."""

    def record_control_audit(
        self,
        session_id: str,
        *,
        source: str,
        command: str,
        target: str,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        result: str,
        reason: Optional[str] = None,
    ) -> int: ...

    def list_control_audit(self, session_id: str) -> list[dict[str, Any]]: ...
