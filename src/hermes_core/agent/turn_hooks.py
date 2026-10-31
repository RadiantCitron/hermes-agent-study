"""턴 단계별 훅 레지스트리 및 컨텍스트 (Turn Hooks).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/hermes_cli/plugins.py의 VALID_HOOKS (pre_llm_call, post_tool_call 등)
- 차이점: 확장 지점을 before_llm과 after_tool_calls 2개로 압축하고, 도구 레벨 차단은
  미들웨어(ToolMiddleware)에 맡기며 훅은 턴 라이프사이클 관찰·중단 및 메시지 조정을 담당한다.
"""

from collections.abc import Callable
import copy
from dataclasses import dataclass, field
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

BEFORE_LLM = "before_llm"
AFTER_TOOL_CALLS = "after_tool_calls"
HOOK_POINTS = (BEFORE_LLM, AFTER_TOOL_CALLS)


@dataclass
class TurnContext:
    """단일 턴의 실행 상태와 컨텍스트를 담는 데이터 클래스."""

    messages: list[dict[str, Any]]
    tools_schema: Optional[list[dict[str, Any]]]
    iteration: int
    session_id: str
    run_state: dict[str, Any] = field(default_factory=dict)
    stop_reason: Optional[str] = None

    def request_stop(self, reason: str) -> None:
        """턴 루프 중단을 요청한다. 먼저 요청된 이유가 유지된다."""
        if self.stop_reason is None:
            self.stop_reason = reason


TurnHook = Callable[[TurnContext], None]


class HookRegistry:
    """턴 라이프사이클 훅 등록 및 순차 실행 레지스트리."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[TurnHook]] = {point: [] for point in HOOK_POINTS}

    def register(self, point: str, hook: TurnHook) -> None:
        """특정 지점에 훅 등록. 등록 순서대로 실행된다."""
        if point not in self._hooks:
            raise ValueError(f"Unknown hook point '{point}'. Must be one of {HOOK_POINTS}")
        self._hooks[point].append(hook)

    def run(
        self,
        point: str,
        ctx: TurnContext,
        on_error: Optional[Callable[[str, TurnHook, Exception], None]] = None,
    ) -> None:
        """등록된 훅들을 순차 실행한다. 훅 예외는 on_error로 넘기고 다음 훅을 계속 실행한다."""
        if point not in self._hooks:
            raise ValueError(f"Unknown hook point '{point}'. Must be one of {HOOK_POINTS}")

        for hook in self._hooks[point]:
            # 시스템 메시지 보존 검사를 위해 스냅샷 캡처
            has_system = bool(ctx.messages and ctx.messages[0].get("role") == "system")
            system_role = ctx.messages[0].get("role") if has_system else None
            system_content = ctx.messages[0].get("content") if has_system else None
            system_snapshot = copy.deepcopy(ctx.messages[0]) if has_system else None

            try:
                hook(ctx)
            except BaseException as e:
                if not isinstance(e, Exception):
                    # KeyboardInterrupt, SystemExit 등 BaseException은 전파
                    raise
                if on_error:
                    try:
                        on_error(point, hook, e)
                    except Exception as err_callback_exc:
                        logger.warning("on_error callback failed: %s", err_callback_exc)

            # 시스템 메시지 무결성 사후 검증
            if has_system:
                current_first = ctx.messages[0] if ctx.messages else None
                first_role = current_first.get("role") if current_first else None
                first_content = current_first.get("content") if current_first else None

                if first_role != system_role or first_content != system_content:
                    # 변조되거나 삭제되었으면 원래 스냅샷으로 복원
                    if first_role != "system":
                        ctx.messages.insert(0, copy.deepcopy(system_snapshot))
                    else:
                        ctx.messages[0] = copy.deepcopy(system_snapshot)

                    err = ValueError("system message modified by hook")
                    if on_error:
                        try:
                            on_error(point, hook, err)
                        except Exception as err_callback_exc:
                            logger.warning("on_error callback failed: %s", err_callback_exc)
