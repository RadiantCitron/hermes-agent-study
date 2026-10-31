"""컨텍스트 자동 압축 확장 (Compression Extension).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py (대화 압축 및 컨텍스트 축약 로직)
- 역할: before_llm 훅 지점에서 현재 메시지 토큰을 추정하고 임계치를 초과했을 때
  중간 메시지들을 요약하여 압축하고 세션 DB 원장과 메모리 메시지 목록을 동기화한다.
- 불변 원칙: 에이전트를 직접 참조하지 않으며, TurnScope/TurnContext 계약과
  CompressionConfig 팩토리만을 통해 동작한다.
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from hermes_core.agent.compression import (
    compress_messages,
    find_previous_summary,
    split_for_compression,
)
from hermes_core.agent.context_budget import (
    compression_tail_budget,
    compression_threshold,
    estimate_current_tokens,
    may_retry_after_reject,
    should_compress,
)
from hermes_core.agent.turn_hooks import BEFORE_LLM, TurnContext
from hermes_core.constants import (
    COMPRESSION_HEAD_MESSAGES,
    COMPRESSION_MAX_PER_RUN,
    COMPRESSION_MIN_TAIL_MESSAGES,
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
)
from hermes_core.extensions.contract import ExtensionHost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompressionConfig:
    """압축 실행에 필요한 설정 값 객체."""

    enabled: bool
    context_window_tokens: int
    threshold_ratio: float
    llm_client: Any


class CompressionExtension:
    """before_llm 시점에 호출되어 필요 시 메시지 목록을 압축하는 코어 확장."""

    name: str = "compression"

    def __init__(self, config: Callable[[], CompressionConfig]) -> None:
        self._config = config

    def install(self, host: ExtensionHost) -> None:
        """호스트에 before_llm 턴 훅 등록."""
        host.add_turn_hook(BEFORE_LLM, self._before_llm)

    def _before_llm(self, ctx: TurnContext) -> None:
        cfg = self._config()
        if not cfg.enabled:
            return

        run_state = ctx.run_state
        if "compression" not in run_state:
            run_state["compression"] = {
                "attempts": 0,
                "count": 0,
                "fallback": 0,
            }
        comp_stats = run_state["compression"]

        if comp_stats["attempts"] >= COMPRESSION_MAX_PER_RUN:
            return

        messages = ctx.messages
        schema_list = ctx.tools_schema

        threshold = compression_threshold(
            cfg.context_window_tokens,
            cfg.threshold_ratio,
        )

        last_input = ctx.last_input_tokens
        last_count = ctx.last_message_count

        current_tokens = estimate_current_tokens(
            messages=messages,
            last_input_tokens=last_input,
            last_message_count=last_count,
            tools=schema_list,
        )

        session_state = ctx.scope.session_state("compression")
        rejected_at_tokens = session_state.get("rejected_at_tokens")

        if should_compress(current_tokens, threshold) and may_retry_after_reject(
            current_tokens, rejected_at_tokens
        ):
            comp_stats["attempts"] += 1
            tail_budget = compression_tail_budget(threshold)

            prev_sum = find_previous_summary(messages)
            eff_head = 0 if prev_sum is not None else COMPRESSION_HEAD_MESSAGES
            can_split = (
                split_for_compression(
                    messages,
                    head_messages=eff_head,
                    tail_tokens=tail_budget,
                    min_tail_messages=COMPRESSION_MIN_TAIL_MESSAGES,
                )
                is not None
            )

            comp_res = compress_messages(
                messages,
                cfg.llm_client,
                head_messages=COMPRESSION_HEAD_MESSAGES,
                tail_tokens=tail_budget,
                min_tail_messages=COMPRESSION_MIN_TAIL_MESSAGES,
                tools=schema_list,
            )
            if comp_res is not None:
                # DB 저장용 새 메시지 목록 (system 제외)
                sys_offset = 1 if (comp_res.messages and comp_res.messages[0].get("role") == "system") else 0
                db_new_msgs = comp_res.messages[sys_offset:]

                summary_idx = None
                for i, m in enumerate(db_new_msgs):
                    content = m.get("content") or ""
                    # 독립 요약 행인지 판별 (PREFIX로 시작하고 END_MARKER로 끝남)
                    if (
                        content.startswith(SUMMARY_PREFIX)
                        and content.endswith(SUMMARY_END_MARKER)
                        and m.get("role") in ("user", "assistant")
                    ):
                        summary_idx = i
                        break

                try:
                    ctx.replace_transcript(
                        comp_res.messages,
                        summary_index=summary_idx,
                        usage=comp_res.usage,
                    )
                    session_state["rejected_at_tokens"] = None
                    comp_stats["count"] += 1
                    if comp_res.used_fallback:
                        comp_stats["fallback"] += 1
                    ctx.scope.report(
                        "compression",
                        {"count": comp_stats["count"], "fallback": comp_stats["fallback"]},
                    )
                    logger.info(
                        "[Compression] Session %s compressed: %d -> %d tokens (fallback=%s)",
                        ctx.session_id,
                        comp_res.tokens_before,
                        comp_res.tokens_after,
                        comp_res.used_fallback,
                    )
                except Exception as e:
                    logger.warning(
                        "[Compression] archive_and_compact failed: %s. Keeping original messages.",
                        e,
                    )
            else:
                if can_split:
                    session_state["rejected_at_tokens"] = current_tokens
