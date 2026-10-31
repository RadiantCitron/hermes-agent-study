"""외부 시스템과 상호작용하기 위한 커널 포트(Protocol) 및 커널 오류 정의."""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from hermes_core.kernel.tool_call import ToolCall


class LLMError(Exception):
    """LLM API 호출 실패 예외.

    속성:
    - kind: rate_limited | timeout | network | http_error | bad_response
    - message: 오류 설명 (API 키 마스킹 적용)
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)

    def __repr__(self) -> str:
        return f"LLMError(kind={self.kind!r}, message={self.message!r})"

    def __str__(self) -> str:
        return self.message


class LLMReply(Protocol):
    """LLM 응답 포트 프로토콜."""

    content: Optional[str]
    tool_calls: Optional[list[dict[str, Any]]]


@runtime_checkable
class LLMPort(Protocol):
    """LLM 클라이언트 포트."""

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMReply:
        """메시지 목록과 도구 스키마를 받아 LLM 응답을 반환한다."""
        ...


@runtime_checkable
class TranscriptStore(Protocol):
    """대화 기록 및 원장 저장소 포트."""

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        """단일 메시지를 추가하고 메시지 번호(sequence)를 반환한다."""
        ...

    def archive_and_compact(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        summary_index: Optional[int] = None,
    ) -> None:
        """대화를 요약본으로 교체하고 이전 대화를 아카이브한다."""
        ...

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """세션의 토큰 사용량을 누적 갱신한다."""
        ...


@runtime_checkable
class ToolExecutor(Protocol):
    """도구 실행 및 스키마 제공 포트."""

    def get_tools_schema(self) -> list[dict[str, Any]]:
        """사용 가능한 도구 스키마 목록을 반환한다."""
        ...

    def dispatch(self, call: ToolCall) -> str:
        """도구 호출을 실행하고 문자열 결과를 반환한다."""
        ...
