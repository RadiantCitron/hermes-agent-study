"""컨텍스트 자동 압축 턴 훅 (Compression Hook).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py (대화 압축 및 컨텍스트 축약 로직)
- 역할: before_llm 훅 지점에서 현재 메시지 토큰을 추정하고 임계치를 초과했을 때
  중간 메시지들을 요약하여 압축하고 세션 DB 원장과 메모리 메시지 목록을 동기화한다.
"""

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
from hermes_core.agent.turn_hooks import TurnContext
from hermes_core.constants import (
    COMPRESSION_HEAD_MESSAGES,
    COMPRESSION_MAX_PER_RUN,
    COMPRESSION_MIN_TAIL_MESSAGES,
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
)

logger = logging.getLogger(__name__)


class CompressionHook:
    """before_llm 시점에 호출되어 필요 시 메시지 목록을 압축하는 훅."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def __call__(self, ctx: TurnContext) -> None:
        agent = self.agent
        if not agent.compression_enabled:
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
            agent.context_window_tokens,
            agent.compression_threshold_ratio,
        )
        current_tokens = estimate_current_tokens(
            messages=messages,
            last_input_tokens=agent._last_input_tokens,
            last_message_count=agent._last_message_count,
            tools=schema_list,
        )

        if should_compress(current_tokens, threshold) and may_retry_after_reject(
            current_tokens, agent._rejected_at_tokens
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
                agent.llm_client,
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
                    agent.session_db.archive_and_compact(
                        agent.session_id,
                        db_new_msgs,
                        summary_index=summary_idx,
                    )
                    # DB 기록 성공 시에만 메모리 목록 원자적 교체
                    messages[:] = comp_res.messages
                    agent._last_input_tokens = None
                    agent._last_message_count = None
                    agent._rejected_at_tokens = None
                    comp_stats["count"] += 1
                    if comp_res.used_fallback:
                        comp_stats["fallback"] += 1
                    if comp_res.usage:
                        in_tok = comp_res.usage.get("prompt_tokens") or comp_res.usage.get("input_tokens") or 0
                        out_tok = comp_res.usage.get("completion_tokens") or comp_res.usage.get("output_tokens") or 0
                        if in_tok or out_tok:
                            agent.session_db.update_token_counts(
                                agent.session_id, input_tokens=in_tok, output_tokens=out_tok
                            )
                    logger.info(
                        "[Compression] Session %s compressed: %d -> %d tokens (fallback=%s)",
                        agent.session_id,
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
                    agent._rejected_at_tokens = current_tokens
