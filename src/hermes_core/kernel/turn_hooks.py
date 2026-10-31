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

from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.transcript import first_message_unchanged

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
    scope: TurnScope = field(default=None)  # type: ignore[assignment]
    last_input_tokens: Optional[int] = None
    last_message_count: Optional[int] = None
    _live_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _replace_fn: Optional[Callable[..., None]] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.scope is None:
            self.scope = TurnScope(session_id=self.session_id, iteration=self.iteration)
        # run_state와 scope.turn_slots를 동일 객체로 연결
        # 기존 run_state에 초기값이 있었다면 turn_slots에 병합
        if self.run_state and self.run_state is not self.scope.turn_slots:
            self.scope.turn_slots.update(self.run_state)
        self.run_state = self.scope.turn_slots
        if self.stop_reason is not None and self.scope.stop_reason is None:
            self.scope.request_stop(self.stop_reason)

    @property
    def stop_reason(self) -> Optional[str]:  # type: ignore[no-redef]
        return self.scope.stop_reason

    @stop_reason.setter
    def stop_reason(self, value: Optional[str]) -> None:
        if self.scope is not None and value is not None:
            self.scope.request_stop(value)

    def request_stop(self, reason: str) -> None:
        """턴 루프 중단을 요청한다. 먼저 요청된 이유가 유지된다."""
        self.scope.request_stop(reason)

    def replace_transcript(
        self,
        new_messages: list[dict[str, Any]],
        *,
        summary_index: Optional[int] = None,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        """세션 DB 원장 압축과 활성 대화 교체를 원자적으로 수행하는 단일 창구."""
        if self._replace_fn is None:
            raise RuntimeError("replace_transcript is not available in this context.")
        self._replace_fn(new_messages, summary_index=summary_index, usage=usage)


TurnHook = Callable[[TurnContext], None]


class HookRegistry:
    """턴 라이프사이클 훅 등록 및 순차 실행 레지스트리."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[tuple[TurnHook, Optional[str]]]] = {point: [] for point in HOOK_POINTS}

    def register(self, point: str, hook: TurnHook, name: Optional[str] = None) -> None:
        """특정 지점에 훅 등록. 등록 순서대로 실행된다."""
        if point not in self._hooks:
            raise ValueError(f"Unknown hook point '{point}'. Must be one of {HOOK_POINTS}")
        self._hooks[point].append((hook, name))

    def run(
        self,
        point: str,
        ctx: TurnContext,
        on_error: Optional[Callable[[str, TurnHook, Exception], None]] = None,
    ) -> None:
        """등록된 훅들을 순차 실행한다. 훅 예외는 on_error로 넘기고 다음 훅을 계속 실행한다."""
        if point not in self._hooks:
            raise ValueError(f"Unknown hook point '{point}'. Must be one of {HOOK_POINTS}")

        live_source = ctx._live_messages if ctx._live_messages is not None else ctx.messages

        for hook, name in self._hooks[point]:
            # 훅 호출 직전 대화 복사본 생성 및 비교용 복사본 보관
            ctx.messages = copy.deepcopy(live_source)
            comparison = copy.deepcopy(live_source)

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

            # 훅 실행 후 대화 변조 사후 검증
            # replace_transcript가 호출된 경우 comparison도 live_source로 맞춰졌을 수 있으므로 대조
            if ctx.messages != comparison:
                has_comp_sys = bool(comparison and comparison[0].get("role") == "system")
                if has_comp_sys and not first_message_unchanged(comparison, ctx.messages):
                    err = ValueError("system message modified by hook")
                else:
                    err = ValueError("transcript modified outside replace_transcript")

                if on_error:
                    try:
                        on_error(point, hook, err)
                    except Exception as err_callback_exc:
                        logger.warning("on_error callback failed: %s", err_callback_exc)

        # 훅 실행 종료 후 ctx.messages를 실제 대화와 동기화
        ctx.messages = live_source
