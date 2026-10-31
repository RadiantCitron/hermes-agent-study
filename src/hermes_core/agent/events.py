"""에이전트 이벤트 출력 계층 (Agent Events).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py의 tool_progress_callback
  (기준 커밋 6134939882의 _execute_tool_calls에서 호출)
- 차이점: 원본의 콜백 인자들을 단일 불변 객체(AgentEvent)로 묶어 전달한다.
"""

from hermes_core.kernel.events import (
    EVENT_EXTENSION_ERROR,
    EVENT_HOOK_ERROR,
    EVENT_TOOL_CALL,
    AgentEvent,
    EventSink,
)

EVENT_LOOP_GUARD = "loop_guard"
EVENT_EXTENSION_TRIPPED = "extension_tripped"


class ConsoleEventSink:
    """표준 콘솔 출력을 담당하는 기본 이벤트 싱크."""

    def __call__(self, event: AgentEvent) -> None:
        if event.kind == EVENT_TOOL_CALL:
            name = event.data.get("name", "")
            arguments = event.data.get("arguments", "")
            print(f"  [Tool Call] {name}({arguments})")


__all__ = [
    "AgentEvent",
    "ConsoleEventSink",
    "EVENT_EXTENSION_ERROR",
    "EVENT_EXTENSION_TRIPPED",
    "EVENT_HOOK_ERROR",
    "EVENT_LOOP_GUARD",
    "EVENT_TOOL_CALL",
    "EventSink",
]
