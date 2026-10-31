"""도구 계층(tools/*)이 요구하는 외부 저장소 및 자식 실행기 포트(Protocol) 정의.

도구 계층 모듈은 구체 SQLite 구현체(SessionDB)나 상위 에이전트 구현체(HermesAgent)를
직접 import하지 않고 본 모듈의 프로토콜에 의존한다 (ADR-0020 D-02, D-04).
"""

from __future__ import annotations

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
