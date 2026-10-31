"""커널 턴 루프 구현 (Kernel Turn Loop).

외부 부품(LLM, Tools, Store, Hooks 등)을 포트로 주입받아 단일 대화 턴의 실행 흐름을 관장한다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
from typing import Any, Optional
import uuid

from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.events import EVENT_TOOL_CALL, EventSink, emit_event
from hermes_core.kernel.llm_pipeline import (
    LLMMiddleware,
    LLMRequest,
    build_llm_chain,
)
from hermes_core.kernel.ports import LLMError, LLMPort, ToolExecutor, TranscriptStore
from hermes_core.kernel.tool_call import ToolCall
from hermes_core.kernel.transcript import (
    assistant_message,
    first_message_unchanged,
    parse_tool_arguments,
    tool_result_message,
)
from hermes_core.kernel.turn_hooks import (
    AFTER_TOOL_CALLS,
    BEFORE_LLM,
    HookRegistry,
    TurnContext,
)


@dataclass
class TokenEstimate:
    """압축이 쓰는 직전 입력 토큰 수와 그때의 메시지 수. 세션 동안 파사드가 소유하고 커널이 제자리에서 고친다."""

    input_tokens: Optional[int] = None
    message_count: Optional[int] = None


def _replace_transcript(
    messages: list[dict[str, Any]],
    new_messages: list[dict[str, Any]],
    *,
    store: TranscriptStore,
    scope: TurnScope,
    estimate: TokenEstimate,
    summary_index: Optional[int] = None,
    usage: Optional[dict[str, Any]] = None,
) -> None:
    """대화 교체 및 세션 원장 압축."""
    if not new_messages:
        raise ValueError("new_messages cannot be empty.")
    if not messages:
        raise ValueError("active transcript cannot be empty.")

    if not first_message_unchanged(messages, new_messages):
        raise ValueError("system message modified by replace_transcript")

    # 세션 원장 압축 (실패 시 예외 전파되어 메모리 메시지는 불변 유지)
    store.archive_and_compact(
        scope.session_id,
        new_messages[1:],
        summary_index=summary_index,
    )

    # 원장 압축 성공 시에만 메모리 목록 원자적 교체 및 추정치 초기화
    messages[:] = new_messages
    estimate.input_tokens = None
    estimate.message_count = None

    if usage and isinstance(usage, dict):
        in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        if in_tok or out_tok:
            store.update_token_counts(
                scope.session_id, input_tokens=in_tok, output_tokens=out_tok
            )


def _execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    store: TranscriptStore,
    tools: ToolExecutor,
    scope: TurnScope,
    event_sink: Optional[EventSink],
    logger: logging.Logger,
) -> None:
    """assistant_message의 tool_calls를 실행하고 결과를 messages 및 원장에 기록."""
    for tc in tool_calls:
        call_id = tc.get("id") or str(uuid.uuid4())
        func_obj = tc.get("function", {})
        func_name = func_obj.get("name") or tc.get("name", "unknown")
        args_str = func_obj.get("arguments", "{}")

        emit_event(
            event_sink,
            EVENT_TOOL_CALL,
            {"name": func_name, "arguments": args_str},
            logger,
        )

        args, parse_error = parse_tool_arguments(args_str)
        if parse_error is not None:
            logger.warning("Invalid JSON arguments for tool '%s': %s", func_name, parse_error)

        call = ToolCall(
            name=func_name,
            args=args or {},
            task_id=call_id,
            scope=scope,
            raw_arguments=args_str if isinstance(args_str, str) else None,
            parse_error=parse_error,
        )
        result_str = tools.dispatch(call)

        tool_msg = tool_result_message(call_id, result_str)
        messages.append(tool_msg)

        store.append_message(
            session_id=scope.session_id,
            role="tool",
            content=result_str,
            tool_call_id=call_id,
            tool_name=func_name,
        )


def run_turn(
    *,
    messages: list[dict[str, Any]],
    user_message: str,
    llm: LLMPort,
    tools: ToolExecutor,
    store: TranscriptStore,
    hooks: HookRegistry,
    scope: TurnScope,
    estimate: TokenEstimate,
    max_iterations: int,
    llm_middlewares: Sequence[LLMMiddleware] = (),
    event_sink: Optional[EventSink],
    on_hook_error: Callable[[str, Any, Exception], None],
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    """단일 대화 턴 루프 실행. 최종 응답 문자열과 전체 messages 목록을 담은 사전을 반환한다."""
    active_logger = logger if logger is not None else logging.getLogger(__name__)

    # 사용자 메시지 추가 및 원장 기록
    messages.append({"role": "user", "content": user_message})
    store.append_message(
        session_id=scope.session_id,
        role="user",
        content=user_message,
    )

    final_response: str = ""
    stopped_reason = "max_iterations"
    iterations = 0
    error_info: Optional[dict[str, str]] = None

    ctx = TurnContext(
        messages=messages,
        tools_schema=None,
        iteration=0,
        session_id=scope.session_id,
        scope=scope,
        last_input_tokens=estimate.input_tokens,
        last_message_count=estimate.message_count,
        _live_messages=messages,
        _replace_fn=lambda new_msgs, summary_index=None, usage=None: _replace_transcript(
            messages,
            new_msgs,
            store=store,
            scope=scope,
            estimate=estimate,
            summary_index=summary_index,
            usage=usage,
        ),
    )

    call_llm = build_llm_chain(llm, llm_middlewares)

    while iterations < max_iterations:
        iterations += 1

        tools_schema = tools.get_tools_schema()
        schema_list = tools_schema if tools_schema else None

        ctx.iteration = iterations
        scope.iteration = iterations
        ctx.tools_schema = schema_list
        ctx.last_input_tokens = estimate.input_tokens
        ctx.last_message_count = estimate.message_count

        hooks.run(BEFORE_LLM, ctx, on_error=on_hook_error)
        if ctx.stop_reason is not None:
            stopped_reason = ctx.stop_reason
            final_response = ""
            break

        msgs_len_before_call = len(messages)

        try:
            resp = call_llm(
                LLMRequest(
                    messages=messages,
                    tools=schema_list,
                    scope=scope,
                )
            )
        except LLMError as e:
            active_logger.warning("LLM call failed with error: %s", e)
            stopped_reason = "llm_error"
            final_response = ""
            error_info = {"kind": e.kind, "message": str(e)}
            break

        usage = getattr(resp, "usage", None)
        if usage and isinstance(usage, dict):
            in_tok = usage.get("input_tokens") or 0
            if in_tok:
                estimate.input_tokens = in_tok
                estimate.message_count = msgs_len_before_call

        asst_msg = assistant_message(content=resp.content, tool_calls=resp.tool_calls)
        messages.append(asst_msg)

        store.append_message(
            session_id=scope.session_id,
            role="assistant",
            content=resp.content,
            tool_calls=resp.tool_calls,
        )

        if resp.tool_calls:
            _execute_tool_calls(
                resp.tool_calls,
                messages,
                store=store,
                tools=tools,
                scope=scope,
                event_sink=event_sink,
                logger=active_logger,
            )
            hooks.run(AFTER_TOOL_CALLS, ctx, on_error=on_hook_error)
            if ctx.stop_reason is not None:
                stopped_reason = ctx.stop_reason
                final_response = ""
                break
        else:
            final_response = resp.content or ""
            stopped_reason = "completed"
            break

    if stopped_reason == "max_iterations":
        active_logger.warning(
            "Agent loop stopped at max_iterations=%d without a final response.",
            max_iterations,
        )

    result: dict[str, Any] = {
        "final_response": final_response,
        "messages": messages,
        "iterations": iterations,
        "session_id": scope.session_id,
        "stopped_reason": stopped_reason,
    }
    if error_info is not None:
        result["error"] = error_info
    result.update(scope.reports)

    return result
