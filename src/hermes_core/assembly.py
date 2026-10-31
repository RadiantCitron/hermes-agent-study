"""Hermes Core 조립 지점 (Assembly).

기본으로 끼울 기능의 목록과 순서는 이 파일 한 곳이 정한다.
기능을 더하거나 빼려면 default_extensions의 목록을 고친다.
기본 LLM 미들웨어와 호출 기록처 연결도 이 파일이 정한다.
"""

from __future__ import annotations

from typing import Any, Callable, Collection, Optional
import uuid

from hermes_core.agent.compression_hook import CompressionConfig, CompressionExtension
from hermes_core.agent.loop_guard import LoopGuardExtension
from hermes_core.constants import (
    DELEGATE_CHILD_MAX_ITERATIONS,
    LOOP_GUARD_BLOCK_THRESHOLD,
    LOOP_GUARD_WARN_THRESHOLD,
    SUBAGENT_SOURCE,
)
from hermes_core.control.settings import SETTING_SPECS, Settings
from hermes_core.kernel.contract import Extension
from hermes_core.kernel.llm_pipeline import LLMMiddleware, LLMNext, LLMRequest
from hermes_core.kernel.ports import CallRecordable, LLMReply, TranscriptStore
from hermes_core.tools.ports import ChildRunnerPort

RUNTIME_SETTING_KEYS: tuple[str, ...] = (
    "max_iterations",
    "context_window_tokens",
    "compression_enabled",
    "compression_threshold_ratio",
    "breaker_threshold",
)


def apply_runtime_settings(
    agent: Any,
    settings: Settings,
    *,
    changed_keys: Optional[Collection[str]] = None,
) -> None:
    """Settings 스냅샷의 런타임 설정 항목을 에이전트에 반영한다."""
    if changed_keys is not None:
        for k in changed_keys:
            if k not in SETTING_SPECS or SETTING_SPECS[k].mutability != "runtime":
                raise ValueError(f"Setting '{k}' is not runtime-mutable")

    for k, spec in SETTING_SPECS.items():
        val = getattr(settings, k, None)
        try:
            spec.validator(val)
        except Exception as exc:
            raise ValueError(f"Invalid setting value for '{k}': {exc}") from exc

    target_keys = set(changed_keys) if changed_keys is not None else set(RUNTIME_SETTING_KEYS)

    if "max_iterations" in target_keys:
        agent.max_iterations = settings.max_iterations
    if "context_window_tokens" in target_keys:
        agent.context_window_tokens = settings.context_window_tokens
    if "compression_enabled" in target_keys:
        agent.compression_enabled = settings.compression_enabled
    if "compression_threshold_ratio" in target_keys:
        agent.compression_threshold_ratio = settings.compression_threshold_ratio
    if "breaker_threshold" in target_keys:
        agent.set_extension_breaker(settings.breaker_threshold)


def compression_config_of(agent: Any) -> CompressionConfig:
    """에이전트 인스턴스로부터 호출 시점의 압축 설정을 생성한다."""
    return CompressionConfig(
        enabled=agent.compression_enabled,
        context_window_tokens=agent.context_window_tokens,
        threshold_ratio=agent.compression_threshold_ratio,
        llm_client=agent.llm_client,
    )


def default_extensions(
    compression_config: Callable[[], CompressionConfig],
    *,
    loop_guard_warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD,
    loop_guard_block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD,
) -> list[Extension]:
    """기본 확장 목록을 새로 인스턴스화하여 반환한다.

    확장은 설치 뒤 호스트 등 상태를 가지므로 호출할 때마다 새 목록과 새 인스턴스를 반환한다.
    """
    return [
        CompressionExtension(compression_config),
        LoopGuardExtension(
            warn_threshold=loop_guard_warn_threshold,
            block_threshold=loop_guard_block_threshold,
        ),
    ]


def create_agent(settings: Settings, **parts: Any) -> Any:
    """Settings 스냅샷과 추가 부품들로 HermesAgent를 조립하여 반환한다."""
    from hermes_core.agent.agent import HermesAgent

    kwargs: dict[str, Any] = {
        "model": settings.model,
        "max_iterations": settings.max_iterations,
        "context_window_tokens": settings.context_window_tokens,
        "compression_threshold_ratio": settings.compression_threshold_ratio,
        "compression_enabled": settings.compression_enabled,
        "skills_index": settings.skills_index,
    }
    kwargs.update(parts)

    if "extensions" not in kwargs or kwargs["extensions"] is None:
        agent: Any = None
        kwargs["extensions"] = default_extensions(
            lambda: compression_config_of(agent),
            loop_guard_warn_threshold=settings.loop_guard_warn_threshold,
            loop_guard_block_threshold=settings.loop_guard_block_threshold,
        )

    agent = HermesAgent(**kwargs)
    agent.set_extension_breaker(settings.breaker_threshold)
    return agent


def usage_ledger_middleware(store: TranscriptStore) -> LLMMiddleware:
    """세션 토큰 사용량을 원장에 누적하는 LLM 미들웨어."""

    def middleware(request: LLMRequest, next: LLMNext) -> LLMReply:
        resp = next(request)
        usage = getattr(resp, "usage", None)
        if usage and isinstance(usage, dict):
            in_tok = usage.get("input_tokens") or 0
            out_tok = usage.get("output_tokens") or 0
            if in_tok or out_tok:
                store.update_token_counts(
                    request.scope.session_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
        return resp

    return middleware


def default_llm_middlewares(store: TranscriptStore) -> list[LLMMiddleware]:
    """기본 LLM 미들웨어 목록을 반환한다.

    기본으로 끼울 LLM 미들웨어의 목록과 순서는 이 함수 한 곳이 정한다.
    """
    return [usage_ledger_middleware(store)]


def connect_call_recorder(agent: Any) -> bool:
    """에이전트의 LLM 클라이언트가 호출 기록을 지원하면 세션 DB에 연결한다."""
    client = agent.llm_client
    if isinstance(client, CallRecordable) and callable(getattr(client, "set_call_recorder", None)):
        client.set_call_recorder(
            lambda record: agent.session_db.record_llm_call(
                session_id=agent.session_id,
                **record,
            )
        )
        return True
    return False


def run_child_session(
    goal: str,
    context: Optional[str] = None,
    *,
    session_db: Any,
    parent_session_id: str,
    llm_client: Any,
    max_iterations: int = DELEGATE_CHILD_MAX_ITERATIONS,
    dispatcher: Optional[Any] = None,
) -> dict[str, Any]:
    """격리된 자식 에이전트 세션을 생성하고 1회 대화를 실행한다 (D-04, R-2)."""
    from hermes_core.agent.agent import HermesAgent
    from hermes_core.agent.prompt_builder import build_child_system_message
    from hermes_core.tools.dispatcher import create_child_dispatcher

    child_session_id = str(uuid.uuid4())
    caught_exception: Optional[Exception] = None
    run_result: Optional[dict[str, Any]] = None

    try:
        child_agent = HermesAgent(
            session_id=child_session_id,
            session_db=session_db,
            dispatcher=dispatcher if dispatcher is not None else create_child_dispatcher(),
            llm_client=llm_client,
            max_iterations=max_iterations,
            quiet_mode=True,
            system_message=build_child_system_message(context),
            parent_session_id=parent_session_id,
            source=SUBAGENT_SOURCE,
            skip_memory=True,
            skip_context_files=True,
        )
        child_session_id = child_agent.session_id
        run_result = child_agent.run_conversation(goal)
    except Exception as e:
        caught_exception = e

    return {
        "child_session_id": child_session_id,
        "run_result": run_result,
        "caught_exception": caught_exception,
    }


def create_child_runner() -> ChildRunnerPort:
    """기본 자식 실행기(run_child_session)를 반환한다."""
    return run_child_session
