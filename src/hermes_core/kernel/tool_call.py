"""도구 호출 요청 및 미들웨어 타입 (Tool Call & Middleware).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/model_tools.py (handle_function_call)
- 차이점: 도구 호출 객체(ToolCall)와 도구 미들웨어(ToolMiddleware) 프로토콜을 커널 계약으로 격리한다.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from hermes_core.kernel.contract import TurnScope


@dataclass(frozen=True)
class ToolCall:
    """도구 호출 요청 데이터 클래스."""

    name: str
    args: dict[str, Any]
    task_id: Optional[str] = None
    scope: Optional[TurnScope] = None
    raw_arguments: Optional[str] = None
    parse_error: Optional[str] = None


ToolMiddleware = Callable[[ToolCall, Callable[[ToolCall], str]], str]
