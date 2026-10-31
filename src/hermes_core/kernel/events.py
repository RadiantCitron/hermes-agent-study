"""에이전트 이벤트 코어 타입 및 상수 (Kernel Events).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py의 tool_progress_callback
- 차이점: 에이전트 이벤트(AgentEvent)와 이벤트 수신 인터페이스(EventSink)를 커널 계약으로 격리한다.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

EVENT_TOOL_CALL = "tool_call"
EVENT_HOOK_ERROR = "hook_error"
EVENT_EXTENSION_ERROR = "extension_error"


@dataclass(frozen=True)
class AgentEvent:
    """에이전트 실행 중 발생하는 단일 이벤트 데이터 클래스."""

    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


EventSink = Callable[[AgentEvent], None]
