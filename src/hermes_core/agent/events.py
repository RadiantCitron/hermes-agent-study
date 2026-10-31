"""에이전트 이벤트 출력 계층 (Agent Events).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py의 tool_progress_callback
  (기준 커밋 6134939882의 _execute_tool_calls에서 호출)
- 차이점: 원본의 콜백 인자들을 단일 불변 객체(AgentEvent)로 묶어 전달한다.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

EVENT_TOOL_CALL = "tool_call"
EVENT_HOOK_ERROR = "hook_error"


@dataclass(frozen=True)
class AgentEvent:
    """에이전트 실행 중 발생하는 단일 이벤트 데이터 클래스."""

    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


EventSink = Callable[[AgentEvent], None]


class ConsoleEventSink:
    """표준 콘솔 출력을 담당하는 기본 이벤트 싱크."""

    def __call__(self, event: AgentEvent) -> None:
        if event.kind == EVENT_TOOL_CALL:
            name = event.data.get("name", "")
            arguments = event.data.get("arguments", "")
            print(f"  [Tool Call] {name}({arguments})")
